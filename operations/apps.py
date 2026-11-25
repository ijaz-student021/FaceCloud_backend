from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "operations"

    def ready(self):
        from . import checks  # noqa: F401
        from .devserver import lengthen_listen_queue

        lengthen_listen_queue()
