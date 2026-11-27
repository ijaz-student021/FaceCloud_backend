"""Reduced, watermarked previews for the owner of an image in an organization that requires download approval.

Approval controls who may take a file away; a preview shown on the screen is a copy too. So while the image's owner holds no approval for it, every route that shows them
the image serves at most ``DOWNLOAD_APPROVAL_PREVIEW_MAX_EDGE`` pixels (800) with a tiled FaceCloud watermark, rendered on the fly (a clean cached copy is never used). With an
active approval, in an organization that does not require approval, and in a personal workspace, nothing here applies and the routes behave exactly as before. The moderators'
review previews are unchanged for the images of other members; a moderator looking at their own image is treated like any owner.
"""
import io

from django.conf import settings
from django.utils import timezone
from PIL import Image

from organizations.models import Organization, OrganizationMembership

from .approval import approval_organization
from .models import DownloadGrant


def _spec():
    from enhancement.watermark import validate_spec

    return validate_spec(mode="facecloud", position="tile", opacity=0.45)


def holds_active_grant(user, image):
    """Whether ``user`` has an approval for this image that works right now (not expired, not revoked, member and organization still active)."""
    organization = image.batch.organization
    if organization is None or organization.status == Organization.Status.SUSPENDED:
        return False
    if not OrganizationMembership.objects.filter(organization=organization, user=user, status=OrganizationMembership.Status.ACTIVE).exists():
        return False
    return DownloadGrant.objects.filter(grantee=user, image=image, revoked_at__isnull=True, expires_at__gt=timezone.now()).exists()


def preview_is_restricted(user, image):
    """True when ``user`` must be shown only the reduced, watermarked preview of ``image``."""
    if image.owner_id != user.pk or approval_organization(image) is None:
        return False
    return not holds_active_grant(user, image)


def max_edge(requested=None):
    cap = int(settings.DOWNLOAD_APPROVAL_PREVIEW_MAX_EDGE)
    return min(int(requested), cap) if requested else cap


def stamp_jpeg(payload, user, requested=None):
    """The JPEG ``payload`` (an avatar, a picture for a report), reduced to at most ``max_edge(requested)`` pixels if it is larger, with the tiled watermark."""
    from enhancement.watermark import apply_watermark

    with Image.open(io.BytesIO(payload)) as source:
        working = source.convert("RGB")
    cap = max_edge(requested)
    if max(working.size) > cap:
        working.thumbnail((cap, cap), Image.Resampling.LANCZOS)
    stamped = apply_watermark(working, _spec(), user)
    buffer = io.BytesIO()
    stamped.save(buffer, format="JPEG", quality=int(settings.THUMBNAIL_JPEG_QUALITY), optimize=True)
    return buffer.getvalue()


def restricted_preview(file_field, user, requested=None):
    """JPEG bytes of ``file_field`` at most ``max_edge(requested)`` pixels on its longest side, stamped with the tiled watermark."""
    from thumbnails.services import render_thumbnail

    jpeg, _width, _height = render_thumbnail(file_field, max_edge(requested))
    return stamp_jpeg(jpeg, user, requested)


def restricted_response(file_field, user, requested=None):
    """A ready ``HttpResponse`` with the restricted preview; never cached beyond the browser's own short copy."""
    from django.http import Http404, HttpResponse

    from thumbnails.services import ThumbnailUnavailable

    try:
        payload = restricted_preview(file_field, user, requested)
    except ThumbnailUnavailable as error:
        raise Http404(str(error)) from error
    response = HttpResponse(payload, content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["X-Preview-Restricted"] = "approval-required"
    return response
