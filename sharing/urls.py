from django.urls import path

from .views import (
    PublicShareInfoView,
    PublicShareView,
    ShareLinkDetailView,
    ShareLinkListCreateView,
    ShareLinkRevokeView,
)

app_name = "sharing"

urlpatterns = [
    path("", ShareLinkListCreateView.as_view(), name="link-list-create"),
    path("<int:pk>/", ShareLinkDetailView.as_view(), name="link-detail"),
    path("<int:pk>/revoke/", ShareLinkRevokeView.as_view(), name="link-revoke"),
    path("public/<str:token>/", PublicShareView.as_view(), name="public-view"),
    path("public/<str:token>/info/", PublicShareInfoView.as_view(), name="public-info"),
]
