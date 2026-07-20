from django.apps import AppConfig


class UploadsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "uploads"

    def ready(self):
        from . import signals  # noqa: F401
