"""Security alert rules: repeated failed logins, authorization failures, rate limits.

Thresholds and the window are settings (SECURITY_ALERT_*). ``evaluate_rules``
runs every five minutes (Celery Beat) and before the alert list is served.
For each rule it counts events per subject in the window; at or above the
threshold it opens an alert or updates the open one. Events from before a
subject's latest acknowledgement are not counted again, so acknowledging a
burst does not immediately reopen it; a new burst opens a new alert.
"""
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from accounts.models import AuthEvent

from .models import SecurityAlert, SecurityEvent

EVIDENCE_LIMIT = 50
Rule = SecurityAlert.Rule


def _thresholds():
    return {
        Rule.FAILED_LOGIN_ACCOUNT: settings.SECURITY_ALERT_FAILED_LOGIN_THRESHOLD,
        Rule.FAILED_LOGIN_ADDRESS: settings.SECURITY_ALERT_FAILED_LOGIN_THRESHOLD,
        Rule.AUTHORIZATION_FAILURES: settings.SECURITY_ALERT_AUTHORIZATION_FAILURE_THRESHOLD,
        Rule.RATE_LIMIT_VIOLATIONS: settings.SECURITY_ALERT_RATE_LIMIT_THRESHOLD,
    }


# -- recording ----------------------------------------------------------------

def record_refused_request(request, status_code):
    """Record a 403 or 429 response; never raises, at most one row per subject per second."""
    try:
        from accounts.services import get_client_ip

        kind = (SecurityEvent.Kind.RATE_LIMITED if status_code == 429
                else SecurityEvent.Kind.AUTHORIZATION_FAILURE)
        user = getattr(request, "user", None)
        user = user if user is not None and user.is_authenticated else None
        ip_address = get_client_ip(request)
        subject = f"user:{user.pk}" if user else f"ip:{ip_address}"
        second = int(timezone.now().timestamp())
        if not cache.add(f"security-event:{kind}:{subject}:{second}", 1, timeout=5):
            return
        SecurityEvent.objects.create(
            kind=kind, subject=subject[:128], user=user, ip_address=ip_address,
            method=request.method[:8], path=request.path[:255], status_code=status_code,
        )
    except Exception:  # recording must never change the response
        import logging

        logging.getLogger(__name__).exception("Could not record a refused request.")


# -- evaluation ---------------------------------------------------------------

def _acknowledged_since(rule):
    """``{subject: latest acknowledgement time}`` for the rule."""
    latest = {}
    for subject, moment in SecurityAlert.objects.filter(
        rule=rule, status=SecurityAlert.Status.ACKNOWLEDGED,
    ).values_list("subject", "acknowledged_at"):
        if moment and (subject not in latest or moment > latest[subject]):
            latest[subject] = moment
    return latest


def _failed_login_events(since):
    return [
        {"id": event.pk, "source": "auth", "at": event.created_at, "identifier": event.identifier,
         "ip_address": event.ip_address, "detail": (event.details or {}).get("reason", "")}
        for event in AuthEvent.objects.filter(event="login_failed", created_at__gte=since).order_by("-created_at")
    ]


def _refused_events(kind, since):
    return [
        {"id": event.pk, "source": "security", "at": event.created_at, "subject": event.subject,
         "ip_address": event.ip_address, "detail": f"{event.method} {event.path} -> {event.status_code}"}
        for event in SecurityEvent.objects.filter(kind=kind, created_at__gte=since).order_by("-created_at")
    ]


def _grouped(events, key):
    groups = defaultdict(list)
    for event in events:
        value = key(event)
        if value:
            groups[value].append(event)
    return groups


def _severity(count, threshold):
    return SecurityAlert.Severity.HIGH if count >= 3 * threshold else SecurityAlert.Severity.WARNING


def _evidence(events):
    return [{**event, "at": event["at"].isoformat()} for event in events[:EVIDENCE_LIMIT]]


def evaluate_rules(now=None):
    """Open or update alerts; returns the number of alerts opened."""
    now = now or timezone.now()
    window = settings.SECURITY_ALERT_WINDOW_MINUTES
    since = now - timedelta(minutes=window)
    thresholds = _thresholds()
    logins = _failed_login_events(since)
    candidates = {
        Rule.FAILED_LOGIN_ACCOUNT: _grouped(logins, lambda e: f"account:{e['identifier'].lower()}" if e["identifier"] else None),
        Rule.FAILED_LOGIN_ADDRESS: _grouped(logins, lambda e: f"ip:{e['ip_address']}" if e["ip_address"] else None),
        Rule.AUTHORIZATION_FAILURES: _grouped(
            _refused_events(SecurityEvent.Kind.AUTHORIZATION_FAILURE, since), lambda e: e["subject"]),
        Rule.RATE_LIMIT_VIOLATIONS: _grouped(
            _refused_events(SecurityEvent.Kind.RATE_LIMITED, since), lambda e: e["subject"]),
    }
    opened = 0
    for rule, groups in candidates.items():
        acknowledged = _acknowledged_since(rule)
        threshold = thresholds[rule]
        for subject, events in groups.items():
            cutoff = acknowledged.get(subject)
            if cutoff:
                events = [event for event in events if event["at"] > cutoff]
            if len(events) < threshold:
                continue
            with transaction.atomic():
                alert = SecurityAlert.objects.select_for_update().filter(
                    rule=rule, subject=subject, status=SecurityAlert.Status.OPEN,
                ).first()
                fields = {
                    "event_count": len(events), "threshold": threshold, "window_minutes": window,
                    "severity": _severity(len(events), threshold), "last_seen_at": events[0]["at"],
                }
                if alert is None:
                    SecurityAlert.objects.create(
                        rule=rule, subject=subject[:160], first_seen_at=events[-1]["at"],
                        evidence=_evidence(events), **fields,
                    )
                    opened += 1
                else:
                    known = {(item["source"], item["id"]) for item in alert.evidence}
                    new = [item for item in _evidence(events) if (item["source"], item["id"]) not in known]
                    for name, value in fields.items():
                        setattr(alert, name, value)
                    alert.evidence = (new + alert.evidence)[:EVIDENCE_LIMIT]
                    alert.save()
    return opened


def acknowledge(alert, user, note):
    """Mark an open alert handled. Nothing is deleted."""
    with transaction.atomic():
        locked = SecurityAlert.objects.select_for_update().get(pk=alert.pk)
        if locked.status != SecurityAlert.Status.OPEN:
            return locked, False
        locked.status = SecurityAlert.Status.ACKNOWLEDGED
        locked.acknowledged_by = user
        locked.acknowledged_at = timezone.now()
        locked.acknowledgement_note = note
        locked.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "acknowledgement_note",
                                   "updated_at"])
    return locked, True


def purge_old_events(now=None):
    cutoff = (now or timezone.now()) - timedelta(days=settings.SECURITY_EVENT_RETENTION_DAYS)
    return SecurityEvent.objects.filter(created_at__lt=cutoff).delete()[0]
