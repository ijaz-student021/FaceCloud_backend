from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from .sessions import token_session_is_active


class FaceCloudJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if not user.is_email_verified:
            raise AuthenticationFailed("Email verification is required.", code="email_unverified")
        if user.is_suspended:
            raise AuthenticationFailed("This account is suspended.", code="account_suspended")
        if validated_token.get("token_version", 0) != user.token_version:
            raise AuthenticationFailed("This token has been revoked.", code="token_revoked")
        if not token_session_is_active(validated_token, user):
            raise AuthenticationFailed("This session has been signed out.", code="session_revoked")
        return user
