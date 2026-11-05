"""`preset` on the enhancement request: one name (auto, standard, pro) for a level + style pair that already exists.

The preset is resolved in EnhancementRequestSerializer.validate, before anything is queued, so the job, the batch and the stored `requested_level` / `requested_style` carry the real pair and
the engine never sees a preset. A request without a preset behaves exactly as before. "pro" is the pair the owner named (ai_max at the strongest level); "auto" is what a request with neither
field already means; "standard" is the one pair nobody named: the test that pins it is there so that changing it is a decision.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from .adaptive.profile import profile_for
from .models import EnhancementBatchJob, EnhancementLevel, EnhancementStyle, ImageEnhancementResult
from .serializers import PRESET_CHOICES, PRESETS, EnhancementRequestSerializer
from .test_pipeline_regressions import ResultFixture


def validated(data):
    serializer = EnhancementRequestSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def pair(data):
    got = validated(data)
    return str(got["level"]), str(got["style"])


class PresetMappingTests(SimpleTestCase):
    def test_the_three_presets_and_their_pairs(self):
        self.assertEqual(
            {name: (str(level), str(style)) for name, (level, style) in PRESETS.items()},
            {"auto": ("auto", "natural"), "standard": ("medium", "dynamic"), "pro": ("high", "ai_max")},
        )
        self.assertEqual([name for name, _label in PRESET_CHOICES], list(PRESETS))

    def test_every_preset_is_a_pair_the_api_already_has(self):
        for name, (level, style) in PRESETS.items():
            with self.subTest(name):
                self.assertIn(level, EnhancementLevel.values)
                self.assertIn(style, EnhancementStyle.values)

    def test_a_preset_sent_alone_becomes_its_pair(self):
        for name, (level, style) in PRESETS.items():
            with self.subTest(name):
                self.assertEqual(pair({"preset": name}), (str(level), str(style)))

    def test_pro_is_ai_enhance_at_the_strongest_level(self):
        got = validated({"preset": "pro"})
        self.assertEqual((got["level"], got["style"]), (EnhancementLevel.HIGH, EnhancementStyle.AI_MAX))

    def test_auto_is_what_a_request_with_nothing_in_it_already_means(self):
        self.assertEqual(pair({"preset": "auto"}), pair({}))

    def test_the_premise_behind_standard_the_engine_treats_auto_and_medium_alike(self):
        """If this fails, `standard` and `auto` can differ by strength too and the choice of `standard` should be looked at again."""
        for style in ("natural", "dynamic", "ai_max"):
            self.assertEqual(profile_for(style, "auto").describe() | {"level": ""}, profile_for(style, "medium").describe() | {"level": ""}, style)
        self.assertNotEqual(profile_for("natural", "auto").describe(), profile_for("dynamic", "medium").describe())

    def test_the_documentation_lists_the_pairs_from_the_same_table(self):
        help_text = EnhancementRequestSerializer().fields["preset"].help_text
        for name, (level, style) in PRESETS.items():
            self.assertIn(f"`{name}` = level `{level}` + style `{style}`", help_text)
        self.assertIn("ai_max", help_text)


class RequestWithoutAPresetTests(SimpleTestCase):
    """Backward compatibility: every request that worked before works the same."""

    def test_the_defaults_are_unchanged(self):
        got = validated({})
        self.assertEqual((got["level"], got["style"]), (EnhancementLevel.AUTO, EnhancementStyle.NATURAL))
        self.assertNotIn("preset", got)

    def test_level_and_style_alone(self):
        cases = (
            ({"level": "low"}, ("low", "natural")), ({"style": "dynamic"}, ("auto", "dynamic")), ({"level": "medium", "style": "dynamic"}, ("medium", "dynamic")),
            ({"level": "high", "style": "ai_max"}, ("high", "ai_max")), ({"level": "auto", "style": "natural"}, ("auto", "natural")),
        )
        for data, expected in cases:
            with self.subTest(data):
                self.assertEqual(pair(data), expected)

    def test_an_invalid_level_or_style_is_still_refused(self):
        for data in ({"level": "extreme"}, {"style": "wild"}, {"level": ""}):
            with self.subTest(data), self.assertRaises(ValidationError):
                validated(data)


class PresetWithLevelAndStyleTests(SimpleTestCase):
    def test_a_level_and_style_that_match_the_preset_are_accepted(self):
        self.assertEqual(pair({"preset": "pro", "level": "high", "style": "ai_max"}), ("high", "ai_max"))
        self.assertEqual(pair({"preset": "pro", "style": "ai_max"}), ("high", "ai_max"))
        self.assertEqual(pair({"preset": "auto", "level": "auto"}), ("auto", "natural"))

    def test_a_level_or_style_that_disagrees_is_refused_not_guessed(self):
        for data in ({"preset": "pro", "level": "low"}, {"preset": "pro", "style": "natural"}, {"preset": "standard", "level": "high"}, {"preset": "auto", "style": "ai_max"},
                     {"preset": "pro", "level": "auto", "style": "natural"}):
            with self.subTest(data):
                with self.assertRaises(ValidationError) as caught:
                    validated(data)
                message = str(caught.exception.detail["preset"][0])
                self.assertIn(f"`{data['preset']}` means level", message)
                self.assertIn("Send the preset alone", message)

    def test_the_message_names_the_field_that_disagrees(self):
        with self.assertRaises(ValidationError) as caught:
            validated({"preset": "pro", "level": "low"})
        self.assertIn("level sent", str(caught.exception.detail["preset"][0]))
        with self.assertRaises(ValidationError) as caught:
            validated({"preset": "pro", "level": "low", "style": "natural"})
        self.assertIn("level and style sent", str(caught.exception.detail["preset"][0]))

    def test_an_unknown_or_malformed_preset_is_refused(self):
        for value in ("ultra", "", "Pro", "PRO", None, 3, ["pro"]):
            with self.subTest(value), self.assertRaises(ValidationError) as caught:
                validated({"preset": value})
            self.assertIn("preset", caught.exception.detail)


class PresetEndpointTests(ResultFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def photo(self, name="photo.jpg"):
        result = self.make_result(name)
        image = result.image
        result.delete()
        return image

    def enhance(self, image, body, format="json"):
        with patch("enhancement.tasks.process_enhancement_image.apply_async"):
            return self.client.post(f"/api/enhancements/images/{image.pk}/", body, format=format)

    def stored(self, image):
        result = ImageEnhancementResult.objects.get(image=image)
        return result.requested_level, result.requested_style

    def test_pro_queues_the_strongest_ai_enhance(self):
        image = self.photo()
        response = self.enhance(image, {"preset": "pro"})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self.stored(image), ("high", "ai_max"))
        body = response.json()
        self.assertEqual((body["requested_level"], body["requested_style"]), ("high", "ai_max"))      # the real pair, never the preset's name

    def test_each_preset_queues_its_pair(self):
        for name, (level, style) in PRESETS.items():
            with self.subTest(name):
                image = self.photo(f"{name}.jpg")
                self.assertEqual(self.enhance(image, {"preset": name}).status_code, 202)
                self.assertEqual(self.stored(image), (str(level), str(style)))

    def test_the_old_requests_queue_what_they_always_did(self):
        cases = (({}, ("auto", "natural")), ({"level": "low"}, ("low", "natural")), ({"level": "high", "style": "ai_max"}, ("high", "ai_max")), ({"style": "dynamic"}, ("auto", "dynamic")))
        for index, (body, expected) in enumerate(cases):
            with self.subTest(body):
                image = self.photo(f"old{index}.jpg")
                self.assertEqual(self.enhance(image, body).status_code, 202)
                self.assertEqual(self.stored(image), expected)

    def test_a_conflict_is_a_400_and_nothing_is_queued(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_image.apply_async") as queued:
            response = self.client.post(f"/api/enhancements/images/{image.pk}/", {"preset": "pro", "level": "low"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("preset", response.json())
        queued.assert_not_called()
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())

    def test_an_unknown_preset_is_a_400(self):
        image = self.photo()
        response = self.enhance(image, {"preset": "ultra"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("preset", response.json())
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())

    def test_a_form_encoded_request_works_the_same_and_its_conflicts_are_found(self):
        ok, clash = self.photo("form_ok.jpg"), self.photo("form_clash.jpg")
        self.assertEqual(self.enhance(ok, {"preset": "pro"}, format="multipart").status_code, 202)
        self.assertEqual(self.stored(ok), ("high", "ai_max"))
        self.assertEqual(self.enhance(clash, {"preset": "pro", "level": "low"}, format="multipart").status_code, 400)

    def test_a_batch_takes_a_preset_too(self):
        photos = [self.photo(f"batch{index}.jpg") for index in range(2)]
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"preset": "pro"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual({self.stored(photo) for photo in photos}, {("high", "ai_max")})
        job = EnhancementBatchJob.objects.get()
        self.assertEqual((job.requested_level, job.requested_style), ("high", "ai_max"))

    def test_a_batch_conflict_creates_nothing(self):
        self.photo()
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"preset": "standard", "style": "natural"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EnhancementBatchJob.objects.exists())
        self.assertFalse(ImageEnhancementResult.objects.exists())

    def test_a_preset_does_not_change_who_may_enhance_what(self):
        """Sign-in and ownership are decided before the body is read: a preset opens nothing."""
        image = self.photo()
        url = f"/api/enhancements/images/{image.pk}/"
        self.assertIn(APIClient().post(url, {"preset": "pro"}, format="json").status_code, (401, 403))
        stranger = get_user_model().objects.create_user(username="stranger", email="stranger@example.com", password="ForestRiver!4821", is_email_verified=True)
        other = APIClient()
        other.force_authenticate(stranger)
        self.assertEqual(other.post(url, {"preset": "pro"}, format="json").status_code, 404)
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())
