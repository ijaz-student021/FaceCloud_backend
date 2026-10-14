from django.urls import path

from .views import CameraSessionDetailView, CameraSessionEndView, CameraSessionListCreateView, CameraSnapshotCreateView

app_name = "camera"

urlpatterns = [
    path("sessions/", CameraSessionListCreateView.as_view(), name="session-list"),
    path("sessions/<uuid:session_id>/", CameraSessionDetailView.as_view(), name="session-detail"),
    path("sessions/<uuid:session_id>/end/", CameraSessionEndView.as_view(), name="session-end"),
    path("sessions/<uuid:session_id>/snapshots/", CameraSnapshotCreateView.as_view(), name="snapshot-create"),
]
