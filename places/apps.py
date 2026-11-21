from django.apps import AppConfig


class PlacesConfig(AppConfig):
    """Retired feature: only the ``Place`` model and its migrations remain (see ``places/models.py``)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "places"
    verbose_name = "Places (retired)"
