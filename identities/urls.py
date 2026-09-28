from django.urls import path

from .views import (IdentityAssignmentReviewView, IdentityChangeListView,
                    IdentityGroupDeleteView, IdentityGroupDetailView,
                    IdentityGroupExportDetailView,
                    IdentityGroupExportDownloadView,
                    IdentityGroupExportListCreateView,
                    IdentityGroupExportRegenerateView,
                    IdentityGroupListView, IdentityGroupMergeView,
                    IdentityGroupProfileView, IdentityGroupRenameView,
                    IdentityImageAnalysisView, IdentityMembershipMoveView,
                    IdentityMembershipRemoveView, IdentityReviewListView,
                    IdentityVisualSearchView)

app_name = "identities"
urlpatterns = [
    path("images/<int:image_id>/", IdentityImageAnalysisView.as_view(), name="image-analysis"),
    path("search/", IdentityVisualSearchView.as_view(), name="visual-search"),
    path("groups/", IdentityGroupListView.as_view(), name="group-list"),
    path("groups/<int:pk>/", IdentityGroupDetailView.as_view(), name="group-detail"),
    path("groups/<int:pk>/rename/", IdentityGroupRenameView.as_view(), name="group-rename"),
    path("groups/<int:pk>/merge/", IdentityGroupMergeView.as_view(), name="group-merge"),
    path("groups/<int:pk>/profile/", IdentityGroupProfileView.as_view(), name="group-profile"),
    path("groups/<int:pk>/delete/", IdentityGroupDeleteView.as_view(), name="group-delete"),
    path("groups/<int:group_id>/exports/", IdentityGroupExportListCreateView.as_view(), name="group-export-list-create"),
    path("exports/<int:pk>/", IdentityGroupExportDetailView.as_view(), name="group-export-detail"),
    path("exports/<int:pk>/download/", IdentityGroupExportDownloadView.as_view(), name="group-export-download"),
    path("exports/<int:pk>/regenerate/", IdentityGroupExportRegenerateView.as_view(), name="group-export-regenerate"),
    path("review/", IdentityReviewListView.as_view(), name="review-list"),
    path("assignments/<int:pk>/review/", IdentityAssignmentReviewView.as_view(), name="assignment-review"),
    path("memberships/<int:pk>/move/", IdentityMembershipMoveView.as_view(), name="membership-move"),
    path("memberships/<int:pk>/remove/", IdentityMembershipRemoveView.as_view(), name="membership-remove"),
    path("history/", IdentityChangeListView.as_view(), name="history-list"),
]
