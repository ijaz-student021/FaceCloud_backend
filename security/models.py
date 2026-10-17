from django.conf import settings
from django.db import models


class SecurityEvent(models.Model):
    """A refused request: 403 (authorization failure) or 429 (rate limit).

    Failed logins are not duplicated here; they are accounts.AuthEvent rows
    ("login_failed"). At most one row per subject per second is written, so a
    flood of refused requests cannot become a flood of writes. Deleted after
    SECURITY_EVENT_RETENTION_DAYS; alerts keep their own copy of the evidence.
    """

    class Kind(models.TextChoices):
        AUTHORIZATION_FAILURE = "authorization_failure", "Authorization failure (403)"
        RATE_LIMITED = "rate_limited", "Rate limit exceeded (429)"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    subject = models.CharField(max_length=128, help_text='"user:<id>" or "ip:<address>".')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=255, blank=True)
    status_code = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["kind", "subject", "created_at"])]


class SecurityAlert(models.Model):
    """A rule crossed its threshold for one subject.

    One open alert per (rule, subject): further events update it. Acknowledging
    records who, when and why; it deletes nothing, and ``evidence`` keeps a copy
    of the triggering events that outlives their retention.
    """

    class Rule(models.TextChoices):
        FAILED_LOGIN_ACCOUNT = "failed_login_account", "Repeated failed logins for one account"
        FAILED_LOGIN_ADDRESS = "failed_login_address", "Repeated failed logins from one address"
        AUTHORIZATION_FAILURES = "authorization_failures", "Repeated authorization failures"
        RATE_LIMIT_VIOLATIONS = "rate_limit_violations", "Repeated rate-limit violations"

    class Severity(models.TextChoices):
        WARNING = "warning", "Warning"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"

    rule = models.CharField(max_length=32, choices=Rule.choices)
    subject = models.CharField(max_length=160)
    severity = models.CharField(max_length=8, choices=Severity.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    event_count = models.PositiveIntegerField(help_text="Events counted in the latest window.")
    threshold = models.PositiveIntegerField()
    window_minutes = models.PositiveIntegerField()
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    evidence = models.JSONField(default=list, help_text="Copy of up to 50 triggering events, newest first.")
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledgement_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen_at"]
        constraints = [
            models.UniqueConstraint(fields=["rule", "subject"], condition=models.Q(status="open"),
                                    name="security_one_open_alert_per_rule_and_subject"),
        ]
