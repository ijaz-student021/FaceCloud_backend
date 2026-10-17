"""The error a consent-gated endpoint answers when the person has not agreed.

Same 400 and same sentence as before; the body gains a stable ``code`` so the frontend can send the
person to the consent screen without matching on wording.
"""
from rest_framework.exceptions import ValidationError

CONSENT_REQUIRED = "consent_required"


class ConsentRequired(ValidationError):
    """400 ``{"detail": ["<message>"], "code": "consent_required"}``."""

    def __init__(self, message):
        super().__init__({"detail": [message], "code": CONSENT_REQUIRED})
