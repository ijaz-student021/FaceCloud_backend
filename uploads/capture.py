"""When a photograph was taken: the EXIF capture time, read once at upload.

``DateTimeOriginal`` (when the shutter fired) first, then ``DateTimeDigitized``, then the generic ``DateTime``. EXIF stores a wall-clock time without a time zone, with an
optional ``OffsetTime*`` such as ``+05:00``; with the offset the time is exact, without it the wall-clock time is stored as if it were UTC (what matters here is the date: "on this
day", the moment a photograph belongs to). A missing, malformed or implausible value gives ``None`` and the photograph is dated by its upload time instead (``ImageUpload.captured_at``).
"""
import math
from datetime import datetime, timedelta, timezone

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
GPS_DECIMALS = 4               # about 11 m: enough for a city and a map pin, not the exact spot of a front door
# (ifd, tag of the time, tag of its UTC offset)
SOURCES = ((EXIF_IFD, 36867, 36881), (EXIF_IFD, 36868, 36882), (None, 306, 36880))
EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
FUTURE_SLACK = timedelta(days=2)


def parse_exif_datetime(raw, offset=None, now=None):
    """``"2023:10:13 21:33:05"`` (and an optional ``"+05:00"``) as an aware datetime, or ``None`` when it is not a believable time."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip("\x00")
    try:
        moment = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    zone = timezone.utc
    if isinstance(offset, bytes):
        offset = offset.decode("ascii", "ignore")
    if isinstance(offset, str):
        cleaned = offset.strip().strip("\x00")
        try:
            sign = -1 if cleaned.startswith("-") else 1
            hours, minutes = cleaned.lstrip("+-").split(":")
            zone = timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
        except ValueError:
            zone = timezone.utc
    moment = moment.replace(tzinfo=zone)
    now = now or datetime.now(timezone.utc)
    if moment < EARLIEST or moment > now + FUTURE_SLACK:
        return None
    return moment.astimezone(timezone.utc)


def capture_time(image):
    """The capture time of an open Pillow image, or ``None``. Never raises."""
    try:
        exif = image.getexif()
    except Exception:
        return None
    for ifd, tag, offset_tag in SOURCES:
        try:
            source = exif.get_ifd(ifd) if ifd else exif
            found = parse_exif_datetime(source.get(tag), source.get(offset_tag))
        except Exception:
            continue
        if found is not None:
            return found
    return None


def _degrees(value, reference):
    """Degrees, minutes, seconds (three numbers) and a hemisphere letter as signed decimal degrees; ``ValueError`` when either is unusable."""
    reference = (reference.decode("ascii", "ignore") if isinstance(reference, bytes) else str(reference)).strip().strip("\x00").upper()[:1]
    if reference not in ("N", "S", "E", "W"):
        raise ValueError("no hemisphere")
    degrees, minutes, seconds = (float(part) for part in value)
    result = degrees + minutes / 60 + seconds / 3600
    if not math.isfinite(result) or minutes < 0 or seconds < 0 or degrees < 0:
        raise ValueError("not a position")
    return -result if reference in ("S", "W") else result


def parse_gps(gps):
    """``(latitude, longitude)`` rounded to ``GPS_DECIMALS`` from an EXIF GPS block, or ``None``.

    Out-of-range values, a missing hemisphere, and exactly ``(0, 0)`` (what many cameras write when they had no fix) are not positions.
    """
    try:
        latitude = _degrees(gps[2], gps[1])
        longitude = _degrees(gps[4], gps[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if abs(latitude) > 90 or abs(longitude) > 180 or (latitude == 0 and longitude == 0):
        return None
    return round(latitude, GPS_DECIMALS), round(longitude, GPS_DECIMALS)


def gps_position(image):
    """Where an open Pillow image says it was taken, or ``None``. Never raises."""
    try:
        return parse_gps(image.getexif().get_ifd(GPS_IFD))
    except Exception:
        return None
