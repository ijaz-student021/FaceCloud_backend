from django.urls import path

from .views import (
    DuplicateAnalysisListView,
    DuplicateImageAnalysisView,
    DuplicateImageMatchListView,
    DuplicateMatchDetailView,
    DuplicateMatchListView,
    DuplicateMatchReviewView,
)

app_name = "duplicates"

urlpatterns = [
    path("images/<int:image_id>/", DuplicateImageAnalysisView.as_view(), name="image-analysis"),
    path(
        "images/<int:image_id>/matches/",
        DuplicateImageMatchListView.as_view(),
        name="image-match-list",
    ),
    path("results/", DuplicateAnalysisListView.as_view(), name="analysis-list"),
    path("matches/", DuplicateMatchListView.as_view(), name="match-list"),
    path("matches/<int:pk>/", DuplicateMatchDetailView.as_view(), name="match-detail"),
    path(
        "matches/<int:pk>/review/",
        DuplicateMatchReviewView.as_view(),
        name="match-review",
    ),
]
