"""The top-bar search: the caller's own photos, person folders and upload batches in one request."""
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from albums.serializers import AlbumSerializer
from albums.views import albums_for
from identities.models import IdentityGroup
from identities.people import people_queryset
from identities.serializers import PersonChipSerializer
from uploads.models import UploadBatch
from uploads.serializers import MyImageSerializer
from uploads.views import my_images_queryset

MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 5
MAX_LIMIT = 10


def job_label(batch):
    """The same label the frontend's "Your uploads" screen draws, so a result is never blank."""
    return batch.display_name or batch.original_archive_name or f"Upload {batch.pk}"


class SearchImageSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    thumbnail_url = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    created_at = serializers.DateTimeField()


class SearchGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityGroup
        fields = ("id", "display_name", "member_count")


class SearchJobSerializer(serializers.ModelSerializer):
    # Plain text on purpose: a model choice field would add one more "status" enum to the schema.
    status = serializers.CharField(read_only=True)
    display_name = serializers.SerializerMethodField(
        help_text='The label "Your uploads" shows: the name, else the ZIP\'s file name, else "Upload <id>".',
    )

    class Meta:
        model = UploadBatch
        fields = ("id", "display_name", "status", "total_files", "created_at")

    def get_display_name(self, batch) -> str:
        return job_label(batch)


class SearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    images = SearchImageSerializer(many=True)
    groups = SearchGroupSerializer(many=True)
    people = PersonChipSerializer(many=True)
    albums = AlbumSerializer(many=True)
    jobs = SearchJobSerializer(many=True)


def _limit(request):
    raw = request.query_params.get("limit", "")
    if raw == "":
        return DEFAULT_LIMIT
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationError({"limit": "Limit must be a positive whole number."})
    return min(int(raw), MAX_LIMIT)


class SearchView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "search"

    @extend_schema(
        operation_id="search_list",
        parameters=[
            OpenApiParameter("q", str, required=True, description="At least 2 characters; case-insensitive 'contains'."),
            OpenApiParameter("limit", int, required=False, description="Results per kind; default 5, maximum 10."),
        ],
        responses={200: SearchResponseSerializer},
    )
    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < MIN_QUERY_LENGTH:
            raise ValidationError({"q": f"Type at least {MIN_QUERY_LENGTH} characters to search."})
        limit = _limit(request)
        user = request.user

        # icontains escapes the LIKE wildcards (% and _) itself, so a query of "%" matches only a literal "%".
        photos = my_images_queryset(user).filter(original_name__icontains=query).order_by("-created_at", "-pk")[:limit]
        status_of = MyImageSerializer(context={"request": request}).get_display_status
        thumbnail_of = MyImageSerializer(context={"request": request}).get_thumbnail_url
        images = [
            {
                "id": photo.pk,
                "name": photo.original_name,
                "thumbnail_url": thumbnail_of(photo),
                "status": status_of(photo),
                "created_at": photo.created_at,
            }
            for photo in photos
        ]
        groups = IdentityGroup.objects.filter(
            owner=user, state=IdentityGroup.State.ACTIVE, display_name__icontains=query,
        ).order_by("display_name", "pk")[:limit]
        # A batch is named only if the person renamed it; a ZIP always has its archive's file name.
        jobs = UploadBatch.objects.filter(
            Q(display_name__icontains=query) | Q(original_archive_name__icontains=query), owner=user,
        ).order_by("-created_at", "-pk")[:limit]
        # The same folders as ``groups``, as tappable chips: a face, a name and how many photographs (a folder with no photographs left is not a person to show).
        people = people_queryset(user, query=query)[:limit]
        return Response({
            "query": query,
            "images": images,
            "groups": SearchGroupSerializer(groups, many=True).data,
            "people": PersonChipSerializer(people, many=True, context={"request": request}).data,
            "albums": AlbumSerializer(albums_for(user).filter(title__icontains=query).order_by("-updated_at", "-pk")[:limit], many=True, context={"request": request}).data,
            "jobs": SearchJobSerializer(jobs, many=True).data,
        })
