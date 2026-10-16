"""The top-bar search: the caller's own photos, person folders and upload batches in one request."""
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from identities.models import IdentityGroup
from uploads.models import UploadBatch
from uploads.serializers import MyImageSerializer
from uploads.views import my_images_queryset

MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 5
MAX_LIMIT = 10


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
    class Meta:
        model = UploadBatch
        fields = ("id", "display_name", "status", "total_files", "created_at")


class SearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    images = SearchImageSerializer(many=True)
    groups = SearchGroupSerializer(many=True)
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
        jobs = UploadBatch.objects.filter(owner=user, display_name__icontains=query).order_by("-created_at", "-pk")[:limit]
        return Response({
            "query": query,
            "images": images,
            "groups": SearchGroupSerializer(groups, many=True).data,
            "jobs": SearchJobSerializer(jobs, many=True).data,
        })
