from django.urls import path

from .views import SystemHealthView


app_name = "operations"

urlpatterns = [
    path("health/", SystemHealthView.as_view(), name="system-health"),
]

