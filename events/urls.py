from django.urls import path

from .views import EventDetailView, EventListView, EventPhotoListView

urlpatterns = [
    path("", EventListView.as_view(), name="event-list"),
    path("<int:event_id>/", EventDetailView.as_view(), name="event-detail"),
    path("<int:event_id>/photos/", EventPhotoListView.as_view(), name="event-photos"),
]
