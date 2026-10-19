from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("", views.OrganizationListView.as_view(), name="organization-list"),
    path("admin/", views.AdminOrganizationListCreateView.as_view(), name="organization-admin-list-create"),
    path("invitations/accept/", views.OrganizationInvitationAcceptView.as_view(), name="organization-invitation-accept"),
    path("invitations/<str:token>/", views.OrganizationInvitationPreviewView.as_view(), name="organization-invitation-preview"),
    path("<int:organization_id>/", views.OrganizationDetailView.as_view(), name="organization-detail"),
    path("<int:organization_id>/members/", views.OrganizationMemberListView.as_view(), name="organization-member-list"),
    path(
        "<int:organization_id>/members/<int:membership_id>/",
        views.OrganizationMemberDetailView.as_view(), name="organization-member-detail",
    ),
    path(
        "<int:organization_id>/invitations/",
        views.OrganizationInvitationListCreateView.as_view(), name="organization-invitation-list-create",
    ),
    path(
        "<int:organization_id>/invitations/<int:invitation_id>/resend/",
        views.OrganizationInvitationResendView.as_view(), name="organization-invitation-resend",
    ),
    path(
        "<int:organization_id>/invitations/<int:invitation_id>/",
        views.OrganizationInvitationCancelView.as_view(), name="organization-invitation-cancel",
    ),
    path("<int:organization_id>/audit/", views.OrganizationAuditListView.as_view(), name="organization-audit-list"),
]
