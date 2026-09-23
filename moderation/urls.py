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
        "<int:organization_id>/moderator/dashboard/",
        views.ModeratorDashboardView.as_view(), name="dashboard",
    ),
]
