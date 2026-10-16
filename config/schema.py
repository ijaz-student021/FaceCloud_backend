"""OpenAPI generation: the catalogue text from ``config/api_docs.py`` plus facts read from the code (access, rate limit).

``FaceCloudAutoSchema`` is the project's default schema class. It changes only the *text* of an operation: the summary, and a
description made of the catalogue entry (or the view's own help text, with internal notes removed) followed by who may call it and
which rate-limit scope applies. The catalogue wins over text a view sets itself; a catalogue entry with an empty description keeps the view's own
help text (internal notes removed).
"""
from django.conf import settings
from drf_spectacular.openapi import AutoSchema

from config.api_docs import CATALOGUE, EXAMPLES

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# Lines of developer-facing help text that must not reach the public documentation.
INTERNAL_MARKERS = ("request.active_membership", "self.organization", "``")


def clean_help_text(text):
    kept = [line for line in (text or "").splitlines() if not any(marker in line for marker in INTERNAL_MARKERS)]
    return "\n".join(kept).strip()


class FaceCloudAutoSchema(AutoSchema):
    def _access_note(self):
        classes = {cls.__name__ for cls in getattr(self.view, "permission_classes", [])}
        if "AllowAny" in classes:
            access = "public, no token needed"
        elif "IsAdminRole" in classes:
            access = "FaceCloud administrators only (403 otherwise)"
        elif self.path.startswith("/api/organizations/") and "/admin/" not in self.path:
            access = "signed-in members of the organization; the role required is enforced per endpoint (403 otherwise)"
        else:
            access = "any signed-in user, on their own data (another user's data is a 404)"
        note = f"**Access:** {access}."
        scope = getattr(self.view, "throttle_scope", None)
        rate = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}).get(scope) if scope else None
        if rate:
            note += f" **Rate limit:** `{scope}` scope, {rate} (429 when exceeded)."
        return note

    def get_operation(self, *args, **kwargs):
        operation = super().get_operation(*args, **kwargs)
        if operation is None:
            return operation
        entry = CATALOGUE.get(operation.get("operationId"))
        if entry:
            operation["summary"] = entry[0]
            if entry[1]:
                operation["description"] = entry[1]
        body = clean_help_text(operation.get("description", ""))
        operation["description"] = f"{body}\n\n{self._access_note()}".strip()
        return operation


def _json_content(container):
    return container.setdefault("content", {}).setdefault("application/json", {"schema": {"type": "object"}})


def add_examples(result, generator, request, public):
    """Attach the example bodies of ``config/api_docs.EXAMPLES`` to the operations they name."""
    for item in result.get("paths", {}).values():
        for method, operation in item.items():
            if method not in HTTP_METHODS or operation.get("operationId") not in EXAMPLES:
                continue
            example = EXAMPLES[operation["operationId"]]
            if "request" in example and "requestBody" in operation:
                content = operation["requestBody"].get("content", {})
                target = content.get("application/json")
                if target is not None:
                    target["examples"] = {"typical": {"summary": "Typical request", "value": example["request"]}}
            for code, body in example.get("responses", {}).items():
                response = operation.get("responses", {}).get(code)
                if response is not None:
                    _json_content(response)["examples"] = {"typical": {"summary": "Typical response", "value": body}}
    return result
