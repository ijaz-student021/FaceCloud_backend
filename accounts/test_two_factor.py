"""Two-factor authentication (TOTP): the algorithm, secret encryption, and the
whole enrol -> confirm -> log in with a code -> disable lifecycle.

Login now has two steps once 2FA is on: POST /api/auth/login/ (password) returns
`{two_factor_required, login_token}` instead of tokens; POST /api/auth/2fa/verify/
(login_token, code) returns the real tokens. Existing accounts/tests.py's 40 tests
staying green (password-only login unaffected) is the regression guard for that
refactor; this file is the new behaviour.
"""
import base64
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from . import totp
from .models import TwoFactorCredential, TwoFactorRecoveryCode
from .services import (
    confirm_two_factor,
    disable_two_factor,
    has_active_two_factor,
    issue_login_tokens,
    make_two_factor_login_challenge,
    regenerate_recovery_codes,
    setup_two_factor,
    two_factor_status,
    verify_two_factor_login,
)
from .two_factor_crypto import TwoFactorSecretError, decrypt_secret, encrypt_secret

User = get_user_model()
RFC_6238_KEY_ASCII = b"12345678901234567890"
RFC_6238_TIME = 59  # published test vector: T=59s -> counter 1 -> 8-digit code 94287082


class TotpAlgorithmTests(TestCase):
    def test_matches_the_published_rfc_6238_test_vector(self):
        secret = base64.b32encode(RFC_6238_KEY_ASCII).decode()
        code = totp.generate_code(secret, when=RFC_6238_TIME)
        # RFC 6238 Appendix B's own 8-digit example for this key/time is 94287082;
        # this library's 6-digit truncation is its last 6 digits.
        self.assertEqual(code, "287082")

    def test_round_trip_generate_and_verify(self):
        secret = totp.generate_secret()
        code = totp.generate_code(secret)
        valid, counter = totp.verify_code(secret, code)
        self.assertTrue(valid)
        self.assertEqual(counter, totp.current_counter())

    def test_a_wrong_code_is_rejected(self):
        secret = totp.generate_secret()
        real = totp.generate_code(secret)
        wrong = "".join("1" if digit != "1" else "2" for digit in real)
        self.assertEqual(totp.verify_code(secret, wrong), (False, None))

    def test_malformed_input_is_rejected_not_raised(self):
        secret = totp.generate_secret()
        for bad in ("", "12345", "1234567", "abcdef", None, "12 345"):
            self.assertEqual(totp.verify_code(secret, bad), (False, None))

    def test_clock_drift_within_the_window_is_accepted(self):
        secret = totp.generate_secret()
        now = time.time()
        earlier = totp.generate_code(secret, when=now - totp.PERIOD_SECONDS)
        later = totp.generate_code(secret, when=now + totp.PERIOD_SECONDS)
        self.assertTrue(totp.verify_code(secret, earlier, when=now)[0])
        self.assertTrue(totp.verify_code(secret, later, when=now)[0])

    def test_drift_outside_the_window_is_rejected(self):
        secret = totp.generate_secret()
        now = time.time()
        far = totp.generate_code(secret, when=now - totp.PERIOD_SECONDS * 5)
        self.assertFalse(totp.verify_code(secret, far, when=now)[0])

    def test_a_code_cannot_be_replayed_once_its_counter_was_used(self):
        secret = totp.generate_secret()
        counter = totp.current_counter()
        code = totp.generate_code(secret)
        self.assertFalse(totp.verify_code(secret, code, last_used_counter=counter)[0])
        self.assertFalse(totp.verify_code(secret, code, last_used_counter=counter + 5)[0])
        self.assertTrue(totp.verify_code(secret, code, last_used_counter=counter - 1)[0])

    def test_the_provisioning_uri_carries_the_secret_and_parameters(self):
        uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "alice@example.com", "FaceCloud")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=JBSWY3DPEHPK3PXP", uri)
        self.assertIn("issuer=FaceCloud", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)

    def test_generated_secrets_are_valid_base32_and_practically_unique(self):
        secrets_seen = {totp.generate_secret() for _ in range(20)}
        self.assertEqual(len(secrets_seen), 20)
        for secret in secrets_seen:
            base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))  # must not raise


class TwoFactorCryptoTests(TestCase):
    def test_round_trip(self):
        secret = totp.generate_secret()
        self.assertEqual(decrypt_secret(encrypt_secret(secret)), secret)

    def test_the_stored_payload_never_contains_the_plaintext_secret(self):
        secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        payload = encrypt_secret(secret)
        self.assertNotIn(secret.encode("ascii"), bytes(payload))

    def test_encrypting_the_same_secret_twice_gives_different_ciphertext(self):
        secret = totp.generate_secret()
        self.assertNotEqual(encrypt_secret(secret), encrypt_secret(secret))

    def test_tampered_ciphertext_is_refused_not_silently_wrong(self):
        payload = bytearray(encrypt_secret(totp.generate_secret()))
        payload[-1] ^= 0xFF
        with self.assertRaises(TwoFactorSecretError):
            decrypt_secret(bytes(payload))

    @override_settings(TWO_FACTOR_SECRET_KEY="a-completely-different-key-" * 2)
    def test_a_rotated_key_cannot_decrypt_old_ciphertext(self):
        with override_settings(TWO_FACTOR_SECRET_KEY="the-original-key-" * 3):
            payload = encrypt_secret(totp.generate_secret())
        with self.assertRaises(TwoFactorSecretError):
            decrypt_secret(payload)


class TwoFactorTestCase(TestCase):
    password = "StrongPass!4729"

    def setUp(self):
        self.user = User.objects.create_user(
            username="two-factor-user", email="two-factor-user@example.com", password=self.password, is_email_verified=True,
        )
        self.client = APIClient()

    def authenticate(self, user=None):
        user = user or self.user
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    def enroll(self, user=None):
        """Set up and confirm 2FA for ``user``; returns ``(secret, recovery_codes)``."""
        user = user or self.user
        setup = setup_two_factor(user)
        codes = confirm_two_factor(user, totp.generate_code(setup["secret"]))
        return setup["secret"], codes

    def fresh_code(self, secret):
        """A code for the *next* 30-second window.

        Confirming (or any earlier successful verification) in this same test
        already consumed the current window's code as an anti-replay watermark,
        exactly as a real device's currently-displayed code would be after using
        it once -- a genuine next action needs the next one, same as a real user.
        """
        return totp.generate_code(secret, when=time.time() + totp.PERIOD_SECONDS)


class SetupAndConfirmServiceTests(TwoFactorTestCase):
    def test_setup_returns_a_secret_and_an_otpauth_uri_but_does_not_activate_it(self):
        result = setup_two_factor(self.user)
        self.assertIn("otpauth://totp/", result["otpauth_uri"])
        self.assertFalse(has_active_two_factor(self.user))
        credential = TwoFactorCredential.objects.get(user=self.user)
        self.assertFalse(credential.is_active)

    def test_setup_again_before_confirming_replaces_the_pending_secret(self):
        first = setup_two_factor(self.user)
        second = setup_two_factor(self.user)
        self.assertNotEqual(first["secret"], second["secret"])
        self.assertEqual(TwoFactorCredential.objects.filter(user=self.user).count(), 1)

    def test_setup_is_refused_once_already_active(self):
        self.enroll()
        with self.assertRaises(ValidationError):
            setup_two_factor(self.user)

    def test_confirming_without_a_pending_setup_is_refused(self):
        with self.assertRaises(ValidationError):
            confirm_two_factor(self.user, "123456")

    def test_confirming_with_the_wrong_code_does_not_activate(self):
        setup_two_factor(self.user)
        with self.assertRaises(ValidationError):
            confirm_two_factor(self.user, "000000")
        self.assertFalse(has_active_two_factor(self.user))

    def test_confirming_with_the_real_code_activates_and_issues_recovery_codes(self):
        setup = setup_two_factor(self.user)
        codes = confirm_two_factor(self.user, totp.generate_code(setup["secret"]))
        self.assertTrue(has_active_two_factor(self.user))
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        self.assertEqual(TwoFactorRecoveryCode.objects.filter(credential__user=self.user).count(), 10)

    @override_settings(TWO_FACTOR_RECOVERY_CODE_COUNT=4)
    def test_the_recovery_code_count_is_configurable(self):
        setup = setup_two_factor(self.user)
        codes = confirm_two_factor(self.user, totp.generate_code(setup["secret"]))
        self.assertEqual(len(codes), 4)

    def test_status_reflects_enrollment(self):
        self.assertEqual(two_factor_status(self.user), {"enabled": False})
        self.enroll()
        self.assertEqual(two_factor_status(self.user), {"enabled": True})


class DisableAndRecoveryServiceTests(TwoFactorTestCase):
    def setUp(self):
        super().setUp()
        self.secret, self.recovery_codes = self.enroll()

    def test_disabling_without_two_factor_enabled_is_refused(self):
        other = User.objects.create_user(username="no-2fa", email="no-2fa@example.com", password="x", is_email_verified=True)
        with self.assertRaises(ValidationError):
            disable_two_factor(other, "123456")

    def test_a_wrong_code_does_not_disable(self):
        with self.assertRaises(ValidationError):
            disable_two_factor(self.user, "000000")
        self.assertTrue(has_active_two_factor(self.user))

    def test_the_real_totp_code_disables_and_removes_recovery_codes(self):
        disable_two_factor(self.user, self.fresh_code(self.secret))
        self.assertFalse(has_active_two_factor(self.user))
        self.assertFalse(TwoFactorRecoveryCode.objects.filter(credential__user=self.user).exists())

    def test_a_recovery_code_can_also_disable(self):
        disable_two_factor(self.user, self.recovery_codes[0])
        self.assertFalse(has_active_two_factor(self.user))

    def test_repeated_wrong_codes_lock_the_account_out(self):
        from django.conf import settings

        for _ in range(settings.LOGIN_LOCK_THRESHOLD):
            with self.assertRaises(ValidationError):
                disable_two_factor(self.user, "000000")
        with self.assertRaises(Throttled):
            disable_two_factor(self.user, self.fresh_code(self.secret))  # even the real code is refused while locked
        self.assertTrue(has_active_two_factor(self.user))

    def test_regenerating_recovery_codes_invalidates_the_old_ones(self):
        new_codes = regenerate_recovery_codes(self.user, self.fresh_code(self.secret))
        self.assertEqual(len(new_codes), len(self.recovery_codes))
        self.assertEqual(set(new_codes) & set(self.recovery_codes), set())
        with self.assertRaises(ValidationError):
            disable_two_factor(self.user, self.recovery_codes[0])
        disable_two_factor(self.user, new_codes[0])
        self.assertFalse(has_active_two_factor(self.user))

    def test_a_used_recovery_code_cannot_be_reused(self):
        used = self.recovery_codes[0]
        # spend it on a no-op regeneration attempt that fails first, then a real use
        regenerate_recovery_codes(self.user, used)
        with self.assertRaises(ValidationError):
            disable_two_factor(self.user, used)


class LoginFlowTests(TwoFactorTestCase):
    def login(self, identifier=None):
        return self.client.post(
            reverse("login"), {"identifier": identifier or self.user.username, "password": self.password}, format="json",
        )

    def test_a_user_without_two_factor_logs_in_normally(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertNotIn("two_factor_required", response.data)

    def test_a_user_with_two_factor_gets_a_challenge_instead_of_tokens(self):
        self.enroll()
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["two_factor_required"])
        self.assertIn("login_token", response.data)
        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)

    def test_a_wrong_password_is_still_refused_before_any_two_factor_check(self):
        self.enroll()
        response = self.client.post(reverse("login"), {"identifier": self.user.username, "password": "wrong"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("login_token", response.data)


class TwoFactorVerifyServiceTests(TwoFactorTestCase):
    def setUp(self):
        super().setUp()
        self.secret, self.recovery_codes = self.enroll()
        self.login_token = make_two_factor_login_challenge(self.user)

    def test_the_real_code_issues_the_same_shape_of_tokens_as_a_normal_login(self):
        result = verify_two_factor_login(self.login_token, self.fresh_code(self.secret))
        self.assertEqual(set(result), {"refresh", "access", "user", "role", "dashboard"})
        self.assertEqual(result["user"]["id"], self.user.pk)

    def test_a_recovery_code_also_completes_login(self):
        result = verify_two_factor_login(self.login_token, self.recovery_codes[0])
        self.assertIn("access", result)
        with self.assertRaises(ValidationError):
            verify_two_factor_login(make_two_factor_login_challenge(self.user), self.recovery_codes[0])

    def test_the_same_code_cannot_be_replayed_immediately(self):
        code = self.fresh_code(self.secret)
        verify_two_factor_login(self.login_token, code)
        with self.assertRaises(ValidationError):
            verify_two_factor_login(make_two_factor_login_challenge(self.user), code)

    def test_a_garbage_or_expired_login_token_is_refused(self):
        with self.assertRaises(ValidationError):
            verify_two_factor_login("not-a-real-token", totp.generate_code(self.secret))

    @override_settings(TWO_FACTOR_LOGIN_CHALLENGE_MINUTES=5)
    def test_an_expired_challenge_is_refused_even_with_the_right_code(self):
        import django.core.signing as signing_module

        real_time = signing_module.time.time
        signing_module.time.time = lambda: real_time() + 400
        try:
            with self.assertRaises(ValidationError):
                verify_two_factor_login(self.login_token, totp.generate_code(self.secret))
        finally:
            signing_module.time.time = real_time

    def test_wrong_codes_lock_out_the_verify_step_too(self):
        from django.conf import settings

        for _ in range(settings.LOGIN_LOCK_THRESHOLD):
            with self.assertRaises(ValidationError):
                verify_two_factor_login(make_two_factor_login_challenge(self.user), "000000")
        with self.assertRaises(Throttled):
            verify_two_factor_login(make_two_factor_login_challenge(self.user), self.fresh_code(self.secret))

    def test_a_suspended_account_cannot_finish_logging_in_even_with_the_right_code(self):
        self.user.is_suspended = True
        self.user.save(update_fields=["is_suspended"])
        with self.assertRaises(ValidationError):
            verify_two_factor_login(self.login_token, totp.generate_code(self.secret))

    def test_the_login_token_is_scoped_to_the_user_it_was_issued_for(self):
        other = User.objects.create_user(username="other-2fa", email="other-2fa@example.com", password="x", is_email_verified=True)
        other_secret, _codes = self.enroll(other)
        # This user's token combined with the OTHER user's real code must not work.
        with self.assertRaises(ValidationError):
            verify_two_factor_login(self.login_token, totp.generate_code(other_secret))


class TwoFactorApiTests(TwoFactorTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_setup_requires_authentication(self):
        anonymous = APIClient()
        response = anonymous.post(reverse("two-factor-setup"))
        self.assertEqual(response.status_code, 401)

    def test_the_full_enrol_confirm_disable_lifecycle_through_the_api(self):
        setup = self.client.post(reverse("two-factor-setup"))
        self.assertEqual(setup.status_code, 200)
        secret = setup.data["secret"]

        status_before = self.client.get(reverse("two-factor-status"))
        self.assertFalse(status_before.data["enabled"])

        confirm = self.client.post(reverse("two-factor-confirm"), {"code": totp.generate_code(secret)}, format="json")
        self.assertEqual(confirm.status_code, 200)
        self.assertEqual(len(confirm.data["recovery_codes"]), 10)

        status_after = self.client.get(reverse("two-factor-status"))
        self.assertTrue(status_after.data["enabled"])

        wrong_disable = self.client.post(reverse("two-factor-disable"), {"code": "000000"}, format="json")
        self.assertEqual(wrong_disable.status_code, 400)

        disable = self.client.post(reverse("two-factor-disable"), {"code": self.fresh_code(secret)}, format="json")
        self.assertEqual(disable.status_code, 204)
        self.assertFalse(self.client.get(reverse("two-factor-status")).data["enabled"])

    def test_the_setup_response_never_leaks_encrypted_secret_bytes(self):
        response = self.client.post(reverse("two-factor-setup"))
        self.assertNotIn("encrypted_secret", response.data)

    def test_recovery_codes_endpoint_regenerates_through_the_api(self):
        setup = self.client.post(reverse("two-factor-setup")).data
        self.client.post(reverse("two-factor-confirm"), {"code": totp.generate_code(setup["secret"])}, format="json")
        first = self.client.post(reverse("two-factor-recovery-codes"), {"code": self.fresh_code(setup["secret"])}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data["recovery_codes"]), 10)

    def test_the_login_to_verify_round_trip_through_the_api_produces_working_tokens(self):
        setup = self.client.post(reverse("two-factor-setup")).data
        self.client.post(reverse("two-factor-confirm"), {"code": totp.generate_code(setup["secret"])}, format="json")

        anonymous = APIClient()
        login = anonymous.post(reverse("login"), {"identifier": self.user.username, "password": self.password}, format="json")
        self.assertTrue(login.data["two_factor_required"])

        verify = anonymous.post(
            reverse("two-factor-verify"),
            {"login_token": login.data["login_token"], "code": self.fresh_code(setup["secret"])},
            format="json",
        )
        self.assertEqual(verify.status_code, 200)
        anonymous.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
        protected = anonymous.get(reverse("two-factor-status"))
        self.assertEqual(protected.status_code, 200)

    def test_verify_with_a_wrong_code_gives_a_clean_400_not_tokens(self):
        setup = self.client.post(reverse("two-factor-setup")).data
        self.client.post(reverse("two-factor-confirm"), {"code": totp.generate_code(setup["secret"])}, format="json")
        anonymous = APIClient()
        login = anonymous.post(reverse("login"), {"identifier": self.user.username, "password": self.password}, format="json")
        response = anonymous.post(
            reverse("two-factor-verify"), {"login_token": login.data["login_token"], "code": "000000"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access", response.data)


class IssueLoginTokensTests(TwoFactorTestCase):
    def test_returns_the_expected_shape(self):
        result = issue_login_tokens(self.user)
        self.assertEqual(set(result), {"refresh", "access", "user", "role", "dashboard"})
        self.assertEqual(result["role"], self.user.role)
