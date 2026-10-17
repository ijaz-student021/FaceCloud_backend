import re

from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework.exceptions import Throttled
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from . import lockout
from .services import (dashboard_for_user, derive_username, has_active_two_factor,
                       issue_login_tokens, make_two_factor_login_challenge,
                       record_auth_event, resolve_moderator_organization, split_full_name)
from .sessions import record_rotation, token_session_is_active

User = get_user_model()


def normalize_email(value):
    return User.objects.normalize_email(value).strip().lower()


def validate_unique_email(value, instance=None):
    email = normalize_email(value)
    queryset = User.objects.annotate(email_lower=Lower("email")).filter(email_lower=email)
    if instance:
        queryset = queryset.exclude(pk=instance.pk)
    if queryset.exists():
        raise serializers.ValidationError("A user with this email already exists.")
    return email


def clean_full_name(value):
    """Trim and collapse inner whitespace; a name must have a visible character."""
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise serializers.ValidationError("Enter your full name.")
    return cleaned


class RegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    # Optional: when absent (or blank) the view derives it from the email's local part.
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField(required=True, allow_blank=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(
        required=False,
        write_only=True,
        trim_whitespace=False,
    )

    def validate_full_name(self, value):
        return clean_full_name(value)

    def validate_username(self, value):
        value = User.normalize_username(value).strip()
        if value and User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_email(self, value):
        return validate_unique_email(value)

    def validate(self, attrs):
        password_confirm = attrs.pop("password_confirm", None)
        if password_confirm is not None and password_confirm != attrs["password"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        if not attrs.get("username"):
            attrs.pop("username", None)
        first_name, last_name = split_full_name(attrs["full_name"])
        candidate = User(
            username=attrs.get("username") or attrs["email"].partition("@")[0],
            email=attrs.get("email"), first_name=first_name, last_name=last_name,
        )
        password_validation.validate_password(attrs["password"], user=candidate)
        return attrs


class VerifyRegistrationSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()
    token = serializers.CharField(
        max_length=256,
        error_messages={"blank": "The verification link is incomplete.", "required": "The verification link is incomplete."},
    )


class ResendVerificationSerializer(serializers.Serializer):
    registration_id = serializers.UUIDField()


class FaceCloudTokenObtainPairSerializer(serializers.Serializer):
    identifier = serializers.CharField(required=False, write_only=True)
    username = serializers.CharField(required=False, write_only=True)
    email = serializers.EmailField(required=False, write_only=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    @staticmethod
    def get_dashboard(user):
        return dashboard_for_user(user)

    def validate(self, attrs):
        request = self.context.get("request")
        supplied = [attrs.get("identifier"), attrs.get("username"), attrs.get("email")]
        identifiers = [value.strip() for value in supplied if value and value.strip()]
        if len(identifiers) != 1:
            raise serializers.ValidationError(
                {"detail": "Provide exactly one login identifier: identifier, username, or email."}
            )
        identifier = identifiers[0]
        user = User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()
        key = lockout.attempt_key(user, identifier)
        remaining = lockout.seconds_locked(key)
        if remaining:
            # Refused before the password is even checked, for real and unknown
            # accounts alike.
            record_auth_event("login_failed", request, user, identifier, {"reason": "locked"})
            raise Throttled(
                wait=remaining,
                detail=f"Too many failed sign-in attempts. Try again in {remaining} seconds.",
            )
        if not user:
            # Hash anyway so an unknown account takes as long as a wrong password.
            User().set_password(attrs["password"])
        if not user or not user.check_password(attrs["password"]):
            record_auth_event("login_failed", request, user, identifier, {"reason": "invalid_credentials"})
            delay = lockout.record_failure(key)
            if delay:
                record_auth_event("login_locked", request, user, identifier, {"seconds": delay})
            raise serializers.ValidationError({"detail": "Invalid username/email or password."})
        lockout.clear(key)
        if not user.is_active:
            record_auth_event("login_failed", request, user, identifier, {"reason": "account_inactive"})
            raise serializers.ValidationError({"detail": "This account is inactive."})
        if user and not user.is_email_verified:
            record_auth_event("login_failed", request, user, identifier, {"reason": "email_unverified"})
            raise serializers.ValidationError({"detail": "Email verification is required."})
        if user and user.is_suspended:
            record_auth_event("login_failed", request, user, identifier, {"reason": "account_suspended"})
            raise serializers.ValidationError({"detail": "This account is suspended."})
        if has_active_two_factor(user):
            record_auth_event("login", request, user, identifier, {"stage": "password_verified_awaiting_two_factor"})
            return {
                "two_factor_required": True,
                "login_token": make_two_factor_login_challenge(user),
            }
        record_auth_event("login", request, user, identifier)
        return issue_login_tokens(user, request)


class FaceCloudTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        token = RefreshToken(attrs["refresh"])
        user = User.objects.filter(pk=token.get("user_id")).first()
        if not user or not user.is_active:
            raise serializers.ValidationError({"detail": "Account is inactive."})
        if not user.is_email_verified:
            raise serializers.ValidationError({"detail": "Email verification is required."})
        if user.is_suspended:
            raise serializers.ValidationError({"detail": "This account is suspended."})
        if token.get("token_version", 0) != user.token_version:
            raise serializers.ValidationError({"detail": "This token has been revoked."})
        if not token_session_is_active(token, user):
            raise serializers.ValidationError({"detail": "This session has been signed out."})
        data = super().validate(attrs)
        if "refresh" in data:
            record_rotation(RefreshToken(data["refresh"]))
        return data


PROFILE_TEXT_FIELDS = ("display_name", "organization_name", "job_title", "country")
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9 ()\-]{0,30}$")


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True, help_text="First and last name, or the username when no name is set.")
    avatar_url = serializers.SerializerMethodField(
        help_text="Authenticated URL of the user's own avatar (changes when it is replaced), or null.",
    )

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "full_name", "role", "is_email_verified", "date_joined",
            "display_name", "organization_name", "job_title", "phone", "country", "avatar_url",
        ]
        read_only_fields = ["id", "full_name", "role", "is_email_verified", "date_joined", "avatar_url"]
        extra_kwargs = {
            "organization_name": {"help_text": "Free text about the user. It grants no organization access."},
        }

    def get_avatar_url(self, user) -> str | None:
        if not user.avatar:
            return None
        version = int(user.avatar_updated_at.timestamp()) if user.avatar_updated_at else 0
        return f"/api/user/avatar/?v={version}"

    def _clean_text(self, value, field):
        value = " ".join(str(value).split())
        if any(ord(character) < 32 for character in value):
            raise serializers.ValidationError(f"{field} contains control characters.")
        return value

    def validate_display_name(self, value):
        return self._clean_text(value, "Name")

    def validate_organization_name(self, value):
        return self._clean_text(value, "Organization")

    def validate_job_title(self, value):
        return self._clean_text(value, "Job title")

    def validate_country(self, value):
        return self._clean_text(value, "Country")

    def validate_phone(self, value):
        value = value.strip()
        if value and not PHONE_PATTERN.match(value):
            raise serializers.ValidationError("Use digits, spaces, brackets, hyphens and an optional leading +.")
        return value

    def validate_username(self, value):
        value = User.normalize_username(value).strip()
        if User.objects.filter(username__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_email(self, value):
        return validate_unique_email(value, self.instance)


class ProfileUpdateSerializer(UserSerializer):
    """Profile edits. Changing the email re-routes password recovery to a new
    mailbox, so it needs proof of the current password: a stolen access token
    alone must not be enough to take over an account."""

    current_password = serializers.CharField(
        write_only=True, required=False, trim_whitespace=False
    )
    full_name = serializers.CharField(required=False, max_length=150, help_text="Stored as first name (first word) and last name (the rest).")

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["current_password"]
        read_only_fields = [name for name in UserSerializer.Meta.read_only_fields if name != "full_name"]

    def validate_full_name(self, value):
        return clean_full_name(value)

    def update(self, instance, validated_data):
        full_name = validated_data.pop("full_name", None)
        if full_name is not None:
            instance.first_name, instance.last_name = split_full_name(full_name)
        return super().update(instance, validated_data)

    def validate(self, attrs):
        password = attrs.pop("current_password", None)
        new_email = attrs.get("email")
        if new_email and new_email != self.instance.email:
            if not password or not self.instance.check_password(password):
                raise serializers.ValidationError(
                    {"current_password": "Current password is required to change the email."}
                )
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uidb64 = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        try:
            uid = force_str(urlsafe_base64_decode(attrs["uidb64"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            raise serializers.ValidationError({"detail": "Invalid or expired reset link."})
        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError({"detail": "Invalid or expired reset link."})
        password_validation.validate_password(attrs["new_password"], user)
        attrs["user"] = user
        return attrs


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class DeleteAccountSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Password is incorrect.")
        return value


class AdminUserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "full_name", "role", "is_email_verified", "is_suspended",
            "suspension_reason", "is_active", "date_joined",
        ]
        read_only_fields = ["id", "username", "email", "full_name", "is_email_verified", "date_joined"]

    def validate(self, attrs):
        if attrs.get("is_suspended") is False:
            attrs["suspension_reason"] = ""
        return attrs


class AdminUserDetailSerializer(AdminUserSerializer):
    """Read-only detail for administrators; PATCH still uses AdminUserSerializer."""

    two_factor_enabled = serializers.SerializerMethodField()
    active_sessions = serializers.SerializerMethodField()
    organizations = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta(AdminUserSerializer.Meta):
        fields = AdminUserSerializer.Meta.fields + [
            "last_login", "display_name", "organization_name", "job_title", "country",
            "two_factor_enabled", "active_sessions", "organizations", "usage",
        ]
        read_only_fields = fields

    def get_two_factor_enabled(self, user) -> bool:
        return has_active_two_factor(user)

    def get_active_sessions(self, user) -> int:
        from .sessions import active_sessions

        return len(active_sessions(user))

    def get_organizations(self, user) -> list[dict]:
        return [
            {"id": membership.organization_id, "name": membership.organization.name,
             "role": membership.role, "status": membership.status}
            for membership in user.organization_memberships.select_related("organization").order_by("organization__name")
        ]

    def get_usage(self, user) -> dict:
        return {
            "upload_batches": user.upload_batches.count(),
            "images": user.image_uploads.count(),
            "enhancements": user.image_enhancement_results.count(),
        }


class AdminUserCreateSerializer(serializers.Serializer):
    """What an administrator sends to create an account: the same checks as signup, without the verification email."""

    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    role = serializers.ChoiceField(choices=["user", "moderator"], default="user",
                                   help_text="`user` or `moderator`. An administrator account cannot be created here.")
    organization_id = serializers.IntegerField(required=False, allow_null=True,
                                               help_text="Moderators only. Needed when there is more than one organization.")
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=150)

    def validate_username(self, value):
        value = User.normalize_username(value).strip()
        if value and User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_email(self, value):
        return validate_unique_email(value)

    def validate_full_name(self, value):
        return " ".join((value or "").split())

    def validate(self, attrs):
        username = attrs.get("username") or derive_username(attrs["email"])
        first_name, last_name = split_full_name(attrs.get("full_name", ""))
        candidate = User(username=username, email=attrs["email"], first_name=first_name, last_name=last_name)
        try:
            password_validation.validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": list(error.messages)}) from error
        attrs["username"] = username
        organization_id = attrs.get("organization_id")
        if attrs["role"] == "moderator":
            attrs["organization"] = resolve_moderator_organization(organization_id)
        elif organization_id is not None:
            raise serializers.ValidationError({"organization_id": ["Only a moderator is placed in an organization."]})
        return attrs


# -- Two-factor authentication -----------------------------------------------

class TwoFactorSetupResponseSerializer(serializers.Serializer):
    secret = serializers.CharField(help_text="For manual entry if the app cannot scan a QR code.")
    otpauth_uri = serializers.CharField(help_text="Render this as a QR code for an authenticator app to scan.")


class TwoFactorCodeSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="A 6-digit code from the authenticator app, or an unused recovery code.")


class TwoFactorCodesResponseSerializer(serializers.Serializer):
    recovery_codes = serializers.ListField(
        child=serializers.CharField(),
        help_text="Shown once. Store these somewhere safe; they are not retrievable again.",
    )


class TwoFactorStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class TwoFactorLoginVerifySerializer(serializers.Serializer):
    login_token = serializers.CharField()
    code = serializers.CharField()


class TwoFactorRequiredResponseSerializer(serializers.Serializer):
    two_factor_required = serializers.BooleanField()
    login_token = serializers.CharField()
