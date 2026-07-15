from django.urls import path

from .views import (
    ImageDownloadView,
    ImageUploadDetailView,
    ImageUploadListView,
    MultipleImageUploadView,
    SingleImageUploadView,
    UploadBatchDetailView,
    UploadBatchListView,
    ZipImageUploadView,
)

urlpatterns = [
    path("upload/single/", SingleImageUploadView.as_view(), name="image-upload-single"),
    path("upload/multiple/", MultipleImageUploadView.as_view(), name="image-upload-multiple"),
    path("upload/zip/", ZipImageUploadView.as_view(), name="image-upload-zip"),
    path("batches/", UploadBatchListView.as_view(), name="upload-batch-list"),
    path("batches/<int:pk>/", UploadBatchDetailView.as_view(), name="upload-batch-detail"),
    path("", ImageUploadListView.as_view(), name="image-upload-list"),
    path("<int:pk>/download/", ImageDownloadView.as_view(), name="image-upload-download"),
    path("<int:pk>/", ImageUploadDetailView.as_view(), name="image-upload-detail"),
]
