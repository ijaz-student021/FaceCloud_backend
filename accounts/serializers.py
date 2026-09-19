from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.tokens import default_token_generator
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers
from rest_framework.exceptions import Throttled
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from . import lockout
from .services import record_auth_event

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


class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(required=True, allow_blank=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["id", "username", "email", "password"]
        read_only_fields = ["id"]

    def validate_username(self, value):
        value = User.normalize_username(value).strip()
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_email(self, value):
        return validate_unique_email(value)

    def validate(self, attrs):
        candidate = User(username=attrs.get("username"), email=attrs.get("email"))
        password_validation.validate_password(attrs["password"], user=candidate)
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class FaceCloudTokenObtainPairSerializer(serializers.Serializer):
    identifier = serializers.CharField(required=False, write_only=True)
    username = serializers.CharField(required=False, write_only=True)
    email = serializers.EmailField(required=False, write_only=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    @staticmethod
    def get_dashboard(user):
        if user.is_superuser or user.role == "admin":
            return "admin"
        if user.role == "moderator":
            return "moderator"
        return "user"

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
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        dashboard = self.get_dashboard(user)
        record_auth_event("login", request, user, identifier)
        return {
            "refresh": str(token),
            "access": str(token.access_token),
            "user": UserSerializer(user).data,
            "role": user.role,
            "dashboard": dashboard,
        }


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
        return super().validate(attrs)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "role", "is_email_verified", "date_joined"]
        read_only_fields = ["id", "role", "is_email_verified", "date_joined"]

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

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["current_password"]

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
    class Meta:
        model = User
        fields = [
            "id", "username", "email", "role", "is_email_verified", "is_suspended",
            "suspension_reason", "is_active", "date_joined",
        ]
        read_only_fields = ["id", "username", "email", "is_email_verified", "date_joined"]

    def validate(self, attrs):
        if attrs.get("is_suspended") is False:
            attrs["suspension_reason"] = ""
        return attrs
