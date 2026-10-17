"""Avatar upload: validate, re-encode, store; never keep the uploaded bytes.

The upload is decoded with the same decoder as photos (orientation, colour
profile, bit depth), reduced to at most AVATAR_MAX_EDGE pixels on its longer
side and written as a fresh PNG. Re-encoding drops EXIF (including GPS) and any
payload hidden in the original file.
"""
import io

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from uploads.decoding import to_working_colour

AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_MAX_SOURCE_PIXELS = 40_000_000
AVATAR_MAX_EDGE = 512
ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP"}


def _decode(uploaded):
    if uploaded.size > AVATAR_MAX_BYTES:
        raise ValidationError({"avatar": f"The image must be at most {AVATAR_MAX_BYTES // (1024 * 1024)} MB."})
    try:
        with Image.open(uploaded) as image:
            if image.format not in ACCEPTED_FORMATS:
                raise ValidationError({"avatar": "Use a JPEG, PNG or WebP image."})
            if image.width * image.height > AVATAR_MAX_SOURCE_PIXELS:
                raise ValidationError({"avatar": "The image has too many pixels."})
            image.load()
            working, _notes = to_working_colour(image)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValidationError({"avatar": "The file is not a readable image."}) from error
    return working


def set_avatar(user, uploaded):
    image = _decode(uploaded)
    image.thumbnail((AVATAR_MAX_EDGE, AVATAR_MAX_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    previous = user.avatar.name if user.avatar else None
    user.avatar.save("avatar.png", ContentFile(buffer.getvalue()), save=False)
    user.avatar_updated_at = timezone.now()
    user.save(update_fields=["avatar", "avatar_updated_at"])
    if previous and previous != user.avatar.name:
        user.avatar.storage.delete(previous)
    return user


def remove_avatar(user):
    if user.avatar:
        user.avatar.storage.delete(user.avatar.name)
    user.avatar = ""
    user.avatar_updated_at = timezone.now()
    user.save(update_fields=["avatar", "avatar_updated_at"])
    return user
