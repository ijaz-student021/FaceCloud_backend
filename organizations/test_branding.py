"""Organization branding: a display name, an accent colour and a logo the owner chooses and every member sees."""
import io
import struct
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User

from .branding import LOGO_MAX_BYTES, LOGO_MAX_EDGE
from .models import Organization, OrganizationAuditEvent, OrganizationMembership
from .services import create_organization, create_personal_organization

PASSWORD = "ForestRiver!4821"
Role = OrganizationMembership.Role
Action = OrganizationAuditEvent.Action


def png_bytes(size=(64, 32), colour=(30, 120, 200, 255), mode="RGBA"):
    buffer = io.BytesIO()
    Image.new(mode, size, colour if mode == "RGBA" else colour[:3]).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_bytes(size=(2000, 1000), with_gps=True):
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (200, 60, 60))
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"                       # Make
    if with_gps:
        exif[0x8825] = {1: "N", 2: (31.0, 33.0, 0.0), 3: "E", 4: (74.0, 21.0, 0.0)}      # a GPS block
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def upload(content, name="logo.png", content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class Fixtures:
    def setUp(self):
        cache.clear()
        self.admin = self.person("br-admin", role="admin")
        self.owner = self.person("br-owner")
        self.moderator = self.person("br-mod", role="moderator")
        self.member = self.person("br-member")
        self.outsider = self.person("br-outsider")
        for user in (self.owner, self.moderator, self.member, self.outsider):
            create_personal_organization(user)
        self.organization = create_organization(name="Acme", owner=self.owner, plan=Organization.Plan.TEAM)
        for user, role in ((self.moderator, Role.MODERATOR), (self.member, Role.MEMBER)):
            OrganizationMembership.objects.create(organization=self.organization, user=user, role=role, status="active")
        self.url = reverse("organizations:organization-detail", args=[self.organization.pk])
        self.logo_url = reverse("organizations:organization-logo", args=[self.organization.pk])

    def person(self, name, **extra):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD, is_email_verified=True, **extra)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def patch(self, body, user=None):
        return self.client_for(user or self.owner).patch(self.url, body, format="json")

    def put_logo(self, content, user=None, **kwargs):
        return self.client_for(user or self.owner).put(self.logo_url, {"logo": upload(content, **kwargs)}, format="multipart")

    def fresh(self):
        return Organization.objects.get(pk=self.organization.pk)

    def stored_image(self):
        with default_storage.open(self.fresh().logo.name, "rb") as handle:
            data = handle.read()
        return data, Image.open(io.BytesIO(data))


class NameAndAccentTests(Fixtures, TestCase):
    def test_nothing_is_set_by_default_and_every_member_sees_the_fields(self):
        for user in (self.owner, self.moderator, self.member):
            body = self.client_for(user).get(self.url).json()
            self.assertEqual((body["brand_name"], body["brand_accent"], body["logo_url"]), ("", "", None), user.username)
        rows = self.client_for(self.member).get(reverse("organizations:organization-list")).json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertTrue(all({"brand_name", "brand_accent", "logo_url"} <= set(row) for row in rows))

    def test_the_owner_sets_the_brand_name_and_accent_and_everyone_in_the_organization_sees_them(self):
        response = self.patch({"brand_name": "  Acme Studios ", "brand_accent": "#1A73E8"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()["brand_name"], response.json()["brand_accent"]), ("Acme Studios", "#1a73e8"))
        for user in (self.moderator, self.member):
            body = self.client_for(user).get(self.url).json()
            self.assertEqual((body["brand_name"], body["brand_accent"], body["name"]), ("Acme Studios", "#1a73e8", "Acme"), user.username)

    def test_a_short_hex_colour_is_expanded_and_blank_clears_both(self):
        self.assertEqual(self.patch({"brand_accent": "#abc"}).json()["brand_accent"], "#aabbcc")
        self.patch({"brand_name": "Acme Studios"})
        cleared = self.patch({"brand_name": "", "brand_accent": ""}).json()
        self.assertEqual((cleared["brand_name"], cleared["brand_accent"]), ("", ""))

    def test_bad_values_are_refused_and_nothing_is_changed(self):
        bad_accents = ("red", "#12345", "#gggggg", "1a73e8", "#1a73e8ff", "#1a73e8; background:url(x)", "rgb(1,2,3)")
        for accent in bad_accents:
            with self.subTest(accent=accent):
                response = self.patch({"brand_name": "Should not stick", "brand_accent": accent})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("brand_accent", response.json())
        for name in ("x" * 101, "two\nlines", "bell\x07", "zero​width"):
            with self.subTest(name=name):
                self.assertEqual(self.patch({"brand_name": name}).status_code, 400)
        organization = self.fresh()
        self.assertEqual((organization.brand_name, organization.brand_accent), ("", ""))

    def test_a_brand_name_of_exactly_the_limit_is_fine(self):
        self.assertEqual(self.patch({"brand_name": "n" * 100}).status_code, 200)

    def test_html_in_a_name_is_stored_as_text_not_interpreted(self):
        body = self.patch({"brand_name": "<b>Acme</b>"}).json()
        self.assertEqual(body["brand_name"], "<b>Acme</b>")               # the frontend escapes text; the API returns JSON and never HTML

    def test_the_change_is_audited_with_old_and_new_values(self):
        self.patch({"brand_name": "First", "brand_accent": "#111111"})
        self.patch({"brand_name": "Second"})
        events = list(OrganizationAuditEvent.objects.filter(organization=self.organization, action=Action.ORGANIZATION_UPDATED).order_by("id"))
        self.assertEqual(events[1].previous_value, {"brand_name": "First"})
        self.assertEqual(events[1].new_value, {"brand_name": "Second"})
        self.assertEqual(events[0].new_value, {"brand_name": "First", "brand_accent": "#111111"})

    def test_only_the_owner_may_change_it(self):
        for user in (self.moderator, self.member):
            self.assertEqual(self.patch({"brand_name": "Mine"}, user=user).status_code, 403, user.username)
        self.assertEqual(self.patch({"brand_name": "Mine"}, user=self.outsider).status_code, 404)
        self.assertEqual(APIClient().patch(self.url, {"brand_name": "Mine"}, format="json").status_code, 401)
        self.assertEqual(self.fresh().brand_name, "")

    def test_the_administrator_does_not_set_branding_and_a_personal_workspace_has_none(self):
        admin_url = reverse("organizations:organization-admin-detail", args=[self.organization.pk])
        self.assertEqual(self.client_for(self.admin).patch(admin_url, {"brand_name": "Admin brand"}, format="json").status_code, 400)
        personal = Organization.objects.get(is_personal=True, memberships__user=self.owner)
        response = self.client_for(self.owner).patch(reverse("organizations:organization-detail", args=[personal.pk]), {"brand_name": "Me"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fresh().brand_name, "")


class LogoTests(Fixtures, TestCase):
    def test_the_owner_uploads_a_logo_and_every_member_can_fetch_it(self):
        response = self.put_logo(png_bytes())
        self.assertEqual(response.status_code, 200, response.content)
        logo_url = response.json()["logo_url"]
        self.assertRegex(logo_url, rf"^/api/organizations/{self.organization.pk}/logo/\?v=\d+$")
        for user in (self.owner, self.moderator, self.member):
            fetched = self.client_for(user).get(self.logo_url)
            self.assertEqual(fetched.status_code, 200, user.username)
            self.assertEqual(fetched["Content-Type"], "image/png")
            self.assertEqual(fetched["Cache-Control"], "private, max-age=3600")
            self.assertEqual(fetched["X-Content-Type-Options"], "nosniff")
            self.assertEqual(Image.open(io.BytesIO(b"".join(fetched.streaming_content))).format, "PNG")
            self.assertEqual(self.client_for(user).get(self.url).json()["logo_url"], logo_url)

    def test_it_is_served_to_members_only(self):
        self.put_logo(png_bytes())
        self.assertEqual(self.client_for(self.outsider).get(self.logo_url).status_code, 404)
        self.assertEqual(APIClient().get(self.logo_url).status_code, 401)
        OrganizationMembership.objects.filter(user=self.member).update(status="suspended")
        self.assertEqual(self.client_for(self.member).get(self.logo_url).status_code, 404)
        OrganizationMembership.objects.filter(user=self.member).update(status="active")
        Organization.objects.filter(pk=self.organization.pk).update(status="suspended")
        self.assertEqual(self.client_for(self.member).get(self.logo_url).status_code, 403)

    def test_there_is_no_public_address_for_the_file(self):
        self.put_logo(png_bytes())
        name = self.fresh().logo.name
        self.assertRegex(name, rf"^users/{self.owner.pk}/organization-logos/[0-9a-f]{{32}}\.png$")
        self.assertEqual(APIClient().get(f"/media/{name}").status_code, 404)

    def test_without_a_logo_the_address_is_404(self):
        self.assertEqual(self.client_for(self.member).get(self.logo_url).status_code, 404)

    def test_only_the_owner_may_replace_or_remove_it(self):
        for user in (self.moderator, self.member):
            self.assertEqual(self.put_logo(png_bytes(), user=user).status_code, 403, user.username)
            self.assertEqual(self.client_for(user).delete(self.logo_url).status_code, 403, user.username)
        self.assertEqual(self.put_logo(png_bytes(), user=self.outsider).status_code, 404)
        self.assertEqual(self.client_for(self.admin).put(self.logo_url, {"logo": upload(png_bytes())}, format="multipart").status_code, 404)
        self.assertFalse(self.fresh().logo)

    def test_a_personal_workspace_has_no_logo(self):
        personal = Organization.objects.get(is_personal=True, memberships__user=self.owner)
        url = reverse("organizations:organization-logo", args=[personal.pk])
        self.assertEqual(self.client_for(self.owner).put(url, {"logo": upload(png_bytes())}, format="multipart").status_code, 403)

    # -- what is stored is a fresh, small PNG ---------------------------------------------------------------------------
    def test_a_big_jpeg_is_reduced_and_re_encoded_without_its_metadata(self):
        original = jpeg_bytes()
        self.assertIn(b"SecretCamera", original)
        self.assertEqual(self.put_logo(original, name="logo.jpg", content_type="image/jpeg").status_code, 200)
        data, image = self.stored_image()
        self.assertEqual(image.format, "PNG")
        self.assertLessEqual(max(image.size), LOGO_MAX_EDGE)
        self.assertEqual(image.size, (512, 256))
        self.assertEqual(len(image.getexif()), 0)
        self.assertNotIn(b"SecretCamera", data)

    def test_transparency_is_kept_and_a_small_logo_is_not_enlarged(self):
        self.put_logo(png_bytes(size=(40, 20), colour=(10, 20, 30, 0)))
        _data, image = self.stored_image()
        self.assertEqual((image.mode, image.size), ("RGBA", (40, 20)))
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_bytes_hidden_after_the_image_never_reach_storage(self):
        payload = b"<script>alert('x')</script>"
        self.assertEqual(self.put_logo(jpeg_bytes(size=(60, 40)) + payload, name="l.jpg", content_type="image/jpeg").status_code, 200)
        data, _image = self.stored_image()
        self.assertNotIn(b"<script>", data)

    # -- what is refused -------------------------------------------------------------------------------------------------
    def test_a_logo_of_exactly_one_megabyte_is_accepted_and_one_byte_more_is_not(self):
        base = png_bytes()
        exactly = base + b"\0" * (LOGO_MAX_BYTES - len(base))
        self.assertEqual(len(exactly), LOGO_MAX_BYTES)
        self.assertEqual(self.put_logo(exactly).status_code, 200)
        response = self.put_logo(exactly + b"\0")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"logo": ["The logo must be at most 1 MB."]})

    def test_only_png_and_jpeg_are_accepted(self):
        def encoded(fmt):
            buffer = io.BytesIO()
            Image.new("RGB", (20, 20), (1, 2, 3)).save(buffer, fmt)
            return buffer.getvalue()

        for label, content in (("gif", encoded("GIF")), ("webp", encoded("WEBP")), ("bmp", encoded("BMP"))):
            with self.subTest(label):
                response = self.put_logo(content, name=f"logo.{label}", content_type=f"image/{label}")
                self.assertEqual((response.status_code, response.json()), (400, {"logo": ["Use a PNG or JPEG image."]}))
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><rect width="10" height="10"/></svg>'
        self.assertEqual(self.put_logo(svg, name="logo.svg", content_type="image/svg+xml").status_code, 400)
        self.assertFalse(self.fresh().logo)

    def test_a_file_that_is_not_an_image_is_refused_whatever_it_is_called(self):
        for content in (b"", b"not an image at all", b"%PDF-1.4 fake", b"\x89PNG\r\n\x1a\n" + b"\0" * 20):
            with self.subTest(content=content[:12]):
                self.assertEqual(self.put_logo(content).status_code, 400)
        self.assertEqual(self.client_for(self.owner).put(self.logo_url, {}, format="multipart").status_code, 400)

    def test_an_image_with_far_too_many_pixels_is_refused_before_it_is_decoded(self):
        buffer = io.BytesIO()
        Image.new("1", (5000, 5000)).save(buffer, "PNG")                  # tiny file, 25 million pixels
        self.assertLess(len(buffer.getvalue()), LOGO_MAX_BYTES)
        response = self.put_logo(buffer.getvalue())
        self.assertEqual((response.status_code, response.json()), (400, {"logo": ["The image has too many pixels."]}))

    def test_a_truncated_image_is_refused(self):
        self.assertEqual(self.put_logo(png_bytes(size=(300, 300))[:80]).status_code, 400)

    # -- replacing and removing -------------------------------------------------------------------------------------------
    def test_replacing_the_logo_deletes_the_old_file_and_changes_the_address_version(self):
        first = self.put_logo(png_bytes(colour=(255, 0, 0, 255))).json()["logo_url"]
        old_name = self.fresh().logo.name
        with mock.patch("organizations.branding.timezone.now", return_value=timezone.now() + timedelta(milliseconds=2)):
            second = self.put_logo(png_bytes(colour=(0, 255, 0, 255))).json()["logo_url"]
        self.assertNotEqual(first, second)                                          # even two replacements in one second get different addresses
        new_name = self.fresh().logo.name
        self.assertNotEqual(old_name, new_name)
        self.assertFalse(default_storage.exists(old_name))
        self.assertTrue(default_storage.exists(new_name))
        _data, image = self.stored_image()
        self.assertEqual(image.convert("RGB").getpixel((0, 0)), (0, 255, 0))
        replaced = OrganizationAuditEvent.objects.filter(organization=self.organization, action=Action.ORGANIZATION_LOGO_CHANGED).order_by("id")
        self.assertEqual([event.new_value["replaced"] for event in replaced], [False, True])

    def test_a_refused_replacement_leaves_the_current_logo_alone(self):
        self.put_logo(png_bytes())
        name = self.fresh().logo.name
        self.assertEqual(self.put_logo(b"garbage").status_code, 400)
        self.assertEqual(self.fresh().logo.name, name)
        self.assertTrue(default_storage.exists(name))

    def test_removing_the_logo_deletes_the_file_and_clears_the_address(self):
        self.put_logo(png_bytes())
        name = self.fresh().logo.name
        response = self.client_for(self.owner).delete(self.logo_url)
        self.assertEqual((response.status_code, response.json()["logo_url"]), (200, None))
        self.assertFalse(default_storage.exists(name))
        self.assertEqual(self.client_for(self.member).get(self.logo_url).status_code, 404)
        self.assertEqual(self.client_for(self.owner).delete(self.logo_url).status_code, 200)         # nothing to remove is not an error
        self.assertEqual(OrganizationAuditEvent.objects.filter(organization=self.organization, action=Action.ORGANIZATION_LOGO_REMOVED).count(), 1)

    def test_changing_the_logo_is_rate_limited_but_reading_it_is_not(self):
        self.put_logo(png_bytes())
        for _ in range(40):
            self.assertEqual(self.client_for(self.member).get(self.logo_url).status_code, 200)
        statuses = [self.client_for(self.owner).delete(self.logo_url).status_code for _ in range(31)]
        self.assertEqual(statuses[:29], [200] * 29)                    # 30 an hour, the upload above was the first
        self.assertEqual(statuses[29:], [429, 429])

    def test_the_stored_file_has_a_sane_signature(self):
        self.put_logo(png_bytes())
        data, _image = self.stored_image()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(struct.unpack(">I", data[8:12])[0], 0)
