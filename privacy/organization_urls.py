"""Organization-scoped privacy endpoints (T-006), mounted at the api/organizations/
prefix organizations.urls and moderation.urls already share -- see those apps for
the OrganizationScopedMixin convention this follows.
"""
from django.urls import path

from .views import OrganizationExportDetailView, OrganizationExportFileView, OrganizationExportListCreateView

app_name = "privacy_organizations"

urlpatterns = [
    path(
        "<int:organization_id>/export/",
        OrganizationExportListCreateView.as_view(), name="organization-export-list-create",
    ),
    path(
        "<int:organization_id>/export/<int:export_id>/",
        OrganizationExportDetailView.as_view(), name="organization-export-detail",
    ),
    path(
        "<int:organization_id>/export/<int:export_id>/download/",
        OrganizationExportFileView.as_view(), name="organization-export-download",
    ),
]
