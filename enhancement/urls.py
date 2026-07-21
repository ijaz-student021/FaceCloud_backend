from django.urls import path

from .views import (
    BatchEnhanceView,
    EnhancementComparisonView,
    EnhancementDownloadView,
    EnhancementJobDetailView,
    EnhancementJobListView,
    EnhancementResultDetailView,
    EnhancementResultListView,
    ImageEnhanceView,
)

app_name = "enhancement"

urlpatterns = [
    path("images/<int:image_id>/", ImageEnhanceView.as_view(), name="image-enhance"),
    path("batches/<int:batch_id>/", BatchEnhanceView.as_view(), name="batch-enhance"),
    path("jobs/", EnhancementJobListView.as_view(), name="job-list"),
    path("jobs/<int:pk>/", EnhancementJobDetailView.as_view(), name="job-detail"),
    path("results/", EnhancementResultListView.as_view(), name="result-list"),
    path("results/<int:pk>/", EnhancementResultDetailView.as_view(), name="result-detail"),
    path(
        "results/<int:pk>/comparison/",
        EnhancementComparisonView.as_view(),
        name="result-comparison",
    ),
    path("results/<int:pk>/download/", EnhancementDownloadView.as_view(), name="result-download"),
]
