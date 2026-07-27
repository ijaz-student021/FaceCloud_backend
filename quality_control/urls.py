from django.urls import path

from .views import ImageQualityView, QualityResultListView

app_name = "quality"

urlpatterns = [
    path("images/<int:image_id>/", ImageQualityView.as_view(), name="image-quality"),
    path("results/", QualityResultListView.as_view(), name="result-list"),
]
