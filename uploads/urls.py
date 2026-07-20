from django.urls import path

from .views import (
    ImageDownloadView,
    ImagePreprocessView,
    ImagePreprocessingDetailView,
    ImagePreprocessingDownloadView,
    ImageUploadDetailView,
    ImageUploadListView,
    MyImageListView,
    MultipleImageUploadView,
    SingleImageUploadView,
    UploadBatchDetailView,
    UploadBatchCancelView,
    UploadBatchProgressView,
    UploadBatchRetryView,
    UploadBatchListView,
    ZipImageUploadView,
)

urlpatterns = [
    path("upload/single/", SingleImageUploadView.as_view(), name="image-upload-single"),
    path("upload/multiple/", MultipleImageUploadView.as_view(), name="image-upload-multiple"),
    path("upload/zip/", ZipImageUploadView.as_view(), name="image-upload-zip"),
    path("batches/", UploadBatchListView.as_view(), name="upload-batch-list"),
    path("batches/<int:pk>/", UploadBatchDetailView.as_view(), name="upload-batch-detail"),
    path("batches/<int:batch_id>/progress/", UploadBatchProgressView.as_view(), name="upload-batch-progress"),
    path("batches/<int:batch_id>/retry/", UploadBatchRetryView.as_view(), name="upload-batch-retry"),
    path("batches/<int:batch_id>/cancel/", UploadBatchCancelView.as_view(), name="upload-batch-cancel"),
    path("my/", MyImageListView.as_view(), name="my-image-list"),
    path("", ImageUploadListView.as_view(), name="image-upload-list"),
    path("<int:pk>/download/", ImageDownloadView.as_view(), name="image-upload-download"),
    path("<int:pk>/preprocess/", ImagePreprocessView.as_view(), name="image-preprocess"),
    path("<int:pk>/preprocessing/", ImagePreprocessingDetailView.as_view(), name="image-preprocessing-detail"),
    path("<int:pk>/preprocessing/download/", ImagePreprocessingDownloadView.as_view(), name="image-preprocessing-download"),
    path("<int:pk>/", ImageUploadDetailView.as_view(), name="image-upload-detail"),
]
