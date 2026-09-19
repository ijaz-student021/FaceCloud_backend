from django.urls import path

from .views import (
    BatchEnhanceView,
    EnhancementComparisonView,
    EnhancementDownloadView,
    EnhancementJobDetailView,
    EnhancementJobListView,
    EnhancementResultDetailView,
    EnhancementResultListView,
    EnhancementWatermarkPreviewView,
    GalleryEditActivateView,
    GalleryEditDetailView,
    GalleryEditDownloadView,
    GalleryEditListCreateView,
    GalleryEditorConfigurationView,
    ImageEnhanceView,
)

app_name = "enhancement"

urlpatterns = [
    path("editor/configuration/", GalleryEditorConfigurationView.as_view(), name="editor-configuration"),
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
    path(
        "results/<int:pk>/watermark-preview/",
        EnhancementWatermarkPreviewView.as_view(),
        name="result-watermark-preview",
    ),
    path("results/<int:result_id>/edits/", GalleryEditListCreateView.as_view(), name="edit-list-create"),
    path("edits/<int:pk>/", GalleryEditDetailView.as_view(), name="edit-detail"),
    path("edits/<int:pk>/activate/", GalleryEditActivateView.as_view(), name="edit-activate"),
    path("edits/<int:pk>/download/", GalleryEditDownloadView.as_view(), name="edit-download"),
]
