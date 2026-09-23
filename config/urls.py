"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/auth/", include("accounts.urls")),
    path("api/user/", include("accounts.user_urls")),
    path("api/images/", include("uploads.urls")),
    path("api/enhancements/", include("enhancement.urls")),
    path("api/validation/", include("validation.urls")),
    path("api/quality/", include("quality_control.urls")),
    path("api/duplicates/", include("duplicates.urls")),
    path("api/identities/", include("identities.urls")),
    path("api/reports/", include("reports.urls")),
    path("api/downloads/", include("downloads.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api/privacy/", include("privacy.urls")),
    path("api/shares/", include("sharing.urls")),
    path("api/system/", include("operations.urls")),
    path("api/organizations/", include("organizations.urls")),
    path("api/organizations/", include("moderation.urls")),
]
