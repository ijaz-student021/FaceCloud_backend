import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound

from activity.services import record_activity_event

from .models import ShareLink, ShareLinkAccess


class LinkGone(APIException):
    status_code = status.HTTP_410_GONE
    default_code = "link_gone"
    default_detail = "This link is no longer available."


class TooManyShareLinks(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "too_many_share_links"
    default_detail = "You have too many active share links."


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def active_links(owner, now=None):
    return ShareLink.objects.filter(
        owner=owner, revoked_at__isnull=True, expires_at__gt=now or timezone.now(),
    )


def _new_link(*, owner, ttl_hours, label, watermark, cap=None, **target):
    """Shared by every kind of link: the per-user limit, the token, the audit event."""
    limit = settings.SHARE_LINK_MAX_ACTIVE_PER_USER
    if limit and active_links(owner).count() >= limit:
        raise TooManyShareLinks(
            f"You already have {limit} active share links. Revoke one or wait for it to expire."
        )
    ttl = ttl_hours or settings.SHARE_LINK_DEFAULT_TTL_HOURS
    expires_at = timezone.now() + timedelta(hours=ttl)
    if cap is not None:
        expires_at = min(expires_at, cap)
    token = secrets.token_urlsafe(32)
    link = ShareLink.objects.create(
        owner=owner, token_hash=hash_token(token), label=label,
        watermark=watermark or {}, expires_at=expires_at, **target,
    )
    record_activity_event(
        owner, "share", "share_link_created", object_type="ShareLink", object_id=link.pk,
        details={"expires_at": link.expires_at.isoformat(), "watermarked": bool(watermark)},
    )
    return link, token


def create_share_link(*, owner, result, edit=None, ttl_hours=None, label="", watermark=None):
    """Create a link and return ``(link, raw_token)``; the token is never stored."""
    return _new_link(
        owner=owner, ttl_hours=ttl_hours, label=label, watermark=watermark,
        enhancement_result=result, edit_version=edit,
    )


def create_export_share_link(*, owner, export, ttl_hours=None, label=""):
    """A link to a person folder's finished ZIP; it never outlives the ZIP itself."""
    return _new_link(
        owner=owner, ttl_hours=ttl_hours, label=label, watermark=None,
        cap=export.expires_at, identity_export=export,
    )


def revoke_share_link(link):
    if link.revoked_at is None:
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
        record_activity_event(
            link.owner, "share", "share_link_revoked",
            object_type="ShareLink", object_id=link.pk,
        )
    return link


def resolve_public_link(token):
    """Find a link by its raw token, or raise 404 (unknown) / 410 (no longer valid).

    Tokens are 256-bit random values, so telling "never existed" from "expired"
    reveals nothing an attacker could use, and the distinction helps real users.
    """
    link = (
        ShareLink.objects.select_related(
            "owner", "enhancement_result", "enhancement_result__image", "edit_version",
            "identity_export",
        )
        .filter(token_hash=hash_token(token or ""))
        .first()
    )
    if link is None:
        raise NotFound("This link does not exist.")
    owner = link.owner
    if link.state() != ShareLink.State.ACTIVE or not owner.is_active or owner.is_suspended:
        raise LinkGone()
    if link.identity_export_id and not export_is_servable(link.identity_export):
        raise LinkGone()
    return link


def export_is_servable(export):
    """A folder ZIP can be served while it is ready, unexpired and still stored."""
    from identities.models import IdentityGroupExport

    if export.status != IdentityGroupExport.Status.READY or not export.export_file:
        return False
    if export.expires_at and export.expires_at <= timezone.now():
        return False
    return export.export_file.storage.exists(export.export_file.name)


def shared_file(link):
    """``(file_field, output_format, filename, width, height)`` for what the link shows."""
    if link.identity_export_id:
        export = link.identity_export
        return export.export_file, "zip", export.display_name or "photos.zip", None, None
    edit = link.edit_version
    if edit is not None:
        return (
            edit.edited_file, edit.output_format, edit.output_name,
            edit.output_width, edit.output_height,
        )
    result = link.enhancement_result
    return (
        result.enhanced_file, result.output_format, result.output_name,
        result.output_width, result.output_height,
    )


def record_access(link, user_agent=""):
    with transaction.atomic():
        ShareLinkAccess.objects.create(link=link, user_agent=(user_agent or "")[:200])
        ShareLink.objects.filter(pk=link.pk).update(
            access_count=F("access_count") + 1, last_accessed_at=timezone.now(),
        )


def cleanup_expired_links(now=None):
    """Delete links that ended more than SHARE_LINK_RETENTION_DAYS ago."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.SHARE_LINK_RETENTION_DAYS)
    ended = ShareLink.objects.filter(expires_at__lt=cutoff) | ShareLink.objects.filter(
        revoked_at__lt=cutoff,
    )
    _, per_model = ended.delete()
    return {"deleted": per_model.get("sharing.ShareLink", 0)}
