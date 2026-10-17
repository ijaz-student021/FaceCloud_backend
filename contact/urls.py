from django.urls import path

from .views import ContactMessageView

app_name = "contact"

urlpatterns = [path("", ContactMessageView.as_view(), name="submit")]
