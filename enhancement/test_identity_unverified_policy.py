"""Identity that cannot be verified is never reported as verified, and what the engine does about it is named and consistent.

Identity can be unverifiable for a photograph with a face because there is no biometric consent, the identity model is missing, the face is too dark
or small to embed, or the comparison is inconclusive. ``ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY`` decides what the adaptive engine then does:
``limited`` (the default, the behaviour so far: operations that do not synthesise a face may be applied, and the result is labelled
``identity_verification.mode == "limited_unverified"``) or ``fail_closed`` (nothing is applied, the original is returned, ``mode == "failed_closed"``).
Models are stand-ins here: these tests are about decisions and what is reported, not about how a photograph looks.
"""
import tempfile
from itertools import product
from unittest.mock import MagicMock, patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from privacy.testing import grant_face_consent, withdraw_face_consent

from .adaptive import identity_policy
from .adaptive import operations as ops
from .adaptive.service import ENHANCED, ORIGINAL_PRESERVED, PARTIALLY_ENHANCED, AdaptiveImageEnhancementService, EngineConfig, identity_report
from .adaptive.validator import FinalImageQualityValidator
from . import test_adaptive_engine as engine_tests      # a module, not its TestCase classes: those would be collected (and run) again here
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait, runner_lift, stub_analyzer_service
from .test_pipeline_regressions import ResultFixture

UNAVAILABLE = "unavailable"          # no model, or a face too dark to embed
INCONCLUSIVE = "inconclusive"        # a checker that exists but cannot decide for the candidate
VERIFIED = "verified"
FAILED = "failed"


def consent_aware(kind):
    """The ``IdentityChecker`` class as the engine calls it, ``IdentityChecker(original, allowed)``: without permission it is unavailable, as the real one is."""
    def build(_original, allowed):
        if not allowed:
            fake = checker(UNAVAILABLE)
            fake.reason = "Biometric processing is not permitted for this account, so identity was not checked."
            return fake
        return checker(kind)

    return build


def checker(kind):
    """A stand-in ``IdentityChecker`` for ``kind``."""
    fake = MagicMock()
    fake.threshold = 0.7
    if kind == UNAVAILABLE:
        fake.available, fake.threshold = False, None
        fake.reason = "The face is too dark to embed, so identity was not checked."
    else:
        fake.available, fake.reason = True, ""
        fake.check.return_value = {"status": kind, "similarity": 0.93 if kind == VERIFIED else (0.3 if kind == FAILED else None), "threshold": 0.7}
    return fake


@override_settings(**ALL_ON)
class EngineBase(SimpleTestCase):
    def run_engine(self, image, kind=UNAVAILABLE, consent=True, runners=None, face=True):
        service = AdaptiveImageEnhancementService(analyzer=analyzer_with() if face else analyzer_with(None), config=EngineConfig(),
                                                  runners=runners or {ops.LOW_LIGHT: runner_lift})
        with patch("enhancement.adaptive.service.IdentityChecker", side_effect=consent_aware(kind)):
            return service.enhance(image, allow_biometric_processing=consent)

    @staticmethod
    def identity(parameters):
        return parameters["identity_verification"]


class LimitedPolicyTests(EngineBase):
    """The default: what the engine has always done, now named. Nothing here changes a decision; it pins what is reported."""

    def test_the_default_policy_is_limited(self):
        self.assertEqual(identity_policy.LIMITED, "limited")
        from django.conf import settings

        self.assertEqual(settings.ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY, identity_policy.LIMITED)

    def test_identity_model_missing_enhances_without_claiming_verification(self):
        output, _level, parameters = self.run_engine(portrait(brightness=0.12), UNAVAILABLE)
        block = self.identity(parameters)
        self.assertIn(parameters["adaptive"]["outcome"], (ENHANCED, PARTIALLY_ENHANCED))
        self.assertFalse(np.array_equal(output, portrait(brightness=0.12)))
        self.assertEqual((block["mode"], block["state"], block["status"], block["verified"]), ("limited_unverified", "not_verifiable", "unavailable", False))
        self.assertEqual(parameters["identity_status"], "unavailable")
        self.assertIn("could not be verified", block["notice"])

    def test_consent_absent_enhances_without_claiming_verification_and_names_consent(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), UNAVAILABLE, consent=False)
        block = self.identity(parameters)
        self.assertEqual((block["mode"], block["verified"], block["status"]), ("limited_unverified", False, "unavailable"))
        self.assertIn("not permitted", block["reason"])

    def test_an_inconclusive_comparison_is_uncertain_never_verified(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), INCONCLUSIVE)
        block = self.identity(parameters)
        self.assertEqual((block["mode"], block["state"], block["status"], block["verified"]), ("limited_unverified", "uncertain", "inconclusive", False))
        self.assertTrue(block["notice"])

    def test_a_comparison_that_held_is_the_only_way_to_be_verified(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), VERIFIED)
        block = self.identity(parameters)
        self.assertEqual((block["mode"], block["state"], block["verified"], block["similarity"]), ("verified", "verified", True, 0.93))

    def test_a_failed_comparison_keeps_the_original_in_every_mode(self):
        source = portrait(brightness=0.12)
        for policy in (identity_policy.LIMITED, identity_policy.FAIL_CLOSED):
            with self.subTest(policy), override_settings(ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=policy):
                output, _level, parameters = self.run_engine(source, FAILED)
                np.testing.assert_array_equal(output, source)
                self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
                self.assertTrue(any("identity_changed" in (stage.get("rejection_reasons") or []) for stage in parameters["adaptive"]["stages"]))

    def test_no_face_means_nothing_to_verify_and_no_blame(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), UNAVAILABLE, face=False)
        block = self.identity(parameters)
        self.assertEqual((block["mode"], block["state"], block["verified"]), ("not_applicable", "not_applicable", False))
        self.assertNotIn("permitted", block["reason"])


@override_settings(ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY="fail_closed")
class FailClosedPolicyTests(EngineBase):
    def assert_kept(self, source, output, parameters, runner_calls=None):
        np.testing.assert_array_equal(output, source)                                         # the original, byte for byte
        self.assertEqual(parameters["adaptive"]["outcome"], ORIGINAL_PRESERVED)
        self.assertEqual(parameters["adaptive"]["operations_applied"], [])
        self.assertEqual(parameters["algorithm"], "adaptive_v1+original_preserved")
        block = parameters["identity_verification"]
        self.assertEqual((block["mode"], block["verified"]), ("failed_closed", False))
        self.assertIn("left unchanged", block["notice"])
        self.assertIn("Identity could not be verified", parameters["adaptive"]["summary"])
        self.assertNotIn("enhanced", parameters["adaptive"]["summary"].lower().replace("enhancement", ""))
        if runner_calls is not None:
            self.assertEqual(runner_calls, [])                                                 # no model ran at all

    def test_consent_absent_keeps_the_original_and_runs_no_model(self):
        calls = []
        source = portrait(brightness=0.12)

        def lift(image, engine, variant):
            calls.append(1)
            return runner_lift(image, engine, variant)

        output, _level, parameters = self.run_engine(source, UNAVAILABLE, consent=False, runners={ops.LOW_LIGHT: lift})
        self.assert_kept(source, output, parameters, calls)
        self.assertEqual(parameters["identity_status"], "unavailable")
        self.assertIn("not permitted", parameters["identity_verification"]["reason"])

    def test_identity_model_missing_keeps_the_original(self):
        calls = []
        source = portrait(brightness=0.12)
        output, _level, parameters = self.run_engine(source, UNAVAILABLE, runners={ops.LOW_LIGHT: lambda *a: calls.append(1)})
        self.assert_kept(source, output, parameters, calls)

    def test_a_face_too_dark_to_embed_keeps_the_original(self):
        """The case the default policy lets through: the lift is acceptable to every other gate, but the face cannot be compared."""
        source = portrait(brightness=0.12)
        output, _level, parameters = self.run_engine(source, UNAVAILABLE)
        self.assert_kept(source, output, parameters)
        skipped = parameters["adaptive"]["operations_skipped"]
        self.assertTrue(any(item.get("policy") == "identity_fail_closed" for item in skipped))

    def test_an_inconclusive_comparison_rejects_the_candidate_and_keeps_the_original(self):
        source = portrait(brightness=0.12)
        output, _level, parameters = self.run_engine(source, INCONCLUSIVE)
        self.assert_kept(source, output, parameters)
        self.assertTrue(any("identity_unverifiable" in (stage.get("rejection_reasons") or []) for stage in parameters["adaptive"]["stages"]))

    def test_a_verified_identity_is_not_blocked(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), VERIFIED)
        self.assertIn(parameters["adaptive"]["outcome"], (ENHANCED, PARTIALLY_ENHANCED))
        self.assertEqual(parameters["identity_verification"]["mode"], "verified")

    def test_a_photograph_without_a_face_is_not_blocked(self):
        _out, _level, parameters = self.run_engine(portrait(brightness=0.12), UNAVAILABLE, face=False)
        self.assertIn(parameters["adaptive"]["outcome"], (ENHANCED, PARTIALLY_ENHANCED))
        self.assertEqual(parameters["identity_verification"]["mode"], "not_applicable")

    def test_a_photograph_that_needs_nothing_is_not_blamed_on_identity(self):
        source = portrait()
        output, _level, parameters = self.run_engine(source, UNAVAILABLE)
        np.testing.assert_array_equal(output, source)
        self.assertIn("did not need", parameters["adaptive"]["summary"])
        self.assertEqual(parameters["identity_verification"]["mode"], "not_applicable")

    def test_the_validator_alone_also_refuses_an_unverified_candidate(self):
        """Defence in depth: even if the engine did not short-circuit, the identity gate rejects."""
        analyzer = analyzer_with()
        parent = portrait(brightness=0.12)
        report = analyzer.analyze(parent)
        from .test_adaptive_engine import exposure_lift

        candidate = exposure_lift(parent, 5.0)
        unavailable = FinalImageQualityValidator(analyzer, report).evaluate(parent, candidate, "low_light_enhancement", report)
        self.assertFalse(unavailable["accepted"])
        self.assertIn("identity_unverifiable", unavailable["reasons"])
        with override_settings(ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY="limited"):
            limited = FinalImageQualityValidator(analyzer, report).evaluate(parent, candidate, "low_light_enhancement", report)
        self.assertNotIn("identity_unverifiable", limited["reasons"])


class NeverClaimedWhenUnverifiedTests(EngineBase):
    """The invariant, over every combination: ``verified`` is true only for a comparison that held (or an untouched photograph)."""

    def test_verified_is_true_only_when_a_comparison_held(self):
        for kind, consent, policy in product((UNAVAILABLE, INCONCLUSIVE, VERIFIED, FAILED), (True, False), ("limited", "fail_closed")):
            with self.subTest(kind=kind, consent=consent, policy=policy), override_settings(ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=policy):
                _out, _level, parameters = self.run_engine(portrait(brightness=0.12), kind, consent=consent)
                block = parameters["identity_verification"]
                applied = parameters["adaptive"]["operations_applied"]
                self.assertIn(block["mode"], identity_policy.MODES)
                if applied:
                    self.assertEqual(block["verified"], block["state"] == "verified")
                    self.assertEqual(block["mode"] == "verified", block["state"] == "verified")
                    if kind != VERIFIED or not consent:
                        self.assertNotEqual(block["state"], "verified")
                        self.assertEqual(block["mode"], "limited_unverified")
                else:
                    self.assertNotEqual(block["mode"], "verified")

    def test_the_report_function_labels_every_branch(self):
        from types import SimpleNamespace

        faces = SimpleNamespace(faces=[object()])
        none = SimpleNamespace(faces=[])
        identity = lambda available, reason="r": SimpleNamespace(available=available, reason=reason, threshold=0.7 if available else None)  # noqa: E731
        cases = {
            "no face": (identity_report(none, identity(False), [ops.LOW_LIGHT], None, True), "not_applicable"),
            "unchanged": (identity_report(faces, identity(True), [], None, True), "not_applicable"),
            "unavailable": (identity_report(faces, identity(False), [ops.LOW_LIGHT], None, True), "limited_unverified"),
            "no consent": (identity_report(faces, identity(False), [ops.LOW_LIGHT], None, False), "limited_unverified"),
            "inconclusive": (identity_report(faces, identity(True), [ops.DENOISE], {"status": "inconclusive", "similarity": None}, True), "limited_unverified"),
            "verified": (identity_report(faces, identity(True), [ops.LOW_LIGHT], {"status": "verified", "similarity": 0.9}, True), "verified"),
            "blocked, no model": (identity_report(faces, identity(False), [], None, True, blocked=True), "failed_closed"),
            "blocked, no consent": (identity_report(faces, identity(False), [], None, False, blocked=True), "failed_closed"),
            "blocked, inconclusive": (identity_report(faces, identity(True), [], None, True, blocked=True), "failed_closed"),
        }
        for name, (block, mode) in cases.items():
            with self.subTest(name):
                self.assertEqual(block["mode"], mode)
                if mode in ("limited_unverified", "failed_closed"):
                    self.assertFalse(block["verified"])
        self.assertIn("not permitted", cases["blocked, no consent"][0]["reason"])


@override_settings(**ALL_ON, MEDIA_ROOT=tempfile.mkdtemp(), CELERY_TASK_ALWAYS_EAGER=True, BIOMETRIC_CONSENT_REQUIRED=True)
class JobConsentTests(ResultFixture):
    """Through the real job code and the API: consent present or absent, identity checker available or not."""

    make_from = engine_tests.AdaptiveJobTests.make_from

    def finish(self, image, kind):
        from contextlib import ExitStack

        from .services import process_enhancement_result

        result = self.make_from(image)
        with ExitStack() as stack:
            stack.enter_context(stub_analyzer_service()[0])
            stack.enter_context(patch("enhancement.adaptive.service.IdentityChecker", side_effect=consent_aware(kind)))
            stack.enter_context(patch("enhancement.adaptive.operations.run_low_light", runner_lift_op))
            return process_enhancement_result(result)

    def body(self, result):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.user)
        return client.get(f"/api/enhancements/results/{result.pk}/").json()

    def test_without_consent_the_api_never_says_verified(self):
        withdraw_face_consent(self.user)
        result = self.finish(portrait(brightness=0.12), VERIFIED)           # a checker that would say yes is not used without consent
        body = self.body(result)
        self.assertEqual(body["identity_status"], "unavailable")
        self.assertEqual(body["applied_parameters"]["identity_verification"]["mode"], "limited_unverified")
        self.assertFalse(body["applied_parameters"]["identity_verification"]["verified"])
        self.assertIn("not permitted", body["applied_parameters"]["identity_verification"]["reason"])

    def test_with_consent_and_a_working_checker_the_api_says_verified(self):
        grant_face_consent(self.user)
        result = self.finish(portrait(brightness=0.12), VERIFIED)
        body = self.body(result)
        self.assertEqual(body["identity_status"], "verified")
        self.assertEqual(body["applied_parameters"]["identity_verification"]["mode"], "verified")

    def test_with_consent_but_no_identity_model_the_api_does_not_say_verified(self):
        grant_face_consent(self.user)
        body = self.body(self.finish(portrait(brightness=0.12), UNAVAILABLE))
        self.assertEqual(body["identity_status"], "unavailable")
        self.assertEqual(body["applied_parameters"]["identity_verification"]["mode"], "limited_unverified")

    @override_settings(ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY="fail_closed")
    def test_fail_closed_stores_the_original_and_says_why(self):
        withdraw_face_consent(self.user)
        source = portrait(brightness=0.12)
        result = self.finish(source, UNAVAILABLE)
        body = self.body(result)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["enhancement_outcome"], "original_preserved")
        self.assertIn("Identity could not be verified", body["enhancement_summary"])
        self.assertEqual(body["applied_parameters"]["identity_verification"]["mode"], "failed_closed")
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.user)
        download = client.get(f"/api/enhancements/results/{result.pk}/download/")
        self.assertEqual(download.status_code, 200)
        import cv2

        decoded = cv2.imdecode(np.frombuffer(b"".join(download.streaming_content), np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(decoded, source)


def runner_lift_op(image, engine, blend):
    """The patched ``operations.run_low_light`` signature (image, engine, blend)."""
    from .test_adaptive_engine import exposure_lift

    return exposure_lift(image, 1.0 + 4.0 * blend), [{"model": engine, "device": "stand-in"}]
