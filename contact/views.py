from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .serializers import ContactAcceptedSerializer, ContactMessageSerializer
from .services import contact_recipients, submit_message

ACCEPTED = "Your message was received and queued for delivery to the FaceCloud team."


class ContactMessageView(APIView):
    """Public contact form. 202 means accepted for delivery, not yet delivered.

    503 when no recipient is configured: the form must not pretend to work.
    Throttled per client address (scope "contact").
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact"

    @extend_schema(
        request=ContactMessageSerializer,
        responses={202: ContactAcceptedSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT,
                   503: OpenApiTypes.OBJECT},
        auth=[],
    )
    def post(self, request):
        payload = ContactMessageSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        if not contact_recipients():
            return Response({"detail": "The contact form is not available at the moment."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        fields = dict(payload.validated_data)
        if fields.pop("website", ""):
            # Answer like a real submission so a bot learns nothing, but keep nothing.
            return Response({"id": None, "status": "queued", "detail": ACCEPTED}, status=status.HTTP_202_ACCEPTED)
        message = submit_message(**fields)
        return Response({"id": message.pk, "status": "queued", "detail": ACCEPTED}, status=status.HTTP_202_ACCEPTED)
