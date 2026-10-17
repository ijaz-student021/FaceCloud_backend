import hashlib
import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import AuthEvent, PendingRegistration
from enhancement.models import ImageEnhancementResult
from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch
from validation.models import FaceValidationResult
from quality_control.models import ImageQualityDecision

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AuthenticationTests(APITestCase):
    password = "StrongPass!4729"

    def create_user(self, username="alice", email="alice@example.com", **kwargs):
        defaults = {"is_email_verified": True}
        defaults.update(kwargs)
        return User.objects.create_user(
            username=username,
            email=email,
            password=self.password,
            **defaults,
        )

    def login(self, user, password=None):
        return self.client.post(
            reverse("login"),
            {"username": user.username, "password": password or self.password},
            format="json",
        )

    def authenticate(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return token

    def latest_verification_link(self):
        """``(registration_id, token)`` from the newest email's link."""
        match = re.search(r"/verify-email\?registration_id=([0-9a-f-]{36})&token=([A-Za-z0-9_-]+)", mail.outbox[-1].body)
        self.assertIsNotNone(match, "Verification email did not contain a verification link.")
        return match.group(1), match.group(2)

    def latest_verification_token(self):
        return self.latest_verification_link()[1]

    def test_registration_normalizes_email_and_sends_verification(self):
        response = self.client.post(
            reverse("register"),
            {"username": "NewUser", "full_name": "New User", "email": "  NewUser@Example.COM ", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(User.objects.filter(username="NewUser").exists())
        pending = PendingRegistration.objects.get(
            public_id=response.data["registration_id"]
        )
        self.assertEqual(pending.email, "newuser@example.com")
        registration_id, token = self.latest_verification_link()
        self.assertEqual(registration_id, str(pending.public_id))
        self.assertTrue(check_password(self.password, pending.password_hash))
        self.assertEqual(pending.token_hash, hashlib.sha256(token.encode()).hexdigest())   # only a hash is stored
        self.assertNotIn(token, pending.token_hash)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            AuthEvent.objects.filter(
                user=None,
                event="registration_pending",
                identifier="newuser@example.com",
            ).exists()
        )

    def test_dashboard_statistics_and_activity_are_owner_scoped(self):
        user = self.create_user(username="dashboard", email="dashboard@example.com")
        other = self.create_user(username="dashboard-other", email="dashboard-other@example.com")
        batch = UploadBatch.objects.create(owner=user, upload_type="single")
        image = ImageUpload.objects.create(
            owner=user, batch=batch, original_name="dashboard.jpg",
            status="uploaded", file_size=100,
        )
        preprocessing = ImagePreprocessingResult.objects.create(
            owner=user, image=image, status="completed", processed_file_size=80,
        )
        ImageEnhancementResult.objects.create(
            owner=user, image=image, preprocessing_result=preprocessing,
            status="completed", output_file_size=70,
        )
        FaceValidationResult.objects.create(
            owner=user, image=image, preprocessing_result=preprocessing,
            decision="accepted", face_count=1,
        )
        ImageQualityDecision.objects.create(
            owner=user, image=image, preprocessing_result=preprocessing,
            face_validation=image.face_validation, status="completed",
            decision="accepted", overall_score=82,
        )
        other_batch = UploadBatch.objects.create(owner=other, upload_type="single")
        ImageUpload.objects.create(
            owner=other, batch=other_batch, original_name="private.jpg", status="rejected",
        )
        self.authenticate(user)

        response = self.client.get(reverse("user-dashboard"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["summary"]["total_images"], 1)
        self.assertEqual(response.data["summary"]["enhanced_images"], 1)
        self.assertEqual(response.data["summary"]["rejected_images"], 0)
        self.assertEqual(response.data["summary"]["storage_bytes"], 250)
        self.assertEqual(response.data["summary"]["average_quality_score"], 82)
        self.assertEqual(len(response.data["uploads_last_7_days"]), 7)
        self.assertEqual(response.data["recent_uploads"][0]["filename"], "dashboard.jpg")
        self.assertNotContains(response, "private.jpg")

    def test_registration_rejects_weak_password_and_duplicate_email_case_insensitively(self):
        self.create_user()
        weak = self.client.post(
            reverse("register"),
            {"username": "weak", "full_name": "Weak Password", "email": "weak@example.com", "password": "password"},
            format="json",
        )
        duplicate = self.client.post(
            reverse("register"),
            {"username": "other", "full_name": "Other Person", "email": "ALICE@EXAMPLE.COM", "password": self.password},
            format="json",
        )
        self.assertEqual(weak.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", duplicate.data)   # a field error under email, as before

    def test_email_verification_enables_login(self):
        started = self.client.post(
            reverse("register"),
            {
                "username": "pending-alice",
                "full_name": "Pending Alice",
                "email": "pending-alice@example.com",
                "password": self.password,
            },
            format="json",
        )
        self.assertEqual(started.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(User.objects.filter(username="pending-alice").exists())

        verified = self.client.post(
            reverse("verify-email"),
            {
                "registration_id": started.data["registration_id"],
                "token": self.latest_verification_token(),
            },
            format="json",
        )

        self.assertEqual(verified.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="pending-alice")
        self.assertTrue(user.is_email_verified)
        self.assertEqual(self.login(user).status_code, status.HTTP_200_OK)
        pending = PendingRegistration.objects.get(
            public_id=started.data["registration_id"]
        )
        self.assertEqual(pending.status, PendingRegistration.STATUS_CONSUMED)
        self.assertEqual(pending.created_user, user)
        self.assertEqual(pending.password_hash, "")
        self.assertEqual(pending.token_hash, "")
        self.assertTrue(
            AuthEvent.objects.filter(user=user, event="registered").exists()
        )
        self.assertTrue(
            AuthEvent.objects.filter(user=user, event="email_verified").exists()
        )

    def test_verification_user_creation_and_events_are_atomic(self):
        started = self.client.post(
            reverse("register"),
            {
                "username": "atomic-user",
                "full_name": "Atomic User",
                "email": "atomic@example.com",
                "password": self.password,
            },
            format="json",
        )
        registration_id = started.data["registration_id"]
        token = self.latest_verification_token()

        with mock.patch(
            "accounts.views.record_auth_event",
            side_effect=RuntimeError("simulated audit failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("verify-email"),
                    {"registration_id": registration_id, "token": token},
                    format="json",
                )

        self.assertFalse(User.objects.filter(username="atomic-user").exists())
        pending = PendingRegistration.objects.get(public_id=registration_id)
        self.assertEqual(pending.status, PendingRegistration.STATUS_PENDING)
        self.assertIsNone(pending.created_user)

    @override_settings(SIGNUP_VERIFICATION_MAX_ATTEMPTS=2)
    def test_verification_locks_after_configured_failed_attempts(self):
        started = self.client.post(
            reverse("register"),
            {
                "username": "locked-user",
                "full_name": "Locked User",
                "email": "locked@example.com",
                "password": self.password,
            },
            format="json",
        )
        payload = {
            "registration_id": started.data["registration_id"],
            "token": "not-the-real-token",
        }
        first = self.client.post(reverse("verify-email"), payload, format="json")
        second = self.client.post(reverse("verify-email"), payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(first.data["attempts_remaining"], 1)
        self.assertEqual(second.status_code, status.HTTP_423_LOCKED)
        self.assertEqual(second.data["attempts_remaining"], 0)
        self.assertFalse(User.objects.filter(username="locked-user").exists())

    @override_settings(SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS=0)
    def test_resend_invalidates_previous_link(self):
        started = self.client.post(
            reverse("register"),
            {
                "username": "resend-user",
                "full_name": "Resend User",
                "email": "resend@example.com",
                "password": self.password,
            },
            format="json",
        )
        old_token = self.latest_verification_token()
        resent = self.client.post(
            reverse("resend-verification"),
            {"registration_id": started.data["registration_id"]},
            format="json",
        )
        new_token = self.latest_verification_token()

        self.assertEqual(resent.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 2)
        self.assertNotEqual(old_token, new_token)
        rejected = self.client.post(
            reverse("verify-email"),
            {"registration_id": started.data["registration_id"], "token": old_token},
            format="json",
        )
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(username="resend-user").exists())

    def test_login_accepts_email_and_returns_user_dashboard(self):
        user = self.create_user()
        response = self.client.post(
            reverse("login"),
            {"email": "ALICE@EXAMPLE.COM", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["role"], "user")
        self.assertEqual(response.data["dashboard"], "user")
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_returns_dashboard_from_trusted_role(self):
        moderator = self.create_user("moderator", "moderator@example.com", role="moderator")
        admin = self.create_user("adminuser", "admin@example.com", role="admin")
        moderator_response = self.client.post(
            reverse("login"),
            {"identifier": moderator.username, "password": self.password},
            format="json",
        )
        admin_response = self.client.post(
            reverse("login"),
            {"identifier": admin.email, "password": self.password},
            format="json",
        )
        self.assertEqual(moderator_response.status_code, status.HTTP_200_OK)
        self.assertEqual(moderator_response.data["dashboard"], "moderator")
        self.assertEqual(admin_response.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_response.data["dashboard"], "admin")

    def test_suspended_user_cannot_login_or_refresh(self):
        user = self.create_user(is_suspended=True)
        self.assertEqual(self.login(user).status_code, status.HTTP_400_BAD_REQUEST)
        refresh = RefreshToken.for_user(user)
        refresh["token_version"] = user.token_version
        response = self.client.post(reverse("refresh"), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_profile_requires_authentication_and_can_be_updated(self):
        user = self.create_user()
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)
        self.authenticate(user)
        response = self.client.patch(reverse("profile"), {"username": "alice2"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.username, "alice2")

    def test_email_change_requires_reverification_and_revokes_token(self):
        user = self.create_user()
        self.authenticate(user)
        response = self.client.patch(
            reverse("profile"),
            {"email": "new@example.com", "current_password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertFalse(user.is_email_verified)
        self.assertEqual(user.token_version, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_email_change_needs_the_current_password(self):
        user = self.create_user()
        self.authenticate(user)
        for payload in ({"email": "new@example.com"}, {"email": "new@example.com", "current_password": "wrong"}):
            response = self.client.patch(reverse("profile"), payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("current_password", response.data)
        user.refresh_from_db()
        self.assertEqual(user.email, "alice@example.com")
        self.assertTrue(user.is_email_verified)
        self.assertEqual(len(mail.outbox), 0)

    def test_current_password_is_never_echoed_or_required_for_other_edits(self):
        user = self.create_user()
        self.authenticate(user)
        response = self.client.patch(
            reverse("profile"), {"username": "alice3", "current_password": self.password}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("current_password", response.data)

    def test_logout_rejects_a_refresh_token_owned_by_someone_else(self):
        alice = self.create_user()
        bob = self.create_user(username="bob", email="bob@example.com")
        bobs_refresh = str(RefreshToken.for_user(bob))
        self.authenticate(alice)
        response = self.client.post(reverse("logout"), {"refresh": bobs_refresh}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Bob's token still works.
        self.client.credentials()
        refreshed = self.client.post(reverse("refresh"), {"refresh": bobs_refresh}, format="json")
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)

    def test_spoofed_forwarded_for_header_does_not_choose_the_recorded_ip(self):
        user = self.create_user()
        self.client.post(
            reverse("login"),
            {"username": user.username, "password": self.password},
            format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.9",
            REMOTE_ADDR="198.51.100.4",
        )
        self.assertEqual(AuthEvent.objects.get(event="login").ip_address, "198.51.100.4")

    def test_forwarded_for_is_honoured_only_for_configured_proxies(self):
        user = self.create_user()
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}):
            self.client.post(
                reverse("login"),
                {"username": user.username, "password": self.password},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.9, 192.0.2.7",
                REMOTE_ADDR="198.51.100.4",
            )
        self.assertEqual(AuthEvent.objects.get(event="login").ip_address, "192.0.2.7")

    def test_unknown_account_and_wrong_password_look_identical(self):
        user = self.create_user()
        unknown = self.client.post(
            reverse("login"), {"username": "nobody", "password": "Whatever!1234"}, format="json"
        )
        wrong = self.login(user, "Whatever!1234")
        self.assertEqual(unknown.status_code, wrong.status_code)
        self.assertEqual(unknown.data, wrong.data)

    def test_change_password_checks_current_password_and_revokes_tokens(self):
        user = self.create_user()
        self.authenticate(user)
        wrong = self.client.post(
            reverse("change-password"),
            {"current_password": "wrong", "new_password": "AnotherStrong!8392"},
            format="json",
        )
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        changed = self.client.post(
            reverse("change-password"),
            {"current_password": self.password, "new_password": "AnotherStrong!8392"},
            format="json",
        )
        self.assertEqual(changed.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.check_password("AnotherStrong!8392"))
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_password_reset_is_non_enumerating_and_resets_password(self):
        user = self.create_user()
        request_response = self.client.post(reverse("password-reset"), {"email": user.email}, format="json")
        missing_response = self.client.post(reverse("password-reset"), {"email": "missing@example.com"}, format="json")
        self.assertEqual(request_response.status_code, status.HTTP_200_OK)
        self.assertEqual(missing_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        confirmed = self.client.post(
            reverse("password-reset-confirm"),
            {"uidb64": uid, "token": token, "new_password": "ResetStrong!5931"},
            format="json",
        )
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.check_password("ResetStrong!5931"))

    def test_logout_blacklists_refresh_token(self):
        user = self.create_user()
        login = self.login(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        logged_out = self.client.post(reverse("logout"), {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(logged_out.status_code, status.HTTP_204_NO_CONTENT)
        refreshed = self.client.post(reverse("refresh"), {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_all_revokes_existing_access_token(self):
        user = self.create_user()
        self.authenticate(user)
        self.assertEqual(self.client.post(reverse("logout-all")).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_delete_account_requires_password(self):
        user = self.create_user()
        self.authenticate(user)
        wrong = self.client.delete(reverse("delete-account"), {"password": "wrong"}, format="json")
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        deleted = self.client.delete(reverse("delete-account"), {"password": self.password}, format="json")
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertTrue(AuthEvent.objects.filter(event="account_deleted", identifier=user.email).exists())

    def test_only_admin_can_manage_roles_and_suspension(self):
        regular = self.create_user()
        target = self.create_user("bob", "bob@example.com")
        self.authenticate(regular)
        denied = self.client.patch(
            reverse("admin-user-update", kwargs={"pk": target.pk}),
            {"role": "moderator"},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        admin = self.create_user("admin", "admin@example.com", role="admin")
        self.authenticate(admin)
        allowed = self.client.patch(
            reverse("admin-user-update", kwargs={"pk": target.pk}),
            {"role": "moderator", "is_suspended": True, "suspension_reason": "Policy violation"},
            format="json",
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        self.assertEqual(target.role, "moderator")
        self.assertTrue(target.is_suspended)
        self.assertEqual(target.suspension_reason, "Policy violation")
