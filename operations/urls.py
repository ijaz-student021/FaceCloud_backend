from django.urls import include, path

from .admin_images import AdminImageDetailView, AdminImageListView, AdminImagePreviewView

from .views import (
    AdminAnalyticsView,
    AdminAuditView,
    AdminBatchRetryView,
    AdminModerationOverviewView,
    FailedBatchListView,
    FailedJobListView,
    QueueStatusView,
    SystemHealthView,
)


app_name = "operations"

urlpatterns = [
    path("health/", SystemHealthView.as_view(), name="system-health"),
    path("queue/", QueueStatusView.as_view(), name="queue-status"),
    path("analytics/", AdminAnalyticsView.as_view(), name="admin-analytics"),
    path("batches/failed/", FailedBatchListView.as_view(), name="failed-batches"),
    path("batches/<int:batch_id>/retry/", AdminBatchRetryView.as_view(), name="admin-batch-retry"),
    path("jobs/failed/", FailedJobListView.as_view(), name="failed-jobs"),
    path("audit/", AdminAuditView.as_view(), name="admin-audit"),
    path("moderation/", AdminModerationOverviewView.as_view(), name="admin-moderation-overview"),
    path("security/", include("security.urls")),
    path("images/", AdminImageListView.as_view(), name="admin-images"),
    path("images/<int:pk>/", AdminImageDetailView.as_view(), name="admin-image-detail"),
    path("images/<int:pk>/preview/", AdminImagePreviewView.as_view(), name="admin-image-preview"),
]

