"""Consent helpers for tests in other apps.

Face embedding, face search and person grouping all sit behind recorded
consent, and production insists on it (``BIOMETRIC_CONSENT_REQUIRED`` is
checked by config/production_guard.py). A test that exercises those paths has
to grant consent the way a real account would, or it only passes on a machine
where the requirement happens to be switched off.
"""
from .models import ConsentPurpose
from .services import current_policy_version, record_consent


def grant_face_consent(*users):
    """Record face-identity consent for each user, against the current policy."""
    for user in users:
        record_consent(user, ConsentPurpose.FACE_IDENTITY, True, current_policy_version())


def withdraw_face_consent(*users):
    """Take face-identity consent back, for tests of what happens without it.

    Prefer this over only flipping ``BIOMETRIC_CONSENT_REQUIRED`` on: a test
    that asserts "refused without consent" should make sure consent is actually
    absent, rather than relying on nothing having granted it.
    """
    for user in users:
        record_consent(user, ConsentPurpose.FACE_IDENTITY, False, current_policy_version())
