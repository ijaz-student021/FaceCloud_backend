from django.urls import path

from activity.views import UserActivityTimelineView

from .views import (
    AccountInvitationCancelView,
    AccountInvitationListCreateView,
    AccountInvitationResendView,
    AdminUserListView,
    AdminUserUpdateView,
    AvatarView,
    ChangePasswordView,
    DeleteAccountView,
    LogoutAllView,
    LogoutView,
    ProfileView,
    UserDashboardView,
    UserSessionListView,
    UserSessionRevokeView,
)

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("avatar/", AvatarView.as_view(), name="avatar"),
    path("dashboard/", UserDashboardView.as_view(), name="user-dashboard"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("sessions/", UserSessionListView.as_view(), name="user-sessions"),
    path("sessions/<uuid:session_id>/revoke/", UserSessionRevokeView.as_view(), name="user-session-revoke"),
    path("delete-account/", DeleteAccountView.as_view(), name="delete-account"),
    path("activity/", UserActivityTimelineView.as_view(), name="user-activity"),
    path("admin/users/", AdminUserListView.as_view(), name="admin-user-list"),
    path("admin/users/<int:pk>/", AdminUserUpdateView.as_view(), name="admin-user-update"),
    path("admin/invitations/", AccountInvitationListCreateView.as_view(), name="admin-invitations"),
    path("admin/invitations/<int:pk>/", AccountInvitationCancelView.as_view(), name="admin-invitation-cancel"),
    path("admin/invitations/<int:pk>/resend/", AccountInvitationResendView.as_view(), name="admin-invitation-resend"),
]
