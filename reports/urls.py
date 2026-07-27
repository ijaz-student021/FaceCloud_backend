from django.urls import path

from .views import (
    BatchReportCreateView,
    ImageReportCreateView,
    ProcessingReportDetailView,
    ProcessingReportDownloadView,
    ProcessingReportListView,
    ProcessingReportRegenerateView,
)

app_name = "reports"

urlpatterns = [
    path("", ProcessingReportListView.as_view(), name="list"),
    path("images/<int:image_id>/", ImageReportCreateView.as_view(), name="image-create"),
    path("batches/<int:batch_id>/", BatchReportCreateView.as_view(), name="batch-create"),
    path("<int:pk>/", ProcessingReportDetailView.as_view(), name="detail"),
    path("<int:pk>/download/", ProcessingReportDownloadView.as_view(), name="download"),
    path("<int:pk>/regenerate/", ProcessingReportRegenerateView.as_view(), name="regenerate"),
]
