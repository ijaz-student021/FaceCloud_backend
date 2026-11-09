from django.conf import settings
from django.core.checks import Warning, register

from . import gazetteer


@register()
def place_name_checks(app_configs, **kwargs):
    """A warning (never an error) when a place-name file is present but cannot be used, so a development server always starts."""
    if not settings.LOCATION_ENABLED:
        return []
    from pathlib import Path

    if not (Path(settings.LOCATION_GAZETTEER_DIR) / gazetteer.CITIES).is_file():
        return []                                                          # not installed: place names are simply off
    ok, reason = gazetteer.status()
    if ok:
        return []
    return [Warning(f"Place names are off: {reason}", hint="See docs/model-licenses/location-gazetteer.md.", id="places.W001")]
