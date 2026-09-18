from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import AuthEvent
from .dashboard import build_user_dashboard
from .permissions import IsAdminRole
from .serializers import (
    AdminUserSerializer,
    AuthEventSerializer,
    ChangePasswordSerializer,
    DeleteAccountSerializer,
    FaceCloudTokenObtainPairSerializer,
    FaceCloudTokenRefreshSerializer,
    LogoutSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
)
from .services import (
    record_auth_event,
    send_password_reset_email,
    send_verification_email,
)

User = get_user_model()


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_scope = "registration"

    @transaction.atomic
    def perform_create(self, serializer):
        user = serializer.save()
        send_verification_email(user)
        record_auth_event("registered", self.request, user, user.email)


class FaceCloudTokenObtainPairView(TokenObtainPairView):
    serializer_class = FaceCloudTokenObtainPairSerializer
    throttle_scope = "login"


class FaceCloudTokenRefreshView(TokenRefreshView):
    serializer_class = FaceCloudTokenRefreshSerializer
    throttle_scope = "token_refresh"


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT})
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response({"detail": "Invalid link."}, status=status.HTTP_400_BAD_REQUEST)
        if user.is_email_verified:
            return Response({"detail": "Email is already verified."})
        if not default_token_generator.check_token(user, token):
            return Response(
                {"detail": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])
        record_auth_event("email_verified", request, user, user.email)
        return Response({"detail": "Email verified successfully."})


class ResendVerificationView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        email = request.data.get("email", "").strip().lower()
        user = User.objects.filter(email__iexact=email, is_email_verified=False).first()
        if user and user.is_active and not user.is_suspended:
            send_verification_email(user)
        return Response({"detail": "If the account is eligible, a verification email has been sent."})


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def perform_update(self, serializer):
        old_username = self.request.user.username
        old_email = self.request.user.email
        user = serializer.save()
        changed = []
        if user.username != old_username:
            changed.append("username")
        if user.email != old_email:
            user.is_email_verified = False
            user.token_version += 1
            user.save(update_fields=["is_email_verified", "token_version"])
            send_verification_email(user)
            changed.append("email")
        record_auth_event("profile_updated", self.request, user, user.email, {"fields": changed})


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "password_change"

    @extend_schema(request=ChangePasswordSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.token_version += 1
        user.save(update_fields=["password", "token_version"])
        record_auth_event("password_changed", request, user, user.email)
        return Response({"detail": "Password changed. Please log in again."})


class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "password_reset"

    @extend_schema(request=PasswordResetRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"],
            is_active=True,
            is_suspended=False,
        ).first()
        if user:
            send_password_reset_email(user)
        return Response({"detail": "If an eligible account exists, a password reset email has been sent."})


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "password_reset"

    @extend_schema(request=PasswordResetConfirmSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.set_password(serializer.validated_data["new_password"])
        user.token_version += 1
        user.save(update_fields=["password", "token_version"])
        record_auth_event("password_reset", request, user, user.email)
        return Response({"detail": "Password reset successfully. Please log in."})


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=LogoutSerializer, responses={204: None, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError:
            return Response({"detail": "Invalid refresh token."}, status=status.HTTP_400_BAD_REQUEST)
        record_auth_event("logout", request, request.user, request.user.email)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LogoutAllView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        user = request.user
        user.token_version += 1
        user.save(update_fields=["token_version"])
        record_auth_event("logout_all", request, user, user.email)
        return Response({"detail": "Logged out from all devices."})


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=DeleteAccountSerializer, responses={204: None})
    @transaction.atomic
    def delete(self, request):
        serializer = DeleteAccountSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        identifier = user.email
        event = record_auth_event("account_deleted", request, user, identifier)
        user.delete()
        event.refresh_from_db()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AuthEventListView(generics.ListAPIView):
    serializer_class = AuthEventSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return AuthEvent.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        events = self.get_queryset()[:100]
        data = [
            {
                "event": item.event,
                "identifier": item.identifier,
                "ip_address": item.ip_address,
                "details": item.details,
                "created_at": item.created_at,
            }
            for item in events
        ]
        return Response(data)


class UserDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request):
        return Response(build_user_dashboard(request.user))


class AdminUserListView(generics.ListAPIView):
    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]


class AdminUserUpdateView(generics.UpdateAPIView):
    queryset = User.objects.all()
    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    http_method_names = ["patch"]

    @transaction.atomic
    def perform_update(self, serializer):
        target = self.get_object()
        if target == self.request.user and (
            serializer.validated_data.get("is_suspended") is True
            or serializer.validated_data.get("is_active") is False
            or serializer.validated_data.get("role") not in (None, "admin")
        ):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"detail": "Administrators cannot remove their own access."})
        previous = {"role": target.role, "is_suspended": target.is_suspended, "is_active": target.is_active}
        user = serializer.save()
        changed = [key for key, value in previous.items() if getattr(user, key) != value]
        if changed:
            user.token_version += 1
            user.save(update_fields=["token_version"])
        if "role" in changed:
            record_auth_event("role_changed", self.request, user, user.email, {"role": user.role})
        if "is_suspended" in changed:
            event = "suspended" if user.is_suspended else "reactivated"
            record_auth_event(event, self.request, user, user.email, {"reason": user.suspension_reason})
