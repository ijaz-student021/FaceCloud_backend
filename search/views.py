"""The top-bar search: the caller's own photos, person folders and upload batches in one request."""
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from albums.serializers import AlbumSerializer
from albums.views import albums_for
from identities.models import IdentityGroup
from identities.people import people_queryset
from identities.serializers import PersonChipSerializer
from places.queries import places_for
from places.serializers import PlaceSerializer
from uploads.models import UploadBatch
from uploads.serializers import MyImageSerializer
from uploads.views import my_images_queryset

from .semantic import service as semantic
from .semantic.embedders import SemanticSearchUnavailable

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


class SemanticHitSerializer(SearchImageSerializer):
    score = serializers.FloatField(help_text="Cosine similarity between the words and the photograph (higher is closer; the results are already in this order).")


class SearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    images = SearchImageSerializer(many=True)
    photos = SemanticHitSerializer(many=True, required=False, help_text="Photographs that show what the words say. Present only when search by meaning is switched on and working.")
    groups = SearchGroupSerializer(many=True)
    people = PersonChipSerializer(many=True)
    albums = AlbumSerializer(many=True)
    places = PlaceSerializer(many=True)
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
        body = {
            "query": query,
            "images": images,
            "groups": SearchGroupSerializer(groups, many=True).data,
            "people": PersonChipSerializer(people, many=True, context={"request": request}).data,
            "albums": AlbumSerializer(albums_for(user).filter(title__icontains=query).order_by("-updated_at", "-pk")[:limit], many=True, context={"request": request}).data,
            "places": PlaceSerializer(places_for(user).filter(name__icontains=query)[:limit], many=True, context={"request": request}).data,
            "jobs": SearchJobSerializer(jobs, many=True).data,
        }
        if semantic.enabled():
            try:
                ranked = semantic.rank(user, query, limit=limit)
            except SemanticSearchUnavailable:
                ranked = None                                                    # a broken model never breaks the rest of the search; the dedicated endpoint says why
            if ranked is not None:
                scores = dict(ranked)
                found = {photo.pk: photo for photo in my_images_queryset(user).filter(pk__in=scores)}
                body["photos"] = [
                    {"id": pk, "name": found[pk].original_name, "thumbnail_url": thumbnail_of(found[pk]), "status": status_of(found[pk]),
                     "created_at": found[pk].created_at, "score": round(score, 4)}
                    for pk, score in ranked if pk in found
                ]
        return Response(body)


class SemanticQuerySerializer(serializers.Serializer):
    q = serializers.CharField(min_length=semantic.MIN_QUERY, max_length=semantic.MAX_QUERY, trim_whitespace=True)
    limit = serializers.IntegerField(required=False, min_value=1)


class SemanticPhotoSerializer(serializers.Serializer):
    score = serializers.FloatField(help_text="Cosine similarity between the words and the photograph; the list is in this order.")
    photo = MyImageSerializer(help_text="The photograph, as the gallery lists it.")


class SemanticSearchResponseSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(help_text="false when search by meaning is switched off on this server (the screen hides its search box).")
    query = serializers.CharField()
    language = serializers.CharField(help_text="The only language queries are understood in.")
    model = serializers.CharField(allow_null=True)
    results = SemanticPhotoSerializer(many=True)


class SemanticPhotoSearchView(APIView):
    """Photographs that show what the words say ("beach", "red car", "birthday cake"): the caller's own, accepted, not in the trash, best first."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "semantic_search"

    @extend_schema(
        operation_id="search_photos_list",
        parameters=[
            OpenApiParameter("q", str, required=True, description=f"{semantic.MIN_QUERY} to {semantic.MAX_QUERY} characters, in English."),
            OpenApiParameter("limit", int, required=False, description="How many photographs; default and maximum are server settings (24 and 100)."),
        ],
        responses={200: SemanticSearchResponseSerializer, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        payload = SemanticQuerySerializer(data=request.query_params)
        payload.is_valid(raise_exception=True)
        query, limit = payload.validated_data["q"], payload.validated_data.get("limit")
        body = {"enabled": semantic.enabled(), "query": query, "language": "en", "model": None, "results": []}
        if not body["enabled"]:
            return Response(body)
        try:
            embedder = semantic.get_embedder()
            ranked = semantic.rank(request.user, query, limit=limit, embedder=embedder)
        except SemanticSearchUnavailable as error:
            return Response({"detail": str(error), "code": "semantic_search_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        found = {photo.pk: photo for photo in my_images_queryset(request.user).filter(pk__in=[pk for pk, _score in ranked])}
        body["model"] = getattr(embedder, "name", None)
        body["results"] = [
            {"score": round(score, 4), "photo": MyImageSerializer(found[pk], context={"request": request}).data} for pk, score in ranked if pk in found
        ]
        return Response(body)
