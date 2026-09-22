from django.urls import path

from .views import (
    AdminAnalyticsView,
    AdminAuditView,
    AdminBatchRetryView,
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
]

