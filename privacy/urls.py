from django.urls import path

from .views import ConsentView, FacialDataView, PrivacyAuditView, PrivacyExportView

app_name = "privacy"

urlpatterns = [
    path("consents/", ConsentView.as_view(), name="consents"),
    path("export/", PrivacyExportView.as_view(), name="export"),
    path("facial-data/", FacialDataView.as_view(), name="facial-data"),
    path("audit/", PrivacyAuditView.as_view(), name="audit"),
]
