"""Organization branding: a colour, a display name and a logo the owner chooses and every member sees.

The logo is decoded with the decoder used for photographs (orientation, colour profile, bit depth), reduced to at most ``LOGO_MAX_EDGE`` pixels and
written as a fresh PNG, so EXIF/GPS and anything hidden in the uploaded bytes never reach storage. Only JPEG and PNG are accepted (no SVG, which can
carry script, and no animated formats). The file is never public: it is served through ``GET /api/organizations/{id}/logo/`` to the organization's members.
"""
import io
import re
import unicodedata

from django.core.files.base import ContentFile
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from uploads.decoding import to_working_colour

from .models import OrganizationAuditEvent
from .services import record_audit_event

LOGO_MAX_BYTES = 1024 * 1024
LOGO_MAX_SOURCE_PIXELS = 16_000_000
LOGO_MAX_EDGE = 512
ACCEPTED_FORMATS = {"JPEG", "PNG"}
BRAND_NAME_MAX = 100
_HEX_COLOUR = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def clean_brand_name(value):
    """The brand name as stored: trimmed, at most 100 characters, no control characters or line breaks. Blank clears it."""
    value = (value or "").strip()
    if len(value) > BRAND_NAME_MAX:
        raise ValidationError(f"The brand name can be at most {BRAND_NAME_MAX} characters.")
    if any(unicodedata.category(character) in ("Cc", "Cf", "Zl", "Zp") for character in value):
        raise ValidationError("The brand name cannot contain control characters or line breaks.")
    return value


def clean_accent(value):
    """``#rrggbb`` in lower case from ``#rgb`` or ``#rrggbb`` in any case. Blank clears it."""
    value = (value or "").strip()
    if not value:
        return ""
    if not _HEX_COLOUR.match(value):
        raise ValidationError("Use a colour like #1a73e8.")
    digits = value[1:].lower()
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return f"#{digits}"


def _decode(uploaded):
    if uploaded.size > LOGO_MAX_BYTES:
        raise ValidationError({"logo": [f"The logo must be at most {LOGO_MAX_BYTES // (1024 * 1024)} MB."]})
    try:
        with Image.open(uploaded) as image:
            if image.format not in ACCEPTED_FORMATS:
                raise ValidationError({"logo": ["Use a PNG or JPEG image."]})
            if image.width * image.height > LOGO_MAX_SOURCE_PIXELS:
                raise ValidationError({"logo": ["The image has too many pixels."]})
            image.load()
            working, _notes = to_working_colour(image)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValidationError({"logo": ["The file is not a readable image."]}) from error
    return working


def set_logo(organization, uploaded, actor, request=None):
    """Validate, re-encode and store ``uploaded`` as the organization's logo, replacing (and deleting) the old file."""
    image = _decode(uploaded)
    image.thumbnail((LOGO_MAX_EDGE, LOGO_MAX_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    previous = organization.logo.name if organization.logo else None
    organization.logo_uploaded_by = actor
    organization.logo.save("logo.png", ContentFile(buffer.getvalue()), save=False)
    organization.logo_updated_at = timezone.now()
    organization.save(update_fields=["logo", "logo_updated_at", "logo_uploaded_by", "updated_at"])
    if previous and previous != organization.logo.name:
        organization.logo.storage.delete(previous)
    record_audit_event(
        organization, actor, OrganizationAuditEvent.Action.ORGANIZATION_LOGO_CHANGED, target_type="Organization", target_id=organization.pk,
        new_value={"replaced": bool(previous), "width": image.width, "height": image.height}, request=request,
    )
    return organization


def remove_logo(organization, actor, request=None):
    if not organization.logo:
        return organization
    organization.logo.storage.delete(organization.logo.name)
    organization.logo = ""
    organization.logo_updated_at = timezone.now()
    organization.save(update_fields=["logo", "logo_updated_at", "updated_at"])
    record_audit_event(
        organization, actor, OrganizationAuditEvent.Action.ORGANIZATION_LOGO_REMOVED, target_type="Organization", target_id=organization.pk, request=request,
    )
    return organization
