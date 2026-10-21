"""What the adaptive engine does when it cannot verify identity, named in one place.

Identity verification can be unavailable for a photograph that has a face: no biometric consent, no identity model, a face too dark or too
small to embed, or a comparison that was inconclusive. ``ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY`` decides what the engine then does, and
``identity_verification.mode`` in the stored result says which of these happened. In every mode ``verified`` is false unless a comparison actually
held, and face restoration (the only operation that synthesises a face) is never applied without a verified identity.

| mode | meaning |
|---|---|
| ``verified`` | every face in the result was compared with the upload and held |
| ``limited_unverified`` | the policy is ``limited`` (the default): operations that do not synthesise a face were applied after the geometry checks, and identity was NOT verified; ``notice`` says why |
| ``failed_closed`` | the policy is ``fail_closed``: identity could not be verified, so nothing was applied and the original was returned |
| ``not_applicable`` | no face, or the photograph was not changed: there was nothing to verify |
"""
from django.conf import settings

LIMITED = "limited"
FAIL_CLOSED = "fail_closed"

MODE_VERIFIED = "verified"
MODE_LIMITED = "limited_unverified"
MODE_FAILED_CLOSED = "failed_closed"
MODE_NOT_APPLICABLE = "not_applicable"
MODES = (MODE_VERIFIED, MODE_LIMITED, MODE_FAILED_CLOSED, MODE_NOT_APPLICABLE)


def fail_closed():
    """Whether a photograph with a face whose identity cannot be verified is left unchanged (the opt-in policy)."""
    return settings.ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY == FAIL_CLOSED
