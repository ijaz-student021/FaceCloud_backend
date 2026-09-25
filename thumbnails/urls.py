from django.urls import path

from .views import ImageThumbnailView

urlpatterns = [
    path("<int:pk>/thumbnail/", ImageThumbnailView.as_view(), name="image-thumbnail"),
]
