"""The optional cloud enhancement (enhancement/cloud.py): the client, the checks, the stored record, and how the job code uses it.

The real client talks to a scripted stand-in for Deep Image on 127.0.0.1 (fake_deep_image.py): the real multipart upload, headers, redirects and error handling, with no account, no
key, no credit and no network. What these tests cannot show is how Deep Image itself behaves: no request has ever been made to it from this code. Face detection and the identity model
are stand-ins (the checks around them are what is under test); the SFace and YuNet models are exercised by the identity and adaptive tests.
"""
import io
import json
import socket
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch
from urllib import request as urllib_request

import cv2
import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from config.production_guard import DEEP_IMAGE_BASE_URL as GUARD_BASE_URL
from config.production_guard import production_problems
from config.test_production_guard import good_config, random_secret
from uploads.models import ImagePreprocessingResult, ImageUpload

from . import cloud
from .fake_deep_image import FakeDeepImageServer
from .models import EnhancementLevel, ImageEnhancementResult
from .restoration import AI_ENGINES, describe_stages, engine_kind, processing_summary
from .services import process_enhancement_result
from .test_adaptive_engine import ALL_ON, analyzer_with, portrait, stub_analyzer_service
from .test_pipeline_regressions import ResultFixture

KEY = "test-key-" + "k" * 30
SETTINGS = dict(
    CLOUD_ENHANCEMENT_ENABLED=True, DEEP_IMAGE_API_KEY=KEY, DEEP_IMAGE_PRESET="auto_enhance_pro", DEEP_IMAGE_POLL_SECONDS=0.01, DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS=3.0,
    DEEP_IMAGE_DEADLINE_SECONDS=10, DEEP_IMAGE_DAILY_REQUEST_LIMIT=0,
)
SOURCE = portrait()                                   # 480 wide, 640 high, with a face patch the stand-in detector reports at a fixed place


def enlarged(factor=2, image=SOURCE):
    return cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


class StubIdentity:
    """Stands in for ``IdentityChecker``: the upload's faces are compared with whatever it is told to answer."""

    def __init__(self, status="verified", similarity=0.93, available=True):
        self.status, self.similarity, self.available = status, similarity, available
        self.threshold, self.restore_threshold = 0.8, 0.65
        self.reason = "" if available else "No identity model is configured."
        self.restoring = []

    def check(self, candidate, restoring=False):
        self.restoring.append(restoring)
        return {"status": self.status, "similarity": self.similarity, "threshold": self.threshold}


class ShiftingAnalyzer:
    """The first analysis (the upload) finds the face where it is; every later one (the result) finds it ``dx`` pixels to the side, or not at all when ``dx`` is None."""

    def __init__(self, dx):
        self.before = analyzer_with()
        self.after = analyzer_with(None if dx is None else (150 + dx, 100, 180, 220))
        self.calls = 0

    def analyze(self, image):
        self.calls += 1
        return (self.before if self.calls == 1 else self.after).analyze(image)


class Interrupted(Exception):
    """What a job's checkpoint raises when the job is cancelled."""


class ServerCase(SimpleTestCase):
    def setUp(self):
        cache.clear()                                   # the daily counter lives in the cache

    def serve(self, behaviours=None, key=KEY, **overrides):
        server = FakeDeepImageServer(behaviours, key=key).start()
        self.addCleanup(server.stop)
        override = override_settings(**{**SETTINGS, "DEEP_IMAGE_BASE_URL": server.base_url, **overrides})
        override.enable()
        self.addCleanup(override.disable)
        return server

    def reason_of(self, call):
        with self.assertRaises(cloud.CloudDeclined) as caught:
            call()
        return caught.exception.reason


class ClientTests(ServerCase):
    def submit(self, client=None):
        return (client or cloud.DeepImageClient()).submit(cloud.encode_upload(SOURCE), "auto_enhance_pro")

    def test_the_upload_carries_the_key_a_jpeg_without_metadata_and_only_the_preset(self):
        server = self.serve()
        payload = cloud.encode_upload(SOURCE)
        job = cloud.DeepImageClient().submit(payload, "auto_enhance_pro")
        sent = server.uploads()[0]
        self.assertEqual(sent["path"], "/rest_api/process")
        self.assertEqual(sent["headers"]["x-api-key"], KEY)
        self.assertTrue(sent["headers"]["content-type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(server.parameters(sent["body"]), {"preset": "auto_enhance_pro"})
        self.assertIn(b'filename="photo.jpg"', sent["body"])
        self.assertEqual(server.upload_bytes(sent["body"]), payload)
        self.assertTrue(payload.startswith(b"\xff\xd8"))
        self.assertNotIn(b"Exif", payload)
        self.assertEqual(job, next(iter(server.jobs)))

    def test_waiting_polls_until_the_job_is_complete_and_returns_the_result_address(self):
        server = self.serve([{"polls": 2}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        seen = []
        url = client.wait(job, time.monotonic() + 5, seen.append)
        self.assertEqual(url, f"{server.origin}/files/{job}.jpg")
        self.assertEqual(seen, ["cloud_wait"] * 3)
        self.assertEqual([item["method"] for item in server.requests], ["POST", "GET", "GET", "GET"])
        self.assertTrue(all(item["headers"]["x-api-key"] == KEY for item in server.requests))

    def test_a_job_that_ends_in_a_failure_status_is_declined(self):
        for status in ("failed", "error", "cancelled", "canceled", "blocked", "expired"):
            with self.subTest(status):
                self.serve([{"status_after": status}])
                client = cloud.DeepImageClient()
                job = self.submit(client)
                self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 5, lambda _name: None)), "job_failed")

    def test_the_status_the_real_service_reports_while_it_works_is_waited_for(self):
        server = self.serve([{"polls": 3, "status_names": ["received", "predicting", "predicting"]}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        url = client.wait(job, time.monotonic() + 5, lambda _name: None)
        self.assertEqual(url, f"{server.origin}/files/{job}.jpg")
        self.assertEqual(len([item for item in server.requests if item["method"] == "GET"]), 4)

    def test_a_status_nobody_knows_is_waited_for_and_named_once_in_the_log(self):
        self.serve([{"polls": 3, "status_names": ["queued_for_gpu"]}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        with self.assertLogs("enhancement.cloud", level="WARNING") as logs:
            client.wait(job, time.monotonic() + 5, lambda _name: None)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("queued_for_gpu", logs.output[0])

    def test_a_status_nobody_knows_that_never_ends_is_given_up_at_the_deadline(self):
        self.serve([{"status_after": "somewhere_else"}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 0.15, lambda _name: None)), "deadline")

    def test_a_job_that_never_finishes_is_given_up_at_the_deadline(self):
        self.serve([{"never_done": True}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 0.15, lambda _name: None)), "deadline")

    def test_a_failed_status_request_is_tried_again_but_the_upload_never_is(self):
        server = self.serve([{"status_errors": 2}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        client.wait(job, time.monotonic() + 5, lambda _name: None)
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual(len([item for item in server.requests if item["method"] == "GET"]), 3)

    def test_status_requests_that_keep_failing_are_given_up_on(self):
        self.serve([{"status_errors": 50}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 5, lambda _name: None)), "provider_error")

    def test_every_refusal_of_the_upload_has_a_reason_and_is_not_repeated(self):
        for code, reason in ((401, "unauthorized"), (403, "unauthorized"), (402, "no_credits"), (429, "rate_limited"), (500, "provider_error"), (400, "rejected")):
            with self.subTest(code):
                server = self.serve([{"upload_status": code}])
                self.assertEqual(self.reason_of(self.submit), reason)
                self.assertEqual(len(server.uploads()), 1)

    def test_a_wrong_key_is_refused(self):
        self.serve(key="another-key")
        self.assertEqual(self.reason_of(self.submit), "unauthorized")

    def test_a_reply_that_is_not_json_is_declined(self):
        self.serve([{"bad_json": True}])
        self.assertEqual(self.reason_of(self.submit), "bad_reply")

    def test_a_key_is_used_without_the_spaces_around_it(self):
        self.assertEqual(cloud.DeepImageClient(api_key="  abc  ").api_key, "abc")
        with override_settings(DEEP_IMAGE_API_KEY=" from-settings\n"):
            self.assertEqual(cloud.DeepImageClient().api_key, "from-settings")

    def test_a_reply_without_a_job_is_declined(self):
        self.serve([{"no_job": True}])
        self.assertEqual(self.reason_of(self.submit), "bad_reply")

    def test_a_finished_job_without_a_result_address_is_declined(self):
        self.serve([{"no_result_url": True}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 5, lambda _name: None)), "bad_reply")

    def test_a_status_request_the_provider_refuses_is_not_tried_again(self):
        server = self.serve()
        client = cloud.DeepImageClient()
        self.assertEqual(self.reason_of(lambda: client.wait("deadbeef", time.monotonic() + 5, lambda _name: None)), "rejected")
        self.assertEqual(len([item for item in server.requests if item["method"] == "GET"]), 1)

    def test_a_closed_port_is_a_connection_error(self):
        self.serve().stop()
        self.assertEqual(self.reason_of(self.submit), "connection_error")

    def test_a_slow_server_times_out(self):
        self.serve([{"delay": 1.0}], DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS=0.2)
        self.assertEqual(self.reason_of(self.submit), "timeout")

    def test_the_result_is_fetched_without_the_key(self):
        server = self.serve()
        client = cloud.DeepImageClient()
        url = client.wait(self.submit(client), time.monotonic() + 5, lambda _name: None)
        data = client.download(url)
        fetched = server.requests[-1]
        self.assertTrue(fetched["path"].startswith("/files/"))
        self.assertNotIn("x-api-key", {name.lower() for name in fetched["headers"]})
        self.assertEqual(cloud.decode_result(data).shape[:2], (1280, 960))

    def test_a_result_one_redirect_away_is_followed_and_the_key_stays_home(self):
        server = self.serve([{"via_redirect": True}])
        client = cloud.DeepImageClient()
        data = client.download(client.wait(self.submit(client), time.monotonic() + 5, lambda _name: None))
        self.assertTrue(data.startswith(b"\xff\xd8"))
        hops = [item for item in server.requests if item["path"].startswith(("/hop/", "/files/"))]
        self.assertEqual(len(hops), 2)
        self.assertTrue(all("x-api-key" not in {name.lower() for name in item["headers"]} for item in hops))

    def test_a_redirect_on_a_request_that_carries_the_key_is_not_followed(self):
        other = FakeDeepImageServer().start()
        self.addCleanup(other.stop)
        self.serve([{"upload_redirect": other.origin + "/rest_api/process"}])
        self.assertEqual(self.reason_of(self.submit), "redirected")
        self.assertEqual(other.requests, [])

    def test_a_status_request_that_carries_the_key_is_not_redirected(self):
        other = FakeDeepImageServer().start()
        self.addCleanup(other.stop)
        self.serve([{"status_redirect": other.origin + "/rest_api/result/abc"}])
        client = cloud.DeepImageClient()
        job = self.submit(client)
        self.assertEqual(self.reason_of(lambda: client.wait(job, time.monotonic() + 5, lambda _name: None)), "redirected")
        self.assertEqual(other.requests, [])

    def test_a_result_that_is_too_big_is_declined(self):
        self.serve(DEEP_IMAGE_MAX_RESULT_BYTES=1000)
        client = cloud.DeepImageClient()
        url = client.wait(self.submit(client), time.monotonic() + 5, lambda _name: None)
        self.assertEqual(self.reason_of(lambda: client.download(url)), "result_too_large")

    def test_a_result_that_is_gone_is_declined(self):
        self.serve([{"result_status": 404}])
        client = cloud.DeepImageClient()
        url = client.wait(self.submit(client), time.monotonic() + 5, lambda _name: None)
        self.assertEqual(self.reason_of(lambda: client.download(url)), "rejected")

    def test_the_key_is_not_in_the_log_even_when_the_provider_repeats_it(self):
        self.serve([{"upload_status": 401, "echo_key": True}])
        with self.assertLogs("enhancement.cloud", level="DEBUG") as logs:
            self.assertEqual(self.reason_of(self.submit), "unauthorized")
        text = "\n".join(logs.output)
        self.assertNotIn(KEY, text)
        self.assertIn("[key]", text)

    def test_a_key_with_a_line_break_is_declined_not_sent(self):
        server = self.serve()
        client = cloud.DeepImageClient(api_key="abc\r\nInjected: header")
        self.assertEqual(self.reason_of(lambda: self.submit(client)), "bad_request")
        self.assertEqual(server.requests, [])

    def test_a_result_is_read_as_the_pixels_it_holds_whatever_its_orientation_tag_says(self):
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6                                                          # "rotate 90 degrees": a viewer would turn the picture, the pixels are what they are
        Image.fromarray(cv2.cvtColor(SOURCE, cv2.COLOR_BGR2RGB)).save(buffer, "JPEG", exif=exif)
        self.assertEqual(cloud.decode_result(buffer.getvalue()).shape[:2], SOURCE.shape[:2])

    def test_a_result_is_decoded_only_when_its_size_is_acceptable(self):
        payload = cv2.imencode(".jpg", SOURCE)[1].tobytes()
        self.assertEqual(cloud.decode_result(payload).shape, SOURCE.shape)
        self.assertEqual(self.reason_of(lambda: cloud.decode_result(b"this is not an image")), "bad_result")
        with override_settings(DEEP_IMAGE_MAX_RESULT_PIXELS=10):
            self.assertEqual(self.reason_of(lambda: cloud.decode_result(payload)), "result_too_large")


class ResultAddressTests(SimpleTestCase):
    def test_the_real_provider_only_gets_https_addresses_that_are_not_private(self):
        for url in ("https://s3.eu-central-1.amazonaws.com/deep-image.ai/x.jpg", "https://deep-image.ai/files/x.jpg", "https://93.184.216.34/x.jpg"):
            with self.subTest(url):
                cloud.check_result_url(url, cloud.DEFAULT_BASE_URL)
        for url in ("http://deep-image.ai/x.jpg", "https://127.0.0.1/x.jpg", "https://10.0.0.5/x.jpg", "https://169.254.169.254/latest", "https://[::1]/x.jpg", "https://localhost/x.jpg",
                    "https://printer.local/x.jpg", "ftp://deep-image.ai/x", "https:///x.jpg", "not a url", ""):
            with self.subTest(url), self.assertRaises(cloud.CloudDeclined):
                cloud.check_result_url(url, cloud.DEFAULT_BASE_URL)

    def test_every_hop_of_a_result_redirect_is_checked_like_the_first(self):
        handler = cloud._CheckedRedirect(cloud.DEFAULT_BASE_URL)
        request = urllib_request.Request("https://deep-image.ai/files/a.jpg")
        for target in ("https://127.0.0.1/x.jpg", "http://deep-image.ai/x.jpg", "https://10.1.2.3/x.jpg"):
            with self.subTest(target), self.assertRaises(cloud.CloudDeclined):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        hop = handler.redirect_request(request, None, 302, "Found", {}, "https://s3.eu-central-1.amazonaws.com/deep-image.ai/b.jpg")
        self.assertEqual(hop.full_url, "https://s3.eu-central-1.amazonaws.com/deep-image.ai/b.jpg")
        self.assertEqual(handler.max_redirections, 3)

    def test_a_test_server_may_be_plain_http_on_this_machine_but_nothing_else_goes_through(self):
        cloud.check_result_url("http://127.0.0.1:9/x.jpg", "http://127.0.0.1:9/rest_api")
        with self.assertRaises(cloud.CloudDeclined):
            cloud.check_result_url("ftp://127.0.0.1/x.jpg", "http://127.0.0.1:9/rest_api")


class JudgeTests(SimpleTestCase):
    def judge(self, result, analyzer=None, identity=None, source=SOURCE):
        analyzer = analyzer or analyzer_with()
        identity = identity or StubIdentity()
        checkable = cloud._checkable_faces(analyzer.analyze(source))
        return cloud.judge(source, result, identity, analyzer, checkable), identity

    def declined(self, result, **kwargs):
        with self.assertRaises(cloud.CloudDeclined) as caught:
            self.judge(result, **kwargs)
        return caught.exception.reason

    def test_a_faithful_result_passes_and_reports_what_was_read(self):
        ((readings, small, report_after, checked), identity) = self.judge(enlarged())
        self.assertEqual(readings["upload_size"], [480, 640])
        self.assertEqual(readings["result_size"], [960, 1280])
        self.assertLess(readings["aspect_error"], 0.001)
        self.assertGreater(readings["structure_ssim"], 0.9)
        self.assertEqual(readings["landmark_shift"], 0.0)
        self.assertEqual(readings["identity"], {"status": "verified", "similarity": 0.93, "threshold": 0.8})
        self.assertEqual(small.shape, SOURCE.shape)
        self.assertEqual(len(report_after.faces), 1)
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(identity.restoring, [False])           # a whole-photograph enhancement is held to the guard limit, not the face-restoration one

    def test_a_result_at_the_uploads_own_size_is_judged_as_it_is(self):
        result = SOURCE.copy()
        (readings, small, _after, _checked), _identity = self.judge(result)
        self.assertIs(small, result)                                          # no resize when the grids already agree
        self.assertEqual(readings["result_size"], [480, 640])

    def test_a_changed_aspect_ratio_is_declined(self):
        self.assertEqual(self.declined(cv2.resize(SOURCE, (960, 1100))), "aspect_changed")

    def test_a_rounded_pixel_is_not_a_changed_aspect_ratio(self):
        self.judge(cv2.resize(SOURCE, (960, 1281)))

    def test_a_result_much_narrower_than_the_upload_is_declined(self):
        self.assertEqual(self.declined(cv2.resize(SOURCE, (200, 267))), "result_too_small")

    def test_a_result_half_as_wide_is_still_accepted_as_a_downscale(self):
        self.judge(cv2.resize(SOURCE, (240, 320)))

    def test_a_flat_result_is_declined_but_a_flat_upload_is_not_blamed_for_it(self):
        flat = np.full((1280, 960, 3), 128, dtype=np.uint8)
        self.assertEqual(self.declined(flat), "result_flat")
        self.judge(flat, analyzer=analyzer_with(None), source=np.full((640, 480, 3), 128, dtype=np.uint8))

    def test_a_recomposed_result_is_declined(self):
        noise = np.random.default_rng(1).integers(0, 255, (1280, 960, 3), dtype=np.uint8)
        self.assertEqual(self.declined(noise), "structure_changed")

    def test_the_structure_limit_is_the_setting(self):
        blurred = cv2.GaussianBlur(enlarged(), (0, 0), 6)
        with override_settings(CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM=0.0):
            self.judge(blurred)
        with override_settings(CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM=1.0):
            self.assertEqual(self.declined(blurred), "structure_changed")

    def test_a_face_that_is_gone_is_declined(self):
        self.assertEqual(self.declined(enlarged(), analyzer=ShiftingAnalyzer(None)), "face_lost")

    def test_a_face_that_moved_is_declined_and_detector_jitter_is_not(self):
        self.assertEqual(self.declined(enlarged(), analyzer=ShiftingAnalyzer(20)), "face_geometry_changed")
        self.judge(enlarged(), analyzer=ShiftingAnalyzer(2))

    def test_the_landmark_limit_is_the_one_an_enlargement_is_held_to(self):
        self.judge(enlarged(), analyzer=ShiftingAnalyzer(9))                  # 0.05 of the face width: over the 0.04 of a same-size step, under the 0.06 of an enlargement
        self.assertEqual(self.declined(enlarged(), analyzer=ShiftingAnalyzer(12)), "face_geometry_changed")        # 0.067

    def test_a_face_that_is_not_the_same_person_is_declined(self):
        self.assertEqual(self.declined(enlarged(), identity=StubIdentity("failed", 0.41)), "identity_changed")

    def test_a_face_that_cannot_be_verified_is_declined_not_taken_on_faith(self):
        self.assertEqual(self.declined(enlarged(), identity=StubIdentity("inconclusive", None)), "identity_unverifiable")

    def test_a_photograph_without_a_judgeable_face_needs_no_face_checks(self):
        (readings, _small, report_after, checked), identity = self.judge(enlarged(), analyzer=analyzer_with(None), identity=StubIdentity("failed", 0.1))
        self.assertIsNone(checked)
        self.assertNotIn("identity", readings)
        self.assertNotIn("landmark_shift", readings)
        self.assertEqual(identity.restoring, [])
        self.assertEqual(report_after.faces, [])

    def test_a_speck_of_a_face_is_not_judged(self):
        speck = analyzer_with((100, 100, 30, 30)).analyze(SOURCE)
        self.assertEqual(len(speck.faces), 1)
        self.assertEqual(cloud._checkable_faces(speck), [])
        self.assertEqual(len(cloud._checkable_faces(analyzer_with().analyze(SOURCE))), 1)


class EnhanceTests(ServerCase):
    def run_enhance(self, behaviours=None, analyzer=None, identity=None, checkpoint=None, **overrides):
        server = self.serve(behaviours, **overrides)
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=identity or StubIdentity()):
            outcome = cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer or analyzer_with(), checkpoint=checkpoint)
        return outcome, server

    def declined(self, *args, **kwargs):
        with self.assertRaises(cloud.CloudDeclined) as caught:
            self.run_enhance(*args, **kwargs)
        return caught.exception

    def test_a_success_returns_the_enlarged_result_and_a_record_every_reader_understands(self):
        (image, level, parameters), server = self.run_enhance()
        self.assertEqual(image.shape[:2], (1280, 960))
        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertEqual(parameters["algorithm"], "cloud_v1+deep_image_auto_enhance_pro")
        adaptive = parameters["adaptive"]
        self.assertEqual(adaptive["outcome"], "enhanced")
        self.assertEqual(adaptive["operations_applied"], ["cloud_enhancement"])
        self.assertIn("Deep Image Auto Enhance Pro, a cloud AI model", adaptive["summary"])
        self.assertIn("faces were checked", adaptive["summary"])
        stage = adaptive["stages"][0]
        self.assertEqual((stage["operation"], stage["status"], stage["engines"], stage["models_run"], stage["variant"]),
                         ("cloud_enhancement", "accepted", ["deep_image_auto_enhance_pro"], ["deep_image_auto_enhance_pro"], "auto_enhance_pro"))
        self.assertEqual(parameters["resolution_contract"], {"validated": True, "source_dimensions": [480, 640], "actual_output_dimensions": [960, 1280]})
        self.assertEqual(parameters["identity_status"], "verified")
        self.assertEqual(parameters["identity_verification"]["state"], "verified")
        self.assertEqual(processing_summary(parameters), {"ai_applied": True, "classical_fallback": False, "stages": [
            {"stage": "cloud_enhance", "engine": "deep_image_auto_enhance_pro", "kind": "ai", "fallback": False, "replaced": []}]})
        self.assertEqual(describe_stages(parameters), {"applied_stages": [{"stage": "cloud_enhance", "engine": "deep_image_auto_enhance_pro", "variant": "auto_enhance_pro"}],
                                                       "skipped_stages": [], "hd_unavailable_reason": None})
        block = parameters["cloud_enhancement"]
        self.assertEqual((block["attempted"], block["used"], block["provider"], block["preset"]), (True, True, "deep_image", "auto_enhance_pro"))
        self.assertEqual(block["job"], next(iter(server.jobs)))
        self.assertEqual(block["gates"]["identity"]["status"], "verified")
        stored = json.dumps(parameters)                                   # it is stored as JSON, and holds neither the key nor the provider's address
        self.assertNotIn(KEY, stored)
        self.assertNotIn(server.origin, stored)

    def test_the_preset_setting_chooses_what_is_asked_and_what_is_recorded(self):
        (_image, _level, parameters), server = self.run_enhance(DEEP_IMAGE_PRESET="auto_enhance_klein9b")
        self.assertEqual(server.parameters(server.uploads()[0]["body"]), {"preset": "auto_enhance_klein9b"})
        self.assertEqual(parameters["algorithm"], "cloud_v1+deep_image_auto_enhance_klein9b")
        self.assertIn("Flux 2 Klein 9B", parameters["adaptive"]["summary"])

    def test_the_stored_record_has_what_the_other_readers_ask_of_an_adaptive_result(self):
        (_image, _level, parameters), _server = self.run_enhance()
        for key in ("quality_before", "quality_after", "quality_comparison", "warnings", "stage_reports", "render_policy"):
            self.assertIn(key, parameters)
        self.assertEqual(parameters["warnings"], [])
        self.assertEqual(parameters["quality_comparison"]["method"], "source_resolution_normalized_v1")
        self.assertEqual(parameters["quality_comparison"]["reference_dimensions"], [480, 640])
        for key in ("quality_report", "final_quality_report", "profile", "candidates", "seconds", "unresolved_defects", "operations_skipped"):
            self.assertIn(key, parameters["adaptive"])

    def test_without_faces_nothing_needs_verifying_and_the_photograph_is_sent(self):
        (_image, _level, parameters), server = self.run_enhance(analyzer=analyzer_with(None), identity=StubIdentity(available=False))
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual(parameters["identity_verification"]["state"], "not_applicable")
        self.assertNotIn("faces were checked", parameters["adaptive"]["summary"])

    def test_faces_too_small_to_judge_are_said_to_be_unchecked_not_verified(self):
        (_image, _level, parameters), _server = self.run_enhance(analyzer=analyzer_with((100, 100, 30, 30)), identity=StubIdentity(available=False))
        block = parameters["identity_verification"]
        self.assertEqual((block["state"], block["verified"], block["status"]), ("not_verifiable", False, "unavailable"))
        self.assertIn("too small", block["notice"])

    def test_a_face_that_could_not_be_verified_sends_nothing(self):
        error = self.declined(identity=StubIdentity(available=False))
        self.assertEqual((error.reason, error.attempted), ("identity_unverifiable", False))

    def test_nothing_is_sent_when_the_provider_is_not_set_up(self):
        server = self.serve(DEEP_IMAGE_API_KEY="  ")
        with self.assertRaises(cloud.CloudDeclined) as caught:
            cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
        self.assertEqual((caught.exception.reason, caught.exception.attempted), ("not_configured", False))
        self.assertEqual(server.requests, [])

    def test_a_result_that_fails_a_check_is_declined_after_the_request_that_was_paid_for(self):
        noise = cv2.imencode(".jpg", np.random.default_rng(1).integers(0, 255, (1280, 960, 3), dtype=np.uint8))[1].tobytes()
        error = self.declined([{"image": noise}])
        self.assertEqual((error.reason, error.attempted), ("structure_changed", True))

    def test_a_provider_that_is_down_is_declined_after_one_upload(self):
        server = self.serve([{"upload_status": 500}])
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()), self.assertRaises(cloud.CloudDeclined) as caught:
            cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
        self.assertEqual((caught.exception.reason, caught.exception.attempted), ("provider_error", True))
        self.assertEqual(len(server.uploads()), 1)

    def test_the_daily_limit_stops_a_request_before_it_is_sent(self):
        server = self.serve(DEEP_IMAGE_DAILY_REQUEST_LIMIT=2)
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()):
            for _ in range(2):
                cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
            with self.assertRaises(cloud.CloudDeclined) as caught:
                cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
        self.assertEqual((caught.exception.reason, caught.exception.attempted), ("daily_limit", False))
        self.assertEqual(len(server.uploads()), 2)

    def test_a_limit_of_zero_is_no_limit(self):
        server = self.serve(DEEP_IMAGE_DAILY_REQUEST_LIMIT=0)
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()):
            for _ in range(3):
                cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
        self.assertEqual(len(server.uploads()), 3)

    def test_a_cancelled_job_stops_the_wait_and_the_cancellation_is_not_swallowed(self):
        stages = []

        def checkpoint(stage):
            stages.append(stage)
            if stages.count("cloud_wait") == 2:
                raise Interrupted

        server = self.serve([{"never_done": True}])
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()), self.assertRaises(Interrupted):
            cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with(), checkpoint=checkpoint)
        self.assertEqual(stages[:3], ["cloud_analyze", "cloud_enhance", "cloud_wait"])
        self.assertEqual(len([item for item in server.requests if item["method"] == "GET"]), 1)

    def test_a_declined_run_names_the_job_in_the_log_so_a_paid_job_can_be_found(self):
        server = self.serve([{"status_after": "failed"}])
        with patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()), self.assertLogs("enhancement.cloud", level="INFO") as logs:
            with self.assertRaises(cloud.CloudDeclined):
                cloud.enhance(SOURCE, "auto", "natural", allow_biometric_processing=True, analyzer=analyzer_with())
        record = json.loads(next(entry for entry in logs.output if "cloud_enhancement" in entry).split("cloud_enhancement ", 1)[1])
        self.assertEqual((record["outcome"], record["reason"], record["attempted"], record["job"]), ("declined", "job_failed", True, next(iter(server.jobs))))

    def test_the_log_line_names_the_decision_and_no_secret(self):
        with self.assertLogs("enhancement.cloud", level="INFO") as logs:
            self.run_enhance()
        line = next(entry for entry in logs.output if "cloud_enhancement" in entry)
        record = json.loads(line.split("cloud_enhancement ", 1)[1])
        self.assertEqual((record["outcome"], record["preset"], record["faces_checked"]), ("used", "auto_enhance_pro", True))
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn("127.0.0.1", "\n".join(logs.output))


class SettingsAndRegistryTests(SimpleTestCase):
    def test_it_is_off_and_has_no_key_by_default(self):
        self.assertFalse(settings.CLOUD_ENHANCEMENT_ENABLED)
        self.assertEqual(settings.DEEP_IMAGE_BASE_URL, cloud.DEFAULT_BASE_URL)
        self.assertEqual(settings.DEEP_IMAGE_PRESET, "auto_enhance_pro")
        self.assertEqual(cloud.PRESETS, settings.DEEP_IMAGE_PRESETS)
        self.assertEqual(len(cloud.PRESETS), 6)
        self.assertEqual(set(cloud.LABELS), set(cloud.PRESETS))

    def test_availability_says_why_and_never_what_the_key_is(self):
        self.assertEqual(cloud.availability(), {"available": False, "reason": "disabled"})
        with override_settings(CLOUD_ENHANCEMENT_ENABLED=True, DEEP_IMAGE_API_KEY=""):
            self.assertEqual(cloud.availability(), {"available": False, "reason": "not_configured"})
        with override_settings(CLOUD_ENHANCEMENT_ENABLED=True, DEEP_IMAGE_API_KEY="   "):
            self.assertEqual(cloud.availability()["reason"], "not_configured")
        with override_settings(CLOUD_ENHANCEMENT_ENABLED=False, DEEP_IMAGE_API_KEY=KEY):
            self.assertEqual(cloud.availability(), {"available": False, "reason": "disabled"})
        with override_settings(CLOUD_ENHANCEMENT_ENABLED=True, DEEP_IMAGE_API_KEY=KEY):
            self.assertEqual(cloud.availability(), {"available": True, "reason": ""})

    def test_a_photograph_is_sent_only_when_nothing_in_the_request_says_otherwise(self):
        self.assertIsNone(cloud.skip_reason(allow_biometric_processing=True))
        self.assertIsNone(cloud.skip_reason(allow_biometric_processing=True, lighting="auto", has_alpha=False))
        self.assertEqual(cloud.skip_reason(allow_biometric_processing=False), "no_consent")
        self.assertEqual(cloud.skip_reason(allow_biometric_processing=True, lighting="dark"), "lighting_requested")
        self.assertEqual(cloud.skip_reason(allow_biometric_processing=True, lighting="light"), "lighting_requested")
        self.assertEqual(cloud.skip_reason(allow_biometric_processing=True, has_alpha=True), "transparency")
        self.assertEqual(cloud.skip_reason(allow_biometric_processing=False, lighting="dark", has_alpha=True), "no_consent")

    def test_the_summary_calls_the_cloud_engines_ai_and_nothing_else_a_cloud_engine(self):
        for preset in cloud.PRESETS:
            self.assertEqual(engine_kind(cloud.engine_name(preset)), "ai")
        self.assertEqual(engine_kind("deep_image_something_else"), "unknown")
        self.assertEqual(engine_kind("deep_image_"), "unknown")
        self.assertEqual(engine_kind("openai_auto_enhance_pro"), "unknown")
        self.assertEqual(engine_kind(None), "unknown")
        self.assertTrue(all(not cloud.is_cloud_engine(name) for name in AI_ENGINES))      # the local engines' table stays the local engines' table

    def test_a_fallback_is_recorded_and_said_in_words_only_where_a_person_should_know(self):
        said = {}
        for reason in ("not_configured", "daily_limit", "unauthorized", "no_credits", "identity_unverifiable", "timeout", "bad_result", "unexpected_error", "structure_changed",
                       "face_lost", "identity_changed", "no_consent", "lighting_requested", "transparency", "disabled"):
            parameters = {"warnings": []}
            cloud.note_fallback(parameters, reason, attempted=reason in ("timeout", "structure_changed"))
            said[reason] = parameters["warnings"]
            self.assertEqual(parameters["cloud_enhancement"]["reason"], reason)
            self.assertFalse(parameters["cloud_enhancement"]["used"])
        for reason in ("no_consent", "lighting_requested", "transparency", "disabled"):
            self.assertEqual(said[reason], [], reason)
        for reason in ("not_configured", "daily_limit", "unauthorized", "no_credits", "identity_unverifiable", "timeout", "bad_result", "unexpected_error", "structure_changed",
                       "face_lost", "identity_changed"):
            self.assertEqual(len(said[reason]), 1, reason)
        self.assertEqual(said["timeout"], [cloud.SERVICE_SENTENCE])
        self.assertEqual(said["bad_result"], [cloud.SERVICE_SENTENCE])
        self.assertEqual(said["structure_changed"], [cloud.RESULT_SENTENCE])
        self.assertTrue(all("local engine" in sentence for sentences in said.values() for sentence in sentences))
        parameters = {}
        cloud.note_fallback(parameters, "timeout", True)
        self.assertEqual(parameters["warnings"], [cloud.SERVICE_SENTENCE])           # a record with no warnings list yet gets one

    def test_the_guard_and_the_client_agree_on_the_providers_address(self):
        self.assertEqual(GUARD_BASE_URL, cloud.DEFAULT_BASE_URL)


class GuardTests(SimpleTestCase):
    def problems(self, **changes):
        config = good_config()
        config.update(changes)
        return " | ".join(production_problems(config))

    def test_it_is_not_asked_for_anything_while_it_is_off(self):
        self.assertNotIn("DEEP_IMAGE", self.problems(CLOUD_ENHANCEMENT_ENABLED=False, DEEP_IMAGE_API_KEY="", DEEP_IMAGE_BASE_URL="http://elsewhere"))

    def test_switching_it_on_needs_a_real_key_and_the_providers_own_address(self):
        good = dict(CLOUD_ENHANCEMENT_ENABLED=True, DEEP_IMAGE_API_KEY=random_secret(40), DEEP_IMAGE_BASE_URL=GUARD_BASE_URL)
        self.assertNotIn("DEEP_IMAGE", self.problems(**good))
        self.assertNotIn("DEEP_IMAGE", self.problems(**{**good, "DEEP_IMAGE_BASE_URL": GUARD_BASE_URL + "/"}))
        self.assertIn("DEEP_IMAGE_API_KEY", self.problems(**{**good, "DEEP_IMAGE_API_KEY": ""}))
        self.assertIn("DEEP_IMAGE_API_KEY", self.problems(**{**good, "DEEP_IMAGE_API_KEY": "changeme"}))
        self.assertIn("DEEP_IMAGE_BASE_URL", self.problems(**{**good, "DEEP_IMAGE_BASE_URL": "http://127.0.0.1:9/rest_api"}))
        self.assertIn("DEEP_IMAGE_BASE_URL", self.problems(**{**good, "DEEP_IMAGE_BASE_URL": ""}))


@override_settings(**ALL_ON, MEDIA_ROOT=tempfile.mkdtemp(), CELERY_TASK_ALWAYS_EAGER=True)
class JobTests(ResultFixture):
    """The cloud engine behind the real job code: claim, publish, API fields, downloads, and the local engine when the cloud one is not used."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def make_from(self, image, name="job.png"):
        encoded = cv2.imencode(".png", image)[1].tobytes()
        height, width = image.shape[:2]
        upload = ImageUpload.objects.create(owner=self.user, batch=self.batch, original_name=name, file_extension=".png", mime_type="image/png", file_size=len(encoded),
                                            width=width, height=height, status="uploaded")
        upload.original_file.save(name, ContentFile(encoded))
        preprocessing = ImagePreprocessingResult.objects.create(owner=self.user, image=upload, status="completed", output_format="png", output_name=name, output_width=width,
                                                                output_height=height, processed_file_size=len(encoded))
        preprocessing.processed_file.save(name, ContentFile(encoded))
        return ImageEnhancementResult.objects.create(owner=self.user, image=upload, preprocessing_result=preprocessing, status="queued", requested_level=EnhancementLevel.AUTO)

    def run_job(self, image=SOURCE, behaviours=None, cloud_on=True, consent=True, lighting=None, **overrides):
        result = self.make_from(image)
        if lighting:
            result.requested_lighting = lighting
            result.save(update_fields=["requested_lighting"])
        server = FakeDeepImageServer(behaviours, key=KEY).start()
        self.addCleanup(server.stop)
        options = {**SETTINGS, "DEEP_IMAGE_BASE_URL": server.base_url, **overrides} if cloud_on else {}
        with ExitStack() as stack:
            stack.enter_context(override_settings(**options))
            stack.enter_context(stub_analyzer_service()[0])
            stack.enter_context(patch("enhancement.adaptive.service.IdentityChecker", return_value=StubIdentity()))
            stack.enter_context(patch("privacy.services.biometric_processing_allowed", return_value=consent))
            return process_enhancement_result(result), server

    def api(self, result, tail=""):
        client = APIClient()
        client.force_authenticate(self.user)
        return client.get(f"/api/enhancements/results/{result.pk}/{tail}").json()

    def test_a_photograph_is_enhanced_in_the_cloud_and_the_api_says_so_in_the_fields_it_always_had(self):
        result, server = self.run_job()
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual(result.applied_level, "high")
        self.assertEqual((result.output_width, result.output_height), (960, 1280))
        stored = cv2.imdecode(np.frombuffer(result.enhanced_file.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(stored.shape[:2], (1280, 960))
        self.assertEqual(result.applied_parameters["source_dimensions"], [480, 640])
        body = self.api(result)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["enhancement_outcome"], "enhanced")
        self.assertIn("Deep Image Auto Enhance Pro", body["enhancement_summary"])
        self.assertEqual(body["processing_summary"], {"ai_applied": True, "classical_fallback": False, "stages": [
            {"stage": "cloud_enhance", "engine": "deep_image_auto_enhance_pro", "kind": "ai", "fallback": False, "replaced": []}]})
        self.assertEqual(body["applied_stages"], [{"stage": "cloud_enhance", "engine": "deep_image_auto_enhance_pro", "variant": "auto_enhance_pro"}])
        self.assertEqual((body["output_width"], body["output_height"]), (960, 1280))
        self.assertEqual(body["identity_status"], "verified")
        self.assertEqual(body["lighting"]["detected"], "light")
        comparison = self.api(result, "comparison/")                       # the comparison endpoint reads the same record
        self.assertEqual(comparison["warnings"], [])
        self.assertEqual(comparison["comparison"]["reference_dimensions"], [480, 640])
        self.assertIsNotNone(comparison["comparison"]["metrics"]["sharpness"]["before"])
        self.assertIsNotNone(comparison["comparison"]["metrics"]["sharpness"]["after"])
        self.assertEqual(comparison["algorithm"], "cloud_v1+deep_image_auto_enhance_pro")

    def test_with_the_switch_off_nothing_is_sent_and_the_record_is_what_it_was(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network access attempted")):
            result, server = self.run_job(cloud_on=False)
        self.assertEqual(server.requests, [])
        self.assertNotIn("cloud_enhancement", result.applied_parameters)
        self.assertTrue(result.applied_parameters["algorithm"].startswith("adaptive_v1"))
        self.assertEqual(result.applied_parameters["adaptive"]["outcome"], "original_preserved")
        self.assertEqual(self.api(result)["processing_summary"], {"ai_applied": False, "classical_fallback": False, "stages": []})

    def test_a_refusal_by_the_provider_leaves_a_local_result_that_says_why(self):
        result, server = self.run_job(behaviours=[{"upload_status": 402}])
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual(result.applied_parameters["adaptive"]["outcome"], "original_preserved")
        self.assertEqual(result.applied_parameters["cloud_enhancement"], {"attempted": True, "used": False, "provider": "deep_image", "preset": "auto_enhance_pro", "reason": "no_credits"})
        self.assertEqual(self.api(result, "comparison/")["warnings"], [cloud.SENTENCES["no_credits"]])
        self.assertEqual(self.api(result)["processing_summary"]["ai_applied"], False)

    def test_a_cloud_result_that_fails_a_check_is_not_stored_and_the_local_engine_answers(self):
        noise = cv2.imencode(".jpg", np.random.default_rng(1).integers(0, 255, (1280, 960, 3), dtype=np.uint8))[1].tobytes()
        result, server = self.run_job(behaviours=[{"image": noise}])
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual((result.output_width, result.output_height), (480, 640))
        self.assertEqual(result.applied_parameters["cloud_enhancement"]["reason"], "structure_changed")
        self.assertEqual(self.api(result, "comparison/")["warnings"], [cloud.RESULT_SENTENCE])

    def test_without_the_owners_face_processing_consent_nothing_is_sent(self):
        result, server = self.run_job(consent=False)
        self.assertEqual(server.requests, [])
        self.assertEqual(result.applied_parameters["cloud_enhancement"], {"attempted": False, "used": False, "provider": "deep_image", "preset": "auto_enhance_pro", "reason": "no_consent"})
        self.assertEqual(result.applied_parameters["warnings"], [])

    def test_a_person_who_names_the_lighting_gets_the_local_engines_method(self):
        result, server = self.run_job(lighting="dark")
        self.assertEqual(server.requests, [])
        self.assertEqual(result.applied_parameters["cloud_enhancement"]["reason"], "lighting_requested")

    def test_a_transparent_png_is_not_sent(self):
        transparent = np.dstack((SOURCE, np.full(SOURCE.shape[:2], 200, dtype=np.uint8)))
        result, server = self.run_job(image=transparent)
        self.assertEqual(server.requests, [])
        self.assertEqual(result.applied_parameters["cloud_enhancement"]["reason"], "transparency")

    def test_switched_on_without_a_key_it_says_so_and_sends_nothing(self):
        result, server = self.run_job(DEEP_IMAGE_API_KEY="")
        self.assertEqual(server.requests, [])
        self.assertEqual(result.applied_parameters["cloud_enhancement"]["reason"], "not_configured")
        self.assertEqual(result.applied_parameters["warnings"], [cloud.SENTENCES["not_configured"]])

    def test_an_unexpected_failure_in_the_cloud_path_falls_back_to_the_local_engine(self):
        with patch("enhancement.cloud.decode_result", side_effect=RuntimeError("boom")), self.assertLogs("enhancement.services", level="ERROR"):
            result, server = self.run_job()
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(server.uploads()), 1)
        self.assertEqual(result.applied_parameters["cloud_enhancement"]["reason"], "unexpected_error")
        self.assertTrue(result.applied_parameters["cloud_enhancement"]["attempted"])

    def test_a_cancelled_job_is_not_turned_into_a_local_result(self):
        from .services import EnhancementCancelled

        def cancelled(*args, **kwargs):
            raise EnhancementCancelled

        with patch("enhancement.cloud.DeepImageClient.wait", side_effect=cancelled), self.assertRaises(EnhancementCancelled):
            self.run_job()
