from django.urls import path

from .approval_views import (
    OrganizationDownloadGrantRevokeView,
    OrganizationDownloadRequestDecisionView,
    OrganizationDownloadRequestDetailView,
    OrganizationDownloadRequestListView,
)

# Mounted under /api/organizations/ (config/urls.py): the owner's and moderators' side of download approval.
urlpatterns = [
    path("<int:organization_id>/download-requests/", OrganizationDownloadRequestListView.as_view(), name="org-download-request-list"),
    path("<int:organization_id>/download-requests/<int:request_id>/", OrganizationDownloadRequestDetailView.as_view(), name="org-download-request-detail"),
    path("<int:organization_id>/download-requests/<int:request_id>/decision/", OrganizationDownloadRequestDecisionView.as_view(), name="org-download-request-decision"),
    path("<int:organization_id>/download-grants/<int:grant_id>/revoke/", OrganizationDownloadGrantRevokeView.as_view(), name="org-download-grant-revoke"),
]
