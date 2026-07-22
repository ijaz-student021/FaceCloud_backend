from django.urls import path

from .views import (
    BatchPackageCreateView,
    DownloadHistoryListView,
    DownloadPackageDetailView,
    DownloadPackageFileView,
    DownloadPackageListView,
    DownloadPackageRegenerateView,
)

app_name = "downloads"

urlpatterns = [
    path("", DownloadHistoryListView.as_view(), name="history"),
    path("packages/", DownloadPackageListView.as_view(), name="package-list"),
    path("batches/<int:batch_id>/packages/", BatchPackageCreateView.as_view(), name="package-create"),
    path("packages/<int:pk>/", DownloadPackageDetailView.as_view(), name="package-detail"),
    path("packages/<int:pk>/download/", DownloadPackageFileView.as_view(), name="package-download"),
    path("packages/<int:pk>/regenerate/", DownloadPackageRegenerateView.as_view(), name="package-regenerate"),
]
