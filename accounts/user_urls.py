from django.urls import path

from .views import (
    AdminUserListView,
    AdminUserUpdateView,
    AuthEventListView,
    ChangePasswordView,
    DeleteAccountView,
    LogoutAllView,
    LogoutView,
    ProfileView,
    UserDashboardView,
)

urlpatterns = [
    path("profile/", ProfileView.as_view(), name="profile"),
    path("dashboard/", UserDashboardView.as_view(), name="user-dashboard"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("logout-all/", LogoutAllView.as_view(), name="logout-all"),
    path("delete-account/", DeleteAccountView.as_view(), name="delete-account"),
    path("activity/", AuthEventListView.as_view(), name="auth-activity"),
    path("admin/users/", AdminUserListView.as_view(), name="admin-user-list"),
    path("admin/users/<int:pk>/", AdminUserUpdateView.as_view(), name="admin-user-update"),
]
