from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import AuthEvent
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

    def test_registration_normalizes_email_and_sends_verification(self):
        response = self.client.post(
            reverse("register"),
            {"username": "NewUser", "email": "  NewUser@Example.COM ", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="NewUser")
        self.assertEqual(user.email, "newuser@example.com")
        self.assertFalse(user.is_email_verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(AuthEvent.objects.filter(user=user, event="registered").exists())

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
            {"username": "weak", "email": "weak@example.com", "password": "password"},
            format="json",
        )
        duplicate = self.client.post(
            reverse("register"),
            {"username": "other", "email": "ALICE@EXAMPLE.COM", "password": self.password},
            format="json",
        )
        self.assertEqual(weak.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)

    def test_email_verification_enables_login(self):
        user = self.create_user(is_email_verified=False)
        denied = self.login(user)
        self.assertEqual(denied.status_code, status.HTTP_400_BAD_REQUEST)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        verified = self.client.get(reverse("verify-email", kwargs={"uidb64": uid, "token": token}))
        self.assertEqual(verified.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_email_verified)
        self.assertEqual(self.login(user).status_code, status.HTTP_200_OK)

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
        response = self.client.patch(reverse("profile"), {"email": "new@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertFalse(user.is_email_verified)
        self.assertEqual(user.token_version, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.client.get(reverse("profile")).status_code, status.HTTP_401_UNAUTHORIZED)

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
