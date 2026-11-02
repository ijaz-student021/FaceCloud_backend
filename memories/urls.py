from django.urls import path

from .views import MemoriesView, MemoryDetailView

app_name = "memories"
urlpatterns = [
    path("", MemoriesView.as_view(), name="memory-list"),
    path("<str:memory_id>/", MemoryDetailView.as_view(), name="memory-detail"),
]
