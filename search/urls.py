from django.urls import path

from .views import SearchView, SemanticPhotoSearchView

urlpatterns = [
    path("", SearchView.as_view(), name="search"),
    path("photos/", SemanticPhotoSearchView.as_view(), name="search-photos"),
]
