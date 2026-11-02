from django.apps import AppConfig


class PlacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "places"
    verbose_name = "Places"

    def ready(self):
        from . import checks  # noqa: F401 - registers the system checks
