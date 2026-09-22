from django.urls import path

from .views import QueueStatusView, SystemHealthView


app_name = "operations"

urlpatterns = [
    path("health/", SystemHealthView.as_view(), name="system-health"),
    path("queue/", QueueStatusView.as_view(), name="queue-status"),
]

