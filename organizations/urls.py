from django.urls import path

from . import leave_views, views

app_name = "organizations"

urlpatterns = [
    path("", views.OrganizationListView.as_view(), name="organization-list"),
    path("admin/", views.AdminOrganizationListCreateView.as_view(), name="organization-admin-list-create"),
    path("admin/<int:organization_id>/", views.AdminOrganizationDetailView.as_view(), name="organization-admin-detail"),
    path("admin/<int:organization_id>/owner-invitation/", views.AdminOwnerInvitationView.as_view(), name="organization-admin-owner-invitation"),
    path("admin/<int:organization_id>/owner-invitation/resend/", views.AdminOwnerInvitationResendView.as_view(), name="organization-admin-owner-invitation-resend"),
    path("admin/<int:organization_id>/owner-invitation/cancel/", views.AdminOwnerInvitationCancelView.as_view(), name="organization-admin-owner-invitation-cancel"),
    path("admin/<int:organization_id>/leave-requests/<int:leave_id>/decision/", leave_views.AdminLeaveRequestDecisionView.as_view(), name="organization-admin-leave-decision"),
    path("<int:organization_id>/leave-request/", leave_views.MyLeaveRequestView.as_view(), name="organization-leave-request"),
    path("<int:organization_id>/leave-request/cancel/", leave_views.MyLeaveRequestCancelView.as_view(), name="organization-leave-request-cancel"),
    path("<int:organization_id>/moderator/leave-requests/", leave_views.ModeratorLeaveRequestListView.as_view(), name="organization-leave-requests"),
    path("<int:organization_id>/moderator/leave-requests/<int:leave_id>/decision/", leave_views.ModeratorLeaveRequestDecisionView.as_view(), name="organization-leave-request-decision"),
    path("invitations/accept/", views.OrganizationInvitationAcceptView.as_view(), name="organization-invitation-accept"),
    path("invitations/<str:token>/", views.OrganizationInvitationPreviewView.as_view(), name="organization-invitation-preview"),
    path("<int:organization_id>/", views.OrganizationDetailView.as_view(), name="organization-detail"),
    path("<int:organization_id>/logo/", views.OrganizationLogoView.as_view(), name="organization-logo"),
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
