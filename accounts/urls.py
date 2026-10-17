from django.urls import path

from .views import (
    InvitationAcceptView,
    InvitationInspectView,
    FaceCloudTokenObtainPairView,
    FaceCloudTokenRefreshView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegistrationStatusView,
    RegisterView,
    ResendVerificationView,
    TwoFactorConfirmView,
    TwoFactorDisableView,
    TwoFactorLoginVerifyView,
    TwoFactorRecoveryCodesView,
    TwoFactorSetupView,
    TwoFactorStatusView,
    VerifyEmailChangeView,
    VerifyEmailView,
)

urlpatterns = [
    path("invitations/inspect/", InvitationInspectView.as_view(), name="invitation-inspect"),
    path("invitations/accept/", InvitationAcceptView.as_view(), name="invitation-accept"),
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", FaceCloudTokenObtainPairView.as_view(), name="login"),
    path("refresh/", FaceCloudTokenRefreshView.as_view(), name="refresh"),
    path("2fa/setup/", TwoFactorSetupView.as_view(), name="two-factor-setup"),
    path("2fa/confirm/", TwoFactorConfirmView.as_view(), name="two-factor-confirm"),
    path("2fa/status/", TwoFactorStatusView.as_view(), name="two-factor-status"),
    path("2fa/disable/", TwoFactorDisableView.as_view(), name="two-factor-disable"),
    path("2fa/recovery-codes/", TwoFactorRecoveryCodesView.as_view(), name="two-factor-recovery-codes"),
    path("2fa/verify/", TwoFactorLoginVerifyView.as_view(), name="two-factor-verify"),
    path("verify-email/", VerifyEmailView.as_view(), name="verify-email"),
    path("verify-email/resend/", ResendVerificationView.as_view(), name="resend-verification"),
    path(
        "verify-email/change/<uidb64>/<token>/",
        VerifyEmailChangeView.as_view(),
        name="verify-email-change",
    ),
    path(
        "registrations/<uuid:registration_id>/status/",
        RegistrationStatusView.as_view(),
        name="registration-status",
    ),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password-reset-confirm"),
]
