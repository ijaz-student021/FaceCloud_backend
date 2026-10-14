import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    PolymorphicProxySerializer,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from organizations.services import create_personal_organization

from . import lockout
from .models import PendingRegistration
from .dashboard import build_user_dashboard
from .permissions import IsAdminRole
from .sessions import revoke as revoke_session
from .sessions import revoke_all as revoke_all_sessions
from .sessions import session_for_token
from .serializers import (
    AdminUserSerializer,
    ChangePasswordSerializer,
    DeleteAccountSerializer,
    FaceCloudTokenObtainPairSerializer,
    FaceCloudTokenRefreshSerializer,
    LogoutSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ProfileUpdateSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    TwoFactorCodeSerializer,
    TwoFactorCodesResponseSerializer,
    TwoFactorLoginVerifySerializer,
    TwoFactorRequiredResponseSerializer,
    TwoFactorSetupResponseSerializer,
    TwoFactorStatusSerializer,
    UserSerializer,
    VerifyRegistrationSerializer,
)
from .services import (
    confirm_two_factor,
    disable_two_factor,
    generate_verification_code,
    mask_email,
    record_auth_event,
    regenerate_recovery_codes,
    send_password_reset_email,
    send_signup_verification_code,
    send_verification_email,
    setup_two_factor,
    two_factor_status,
    verify_two_factor_login,
)

User = get_user_model()
logger = logging.getLogger(__name__)


LoginResponseSerializer = inline_serializer("LoginResponse", {
    "access": serializers.CharField(help_text="Short-lived (15 min) access token."),
    "refresh": serializers.CharField(help_text="Rotating refresh token (7 days)."),
    "role": serializers.ChoiceField(choices=User.ROLE_CHOICES),
    "dashboard": serializers.CharField(
        help_text="Which dashboard the frontend should route to: user, moderator or admin.",
    ),
    "user": UserSerializer(),
})


@extend_schema_view(
    post=extend_schema(
        summary="Start registration",
        description=(
            "Creates a pending registration and emails a six-digit verification code. "
            "No account exists until the code is verified with POST /verify-email/."
        ),
        request=RegisterSerializer,
        responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample(
                "New account",
                value={"username": "alice", "email": "alice@example.com", "password": "A-long-unique-passphrase-7"},
                request_only=True,
            ),
            OpenApiExample(
                "Email already used",
                value={"email": ["A user with this email already exists."]},
                response_only=True, status_codes=["400"],
            ),
        ],
    )
)
class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "registration"

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]
        code = generate_verification_code()
        now = timezone.now()
        expires_at = now + timedelta(
            seconds=settings.SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS
        )
        resend_at = now + timedelta(
            seconds=settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
        )

        try:
            with transaction.atomic():
                active = list(
                    PendingRegistration.objects.select_for_update().filter(
                        Q(username__iexact=username) | Q(email__iexact=email),
                        status__in=PendingRegistration.ACTIVE_STATUSES,
                    )
                )
                conflicting = next(
                    (
                        item
                        for item in active
                        if item.username.lower() != username.lower()
                        or item.email.lower() != email.lower()
                    ),
                    None,
                )
                if conflicting:
                    return Response(
                        {
                            "detail": (
                                "An active registration already uses this "
                                "username or email address."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                pending = active[0] if active else PendingRegistration()
                pending.username = username
                pending.email = email
                pending.password_hash = make_password(password)
                pending.code_hash = make_password(code)
                pending.status = PendingRegistration.STATUS_PENDING
                pending.failed_attempts = 0
                pending.resend_count = 1
                pending.resend_window_started_at = now
                pending.code_expires_at = expires_at
                pending.resend_available_at = resend_at
                pending.consumed_at = None
                pending.created_user = None
                pending.save()

                send_signup_verification_code(email, code)
                record_auth_event(
                    "registration_pending",
                    request,
                    identifier=email,
                    details={"registration_id": str(pending.public_id)},
                )
                record_auth_event(
                    "verification_code_sent",
                    request,
                    identifier=email,
                    details={"registration_id": str(pending.public_id)},
                )
        except Exception:
            logger.exception("Unable to send signup verification email.")
            return Response(
                {"detail": "Verification email delivery is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "registration_id": str(pending.public_id),
                "email": mask_email(email),
                "detail": "A six-digit verification code has been sent.",
                "expires_in_seconds": settings.SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS,
                "resend_available_in_seconds": (
                    settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )


@extend_schema_view(
    post=extend_schema(
        summary="Log in",
        description=(
            "Provide exactly one of identifier, username or email, plus the password. "
            "When the account has two-factor authentication enabled this returns "
            "`two_factor_required` and a `login_token` instead of tokens -- "
            "POST that to `/api/auth/2fa/verify/` with a code to finish signing in."
        ),
        request=FaceCloudTokenObtainPairSerializer,
        responses={
            200: PolymorphicProxySerializer(
                component_name="LoginResult",
                serializers=[LoginResponseSerializer, TwoFactorRequiredResponseSerializer],
                resource_type_field_name=None,
            ),
            400: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample(
                "Log in with an email", value={"email": "alice@example.com", "password": "A-long-unique-passphrase-7"},
                request_only=True,
            ),
            OpenApiExample(
                "Wrong credentials, unverified, suspended or inactive account",
                value={"detail": ["Invalid username/email or password."]},
                response_only=True, status_codes=["400"],
            ),
            OpenApiExample(
                "Two-factor authentication is enabled",
                value={"two_factor_required": True, "login_token": "opaque-signed-token"},
                response_only=True, status_codes=["200"],
            ),
        ],
    )
)
class FaceCloudTokenObtainPairView(TokenObtainPairView):
    serializer_class = FaceCloudTokenObtainPairSerializer
    throttle_scope = "login"


@extend_schema_view(
    post=extend_schema(
        summary="Finish logging in with a two-factor code",
        description="Exchange the login_token from a password-verified login plus a device code (or recovery code) for real tokens.",
        request=TwoFactorLoginVerifySerializer,
        responses={200: LoginResponseSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
)
class TwoFactorLoginVerifyView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "login"

    def post(self, request):
        payload = TwoFactorLoginVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        result = verify_two_factor_login(
            payload.validated_data["login_token"], payload.validated_data["code"], request=request,
        )
        return Response(result)


class TwoFactorSetupView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "two_factor"

    @extend_schema(
        summary="Start two-factor setup",
        description="Generates a new secret, not yet enforced at login until confirmed with a real code.",
        request=None,
        responses={200: TwoFactorSetupResponseSerializer, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        return Response(setup_two_factor(request.user))


class TwoFactorConfirmView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "two_factor"

    @extend_schema(
        summary="Confirm two-factor setup",
        description="Proves one real code from the device, turns two-factor on, and returns one-time recovery codes.",
        request=TwoFactorCodeSerializer,
        responses={200: TwoFactorCodesResponseSerializer, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        payload = TwoFactorCodeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        codes = confirm_two_factor(request.user, payload.validated_data["code"], request=request)
        return Response({"recovery_codes": codes})


class TwoFactorStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="Whether two-factor authentication is enabled", responses={200: TwoFactorStatusSerializer})
    def get(self, request):
        return Response(two_factor_status(request.user))


class TwoFactorDisableView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "two_factor"

    @extend_schema(
        summary="Disable two-factor authentication",
        description="Requires a current device code or an unused recovery code -- a signed-in session alone is not enough.",
        request=TwoFactorCodeSerializer,
        responses={204: None, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        payload = TwoFactorCodeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        disable_two_factor(request.user, payload.validated_data["code"], request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TwoFactorRecoveryCodesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "two_factor"

    @extend_schema(
        summary="Regenerate recovery codes",
        description="Invalidates every existing recovery code and issues a fresh set, proven by a live device code.",
        request=TwoFactorCodeSerializer,
        responses={200: TwoFactorCodesResponseSerializer, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        payload = TwoFactorCodeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        codes = regenerate_recovery_codes(request.user, payload.validated_data["code"], request=request)
        return Response({"recovery_codes": codes})


class FaceCloudTokenRefreshView(TokenRefreshView):
    serializer_class = FaceCloudTokenRefreshSerializer
    throttle_scope = "token_refresh"


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(
        summary="Verify a signup code",
        description="Verifies the six-digit code and, on success, creates the account.",
        request=VerifyRegistrationSerializer,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        serializer = VerifyRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration_id = serializer.validated_data["registration_id"]
        code = serializer.validated_data["code"]
        now = timezone.now()

        with transaction.atomic():
            pending = (
                PendingRegistration.objects.select_for_update()
                .filter(public_id=registration_id)
                .first()
            )
            if not pending:
                return Response(
                    {"detail": "Invalid or expired verification request."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                pending.status == PendingRegistration.STATUS_CONSUMED
                and pending.created_user
            ):
                return Response(
                    {
                        "detail": "Account has already been created.",
                        "user": UserSerializer(pending.created_user).data,
                        "login_required": True,
                    },
                    status=status.HTTP_200_OK,
                )
            if pending.status == PendingRegistration.STATUS_LOCKED:
                return Response(
                    {"detail": "Too many incorrect attempts. Request a new code."},
                    status=status.HTTP_423_LOCKED,
                )
            if pending.status in {
                PendingRegistration.STATUS_CANCELLED,
                PendingRegistration.STATUS_EXPIRED,
            }:
                return Response(
                    {"detail": "Invalid or expired verification request."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if pending.code_expires_at <= now:
                pending.status = PendingRegistration.STATUS_EXPIRED
                pending.save(update_fields=["status", "updated_at"])
                return Response(
                    {
                        "detail": (
                            "The verification code has expired. Request a new code."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not check_password(code, pending.code_hash):
                pending.failed_attempts += 1
                attempts_remaining = max(
                    0,
                    settings.SIGNUP_VERIFICATION_MAX_ATTEMPTS
                    - pending.failed_attempts,
                )
                if attempts_remaining == 0:
                    pending.status = PendingRegistration.STATUS_LOCKED
                pending.save(
                    update_fields=["failed_attempts", "status", "updated_at"]
                )
                response_status = (
                    status.HTTP_423_LOCKED
                    if attempts_remaining == 0
                    else status.HTTP_400_BAD_REQUEST
                )
                return Response(
                    {
                        "detail": (
                            "Too many incorrect attempts. Request a new code."
                            if attempts_remaining == 0
                            else "Incorrect verification code."
                        ),
                        "attempts_remaining": attempts_remaining,
                    },
                    status=response_status,
                )

            if User.objects.filter(
                Q(username__iexact=pending.username)
                | Q(email__iexact=pending.email)
            ).exists():
                pending.status = PendingRegistration.STATUS_CANCELLED
                pending.password_hash = ""
                pending.code_hash = ""
                pending.save(
                    update_fields=[
                        "status",
                        "password_hash",
                        "code_hash",
                        "updated_at",
                    ]
                )
                return Response(
                    {
                        "detail": (
                            "The username or email was claimed. "
                            "Restart registration."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                with transaction.atomic():
                    user = User(
                        username=pending.username,
                        email=pending.email,
                        role="user",
                        is_email_verified=True,
                    )
                    user.password = pending.password_hash
                    user.save(force_insert=True)
                    create_personal_organization(user)
            except IntegrityError:
                pending.status = PendingRegistration.STATUS_CANCELLED
                pending.password_hash = ""
                pending.code_hash = ""
                pending.save(
                    update_fields=[
                        "status",
                        "password_hash",
                        "code_hash",
                        "updated_at",
                    ]
                )
                return Response(
                    {
                        "detail": (
                            "The username or email was claimed. "
                            "Restart registration."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            pending.status = PendingRegistration.STATUS_CONSUMED
            pending.consumed_at = now
            pending.created_user = user
            pending.password_hash = ""
            pending.code_hash = ""
            pending.save(
                update_fields=[
                    "status",
                    "consumed_at",
                    "created_user",
                    "password_hash",
                    "code_hash",
                    "updated_at",
                ]
            )
            record_auth_event("registered", request, user, user.email)
            record_auth_event("email_verified", request, user, user.email)

        return Response(
            {
                "detail": "Email verified and account created successfully.",
                "user": UserSerializer(user).data,
                "login_required": True,
            },
            status=status.HTTP_201_CREATED,
        )


class ResendVerificationView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(
        summary="Resend a signup code",
        description="Invalidates the current code and sends a new one to the same pending registration.",
        request=ResendVerificationSerializer,
        responses={200: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration_id = serializer.validated_data["registration_id"]
        now = timezone.now()
        generic_detail = (
            "If the registration is eligible, a new verification code has been sent."
        )
        code = generate_verification_code()

        try:
            with transaction.atomic():
                pending = (
                    PendingRegistration.objects.select_for_update()
                    .filter(public_id=registration_id)
                    .first()
                )
                if not pending or pending.status in {
                    PendingRegistration.STATUS_CONSUMED,
                    PendingRegistration.STATUS_CANCELLED,
                }:
                    return Response({"detail": generic_detail})
                if pending.resend_available_at > now:
                    retry_after = max(
                        1,
                        int((pending.resend_available_at - now).total_seconds()),
                    )
                    return Response(
                        {
                            "detail": "Please wait before requesting another code.",
                            "retry_after_seconds": retry_after,
                        },
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )

                window_age = now - pending.resend_window_started_at
                if window_age >= timedelta(hours=1):
                    pending.resend_count = 0
                    pending.resend_window_started_at = now
                if (
                    pending.resend_count
                    >= settings.SIGNUP_VERIFICATION_MAX_SENDS_PER_HOUR
                ):
                    retry_after = max(
                        1,
                        int(
                            (
                                pending.resend_window_started_at
                                + timedelta(hours=1)
                                - now
                            ).total_seconds()
                        ),
                    )
                    return Response(
                        {
                            "detail": "Verification send limit reached.",
                            "retry_after_seconds": retry_after,
                        },
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )

                conflicting_user = User.objects.filter(
                    Q(username__iexact=pending.username)
                    | Q(email__iexact=pending.email)
                ).exists()
                conflicting_pending = PendingRegistration.objects.filter(
                    Q(username__iexact=pending.username)
                    | Q(email__iexact=pending.email),
                    status__in=PendingRegistration.ACTIVE_STATUSES,
                ).exclude(pk=pending.pk).exists()
                if conflicting_user or conflicting_pending:
                    pending.status = PendingRegistration.STATUS_CANCELLED
                    pending.password_hash = ""
                    pending.code_hash = ""
                    pending.save(
                        update_fields=[
                            "status",
                            "password_hash",
                            "code_hash",
                            "updated_at",
                        ]
                    )
                    return Response(
                        {"detail": "Registration can no longer be completed."},
                        status=status.HTTP_409_CONFLICT,
                    )

                pending.code_hash = make_password(code)
                pending.code_expires_at = now + timedelta(
                    seconds=settings.SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS
                )
                pending.resend_available_at = now + timedelta(
                    seconds=settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
                )
                pending.failed_attempts = 0
                pending.resend_count += 1
                pending.status = PendingRegistration.STATUS_PENDING
                pending.save()
                send_signup_verification_code(pending.email, code)
                record_auth_event(
                    "verification_code_sent",
                    request,
                    identifier=pending.email,
                    details={
                        "registration_id": str(pending.public_id),
                        "reason": "resend",
                    },
                )
        except Exception:
            logger.exception("Unable to resend signup verification email.")
            return Response(
                {"detail": "Verification email delivery is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "detail": generic_detail,
                "resend_available_in_seconds": (
                    settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
                ),
            }
        )


class RegistrationStatusView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification_status"

    @extend_schema(
        summary="Read pending-registration status",
        description="Masked status for a registration in progress (no secrets).",
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request, registration_id):
        pending = PendingRegistration.objects.filter(
            public_id=registration_id
        ).first()
        if not pending:
            return Response(
                {"detail": "Invalid or expired verification request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        now = timezone.now()
        public_status = pending.status
        if (
            public_status == PendingRegistration.STATUS_PENDING
            and pending.code_expires_at <= now
        ):
            public_status = PendingRegistration.STATUS_EXPIRED
        return Response(
            {
                "status": public_status,
                "email": mask_email(pending.email),
                "expires_in_seconds": max(
                    0,
                    int((pending.code_expires_at - now).total_seconds()),
                ),
                "resend_available_in_seconds": max(
                    0,
                    int((pending.resend_available_at - now).total_seconds()),
                ),
                "attempts_remaining": max(
                    0,
                    settings.SIGNUP_VERIFICATION_MAX_ATTEMPTS
                    - pending.failed_attempts,
                ),
            }
        )


class VerifyEmailChangeView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(
        summary="Verify a changed email address",
        parameters=[
            OpenApiParameter("uidb64", OpenApiTypes.STR, OpenApiParameter.PATH,
                             description="URL-safe base64 user id from the verification email."),
            OpenApiParameter("token", OpenApiTypes.STR, OpenApiParameter.PATH,
                             description="Signed verification token from the verification email."),
        ],
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def get(self, request, uidb64, token):
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response(
                {"detail": "Invalid link."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileUpdateSerializer
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
        lockout.clear_for_user(user)
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
        # Resetting the password is the legitimate owner's way back in.
        lockout.clear_for_user(user)
        record_auth_event("password_reset", request, user, user.email)
        return Response({"detail": "Password reset successfully. Please log in."})


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=LogoutSerializer, responses={204: None, 400: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token = RefreshToken(serializer.validated_data["refresh"])
            if str(token.get("user_id")) != str(request.user.pk):
                raise TokenError("Token belongs to another account.")
            token.blacklist()
            session = session_for_token(token)
            if session is not None and session.user_id == request.user.pk:
                revoke_session(session, "logout")
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
        revoke_all_sessions(user, "logout_all")
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
        if user.avatar:
            avatar_name = user.avatar.name
            transaction.on_commit(lambda: user.avatar.storage.delete(avatar_name))
        user.delete()
        event.refresh_from_db()
        return Response(status=status.HTTP_204_NO_CONTENT)


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


class AvatarUploadSerializer(serializers.Serializer):
    avatar = serializers.FileField(help_text="JPEG, PNG or WebP, at most 5 MB. Stored as a PNG of at most 512 px.")


class AvatarView(APIView):
    """The signed-in user's own avatar: fetch, replace or remove.

    Served only to its owner through this authenticated endpoint; files are
    never public. The response to PUT is the updated profile.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={(200, "image/png"): OpenApiTypes.BINARY, 404: OpenApiTypes.OBJECT})
    def get(self, request):
        from django.http import FileResponse, Http404

        user = request.user
        if not user.avatar:
            raise Http404("No avatar has been set.")
        response = FileResponse(user.avatar.open("rb"), content_type="image/png")
        response["Cache-Control"] = "private, max-age=3600"
        return response

    @extend_schema(request={"multipart/form-data": AvatarUploadSerializer}, responses={200: UserSerializer, 400: OpenApiTypes.OBJECT})
    def put(self, request):
        from .avatars import set_avatar

        payload = AvatarUploadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        set_avatar(request.user, payload.validated_data["avatar"])
        return Response(UserSerializer(request.user).data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request):
        from .avatars import remove_avatar

        remove_avatar(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserSessionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    device = serializers.CharField(help_text='Parsed from the user agent, e.g. "Chrome on macOS".')
    user_agent = serializers.CharField()
    ip_address = serializers.IPAddressField(allow_null=True)
    created_at = serializers.DateTimeField()
    last_seen_at = serializers.DateTimeField(help_text="Last sign-in or token refresh.")
    expires_at = serializers.DateTimeField()
    is_current = serializers.BooleanField(help_text="The session making this request.")


class UserSessionListView(APIView):
    """The signed-in user's active sessions (devices), most recently active first."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: UserSessionSerializer(many=True)})
    def get(self, request):
        from .sessions import SID_CLAIM, active_sessions, describe_device

        current = str((request.auth or {}).get(SID_CLAIM, "")) if request.auth is not None else ""
        rows = [{
            "id": session.pk, "device": describe_device(session.user_agent), "user_agent": session.user_agent,
            "ip_address": session.ip_address, "created_at": session.created_at,
            "last_seen_at": session.last_seen_at, "expires_at": session.expires_at,
            "is_current": str(session.pk) == current,
        } for session in active_sessions(request.user)]
        return Response(UserSessionSerializer(rows, many=True).data)


class UserSessionRevokeView(APIView):
    """Sign one of the user's sessions out, at once.

    Its access tokens stop working immediately and its refresh token is
    blacklisted. Revoking the current session signs this client out too.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={204: None, 404: OpenApiTypes.OBJECT})
    def post(self, request, session_id):
        from django.shortcuts import get_object_or_404

        from .models import UserSession

        session = get_object_or_404(UserSession, pk=session_id, user=request.user, revoked_at__isnull=True)
        revoke_session(session, "revoked_by_user")
        record_auth_event("session_revoked", request, request.user, request.user.email, {"session": str(session.pk)})
        return Response(status=status.HTTP_204_NO_CONTENT)
