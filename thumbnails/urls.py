from django.urls import path

from .views import ImageThumbnailView, TrashThumbnailView

urlpatterns = [
    path("<int:pk>/thumbnail/", ImageThumbnailView.as_view(), name="image-thumbnail"),
    path("trash/<int:pk>/thumbnail/", TrashThumbnailView.as_view(), name="image-trash-thumbnail"),
]
