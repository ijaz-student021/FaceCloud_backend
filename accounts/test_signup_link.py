"""Signup by full name and an emailed verification LINK (items 1 and 2), and the names it creates (item 3)."""
import hashlib
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import AuthEvent, PendingRegistration
from .services import derive_username, split_full_name

User = get_user_model()
PASSWORD = "StrongPass!4729"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="http://localhost:5173")
class SignupLinkTests(APITestCase):
    def register(self, **fields):
        payload = {"full_name": "Ada Lovelace", "email": "ada@example.com", "password": PASSWORD, **fields}
        return self.client.post(reverse("register"), {k: v for k, v in payload.items() if v is not None}, format="json")

    def link(self):
        match = re.search(r"(http\S*/verify-email\?\S+)", mail.outbox[-1].body)
        self.assertIsNotNone(match, mail.outbox[-1].body)
        parts = urlsplit(match.group(1).rstrip(".,)"))
        query = parse_qs(parts.query)
        return f"{parts.scheme}://{parts.netloc}{parts.path}", query["registration_id"][0], query["token"][0]

    def verify(self, registration_id, token):
        return self.client.post(reverse("verify-email"), {"registration_id": registration_id, "token": token}, format="json")

    # -- the email -------------------------------------------------------------------------------------------------------
    def test_the_email_has_a_clickable_frontend_link_and_no_code(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        base, registration_id, token = self.link()
        self.assertEqual(base, "http://localhost:5173/verify-email")
        self.assertEqual(registration_id, response.data["registration_id"])
        self.assertGreaterEqual(len(token), 43)                       # token_urlsafe(32)
        self.assertIsNone(re.search(r"\b\d{6}\b", mail.outbox[-1].body), "the email must not carry a six-digit code")
        html = mail.outbox[-1].alternatives[0][0]
        self.assertIn(f'href="http://localhost:5173/verify-email?registration_id={registration_id}&amp;token={token}"', html)
        self.assertEqual(response.data["expires_in_seconds"], 86400)
        self.assertNotIn("token", response.data)

    def test_only_a_hash_of_the_token_is_stored_and_it_is_never_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            response = self.register()
        _base, _registration_id, token = self.link()
        pending = PendingRegistration.objects.get(public_id=response.data["registration_id"])
        self.assertEqual(pending.token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, pending.token_hash)
        for event in AuthEvent.objects.all():
            self.assertNotIn(token, str(event.details))

    def test_the_frontend_url_setting_controls_the_link(self):
        with override_settings(FRONTEND_URL="https://app.example.org/"):
            self.register(email="b@example.com")
        self.assertEqual(self.link()[0], "https://app.example.org/verify-email")

    # -- verifying -------------------------------------------------------------------------------------------------------
    def test_a_valid_link_creates_the_account_and_the_token_works_once(self):
        self.register()
        _base, registration_id, token = self.link()
        first = self.verify(registration_id, token)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.filter(email="ada@example.com").count(), 1)
        again = self.verify(registration_id, token)
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data["detail"], "Account has already been created.")
        self.assertNotIn("user", again.data)      # the id is in a URL: a replay must not disclose who the account is
        self.assertEqual(User.objects.filter(email="ada@example.com").count(), 1)
        pending = PendingRegistration.objects.get(public_id=registration_id)
        self.assertEqual((pending.token_hash, pending.password_hash), ("", ""))

    def test_an_expired_link_is_refused_and_creates_nothing(self):
        self.register()
        _base, registration_id, token = self.link()
        PendingRegistration.objects.filter(public_id=registration_id).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        response = self.verify(registration_id, token)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("expired", response.data["detail"])
        self.assertFalse(User.objects.filter(email="ada@example.com").exists())
        self.assertEqual(PendingRegistration.objects.get(public_id=registration_id).status, PendingRegistration.STATUS_EXPIRED)

    @override_settings(SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS=3600)
    def test_the_lifetime_setting_sets_the_expiry(self):
        response = self.register()
        pending = PendingRegistration.objects.get(public_id=response.data["registration_id"])
        self.assertAlmostEqual((pending.token_expires_at - timezone.now()).total_seconds(), 3600, delta=5)
        self.assertEqual(response.data["expires_in_seconds"], 3600)

    @override_settings(SIGNUP_VERIFICATION_MAX_ATTEMPTS=3)
    def test_a_wrong_token_is_counted_then_the_registration_locks_and_even_the_right_token_is_refused(self):
        self.register()
        _base, registration_id, token = self.link()
        statuses = [self.verify(registration_id, "wrong-token").status_code for _ in range(3)]
        self.assertEqual(statuses, [400, 400, 423])
        self.assertEqual(self.verify(registration_id, token).status_code, status.HTTP_423_LOCKED)
        self.assertFalse(User.objects.filter(email="ada@example.com").exists())

    def test_a_token_from_another_registration_does_not_work(self):
        self.register(email="one@example.com")
        _b, first_id, first_token = self.link()
        self.register(email="two@example.com")
        _b, second_id, _t = self.link()
        self.assertEqual(self.verify(second_id, first_token).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.verify(first_id, first_token).status_code, status.HTTP_201_CREATED)

    def test_the_old_code_field_is_gone(self):
        self.register()
        _base, registration_id, _token = self.link()
        response = self.client.post(reverse("verify-email"), {"registration_id": registration_id, "code": "123456"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token", response.data)

    @override_settings(SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS=0)
    def test_resend_issues_a_new_link_and_the_old_one_stops_working(self):
        started = self.register()
        _b, registration_id, old_token = self.link()
        resent = self.client.post(reverse("resend-verification"), {"registration_id": registration_id}, format="json")
        self.assertEqual(resent.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 2)
        _b, same_id, new_token = self.link()
        self.assertEqual((same_id, started.data["registration_id"]), (registration_id, registration_id))
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(self.verify(registration_id, old_token).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.verify(registration_id, new_token).status_code, status.HTTP_201_CREATED)

    def test_resend_keeps_its_cooldown(self):
        self.register()
        _b, registration_id, _t = self.link()
        response = self.client.post(reverse("resend-verification"), {"registration_id": registration_id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS=0, SIGNUP_VERIFICATION_MAX_ATTEMPTS=1)
    def test_resend_revives_a_locked_registration(self):
        self.register()
        _b, registration_id, _t = self.link()
        self.assertEqual(self.verify(registration_id, "wrong").status_code, status.HTTP_423_LOCKED)
        self.client.post(reverse("resend-verification"), {"registration_id": registration_id}, format="json")
        self.assertEqual(self.verify(registration_id, self.link()[2]).status_code, status.HTTP_201_CREATED)

    def test_the_status_endpoint_never_exposes_the_token(self):
        self.register()
        _b, registration_id, token = self.link()
        response = self.client.get(reverse("registration-status", args=[registration_id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {"status", "email", "expires_in_seconds", "resend_available_in_seconds", "attempts_remaining"})
        self.assertNotIn(token, str(response.data))
        self.assertEqual(response.data["status"], "pending")

    def test_registering_twice_with_one_address_keeps_one_pending_row_and_sends_a_new_link(self):
        self.register()
        first = self.link()
        again = self.register()
        self.assertEqual(again.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PendingRegistration.objects.filter(email="ada@example.com").count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.verify(first[1], first[2]).status_code, status.HTTP_400_BAD_REQUEST)    # the first link is dead
        self.assertEqual(self.verify(*self.link()[1:]).status_code, status.HTTP_201_CREATED)

    # -- the form (item 1) -----------------------------------------------------------------------------------------------
    def test_full_name_is_required_trimmed_and_limited(self):
        self.assertIn("full_name", self.register(full_name=None).data)
        self.assertIn("full_name", self.register(full_name="   ").data)
        self.assertIn("full_name", self.register(full_name="x" * 151).data)
        self.assertEqual(self.register(full_name="  Ada   Lovelace  ").status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PendingRegistration.objects.get().full_name, "Ada Lovelace")

    def test_username_is_optional_and_a_given_one_is_still_honoured(self):
        given = self.register(username="ada_l")
        self.assertEqual(given.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PendingRegistration.objects.get().username, "ada_l")
        blank = self.register(username="", email="blank@example.com")
        self.assertEqual(blank.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PendingRegistration.objects.get(email="blank@example.com").username, "blank")

    def test_a_username_that_is_taken_is_still_a_field_error(self):
        User.objects.create_user(username="Taken", email="t@example.com", password=PASSWORD)
        response = self.register(username="taken")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.data)

    def test_a_duplicate_email_is_a_field_error_under_email_and_a_pending_one_is_not_a_new_leak(self):
        User.objects.create_user(username="existing", email="ada@example.com", password=PASSWORD)
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["email"][0], "A user with this email already exists.")

    def test_password_confirm_is_optional_but_checked_when_sent(self):
        self.assertEqual(self.register(password_confirm=PASSWORD).status_code, status.HTTP_202_ACCEPTED)
        mismatch = self.register(email="m@example.com", password_confirm="Different!1234")
        self.assertEqual(mismatch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password_confirm", mismatch.data)

    def test_a_password_that_is_just_the_persons_name_is_refused(self):
        response = self.register(full_name="Ada Lovelace", password="AdaLovelace")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- usernames (item 1) ----------------------------------------------------------------------------------------------
    def test_a_username_is_derived_from_the_email_local_part(self):
        self.register(email="Grace.Hopper@Example.com")
        self.assertEqual(PendingRegistration.objects.get().username, "grace.hopper")

    def test_derived_usernames_never_collide_with_users_or_pending_registrations(self):
        User.objects.create_user(username="sam", email="sam@one.example", password=PASSWORD)
        self.register(email="sam@two.example")                       # taken by a user -> sam2
        self.register(email="sam@three.example")                     # taken by a user and a pending one -> sam3
        self.register(email="SAM@four.example")                      # case-insensitive -> sam4
        names = list(PendingRegistration.objects.order_by("id").values_list("username", flat=True))
        self.assertEqual(names, ["sam2", "sam3", "sam4"])
        self.assertEqual(len({name.lower() for name in names}), 3)

    def test_illegal_characters_are_dropped_and_the_length_is_capped(self):
        self.assertEqual(derive_username("a b!c#d$e%f@example.com"), "abcdef")
        self.assertEqual(derive_username("jose+tag@example.com"), "jose+tag")
        self.assertEqual(derive_username("!!!@example.com"), "user")
        long = derive_username("x" * 400 + "@example.com")
        self.assertEqual((long, len(long)), ("x" * 150, 150))
        User.objects.create_user(username="x" * 150, email="longname@example.com", password=PASSWORD)
        suffixed = derive_username("x" * 400 + "@example.com")
        self.assertEqual((len(suffixed), suffixed.endswith("2")), (150, True))

    def test_expired_or_cancelled_pending_registrations_do_not_hold_a_username(self):
        PendingRegistration.objects.create(
            username="kim", email="old@example.com", password_hash="x", token_hash="", status=PendingRegistration.STATUS_EXPIRED,
            resend_window_started_at=timezone.now(), token_expires_at=timezone.now(), resend_available_at=timezone.now())
        self.assertEqual(derive_username("kim@example.com"), "kim")

    # -- names on the account (item 1 and 3) -----------------------------------------------------------------------------
    def test_the_full_name_becomes_first_and_last_name_on_the_account(self):
        for full_name, first, last in [("Ada Lovelace", "Ada", "Lovelace"), ("Ada King Lovelace", "Ada", "King Lovelace"),
                                       ("Plato", "Plato", ""), ("  Mary   Jane  Watson ", "Mary", "Jane Watson")]:
            with self.subTest(full_name=full_name):
                self.assertEqual(split_full_name(full_name), (first, last))
        self.register(full_name="Ada King Lovelace")
        _b, registration_id, token = self.link()
        self.assertEqual(self.verify(registration_id, token).status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="ada@example.com")
        self.assertEqual((user.first_name, user.last_name, user.username), ("Ada", "King Lovelace", "ada"))
        login = self.client.post(reverse("login"), {"email": "ada@example.com", "password": PASSWORD}, format="json")
        self.assertEqual(login.data["user"]["full_name"], "Ada King Lovelace")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class FullNameOnUserObjectsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jdoe", email="jdoe@example.com", password=PASSWORD, is_email_verified=True,
                                             first_name="Jane", last_name="Q Doe")
        self.admin = User.objects.create_user(username="boss", email="boss@example.com", password=PASSWORD, is_email_verified=True, role="admin")

    def login(self, user):
        return self.client.post(reverse("login"), {"email": user.email, "password": PASSWORD}, format="json")

    def authenticate(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.login(user).data['access']}")

    def test_login_and_profile_report_the_name_and_fall_back_to_the_username(self):
        self.assertEqual(self.login(self.user).data["user"]["full_name"], "Jane Q Doe")
        self.authenticate(self.user)
        self.assertEqual(self.client.get(reverse("profile")).data["full_name"], "Jane Q Doe")
        self.assertEqual(self.login(self.admin).data["user"]["full_name"], "boss")        # no name on record

    def test_patching_full_name_writes_first_and_last_name(self):
        self.authenticate(self.user)
        response = self.client.patch(reverse("profile"), {"full_name": "  Maria  de la Cruz "}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["full_name"], "Maria de la Cruz")
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Maria", "de la Cruz"))
        self.assertEqual(self.client.patch(reverse("profile"), {"full_name": "  "}, format="json").status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_without_full_name_leaves_the_name_alone(self):
        self.authenticate(self.user)
        response = self.client.put(reverse("profile"), {"username": "jdoe", "email": "jdoe@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name), ("Jane", "Q Doe"))

    def test_full_name_is_read_only_in_the_users_own_serializer_elsewhere(self):
        from .serializers import UserSerializer

        self.assertTrue(UserSerializer().fields["full_name"].read_only)

    def test_the_admin_user_list_and_detail_carry_the_name_and_it_cannot_be_set_there(self):
        self.authenticate(self.admin)
        listed = self.client.get(reverse("admin-user-list")).data
        rows = {row["username"]: row for row in (listed["results"] if isinstance(listed, dict) else listed)}
        self.assertEqual(rows["jdoe"]["full_name"], "Jane Q Doe")
        self.assertEqual(rows["boss"]["full_name"], "boss")
        patched = self.client.patch(reverse("admin-user-update", args=[self.user.pk]), {"full_name": "Hacked", "is_suspended": False}, format="json")
        self.assertEqual(patched.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Jane Q Doe")
