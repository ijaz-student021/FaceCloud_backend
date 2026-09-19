"""The error shapes documented in docs/API_ERRORS.md, checked against the real API."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()
PASSWORD = "ForestRiver!4821"


class ErrorContractTests(TestCase):
    def make_user(self, name, **extra):
        defaults = {"is_email_verified": True}
        defaults.update(extra)
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password=PASSWORD, **defaults,
        )

    def bearer(self, user):
        token = RefreshToken.for_user(user)
        token["role"] = user.role
        token["token_version"] = user.token_version
        return f"Bearer {token.access_token}"

    def test_missing_credentials_is_401_with_detail(self):
        response = APIClient().get(reverse("profile"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("detail", response.data)

    def test_garbage_token_is_401_with_a_code(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        response = client.get(reverse("profile"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], "token_not_valid")

    def test_revoked_token_is_401(self):
        user = self.make_user("revoked")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=self.bearer(user))
        User.objects.filter(pk=user.pk).update(token_version=5)
        response = client.get(reverse("profile"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "This token has been revoked.")

    def test_suspended_account_is_401(self):
        user = self.make_user("suspended")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=self.bearer(user))
        User.objects.filter(pk=user.pk).update(is_suspended=True)
        response = client.get(reverse("profile"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "This account is suspended.")

    def test_wrong_role_is_403_with_detail(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=self.bearer(self.make_user("plain")))
        response = client.get(reverse("operations:queue-status"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Administrator access is required.")

    def test_someone_elses_image_is_404_not_403(self):
        """Ownership failures look identical to a missing record."""
        owner = self.make_user("owner")
        stranger = self.make_user("stranger")
        from uploads.models import ImageUpload, UploadBatch

        batch = UploadBatch.objects.create(owner=owner, upload_type="single")
        image = ImageUpload.objects.create(
            owner=owner, batch=batch, original_name="a.png", status="uploaded",
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=self.bearer(stranger))
        response = client.get(reverse("image-upload-detail", kwargs={"pk": image.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.data)

    def test_validation_errors_are_a_field_to_messages_map(self):
        response = APIClient().post(
            reverse("register"), {"username": "x", "email": "not-an-email", "password": "short"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsInstance(response.data["email"], list)

    def test_login_failures_share_one_message_for_credentials(self):
        self.make_user("real")
        client = APIClient()
        unknown = client.post(reverse("login"), {"username": "ghost", "password": PASSWORD}, format="json")
        wrong = client.post(reverse("login"), {"username": "real", "password": "Nope!123456"}, format="json")
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.data, wrong.data)
        # Errors raised inside a serializer keep DRF's list form: {"detail": ["..."]}.
        self.assertEqual(unknown.data["detail"], ["Invalid username/email or password."])
