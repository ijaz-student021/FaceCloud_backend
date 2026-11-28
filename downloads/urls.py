from django.urls import path

from .approval_views import (
    DownloadEligibilityView,
    DownloadRequestCancelView,
    DownloadRequestDetailView,
    DownloadRequestItemFileView,
    DownloadRequestListCreateView,
    DownloadRequestZipView,
)
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
    # Download approval (organizations that require it): the member's side.
    path("requests/", DownloadRequestListCreateView.as_view(), name="request-list-create"),
    path("requests/check/", DownloadEligibilityView.as_view(), name="request-check"),
    path("requests/<int:pk>/", DownloadRequestDetailView.as_view(), name="request-detail"),
    path("requests/<int:pk>/cancel/", DownloadRequestCancelView.as_view(), name="request-cancel"),
    path("requests/<int:pk>/zip/", DownloadRequestZipView.as_view(), name="request-zip"),
    path("requests/<int:pk>/items/<int:item_id>/file/", DownloadRequestItemFileView.as_view(), name="request-item-file"),
]
