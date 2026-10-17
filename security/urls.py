from django.urls import path

from .views import SecurityAlertAcknowledgeView, SecurityAlertDetailView, SecurityAlertListView, SecurityRulesView

app_name = "security"

urlpatterns = [
    path("alerts/", SecurityAlertListView.as_view(), name="alert-list"),
    path("alerts/<int:pk>/", SecurityAlertDetailView.as_view(), name="alert-detail"),
    path("alerts/<int:pk>/acknowledge/", SecurityAlertAcknowledgeView.as_view(), name="alert-acknowledge"),
    path("rules/", SecurityRulesView.as_view(), name="rules"),
]
