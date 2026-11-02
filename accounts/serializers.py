import re

from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import update_last_login
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework.exceptions import Throttled
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from . import lockout
from .services import (ModeratorWithoutOrganization, dashboard_for_user, derive_username, has_moderator_organization,
                       issue_login_tokens, record_auth_event,
                       resolve_moderator_organization, split_full_name)
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


# Public endpoints take text from anyone, so every identifier, email and password has a ceiling: 254 characters is the longest an email address can be, and no real password needs more
# than 1024. Without it a 300,000-character identifier reached a LIKE query (HTTP 500 on SQLite) and a huge password reached the password hasher.
MAX_IDENTIFIER_LENGTH = 254
MAX_PASSWORD_LENGTH = 1024
TOO_LONG_IDENTIFIER = {"max_length": f"This is too long: use at most {MAX_IDENTIFIER_LENGTH} characters."}
TOO_LONG_PASSWORD = {"max_length": f"The password is too long: use at most {MAX_PASSWORD_LENGTH} characters."}


class RegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    # Optional: when absent (or blank) the view derives it from the email's local part.
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField(required=True, allow_blank=False, max_length=MAX_IDENTIFIER_LENGTH, error_messages=TOO_LONG_IDENTIFIER)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=MAX_PASSWORD_LENGTH, error_messages=TOO_LONG_PASSWORD)
    password_confirm = serializers.CharField(
        required=False,
        write_only=True,
        trim_whitespace=False,
        max_length=MAX_PASSWORD_LENGTH,
        error_messages=TOO_LONG_PASSWORD,
    )
    invitation_token = serializers.CharField(
        required=False, allow_blank=True, write_only=True, max_length=256,
        help_text="The token of an organization invitation sent to this email address (from the link in the invitation email). "
                  "With it the address counts as verified: the account is created, the invitation accepted and the login tokens returned (201).",
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
    identifier = serializers.CharField(required=False, write_only=True, max_length=MAX_IDENTIFIER_LENGTH, error_messages=TOO_LONG_IDENTIFIER)
    username = serializers.CharField(required=False, write_only=True, max_length=MAX_IDENTIFIER_LENGTH, error_messages=TOO_LONG_IDENTIFIER)
    email = serializers.EmailField(required=False, write_only=True, max_length=MAX_IDENTIFIER_LENGTH, error_messages=TOO_LONG_IDENTIFIER)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=MAX_PASSWORD_LENGTH, error_messages=TOO_LONG_PASSWORD)

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
        if user.role == "moderator" and not has_moderator_organization(user):
            record_auth_event("login_failed", request, user, identifier, {"reason": "no_organization"})
            raise ModeratorWithoutOrganization(user)
        record_auth_event("login", request, user, identifier)
        if api_settings.UPDATE_LAST_LOGIN:
            # SimpleJWT does this in its own serializer, which this one replaces; without it `last_login` stayed null for everyone.
            update_last_login(None, user)
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
        if user.role == "moderator" and not has_moderator_organization(user):
            raise ModeratorWithoutOrganization(user)
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

    def validate(self, attrs):
        # Compared with what was typed as the current password, which is only reached once that has been checked as correct: asking "is this the
        # current password?" of the new-password field would let a stolen session test guesses without knowing it.
        if attrs["new_password"] == attrs["current_password"]:
            raise serializers.ValidationError({"new_password": ["Choose a password you have not just used."]})
        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=MAX_IDENTIFIER_LENGTH, error_messages=TOO_LONG_IDENTIFIER)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uidb64 = serializers.CharField(max_length=64)
    token = serializers.CharField(max_length=256)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=MAX_PASSWORD_LENGTH, error_messages=TOO_LONG_PASSWORD)

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
    uploads_count = serializers.SerializerMethodField(help_text="Photos this account has uploaded (the list computes it for every row at once).")

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "full_name", "role", "is_email_verified", "is_suspended",
            "suspension_reason", "is_active", "date_joined", "last_login", "uploads_count",
        ]
        read_only_fields = ["id", "username", "email", "full_name", "is_email_verified", "date_joined", "last_login", "uploads_count"]

    def get_uploads_count(self, user) -> int:
        annotated = getattr(user, "uploads_count", None)
        return annotated if annotated is not None else user.image_uploads.count()

    def validate(self, attrs):
        if attrs.get("is_suspended") is False:
            attrs["suspension_reason"] = ""
        return attrs


class AdminUserUpdateSerializer(AdminUserSerializer):
    """PATCH /api/user/admin/users/{id}/: the account's fields plus, when making someone a moderator, the organization they will work for."""

    organization_id = serializers.IntegerField(
        write_only=True, required=False, allow_null=True,
        help_text="With role=moderator: the organization the moderator will work for (needed when there is more than one, and when the account "
                  "has no moderator membership yet). Ignored otherwise.",
    )

    class Meta(AdminUserSerializer.Meta):
        fields = AdminUserSerializer.Meta.fields + ["organization_id"]


class AdminUserDetailSerializer(AdminUserSerializer):
    """Read-only detail for administrators; PATCH still uses AdminUserSerializer."""

    active_sessions = serializers.SerializerMethodField()
    organizations = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta(AdminUserSerializer.Meta):
        fields = AdminUserSerializer.Meta.fields + [
            "last_login", "display_name", "organization_name", "job_title", "country",
            "active_sessions", "organizations", "usage",
        ]
        read_only_fields = fields

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


# -- User dashboard (GET /api/user/dashboard/) --------------------------------
# The response is rendered through these serializers, and a test compares them
# with accounts.dashboard.build_user_dashboard key by key.

class DashboardSummarySerializer(serializers.Serializer):
    total_images = serializers.IntegerField(help_text="Every image the user uploaded, accepted or rejected.")
    upload_batches = serializers.IntegerField()
    enhanced_images = serializers.IntegerField(help_text="Distinct images with at least one completed enhancement.")
    accepted_images = serializers.IntegerField(help_text="Completed quality decisions of 'accepted'.")
    rejected_images = serializers.IntegerField(
        help_text="Uploads rejected at validation plus uploaded images whose quality decision is 'rejected'.",
    )
    needs_review_images = serializers.IntegerField()
    average_quality_score = serializers.FloatField(help_text="Mean overall quality score (0-100); 0 when none.")
    most_common_rejection_reason = serializers.CharField(allow_null=True)
    processing_images = serializers.IntegerField(help_text="Images with enhancement or quality analysis in flight.")
    failed_images = serializers.IntegerField()
    identity_groups = serializers.IntegerField()
    identity_grouped_images = serializers.IntegerField()
    identity_review_items = serializers.IntegerField()
    identity_groups_available = serializers.BooleanField(help_text="False when identity grouping is switched off.")
    storage_bytes = serializers.IntegerField(help_text="Originals, standardized copies and enhanced outputs, including those of photographs in the trash (they are still stored).")
    trash_images = serializers.IntegerField(help_text="Photographs in the trash. They are not counted in any other figure here.")
    trash_bytes = serializers.IntegerField(help_text="Size of the originals in the trash.")


class DashboardDaySerializer(serializers.Serializer):
    date = serializers.DateField(help_text="Server local date.")
    uploads = serializers.IntegerField()
    accepted = serializers.IntegerField(help_text="Images given an 'accepted' quality decision that day.")
    rejected = serializers.IntegerField(help_text="Images given a 'rejected' quality decision that day.")


class DashboardRecentUploadSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    filename = serializers.CharField()
    status = serializers.CharField()
    batch_id = serializers.IntegerField()
    uploaded_at = serializers.DateTimeField()


class DashboardActivitySerializer(serializers.Serializer):
    type = serializers.CharField(help_text="e.g. image_uploaded, enhancement_completed, quality_rejected.")
    message = serializers.CharField()
    image_id = serializers.IntegerField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    enhancement_id = serializers.IntegerField(required=False)
    validation_id = serializers.IntegerField(required=False)
    quality_id = serializers.IntegerField(required=False)


class UserDashboardSerializer(serializers.Serializer):
    summary = DashboardSummarySerializer()
    uploads_last_7_days = DashboardDaySerializer(many=True, help_text="Seven rows, oldest first, zero-filled.")
    recent_uploads = DashboardRecentUploadSerializer(many=True, help_text="Up to five, newest first.")
    recent_activity = DashboardActivitySerializer(many=True, help_text="Newest first.")
