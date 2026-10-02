from django.urls import path

from . import views

app_name = "moderation"

urlpatterns = [
    path(
        "<int:organization_id>/moderator/reviews/",
        views.ModeratorReviewListView.as_view(), name="review-list",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/",
        views.ModeratorReviewDetailView.as_view(), name="review-detail",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/decision/",
        views.ModeratorReviewDecisionView.as_view(), name="review-decision",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/escalate/",
        views.ModeratorReviewEscalateView.as_view(), name="review-escalate",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/assign/",
        views.ModeratorReviewAssignView.as_view(), name="review-assign",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/claim/",
        views.ModeratorReviewClaimView.as_view(), name="review-claim",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/release/",
        views.ModeratorReviewReleaseView.as_view(), name="review-release",
    ),
    path(
        "<int:organization_id>/moderator/reviews/bulk-decision/",
        views.ModeratorBulkDecisionView.as_view(), name="review-bulk-decision",
    ),
    path(
        "<int:organization_id>/moderator/dashboard/",
        views.ModeratorDashboardView.as_view(), name="dashboard",
    ),
    path(
        "<int:organization_id>/moderator/metrics/",
        views.ModeratorPerformanceView.as_view(), name="performance-metrics",
    ),
    path(
        "<int:organization_id>/moderator/batches/",
        views.ModeratorBatchListView.as_view(), name="batch-list",
    ),
    path(
        "<int:organization_id>/moderator/batches/<int:batch_id>/retry/",
        views.ModeratorBatchRetryView.as_view(), name="batch-retry",
    ),
    path(
        "<int:organization_id>/moderator/batches/<int:batch_id>/cancel/",
        views.ModeratorBatchCancelView.as_view(), name="batch-cancel",
    ),
    path(
        "<int:organization_id>/moderator/reports/",
        views.ModeratorReportListCreateView.as_view(), name="report-list-create",
    ),
    path(
        "<int:organization_id>/moderator/reports/<int:report_id>/",
        views.ModeratorReportDetailView.as_view(), name="report-detail",
    ),
    path(
        "<int:organization_id>/moderator/reports/<int:report_id>/download/",
        views.ModeratorReportFileView.as_view(), name="report-download",
    ),
    path(
        "<int:organization_id>/moderator/reviews/<int:review_id>/merge-identity-groups/",
        views.ModeratorDuplicateGroupMergeView.as_view(), name="review-merge-identity-groups",
    ),
]
