from django.urls import path

from .views import ImageFaceValidationView

app_name = "validation"

urlpatterns = [
    path("images/<int:image_id>/", ImageFaceValidationView.as_view(), name="image-validation"),
]
