from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdminRole

from .models import ContactMessage, EarlyAccessSignup
from .serializers import (
    AdminContactMessageSerializer, AdminEarlyAccessSerializer, ContactAcceptedSerializer, ContactMessageSerializer, EarlyAccessAcceptedSerializer,
    EarlyAccessRequestSerializer, MarkAllReadSerializer, MarkReadSerializer,
)
from .services import submit_early_access, submit_message

ACCEPTED = "Your message was received and queued for delivery to the FaceCloud team."


class ContactMessageView(APIView):
    """Public contact form. 202 means accepted for delivery, not yet delivered.

    Every message is stored and shown to the administrators (admin inbox + notification); it is also e-mailed to CONTACT_RECIPIENT_EMAILS when that is set.
    Throttled per client address (scope "contact").
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact"

    @extend_schema(
        request=ContactMessageSerializer,
        responses={202: ContactAcceptedSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
        auth=[],
    )
    def post(self, request):
        payload = ContactMessageSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        fields = dict(payload.validated_data)
        if fields.pop("website", ""):
            # Answer like a real submission so a bot learns nothing, but keep nothing.
            return Response({"id": None, "status": "queued", "detail": ACCEPTED}, status=status.HTTP_202_ACCEPTED)
        message = submit_message(**fields)
        return Response({"id": message.pk, "status": "queued", "detail": ACCEPTED}, status=status.HTTP_202_ACCEPTED)


class EarlyAccessView(APIView):
    """The landing page's "Enter your email for early access" box. Public, throttled per client address (scope "early_access"). The same address twice is one signup."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "early_access"

    @extend_schema(request=EarlyAccessRequestSerializer, responses={201: EarlyAccessAcceptedSerializer, 200: EarlyAccessAcceptedSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT}, auth=[])
    def post(self, request):
        payload = EarlyAccessRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        if payload.validated_data["website"]:
            return Response({"status": "registered", "detail": "Thank you. We will be in touch."}, status=status.HTTP_201_CREATED)      # a spam trap: kept nothing
        _signup, created = submit_early_access(payload.validated_data["email"].strip())
        if created:
            return Response({"status": "registered", "detail": "Thank you. We will be in touch."}, status=status.HTTP_201_CREATED)
        return Response({"status": "already_registered", "detail": "This address is already on the early-access list."}, status=status.HTTP_200_OK)


def _filtered(queryset, request, search_fields):
    unread = request.query_params.get("unread", "").strip().lower()
    if unread in ("1", "true", "yes"):
        queryset = queryset.filter(read_at__isnull=True)
    term = request.query_params.get("q", "").strip()
    if term:
        condition = Q()
        for field in search_fields:
            condition |= Q(**{f"{field}__icontains": term})
        queryset = queryset.filter(condition)
    return queryset


def _limit(request, default=100, cap=500):
    raw = request.query_params.get("limit", "")
    return min(int(raw), cap) if raw.isdigit() and int(raw) > 0 else default


class AdminInboxSummaryView(APIView):
    """Counts for the admin inbox badge: what has not been read yet, and the totals."""

    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response({
            "contact_unread": ContactMessage.objects.filter(read_at__isnull=True).count(), "contact_total": ContactMessage.objects.count(),
            "early_access_unread": EarlyAccessSignup.objects.filter(read_at__isnull=True).count(), "early_access_total": EarlyAccessSignup.objects.count(),
        })


class AdminContactMessageListView(APIView):
    """The contact form messages, newest first. ``unread=1`` keeps the unread ones, ``q`` searches name, e-mail, company, reason and message, ``limit`` (default 100, at most 500)."""

    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: AdminContactMessageSerializer(many=True)})
    def get(self, request):
        rows = _filtered(ContactMessage.objects.all(), request, ("name", "email", "company", "reason", "message"))
        total = rows.count()
        data = AdminContactMessageSerializer(rows[: _limit(request)], many=True).data
        return Response({"count": total, "results": data})


class AdminContactMessageDetailView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: AdminContactMessageSerializer, 404: OpenApiTypes.OBJECT})
    def get(self, request, pk):
        return Response(AdminContactMessageSerializer(get_object_or_404(ContactMessage, pk=pk)).data)

    @extend_schema(request=MarkReadSerializer, responses={200: AdminContactMessageSerializer, 404: OpenApiTypes.OBJECT})
    def patch(self, request, pk):
        payload = MarkReadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        message = get_object_or_404(ContactMessage, pk=pk)
        message.read_at = timezone.now() if payload.validated_data["read"] else None
        message.save(update_fields=["read_at"])
        return Response(AdminContactMessageSerializer(message).data)


class AdminEarlyAccessListView(APIView):
    """The early-access e-mail addresses, newest first (same ``unread``, ``q`` and ``limit`` as the messages)."""

    permission_classes = [IsAdminRole]

    @extend_schema(responses={200: AdminEarlyAccessSerializer(many=True)})
    def get(self, request):
        rows = _filtered(EarlyAccessSignup.objects.all(), request, ("email",))
        total = rows.count()
        return Response({"count": total, "results": AdminEarlyAccessSerializer(rows[: _limit(request)], many=True).data})


class AdminEarlyAccessDetailView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(request=MarkReadSerializer, responses={200: AdminEarlyAccessSerializer, 404: OpenApiTypes.OBJECT})
    def patch(self, request, pk):
        payload = MarkReadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        signup = get_object_or_404(EarlyAccessSignup, pk=pk)
        signup.read_at = timezone.now() if payload.validated_data["read"] else None
        signup.save(update_fields=["read_at"])
        return Response(AdminEarlyAccessSerializer(signup).data)


class AdminMarkAllReadView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(request=MarkAllReadSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        payload = MarkAllReadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        kind, now = payload.validated_data["kind"], timezone.now()
        marked = {}
        if kind in ("contact", "all"):
            marked["contact"] = ContactMessage.objects.filter(read_at__isnull=True).update(read_at=now)
        if kind in ("early_access", "all"):
            marked["early_access"] = EarlyAccessSignup.objects.filter(read_at__isnull=True).update(read_at=now)
        return Response({"marked_read": marked})
