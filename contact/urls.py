from django.urls import path

from .views import (
    AdminContactMessageDetailView, AdminContactMessageListView, AdminEarlyAccessDetailView, AdminEarlyAccessListView, AdminInboxSummaryView, AdminMarkAllReadView,
    ContactMessageView, EarlyAccessView,
)

app_name = "contact"

urlpatterns = [
    path("", ContactMessageView.as_view(), name="submit"),
    path("early-access/", EarlyAccessView.as_view(), name="early-access"),
    path("admin/summary/", AdminInboxSummaryView.as_view(), name="admin-summary"),
    path("admin/messages/", AdminContactMessageListView.as_view(), name="admin-messages"),
    path("admin/messages/<int:pk>/", AdminContactMessageDetailView.as_view(), name="admin-message"),
    path("admin/early-access/", AdminEarlyAccessListView.as_view(), name="admin-early-access"),
    path("admin/early-access/<int:pk>/", AdminEarlyAccessDetailView.as_view(), name="admin-early-access-row"),
    path("admin/mark-all-read/", AdminMarkAllReadView.as_view(), name="admin-mark-all-read"),
]
