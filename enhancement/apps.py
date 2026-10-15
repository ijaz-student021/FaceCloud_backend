from django.apps import AppConfig


class EnhancementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "enhancement"
    verbose_name = "Image Enhancement"

    def ready(self):
        from . import checks  # noqa: F401
