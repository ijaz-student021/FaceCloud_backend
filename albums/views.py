from django.conf import settings
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload
from uploads.serializers import MyImageSerializer
from uploads.views import MyImagesPagination, my_images_queryset

from .models import Album, AlbumImage
from .serializers import (
    AddResultSerializer, AlbumPatchSerializer, AlbumSerializer, AlbumWriteSerializer, CoverRequestSerializer, ImageIdsSerializer, RemoveResultSerializer,
)

# What "a photograph that is in the album" means: still stored. One place, so a rule about which photographs are visible changes here only.
VISIBLE = Q(items__image__status="uploaded")


class AlbumPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 100


def albums_for(user):
    """The caller's albums with ``photo_count`` and the ids the cover is chosen from, in one query."""
    first = AlbumImage.objects.filter(album=OuterRef("pk"), image__status="uploaded").order_by("added_at", "pk").values("image_id")[:1]
    chosen = ImageUpload.objects.filter(pk=OuterRef("cover_image_id"), status="uploaded").values("pk")[:1]
    return Album.objects.filter(owner=user).annotate(
        photo_count=Count("items", filter=VISIBLE, distinct=True), first_image_id=Subquery(first), visible_cover_id=Subquery(chosen),
    )


def add_images(album, image_ids):
    """Put photographs in ``album``: ``(added, already_in_album, not_found)`` as id lists in request order. Idempotent; never adds a photograph that is not the owner's."""
    wanted = list(dict.fromkeys(image_ids))
    found = set(ImageUpload.objects.filter(owner=album.owner, status="uploaded", pk__in=wanted).values_list("pk", flat=True))
    existing = set(AlbumImage.objects.filter(album=album, image_id__in=found).values_list("image_id", flat=True))
    to_add = [pk for pk in wanted if pk in found and pk not in existing]
    room = settings.ALBUM_MAX_IMAGES - AlbumImage.objects.filter(album=album).count()
    if len(to_add) > room:
        raise ValidationError({"image_ids": f"An album holds at most {settings.ALBUM_MAX_IMAGES} photographs; this one has room for {max(room, 0)} more."})
    AlbumImage.objects.bulk_create([AlbumImage(album=album, image_id=pk) for pk in to_add], ignore_conflicts=True)
    if to_add:
        Album.objects.filter(pk=album.pk).update(updated_at=timezone.now())
    return to_add, [pk for pk in wanted if pk in existing], [pk for pk in wanted if pk not in found]


class AlbumListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AlbumPagination
    throttle_scope = "album"

    def get_serializer_class(self):
        return AlbumWriteSerializer if self.request.method == "POST" else AlbumSerializer

    def get_queryset(self):
        queryset = albums_for(self.request.user)
        query = self.request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(title__icontains=query)
        image = self.request.query_params.get("image", "")
        if image:
            if not image.isdigit():
                raise ValidationError({"image": "Image must be a positive integer."})
            queryset = queryset.filter(items__image_id=int(image))
        return queryset.order_by("-updated_at", "-pk")

    @extend_schema(
        operation_id="albums_list",
        parameters=[
            OpenApiParameter("q", str, required=False, description="Titles containing this text."),
            OpenApiParameter("image", int, required=False, description="Only the albums that contain this photograph (to show where a photo already is)."),
        ],
        responses={200: AlbumSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(operation_id="albums_create", request=AlbumWriteSerializer, responses={201: AlbumSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request, *args, **kwargs):
        payload = AlbumWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        if Album.objects.filter(owner=request.user).count() >= settings.ALBUM_MAX_PER_USER:
            raise ValidationError({"title": f"You already have the most albums allowed ({settings.ALBUM_MAX_PER_USER}). Delete one first."})
        album = Album.objects.create(owner=request.user, title=payload.validated_data["title"], description=payload.validated_data.get("description", ""))
        added, already, missing = add_images(album, payload.validated_data.get("image_ids", []))
        body = AlbumSerializer(albums_for(request.user).get(pk=album.pk), context={"request": request}).data
        return Response({**body, "added_ids": added, "not_found_ids": missing}, status=status.HTTP_201_CREATED)


class AlbumDetailView(generics.RetrieveUpdateDestroyAPIView):
    http_method_names = ["get", "patch", "delete", "head", "options"]          # no PUT: a change is a PATCH of the fields that change
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = AlbumSerializer
    throttle_scope = "album"

    def get_queryset(self):
        return albums_for(self.request.user)

    @extend_schema(operation_id="albums_partial_update", request=AlbumPatchSerializer, responses={200: AlbumSerializer, 400: OpenApiTypes.OBJECT})
    def patch(self, request, *args, **kwargs):
        album = self.get_object()
        payload = AlbumPatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        fields = list(payload.validated_data)
        for field in fields:
            setattr(album, field, payload.validated_data[field])
        if fields:
            album.save(update_fields=[*fields, "updated_at"])
        return Response(AlbumSerializer(albums_for(request.user).get(pk=album.pk), context={"request": request}).data)


class AlbumImagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "album"

    def _album(self, request, pk):
        return get_object_or_404(Album.objects.filter(owner=request.user), pk=pk)

    @extend_schema(operation_id="albums_images_list", responses={200: MyImageSerializer(many=True)})
    def get(self, request, pk):
        """The photographs in the album, the most recently added first, as the same rows the gallery lists."""
        album = self._album(request, pk)
        added = AlbumImage.objects.filter(album=album, image_id=OuterRef("pk")).values("added_at")[:1]
        photos = my_images_queryset(request.user).filter(album_items__album=album, status="uploaded").annotate(added_at=Subquery(added)).order_by("-added_at", "-pk")
        paginator = MyImagesPagination()
        page = paginator.paginate_queryset(photos, request, view=self)
        return paginator.get_paginated_response(MyImageSerializer(page, many=True, context={"request": request}).data)

    @extend_schema(operation_id="albums_images_create", request=ImageIdsSerializer, responses={200: AddResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request, pk):
        """Put photographs in the album (idempotent; ids that are not yours are reported, not added)."""
        album = self._album(request, pk)
        payload = ImageIdsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        added, already, missing = add_images(album, payload.validated_data["image_ids"])
        count = albums_for(request.user).get(pk=album.pk).photo_count
        return Response({"added_ids": added, "already_in_album_ids": already, "not_found_ids": missing, "photo_count": count})


class AlbumImagesRemoveView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "album"

    @extend_schema(operation_id="albums_images_remove_create", request=ImageIdsSerializer, responses={200: RemoveResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request, pk):
        """Take photographs out of the album. The photographs themselves are never deleted."""
        album = get_object_or_404(Album.objects.filter(owner=request.user), pk=pk)
        payload = ImageIdsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        present = set(AlbumImage.objects.filter(album=album, image_id__in=wanted).values_list("image_id", flat=True))
        AlbumImage.objects.filter(album=album, image_id__in=present).delete()
        if album.cover_image_id in present:
            album.cover_image = None
            album.save(update_fields=["cover_image", "updated_at"])
        elif present:
            Album.objects.filter(pk=album.pk).update(updated_at=timezone.now())
        count = albums_for(request.user).get(pk=album.pk).photo_count
        return Response({"removed_ids": [pk for pk in wanted if pk in present], "not_in_album_ids": [pk for pk in wanted if pk not in present], "photo_count": count})


class AlbumCoverView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "album"

    @extend_schema(operation_id="albums_cover_update", request=CoverRequestSerializer, responses={200: AlbumSerializer, 400: OpenApiTypes.OBJECT})
    def put(self, request, pk):
        """Choose the cover from the photographs that are in the album."""
        album = get_object_or_404(Album.objects.filter(owner=request.user), pk=pk)
        payload = CoverRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        image_id = payload.validated_data["image_id"]
        if not AlbumImage.objects.filter(album=album, image_id=image_id, image__status="uploaded").exists():
            raise ValidationError({"image_id": "That photograph is not in this album."})
        album.cover_image_id = image_id
        album.save(update_fields=["cover_image", "updated_at"])
        return Response(AlbumSerializer(albums_for(request.user).get(pk=album.pk), context={"request": request}).data)

    @extend_schema(operation_id="albums_cover_destroy", request=None, responses={200: AlbumSerializer})
    def delete(self, request, pk):
        """Go back to the automatic cover (the first photograph added)."""
        album = get_object_or_404(Album.objects.filter(owner=request.user), pk=pk)
        if album.cover_image_id is not None:
            album.cover_image = None
            album.save(update_fields=["cover_image", "updated_at"])
        return Response(AlbumSerializer(albums_for(request.user).get(pk=album.pk), context={"request": request}).data)
