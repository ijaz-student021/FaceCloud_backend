"""OpenAPI post-processing shared by every endpoint.

Two responses apply to (almost) the whole API and would otherwise have to be
repeated on each view: 401 for authenticated routes and 429 from the rate
limiter. Adding them here keeps the schema honest without touching every view.
Endpoint-specific errors (400, 403, 404, the 429 for the job limit) stay on the
views that raise them.
"""

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
ERROR_SCHEMA = "ErrorDetail"


def _requires_authentication(operation):
    security = operation.get("security")
    # An empty requirement ({}) means anonymous access is also accepted.
    return bool(security) and all(security)


def add_standard_error_responses(result, generator, request, public):
    schemas = result.setdefault("components", {}).setdefault("schemas", {})
    schemas[ERROR_SCHEMA] = {
        "type": "object",
        "description": "Error body used by every non-validation error.",
        "properties": {
            "detail": {"type": "string", "description": "Human-readable explanation."},
            "code": {
                "type": "string",
                "description": "Stable machine-readable code, present on some errors.",
            },
        },
        "required": ["detail"],
    }
    reference = {"$ref": f"#/components/schemas/{ERROR_SCHEMA}"}

    def response(description):
        return {"description": description, "content": {"application/json": {"schema": reference}}}

    for path_item in result.get("paths", {}).values():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            responses = operation.setdefault("responses", {})
            if _requires_authentication(operation):
                responses.setdefault(
                    "401", response("Missing, expired, revoked or invalid access token."),
                )
            responses.setdefault(
                "429", response("Rate limit or per-user job limit reached; retry later."),
            )
    return result
