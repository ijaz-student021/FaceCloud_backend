"""A person's avatar: a square crop of the best face of their folder, like the round faces of Google Photos.

Nothing new is analysed: the crop uses the face box the face check already stored (``IdentityFace.bounding_box``, in the coordinates of the standardized image) on that same
standardized picture, for a face that was grouped under the person's consent. Withdrawing consent with erasure deletes the folders, and with them any avatar.
"""
import io

from django.conf import settings
from PIL import Image

from uploads.decoding import to_working_colour

from .models import IdentityMembership

AVATAR_SIZES = (96, 192, 384)
DEFAULT_AVATAR_SIZE = 96
MARGIN = 0.35                    # share of the face's larger side added on every side, so the avatar shows the head, not just the face
CANDIDATES = 200                 # the newest faces looked at when a folder is huge
CONFIRMED = (IdentityMembership.State.AUTO_MATCHED, IdentityMembership.State.NEW_GROUP, IdentityMembership.State.CONFIRMED)
MIN_BRIGHTNESS = 45.0            # a face darker than this makes a poor avatar when a brighter one exists


class AvatarUnavailable(Exception):
    """The folder has no face that can be cropped (none grouped, or its standardized picture is missing)."""


def _box_of(face):
    box = face.bounding_box or {}
    try:
        x, y, width, height = (float(box[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


def face_score(face):
    """Higher is a better avatar: bright enough, then the detector's confidence, then the size."""
    quality = face.quality or {}
    box = _box_of(face)
    area = box[2] * box[3] if box else 0.0
    brightness = quality.get("brightness")
    bright_enough = 1 if brightness is None or float(brightness) >= MIN_BRIGHTNESS else 0
    return (bright_enough, round(float(quality.get("confidence") or 0.0), 2), area)


def best_face(group):
    """The face of ``group`` the avatar is cut from, or ``None``."""
    memberships = (
        IdentityMembership.objects.filter(identity_group=group, state__in=CONFIRMED)
        .select_related("identity_face__image__preprocessing_result").order_by("-identity_face__created_at")[:CANDIDATES]
    )
    best = None
    for membership in memberships:
        face = membership.identity_face
        if _box_of(face) is None:
            continue
        try:
            standardized = face.image.preprocessing_result.processed_file
        except Exception:
            continue
        if not standardized:
            continue
        if best is None or face_score(face) > face_score(best):
            best = face
    return best


def square_around(box, width, height, margin=MARGIN):
    """``(left, top, side)`` of the square that holds ``box`` plus ``margin`` on every side, moved to stay inside a ``width`` x ``height`` picture."""
    x, y, box_width, box_height = box
    side = max(box_width, box_height) * (1 + 2 * margin)
    side = min(side, width, height)
    left = min(max(x + box_width / 2 - side / 2, 0), width - side)
    top = min(max(y + box_height / 2 - side / 2, 0), height - side)
    return int(round(left)), int(round(top)), max(1, int(round(side)))


def render_avatar(face, size):
    """JPEG bytes of the avatar of ``face`` at ``size`` x ``size``."""
    box = _box_of(face)
    field = face.image.preprocessing_result.processed_file
    if box is None or not field:
        raise AvatarUnavailable("This face has no stored box or standardized picture.")
    try:
        field.open("rb")
        try:
            with Image.open(field) as source:
                source.load()
                working, _notes = to_working_colour(source)
                working = working.convert("RGB")
        finally:
            field.close()
    except Exception as error:
        raise AvatarUnavailable("The standardized picture could not be read.") from error
    left, top, side = square_around(box, working.width, working.height)
    crop = working.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    crop.save(buffer, format="JPEG", quality=int(settings.THUMBNAIL_JPEG_QUALITY), optimize=True)
    return buffer.getvalue()
