from django.apps import AppConfig


class SearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "search"
    verbose_name = "Top-bar search"

    def ready(self):
        from . import checks  # noqa: F401 - registers the system checks
