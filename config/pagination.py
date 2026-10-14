"""Pagination for list endpoints that existing clients read as plain arrays.

A request with ``page`` or ``page_size`` gets the standard envelope
``{"count", "next", "previous", "results"}``; one without gets the plain array
it always got. New clients should always pass ``page`` (or ``page_size``):
the unpaginated form remains only so current consumers keep working while they
migrate, and is unbounded on large accounts.
"""
from rest_framework.pagination import PageNumberPagination


class OptionalPageNumberPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def paginate_queryset(self, queryset, request, view=None):
        if not ({self.page_query_param, self.page_size_query_param} & set(request.query_params)):
            return None
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response_schema(self, schema):
        envelope = super().get_paginated_response_schema(schema)
        envelope["description"] = "Returned when page or page_size is given."
        return {
            "oneOf": [
                envelope,
                {**schema, "description": "Returned when neither page nor page_size is given (legacy, unbounded)."},
            ],
        }
