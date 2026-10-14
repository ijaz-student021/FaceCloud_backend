from django.urls import path

from activity.views import UserActivityTimelineView

from .views import (
    AdminUserListView,
    AdminUserUpdateView,
    AvatarView,
    ChangePasswordView,
    DeleteAccountView,
    LogoutAllView,
    LogoutView,
    ProfileView,
    UserDashboardView,
)

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("avatar/", AvatarView.as_view(), name="avatar"),
    path("dashboard/", UserDashboardView.as_view(), name="user-dashboard"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("delete-account/", DeleteAccountView.as_view(), name="delete-account"),
    path("activity/", UserActivityTimelineView.as_view(), name="user-activity"),
    path("admin/users/", AdminUserListView.as_view(), name="admin-user-list"),
    path("admin/users/<int:pk>/", AdminUserUpdateView.as_view(), name="admin-user-update"),
]
