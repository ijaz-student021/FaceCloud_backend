from django.urls import path

from .views import CollageDetailView, CollageFileView, CollageListCreateView

urlpatterns = [
    path("", CollageListCreateView.as_view(), name="collage-list"),
    path("<int:pk>/", CollageDetailView.as_view(), name="collage-detail"),
    path("<int:pk>/download/", CollageFileView.as_view(), name="collage-download"),
]
