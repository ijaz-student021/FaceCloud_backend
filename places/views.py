"""The places endpoints (``/api/places/``) and a photograph's location (``/api/images/{id}/location/``). Coordinates are shown to the photograph's owner and nobody else."""
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from uploads.models import ImageUpload

from . import gazetteer, service
from .queries import places_for
from .serializers import (
    LocationRemoveRequestSerializer, LocationRemoveResultSerializer, LocationSerializer, PlaceBriefSerializer, PlaceSerializer, PlacesResponseSerializer,
)

DEFAULT_LIMIT = 24
MAX_LIMIT = 100


def _limit(request):
    raw = request.query_params.get("limit", "")
    if raw == "":
        return DEFAULT_LIMIT
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationError({"limit": "Limit must be a positive whole number."})
    return min(int(raw), MAX_LIMIT)


class PlaceListView(APIView):
    """The places the caller's photographs were taken in, most photographs first."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "places"

    @extend_schema(
        operation_id="places_list",
        parameters=[
            OpenApiParameter("q", str, required=False, description="Part of a place name."),
            OpenApiParameter("country", str, required=False, description="A two-letter country code, for example PK."),
            OpenApiParameter("limit", int, required=False, description="Default 24, at most 100."),
        ],
        responses={200: PlacesResponseSerializer},
    )
    def get(self, request):
        queryset = places_for(request.user)
        query = request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(name__icontains=query)
        country = request.query_params.get("country", "").strip()
        if country:
            if len(country) != 2 or not country.isalpha():
                raise ValidationError({"country": "Use a two-letter country code."})
            queryset = queryset.filter(country_code=country.upper())
        return Response({
            "attribution": gazetteer.ATTRIBUTION,
            "names_available": gazetteer.available(),
            "places": PlaceSerializer(queryset[: _limit(request)], many=True, context={"request": request}).data,
        })


class PlaceDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "places"

    @extend_schema(operation_id="places_retrieve", responses={200: PlaceSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        place = get_object_or_404(places_for(request.user), pk=pk)
        return Response(PlaceSerializer(place, context={"request": request}).data)


class ImageLocationView(APIView):
    """Where one of the caller's photographs was taken (``GET``) or take that off (``DELETE``)."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "places"

    def _photo(self, request, pk):
        return get_object_or_404(ImageUpload.objects.filter(owner=request.user, status="uploaded").select_related("place"), pk=pk)

    @extend_schema(operation_id="images_location_retrieve", responses={200: LocationSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        photo = self._photo(request, pk)
        if photo.gps_lat is None or photo.gps_lon is None:
            return Response({"detail": "This photograph has no location."}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "latitude": photo.gps_lat, "longitude": photo.gps_lon,
            "place": PlaceBriefSerializer(photo.place).data if photo.place_id else None,
        })

    @extend_schema(operation_id="images_location_destroy", request=None, responses={204: None, 404: OpenApiTypes.OBJECT})
    def delete(self, request, pk):
        service.remove_location([self._photo(request, pk)])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LocationRemoveView(APIView):
    """Take the location off several of the caller's photographs at once."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "places"

    @extend_schema(operation_id="images_location_remove_create", request=LocationRemoveRequestSerializer, responses={200: LocationRemoveResultSerializer, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = LocationRemoveRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        wanted = list(dict.fromkeys(payload.validated_data["image_ids"]))
        photos = {photo.pk: photo for photo in ImageUpload.objects.filter(owner=request.user, status="uploaded", pk__in=wanted)}
        has = [pk for pk in wanted if pk in photos and (photos[pk].gps_lat is not None or photos[pk].place_id)]
        service.remove_location([photos[pk] for pk in has])
        return Response({
            "removed_ids": has,
            "without_location_ids": [pk for pk in wanted if pk in photos and pk not in has],
            "not_found_ids": [pk for pk in wanted if pk not in photos],
        })
