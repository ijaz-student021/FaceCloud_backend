"""Biometric consent, facial-data erasure with verification, retention and export.

"Facial data" here means the biometric identity data FaceCloud derives from
photos: face embeddings, the identity groups built from them, the records of
edits made to those groups, and ZIP exports of a group. Uploaded photos and
enhancement results are the user's own content and are handled by the normal
image deletion flow; face-detection boxes and duplicate-image hashes identify
images, not people, and are left in place.
"""

import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .cleanup import stale_temp_archives
from .models import BiometricConsent, ConsentPurpose, OrganizationExport, PrivacyRequest

logger = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = 1


def current_policy_version():
    return settings.BIOMETRIC_CONSENT_POLICY_VERSION


def _latest_consent(user, purpose):
    return BiometricConsent.objects.filter(user=user, purpose=purpose).first()


def has_active_consent(user, purpose=ConsentPurpose.FACE_IDENTITY):
    """Consent counts only if granted against the current policy version."""
    latest = _latest_consent(user, purpose)
    return bool(latest and latest.granted and latest.policy_version == current_policy_version())


def biometric_processing_allowed(user):
    return (not settings.BIOMETRIC_CONSENT_REQUIRED) or has_active_consent(user)


def consent_status(user):
    purposes = []
    for purpose, label in ConsentPurpose.choices:
        latest = _latest_consent(user, purpose)
        purposes.append(
            {
                "purpose": purpose,
                "label": label,
                "granted": bool(latest and latest.granted),
                "active": has_active_consent(user, purpose),
                "policy_version": latest.policy_version if latest else None,
                "updated_at": latest.created_at if latest else None,
            }
        )
    return {
        "required": bool(settings.BIOMETRIC_CONSENT_REQUIRED),
        "current_policy_version": current_policy_version(),
        "purposes": purposes,
    }


@transaction.atomic
def record_consent(user, purpose, granted, policy_version):
    """Append a consent change; unchanged state is not duplicated."""
    from activity.services import record_activity_event

    if purpose not in ConsentPurpose.values:
        raise ValidationError({"purpose": "Choose a supported consent purpose."})
    version = current_policy_version()
    if granted and policy_version != version:
        raise ValidationError(
            {"policy_version": f"Consent must reference the current policy version ({version})."}
        )
    latest = _latest_consent(user, purpose)
    if latest and latest.granted == granted and latest.policy_version == version:
        return latest, False
    row = BiometricConsent.objects.create(
        user=user, purpose=purpose, granted=granted, policy_version=version
    )
    record_activity_event(
        user,
        "privacy",
        "consent_granted" if granted else "consent_withdrawn",
        object_type="BiometricConsent",
        object_id=row.pk,
        details={"purpose": purpose, "policy_version": version},
    )
    return row, True


def _analyses(user, older_than):
    from identities.models import IdentityAnalysis

    queryset = IdentityAnalysis.objects.filter(owner=user)
    return queryset.filter(created_at__lt=older_than) if older_than else queryset


def facial_data_counts(user, *, older_than=None):
    """Counts of stored biometric data; scoped to old analyses for retention."""
    from identities.models import (
        IdentityChange, IdentityFace, IdentityGroup, IdentityGroupExport, IdentityMembership,
    )

    analyses = _analyses(user, older_than)
    counts = {
        "analyses": analyses.count(),
        "faces": IdentityFace.objects.filter(analysis__in=analyses).count(),
        "memberships": IdentityMembership.objects.filter(identity_face__analysis__in=analyses).count(),
    }
    if older_than is None:
        counts["groups"] = IdentityGroup.objects.filter(owner=user).count()
        counts["changes"] = IdentityChange.objects.filter(owner=user).count()
        counts["exports"] = IdentityGroupExport.objects.filter(owner=user).count()
    return counts


def _stored_export_files(user):
    """Group-export ZIPs still on disk for this user (they contain face photos)."""
    try:
        _directories, files = default_storage.listdir(f"users/{user.pk}/identity-exports")
    except (FileNotFoundError, OSError):
        return []
    return files


def erase_facial_data(user, *, older_than=None, trigger=PrivacyRequest.Trigger.USER, record=True):
    """Delete biometric data, then prove it is gone.

    ``older_than=None`` erases everything for the user (identity groups and
    their edit history and exports included). With a cutoff, only analyses
    created before it are removed; groups are recomputed, or removed if they
    lose every member.
    """
    from activity.services import record_activity_event
    from identities.models import IdentityChange, IdentityGroup, IdentityMembership
    from identities.services import _refresh_group

    with transaction.atomic():
        deleted = facial_data_counts(user, older_than=older_than)
        analyses = _analyses(user, older_than)
        affected_group_ids = set(
            IdentityMembership.objects.filter(
                identity_face__analysis__in=analyses, identity_group__isnull=False
            ).values_list("identity_group_id", flat=True)
        )
        analyses.delete()
        groups_removed = 0
        if older_than is None:
            IdentityChange.objects.filter(owner=user).delete()
            groups_removed = IdentityGroup.objects.filter(owner=user).count()
            IdentityGroup.objects.filter(owner=user).delete()
        else:
            for group in IdentityGroup.objects.filter(pk__in=affected_group_ids):
                if group.memberships.exists():
                    _refresh_group(group)
                else:
                    IdentityChange.objects.filter(group=group).delete()
                    group.delete()
                    groups_removed += 1

    remaining = facial_data_counts(user, older_than=older_than)
    leftover_files = _stored_export_files(user) if older_than is None else []
    verified = all(value == 0 for value in remaining.values()) and not leftover_files
    result = {
        "scope": "all" if older_than is None else "older_than",
        "deleted": deleted,
        "groups_removed": groups_removed,
        "remaining": remaining,
        "leftover_export_files": len(leftover_files),
        "verified": verified,
    }
    if record:
        request = PrivacyRequest.objects.create(
            user=user,
            kind=PrivacyRequest.Kind.FACIAL_ERASURE,
            trigger=trigger,
            status=PrivacyRequest.Status.COMPLETED if verified else PrivacyRequest.Status.FAILED,
            summary=result,
            verified=verified,
        )
        result["request_id"] = request.pk
        record_activity_event(
            user, "privacy", "facial_data_erased", object_type="PrivacyRequest",
            object_id=request.pk, details={"verified": verified, "trigger": trigger},
        )
    if not verified:
        logger.error("Facial-data erasure for user_id=%s could not be verified.", user.pk)
    return result


def purge_expired_facial_data(now=None):
    """Apply FACIAL_DATA_RETENTION_DAYS across all users; one audit record per run."""
    from identities.models import IdentityAnalysis

    days = settings.FACIAL_DATA_RETENTION_DAYS
    if days <= 0:
        return {"enabled": False, "retention_days": days}
    cutoff = (now or timezone.now()) - timedelta(days=days)
    owner_ids = list(
        IdentityAnalysis.objects.filter(created_at__lt=cutoff)
        .values_list("owner_id", flat=True)
        .distinct()
    )
    totals = Counter()
    unverified = 0
    for user in get_user_model().objects.filter(pk__in=owner_ids):
        result = erase_facial_data(
            user, older_than=cutoff, trigger=PrivacyRequest.Trigger.RETENTION, record=False
        )
        totals.update(result["deleted"])
        totals["groups_removed"] += result["groups_removed"]
        unverified += 0 if result["verified"] else 1
    summary = {
        "enabled": True,
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "users_affected": len(owner_ids),
        "deleted": dict(totals),
        "unverified_users": unverified,
    }
    if owner_ids:
        PrivacyRequest.objects.create(
            user=None,
            kind=PrivacyRequest.Kind.RETENTION_PURGE,
            trigger=PrivacyRequest.Trigger.RETENTION,
            status=PrivacyRequest.Status.COMPLETED if not unverified else PrivacyRequest.Status.FAILED,
            summary=summary,
            verified=unverified == 0,
        )
    return summary


def _capped(queryset, fields, limit):
    total = queryset.count()
    rows = list(queryset.values(*fields)[:limit])
    return rows, total > limit


def build_privacy_export(user):
    """The user's own data as JSON-serialisable dicts.

    Face embeddings and identity centroids are biometric templates: they are
    reported as counts, never exported, so a stolen export cannot be used to
    recognise the person.
    """
    from accounts.models import AuthEvent
    from activity.models import ActivityEvent
    from enhancement.models import ImageEnhancementResult
    from identities.models import IdentityFace, IdentityGroup
    from uploads.models import ImageUpload

    limit = settings.PRIVACY_EXPORT_MAX_ROWS
    truncated = {}

    def collect(name, queryset, fields):
        rows, was_truncated = _capped(queryset, fields, limit)
        truncated[name] = was_truncated
        return rows

    images = collect(
        "images",
        ImageUpload.all_objects.filter(owner=user).order_by("-created_at"),         # everything held about the person, the trash included
        ("id", "original_name", "file_size", "width", "height", "status", "created_at", "trashed_at", "gps_lat", "gps_lon"),
    )
    enhancement_rows = collect(
        "enhancements",
        ImageEnhancementResult.objects.filter(owner=user).order_by("-created_at"),
        ("id", "image_id", "requested_level", "requested_style", "applied_level", "status", "created_at"),
    )
    groups = collect(
        "identity_groups",
        IdentityGroup.objects.filter(owner=user).order_by("display_name"),
        ("id", "display_name", "state", "member_count", "created_at"),
    )
    auth_events = collect(
        "auth_events",
        AuthEvent.objects.filter(user=user).order_by("-created_at"),
        ("event", "identifier", "ip_address", "created_at"),
    )
    activity = collect(
        "activity_events",
        ActivityEvent.objects.filter(user=user).order_by("-created_at"),
        ("category", "event", "object_type", "object_id", "created_at"),
    )
    consents = collect(
        "consents",
        BiometricConsent.objects.filter(user=user),
        ("purpose", "granted", "policy_version", "created_at"),
    )
    from enhancement.models import AdvisorConsent, AdvisorRecommendation

    advisor_consents = collect(
        "external_ai_consents",
        AdvisorConsent.objects.filter(user=user),
        ("granted", "policy_version", "created_at"),
    )
    advisor_recommendations = collect(
        "advisor_recommendations",
        AdvisorRecommendation.objects.filter(owner=user).order_by("-created_at"),
        ("id", "enhancement_result_id", "intent", "model", "status", "origin", "action", "fallback_reason",
         "input_tokens", "output_tokens", "created_at"),
    )
    requests = collect(
        "privacy_requests",
        PrivacyRequest.objects.filter(user=user),
        ("kind", "trigger", "status", "verified", "created_at"),
    )
    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": timezone.now(),
        "account": {
            "id": user.pk,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "email_verified": user.is_email_verified,
            "date_joined": user.date_joined,
            "last_login": user.last_login,
            "profile": {
                "display_name": user.display_name,
                "organization_name": user.organization_name,
                "job_title": user.job_title,
                "phone": user.phone,
                "country": user.country,
                "has_avatar": bool(user.avatar),
            },
        },
        "consents": consents,
        "external_ai_consents": advisor_consents,
        "advisor_recommendations": advisor_recommendations,
        "camera_sessions": [
            {"id": str(session.pk), "device_label": session.device_label, "settings": session.settings,
             "started_at": session.started_at, "ended_at": session.ended_at,
             "snapshot_image_ids": list(session.snapshots.values_list("image_id", flat=True))}
            for session in user.camera_sessions.order_by("-started_at")
        ],
        "images": images,
        "enhancements": enhancement_rows,
        "identity": {
            "groups": groups,
            "face_records": IdentityFace.objects.filter(owner=user).count(),
            "note": (
                "Face embeddings and group centroids are biometric templates and "
                "are not included in exports."
            ),
        },
        "auth_events": auth_events,
        "activity_events": activity,
        "privacy_requests": requests,
        "truncated": truncated,
        "row_limit": limit,
    }


def record_export_request(user):
    request = PrivacyRequest.objects.create(
        user=user,
        kind=PrivacyRequest.Kind.EXPORT,
        trigger=PrivacyRequest.Trigger.USER,
        status=PrivacyRequest.Status.COMPLETED,
        summary={"format_version": EXPORT_FORMAT_VERSION},
        verified=True,
    )
    from activity.services import record_activity_event

    record_activity_event(
        user, "privacy", "data_exported", object_type="PrivacyRequest", object_id=request.pk
    )
    return request


def build_organization_export_payload(organization):
    """An organization's own data as JSON-serialisable dicts.

    Covers organization-*owned* content only -- batches, images, reviews,
    membership, its own audit trail. Deliberately excludes anything keyed to
    an individual member's personal account (consent, login, activity
    history): organization membership must never expose a member's personal
    content, the same rule every other org-scoped view in this codebase
    follows. Biometric templates (protected_embedding/protected_centroid)
    are never included, matching build_privacy_export's guarantee.
    """
    from duplicates.models import DuplicateMatch
    from identities.models import IdentityGroup, IdentityMembership
    from moderation.models import ModeratorReview
    from organizations.models import OrganizationAuditEvent, OrganizationInvitation, OrganizationMembership
    from quality_control.models import ImageQualityDecision
    from uploads.models import ImageUpload, UploadBatch

    limit = settings.ORGANIZATION_EXPORT_MAX_ROWS
    truncated = {}
    row_counts = {}

    def collect(name, queryset, fields):
        rows, was_truncated = _capped(queryset, fields, limit)
        truncated[name] = was_truncated
        row_counts[name] = len(rows)
        return rows

    members = collect(
        "members",
        OrganizationMembership.objects.filter(organization=organization).select_related("user"),
        ("id", "user__id", "user__username", "user__email", "role", "status", "created_at"),
    )
    invitations = collect(
        "invitations",
        OrganizationInvitation.objects.filter(organization=organization),
        ("id", "email", "role", "status", "expires_at", "created_at"),
    )
    batches = collect(
        "batches",
        UploadBatch.objects.filter(organization=organization),
        ("id", "owner_id", "upload_type", "status", "processing_status", "created_at"),
    )
    images = collect(
        "images",
        ImageUpload.all_objects.filter(batch__organization=organization),
        ("id", "batch_id", "original_name", "status", "created_at", "trashed_at"),
    )
    quality_decisions = collect(
        "quality_decisions",
        ImageQualityDecision.objects.filter(image__batch__organization=organization),
        ("id", "image_id", "status", "decision", "overall_score", "created_at"),
    )
    duplicate_matches = collect(
        "duplicate_matches",
        DuplicateMatch.objects.filter(source_image__batch__organization=organization),
        ("id", "source_image_id", "matched_image_id", "match_type", "review_status", "similarity_score", "created_at"),
    )
    identity_groups = collect(
        "identity_groups",
        IdentityGroup.objects.filter(organization=organization),
        ("id", "display_name", "state", "member_count", "created_at"),
    )
    identity_memberships = collect(
        "identity_memberships",
        IdentityMembership.objects.filter(identity_group__organization=organization),
        ("id", "identity_face_id", "identity_group_id", "state", "similarity_score", "created_at"),
    )
    moderator_reviews = collect(
        "moderator_reviews",
        ModeratorReview.objects.filter(organization=organization),
        ("id", "image_id", "source", "status", "ai_recommendation", "decision_reason", "decided_at", "created_at"),
    )
    audit_events = collect(
        "audit_events",
        OrganizationAuditEvent.objects.filter(organization=organization),
        ("id", "actor_id", "action", "target_type", "target_id", "reason", "created_at"),
    )
    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": timezone.now(),
        "organization": {
            "id": organization.pk,
            "name": organization.name,
            "slug": organization.slug,
            "status": organization.status,
            "plan": organization.plan,
            "created_at": organization.created_at,
        },
        "members": members,
        "invitations": invitations,
        "batches": batches,
        "images": images,
        "quality_decisions": quality_decisions,
        "duplicate_matches": duplicate_matches,
        "identity": {
            "groups": identity_groups,
            "memberships": identity_memberships,
            "note": (
                "Face embeddings and group centroids are biometric templates and "
                "are not included in exports."
            ),
        },
        "moderator_reviews": moderator_reviews,
        "audit_events": audit_events,
        "row_counts": row_counts,
        "truncated": truncated,
        "row_limit": limit,
    }


def organization_export_expiration_time():
    return timezone.now() + timedelta(hours=settings.ORGANIZATION_EXPORT_TTL_HOURS)


def enqueue_organization_export(*, organization, requested_by):
    """Create (or reuse) the organization's in-flight export and queue the build.

    Mirrors downloads.services.enqueue_download_package: one active
    (queued/generating) export per organization, dispatched only after the
    row commits so the task can never fire before its own row exists.
    """
    from .tasks import generate_organization_export_task

    active_filters = {
        "organization": organization,
        "status__in": [OrganizationExport.Status.QUEUED, OrganizationExport.Status.GENERATING],
    }
    try:
        with transaction.atomic():
            active = OrganizationExport.objects.select_for_update().filter(**active_filters).first()
            if active:
                return active, False
            task_id = uuid.uuid4().hex
            export = OrganizationExport.objects.create(
                organization=organization, requested_by=requested_by,
                status=OrganizationExport.Status.QUEUED, task_id=task_id,
            )

            def dispatch():
                try:
                    generate_organization_export_task.apply_async(args=[export.pk], task_id=task_id)
                except Exception:
                    logger.exception("Could not queue organization export_id=%s.", export.pk)
                    OrganizationExport.objects.filter(pk=export.pk).update(
                        status=OrganizationExport.Status.FAILED,
                        failure_reason="The export queue is unavailable.",
                    )

            transaction.on_commit(dispatch)
    except IntegrityError:
        active = OrganizationExport.objects.filter(**active_filters).first()
        if active:
            return active, False
        raise
    export.refresh_from_db()
    return export, True


def process_organization_export(export_id, task_id=""):
    with transaction.atomic():
        export = OrganizationExport.objects.select_for_update().filter(pk=export_id).first()
        if export is None:
            return None
        if export.status == OrganizationExport.Status.READY:
            return export
        if export.task_id and task_id and export.task_id != task_id:
            return export
        export.status = OrganizationExport.Status.GENERATING
        export.failure_reason = ""
        export.save(update_fields=["status", "failure_reason", "updated_at"])

    try:
        export = OrganizationExport.objects.select_related("organization").get(pk=export_id)
        payload = build_organization_export_payload(export.organization)
        content = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        checksum = hashlib.sha256(content).hexdigest()
        with transaction.atomic():
            locked = OrganizationExport.objects.select_for_update().filter(pk=export_id).first()
            if locked is None:
                return None
            if locked.task_id and task_id and locked.task_id != task_id:
                return locked
            if locked.export_file:
                locked.export_file.delete(save=False)
            locked.export_file.save(
                f"organization-export-{export_id}.json", ContentFile(content), save=False,
            )
            locked.file_size = len(content)
            locked.file_checksum_sha256 = checksum
            locked.row_counts = payload["row_counts"]
            locked.truncated = payload["truncated"]
            locked.status = OrganizationExport.Status.READY
            locked.failure_reason = ""
            locked.generated_at = timezone.now()
            locked.expires_at = organization_export_expiration_time()
            locked.save()
        from organizations.models import OrganizationAuditEvent
        from organizations.services import record_audit_event
        record_audit_event(
            locked.organization, locked.requested_by, OrganizationAuditEvent.Action.ORGANIZATION_DATA_EXPORTED,
            target_type="OrganizationExport", target_id=locked.pk,
        )
        return locked
    except Exception:
        logger.exception("Could not generate organization export_id=%s.", export_id)
        with transaction.atomic():
            failed = OrganizationExport.objects.select_for_update().filter(pk=export_id).first()
            if failed is None:
                return None
            if failed.export_file:
                failed.export_file.delete(save=False)
            failed.export_file = ""
            failed.status = OrganizationExport.Status.FAILED
            failed.failure_reason = "The organization export could not be generated safely."
            failed.file_size = 0
            failed.file_checksum_sha256 = ""
            failed.expires_at = None
            failed.save()
            return failed


def expire_organization_export_if_needed(export, now=None):
    now = now or timezone.now()
    if (
        export.status == OrganizationExport.Status.READY
        and export.expires_at
        and export.expires_at <= now
    ):
        if export.export_file:
            export.export_file.delete(save=False)
        export.status = OrganizationExport.Status.EXPIRED
        export.export_file = ""
        export.file_size = 0
        export.file_checksum_sha256 = ""
        export.save(update_fields=[
            "status", "export_file", "file_size", "file_checksum_sha256", "updated_at",
        ])
        return True
    return False


def expire_due_organization_exports(now=None):
    now = now or timezone.now()
    queryset = OrganizationExport.objects.filter(
        status=OrganizationExport.Status.READY, expires_at__lte=now,
    )
    expired = 0
    for export in queryset.iterator():
        expired += int(expire_organization_export_if_needed(export, now=now))
    return expired


def _embedding_fields():
    from identities.models import IdentityFace, IdentityGroup

    return (
        ("faces", IdentityFace, "protected_embedding"),
        ("groups", IdentityGroup, "protected_centroid"),
    )


def embedding_encryption_status():
    """How stored biometric templates are protected, by key. Never decrypts."""
    from identities.storage import current_key_id, payload_format

    current = current_key_id()
    counts = Counter()
    for _label, model, field in _embedding_fields():
        stored = model.objects.exclude(**{field: b""}).values_list(field, flat=True)
        for payload in stored.iterator(chunk_size=1000):
            kind, key_id = payload_format(payload)
            if kind == "v2" and key_id == current:
                counts["current_key"] += 1
            elif kind == "v2":
                counts["older_key"] += 1
            elif kind == "v1":
                counts["legacy_v1"] += 1
            else:
                counts["unrecognized"] += 1
    counts = {name: counts.get(name, 0) for name in ("current_key", "older_key", "legacy_v1", "unrecognized")}
    counts["rotation_needed"] = bool(counts["older_key"] or counts["legacy_v1"])
    return counts


def reencrypt_identity_embeddings(*, dry_run=False, batch_size=500, record=True):
    """Re-protect every stored template under the current key and format.

    Idempotent and safe to interrupt: each batch commits on its own, rows are
    locked while rewritten, and ``.update()`` is used so ``updated_at`` (which
    orders identity-group candidates) is not disturbed. A record that cannot be
    decrypted is counted and left exactly as it is.
    """
    from identities.storage import is_current_payload, protect_embedding, reveal_embedding

    stats = {"dry_run": dry_run}
    for label, model, field in _embedding_fields():
        section = Counter()
        last_pk = 0
        while True:
            with transaction.atomic():
                rows = list(
                    model.objects.select_for_update()
                    .filter(pk__gt=last_pk)
                    .order_by("pk")[:batch_size]
                )
                for row in rows:
                    last_pk = row.pk
                    payload = bytes(getattr(row, field))
                    if not payload:
                        section["empty"] += 1
                        continue
                    section["total"] += 1
                    if is_current_payload(payload):
                        section["already_current"] += 1
                        continue
                    try:
                        vector = reveal_embedding(payload, row.embedding_dimension)
                    except ValidationError:
                        section["failed"] += 1
                        logger.error("Could not decrypt %s pk=%s during re-encryption.", label, row.pk)
                        continue
                    section["upgraded"] += 1
                    if not dry_run:
                        model.objects.filter(pk=row.pk).update(**{field: protect_embedding(vector)})
            if not rows:
                break
        stats[label] = {
            name: section.get(name, 0)
            for name in ("total", "already_current", "upgraded", "failed", "empty")
        }
    failed = sum(stats[label]["failed"] for label in ("faces", "groups"))
    upgraded = sum(stats[label]["upgraded"] for label in ("faces", "groups"))
    stats["failed"] = failed
    if record and not dry_run and (upgraded or failed):
        PrivacyRequest.objects.create(
            user=None,
            kind=PrivacyRequest.Kind.KEY_ROTATION,
            trigger=PrivacyRequest.Trigger.OPERATOR,
            status=PrivacyRequest.Status.FAILED if failed else PrivacyRequest.Status.COMPLETED,
            summary=stats,
            verified=failed == 0,
        )
    return stats


def audit_report(now=None):
    """Aggregate, non-personal privacy posture for administrators."""
    now = now or timezone.now()
    since = now - timedelta(days=30)
    latest_by_user = {}
    for row in BiometricConsent.objects.order_by("user_id", "purpose", "-created_at", "-id").values(
        "user_id", "purpose", "granted", "policy_version"
    ):
        latest_by_user.setdefault((row["user_id"], row["purpose"]), row)
    current = current_policy_version()
    recent = PrivacyRequest.objects.filter(created_at__gte=since)
    by_kind = {}
    for row in recent.values("kind", "status"):
        bucket = by_kind.setdefault(row["kind"], {"completed": 0, "failed": 0})
        bucket[row["status"]] = bucket.get(row["status"], 0) + 1
    last_purge = (
        PrivacyRequest.objects.filter(kind=PrivacyRequest.Kind.RETENTION_PURGE)
        .values("created_at", "verified", "summary")
        .first()
    )
    return {
        "generated_at": now,
        "consent": {
            "required": bool(settings.BIOMETRIC_CONSENT_REQUIRED),
            "current_policy_version": current,
            "users_with_active_consent": sum(
                1 for row in latest_by_user.values() if row["granted"] and row["policy_version"] == current
            ),
            "users_with_withdrawn_consent": sum(1 for row in latest_by_user.values() if not row["granted"]),
            "users_needing_reconsent": sum(
                1 for row in latest_by_user.values() if row["granted"] and row["policy_version"] != current
            ),
        },
        "requests_last_30_days": by_kind,
        "unverified_erasures_total": PrivacyRequest.objects.filter(
            kind__in=[PrivacyRequest.Kind.FACIAL_ERASURE, PrivacyRequest.Kind.RETENTION_PURGE],
            verified=False,
        ).count(),
        "temporary_files": {
            "stale_temp_archives": len(stale_temp_archives()),
            "max_age_hours": settings.TEMP_FILE_MAX_AGE_HOURS,
            "orphan_media_deletion_enabled": settings.ORPHAN_MEDIA_CLEANUP_ENABLED,
        },
        "retention": {
            "days": settings.FACIAL_DATA_RETENTION_DAYS,
            "enabled": settings.FACIAL_DATA_RETENTION_DAYS > 0,
            "last_purge": last_purge,
        },
        "encryption": {
            "identity_embeddings": "AES-256-GCM with HKDF-derived keys and key ids (legacy FCID1 records are read and upgraded)",
            "current_key_id": settings.IDENTITY_EMBEDDING_KEY_ID,
            "previous_keys_configured": len(settings.IDENTITY_EMBEDDING_PREVIOUS_KEYS),
            "dedicated_embedding_key_configured": settings.IDENTITY_EMBEDDING_KEY != settings.SECRET_KEY,
            "stored_records": embedding_encryption_status(),
            "uploaded_images_encrypted_at_rest": False,
        },
    }
