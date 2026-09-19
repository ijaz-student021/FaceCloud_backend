from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register()
def privacy_configuration_check(app_configs, **kwargs):
    issues = []
    if settings.FACIAL_DATA_RETENTION_DAYS < 0:
        issues.append(Error("FACIAL_DATA_RETENTION_DAYS cannot be negative.", id="privacy.E002"))
    if not str(settings.BIOMETRIC_CONSENT_POLICY_VERSION).strip():
        issues.append(Error("BIOMETRIC_CONSENT_POLICY_VERSION cannot be empty.", id="privacy.E003"))
    if settings.PRIVACY_EXPORT_MAX_ROWS <= 0:
        issues.append(Error("PRIVACY_EXPORT_MAX_ROWS must be positive.", id="privacy.E004"))
    return issues


@register(Tags.security, deploy=True)
def privacy_deployment_check(app_configs, **kwargs):
    """Deployment-only findings (`manage.py check --deploy`)."""
    issues = []
    key = settings.IDENTITY_EMBEDDING_KEY
    if not key.strip():
        issues.append(
            Error(
                "IDENTITY_EMBEDDING_KEY is empty, so face embeddings would be protected "
                "with a publicly known key.",
                id="privacy.E005",
            )
        )
    elif key == settings.SECRET_KEY:
        issues.append(
            Error(
                "IDENTITY_EMBEDDING_KEY falls back to SECRET_KEY. Face embeddings must "
                "use a dedicated key so rotating the Django secret cannot lock or expose them.",
                id="privacy.E001",
            )
        )
    elif len(key) < 32:
        issues.append(Warning("IDENTITY_EMBEDDING_KEY should be at least 32 characters.", id="privacy.W001"))
    if not settings.BIOMETRIC_CONSENT_REQUIRED:
        issues.append(
            Warning(
                "BIOMETRIC_CONSENT_REQUIRED is off: face embeddings are created without recorded consent.",
                id="privacy.W002",
            )
        )
    if settings.FACIAL_DATA_RETENTION_DAYS <= 0:
        issues.append(
            Warning(
                "FACIAL_DATA_RETENTION_DAYS is 0: biometric data is kept until the user deletes it.",
                id="privacy.W003",
            )
        )
    return issues
