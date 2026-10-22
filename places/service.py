"""Putting photographs in places, and taking their location away."""
import logging

from django.utils import timezone

from uploads.models import ImageUpload

from . import gazetteer
from .models import Place

logger = logging.getLogger(__name__)


def place_for(latitude, longitude):
    """The ``Place`` row for a position (created the first time a photograph is placed there), or ``None``."""
    hit = gazetteer.nearest(latitude, longitude)
    if hit is None:
        return None
    place, _created = Place.objects.get_or_create(
        geoname_id=hit["geoname_id"],
        defaults={key: hit[key] for key in ("name", "region", "country_code", "country", "latitude", "longitude")},
    )
    return place


def attach_place(image):
    """Place ``image`` from its coordinates. Never raises: a place that cannot be found never costs an upload. Returns the place or ``None``."""
    try:
        if image.gps_lat is None or image.gps_lon is None:
            return None
        place = place_for(image.gps_lat, image.gps_lon)
        if place is not None:
            ImageUpload.all_objects.filter(pk=image.pk).update(place=place)
            image.place = place
        return place
    except Exception:
        logger.exception("Could not place image_id=%s.", image.pk)
        return None


def remove_location(images):
    """Take the location (coordinates and place) off ``images``; they are marked as looked at, so the backfill never puts it back. Returns how many had one."""
    ids = [image.pk for image in images]
    had = ImageUpload.all_objects.filter(pk__in=ids).exclude(gps_lat__isnull=True, place__isnull=True).count()
    ImageUpload.all_objects.filter(pk__in=ids).update(gps_lat=None, gps_lon=None, place=None, location_checked_at=timezone.now())
    return had
