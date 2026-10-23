"""JSON errors for the API, Django's own pages for everything else.

Every error the API raises is JSON (``config/exceptions.py``), except two Django produces before or after a view: an address that matches no route (``/api/images/abc/``, where the
route expects a number) and an uncaught exception. Both used to answer with Django's HTML page. ``config/urls.py`` points ``handler404`` and ``handler500`` here: under ``/api/`` they
answer JSON, anywhere else (the Django admin, static files) they hand over to Django's own handlers, unchanged.

With ``DEBUG`` on, Django bypasses ``handler404`` and shows its yellow debug page; ``ApiJsonNotFoundMiddleware`` turns that page into the same JSON under ``/api/`` so a development server
behaves like production. A 500 is left alone in DEBUG, so the traceback stays visible to the developer.
"""
from django.conf import settings
from django.http import JsonResponse
from django.views import defaults

API_PREFIX = "/api/"


def is_api(request):
    return request.path.startswith(API_PREFIX)


def not_found(request, exception, template_name=defaults.ERROR_404_TEMPLATE_NAME):
    if is_api(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    return defaults.page_not_found(request, exception, template_name=template_name)


def server_error(request, template_name=defaults.ERROR_500_TEMPLATE_NAME):
    if is_api(request):
        return JsonResponse({"detail": "Server error."}, status=500)
    return defaults.server_error(request, template_name=template_name)


class ApiJsonNotFoundMiddleware:
    """Under ``/api/`` with DEBUG on, an HTML 404 (Django's debug page) becomes the JSON 404 production gives. Does nothing when DEBUG is off (the handler already answered)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if settings.DEBUG and response.status_code == 404 and is_api(request) and response.get("Content-Type", "").startswith("text/html"):
            return JsonResponse({"detail": "Not found."}, status=404)
        return response
