"""Administrator image monitoring (B06): every user's images, and the administrator's decision on one.

A separate, admin-only surface. The owner-scoped image endpoints are not
widened: they still show a user only their own images.

* ``GET /api/system/images/``: always paginated (default 50, at most 200),
  newest first unless ``ordering`` says otherwise, metadata only, filterable.
  Not audited per page.
* ``GET /api/system/images/{id}/``: processing details. Audited.
* ``GET /api/system/images/{id}/preview/``: a JPEG thumbnail, never the
  original file. Audited.
* ``POST /api/system/images/{id}/decision/``: accept, reject or put the photo
  back under review (``operations/image_decision.py`` holds the rules).
  Answers with the same body as the detail call. Audited as a decision.

Each audited access is an ``admin`` activity event by the administrator,
naming the image and its owner, so it appears in ``/api/system/audit/``.
"""
from django.db.models import F, Prefetch, Q
from django.db.models.functions import Lower
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from rest_framework import generics, permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole
from activity.services import record_activity_event
from enhancement.models import ImageEnhancementResult
from moderation.serializers import ModeratorDecisionSerializer
from uploads.models import ImageUpload

from .image_decision import DECISION_FOR_ACTION, REASON_REQUIRED, decide_image


class AdminImagePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class AdminImageOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()


class AdminImageQualitySerializer(serializers.Serializer):
    status = serializers.CharField()
    decision = serializers.CharField(allow_blank=True)
    overall_score = serializers.FloatField(allow_null=True)
    sharpness_score = serializers.FloatField(allow_null=True)
    brightness_score = serializers.FloatField(allow_null=True)


class AdminEnhancementSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    requested_level = serializers.CharField()
    applied_level = serializers.CharField(allow_blank=True)
    algorithm = serializers.CharField(allow_null=True)
    identity_status = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)


def _quality(image):
    decision = getattr(image, "quality_decision", None)
    if decision is None:
        return None
    return {
        "status": decision.status, "decision": decision.decision, "overall_score": decision.overall_score,
        "sharpness_score": decision.sharpness_score, "brightness_score": decision.brightness_score,
    }


def _enhancement(result):
    parameters = result.applied_parameters or {}
    return {
        "id": result.pk, "status": result.status, "requested_level": result.requested_level,
        "applied_level": result.applied_level or "", "algorithm": parameters.get("algorithm"),
        "identity_status": parameters.get("identity_status"), "created_at": result.created_at,
        "finished_at": result.finished_at,
    }


class AdminImageSerializer(serializers.ModelSerializer):
    owner = AdminImageOwnerSerializer()
    organization_id = serializers.SerializerMethodField()
    quality = serializers.SerializerMethodField()
    latest_enhancement = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = [
            "id", "original_name", "owner", "organization_id", "batch", "status", "rejection_reason",
            "file_extension", "mime_type", "file_size", "width", "height", "created_at", "trashed_at",
            "quality", "latest_enhancement",
        ]
        read_only_fields = fields

    def get_organization_id(self, image) -> int | None:
        return image.batch.organization_id if image.batch_id else None

    @extend_schema_field(AdminImageQualitySerializer(allow_null=True))
    def get_quality(self, image):
        return _quality(image)

    @extend_schema_field(AdminEnhancementSummarySerializer(allow_null=True))
    def get_latest_enhancement(self, image):
        results = getattr(image, "latest_enhancements", None)
        return _enhancement(results[0]) if results else None


class AdminImageDetailSerializer(AdminImageSerializer):
    preprocessing = serializers.SerializerMethodField()
    face_validation = serializers.SerializerMethodField()
    duplicate_matches = serializers.SerializerMethodField()
    enhancements = serializers.SerializerMethodField()

    class Meta(AdminImageSerializer.Meta):
        fields = AdminImageSerializer.Meta.fields + ["preprocessing", "face_validation", "duplicate_matches", "enhancements"]
        read_only_fields = fields

    def get_preprocessing(self, image) -> dict | None:
        result = getattr(image, "preprocessing_result", None)
        if result is None:
            return None
        return {"status": result.status, "output_format": result.output_format,
                "output_width": result.output_width, "output_height": result.output_height,
                "failure_reason": result.failure_reason}

    def get_face_validation(self, image) -> dict | None:
        validation = getattr(image, "face_validation", None)
        if validation is None:
            return None
        return {"decision": validation.decision, "face_count": validation.face_count,
                "rejection_reason": validation.rejection_reason, "detector": validation.detector}

    def get_duplicate_matches(self, image) -> int:
        return image.duplicate_matches_as_source.count() if hasattr(image, "duplicate_matches_as_source") else 0

    @extend_schema_field(AdminEnhancementSummarySerializer(many=True))
    def get_enhancements(self, image):
        return [_enhancement(result) for result in image.enhancement_results.order_by("-created_at")[:20]]


def _base_queryset():
    return ImageUpload.all_objects.select_related("owner", "batch", "quality_decision").prefetch_related(
        Prefetch("enhancement_results", queryset=ImageEnhancementResult.objects.order_by("-created_at"),
                 to_attr="latest_enhancements"),
    ).order_by("-created_at", "-pk")


FILTERS = [
    OpenApiParameter("status", str, description="Upload status, e.g. uploaded or rejected."),
    OpenApiParameter("owner", int, description="Owner user id."),
    OpenApiParameter("organization", int, description="Organization id of the image's batch."),
    OpenApiParameter("quality_decision", str, description="accepted, needs_review or rejected."),
    OpenApiParameter("q", str, description="Filename or owner email contains (case-insensitive)."),
    OpenApiParameter("created_after", str, description="ISO date, inclusive (UTC)."),
    OpenApiParameter("created_before", str, description="ISO date, exclusive (UTC)."),
]

# ``ordering``: one field, with an optional leading "-" for descending. Names sort A to Z without regard to case, photos without a sharpness score come last
# either way, and ties are broken by id (newest first), so a page never repeats or skips a photo. Anything else is a 400, never a 500.
ORDERING_FIELDS = ("created_at", "original_name", "file_size", "owner__username", "quality_decision__sharpness_score")
ORDERING_VALUES = [*ORDERING_FIELDS, *(f"-{field}" for field in ORDERING_FIELDS)]
_TEXT_FIELDS = ("original_name", "owner__username")
_NULLABLE_FIELDS = ("quality_decision__sharpness_score",)
ORDERING = [OpenApiParameter(
    "ordering", str, enum=ORDERING_VALUES,
    description=(
        "Sort order: created_at, original_name, file_size, owner__username or quality_decision__sharpness_score, with a leading - for descending "
        "(for example -file_size is largest first). Names sort A to Z without regard to case; photos without a sharpness score come last in both directions; "
        "ties are broken by id, newest first. Any other value is a 400. Without it: newest first (-created_at)."
    ),
)]


def _apply_ordering(queryset, value):
    field = value.removeprefix("-")
    if field not in ORDERING_FIELDS:
        raise ValidationError({"ordering": [f"Use one of {', '.join(ORDERING_FIELDS)}, with a leading - to sort the other way."]})
    expression = Lower(field) if field in _TEXT_FIELDS else F(field)
    nulls_last = True if field in _NULLABLE_FIELDS else None
    ordered = expression.desc(nulls_last=nulls_last) if value.startswith("-") else expression.asc(nulls_last=nulls_last)
    return queryset.order_by(ordered, "-pk")


class AdminImageListView(generics.ListAPIView):
    """Every user's images, newest first unless ``ordering`` says otherwise (administrators only)."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    serializer_class = AdminImageSerializer
    pagination_class = AdminImagePagination

    @extend_schema(parameters=FILTERS + ORDERING)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        params = self.request.query_params
        queryset = _base_queryset()
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        for name, lookup in (("owner", "owner_id"), ("organization", "batch__organization_id")):
            if params.get(name):
                if not params[name].isdigit():
                    raise ValidationError({name: "Use a numeric id."})
                queryset = queryset.filter(**{lookup: int(params[name])})
        if params.get("quality_decision"):
            queryset = queryset.filter(quality_decision__decision=params["quality_decision"])
        if params.get("q"):
            text = params["q"].strip()[:120]
            queryset = queryset.filter(Q(original_name__icontains=text) | Q(owner__email__icontains=text))
        for name, lookup in (("created_after", "created_at__date__gte"), ("created_before", "created_at__date__lt")):
            if params.get(name):
                day = parse_date(params[name])
                if day is None:
                    raise ValidationError({name: "Use an ISO date, e.g. 2026-10-01."})
                queryset = queryset.filter(**{lookup: day})
        if params.get("ordering"):
            queryset = _apply_ordering(queryset, params["ordering"])
        return queryset


def _audit(request, image, event):
    record_activity_event(
        request.user, "admin", event, object_type="ImageUpload", object_id=image.pk,
        details={"owner_id": image.owner_id},
    )


class AdminImageDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={200: AdminImageDetailSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        image = get_object_or_404(_base_queryset().select_related("preprocessing_result"), pk=pk)
        _audit(request, image, "admin_image_viewed")
        return Response(AdminImageDetailSerializer(image).data)


# The moderator's own action names (ModeratorDecisionSerializer), without "correct": that picks a person folder, which has no meaning for a photograph.
DECISION_ACTIONS = [action for action in ModeratorDecisionSerializer().fields["action"].choices if action in DECISION_FOR_ACTION]


class AdminImageDecisionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=DECISION_ACTIONS,
        help_text="approve: the photo becomes accepted. reject: it becomes rejected (a reason is required). keep_in_review: it goes back under review "
                  "(needs_review) and, in a team, into the moderators' queue.",
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=500,
        help_text="At most 500 characters. Required for reject. The owner reads it as the message of an approval or a rejection (an approval without one gets "
                  "a standard sentence); for keep_in_review it is only kept in the audit row and, in a team, as the review's note for the moderators.",
    )

    def validate(self, attrs):
        if attrs["action"] == "reject" and not attrs.get("reason", "").strip():
            raise serializers.ValidationError({"reason": REASON_REQUIRED})
        return attrs


class AdminImageDecisionView(APIView):
    """An administrator accepts, rejects or puts a photo back under review. The rules, effects and errors are in ``operations/image_decision.py``."""

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(
        request=AdminImageDecisionSerializer,
        responses={200: AdminImageDetailSerializer, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
    )
    def post(self, request, pk):
        queryset = _base_queryset().select_related("preprocessing_result")
        image = get_object_or_404(queryset, pk=pk)
        serializer = AdminImageDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decide_image(
            image=image, actor=request.user, action=serializer.validated_data["action"],
            reason=serializer.validated_data.get("reason", ""), request=request,
        )
        # Read again: the body shows the decision now in force (quality.decision), not the one the photo had when it was loaded.
        return Response(AdminImageDetailSerializer(get_object_or_404(queryset, pk=pk)).data)


class AdminImagePreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(responses={(200, "image/jpeg"): OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        from thumbnails.services import ThumbnailUnavailable, default_size, get_or_create_thumbnail

        image = get_object_or_404(ImageUpload.objects.filter(status="uploaded"), pk=pk)
        try:
            thumbnail = get_or_create_thumbnail(image, default_size())
        except ThumbnailUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        _audit(request, image, "admin_image_previewed")
        response = FileResponse(thumbnail.file.open("rb"), content_type="image/jpeg")
        response["Cache-Control"] = "private, no-store"
        return response
