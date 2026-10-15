"""Endpoints of the optional cloud adjustment advisor.

Everything is owner-only (another user's ids are 404), authenticated, and a no-op
until the advisor is enabled and configured. The API key never appears in a
response. The advisor only ever produces *suggestions*; applying one creates an
ordinary edit version through the same task and storage lifecycle as any other.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import advisor_client
from . import advisor_service as service
from .advisor_schema import CAUTION_FLAGS, INTENT_LOCKS
from .controls import CONTROL_NAMES, CONTROLS, INTENTS
from .models import AdvisorRecommendation, AdvisorRun, EnhancementStatus, ImageEnhancementResult
from .serializers import (
    AdvisorConsentRequestSerializer,
    AdvisorRecommendationSerializer,
    AdvisorRecommendRequestSerializer,
    AdvisorRunRequestSerializer,
    AdvisorRunSerializer,
)
from .views import start_edit

DISCLOSURE = {
    "what_is_sent": (
        "One downscaled preview of the enhanced image (re-encoded, so it carries no EXIF, location, ICC profile "
        "or filename), the measured numbers about it (brightness, contrast, clipping, noise, blur), and the "
        "adjustment intent you chose. Never your name, email or account, the original file, face data or "
        "embeddings, or other images."
    ),
    "what_is_not_sent": ["account or contact details", "file names and metadata", "face embeddings or identity data",
                         "archives or other images"],
    "purpose": "To suggest slider settings for this image. The suggestion is advice; you decide whether to apply it.",
    "provider": "OpenAI (Responses API)",
    "retention": (
        "Requests are made with store=false, so OpenAI does not keep the response for later retrieval, and API "
        "data is not used to train its models by default. OpenAI may still retain API request logs for abuse "
        "monitoring, currently up to 30 days, unless the account has zero data retention; images are also scanned "
        "for child-safety material. FaceCloud keeps only the suggestion, token counts and an outcome code, "
        "never the image sent."
    ),
    "separate_from": "Face identification consent; granting one does not grant the other.",
}


class AdvisorConfigurationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        state = advisor_client.availability()
        return Response({
            "available": state["available"],
            "unavailable_reason": state["reason"] or None,
            "provider": "openai",
            "model": settings.OPENAI_ADVISOR_MODEL or None,
            "policy_version": settings.OPENAI_ADVISOR_POLICY_VERSION,
            "consent": service.consent_state(request.user),
            "disclosure": DISCLOSURE,
            "intents": list(INTENTS),
            "locked_controls_by_intent": {intent: list(locks) for intent, locks in INTENT_LOCKS.items()},
            "controls": {name: {"auto_minimum": CONTROLS[name].auto[0], "auto_maximum": CONTROLS[name].auto[1]}
                         for name in CONTROL_NAMES},
            "caution_flags": list(CAUTION_FLAGS),
            "limits": {
                "max_images_per_request": settings.OPENAI_ADVISOR_MAX_IMAGES_PER_RUN,
                "image_edge": settings.OPENAI_ADVISOR_IMAGE_EDGE,
                "recommendation_hours": settings.OPENAI_ADVISOR_RECOMMENDATION_TTL_HOURS,
            },
            "budget": service.budget_state(request.user) if state["available"] else None,
            "cost_note": "Each suggestion is one paid request; do not request one per slider move or camera frame.",
        })


class AdvisorConsentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(service.consent_state(request.user))

    @extend_schema(request=AdvisorConsentRequestSerializer, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = AdvisorConsentRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        return Response(service.record_consent(request.user, data["granted"], data["policy_version"]))


def _result_for(request, result_id):
    return get_object_or_404(
        ImageEnhancementResult.objects.filter(owner=request.user, status=EnhancementStatus.COMPLETED), pk=result_id)


def _serialize(recommendation, request):
    return AdvisorRecommendationSerializer(recommendation, context={"request": request}).data


class AdvisorRecommendationCreateView(APIView):
    """Ask the advisor about one enhanced image."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "advisor"

    @extend_schema(
        request=AdvisorRecommendRequestSerializer,
        responses={202: AdvisorRecommendationSerializer, 200: AdvisorRecommendationSerializer,
                   400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
        description=(
            "202: a job was queued. 200: an identical recommendation already exists (nothing new is sent or paid for). "
            "403 advisor_consent_required: grant consent first. 503 advisor_unavailable: disabled or not configured. "
            "Poll GET advisor/recommendations/{id}/. Whatever happens the job ends with a usable result: the advisor's "
            "validated suggestion, or the local conservative suggestion marked origin=local_fallback with a fallback_reason."
        ),
    )
    def post(self, request, result_id):
        result = _result_for(request, result_id)
        payload = AdvisorRecommendRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        recommendation, created = service.create_recommendation(request.user, result, payload.validated_data["intent"])
        if created:
            recommendation = service.enqueue(recommendation)
        return Response(_serialize(recommendation, request),
                        status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK)


class AdvisorRunCreateView(APIView):
    """Ask the advisor about several images: one independent job per image."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "advisor"

    @extend_schema(request=AdvisorRunRequestSerializer, responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT,
                                                                   403: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = AdvisorRunRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = list(dict.fromkeys(payload.validated_data["result_ids"]))
        found = {result.pk: result for result in ImageEnhancementResult.objects.filter(
            owner=request.user, status=EnhancementStatus.COMPLETED, pk__in=ids)}
        run, items = service.create_run(request.user, [found[i] for i in ids if i in found], payload.validated_data["intent"])
        report = [{"result_id": i, "state": "skipped", "reason": "not_found"} for i in ids if i not in found]
        for item in items:
            if "recommendation" in item:
                item = {**item, "recommendation": _serialize(item["recommendation"], request)}
            report.append(item)
        run = AdvisorRun.objects.prefetch_related("recommendations").get(pk=run.pk)
        return Response({"run": AdvisorRunSerializer(run, context={"request": request}).data, "items": report},
                        status=status.HTTP_202_ACCEPTED)


class AdvisorRunDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _run(self, request, pk):
        return get_object_or_404(AdvisorRun.objects.filter(owner=request.user).prefetch_related("recommendations"), pk=pk)

    @extend_schema(responses={200: AdvisorRunSerializer})
    def get(self, request, pk):
        return Response(AdvisorRunSerializer(self._run(request, pk), context={"request": request}).data)


class AdvisorRunCancelView(AdvisorRunDetailView):
    @extend_schema(request=None, responses={200: AdvisorRunSerializer})
    def post(self, request, pk):
        service.cancel_run(self._run(request, pk))
        return Response(AdvisorRunSerializer(self._run(request, pk), context={"request": request}).data)


class AdvisorRecommendationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _recommendation(self, request, pk):
        return get_object_or_404(
            AdvisorRecommendation.objects.filter(owner=request.user).select_related("enhancement_result"), pk=pk)

    @extend_schema(responses={200: AdvisorRecommendationSerializer})
    def get(self, request, pk):
        return Response(_serialize(self._recommendation(request, pk), request))


class AdvisorRecommendationCancelView(AdvisorRecommendationDetailView):
    @extend_schema(request=None, responses={200: AdvisorRecommendationSerializer})
    def post(self, request, pk):
        service.cancel_recommendation(self._recommendation(request, pk))
        return Response(_serialize(self._recommendation(request, pk), request))


class AdvisorRecommendationApplyView(AdvisorRecommendationDetailView):
    @extend_schema(
        request=None,
        responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
        description=(
            "Create a full-resolution edit version with this recommendation's settings (manual mode, so the "
            "recorded settings are exactly these numbers), through the normal edit task and protected download. "
            "409 recommendation_stale (with reason) when the image changed, a newer edit exists, it was "
            "cancelled or it expired; 400 when there is nothing to apply."
        ),
    )
    def post(self, request, pk):
        recommendation = self._recommendation(request, pk)
        if recommendation.status != "completed":
            raise ValidationError("This recommendation is not finished yet.")
        reason = service.staleness(recommendation)
        if reason:
            return Response({"detail": "This recommendation can no longer be applied.", "code": "recommendation_stale",
                             "reason": reason}, status=status.HTTP_409_CONFLICT)
        if recommendation.action != "apply" or not any(recommendation.adjustments.values()):
            raise ValidationError("This recommendation has nothing to apply.")
        return start_edit(
            request, recommendation.enhancement_result, "manual", dict(recommendation.adjustments),
            claimed_version=recommendation.source_version, advisor_recommendation=recommendation,
        )
