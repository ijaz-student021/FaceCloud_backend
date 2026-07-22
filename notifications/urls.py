from django.urls import path

from .views import (
    NotificationDetailView,
    NotificationListView,
    NotificationMarkAllReadView,
    NotificationMarkReadView,
    NotificationPreferenceView,
    NotificationUnreadCountView,
)


urlpatterns = [
    path("", NotificationListView.as_view(), name="notification-list"),
    path("preferences/", NotificationPreferenceView.as_view(), name="notification-preferences"),
    path("unread-count/", NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("mark-all-read/", NotificationMarkAllReadView.as_view(), name="notification-mark-all-read"),
    path("<int:pk>/", NotificationDetailView.as_view(), name="notification-detail"),
    path("<int:pk>/read/", NotificationMarkReadView.as_view(), name="notification-mark-read"),
]

