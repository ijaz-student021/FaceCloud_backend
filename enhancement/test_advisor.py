"""The optional cloud adjustment advisor.

*** Every OpenAI reply in this file is MOCKED. No test here contacts OpenAI, and
*** none needs a key: ``socket.connect`` is blocked while they run, so a real
*** request would fail the test. Real-API validation is a separate, paid,
*** explicitly authorised step (``manage.py evaluate_advisor``), not part of the suite.
"""
import base64
import io
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import openai
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from privacy.models import BiometricConsent, ConsentPurpose
from uploads.models import UploadBatch

from . import advisor_client, advisor_service as service
from .advisor_schema import (
    CAUTION_FLAGS, INTENT_LOCKS, InvalidAdvice, build_instructions, build_user_text, response_schema, validate_advice,
)
from .controls import CONTROL_NAMES, CONTROLS
from .editor_source import source_version
from .models import (
    AdvisorApiCall, AdvisorConsent, AdvisorRecommendation, AdvisorRun, AdvisorStatus, ImageEditVersion,
)
from .services import process_enhancement_result
from .tasks import advisor_recommend, cleanup_advisor_records
from .test_pipeline_regressions import NO_MODELS, ResultFixture, textured
from .test_previews import PreviewFixture

ADVISOR = dict(
    OPENAI_ADVISOR_ENABLED=True, OPENAI_API_KEY="sk-test-NOT-A-REAL-KEY", OPENAI_ADVISOR_MODEL="mock-vision-model",
    OPENAI_ADVISOR_MAX_ATTEMPTS=3, OPENAI_ADVISOR_MAX_CONCURRENCY=2,
)
REQUEST = SimpleNamespace(method="POST", url="https://api.openai.com/v1/responses")  # stand-in for the SDK's request object


def zero(**values):
    return {**{name: 0 for name in CONTROL_NAMES}, **values}


def reply_object(payload=None, *, text=None, status="completed", parts=None, tokens=(320, 70)):
    """What the SDK returns, as a plain object (MOCKED)."""
    if parts is None:
        parts = [SimpleNamespace(type="output_text", text=text if text is not None else json.dumps(payload))]
    return SimpleNamespace(
        status=status, output=[SimpleNamespace(type="message", content=parts)],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def advice(action="apply", rationale="Highlights are slightly clipped.", flags=(), **adjustments):
    return {"action": action, "adjustments": zero(**adjustments), "rationale": rationale, "caution_flags": list(flags)}


def api_error(cls, status, body=None, headers=None):
    response = SimpleNamespace(status_code=status, headers=headers or {}, request=REQUEST)
    return cls("mock provider error", response=response, body=body)


class MockProvider:
    """Stands in for ``openai.OpenAI`` (MOCKED). Records every request; replies are consumed in order, the last repeats."""

    def __init__(self, *replies, hook=None, delay=0.0):
        self.replies = list(replies) or [reply_object(advice())]
        self.calls = []
        self.hook = hook
        self.delay = delay
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        try:
            if self.hook:
                self.hook(kwargs)
            if self.delay:
                time.sleep(self.delay)
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            with self._lock:
                self.in_flight -= 1


class AdvisorFixture(PreviewFixture):
    """Own storage, no real queue, the advisor on, and no network at all."""

    def setUp(self):
        super().setUp()
        cache.clear()
        override = override_settings(**ADVISOR)
        override.enable()
        self.addCleanup(override.disable)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network access attempted"))
        guard.start()
        self.addCleanup(guard.stop)
        queued = patch("enhancement.tasks.advisor_recommend.apply_async")
        self.advisor_queue = queued.start()
        self.addCleanup(queued.stop)
        self.provider = None
        self.savepoints = len(connection.savepoint_ids)

    def use(self, provider):
        self.provider = provider
        patcher = patch("enhancement.advisor_client.build_client", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def consent(self, user=None, granted=True):
        return service.record_consent(user or self.user, granted, settings.OPENAI_ADVISOR_POLICY_VERSION)

    def ask(self, result=None, intent="auto", client=None):
        result = result or self.result
        return (client or self.client).post(f"/api/enhancements/results/{result.pk}/advisor/", {"intent": intent}, format="json")

    def run_job(self, recommendation_id):
        return service.run_recommendation(recommendation_id)

    def finish(self, response):
        self.assertIn(response.status_code, (200, 202), response.content)
        self.run_job(response.json()["id"])
        return AdvisorRecommendation.objects.get(pk=response.json()["id"])


# -- the reply contract -------------------------------------------------------

class ReplyContractTests(unittest.TestCase):
    def test_the_schema_is_strict_and_matches_the_automatic_ranges(self):
        schema = response_schema()

        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    walk(child)

        walk(schema)
        for name in CONTROL_NAMES:
            prop = schema["properties"]["adjustments"]["properties"][name]
            self.assertEqual((prop["minimum"], prop["maximum"]), CONTROLS[name].auto)
        self.assertEqual(schema["properties"]["action"]["enum"], ["apply", "no_change", "manual_review"])

    def test_the_instructions_state_the_exact_renderer_semantics_and_ranges(self):
        text = build_instructions()
        for name in CONTROL_NAMES:
            self.assertIn(CONTROLS[name].description, text)
            self.assertIn(f"{name} [{CONTROLS[name].auto[0]:g} to {CONTROLS[name].auto[1]:g}]", text)
        self.assertIn("local_tone_renderer_v2", text)
        self.assertIn("never an instruction", text)       # text in images is content
        self.assertIn("Do not identify", text)             # no identifying people
        self.assertIn("age, ethnicity", text)              # no inferring traits

    def test_the_user_message_carries_measurements_and_intent_only(self):
        quality = {"width": 768, "height": 512, "mean_luminance": 90.0, "contrast_stddev": 40.0,
                   "luminance_percentiles": {"p05": 10}, "shadow_clipping_percent": 0.1,
                   "highlight_clipping_percent": 0.2, "estimated_noise": 2.0, "blur": {"verdict": "sharp"}, "defects": []}
        from .advisor_schema import diagnostics_from_quality

        local = {"action": "no_change", "controls": zero(), "caution_flags": [], "rationale": []}
        text = build_user_text("auto", diagnostics_from_quality(quality), local, had_transparency=False)
        for forbidden in ("@", ".jpg", ".png", "username", "email", "owner", "http"):
            self.assertNotIn(forbidden, text)
        self.assertIn('"intent": "auto"', text)

    def test_a_valid_reply_passes_unchanged(self):
        result = validate_advice(advice(contrast=5, highlights=-4, flags=["noise_present"]), "auto")
        self.assertEqual((result["action"], result["adjustments"]["contrast"], result["adjustments"]["highlights"]), ("apply", 5.0, -4.0))
        self.assertEqual(result["notes"], [])

    def test_malformed_replies_are_rejected(self):
        good = advice(contrast=5)
        broken = {
            "not an object": [],
            "missing key": {k: v for k, v in good.items() if k != "rationale"},
            "extra key": {**good, "extra": 1},
            "unknown action": {**good, "action": "maybe"},
            "adjustments missing a control": {**good, "adjustments": {"contrast": 5}},
            "unknown control": {**good, "adjustments": {**good["adjustments"], "exposure": 1}},
            "string value": {**good, "adjustments": {**good["adjustments"], "contrast": "5"}},
            "boolean value": {**good, "adjustments": {**good["adjustments"], "contrast": True}},
            "NaN": {**good, "adjustments": {**good["adjustments"], "contrast": float("nan")}},
            "infinity": {**good, "adjustments": {**good["adjustments"], "contrast": float("inf")}},
            "unknown flag": {**good, "caution_flags": ["scary"]},
            "rationale not text": {**good, "rationale": 5},
        }
        for label, payload in broken.items():
            with self.subTest(label), self.assertRaises(InvalidAdvice):
                validate_advice(payload, "auto")
        with self.assertRaises(InvalidAdvice):
            validate_advice(good, "nonsense")

    def test_values_outside_the_automatic_range_are_clamped_and_noted(self):
        result = validate_advice(advice(contrast=90, highlights=-90, sharpness=-5), "auto")
        self.assertEqual(result["adjustments"]["contrast"], CONTROLS["contrast"].auto[1])
        self.assertEqual(result["adjustments"]["highlights"], CONTROLS["highlights"].auto[0])
        self.assertEqual(result["adjustments"]["sharpness"], CONTROLS["sharpness"].auto[0])
        self.assertEqual({note["control"] for note in result["notes"]}, {"contrast", "highlights", "sharpness"})

    def test_the_intent_locks_controls_it_may_not_move(self):
        for intent, locked in INTENT_LOCKS.items():
            reply = advice(brightness=10, shadows=10, contrast=5, highlights=-5, saturation=3, warmth=3, sharpness=8)
            result = validate_advice(reply, intent)
            for name in CONTROL_NAMES:
                with self.subTest(intent=intent, control=name):
                    if name in locked:
                        self.assertEqual(result["adjustments"][name], 0.0)
                    else:
                        self.assertNotEqual(result["adjustments"][name], 0.0)
        # the safe default never changes overall exposure
        self.assertEqual({"brightness", "shadows"}, set(INTENT_LOCKS["auto"]))

    def test_no_change_and_manual_review_carry_no_adjustments_and_empty_apply_becomes_no_change(self):
        for action in ("no_change", "manual_review"):
            result = validate_advice(advice(action=action, contrast=5), "auto")
            self.assertFalse(any(result["adjustments"].values()))
            self.assertEqual(result["action"], action)
        self.assertEqual(validate_advice(advice(action="apply"), "auto")["action"], "no_change")
        self.assertEqual(validate_advice(advice(action="apply", brightness=10), "auto")["action"], "no_change")  # locked to zero

    def test_the_rationale_is_cleaned_and_bounded(self):
        result = validate_advice(advice(contrast=3, rationale="line\x00one\n" + "x" * 1000), "auto")
        self.assertNotIn("\x00", result["rationale"])
        self.assertLessEqual(len(result["rationale"]), 280)


# -- the provider wrapper -----------------------------------------------------

class ProviderWrapperTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network access attempted"))
        guard.start()
        self.addCleanup(guard.stop)
        override = override_settings(**ADVISOR)
        override.enable()
        self.addCleanup(override.disable)

    def call(self, provider):
        return advisor_client.request_advice(provider, image_data_url="data:image/jpeg;base64,AAAA", user_text="data")

    def test_the_request_is_a_stored_false_structured_responses_call(self):
        provider = MockProvider(reply_object(advice(contrast=4)))
        reply = self.call(provider)
        sent = provider.calls[0]
        self.assertEqual(sent["model"], "mock-vision-model")
        self.assertIs(sent["store"], False)
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertIs(sent["text"]["format"]["strict"], True)
        self.assertEqual(sent["text"]["format"]["schema"], response_schema())
        self.assertEqual(sent["max_output_tokens"], settings.OPENAI_ADVISOR_MAX_OUTPUT_TOKENS)
        content = sent["input"][0]["content"]
        self.assertEqual([part["type"] for part in content], ["input_text", "input_image"])
        self.assertEqual(content[1]["detail"], settings.OPENAI_ADVISOR_IMAGE_DETAIL)
        self.assertEqual(sent["instructions"], build_instructions())
        self.assertEqual((reply.input_tokens, reply.output_tokens), (320, 70))
        self.assertNotIn("metadata", sent)
        self.assertNotIn("safety_identifier", sent)  # no account-derived identifier is sent
        self.assertNotIn("user", sent)

    def test_failures_are_mapped_without_keeping_provider_text(self):
        cases = [
            (openai.APITimeoutError(request=REQUEST), advisor_client.TransientProviderError, "timeout"),
            (openai.APIConnectionError(request=REQUEST), advisor_client.TransientProviderError, "connection_error"),
            (api_error(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"}, {"retry-after": "7"}),
             advisor_client.TransientProviderError, "rate_limited"),
            (api_error(openai.InternalServerError, 503), advisor_client.TransientProviderError, "provider_unavailable"),
            (api_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), advisor_client.ProviderError, "quota_exhausted"),
            (api_error(openai.AuthenticationError, 401), advisor_client.ProviderError, "not_authorized"),
            (api_error(openai.NotFoundError, 404), advisor_client.ProviderError, "model_unavailable"),
            (api_error(openai.BadRequestError, 400), advisor_client.ProviderError, "request_rejected"),
        ]
        for error, expected, reason in cases:
            with self.subTest(reason), self.assertRaises(expected) as caught:
                self.call(MockProvider(error))
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("mock provider error", str(caught.exception))
        with self.assertRaises(advisor_client.TransientProviderError) as caught:
            self.call(MockProvider(api_error(openai.RateLimitError, 429, {"code": "x"}, {"retry-after": "7"})))
        self.assertEqual(caught.exception.retry_after, 7.0)

    def test_refusals_incomplete_and_unparseable_replies_are_distinguished_and_still_report_tokens(self):
        with self.assertRaises(advisor_client.ReplyRefused) as caught:
            self.call(MockProvider(reply_object(parts=[SimpleNamespace(type="refusal", refusal="no")])))
        self.assertEqual(caught.exception.tokens, (320, 70))
        for reply, reason in ((reply_object(status="incomplete", parts=[]), "incomplete"),
                              (reply_object(text="not json"), "not_json"),
                              (reply_object(parts=[]), "empty"),
                              (reply_object(status="failed", parts=[]), "status_failed")):
            with self.subTest(reason), self.assertRaises(advisor_client.ReplyUnusable) as caught:
                self.call(MockProvider(reply))
            self.assertEqual(caught.exception.reason, reason)

    def test_availability_names_what_is_missing_and_never_the_key(self):
        cases = [
            (dict(OPENAI_ADVISOR_ENABLED=False), "disabled"),
            (dict(OPENAI_API_KEY=""), "not_configured"),
            (dict(OPENAI_ADVISOR_MODEL=""), "model_not_configured"),
        ]
        for overrides, reason in cases:
            with self.subTest(reason), override_settings(**overrides):
                self.assertEqual(advisor_client.availability(), {"available": False, "reason": reason})
                with patch("openai.OpenAI", side_effect=AssertionError("the SDK client must not be built")):
                    with self.assertRaises(advisor_client.AdvisorUnavailable):
                        advisor_client.build_client()
        self.assertTrue(advisor_client.availability()["available"])

    def test_the_client_is_built_without_sdk_retries_and_with_the_configured_timeout(self):
        with patch("openai.OpenAI") as factory:
            advisor_client.build_client()
        kwargs = factory.call_args.kwargs
        self.assertEqual((kwargs["max_retries"], kwargs["timeout"]), (0, settings.OPENAI_ADVISOR_TIMEOUT_SECONDS))


# -- the real SDK against a local scripted server --------------------------------------

@override_settings(**ADVISOR)
class RealSdkAgainstLocalServerTests(SimpleTestCase):
    """The REAL ``openai`` SDK, talking to a scripted server on 127.0.0.1 (not OpenAI, no key, no internet).

    This checks what a hand-made stand-in cannot: that the SDK accepts and serialises our exact
    parameters, sends the key only as a bearer token, and that its real exceptions and response
    objects are mapped correctly. It cannot show how the real model behaves.
    """

    def serve(self, *behaviours, **options):
        from .fake_openai import FakeResponsesServer

        server = FakeResponsesServer(list(behaviours) or None, **options).start()
        self.addCleanup(server.stop)
        override = override_settings(OPENAI_BASE_URL=server.base_url)
        override.enable()
        self.addCleanup(override.disable)
        return server

    def call(self):
        return advisor_client.request_advice(
            advisor_client.build_client(), image_data_url="data:image/jpeg;base64,AAAA", user_text="data")

    def test_the_sdk_sends_exactly_the_request_we_intend(self):
        server = self.serve()
        reply = self.call()
        self.assertEqual((reply.input_tokens, reply.output_tokens), (300, 60))
        self.assertEqual(reply.payload["action"], "apply")
        self.assertEqual(len(server.requests), 1)
        sent = server.requests[0]
        self.assertTrue(sent["path"].endswith("/responses"))
        self.assertEqual({k.lower(): v for k, v in sent["headers"].items()}["authorization"], "Bearer " + settings.OPENAI_API_KEY)
        body = sent["body"]
        self.assertEqual(body["model"], "mock-vision-model")
        self.assertIs(body["store"], False)
        self.assertEqual(body["max_output_tokens"], settings.OPENAI_ADVISOR_MAX_OUTPUT_TOKENS)
        self.assertEqual(body["instructions"], build_instructions())
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "facecloud_adjustment_suggestion",
                                                  "strict": True, "schema": response_schema()})
        parts = body["input"][0]["content"]
        self.assertEqual([part["type"] for part in parts], ["input_text", "input_image"])
        self.assertEqual(parts[1]["image_url"], "data:image/jpeg;base64,AAAA")
        self.assertEqual(parts[1]["detail"], settings.OPENAI_ADVISOR_IMAGE_DETAIL)
        self.assertEqual(set(body) - {"model", "store", "max_output_tokens", "instructions", "text", "input"}, set())

    def test_real_sdk_exceptions_are_mapped(self):
        cases = [
            ({"status": 429, "retry_after": "7", "code": "rate_limit_exceeded"}, advisor_client.TransientProviderError, "rate_limited"),
            ({"status": 429, "code": "insufficient_quota"}, advisor_client.ProviderError, "quota_exhausted"),
            ({"status": 401}, advisor_client.ProviderError, "not_authorized"),
            ({"status": 404}, advisor_client.ProviderError, "model_unavailable"),
            ({"status": 400}, advisor_client.ProviderError, "request_rejected"),
            ({"status": 500}, advisor_client.TransientProviderError, "provider_unavailable"),
            ({"status": 503}, advisor_client.TransientProviderError, "provider_unavailable"),
        ]
        for behaviour, expected, reason in cases:
            with self.subTest(reason, status=behaviour["status"]):
                server = self.serve(behaviour)
                with self.assertRaises(expected) as caught:
                    self.call()
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(len(server.requests), 1, "the SDK must not retry on its own")
        server = self.serve({"status": 429, "retry_after": "7", "code": "rate_limit_exceeded"})
        with self.assertRaises(advisor_client.TransientProviderError) as caught:
            self.call()
        self.assertEqual(caught.exception.retry_after, 7.0)

    def test_real_sdk_replies_refusals_and_truncation_are_recognised(self):
        self.serve({"refusal": "I can't help with that."})
        with self.assertRaises(advisor_client.ReplyRefused):
            self.call()
        self.serve({"incomplete": True})
        with self.assertRaises(advisor_client.ReplyUnusable) as caught:
            self.call()
        self.assertEqual(caught.exception.reason, "incomplete")
        self.serve({"text": "contrast +5, please"})
        with self.assertRaises(advisor_client.ReplyUnusable) as caught:
            self.call()
        self.assertEqual(caught.exception.reason, "not_json")

    def test_a_slow_server_times_out_and_a_closed_port_is_a_connection_error(self):
        with override_settings(OPENAI_ADVISOR_TIMEOUT_SECONDS=0.3):
            self.serve(delay=1.5)
            with self.assertRaises(advisor_client.TransientProviderError) as caught:
                self.call()
            self.assertEqual(caught.exception.reason, "timeout")
        server = self.serve()
        server.stop()
        with self.assertRaises(advisor_client.TransientProviderError) as caught:
            self.call()
        self.assertEqual(caught.exception.reason, "connection_error")


# -- consent, availability, ownership ------------------------------------------

class ConsentAndAvailabilityTests(AdvisorFixture):
    def test_nothing_is_sent_and_nothing_queued_when_the_advisor_is_off_or_unconfigured(self):
        provider = self.use(MockProvider())
        self.consent()
        for overrides, reason in ((dict(OPENAI_ADVISOR_ENABLED=False), "disabled"), (dict(OPENAI_API_KEY=""), "not_configured"),
                                  (dict(OPENAI_ADVISOR_MODEL=""), "model_not_configured")):
            with self.subTest(reason), override_settings(**overrides):
                response = self.ask()
                self.assertEqual(response.status_code, 503)
                self.assertEqual((response.json()["code"], response.json()["reason"]), ("advisor_unavailable", reason))
        self.assertEqual(provider.calls, [])
        self.assertFalse(AdvisorRecommendation.objects.exists())
        self.advisor_queue.assert_not_called()

    def test_the_default_configuration_is_off(self):
        # the shipped defaults, not this suite's overrides
        with override_settings(OPENAI_ADVISOR_ENABLED=False, OPENAI_API_KEY="", OPENAI_ADVISOR_MODEL=""):
            body = self.client.get("/api/enhancements/advisor/configuration/").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["unavailable_reason"], "disabled")

    def test_consent_is_required_and_is_a_separate_decision_from_identity_consent(self):
        self.use(MockProvider())
        BiometricConsent.objects.create(user=self.user, purpose=ConsentPurpose.FACE_IDENTITY, granted=True, policy_version="x")
        response = self.ask()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "advisor_consent_required")
        self.assertFalse(AdvisorRecommendation.objects.exists())
        # and the other way round
        self.consent()
        self.assertEqual(BiometricConsent.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.ask().status_code, 202)

    def test_a_grant_must_name_the_current_policy_and_a_new_policy_asks_again(self):
        posted = self.client.post("/api/enhancements/advisor/consent/", {"granted": True, "policy_version": "old"}, format="json")
        self.assertEqual(posted.status_code, 400)
        self.assertFalse(AdvisorConsent.objects.exists())
        ok = self.client.post("/api/enhancements/advisor/consent/",
                              {"granted": True, "policy_version": settings.OPENAI_ADVISOR_POLICY_VERSION}, format="json")
        self.assertTrue(ok.json()["granted"])
        with override_settings(OPENAI_ADVISOR_POLICY_VERSION="advisor_policy_v2"):
            state = self.client.get("/api/enhancements/advisor/consent/").json()
            self.assertFalse(state["granted"])
            self.assertTrue(state["needs_renewal"])

    def test_withdrawing_consent_cancels_queued_work_and_the_send_is_rechecked(self):
        provider = self.use(MockProvider())
        self.consent()
        queued = self.ask().json()["id"]
        self.consent(granted=False)
        self.assertEqual(AdvisorRecommendation.objects.get(pk=queued).status, AdvisorStatus.CANCELLED)
        # withdrawn after a job was queued but before the worker sends: nothing leaves, local fallback
        self.consent()
        second = self.ask().json()["id"]
        AdvisorConsent.objects.create(user=self.user, granted=False, policy_version=settings.OPENAI_ADVISOR_POLICY_VERSION)
        AdvisorRecommendation.objects.filter(pk=second).update(status=AdvisorStatus.QUEUED)
        self.run_job(second)
        row = AdvisorRecommendation.objects.get(pk=second)
        self.assertEqual(provider.calls, [])
        self.assertEqual((row.status, row.origin, row.fallback_reason), (AdvisorStatus.COMPLETED, "local_fallback", "consent_missing"))

    def test_configuration_describes_what_is_sent_and_never_reveals_the_key(self):
        self.consent()
        response = self.client.get("/api/enhancements/advisor/configuration/")
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["model"], "mock-vision-model")
        self.assertIn("store=false", body["disclosure"]["retention"])
        self.assertIn("30 days", body["disclosure"]["retention"])
        self.assertNotIn(settings.OPENAI_API_KEY, response.content.decode())
        self.assertNotIn("sk-", response.content.decode())
        self.assertEqual(body["locked_controls_by_intent"]["auto"], ["brightness", "shadows"])

    def test_ownership_and_authentication(self):
        self.use(MockProvider())
        self.consent()
        stranger = self.other_client()
        self.assertEqual(self.ask(client=stranger).status_code, 404)  # not their image, whatever their consent
        self.consent(User.objects.get(username="intruder"))
        self.assertEqual(self.ask(client=stranger).status_code, 404)  # not their image
        own = self.ask().json()["id"]
        for path in (f"/api/enhancements/advisor/recommendations/{own}/",):
            self.assertEqual(stranger.get(path).status_code, 404)
        for suffix in ("cancel/", "apply/"):
            self.assertEqual(stranger.post(f"/api/enhancements/advisor/recommendations/{own}/{suffix}").status_code, 404)
        self.assertEqual(APIClient().post(f"/api/enhancements/results/{self.result.pk}/advisor/", {}).status_code, 401)
        self.assertEqual(APIClient().get("/api/enhancements/advisor/configuration/").status_code, 401)
        run = stranger.post("/api/enhancements/advisor/runs/", {"result_ids": [self.result.pk]}, format="json").json()
        self.assertEqual(run["items"][0]["reason"], "not_found")
        self.assertEqual(stranger.get(f"/api/enhancements/advisor/runs/{run['run']['id']}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/enhancements/advisor/runs/{run['run']['id']}/").status_code, 404)


# -- one image -------------------------------------------------------------------

class SingleRecommendationTests(AdvisorFixture):
    def setUp(self):
        super().setUp()
        self.consent()

    def test_the_advisor_is_called_once_with_a_bounded_metadata_free_preview(self):
        provider = self.use(MockProvider(reply_object(advice(contrast=6, highlights=-4, rationale="Slightly flat."))))
        response = self.ask(intent="auto")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["status"], "queued")
        self.advisor_queue.assert_called_once()
        row = self.finish(response)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual((row.status, row.origin, row.action), (AdvisorStatus.COMPLETED, "openai", "apply"))
        self.assertEqual((row.adjustments["contrast"], row.adjustments["highlights"]), (6.0, -4.0))
        self.assertEqual((row.input_tokens, row.output_tokens, row.attempts), (320, 70, 1))
        self.assertEqual((row.model, row.policy_version, row.renderer_version), ("mock-vision-model", "advisor_policy_v1", "local_tone_renderer_v2"))
        self.assertEqual(row.source_version, source_version(self.result))

        content = provider.calls[0]["input"][0]["content"]
        data_url = content[1]["image_url"]
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        image = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
        self.assertLessEqual(max(image.size), settings.OPENAI_ADVISOR_IMAGE_EDGE)
        self.assertEqual(dict(image.getexif()), {})
        self.assertNotIn("icc_profile", image.info)
        sent = json.dumps(provider.calls[0], default=str)
        for private in (self.user.email, self.user.username, "big.jpg", self.result.output_name, "original", str(self.user.pk) + "/"):
            self.assertNotIn(private, sent)

    def test_the_ledger_records_the_paid_request_without_content(self):
        self.use(MockProvider(reply_object(advice(contrast=6))))
        self.finish(self.ask())
        call = AdvisorApiCall.objects.get()
        self.assertEqual((call.outcome, call.input_tokens, call.output_tokens, call.owner_id), ("ok", 320, 70, self.user.pk))

    def test_no_database_transaction_is_open_while_the_provider_is_called(self):
        seen = []
        self.use(MockProvider(reply_object(advice(contrast=6)), hook=lambda _: seen.append(len(connection.savepoint_ids))))
        self.finish(self.ask())
        self.assertEqual(seen, [self.savepoints])

    def test_an_identical_request_never_pays_twice(self):
        provider = self.use(MockProvider(reply_object(advice(contrast=6))))
        first = self.ask()
        again_active = self.ask()
        self.assertEqual((first.status_code, again_active.status_code), (202, 200))
        self.assertEqual(first.json()["id"], again_active.json()["id"])
        self.assertEqual(self.advisor_queue.call_count, 1)
        self.finish(first)
        done = self.ask()
        self.assertEqual((done.status_code, done.json()["id"]), (200, first.json()["id"]))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.advisor_queue.call_count, 1)

    def test_a_different_intent_while_one_is_running_is_refused_and_afterwards_allowed(self):
        self.use(MockProvider(reply_object(advice(contrast=6))))
        first = self.ask(intent="auto")
        self.assertEqual(self.ask(intent="brighten").status_code, 400)
        self.finish(first)
        self.assertEqual(self.ask(intent="brighten").status_code, 202)

    def test_an_unknown_intent_is_a_field_error(self):
        response = self.ask(intent="dramatic")
        self.assertEqual(response.status_code, 400)
        self.assertIn("intent", response.json())

    def test_a_duplicate_delivery_of_the_same_job_sends_one_request(self):
        provider = self.use(MockProvider(reply_object(advice(contrast=6))))
        recommendation_id = self.ask().json()["id"]
        row, _ = service._claim(recommendation_id, "someone-else")  # another worker is mid-job
        self.assertEqual(self.run_job(recommendation_id), "in_progress")
        self.assertEqual(provider.calls, [])

    def test_a_dark_photo_is_not_lifted_unless_the_person_asked(self):
        reply = advice(brightness=16, shadows=18, contrast=5)
        self.use(MockProvider(reply_object(reply)))
        row = self.finish(self.ask(intent="auto"))
        self.assertEqual((row.adjustments["brightness"], row.adjustments["shadows"]), (0.0, 0.0))
        kinds = {note["reason"] for note in row.guard["clamps_and_locks"]}
        self.assertIn("locked_by_intent_auto", kinds)
        self.use(MockProvider(reply_object(reply)))
        AdvisorRecommendation.objects.all().delete()
        lifted = self.finish(self.ask(intent="brighten"))
        self.assertGreater(lifted.adjustments["brightness"], 0)


# -- fallback ---------------------------------------------------------------------

class FallbackTests(AdvisorFixture):
    def setUp(self):
        super().setUp()
        self.consent()

    def assert_safe_fallback(self, row, reason, calls=None):
        self.assertEqual((row.status, row.origin, row.fallback_reason), (AdvisorStatus.COMPLETED, "local_fallback", reason))
        self.assertIn(row.action, ("apply", "no_change"))
        local = row.local_recipe
        self.assertEqual(row.adjustments, local["controls"])        # the conservative local recipe, nothing else
        for name in CONTROL_NAMES:                                    # and inside the automatic ranges
            low, high = CONTROLS[name].auto
            self.assertTrue(low <= row.adjustments[name] <= high)
        self.assertEqual(row.adjustments["brightness"], 0.0)

    def test_every_failure_ends_in_the_conservative_local_result(self):
        cases = {
            "refused": reply_object(parts=[SimpleNamespace(type="refusal", refusal="I can't help")]),
            "incomplete": reply_object(status="incomplete", parts=[]),
            "not_json": reply_object(text="sure! contrast +5"),
            "invalid_shape": reply_object({"action": "apply"}),
            "invalid_adjustments": reply_object({**advice(), "adjustments": {**zero(), "contrast": float("nan")}}),
            "provider_unavailable": api_error(openai.InternalServerError, 503),
            "not_authorized": api_error(openai.AuthenticationError, 401),
            "model_unavailable": api_error(openai.NotFoundError, 404),
            "quota_exhausted": api_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}),
            "timeout": openai.APITimeoutError(request=REQUEST),
        }
        for reason, reply in cases.items():
            with self.subTest(reason):
                AdvisorRecommendation.objects.all().delete()
                AdvisorApiCall.objects.all().delete()
                self.use(MockProvider(reply))
                with patch("enhancement.advisor_service.random.uniform", return_value=0.0), \
                        patch("enhancement.advisor_service._backoff", return_value=0.0):
                    response = self.ask()
                    row_id = response.json()["id"]
                    for _ in range(service.settings.OPENAI_ADVISOR_MAX_ATTEMPTS + 1):  # transient errors re-queue
                        try:
                            self.run_job(row_id)
                        except service.RetryLater:
                            continue
                        break
                row = AdvisorRecommendation.objects.get(pk=row_id)
                self.assert_safe_fallback(row, reason)
                self.assertNotIn("mock provider error", json.dumps(row.__dict__, default=str))

    def test_a_transient_error_is_retried_then_succeeds_and_each_attempt_is_counted(self):
        provider = self.use(MockProvider(api_error(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"}),
                                         reply_object(advice(contrast=6))))
        recommendation_id = self.ask().json()["id"]
        with patch("enhancement.advisor_service._backoff", return_value=3.0):
            with self.assertRaises(service.RetryLater) as wait:
                self.run_job(recommendation_id)
        self.assertEqual((wait.exception.countdown, wait.exception.reason), (3.0, "rate_limited"))
        self.assertEqual(AdvisorRecommendation.objects.get(pk=recommendation_id).status, AdvisorStatus.QUEUED)
        self.assertEqual(self.run_job(recommendation_id), "completed")
        row = AdvisorRecommendation.objects.get(pk=recommendation_id)
        self.assertEqual((row.origin, row.attempts, len(provider.calls)), ("openai", 2, 2))
        self.assertEqual(sorted(AdvisorApiCall.objects.values_list("outcome", flat=True)), ["ok", "rate_limited"])

    def test_retries_are_capped(self):
        provider = self.use(MockProvider(openai.APITimeoutError(request=REQUEST)))
        recommendation_id = self.ask().json()["id"]
        with patch("enhancement.advisor_service._backoff", return_value=0.0):
            outcomes = []
            for _ in range(10):
                try:
                    outcomes.append(self.run_job(recommendation_id))
                    break
                except service.RetryLater:
                    outcomes.append("retry")
        self.assertEqual(outcomes, ["retry", "retry", "completed"])
        self.assertEqual(len(provider.calls), settings.OPENAI_ADVISOR_MAX_ATTEMPTS)
        self.assert_safe_fallback(AdvisorRecommendation.objects.get(pk=recommendation_id), "timeout")

    def test_the_celery_task_requeues_a_waiting_job_and_reports_a_finished_one(self):
        from celery.exceptions import Retry

        self.use(MockProvider(openai.APIConnectionError(request=REQUEST), reply_object(advice(contrast=6))))
        recommendation_id = self.ask().json()["id"]
        with patch("enhancement.advisor_service._backoff", return_value=4.0), \
                patch.object(advisor_recommend, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                advisor_recommend.run(recommendation_id)
        self.assertEqual(retry.call_args.kwargs["countdown"], 4.0)
        self.assertEqual(advisor_recommend.run(recommendation_id), {"recommendation_id": recommendation_id, "status": "completed"})
        self.assertEqual(AdvisorRecommendation.objects.get(pk=recommendation_id).origin, "openai")

    def test_a_missing_configuration_at_run_time_falls_back_without_calling(self):
        provider = self.use(MockProvider())
        recommendation_id = self.ask().json()["id"]
        with override_settings(OPENAI_API_KEY=""):
            self.run_job(recommendation_id)
        self.assertEqual(provider.calls, [])
        self.assert_safe_fallback(AdvisorRecommendation.objects.get(pk=recommendation_id), "not_configured")

    def test_an_exhausted_budget_falls_back_without_calling(self):
        for label, overrides, seed in (
            ("user limit", dict(OPENAI_ADVISOR_DAILY_REQUESTS_PER_USER=2), dict(owner=self.user)),
            ("total requests", dict(OPENAI_ADVISOR_DAILY_REQUESTS_TOTAL=2), dict(owner=None)),
            ("total tokens", dict(OPENAI_ADVISOR_DAILY_TOKENS_TOTAL=1000), dict(owner=None, input_tokens=900, output_tokens=200)),
        ):
            with self.subTest(label):
                AdvisorRecommendation.objects.all().delete()
                AdvisorApiCall.objects.all().delete()
                for _ in range(2):
                    AdvisorApiCall.objects.create(**seed)
                provider = self.use(MockProvider())
                with override_settings(**overrides):
                    recommendation_id = self.ask().json()["id"]
                    self.run_job(recommendation_id)
                self.assertEqual(provider.calls, [])
                self.assert_safe_fallback(AdvisorRecommendation.objects.get(pk=recommendation_id), "budget_exhausted")

    def test_budgets_count_only_today(self):
        yesterday = AdvisorApiCall.objects.create(owner=self.user, input_tokens=10 ** 7)
        AdvisorApiCall.objects.filter(pk=yesterday.pk).update(started_at=timezone.now() - timedelta(days=2))
        self.assertFalse(service.budget_state(self.user)["exhausted"])

    def test_a_job_past_its_deadline_falls_back_without_calling(self):
        provider = self.use(MockProvider())
        recommendation_id = self.ask().json()["id"]
        AdvisorRecommendation.objects.filter(pk=recommendation_id).update(deadline_at=timezone.now() - timedelta(seconds=1))
        self.run_job(recommendation_id)
        self.assertEqual(provider.calls, [])
        self.assert_safe_fallback(AdvisorRecommendation.objects.get(pk=recommendation_id), "deadline_exceeded")

    def test_the_concurrency_limit_queues_the_job_instead_of_sending(self):
        provider = self.use(MockProvider())
        recommendation_id = self.ask().json()["id"]
        for index in range(settings.OPENAI_ADVISOR_MAX_CONCURRENCY):
            self.assertEqual(service.acquire_slot("busy"), index)
        with self.assertRaises(service.RetryLater):
            self.run_job(recommendation_id)
        self.assertEqual(provider.calls, [])
        self.assertEqual(AdvisorRecommendation.objects.get(pk=recommendation_id).status, AdvisorStatus.QUEUED)
        service.release_slot(0, "busy")
        self.assertEqual(self.run_job(recommendation_id), "completed")

    def test_an_unexpected_error_still_ends_in_a_safe_result(self):
        self.use(MockProvider(reply_object(advice(contrast=6))))
        recommendation_id = self.ask().json()["id"]
        with patch("enhancement.advisor_service.guard_suggestion", side_effect=RuntimeError("boom")):
            self.run_job(recommendation_id)
        self.assert_safe_fallback(AdvisorRecommendation.objects.get(pk=recommendation_id), "internal_error")


# -- local guards ---------------------------------------------------------------------

class GuardTests(AdvisorFixture):
    """The renderer's own quality guards, applied to what the advisor suggests."""

    def setUp(self):
        super().setUp()
        self.consent()

    def guard(self, image, adjustments, intent="brighten"):
        from .services import analyze_image_quality

        return service.guard_suggestion(image, analyze_image_quality(image), zero(**adjustments), intent)

    def test_a_suggestion_that_would_blow_the_highlights_is_cut_back_and_then_rejected(self):
        # most of the frame sits just below clipping, so any real lift clips it
        bright = np.full((480, 640, 3), 120, np.uint8)
        bright[:, :400] = (246, 247, 248)
        bright = cv2.GaussianBlur(bright, (0, 0), 3)
        final, info, rejected = self.guard(bright, {"brightness": 16})
        self.assertTrue(info["guard_reductions"], "the guard did not react to a clipping suggestion")
        self.assertIn("highlight_clipping_limited", info["safety_reasons"])
        self.assertLess(info["safety_strength"], 1.0)
        if rejected:
            self.assertEqual((final, rejected), (None, "guard_rejected"))
        else:
            self.assertLess(final["brightness"], 16.0 * 0.9)

    def test_a_rejected_suggestion_becomes_the_local_fallback_end_to_end(self):
        self.use(MockProvider(reply_object(advice(brightness=16, contrast=5))))
        recommendation_id = self.ask(intent="brighten").json()["id"]
        rejected = ({}, {"safety_strength": 0.2, "guard_reductions": [{"strength": 1.0}]}, "guard_rejected")
        with patch("enhancement.advisor_service.guard_suggestion", return_value=(None, rejected[1], "guard_rejected")):
            self.run_job(recommendation_id)
        row = AdvisorRecommendation.objects.get(pk=recommendation_id)
        self.assertEqual((row.origin, row.fallback_reason), ("local_fallback", "guard_rejected"))
        self.assertEqual(row.adjustments, row.local_recipe["controls"])
        self.assertEqual(row.guard["safety_strength"], 0.2)
        self.assertEqual(row.suggested["adjustments"]["brightness"], 16.0)  # what was suggested is kept for audit

    def test_sharpening_is_removed_where_noise_is_visible(self):
        rng = np.random.default_rng(3)
        noisy = np.clip(textured(480, 640).astype(np.int16) + rng.normal(0, 26, (480, 640, 3)), 0, 255).astype(np.uint8)
        final, info, rejected = self.guard(noisy, {"sharpness": 20, "contrast": 4}, "auto")
        self.assertEqual(rejected, "")
        self.assertEqual(final["sharpness"], 0.0)
        self.assertIn({"control": "sharpness", "reason": "noise_present"}, info["reductions"])
        self.assertNotEqual(final["contrast"], 0.0)

    def test_a_clean_image_keeps_a_modest_suggestion_untouched(self):
        final, info, rejected = self.guard(textured(480, 640), {"contrast": 6, "sharpness": 8}, "auto")
        self.assertEqual((rejected, info["safety_strength"], info["reductions"]), ("", 1.0, []))
        self.assertAlmostEqual(final["contrast"], 6.0, delta=0.5)


# -- stale recommendations --------------------------------------------------------------

class StaleRecommendationTests(AdvisorFixture):
    def setUp(self):
        super().setUp()
        self.consent()
        self.use(MockProvider(reply_object(advice(contrast=6, highlights=-4))))

    def done(self):
        return self.finish(self.ask())

    def apply(self, row):
        return self.client.post(f"/api/enhancements/advisor/recommendations/{row.pk}/apply/")

    def test_apply_creates_an_ordinary_edit_with_the_recommended_settings_and_provenance(self):
        row = self.done()
        response = self.apply(row)
        self.assertEqual(response.status_code, 202, response.content)
        edit = ImageEditVersion.objects.get(pk=response.json()["id"])
        self.assertEqual((edit.mode, edit.advisor_recommendation_id, edit.source_version), ("manual", row.pk, row.source_version))
        self.assertEqual(edit.requested_adjustments, row.adjustments)
        self.assertEqual(response.json()["advisor_recommendation"], row.pk)
        from .tasks import process_gallery_edit

        self.assertEqual(process_gallery_edit.run(edit.pk)["status"], "completed")
        self.assertEqual(self.client.get(f"/api/enhancements/edits/{edit.pk}/download/").status_code, 200)
        self.assertEqual(self.other_client().get(f"/api/enhancements/edits/{edit.pk}/download/").status_code, 404)

    def test_a_recommendation_is_not_applicable_until_it_is_finished(self):
        queued = self.ask().json()
        response = self.client.post(f"/api/enhancements/advisor/recommendations/{queued['id']}/apply/")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(queued["usable"])

    def test_cancelling_before_the_work_stops_it_and_blocks_applying(self):
        queued = self.ask().json()
        cancelled = self.client.post(f"/api/enhancements/advisor/recommendations/{queued['id']}/cancel/")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.run_job(queued["id"]), "cancelled")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.client.post(f"/api/enhancements/advisor/recommendations/{queued['id']}/apply/").status_code, 400)

    def test_cancelling_while_the_request_is_in_flight_discards_the_answer(self):
        recommendation_id = self.ask().json()["id"]
        self.provider.hook = lambda _: service.cancel_recommendation(AdvisorRecommendation.objects.get(pk=recommendation_id))
        self.assertEqual(self.run_job(recommendation_id), "discarded")
        row = AdvisorRecommendation.objects.get(pk=recommendation_id)
        self.assertEqual((row.status, row.adjustments), (AdvisorStatus.CANCELLED, {}))
        reason = service.staleness(row)
        self.assertEqual(reason, "cancelled")

    def test_a_manual_edit_made_after_the_request_makes_the_answer_unusable(self):
        recommendation_id = self.ask().json()["id"]
        self.provider.hook = lambda _: self.make_edit(self.result)  # the person saves an edit while the advisor thinks
        self.assertEqual(self.run_job(recommendation_id), "superseded")
        row = AdvisorRecommendation.objects.get(pk=recommendation_id)
        self.assertEqual((row.status, row.adjustments), (AdvisorStatus.SUPERSEDED, {}))
        self.assertEqual(self.client.post(f"/api/enhancements/advisor/recommendations/{recommendation_id}/apply/").status_code, 400)

    def test_an_edit_saved_after_the_answer_makes_it_stale(self):
        row = self.done()
        self.make_edit(self.result)
        response = self.apply(row)
        self.assertEqual((response.status_code, response.json()["code"], response.json()["reason"]), (409, "recommendation_stale", "edited_since"))
        self.assertEqual(self.client.get(f"/api/enhancements/advisor/recommendations/{row.pk}/").json()["unusable_reason"], "edited_since")

    def test_applying_twice_cannot_stack_the_same_advice(self):
        row = self.done()
        self.assertEqual(self.apply(row).status_code, 202)
        second = self.apply(row)
        self.assertEqual(second.status_code, 409)

    def test_a_replaced_source_makes_it_stale(self):
        row = self.done()
        from .models import ImageEnhancementResult

        ImageEnhancementResult.objects.filter(pk=self.result.pk).update(output_file_size=self.result.output_file_size + 1)
        response = self.apply(row)
        self.assertEqual((response.status_code, response.json()["reason"]), (409, "source_changed"))

    def test_an_expired_recommendation_is_not_applicable(self):
        row = self.done()
        AdvisorRecommendation.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.apply(row).json()["reason"], "expired")

    def test_advice_for_a_replaced_source_is_never_published(self):
        from .models import ImageEnhancementResult

        recommendation_id = self.ask().json()["id"]
        self.provider.hook = lambda _: ImageEnhancementResult.objects.filter(pk=self.result.pk).update(
            output_file_size=self.result.output_file_size + 5)
        self.assertEqual(self.run_job(recommendation_id), "superseded")

    def test_nothing_to_apply_is_a_clear_refusal(self):
        self.use(MockProvider(reply_object(advice(action="no_change", rationale="Looks fine."))))
        row = self.done()
        self.assertEqual((row.action, row.adjustments["contrast"]), ("no_change", 0.0))
        self.assertEqual(self.apply(row).status_code, 400)
        self.assertEqual(self.client.get(f"/api/enhancements/advisor/recommendations/{row.pk}/").json()["unusable_reason"], "nothing_to_apply")

    def test_a_manual_review_answer_is_reported_and_cannot_be_applied(self):
        self.use(MockProvider(reply_object(advice(action="manual_review", flags=["image_not_suitable_for_adjustment"],
                                                  rationale="This is a screenshot."))))
        row = self.done()
        self.assertEqual(row.action, "manual_review")
        self.assertIn("image_not_suitable_for_adjustment", row.caution_flags)
        self.assertEqual(self.apply(row).status_code, 400)


# -- several images ---------------------------------------------------------------------

class BatchTests(AdvisorFixture):
    def setUp(self):
        super().setUp()
        self.consent()
        self.results = [self.result] + [process_enhancement_result(self.make_result(f"p{n}.jpg", size=(300, 400))) for n in range(3)]

    def start(self, results=None, intent="auto"):
        ids = [result.pk for result in (results or self.results)]
        return self.client.post("/api/enhancements/advisor/runs/", {"result_ids": ids, "intent": intent}, format="json")

    def test_each_image_gets_its_own_independent_job_and_a_failure_does_not_spread(self):
        order = [result.pk for result in self.results]
        # jobs run in request order, so the mocked replies are consumed in the same order
        self.use(MockProvider(
            reply_object(advice(contrast=6)),
            api_error(openai.AuthenticationError, 401),
            reply_object(advice(highlights=-5)),
            reply_object(text="not json"),
        ))
        response = self.start()
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertEqual(self.advisor_queue.call_count, 4)
        self.assertEqual([item["state"] for item in body["items"]], ["queued"] * 4)
        ids = [item["recommendation"]["id"] for item in body["items"]]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual({AdvisorRecommendation.objects.get(pk=i).enhancement_result_id for i in ids}, set(order))
        for recommendation_id in ids:
            self.run_job(recommendation_id)
        run = self.client.get(f"/api/enhancements/advisor/runs/{body['run']['id']}/").json()
        self.assertEqual(run["status"], "completed")
        self.assertEqual((run["counts"]["total"], run["counts"]["from_advisor"], run["counts"]["local_fallback"]), (4, 2, 2))
        by_result = {row["enhancement_result"]: row for row in run["recommendations"]}
        self.assertEqual(by_result[order[0]]["origin"], "openai")
        self.assertEqual((by_result[order[1]]["origin"], by_result[order[1]]["fallback_reason"]), ("local_fallback", "not_authorized"))
        self.assertEqual(by_result[order[2]]["origin"], "openai")
        self.assertEqual((by_result[order[3]]["origin"], by_result[order[3]]["fallback_reason"]), ("local_fallback", "not_json"))

    def test_the_run_reports_skipped_images_and_reuses_what_exists(self):
        self.use(MockProvider(reply_object(advice(contrast=6))))
        first = self.ask(self.results[1])
        self.finish(first)
        pending = self.make_result("pending.jpg")  # still queued: not an enhanced image yet
        pending_id = pending.pk
        body = self.client.post("/api/enhancements/advisor/runs/",
                                {"result_ids": [self.results[0].pk, self.results[1].pk, 999999, pending_id], "intent": "auto"},
                                format="json").json()
        states = {item["result_id"]: item["state"] for item in body["items"]}
        self.assertEqual(states[self.results[0].pk], "queued")
        self.assertEqual(states[self.results[1].pk], "reused")
        self.assertEqual(states[999999], "skipped")
        self.assertEqual(states[pending_id], "skipped")
        self.assertEqual(self.advisor_queue.call_count, 2)  # the single earlier request + one new job

    def test_a_run_is_bounded(self):
        with override_settings(OPENAI_ADVISOR_MAX_IMAGES_PER_RUN=2):
            response = self.start()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AdvisorRun.objects.exists())
        self.assertEqual(self.client.post("/api/enhancements/advisor/runs/", {"result_ids": []}, format="json").status_code, 400)

    def test_a_run_requires_consent_and_a_configured_advisor(self):
        self.consent(granted=False)
        self.assertEqual(self.start().status_code, 403)
        self.consent()
        with override_settings(OPENAI_ADVISOR_ENABLED=False):
            self.assertEqual(self.start().status_code, 503)
        self.assertFalse(AdvisorRecommendation.objects.exists())

    def test_cancelling_a_run_cancels_the_images_not_yet_started_and_keeps_finished_ones(self):
        self.use(MockProvider(reply_object(advice(contrast=6))))
        body = self.start().json()
        ids = [item["recommendation"]["id"] for item in body["items"]]
        self.run_job(ids[0])
        cancelled = self.client.post(f"/api/enhancements/advisor/runs/{body['run']['id']}/cancel/").json()
        self.assertEqual(cancelled["status"], "cancelled")
        statuses = [AdvisorRecommendation.objects.get(pk=i).status for i in ids]
        self.assertEqual(statuses, ["completed", "cancelled", "cancelled", "cancelled"])
        self.assertEqual(self.run_job(ids[1]), "cancelled")
        self.assertEqual(len(self.provider.calls), 1)


class PrivacyTests(AdvisorFixture):
    def test_the_personal_data_export_includes_the_advisor_consent_history_and_suggestions_but_no_image_data(self):
        from privacy.services import build_privacy_export

        self.consent()
        self.use(MockProvider(reply_object(advice(contrast=6))))
        row = self.finish(self.ask())
        self.consent(granted=False)
        export = build_privacy_export(self.user)
        self.assertEqual([item["granted"] for item in export["external_ai_consents"]], [False, True])
        item = export["advisor_recommendations"][0]
        self.assertEqual((item["id"], item["origin"], item["input_tokens"]), (row.pk, "openai", 320))
        flat = json.dumps(export, default=str)
        self.assertNotIn("base64", flat)
        self.assertNotIn(settings.OPENAI_API_KEY, flat)

    def test_deleting_the_account_removes_every_advisor_record(self):
        self.consent()
        self.use(MockProvider(reply_object(advice(contrast=6))))
        self.finish(self.ask())
        self.user.delete()
        self.assertEqual(
            [AdvisorRecommendation.objects.count(), AdvisorConsent.objects.count(), AdvisorRun.objects.count()], [0, 0, 0])
        self.assertEqual(AdvisorApiCall.objects.filter(owner__isnull=False).count(), 0)  # the ledger keeps counts only


# -- housekeeping ---------------------------------------------------------------------

class HousekeepingTests(AdvisorFixture):
    def test_jobs_no_worker_finished_are_closed_and_old_records_deleted(self):
        self.consent()
        self.use(MockProvider())
        stuck = self.ask().json()["id"]
        AdvisorRecommendation.objects.filter(pk=stuck).update(
            deadline_at=timezone.now() - timedelta(seconds=settings.OPENAI_ADVISOR_DEADLINE_SECONDS * 3))
        old = AdvisorApiCall.objects.create(owner=self.user)
        AdvisorApiCall.objects.filter(pk=old.pk).update(started_at=timezone.now() - timedelta(days=settings.OPENAI_ADVISOR_RECORD_RETENTION_DAYS + 1))
        fresh = AdvisorApiCall.objects.create(owner=self.user)
        counts = cleanup_advisor_records()
        self.assertEqual(counts["closed"], 1)
        self.assertEqual(AdvisorRecommendation.objects.get(pk=stuck).status, AdvisorStatus.FAILED)
        self.assertEqual(list(AdvisorApiCall.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertEqual(self.ask().status_code, 202)  # the stuck job no longer blocks the image

    def test_the_cleanup_is_scheduled(self):
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE["cleanup-advisor-records"]["task"], "enhancement.cleanup_advisor")


# -- real concurrency on PostgreSQL -----------------------------------------------------

@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL (DB_ENGINE=django.db.backends.postgresql)")
@override_settings(**NO_MODELS, **ADVISOR)
class ConcurrentAdvisorTests(TransactionTestCase):
    """MOCKED provider, real threads, real PostgreSQL row locks and the shared concurrency slots."""

    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp(prefix="advisor-concurrent-")
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(
            username="advisor-load", email="advisor-load@example.com", password="ForestRiver!4821", is_email_verified=True)
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="multiple")
        service.record_consent(self.user, True, settings.OPENAI_ADVISOR_POLICY_VERSION)
        self.results = [process_enhancement_result(ResultFixture.make_result(self, f"c{n}.jpg", size=(240, 320))) for n in range(6)]
        patcher = patch("enhancement.tasks.advisor_recommend.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)

    def drive(self, recommendation_id):
        for _ in range(200):
            try:
                return service.run_recommendation(recommendation_id)
            except service.RetryLater as wait:
                time.sleep(min(wait.countdown, 0.05))
            finally:
                connection.close()
        raise AssertionError("job never finished")

    def test_the_provider_is_never_called_more_than_the_limit_at_once(self):
        provider = MockProvider(reply_object(advice(contrast=6)), delay=0.15)
        with patch("enhancement.advisor_client.build_client", return_value=provider):
            run, items = service.create_run(self.user, self.results, "auto")
            ids = [item["recommendation"].pk for item in items]
            threads = [threading.Thread(target=self.drive, args=(i,)) for i in ids]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
        self.assertLessEqual(provider.peak, settings.OPENAI_ADVISOR_MAX_CONCURRENCY)
        self.assertGreaterEqual(provider.peak, 2, "the test did not exercise concurrency")
        self.assertEqual(sorted(AdvisorRecommendation.objects.values_list("status", flat=True)), ["completed"] * 6)
        self.assertEqual(len(provider.calls), 6)

    def test_two_workers_given_the_same_job_send_one_request(self):
        provider = MockProvider(reply_object(advice(contrast=6)), delay=0.3)
        with patch("enhancement.advisor_client.build_client", return_value=provider):
            run, items = service.create_run(self.user, self.results[:1], "auto")
            recommendation_id = items[0]["recommendation"].pk
            outcomes = []
            threads = [threading.Thread(target=lambda: outcomes.append(self.drive(recommendation_id))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(AdvisorRecommendation.objects.get(pk=recommendation_id).attempts, 1)
        self.assertEqual(sorted(outcomes), ["completed", "in_progress"])
