"""DRF exception handler: the default response, plus a record of refused requests.

403 (authorization failure) and 429 (rate limit) responses are recorded for the
security alert rules (security/services.py). The response itself is exactly
DRF's default.
"""
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None and response.status_code in (403, 429):
        from security.services import record_refused_request

        request = context.get("request")
        if request is not None:
            record_refused_request(request, response.status_code)
    return response
