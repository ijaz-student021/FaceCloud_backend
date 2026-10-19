import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.tokens import default_token_generator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
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
from .throttles import (
    FailedSignInPerAccountThrottle, FailedSignInPerAddressThrottle, TokenRefreshPerAddressThrottle, TokenRefreshPerTokenThrottle,
    record_failed_sign_in,
)
from .models import PendingRegistration
from .dashboard import build_user_dashboard
from .permissions import IsAdminRole
from .sessions import revoke as revoke_session
from .sessions import revoke_all as revoke_all_sessions
from .sessions import session_for_token
from .serializers import (
    AdminUserDetailSerializer,
    AdminUserCreateSerializer,
    AdminUserSerializer,
    AdminUserUpdateSerializer,
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
    UserDashboardSerializer,
    UserSerializer,
    VerifyRegistrationSerializer,
)
from .services import (
    has_moderator_organization,
    resolve_moderator_organization,
    issue_login_tokens,
    create_account_as_admin,
    derive_username,
    generate_verification_token,
    hash_verification_token,
    split_full_name,
    verification_token_matches,
    mask_email,
    record_auth_event,
    send_password_reset_email,
    send_signup_verification_link,
    send_verification_email,
)

User = get_user_model()
logger = logging.getLogger(__name__)


LoginResponseSerializer = inline_serializer("LoginResponse", {
    "access": serializers.CharField(help_text="Short-lived (15 min) access token."),
    "refresh": serializers.CharField(help_text="Rotating refresh token (7 days)."),
    "role": serializers.ChoiceField(
        choices=User.ROLE_CHOICES,
        help_text="The portal the person belongs to (user, moderator or admin); always equal to `dashboard`.",
    ),
    "dashboard": serializers.CharField(
        help_text="Which dashboard the frontend should route to: user, moderator or admin. A moderator of an organization gets moderator.",
    ),
    "user": UserSerializer(),
    "organization_roles": inline_serializer("OrganizationRole", {
        "organization_id": serializers.IntegerField(),
        "name": serializers.CharField(),
        "role": serializers.CharField(help_text="owner, moderator or member."),
        "status": serializers.CharField(help_text="The organization's status: active or suspended."),
    }, many=True),
})


@extend_schema_view(
    post=extend_schema(
        summary="Start registration",
        description=(
            "Creates a pending registration and emails a verification **link** "
            "(`{FRONTEND_URL}/verify-email?registration_id=...&token=...`). "
            "No account exists until the link's `registration_id` and `token` are sent to POST /verify-email/. "
            "`username` is optional: when omitted it is derived from the email address."
        ),
        request=RegisterSerializer,
        responses={202: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample(
                "New account",
                value={"full_name": "Alice Johnson", "email": "alice@example.com", "password": "A-long-unique-passphrase-7",
                       "password_confirm": "A-long-unique-passphrase-7"},
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
        supplied_username = serializer.validated_data.get("username")
        full_name = serializer.validated_data["full_name"]
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]
        token = generate_verification_token()
        now = timezone.now()
        expires_at = now + timedelta(
            seconds=settings.SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS
        )
        resend_at = now + timedelta(
            seconds=settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
        )

        pending = None
        for attempt in range(3):
            try:
                with transaction.atomic():
                    username = supplied_username
                    if not username:
                        # Registering again with the same address keeps its pending row (and its username) when still free.
                        same_email = PendingRegistration.objects.filter(
                            email__iexact=email, status__in=PendingRegistration.ACTIVE_STATUSES,
                        ).first()
                        if same_email and not User.objects.filter(username__iexact=same_email.username).exists():
                            username = same_email.username
                        else:
                            username = derive_username(email)
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
                    pending.full_name = full_name
                    pending.email = email
                    pending.password_hash = make_password(password)
                    pending.token_hash = hash_verification_token(token)
                    pending.status = PendingRegistration.STATUS_PENDING
                    pending.failed_attempts = 0
                    pending.resend_count = 1
                    pending.resend_window_started_at = now
                    pending.token_expires_at = expires_at
                    pending.resend_available_at = resend_at
                    pending.consumed_at = None
                    pending.created_user = None
                    pending.save()

                    send_signup_verification_link(email, pending.public_id, token, full_name)
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
                        details={"registration_id": str(pending.public_id), "kind": "link"},
                    )
                break
            except IntegrityError:
                # Two sign-ups chose the same derived username at once: derive again. A username the person typed is theirs to change.
                if supplied_username or attempt == 2:
                    return Response(
                        {"detail": "That username was just taken. Please try again."},
                        status=status.HTTP_409_CONFLICT,
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
                "detail": "A verification link has been sent to your email address.",
                "expires_in_seconds": settings.SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS,
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
            "Returns the access and refresh tokens."
        ),
        request=FaceCloudTokenObtainPairSerializer,
        responses={
            200: LoginResponseSerializer,
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
        ],
    )
)
class FaceCloudTokenObtainPairView(TokenObtainPairView):
    """Sign in. Limited by *failed* attempts per (address, account name) and per address (``accounts/throttles.py``), plus the per-account backoff,
    so a room full of people on one address is never locked out by each other, while guessing one account's password is."""

    serializer_class = FaceCloudTokenObtainPairSerializer
    throttle_classes = [FailedSignInPerAccountThrottle, FailedSignInPerAddressThrottle]

    def get_throttles(self):
        # The same instances for the check before the view and the count after it.
        if not hasattr(self, "_sign_in_throttles"):
            self._sign_in_throttles = super().get_throttles()
        return self._sign_in_throttles

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code == status.HTTP_400_BAD_REQUEST:          # wrong password, unknown account, unverified, suspended...
            record_failed_sign_in(request, self.get_throttles())
        return response


class FaceCloudTokenRefreshView(TokenRefreshView):
    """Refresh an access token. Limited per (address, refresh token), so many people refreshing from one address each have their own allowance."""

    serializer_class = FaceCloudTokenRefreshSerializer
    throttle_classes = [TokenRefreshPerTokenThrottle, TokenRefreshPerAddressThrottle]


class VerifyEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_scope = "verification"

    @extend_schema(
        summary="Verify a signup link",
        description=(
            "Send the `registration_id` and `token` from the link in the signup email. On success the account is created (201). "
            "The token works once and expires after SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS; wrong tokens are counted and "
            "the registration locks (423) after too many."
        ),
        request=VerifyRegistrationSerializer,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        serializer = VerifyRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration_id = serializer.validated_data["registration_id"]
        token = serializer.validated_data["token"]
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
                # The link is in a URL (history, referrers, logs): say only that the account exists, never who it belongs to.
                return Response(
                    {
                        "detail": "Account has already been created.",
                        "login_required": True,
                    },
                    status=status.HTTP_200_OK,
                )
            if pending.status == PendingRegistration.STATUS_LOCKED:
                return Response(
                    {"detail": "Too many incorrect attempts. Request a new link."},
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
            if pending.token_expires_at <= now:
                pending.status = PendingRegistration.STATUS_EXPIRED
                pending.save(update_fields=["status", "updated_at"])
                return Response(
                    {
                        "detail": (
                            "The verification link has expired. Request a new link."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not verification_token_matches(token, pending.token_hash):
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
                            "Too many incorrect attempts. Request a new link."
                            if attempts_remaining == 0
                            else "This verification link is not valid. Request a new link."
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
                pending.token_hash = ""
                pending.save(
                    update_fields=[
                        "status",
                        "password_hash",
                        "token_hash",
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
                    first_name, last_name = split_full_name(pending.full_name)
                    user = User(
                        username=pending.username,
                        email=pending.email,
                        first_name=first_name,
                        last_name=last_name,
                        role="user",
                        is_email_verified=True,
                    )
                    user.password = pending.password_hash
                    user.save(force_insert=True)
                    create_personal_organization(user)
            except IntegrityError:
                pending.status = PendingRegistration.STATUS_CANCELLED
                pending.password_hash = ""
                pending.token_hash = ""
                pending.save(
                    update_fields=[
                        "status",
                        "password_hash",
                        "token_hash",
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
            pending.token_hash = ""   # single use: nothing is left to match
            pending.save(
                update_fields=[
                    "status",
                    "consumed_at",
                    "created_user",
                    "password_hash",
                    "token_hash",
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
        summary="Resend a signup link",
        description="Invalidates the current link and emails a new one for the same pending registration.",
        request=ResendVerificationSerializer,
        responses={200: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration_id = serializer.validated_data["registration_id"]
        now = timezone.now()
        generic_detail = (
            "If the registration is eligible, a new verification link has been sent."
        )
        token = generate_verification_token()

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
                            "detail": "Please wait before requesting another link.",
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
                    pending.token_hash = ""
                    pending.save(
                        update_fields=[
                            "status",
                            "password_hash",
                            "token_hash",
                            "updated_at",
                        ]
                    )
                    return Response(
                        {"detail": "Registration can no longer be completed."},
                        status=status.HTTP_409_CONFLICT,
                    )

                pending.token_hash = hash_verification_token(token)   # the old link stops working here
                pending.token_expires_at = now + timedelta(
                    seconds=settings.SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS
                )
                pending.resend_available_at = now + timedelta(
                    seconds=settings.SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS
                )
                pending.failed_attempts = 0
                pending.resend_count += 1
                pending.status = PendingRegistration.STATUS_PENDING
                pending.save()
                send_signup_verification_link(pending.email, pending.public_id, token, pending.full_name)
                record_auth_event(
                    "verification_code_sent",
                    request,
                    identifier=pending.email,
                    details={
                        "registration_id": str(pending.public_id),
                        "reason": "resend",
                        "kind": "link",
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
            and pending.token_expires_at <= now
        ):
            public_status = PendingRegistration.STATUS_EXPIRED
        return Response(
            {
                "status": public_status,
                "email": mask_email(pending.email),
                "expires_in_seconds": max(
                    0,
                    int((pending.token_expires_at - now).total_seconds()),
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

    @extend_schema(responses={200: UserDashboardSerializer})
    def get(self, request):
        return Response(UserDashboardSerializer(build_user_dashboard(request.user)).data)


@extend_schema_view(
    get=extend_schema(summary="List every account (administrators)"),
    post=extend_schema(
        summary="Create an account (administrators)",
        description=(
            "Creates an **already verified** account for someone, with a password the administrator hands over. No email is sent "
            "and the password is never returned or logged. `role` is `user` or `moderator` (never `admin`). A moderator is also "
            "added to an organization so the moderator panel can find it: the one in `organization_id`, otherwise the only active "
            "non-personal organization; with none or several the request is refused (400 `detail`). `username` may be left blank "
            "(derived from the email); `full_name` is optional. Errors are field errors under `email`, `username`, `password`, "
            "`role`. Respond 201 with the same representation the list returns."
        ),
        request=AdminUserCreateSerializer,
        responses={201: AdminUserSerializer, 400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample("A plain user", value={"username": "sara", "email": "sara@example.com", "password": "Ocean!Breeze2026x",
                                                   "role": "user"}, request_only=True),
            OpenApiExample("A moderator", value={"username": "omar", "email": "omar@example.com", "password": "Ocean!Breeze2026x",
                                                  "role": "moderator", "organization_id": 3}, request_only=True),
            OpenApiExample("Created", value={"id": 12, "username": "omar", "email": "omar@example.com", "full_name": "omar",
                                             "role": "moderator", "is_email_verified": True, "is_suspended": False,
                                             "suspension_reason": "", "is_active": True, "date_joined": "2026-10-16T15:30:00Z"},
                           response_only=True, status_codes=["201"]),
            OpenApiExample("Several organizations", value={"detail": "There is more than one organization: send organization_id to choose one."},
                           response_only=True, status_codes=["400"]),
            OpenApiExample("Weak password", value={"password": ["This password is too common."]}, response_only=True, status_codes=["400"]),
        ],
    ),
)
class AdminUserListView(generics.ListCreateAPIView):
    """``GET`` lists everyone (a plain array unless ``page``/``page_size`` is sent); ``POST`` creates an account."""

    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    def get_serializer_class(self):
        return AdminUserCreateSerializer if self.request.method == "POST" else AdminUserSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, description="Part of the username, email or full name."),
            OpenApiParameter("role", OpenApiTypes.STR, description="user, moderator or admin."),
            OpenApiParameter("status", OpenApiTypes.STR, description="active, suspended or inactive."),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = User.objects.annotate(uploads_count=Count("image_uploads", distinct=True)).order_by("-date_joined", "-id")
        params = self.request.query_params
        search = (params.get("q") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search) | Q(email__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search),
            )
        role = params.get("role")
        if role:
            roles = [value for value, _label in User.ROLE_CHOICES]
            if role not in roles:
                raise serializers.ValidationError({"role": [f"Choose one of: {', '.join(roles)}."]})
            queryset = queryset.filter(role=role)
        state = params.get("status")
        if state:
            filters = {"active": Q(is_active=True, is_suspended=False), "suspended": Q(is_suspended=True), "inactive": Q(is_active=False)}
            if state not in filters:
                raise serializers.ValidationError({"status": [f"Choose one of: {', '.join(filters)}."]})
            queryset = queryset.filter(filters[state])
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = AdminUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            user = create_account_as_admin(
                administrator=request.user, username=data["username"], email=data["email"], password=data["password"],
                role=data["role"], organization=data.get("organization"), full_name=data.get("full_name", ""), request=request,
            )
        except IntegrityError:
            # A concurrent request took the username or email between the validation and the insert.
            raise serializers.ValidationError({"email": ["A user with this email or username already exists."]}) from None
        return Response(AdminUserSerializer(user).data, status=status.HTTP_201_CREATED)


class AdminUserUpdateView(generics.RetrieveUpdateAPIView):
    """GET: an account's detail for administrators. PATCH: role, suspension, activity."""

    queryset = User.objects.all()
    serializer_class = AdminUserUpdateSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    http_method_names = ["get", "patch"]

    @extend_schema(responses={200: AdminUserDetailSerializer})
    def get(self, request, *args, **kwargs):
        return Response(AdminUserDetailSerializer(self.get_object()).data)

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
        new_role = serializer.validated_data.get("role")
        if new_role is not None and new_role != target.role:
            from .invitations import assert_can_grant

            assert_can_grant(self.request.user, new_role)
        organization_id = serializer.validated_data.pop("organization_id", None)
        moderator_organization = None
        if new_role == "moderator" and new_role != target.role and not has_moderator_organization(target):
            # A moderator works for an organization: name it (or the only one), with the same messages as when creating a moderator account.
            moderator_organization = resolve_moderator_organization(organization_id)
        if moderator_organization is not None:
            from organizations.models import OrganizationMembership
            from organizations.services import assert_seat_available

            assert_seat_available(moderator_organization, OrganizationMembership.Role.MODERATOR, excluding_user=target)
        previous = {"role": target.role, "is_suspended": target.is_suspended, "is_active": target.is_active}
        user = serializer.save()
        if moderator_organization is not None:
            from organizations.models import OrganizationMembership

            OrganizationMembership.objects.update_or_create(
                organization=moderator_organization, user=user,
                defaults={"role": OrganizationMembership.Role.MODERATOR, "status": OrganizationMembership.Status.ACTIVE,
                          "invited_by": self.request.user},
            )
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


# -- Administrator account invitations ---------------------------------------

class AccountInvitationSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=["user", "moderator", "admin"])
    organization_id = serializers.IntegerField(
        required=False, allow_null=True, write_only=True,
        help_text="Required for a moderator invitation: the organization the moderator will work for. Refused for any other role.",
    )
    organization = serializers.SerializerMethodField(help_text="{id, name} of the organization a moderator invitation is for; null otherwise.")
    status = serializers.CharField(read_only=True)
    invited_by = serializers.CharField(source="invited_by.username", read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(read_only=True)
    accepted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)

    def get_organization(self, invitation) -> dict | None:
        organization = invitation.organization
        return {"id": organization.pk, "name": organization.name} if organization else None


class AccountInvitationListCreateView(generics.ListAPIView):
    """Administrators invite people to create an account with a role.

    An administrator may grant user or moderator; only a superuser may grant
    admin (403 otherwise). 409 when the email already has an account or a
    pending invitation. The invitee receives a link with a one-time token.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdminRole]
    serializer_class = AccountInvitationSerializer

    def get_queryset(self):
        from .models import AccountInvitation

        queryset = AccountInvitation.objects.select_related("invited_by", "organization").order_by("-created_at", "-pk")
        state = self.request.query_params.get("status")
        return queryset.filter(status=state) if state else queryset

    @extend_schema(request=AccountInvitationSerializer,
                   responses={201: AccountInvitationSerializer, 400: OpenApiTypes.OBJECT,
                              403: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request):
        from .invitations import invite

        payload = AccountInvitationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        invitation = invite(actor=request.user, email=payload.validated_data["email"],
                            role=payload.validated_data["role"], organization_id=payload.validated_data.get("organization_id"))
        record_auth_event("account_invited", request, request.user, invitation.email,
                          {"role": invitation.role, "organization_id": invitation.organization_id})
        return Response(AccountInvitationSerializer(invitation).data, status=status.HTTP_201_CREATED)


class AccountInvitationResendView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(request=None, responses={200: AccountInvitationSerializer, 403: OpenApiTypes.OBJECT,
                                            404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request, pk):
        from django.shortcuts import get_object_or_404

        from .invitations import resend
        from .models import AccountInvitation

        invitation = resend(get_object_or_404(AccountInvitation, pk=pk), request.user)
        return Response(AccountInvitationSerializer(invitation).data)


class AccountInvitationCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminRole]

    @extend_schema(request=None, responses={204: None, 403: OpenApiTypes.OBJECT,
                                            404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def delete(self, request, pk):
        from django.shortcuts import get_object_or_404

        from .invitations import cancel
        from .models import AccountInvitation

        cancel(get_object_or_404(AccountInvitation, pk=pk), request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvitationTokenSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)


class InvitationAcceptSerializer(InvitationTokenSerializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class InvitationPreviewSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.CharField()
    expires_at = serializers.DateTimeField()
    organization_name = serializers.SerializerMethodField(help_text="The organization a moderator invitation is for; null otherwise.")

    def get_organization_name(self, invitation) -> str | None:
        return invitation.organization.name if invitation.organization_id else None


class InvitationInspectView(APIView):
    """Public: what an invitation token is for, so the sign-up form can show it."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "invitation"

    @extend_schema(request=InvitationTokenSerializer, responses={200: InvitationPreviewSerializer,
                                                                 404: OpenApiTypes.OBJECT}, auth=[])
    def post(self, request):
        from .invitations import pending_invitation

        payload = InvitationTokenSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        invitation = pending_invitation(payload.validated_data["token"])
        if invitation is None:
            return Response({"detail": "This invitation is invalid, used or expired."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(InvitationPreviewSerializer(invitation).data)


class InvitationAcceptView(APIView):
    """Public: create the invited account and sign in.

    The email is the invitation's and is verified by the token; the role is
    the one the administrator granted. Answers like login (tokens and user).
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "invitation"

    @extend_schema(request=InvitationAcceptSerializer,
                   responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT},
                   auth=[])
    def post(self, request):
        from .invitations import accept

        payload = InvitationAcceptSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user, invitation = accept(**payload.validated_data)
        record_auth_event("invitation_accepted", request, user, user.email, {"role": user.role})
        return Response(issue_login_tokens(user, request), status=status.HTTP_201_CREATED)
