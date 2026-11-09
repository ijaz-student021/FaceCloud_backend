"""A person can say "this is a dark photo" or "this is a light photo" (``lighting``: auto | dark | light) on an enhancement request, a batch request and an upload.

``auto`` (the default, what every request before the choice meant) lets the analyzer decide, byte for byte as before. ``dark`` runs the dark-photo method whatever the analyzer saw (the
shadows are lifted first: a dark scene, a back-lit subject's shadows, or the whole frame; noise is then treated from the "low" level), ``light`` runs the light-photo method (no lift is tried).
A forced method goes through every gate and limit an automatic one does, the result says what happened in one plain sentence and never claims a method ran that did not, and the result's
``lighting`` block gains ``used`` and ``forced``. Models are replaced by the classical lift here: what is tested is the decision, the request, the storage and what the result says.
"""
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from . import services
from . import test_auto_enhancement as auto_tests
from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import AdaptiveImageEnhancementService, EngineConfig
from .auto import batch_enhancement_options
from .models import EnhancementBatchJob, EnhancementLighting, EnhancementTrigger, ImageEnhancementResult
from .outcome import result_lighting
from .serializers import EnhancementRequestSerializer, ImageEnhancementResultSerializer
from .test_adaptive_backlit_subject import CLASSICAL, HEAD, scene
from .test_adaptive_engine import analyzer_with, portrait
from .test_pipeline_regressions import NO_MODELS, ResultFixture


def route(image, style="natural", level="auto", lighting="auto", head=HEAD):
    """The router's next step for a photograph under a style, a strength and a word on the lighting."""
    report = analyzer_with(head).analyze(image)
    return AdaptiveEnhancementRouter(EngineConfig()).next_step(report, set(), profile=profile_for(style, level, lighting))


class ProfileTests(SimpleTestCase):
    def test_the_default_is_auto_and_changes_nothing_else(self):
        for style in ("natural", "dynamic", "ai_max"):
            for level in ("auto", "low", "medium", "high"):
                with self.subTest(style=style, level=level):
                    plain = profile_for(style, level)
                    self.assertEqual(plain, profile_for(style, level, "auto"))
                    self.assertEqual(plain.lighting, "auto")
                    self.assertEqual(plain.describe()["lighting"], "auto")

    def test_dark_treats_noise_from_the_low_level_and_otherwise_changes_nothing(self):
        for style in ("natural", "dynamic", "ai_max"):
            with self.subTest(style=style):
                plain, dark = profile_for(style), profile_for(style, "auto", "dark")
                self.assertEqual(dark.lighting, "dark")
                self.assertTrue(dark.treat_low_noise)         # a lift makes the noise of a dark photograph visible: "brightens shadows first, then reduces noise"
                self.assertEqual(dark.lift_backlit_subjects, plain.lift_backlit_subjects)
                self.assertEqual(dark.lift_dark_scenes, plain.lift_dark_scenes)
                self.assertEqual(dark.polish_scale, plain.polish_scale)

    def test_light_turns_every_lift_off(self):
        for style in ("natural", "dynamic", "ai_max"):
            with self.subTest(style=style):
                light = profile_for(style, "high", "light")
                self.assertEqual(light.lighting, "light")
                self.assertFalse(light.lift_dark_scenes)
                self.assertFalse(light.lift_backlit_subjects)

    def test_anything_else_is_auto_and_the_enum_is_accepted(self):
        self.assertEqual(profile_for("natural", "auto", "bright").lighting, "auto")
        self.assertEqual(profile_for("natural", "auto", None).lighting, "auto")
        self.assertEqual(profile_for("natural", "auto", EnhancementLighting.DARK).lighting, "dark")


class RouterTests(SimpleTestCase):
    def lift(self, *args, **kwargs):
        decision = route(*args, **kwargs)
        return decision.step if decision.step is not None and decision.step.operation == ops.LOW_LIGHT else None

    # -- "auto": exactly as before ----------------------------------------------------------------------------------------------------
    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)
    def test_auto_is_what_it_was(self):
        self.assertIsNone(self.lift(scene(), "natural"))                           # back-lit, natural: left as photographed
        self.assertIsNotNone(self.lift(scene(), "ai_max"))                         # back-lit, AI Enhance, switch on: the shadow lift
        self.assertIsNone(self.lift(portrait(), "ai_max", head=(150, 100, 180, 220)))
        self.assertIsNotNone(self.lift(portrait(brightness=0.12), "natural", head=None))     # a dark scene is lifted by every style (profile.lift_dark_scenes), with or without the person's word

    # -- "dark" -----------------------------------------------------------------------------------------------------------------------
    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
    def test_dark_lifts_a_back_lit_subjects_shadows_in_every_style_and_whatever_the_servers_switch_says(self):
        for style in ("natural", "dynamic", "ai_max"):
            with self.subTest(style=style):
                step = self.lift(scene(), style, lighting="dark")
                self.assertTrue(step.optional)
                self.assertTrue(all(variant["local"] == "shadows" for variant in step.variants))      # only the shadows: the sky is left as photographed
                self.assertIn("back-lit", step.reason)
                self.assertIn("You said this is a dark photograph.", step.reason)

    def test_dark_lifts_a_dark_scene_even_where_the_profile_would_leave_it_as_photographed(self):
        """No style does today, but a profile that does (``lift_dark_scenes`` off) must not hold back a person who said the photograph is dark."""
        from dataclasses import replace

        scene = portrait(brightness=0.35)                      # a dark scene whose face stays well lit (a flash, a lamp): the analyzer's "dark_scene"
        x, y, w, h = (150, 100, 180, 220)
        scene[y:y + h, x:x + w] = np.clip(portrait()[y:y + h, x:x + w].astype(np.float32) * 1.05, 0, 255).astype(np.uint8)
        report = analyzer_with().analyze(scene)
        self.assertEqual(report.exposure["status"], "dark_scene")
        router = AdaptiveEnhancementRouter(EngineConfig())
        held_back = replace(profile_for("natural"), lift_dark_scenes=False)
        self.assertIsNone(router.next_step(report, set(), profile=held_back).step)                    # as photographed
        asked = replace(held_back, lighting="dark")
        step = router.next_step(report, set(), profile=asked).step
        self.assertEqual(step.operation, ops.LOW_LIGHT)
        self.assertIn("You said this is a dark photograph.", step.reason)

    def test_dark_lifts_a_photograph_the_analyzer_calls_well_exposed_the_way_a_dark_one_is_lifted(self):
        step = self.lift(portrait(), "natural", lighting="dark", head=(150, 100, 180, 220))
        self.assertIsNotNone(step)
        self.assertTrue(all("local" not in variant for variant in step.variants))      # the whole frame, with the same blends the automatic path uses (no looser limit)
        self.assertEqual([variant["blend"] for variant in step.variants], [1.0, 0.65])
        self.assertTrue(step.optional)
        self.assertIn("You said this is a dark photograph.", step.reason)

    def test_dark_on_a_photograph_that_was_already_low_light_is_the_same_lift_with_the_sentence_added(self):
        auto, dark = self.lift(portrait(brightness=0.12), "ai_max", head=None), self.lift(portrait(brightness=0.12), "ai_max", lighting="dark", head=None)
        self.assertEqual([variant["blend"] for variant in auto.variants], [variant["blend"] for variant in dark.variants])
        self.assertTrue(dark.reason.startswith(auto.reason))
        self.assertFalse(auto.optional)          # a real low-light photograph: a refused lift is a defect that stays
        self.assertTrue(dark.optional)           # the person's own word: a refused lift must not hold the polish back

    def test_dark_makes_the_strength_level_irrelevant_for_whether_a_dark_scene_is_lifted(self):
        for level in ("auto", "low", "medium", "high"):
            with self.subTest(level=level):
                self.assertIsNotNone(self.lift(portrait(brightness=0.12), "natural", level=level, lighting="dark", head=None))

    # -- "light" ----------------------------------------------------------------------------------------------------------------------
    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)
    def test_light_tries_no_lift_at_all(self):
        for style in ("natural", "dynamic", "ai_max"):
            for level in ("auto", "high"):
                with self.subTest(style=style, level=level):
                    self.assertIsNone(self.lift(scene(), style, level, "light"))                              # a back-lit subject, even for AI Enhance with the switch on
                    self.assertIsNone(self.lift(portrait(brightness=0.12), style, level, "light", head=None))   # a dark scene, even where the style or strength lifts one
        self.assertIsNone(self.lift(portrait(brightness=0.03), "ai_max", "high", "light", head=None))           # even a very dark one: the person said so (the validator is not asked)

    def test_light_asks_the_router_for_the_other_operations_as_before(self):
        """Nothing but the lift is switched off: a noisy photograph is still denoised."""
        noisy = np.clip(portrait().astype(np.float32) + np.random.default_rng(9).normal(0, 22, portrait().shape), 0, 255).astype(np.uint8)
        steps = {lighting: route(noisy, "natural", lighting=lighting, head=(150, 100, 180, 220)).step for lighting in ("auto", "light")}
        self.assertIsNotNone(steps["light"])
        self.assertEqual(steps["auto"].operation, steps["light"].operation)


class SentenceTests(SimpleTestCase):
    """The one plain sentence on what the person's word did. It never says a method ran that did not."""

    def sentence(self, lighting, stages=(), accepted=()):
        return AdaptiveImageEnhancementService._lighting_sentence(profile_for("natural", "auto", lighting), list(stages), list(accepted))

    def test_auto_says_nothing(self):
        self.assertEqual(self.sentence("auto", [{"operation": ops.LOW_LIGHT, "status": "accepted"}], [ops.LOW_LIGHT]), "")

    def test_light_says_the_brightness_was_kept(self):
        self.assertEqual(self.sentence("light"), "As you asked, the light-photo method was used: the brightness was kept.")

    def test_dark_that_brightened_says_so(self):
        self.assertEqual(self.sentence("dark", [{"operation": ops.LOW_LIGHT, "status": "accepted"}], [ops.LOW_LIGHT]), "As you asked, the dark-photo method brightened the photograph.")

    def test_dark_whose_lift_was_refused_says_nothing_was_changed(self):
        self.assertEqual(self.sentence("dark", [{"operation": ops.LOW_LIGHT, "status": "rejected"}]), "Brightening was not safe to apply, so the dark-photo method changed nothing.")

    def test_dark_whose_model_was_not_there_says_that(self):
        for status in ("unavailable", "no_engine"):
            with self.subTest(status=status):
                self.assertEqual(self.sentence("dark", [{"operation": ops.LOW_LIGHT, "status": status}]), "The brightening model was not available, so the dark-photo method changed nothing.")

    def test_dark_that_was_never_proposed_does_not_claim_it_ran(self):
        self.assertEqual(self.sentence("dark"), "The dark-photo method could not brighten this photograph, so it changed nothing.")
        self.assertNotIn("brightened the photograph", self.sentence("dark", [{"operation": ops.DENOISE, "status": "accepted"}], [ops.DENOISE]))


@override_settings(**CLASSICAL, ADAPTIVE_ALWAYS_ENHANCE=False, ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
class EngineTests(SimpleTestCase):
    def run_engine(self, image, style="natural", lighting="auto", head=HEAD):
        service = AdaptiveImageEnhancementService(analyzer=analyzer_with(head), config=EngineConfig(), runners={})
        with patch("enhancement.adaptive.service.IdentityChecker") as checker:
            checker.return_value.available = False
            checker.return_value.threshold = None
            checker.return_value.reason = "No identity model is configured, so identity was not checked."
            output, _level, parameters = service.enhance(image, requested_level="auto", style=style, lighting=lighting)
        return output, parameters

    def test_dark_lifts_the_subject_of_a_back_lit_photograph_and_leaves_the_sky_and_says_so(self):
        image = scene()
        output, parameters = self.run_engine(image, lighting="dark")
        adaptive = parameters["adaptive"]
        self.assertIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        self.assertGreater(float(output[250:330, 280:350].mean()), float(image[250:330, 280:350].mean()) + 15)
        self.assertLessEqual(int(np.abs(output[10:90, 20:600].astype(int) - image[10:90, 20:600].astype(int)).max()), 2)
        self.assertTrue(adaptive["summary"].startswith("As you asked, the dark-photo method brightened the photograph."))
        self.assertEqual(adaptive["profile"]["lighting"], "dark")
        self.assertEqual(parameters["requested_lighting"], "dark")

    def test_the_same_photograph_with_auto_is_left_alone_exactly_as_before(self):
        image = scene()
        output, parameters = self.run_engine(image)
        np.testing.assert_array_equal(output, image)
        self.assertNotIn(ops.LOW_LIGHT, parameters["adaptive"]["operations_applied"])
        self.assertEqual(parameters["adaptive"]["profile"]["lighting"], "auto")
        self.assertEqual(parameters["requested_lighting"], "auto")
        self.assertNotIn("As you asked", parameters["adaptive"]["summary"])

    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)
    def test_light_keeps_the_brightness_of_a_photograph_the_engine_would_have_lifted_and_says_so(self):
        image = scene()
        auto_output, auto = self.run_engine(image, style="ai_max")
        light_output, light = self.run_engine(image, style="ai_max", lighting="light")
        self.assertIn(ops.LOW_LIGHT, auto["adaptive"]["operations_applied"])             # AI Enhance with the server's switch on lifts the subject's shadows
        self.assertNotIn(ops.LOW_LIGHT, light["adaptive"]["operations_applied"])
        self.assertLess(float(light_output[250:330, 280:350].mean()), float(auto_output[250:330, 280:350].mean()) - 15)       # the subject keeps its brightness
        self.assertTrue(light["adaptive"]["summary"].startswith("As you asked, the light-photo method was used: the brightness was kept."))

    def test_dark_on_a_photograph_the_analyzer_calls_light_is_judged_by_the_same_gates(self):
        """A forced lift is a candidate like any other: when it is refused, the photograph is returned as it was and the result says so; when it passes, it is applied. It never lies."""
        image = portrait()                         # already well exposed: the analyzer calls it light, so the lift is not needed and the same gates that guard an automatic one refuse it
        output, parameters = self.run_engine(image, lighting="dark", head=(150, 100, 180, 220))
        adaptive = parameters["adaptive"]
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)         # the lift was TRIED: the person asked for it
        self.assertEqual(stage["status"], "rejected")
        self.assertIn("subject_not_clearer_or_too_bright", stage["rejection_reasons"])
        self.assertNotIn(ops.LOW_LIGHT, adaptive["operations_applied"])
        self.assertTrue(adaptive["summary"].startswith("Brightening was not safe to apply, so the dark-photo method changed nothing."))       # it never says a method ran that did not
        self.assertEqual(adaptive["outcome"], "original_preserved")
        np.testing.assert_array_equal(output, image)
        self.assertEqual((adaptive["profile"]["lighting"], parameters["requested_lighting"]), ("dark", "dark"))

    @override_settings(ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
    def test_the_forced_lift_is_held_to_the_compression_gate_like_the_automatic_one(self):
        """The shadow lift of a heavily compressed photograph shows its blocks and is refused, however it was asked for."""
        from .test_adaptive_backlit_subject import SUBJECT_WITH_STRUCTURE, jpeg

        image = jpeg(scene(**SUBJECT_WITH_STRUCTURE), 40)
        output, parameters = self.run_engine(image, lighting="dark")
        adaptive = parameters["adaptive"]
        stage = next(item for item in adaptive["stages"] if item["operation"] == ops.LOW_LIGHT)
        self.assertEqual(stage["status"], "rejected")
        self.assertIn("lift_shows_compression_blocks", stage["rejection_reasons"])
        self.assertTrue(adaptive["summary"].startswith("Brightening was not safe to apply, so the dark-photo method changed nothing."))
        np.testing.assert_array_equal(output, image)


def parameters_of(lighting_in_profile, exposure="backlit_subject", operations=()):
    """The part of ``applied_parameters`` that ``result_lighting`` reads."""
    adaptive = {"quality_report": {"exposure": {"status": exposure, "scene_median": 112.0}}, "operations_applied": list(operations)}
    if lighting_in_profile is not None:
        adaptive["profile"] = {"lighting": lighting_in_profile}
    return {"adaptive": adaptive}


class ResultBlockTests(SimpleTestCase):
    def block(self, params):
        return result_lighting(SimpleNamespace(applied_parameters=params))

    def test_an_automatic_result_is_not_forced_and_used_is_what_was_detected(self):
        self.assertEqual(self.block(parameters_of("auto", "low_light", [ops.LOW_LIGHT])),
                         {"detected": "dark", "exposure": "low_light", "scene_median": 112.0, "brightened": True, "used": "dark", "forced": False})
        self.assertEqual(self.block(parameters_of("auto", "backlit_subject")),
                         {"detected": "light", "exposure": "backlit_subject", "scene_median": 112.0, "brightened": False, "used": "light", "forced": False})

    def test_a_forced_dark_result_reports_the_method_the_person_chose(self):
        block = self.block(parameters_of("dark", "backlit_subject", [ops.LOW_LIGHT]))
        self.assertEqual((block["detected"], block["used"], block["forced"], block["brightened"]), ("light", "dark", True, True))

    def test_a_forced_dark_result_whose_lift_was_refused_says_brightened_false(self):
        block = self.block(parameters_of("dark", "backlit_subject"))
        self.assertEqual((block["used"], block["forced"], block["brightened"]), ("dark", True, False))      # the method was followed; it changed nothing: the summary says so

    def test_a_forced_light_result(self):
        block = self.block(parameters_of("light", "dark_scene"))
        self.assertEqual((block["detected"], block["used"], block["forced"], block["brightened"]), ("dark", "light", True, False))

    def test_a_result_made_before_the_choice_existed_keeps_its_shape_and_is_not_forced(self):
        block = self.block(parameters_of(None, "dark_scene"))
        self.assertEqual((block["detected"], block["used"], block["forced"]), ("dark", "dark", False))

    def test_a_result_without_an_adaptive_report_has_no_block(self):
        self.assertIsNone(self.block({}))
        self.assertIsNone(self.block({"adaptive": {}}))


class RequestSerializerTests(SimpleTestCase):
    def validated(self, data):
        serializer = EnhancementRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.validated_data

    def test_a_request_without_the_field_means_auto(self):
        for data in ({}, {"level": "high"}, {"preset": "pro"}, {"style": "dynamic", "level": "low"}):
            with self.subTest(data):
                self.assertEqual(self.validated(data)["lighting"], "auto")

    def test_each_word_is_accepted(self):
        for word in ("auto", "dark", "light"):
            self.assertEqual(self.validated({"lighting": word})["lighting"], word)

    def test_it_is_independent_of_the_preset_rule(self):
        data = self.validated({"preset": "pro", "lighting": "dark"})
        self.assertEqual((data["level"], data["style"], data["lighting"]), ("high", "ai_max", "dark"))

    def test_any_other_value_is_a_400_with_a_sentence(self):
        for value in ("bright", "Dark", "", None, 3):
            with self.subTest(value=value):
                serializer = EnhancementRequestSerializer(data={"lighting": value})
                self.assertFalse(serializer.is_valid())
                self.assertIn("lighting", serializer.errors)


class LightingEndpointTests(ResultFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def photo(self, name="photo.jpg"):
        result = self.make_result(name)
        image = result.image
        result.delete()
        return image

    def enhance(self, image, body):
        with patch("enhancement.tasks.process_enhancement_image.apply_async") as queued:
            response = self.client.post(f"/api/enhancements/images/{image.pk}/", body, format="json")
        return response, queued

    def test_a_request_without_the_field_stores_auto_exactly_as_before(self):
        image = self.photo()
        response, _queued = self.enhance(image, {"level": "auto", "style": "natural"})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(ImageEnhancementResult.objects.get(image=image).requested_lighting, "auto")
        self.assertEqual(response.json()["requested_lighting"], "auto")

    def test_dark_and_light_are_stored_and_answered(self):
        for word in ("dark", "light"):
            with self.subTest(word):
                image = self.photo(f"{word}.jpg")
                response, _queued = self.enhance(image, {"style": "natural", "lighting": word})
                self.assertEqual(response.status_code, 202, response.content)
                self.assertEqual(ImageEnhancementResult.objects.get(image=image).requested_lighting, word)
                self.assertEqual(response.json()["requested_lighting"], word)

    def test_it_works_with_a_preset(self):
        image = self.photo()
        response, _queued = self.enhance(image, {"preset": "pro", "lighting": "dark"})
        result = ImageEnhancementResult.objects.get(image=image)
        self.assertEqual((result.requested_level, result.requested_style, result.requested_lighting), ("high", "ai_max", "dark"))
        self.assertEqual(response.status_code, 202)

    def test_an_unknown_word_is_a_400_a_sentence_and_nothing_is_queued(self):
        image = self.photo()
        response, queued = self.enhance(image, {"lighting": "bright"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("lighting", response.json())
        self.assertIn("not a valid choice", response.json()["lighting"][0])
        queued.assert_not_called()
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())

    def test_a_form_encoded_request_works_the_same(self):
        image = self.photo()
        with patch("enhancement.tasks.process_enhancement_image.apply_async"):
            response = self.client.post(f"/api/enhancements/images/{image.pk}/", {"lighting": "dark"}, format="multipart")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(ImageEnhancementResult.objects.get(image=image).requested_lighting, "dark")

    def test_a_batch_applies_it_to_every_photograph(self):
        photos = [self.photo(f"batch{index}.jpg") for index in range(2)]
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"lighting": "light"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual({ImageEnhancementResult.objects.get(image=photo).requested_lighting for photo in photos}, {"light"})
        self.assertEqual(EnhancementBatchJob.objects.count(), 1)

    def test_a_batch_without_the_field_is_auto_and_a_bad_word_creates_nothing(self):
        photos = [self.photo(f"plain{index}.jpg") for index in range(2)]
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            self.assertEqual(self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"level": "auto"}, format="json").status_code, 201)
        self.assertEqual({ImageEnhancementResult.objects.get(image=photo).requested_lighting for photo in photos}, {"auto"})
        ImageEnhancementResult.objects.all().delete()
        EnhancementBatchJob.objects.all().delete()
        with patch("enhancement.tasks.process_enhancement_batch.apply_async"):
            response = self.client.post(f"/api/enhancements/batches/{self.batch.pk}/", {"lighting": "x"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageEnhancementResult.objects.exists())

    def test_two_requests_with_different_lighting_never_share_a_result(self):
        """Nothing is reused for "the same request": each request makes its own result row with its own word, and a dark request never answers with the light one."""
        image = self.photo()
        first, _queued = self.enhance(image, {"lighting": "light"})
        self.assertEqual(first.status_code, 202)
        ImageEnhancementResult.objects.filter(pk=first.json()["id"]).update(status="completed")        # no longer in progress, so the person may ask again
        second, _queued = self.enhance(image, {"lighting": "dark"})
        self.assertEqual(second.status_code, 202, second.content)
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.json()["requested_lighting"], "dark")
        self.assertEqual(ImageEnhancementResult.objects.get(pk=first.json()["id"]).requested_lighting, "light")

    def test_an_old_result_serialises_without_a_forced_method(self):
        result = self.make_result("old.jpg", status="completed")
        result.applied_parameters = parameters_of(None, "dark_scene", [ops.LOW_LIGHT])          # made before the choice existed: no "lighting" in the engine's profile
        result.save()
        data = ImageEnhancementResultSerializer(result, context={}).data
        self.assertEqual(data["requested_lighting"], "auto")
        self.assertEqual((data["lighting"]["used"], data["lighting"]["forced"]), ("dark", False))

    def test_the_schema_lists_the_three_words(self):
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator().get_schema(request=None, public=True)
        self.assertEqual(schema["components"]["schemas"]["EnhancementLightingEnum"]["enum"], ["auto", "dark", "light"])
        self.assertIn("lighting", schema["components"]["schemas"]["EnhancementRequest"]["properties"])
        self.assertEqual(set(schema["components"]["schemas"]["Lighting"]["properties"]) & {"used", "forced"}, {"used", "forced"})


class EnhanceBgrTests(SimpleTestCase):
    """``enhance_bgr`` hands the adaptive engine the word only when there is one: the call for ``auto`` is the call it always was."""

    def call(self, **kwargs):
        image = np.zeros((8, 8, 3), np.uint8)
        with override_settings(ENHANCEMENT_RENDER_POLICY="adaptive_v1"), patch.object(AdaptiveImageEnhancementService, "enhance", return_value=(image, "low", {})) as engine:
            services.enhance_bgr(image, "auto", "natural", **kwargs)
        return engine.call_args

    def test_auto_is_not_passed(self):
        self.assertNotIn("lighting", self.call().kwargs)
        self.assertNotIn("lighting", self.call(lighting="auto").kwargs)
        self.assertNotIn("lighting", self.call(lighting=EnhancementLighting.AUTO).kwargs)

    def test_dark_and_light_are_passed_on(self):
        self.assertEqual(self.call(lighting="dark").kwargs["lighting"], "dark")
        self.assertEqual(self.call(lighting=EnhancementLighting.LIGHT).kwargs["lighting"], "light")

    def test_an_enlarged_source_is_passed_on_only_when_it_is_one(self):
        self.assertNotIn("source_enlarged", self.call().kwargs)
        self.assertNotIn("source_enlarged", self.call(source_enlarged=False).kwargs)
        self.assertIs(self.call(source_enlarged=True).kwargs["source_enlarged"], True)


@override_settings(**NO_MODELS)
class ThroughTheTaskTests(ResultFixture):
    """What ``process_enhancement_result`` hands the engine."""

    def run_result(self, lighting, strategy=None):
        result = self.make_result("task.jpg")
        result.requested_lighting = lighting
        result.save(update_fields=["requested_lighting"])
        read = services._read_enhancement_source
        source = (lambda made, decoding=None: (read(made, decoding)[0], strategy)) if strategy else read          # the strategy the source was read with (preprocessing's enlargement is "standardized_enlarged")
        with patch.object(services, "_read_enhancement_source", side_effect=source), patch.object(services, "enhance_bgr", wraps=services.enhance_bgr) as spy:
            services.process_enhancement_result(result)
        return spy

    def test_the_default_call_is_exactly_what_it_was(self):
        spy = self.run_result("auto")
        self.assertNotIn("lighting", spy.call_args.kwargs)
        self.assertNotIn("source_enlarged", spy.call_args.kwargs)

    def test_a_forced_word_is_handed_on(self):
        for word in ("dark", "light"):
            with self.subTest(word):
                self.assertEqual(self.run_result(word).call_args.kwargs["lighting"], word)

    def test_a_source_the_pipeline_enlarged_is_said_to_be(self):
        """Preprocessing enlarges a very small photograph and the engine then sees the enlarged copy: it is told, because the blocks of a tiny JPEG are invisible in it."""
        self.assertTrue(self.run_result("auto", "standardized_enlarged").call_args.kwargs["source_enlarged"])
        for strategy in ("full_resolution_original", "standardized_fallback"):
            with self.subTest(strategy):
                self.assertNotIn("source_enlarged", self.run_result("auto", strategy).call_args.kwargs)


class AutomaticEnhancementTests(auto_tests.Base):
    """The automatic enhancement of an upload uses what the upload said, so the person does not have to enhance twice."""

    def results(self, image):
        return ImageEnhancementResult.objects.filter(image=image)

    def accepted(self, **options):
        self.batch.auto_enhance_options = options
        self.batch.save(update_fields=["auto_enhance_options"])
        image = self.photo()
        self.decide(image, "accepted")
        return self.results(image).get()

    def test_a_batch_that_said_nothing_is_enhanced_as_it_always_was(self):
        result = self.accepted()
        self.assertEqual((result.trigger, result.requested_level, result.requested_style, result.requested_lighting), (EnhancementTrigger.AUTO, "auto", "natural", "auto"))

    def test_the_lighting_of_the_upload_is_used(self):
        for word in ("dark", "light"):
            with self.subTest(word):
                ImageEnhancementResult.objects.all().delete()
                result = self.accepted(level="auto", style="natural", lighting=word)
                self.assertEqual((result.requested_level, result.requested_style, result.requested_lighting), ("auto", "natural", word))

    def test_the_preset_pair_of_the_upload_is_used_too(self):
        result = self.accepted(level="high", style="ai_max", lighting="auto")
        self.assertEqual((result.trigger, result.requested_level, result.requested_style, result.requested_lighting), (EnhancementTrigger.AUTO, "high", "ai_max", "auto"))

    def test_the_options_helper_reads_what_is_stored_and_defaults_the_rest(self):
        self.assertEqual(batch_enhancement_options(SimpleNamespace(auto_enhance_options={})), ("auto", "natural", "auto"))
        self.assertEqual(batch_enhancement_options(SimpleNamespace(auto_enhance_options=None)), ("auto", "natural", "auto"))
        self.assertEqual(batch_enhancement_options(SimpleNamespace(auto_enhance_options={"lighting": "dark"})), ("auto", "natural", "dark"))
        self.assertEqual(batch_enhancement_options(SimpleNamespace(auto_enhance_options={"level": "high", "style": "ai_max", "lighting": "light"})), ("high", "ai_max", "light"))


class UploadFieldsTests(ResultFixture):
    """``preset`` / ``level`` / ``style`` / ``lighting`` on the three upload calls: validated like an enhancement request, stored on the batch, shown in the answer."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def png(self, name="up.png"):
        import cv2
        from django.core.files.uploadedfile import SimpleUploadedFile

        picture = np.random.default_rng(4).integers(60, 200, (240, 320, 3), dtype=np.uint8)
        return SimpleUploadedFile(name, cv2.imencode(".png", picture)[1].tobytes(), content_type="image/png")

    def upload(self, **fields):
        from uploads.models import UploadBatch

        before = set(UploadBatch.objects.values_list("pk", flat=True))
        with patch("uploads.services.preprocess_image"):
            response = self.client.post("/api/images/upload/single/", {"file": self.png(), **fields}, format="multipart")
        created = UploadBatch.objects.exclude(pk__in=before)
        return response, (created.get() if created.count() == 1 else None)

    def test_an_upload_that_says_nothing_stores_nothing_and_answers_an_empty_object(self):
        response, batch = self.upload()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(batch.auto_enhance_options, {})
        self.assertEqual(response.json()["auto_enhance_options"], {})

    def test_the_lighting_alone_is_stored_with_the_defaults_for_the_rest(self):
        response, batch = self.upload(lighting="dark")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(batch.auto_enhance_options, {"level": "auto", "style": "natural", "lighting": "dark"})
        self.assertEqual(response.json()["auto_enhance_options"], {"level": "auto", "style": "natural", "lighting": "dark"})

    def test_a_preset_is_resolved_to_its_pair_exactly_as_on_an_enhancement_request(self):
        cases = ((dict(preset="pro"), {"level": "high", "style": "ai_max", "lighting": "auto"}),
                 (dict(preset="standard"), {"level": "high", "style": "natural", "lighting": "auto"}),
                 (dict(preset="auto"), {"level": "auto", "style": "natural", "lighting": "auto"}),
                 (dict(preset="pro", level="low", lighting="light"), {"level": "low", "style": "ai_max", "lighting": "light"}),      # a level that is sent wins
                 (dict(level="medium", style="dynamic"), {"level": "medium", "style": "dynamic", "lighting": "auto"}))
        for fields, expected in cases:
            with self.subTest(fields):
                response, batch = self.upload(**fields)
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(batch.auto_enhance_options, expected)

    def test_a_value_that_is_not_allowed_is_a_400_and_no_batch_is_made(self):
        from uploads.models import UploadBatch

        for fields, key in ((dict(lighting="bright"), "lighting"), (dict(preset="ultra"), "preset"), (dict(level="extreme"), "level"), (dict(style="wild"), "style")):
            with self.subTest(fields):
                response, batch = self.upload(**fields)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(key, response.json())
                self.assertIsNone(batch)
        self.assertFalse(UploadBatch.objects.exclude(pk=self.batch.pk).exists())                    # the check runs before anything is stored

    def test_an_empty_field_means_it_was_not_sent(self):
        response, batch = self.upload(lighting="", preset="")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(batch.auto_enhance_options, {})

    def test_the_options_are_kept_when_the_upload_is_not_enhanced_automatically(self):
        response, batch = self.upload(lighting="light", auto_enhance="false")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(batch.auto_enhance)
        self.assertEqual(batch.auto_enhance_options["lighting"], "light")

    def test_multiple_and_zip_uploads_take_the_same_fields(self):
        import io
        import zipfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        from uploads.models import UploadBatch

        with patch("uploads.services.preprocess_image"), patch("uploads.views.enqueue_batch_preprocessing", side_effect=lambda batch: batch):
            response = self.client.post("/api/images/upload/multiple/", {"files": [self.png("a.png"), self.png("b.png")], "preset": "pro", "lighting": "dark"}, format="multipart")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.json()["auto_enhance_options"], {"level": "high", "style": "ai_max", "lighting": "dark"})
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("c.png", self.png("c.png").read())
            sent = SimpleUploadedFile("photos.zip", archive.getvalue(), content_type="application/zip")
            response = self.client.post("/api/images/upload/zip/", {"file": sent, "lighting": "light"}, format="multipart")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.json()["auto_enhance_options"]["lighting"], "light")
        self.assertEqual(UploadBatch.objects.exclude(pk=self.batch.pk).count(), 2)

    def test_the_schema_describes_the_four_fields_of_every_upload_call(self):
        from drf_spectacular.generators import SchemaGenerator

        paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
        for path in ("/api/images/upload/single/", "/api/images/upload/multiple/", "/api/images/upload/zip/"):
            with self.subTest(path):
                properties = paths[path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]
                self.assertEqual(properties["lighting"]["enum"], ["auto", "dark", "light"])
                self.assertEqual(properties["preset"]["enum"], ["auto", "standard", "pro"])
                self.assertIn("level", properties)
                self.assertIn("style", properties)


class NobodyElseCanUseItTests(ResultFixture):
    """The word on the lighting opens nothing: sign-in and ownership are decided before the body is read."""

    def test_a_stranger_and_an_anonymous_caller_are_refused(self):
        result = self.make_result("mine.jpg")
        image = result.image
        result.delete()
        url = f"/api/enhancements/images/{image.pk}/"
        self.assertIn(APIClient().post(url, {"lighting": "dark"}, format="json").status_code, (401, 403))
        stranger = get_user_model().objects.create_user(username="stranger2", email="stranger2@example.com", password="ForestRiver!4821", is_email_verified=True)
        other = APIClient()
        other.force_authenticate(stranger)
        self.assertEqual(other.post(url, {"lighting": "dark"}, format="json").status_code, 404)
        self.assertFalse(ImageEnhancementResult.objects.filter(image=image).exists())
