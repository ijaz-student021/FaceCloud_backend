from rest_framework import serializers

from .models import SecurityAlert

Rule = SecurityAlert.Rule

# How an alert reads on the administrator's Security screen: its kind, and a risk word. The backend has two severities, so there are two risk
# words; "low" and "critical" are never produced (an alert only exists once its threshold is crossed, and "high" means three times over).
TYPE_BY_RULE = {
    Rule.FAILED_LOGIN_ACCOUNT: "failed_login",
    Rule.FAILED_LOGIN_ADDRESS: "failed_login",
    Rule.AUTHORIZATION_FAILURES: "authorization_failure",
    Rule.RATE_LIMIT_VIOLATIONS: "rate_limit",
}
RISK_BY_SEVERITY = {SecurityAlert.Severity.WARNING: "medium", SecurityAlert.Severity.HIGH: "high"}
MESSAGE_BY_RULE = {
    Rule.FAILED_LOGIN_ACCOUNT: "{count} failed sign-ins for {who} in {window} minutes (alert at {threshold}).",
    Rule.FAILED_LOGIN_ADDRESS: "{count} failed sign-ins from {who} in {window} minutes (alert at {threshold}).",
    Rule.AUTHORIZATION_FAILURES: "{count} requests refused as not allowed (403) from {who} in {window} minutes (alert at {threshold}).",
    Rule.RATE_LIMIT_VIOLATIONS: "{count} requests blocked by the rate limit (429) from {who} in {window} minutes (alert at {threshold}).",
}


def _who(subject):
    kind, _, value = subject.partition(":")
    if kind == "ip":
        return f"address {value}"
    if kind == "user":
        return f"user {value}"
    return subject


class SecurityAlertSerializer(serializers.ModelSerializer):
    acknowledged_by = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField(help_text="failed_login, authorization_failure or rate_limit.")
    risk = serializers.SerializerMethodField(help_text="medium (over the threshold) or high (three times over).")
    message = serializers.SerializerMethodField(help_text="One sentence saying what happened, with the numbers.")
    ip_address = serializers.SerializerMethodField(help_text="The address the alert is about, when it is about one; otherwise null.")

    class Meta:
        model = SecurityAlert
        fields = [
            "id", "rule", "subject", "severity", "status", "event_count", "threshold", "window_minutes",
            "first_seen_at", "last_seen_at", "acknowledged_by", "acknowledged_at", "acknowledgement_note",
            "created_at", "type", "risk", "message", "ip_address",
        ]
        read_only_fields = fields

    def get_type(self, alert) -> str:
        return TYPE_BY_RULE.get(alert.rule, alert.rule)

    def get_risk(self, alert) -> str:
        return RISK_BY_SEVERITY.get(alert.severity, alert.severity)

    def get_message(self, alert) -> str:
        template = MESSAGE_BY_RULE.get(alert.rule, "{count} events for {who} in {window} minutes (alert at {threshold}).")
        return template.format(count=alert.event_count, who=_who(alert.subject), window=alert.window_minutes, threshold=alert.threshold)

    def get_ip_address(self, alert) -> str | None:
        kind, _, value = alert.subject.partition(":")
        if kind == "ip":
            return value
        for item in alert.evidence or []:
            if item.get("ip_address"):
                return item["ip_address"]
        return None

    def get_acknowledged_by(self, alert) -> dict | None:
        user = alert.acknowledged_by
        return {"id": user.pk, "username": user.username} if user else None


class SecurityAlertDetailSerializer(SecurityAlertSerializer):
    class Meta(SecurityAlertSerializer.Meta):
        fields = SecurityAlertSerializer.Meta.fields + ["evidence"]
        read_only_fields = fields


class AcknowledgeSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=2000, help_text="What was checked or done. Required.")


class SecurityRulesSerializer(serializers.Serializer):
    window_minutes = serializers.IntegerField()
    failed_login_threshold = serializers.IntegerField()
    authorization_failure_threshold = serializers.IntegerField()
    rate_limit_threshold = serializers.IntegerField()
    event_retention_days = serializers.IntegerField()
