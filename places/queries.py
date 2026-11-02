from django.db.models import Count, OuterRef, Q, Subquery

from uploads.models import ImageUpload

from .models import Place


def places_for(user):
    """The places the caller has photographs in, with ``photo_count`` (their own stored photographs, not in the trash) and the id of a cover photograph, most photographs first."""
    mine = Q(images__owner=user, images__status="uploaded", images__trashed_at__isnull=True)
    cover = ImageUpload.objects.filter(owner=user, status="uploaded", place=OuterRef("pk")).order_by("-is_favorite", "-created_at").values("pk")[:1]
    return (
        Place.objects.annotate(photo_count=Count("images", filter=mine, distinct=True), cover_image_id=Subquery(cover))
        .filter(photo_count__gt=0)
        .order_by("-photo_count", "name", "pk")
    )
