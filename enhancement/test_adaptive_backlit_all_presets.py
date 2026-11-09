"""The shadow lift of a back-lit subject for EVERY preset, on request (ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS, off by default).

In the owner's database 29 results (9 distinct photographs) whose main face had a median luminance under 60 in a bright frame (``backlit_subject``) came back with no lift under Natural / Dynamic:
those presets leave a dark face in a bright frame alone, AI Enhance lifts it. The switch gives Natural, Dynamic and Auto the lift AI Enhance has; it is still the SHADOW lift (a global one would burn the window), still behind
ADAPTIVE_BACKLIT_SUBJECT_LIFT, still judged by every gate, and the person's "Light photo" still turns it off. Models are the classical lift here: what is tested is the decision.
"""
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .adaptive import operations as ops
from .adaptive.profile import profile_for
from .adaptive.router import AdaptiveEnhancementRouter
from .adaptive.service import EngineConfig
from .test_adaptive_backlit_subject import CLASSICAL, HEAD, scene
from .test_adaptive_engine import analyzer_with, portrait

STYLES = ("natural", "dynamic", "ai_max")


class SettingTests(SimpleTestCase):
    def test_it_is_off_by_default(self):
        self.assertIs(settings.ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS, False)


class ProfileTests(SimpleTestCase):
    def test_by_default_only_ai_enhance_has_the_lift(self):
        self.assertEqual([profile_for(style).lift_backlit_subjects for style in STYLES], [False, False, True])

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_switched_on_every_preset_has_it_at_every_strength(self):
        for style in STYLES:
            for level in ("auto", "low", "medium", "high"):
                with self.subTest(style=style, level=level):
                    self.assertTrue(profile_for(style, level).lift_backlit_subjects)
                    self.assertTrue(profile_for(style, level).describe()["lift_backlit_subjects"])

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_the_persons_light_photo_still_turns_it_off(self):
        for style in STYLES:
            with self.subTest(style=style):
                self.assertFalse(profile_for(style, lighting="light").lift_backlit_subjects)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_the_persons_dark_photo_keeps_it(self):
        self.assertTrue(profile_for("natural", lighting="dark").lift_backlit_subjects)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_nothing_else_in_the_profile_changes(self):
        for style in ("natural", "dynamic"):
            with self.subTest(style=style):
                with override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=False):
                    before = profile_for(style, "high").describe()
                after = profile_for(style, "high").describe()
                before.pop("lift_backlit_subjects"), after.pop("lift_backlit_subjects")
                self.assertEqual(before, after)


@override_settings(**CLASSICAL, ADAPTIVE_ALWAYS_ENHANCE=False, ADAPTIVE_BACKLIT_SUBJECT_LIFT=True)
class RouterTests(SimpleTestCase):
    def step(self, style, image=None, level="auto", lighting="auto"):
        report = analyzer_with(HEAD).analyze(scene() if image is None else image)
        return AdaptiveEnhancementRouter(EngineConfig()).next_step(report, set(), profile=profile_for(style, level, lighting)).step

    def test_by_default_auto_and_standard_leave_the_back_lit_subject_as_photographed(self):
        for style in ("natural", "dynamic"):
            with self.subTest(style=style):
                step = self.step(style)
                self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_switched_on_they_propose_the_shadow_lift_not_a_global_one(self):
        for style in STYLES:
            with self.subTest(style=style):
                step = self.step(style)
                self.assertEqual(step.operation, ops.LOW_LIGHT)
                self.assertTrue(all(variant["local"] == "shadows" for variant in step.variants))      # the window is left as photographed
                self.assertIn("back-lit", step.reason)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True, ADAPTIVE_BACKLIT_SUBJECT_LIFT=False)
    def test_it_does_not_bypass_the_servers_own_switch(self):
        step = self.step("natural")
        self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_a_photograph_that_is_not_back_lit_is_not_touched(self):
        step = self.step("natural", image=portrait())
        self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)

    @override_settings(ADAPTIVE_BACKLIT_LIFT_ALL_PRESETS=True)
    def test_the_persons_light_photo_is_respected(self):
        step = self.step("natural", lighting="light")
        self.assertTrue(step is None or step.operation != ops.LOW_LIGHT)
