"""The trash endpoints (``/api/images/trash/...``); the rules are in ``uploads/trash.py``."""
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_field
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import trash
from .models import ImageUpload
from .views import BULK_DELETE_LIMIT, MyImagesPagination

EMPTY_LIMIT = 500


class TrashItemSerializer(serializers.ModelSerializer):
    thumbnail_url = serializers.SerializerMethodField()
    purge_at = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()

    class Meta:
        model = ImageUpload
        fields = [
            "id", "original_name", "file_extension", "mime_type", "file_size", "width", "height", "thumbnail_url", "is_favorite", "taken_at", "created_at",
            "trashed_at", "purge_at", "days_left",
        ]

    def get_thumbnail_url(self, obj) -> str | None:
        if not obj.original_file:
            return None
        request = self.context.get("request")
        path = f"/api/images/trash/{obj.pk}/thumbnail/"
        return request.build_absolute_uri(path) if request else path

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_purge_at(self, obj):
        return trash.purge_at(obj)

    def get_days_left(self, obj) -> int | None:
        return trash.days_left(obj, self.context.get("now"))


class TrashIdsSerializer(serializers.Serializer):
    image_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=BULK_DELETE_LIMIT, allow_empty=False,
        help_text=f"Up to {BULK_DELETE_LIMIT} photo ids.",
    )


class SkippedSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    reason = serializers.ChoiceField(
        choices=[trash.STILL_PROCESSING, trash.IN_REVIEW],
        help_text="still_processing: its checks or an enhancement are still running, try again in a moment. in_review: it waits for an organization moderator.",
    )


class TrashMoveResultSerializer(serializers.Serializer):
    trashed_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Photos now in the trash.")
    skipped = SkippedSerializer(many=True, help_text="Photos that could not be moved right now, with the reason; nothing was done for them.")
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Ids that are not yours, do not exist, are not stored photographs or are already in the trash.")
    retention_days = serializers.IntegerField(help_text="Days a photograph stays restorable.")


class TrashRestoreResultSerializer(serializers.Serializer):
    restored_ids = serializers.ListField(child=serializers.IntegerField())
    not_found_ids = serializers.ListField(child=serializers.IntegerField(), help_text="Ids that are not in your trash.")
    folders_restored = serializers.IntegerField(help_text="Person-folder memberships put back.")


class TrashEmptyRequestSerializer(serializers.Serializer):
    confirm = serializers.BooleanField(help_text="Must be true: this deletes the photographs in the trash for good.")

    def validate_confirm(self, value):
        if value is not True:
            raise serializers.ValidationError("Send confirm: true to empty the trash. This cannot be undone.")
        return value


class TrashEmptyResultSerializer(serializers.Serializer):
    deleted = serializers.IntegerField()
    remaining = serializers.IntegerField(help_text=f"Photos still in the trash (at most {EMPTY_LIMIT} are deleted per call): call again while this is above 0.")


def _in_trash(user):
    return ImageUpload.all_objects.in_trash().filter(owner=user, status="uploaded")


class TrashView(generics.ListAPIView):
    """``GET``: the photographs in your trash, newest first. ``POST``: move photographs to the trash."""

    serializer_class = TrashItemSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = MyImagesPagination
    throttle_scope = "image_trash"

    def get_queryset(self):
        queryset = _in_trash(self.request.user).order_by("-trashed_at", "-pk")
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(original_name__icontains=search)
        return queryset

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": timezone.now()}

    @extend_schema(parameters=[OpenApiParameter("search", str, required=False, description="Part of the file name.")])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(request=TrashIdsSerializer, responses={200: TrashMoveResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = TrashIdsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        images = list(ImageUpload.objects.filter(owner=request.user, status="uploaded", pk__in=wanted))
        outcome = trash.move_to_trash(images)
        moved = {image.pk for image in outcome["trashed"]}
        found = {image.pk for image in images}
        return Response({
            "trashed_ids": [pk for pk in wanted if pk in moved],
            "skipped": [{"id": pk, "reason": reason} for pk, reason in ((pk, outcome["skipped"].get(pk)) for pk in wanted) if reason],
            "not_found_ids": [pk for pk in wanted if pk not in found],
            "retention_days": settings.TRASH_RETENTION_DAYS,
        })


class TrashRestoreView(APIView):
    """Take photographs out of the trash: they return to My Images, their albums and, where possible, their person folders."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_trash"

    @extend_schema(request=TrashIdsSerializer, responses={200: TrashRestoreResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = TrashIdsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        images = list(_in_trash(request.user).filter(pk__in=wanted))
        outcome = trash.restore(images)
        restored = {image.pk for image in outcome["restored"]}
        return Response({
            "restored_ids": [pk for pk in wanted if pk in restored],
            "not_found_ids": [pk for pk in wanted if pk not in restored],
            "folders_restored": outcome["folders_restored"],
        })


class TrashEmptyView(APIView):
    """Delete the photographs in your trash for good (up to 500 per call; call again while ``remaining`` is above 0)."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_trash"

    @extend_schema(request=TrashEmptyRequestSerializer, responses={200: TrashEmptyResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        TrashEmptyRequestSerializer(data=request.data).is_valid(raise_exception=True)
        images = list(_in_trash(request.user).order_by("trashed_at", "pk")[:EMPTY_LIMIT])
        deleted = trash.purge(images)["deleted"]
        return Response({"deleted": deleted, "remaining": _in_trash(request.user).count()})


class TrashItemView(APIView):
    """Delete one photograph in the trash for good."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "image_trash"

    @extend_schema(request=None, responses={204: None, 404: OpenApiTypes.OBJECT})
    def delete(self, request, pk):
        image = get_object_or_404(_in_trash(request.user), pk=pk)
        trash.purge([image])
        return Response(status=status.HTTP_204_NO_CONTENT)
