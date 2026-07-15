from django.urls import path

from .views import (
    FaceCloudTokenObtainPairView,
    FaceCloudTokenRefreshView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegisterView,
    ResendVerificationView,
    VerifyEmailView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", FaceCloudTokenObtainPairView.as_view(), name="login"),
    path("refresh/", FaceCloudTokenRefreshView.as_view(), name="refresh"),
    path("verify-email/resend/", ResendVerificationView.as_view(), name="resend-verification"),
    path("verify-email/<uidb64>/<token>/", VerifyEmailView.as_view(), name="verify-email"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
]
