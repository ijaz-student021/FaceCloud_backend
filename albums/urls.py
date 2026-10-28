from django.urls import path

from .views import AlbumCoverView, AlbumDetailView, AlbumImagesRemoveView, AlbumImagesView, AlbumListCreateView

app_name = "albums"
urlpatterns = [
    path("", AlbumListCreateView.as_view(), name="album-list"),
    path("<int:pk>/", AlbumDetailView.as_view(), name="album-detail"),
    path("<int:pk>/images/", AlbumImagesView.as_view(), name="album-images"),
    path("<int:pk>/images/remove/", AlbumImagesRemoveView.as_view(), name="album-images-remove"),
    path("<int:pk>/cover/", AlbumCoverView.as_view(), name="album-cover"),
]
