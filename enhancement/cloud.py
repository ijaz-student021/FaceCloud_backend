"""Optional cloud enhancement: Deep Image's Auto Enhance presets make the pixels. This is the only module that talks to Deep Image.

Off until an operator switches it on (``CLOUD_ENHANCEMENT_ENABLED``) AND supplies a key (``DEEP_IMAGE_API_KEY``); with either missing nothing is sent anywhere and a
result is exactly what it was. When it is on, a photograph the local engine would have enhanced is sent to Deep Image instead: the pixels only, re-encoded as a JPEG, so no EXIF,
location, colour profile or file name leaves the server. The answer is judged here (same picture, same faces) and, when it holds, stored in the shape of an adaptive result with
one stage, so the API, the processing summary and every report read it unchanged. Anything else hands the photograph to the local engine (``services._enhance_photo``): the switch
off, no face-processing consent, a request that names a lighting method, a transparent PNG, the daily limit, a failed or refused request, or a result that fails a check. The result
says which engine really made the pixels, and never claims the cloud one when it did not.

* Only the six ``auto_enhance*`` presets can be named (``settings.DEEP_IMAGE_PRESETS``); no other Deep Image operation is called.
* The upload is never repeated: every attempt is paid for. Waiting for the answer (a status request) is free and may be retried.
* The key goes only to the provider's own address, in a header: never to a result address, a log line, an error or a stored record. A redirect on a request that carries it is refused.
* The result's address is checked (https, not a private address) before anything is read from it.
* The checks use the local engine's own measurements of face geometry and identity, held to the limits it holds an enlargement to (validator.Limits, SFace guard): they are not loosened.
"""
import io
import ipaddress
import json
import logging
import re
import time
import uuid
from http.client import HTTPException
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from PIL import Image

from .models import EnhancementLevel

logger = logging.getLogger(__name__)

PROVIDER = "deep_image"
OPERATION = "cloud_enhancement"           # the stage's operation in the stored record (the API names it ``cloud_enhance``: restoration.OPERATION_STAGE)
ALGORITHM = "cloud_v1"
ENGINE_PREFIX = "deep_image_"
PRESETS = tuple(settings.DEEP_IMAGE_PRESETS)
LABELS = {
    "auto_enhance_pro": "Auto Enhance Pro", "auto_enhance_qwen": "Auto Enhance Qwen", "auto_enhance_klein9b": "Auto Enhance Flux 2 Klein 9B",
    "auto_enhance_generative": "Auto Enhance Generative", "auto_enhance_tone_correction": "Auto Enhance Normal with Tone Correction", "auto_enhance": "Auto Enhance Normal",
}
DEFAULT_BASE_URL = "https://deep-image.ai/rest_api"       # the address the production guard insists on (config/production_guard.py repeats it)

# The provider's job statuses. The documentation's samples show ``received``, ``in_progress``, ``not_started`` and ``complete``; the first real run (2026-11-09) also showed ``predicting``,
# which an earlier version of this client took for a failure and so abandoned a paid job. A status in neither set is not a verdict: the client keeps waiting (and says so once in the log)
# until the deadline, because giving up on a job that is still running wastes the credit it costs.
IN_PROGRESS = frozenset({"received", "in_progress", "not_started", "queued", "pending", "processing", "predicting"})
FAILED = frozenset({"failed", "failure", "error", "errored", "cancelled", "canceled", "rejected", "blocked", "expired", "timeout"})
TRANSIENT = frozenset({"timeout", "connection_error", "rate_limited", "provider_error"})      # a status request that fails like this is worth another try; an upload is never repeated
S3_BUCKET_HOST = re.compile(r"^(?P<bucket>[a-z0-9][a-z0-9.-]*[a-z0-9])\.s3\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$")
POLL_RETRIES = 3                  # consecutive failed status requests before the job is given up on
MAX_REPLY_BYTES = 1024 * 1024     # a JSON reply is a few hundred bytes
ASPECT_TOLERANCE = 0.02           # the result's width / height against the upload's: a rounded pixel is far below it, a crop or an extension far above
MIN_WIDTH_SHARE = 0.5             # a result narrower than this share of the upload is an error image or a thumbnail, not an enhancement
FLAT_CONTRAST = 2.0               # luminance standard deviation under which a result is a flat fill (when the upload was not)
QUOTA_TIMEOUT = 26 * 3600         # the daily counter outlives its day, so a clock change cannot reset it early

# What a person may read when the cloud engine was wanted and the local engine did the work. ``None``: nothing is added (the photograph never asked for the cloud engine, or the reason is
# the person's own choice); the reason code is still recorded in ``applied_parameters["cloud_enhancement"]``.
SENTENCES = {
    "not_configured": "Cloud enhancement is switched on but not set up, so the local engine was used.",
    "daily_limit": "The daily limit of cloud enhancements was reached, so the local engine was used.",
    "unauthorized": "The cloud service refused the request, so the local engine was used.",
    "no_credits": "The cloud service has no credits left, so the local engine was used.",
    "identity_unverifiable": "The faces in this photograph could not be verified for the cloud result, so the local engine was used.",
    "plan_required": "The cloud service's plan does not include this enhancement, so the local engine was used.",
    "no_consent": None, "lighting_requested": None, "transparency": None, "disabled": None,
}
SERVICE_SENTENCE = "The cloud service could not deliver an enhanced photograph, so the local engine was used."
RESULT_SENTENCE = "The cloud result changed the photograph too much to be used, so the local engine was used."
# The service did not deliver a usable image (as against a result that was delivered and refused by a check, which is everything else).
SERVICE_REASONS = frozenset({
    "timeout", "connection_error", "rate_limited", "provider_error", "job_failed", "deadline", "bad_reply", "rejected", "redirected", "bad_request", "encode_failed", "bad_result",
    "result_too_large", "unexpected_error",
})


class CloudDeclined(Exception):
    """The cloud engine did not make this photograph's pixels. ``reason`` is a short code; ``attempted`` says whether a request was sent (and so paid for)."""

    def __init__(self, reason, attempted=False):
        super().__init__(reason)
        self.reason = reason
        self.attempted = attempted


def engine_name(preset):
    return ENGINE_PREFIX + preset


def is_cloud_engine(name):
    """True for the engine names this module records (``deep_image_<preset>``): learned models that ran at the provider."""
    return isinstance(name, str) and name.startswith(ENGINE_PREFIX) and name[len(ENGINE_PREFIX):] in PRESETS


def availability():
    """Why the cloud engine can or cannot be used right now; contains no secret."""
    if not settings.CLOUD_ENHANCEMENT_ENABLED:
        return {"available": False, "reason": "disabled"}
    if not str(settings.DEEP_IMAGE_API_KEY or "").strip():
        return {"available": False, "reason": "not_configured"}
    return {"available": True, "reason": ""}


def skip_reason(*, allow_biometric_processing, lighting="auto", has_alpha=False):
    """Why this photograph is not sent although the engine is available (a code), or ``None`` when it may be. Decided before anything leaves the server.

    Faces are processed by the provider's model, so the owner's face-processing consent is required (the switch face restoration already needs); a person who named a lighting method
    asked for the local engine's method; and a transparent PNG would lose its transparency in a round trip through a format the provider's result does not carry.
    """
    if not allow_biometric_processing:
        return "no_consent"
    if str(getattr(lighting, "value", lighting) or "auto") != "auto":
        return "lighting_requested"
    if has_alpha:
        return "transparency"
    return None


def note_fallback(parameters, reason, attempted):
    """Record on a local engine's result that the cloud engine was wanted and was not used, and say so in words when a person should know."""
    parameters["cloud_enhancement"] = {"attempted": bool(attempted), "used": False, "provider": PROVIDER, "preset": settings.DEEP_IMAGE_PRESET, "reason": reason}
    sentence = _sentence(reason)
    if sentence:
        parameters.setdefault("warnings", []).append(sentence)


def _sentence(reason):
    if reason in SENTENCES:
        return SENTENCES[reason]
    return SERVICE_SENTENCE if reason in SERVICE_REASONS else RESULT_SENTENCE


# -- the network ----------------------------------------------------------------------------------------------------------------------------------------------------------------


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    """A request that carries the key is never sent anywhere else: a redirect is an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _CheckedRedirect(urllib_request.HTTPRedirectHandler):
    """A result address may redirect (a few hops), but every hop is checked like the first."""

    max_redirections = 3

    def __init__(self, base_url):
        self.base_url = base_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_result_url(newurl, self.base_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_provider(base_url):
    return urlsplit(base_url).hostname == "deep-image.ai"


def check_result_url(url, base_url):
    """A result is fetched only from an https address that is not a private one. A test server (a base URL that is not the provider's) may be plain http on this machine."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if not host or parts.scheme not in ("http", "https"):
        raise CloudDeclined("bad_reply")
    if _is_provider(base_url):
        if parts.scheme != "https":
            raise CloudDeclined("bad_reply")
        if host == "localhost" or host.endswith(".local"):
            raise CloudDeclined("bad_reply")
        try:
            private = not ipaddress.ip_address(host).is_global
        except ValueError:
            private = False              # a name, not an address literal (a name that resolves to a private address is not caught: this is a check of the reply, not a firewall)
        if private:
            raise CloudDeclined("bad_reply")


def path_style(url):
    """The same S3 object through the region's own endpoint (``https://s3.<region>.amazonaws.com/<bucket>/<key>``), or ``None`` for an address that is not a bucket's own host.

    The provider answers with ``https://<bucket>.s3.<region>.amazonaws.com/<key>``. On the owner's network (2026-11-09) a connection to that hostname was reset in the TLS handshake,
    for Python and for curl alike, while the regional endpoint answered: the same object, through the other address, was fetched at once.
    """
    parts = urlsplit(str(url))
    match = S3_BUCKET_HOST.match(parts.hostname or "")
    if parts.scheme != "https" or not match:
        return None
    return urlunsplit(("https", f"s3.{match.group('region')}.amazonaws.com", f"/{match.group('bucket')}{parts.path}", parts.query, ""))


def _status_reason(code):
    if code in (401, 403):
        return "unauthorized"
    if code == 402:
        return "no_credits"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "provider_error"
    return "redirected" if 300 <= code < 400 else "rejected"


def _multipart(fields, file_field, filename, content_type, payload):
    boundary = "----facecloud" + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode() for name, value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    parts += [payload, f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class DeepImageClient:
    """Three requests: upload a photograph (``POST /process``: a job id comes back), ask how the job is doing (``GET /result/<job>``), fetch the finished image."""

    def __init__(self, api_key=None, base_url=None, timeout=None):
        self.api_key = str(settings.DEEP_IMAGE_API_KEY if api_key is None else api_key).strip()
        self.base_url = str(settings.DEEP_IMAGE_BASE_URL if base_url is None else base_url).rstrip("/")
        self.timeout = float(settings.DEEP_IMAGE_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout)

    def submit(self, jpeg, preset):
        """Send the photograph; returns the provider's job id. Called once per photograph."""
        body, content_type = _multipart({"parameters": json.dumps({"preset": preset})}, "image", "photo.jpg", "image/jpeg", jpeg)
        reply = self._json("POST", "/process", body, content_type)
        job = reply.get("job") if isinstance(reply, dict) else None
        if not isinstance(job, str) or not job:
            raise CloudDeclined("bad_reply")
        return job

    def status(self, job):
        reply = self._json("GET", "/result/" + quote(job, safe=""))
        if not isinstance(reply, dict):
            raise CloudDeclined("bad_reply")
        return reply

    def wait(self, job, deadline, checkpoint):
        """The address of the finished image, polling until the deadline. ``checkpoint`` keeps the claim alive and stops the wait when the job is cancelled."""
        failures, unknown = 0, set()
        while True:
            checkpoint("cloud_wait")
            try:
                reply = self.status(job)
            except CloudDeclined as error:
                failures += 1
                if error.reason not in TRANSIENT or failures > POLL_RETRIES:
                    raise
            else:
                failures = 0
                state = str(reply.get("status") or "").strip().lower()
                if state == "complete":
                    url = reply.get("result_url")
                    if not isinstance(url, str) or not url:
                        raise CloudDeclined("bad_reply")
                    return url
                if state in FAILED:
                    logger.warning("Deep Image reported the job status %r.", state[:40])
                    raise CloudDeclined("job_failed")
                if state not in IN_PROGRESS and state not in unknown:
                    unknown.add(state)
                    logger.warning("Deep Image reported the job status %r, which this client does not know: it keeps waiting.", state[:40])
            if time.monotonic() + float(settings.DEEP_IMAGE_POLL_SECONDS) > deadline:
                raise CloudDeclined("deadline")
            time.sleep(float(settings.DEEP_IMAGE_POLL_SECONDS))

    def download(self, url):
        """The finished image's bytes. No key is sent: the address is the provider's reply, not the provider's API.

        When the address cannot be reached (a reset or a timeout) and it is a bucket's own S3 host, the same object is asked for through the regional endpoint (``path_style``).
        """
        try:
            return self._fetch(url)
        except CloudDeclined as error:
            fallback = path_style(url) if error.reason in ("connection_error", "timeout") else None
            if fallback is None:
                raise
            logger.warning("The result's own host could not be reached (%s): asking the regional S3 endpoint for the same object.", error.reason)
            return self._fetch(fallback)

    def _fetch(self, url):
        check_result_url(url, self.base_url)
        request = urllib_request.Request(url, headers={"Accept": "image/*"})
        return self._call(request, int(settings.DEEP_IMAGE_MAX_RESULT_BYTES), urllib_request.build_opener(_CheckedRedirect(self.base_url)), "result_too_large")

    def _json(self, method, path, body=None, content_type=None):
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib_request.Request(self.base_url + path, data=body, headers=headers, method=method)
        data = self._call(request, MAX_REPLY_BYTES, urllib_request.build_opener(_NoRedirect), "bad_reply")
        try:
            return json.loads(data)
        except ValueError:
            raise CloudDeclined("bad_reply") from None

    def _call(self, request, limit, opener, too_big):
        try:
            with opener.open(request, timeout=self.timeout) as response:     # nosec B310 - the provider's own https address (settings), or an address check_result_url accepted
                data = response.read(limit + 1)
        except urllib_error.HTTPError as error:
            said = self._note_refusal(error)
            # The first real refusal (2026-11-09, a free account asking for auto_enhance_pro) was an HTTP 400 whose message said "available only for paid accounts": worth its own code.
            raise CloudDeclined("plan_required" if error.code == 400 and "paid account" in said.lower() else _status_reason(error.code)) from None
        except urllib_error.URLError as error:
            raise CloudDeclined("timeout" if isinstance(error.reason, TimeoutError) else "connection_error") from None
        except TimeoutError:
            raise CloudDeclined("timeout") from None
        except ValueError:
            raise CloudDeclined("bad_request") from None          # e.g. a key with a line break in it: http.client refuses the header
        except (HTTPException, OSError):
            raise CloudDeclined("connection_error") from None
        if len(data) > limit:
            raise CloudDeclined(too_big)
        return data

    def _note_refusal(self, error):
        """The provider's own words for a refusal, short and with the key taken out, so the first misconfiguration is diagnosable from the log."""
        try:
            text = error.read(200).decode("utf-8", "replace")
        except Exception:
            text = ""
        if self.api_key:
            text = text.replace(self.api_key, "[key]")
        text = " ".join(text.split())[:200]
        logger.warning("Deep Image answered HTTP %s: %s", error.code, text)
        return text


# -- the photograph, there and back --------------------------------------------------------------------------------------------------------------------------------------------


def encode_upload(image):
    """The pixels as a JPEG (no EXIF, location, colour profile or file name travel with them)."""
    done, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(settings.DEEP_IMAGE_UPLOAD_JPEG_QUALITY)])
    if not done:
        raise CloudDeclined("encode_failed")
    return buffer.tobytes()


def decode_result(payload):
    """The provider's image as a BGR array; its size is read from the header first, so an absurd image is never decoded."""
    try:
        with Image.open(io.BytesIO(payload)) as header:
            width, height = header.size
    except Exception:
        raise CloudDeclined("bad_result") from None
    if width * height > int(settings.DEEP_IMAGE_MAX_RESULT_PIXELS):
        raise CloudDeclined("result_too_large")
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if image is None or image.ndim != 3 or image.dtype != np.uint8:
        raise CloudDeclined("bad_result")
    return image


def _reserve_request():
    """Count this request against today's limit before it is sent (the counter lives in the cache, which production keeps in Redis)."""
    limit = int(settings.DEEP_IMAGE_DAILY_REQUEST_LIMIT)
    if limit <= 0:
        return
    key = f"cloud_enhancement:requests:{timezone.localdate().isoformat()}"
    cache.add(key, 0, QUOTA_TIMEOUT)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, QUOTA_TIMEOUT)
        used = 1
    if used > limit:
        raise CloudDeclined("daily_limit")


# -- the judgement -------------------------------------------------------------------------------------------------------------------------------------------------------------


def _checkable_faces(report):
    """The upload's faces that can be judged: one under 48 px (``too_small``) is a speck whose landmarks are guesses, and the local engine's own gates let it neither veto nor vouch."""
    return [face for face in report.faces if face.quality != "too_small"]


def judge(source, result, identity, analyzer, checkable):
    """The checks a cloud result must pass before it is kept. Raises ``CloudDeclined`` with the first that fails.

    Returns ``(readings, small, report_after, checked)``: the numbers behind the verdict, the result on the upload's own pixel grid, its analysis, and the identity check (``None``
    when no face could be judged). The result is the same picture (aspect, size, not a flat fill, structure); a face that was there is still there where it was (landmark geometry, the
    limit the local engine holds an enlargement to, ``Limits.face_landmark_shift_max_upscale``: faces under 48 px never reach it, so its pixel floor could not change it); and every
    face is verified as the same person (the SFace guard). A face that cannot be verified is not accepted: a generative model's result is never taken on faith.
    """
    from .adaptive.validator import Limits, landmark_shift, structure_ssim

    limits = Limits()
    source_height, source_width = source.shape[:2]
    height, width = result.shape[:2]
    readings = {"upload_size": [source_width, source_height], "result_size": [width, height]}
    aspect = abs((width / height) / (source_width / source_height) - 1.0)
    readings["aspect_error"] = round(aspect, 4)
    if aspect > ASPECT_TOLERANCE:
        raise CloudDeclined("aspect_changed")
    if width < source_width * MIN_WIDTH_SHARE:
        raise CloudDeclined("result_too_small")
    contrast = float(cv2.cvtColor(result, cv2.COLOR_BGR2GRAY).std())
    readings["result_contrast"] = round(contrast, 2)
    if contrast < FLAT_CONTRAST and float(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY).std()) >= FLAT_CONTRAST:
        raise CloudDeclined("result_flat")
    small = result if (width, height) == (source_width, source_height) else cv2.resize(result, (source_width, source_height), interpolation=cv2.INTER_AREA)
    ssim = float(structure_ssim(source, small))
    readings["structure_ssim"] = round(ssim, 4)
    if ssim < float(settings.CLOUD_ENHANCEMENT_MIN_STRUCTURE_SSIM):
        raise CloudDeclined("structure_changed")
    report_after = analyzer.analyze(small)
    if not checkable:
        return readings, small, report_after, None
    found = list(report_after.faces) + list(getattr(report_after, "other_faces", None) or [])
    shift = landmark_shift(checkable, found, 1.0)
    readings["landmark_shift"] = None if shift is None else round(shift, 4)
    if shift is None:
        raise CloudDeclined("face_lost")
    limit = limits.face_landmark_shift_max_upscale
    readings["landmark_limit"] = round(limit, 4)
    if shift > limit:
        raise CloudDeclined("face_geometry_changed")
    checked = identity.check(small, restoring=False)
    readings["identity"] = {"status": checked["status"], "similarity": checked.get("similarity"), "threshold": checked.get("threshold")}
    if checked["status"] == "failed":
        raise CloudDeclined("identity_changed")
    if checked["status"] != "verified":
        raise CloudDeclined("identity_unverifiable")
    return readings, small, report_after, checked


def _identity_block(report0, identity, checked, allow_biometric_processing):
    from .adaptive.service import identity_report

    if checked is not None or not report0.faces:
        return identity_report(report0, identity, [OPERATION], checked, allow_biometric_processing)
    return {                          # faces were found but every one is too small to compare: said so, not reported as verified or as a restoration that did not happen
        "threshold": identity.threshold, "similarity": None, "face_restoration_unverified": False, "status": "unavailable", "state": "not_verifiable", "verified": False,
        "scope": "Compared with the uploaded photograph only; similarity of face embeddings is evidence, not a guarantee.", "reason": "The faces are too small to compare.",
        "notice": "The faces in this photograph are too small to compare with the original, so identity was not checked.",
    }


def _parameters(image, result, small, report0, report_after, checked, identity, readings, run):
    """The stored record: an adaptive result with one accepted stage, so ``describe_stages``, ``processing_summary``, the outcome and the reports all read it as they read any other."""
    from .adaptive.profile import profile_for
    from .adaptive.service import ENHANCED
    from .services import analyze_image_quality, compare_image_quality

    preset, engine = run["preset"], engine_name(run["preset"])
    summary = f"Enhanced: improved the photograph with Deep Image {LABELS[preset]}, a cloud AI model."
    if checked is not None:
        summary += " The faces were checked against your original."
    identity_block = _identity_block(report0, identity, checked, run["allow_biometric_processing"])
    stage = {
        "operation": OPERATION, "reason": "A cloud AI model enhanced the whole photograph.", "engines": [engine], "skipped_engines": [], "notes": [], "candidates": ["c1_" + OPERATION],
        "optional": False, "status": "accepted", "chosen": "c1_" + OPERATION, "variant": preset, "seconds": round(run["cloud_seconds"], 2), "models_run": [engine],
    }
    return {
        "algorithm": f"{ALGORITHM}+{engine}", "pipeline_version": ALGORITHM,
        "render_policy": {"policy": ALGORITHM, "provider": PROVIDER, "preset": preset},
        "requested_level": str(run["requested_level"]), "style": str(run["style"]), "requested_lighting": "auto",
        "quality_before": analyze_image_quality(image), "quality_after": analyze_image_quality(small), "quality_comparison": compare_image_quality(image, result),
        "warnings": [], "identity_status": identity_block["status"], "identity_verification": identity_block,
        "stage_reports": [{"stage": OPERATION, "status": "accepted", "changed_pixels": True}],
        "resolution_contract": {"validated": True, "source_dimensions": [image.shape[1], image.shape[0]], "actual_output_dimensions": [result.shape[1], result.shape[0]]},
        "adaptive": {
            "outcome": ENHANCED, "summary": summary, "operations_applied": [OPERATION], "profile": profile_for(run["style"], run["requested_level"], "auto").describe(),
            "operations_skipped": [], "stages": [stage], "unresolved_defects": [], "conditions_seen_only_after_enhancement": [], "conditions_not_counted_as_defects": [],
            "quality_report": report0.as_dict(), "final_quality_report": report_after.as_dict(),
            "candidates": [
                {"id": "c0_original", "operation": "original", "variant": "", "selected": False, "accepted": None, "seconds": 0.0, "validation_seconds": 0.0},
                {"id": "c1_" + OPERATION, "operation": OPERATION, "variant": preset, "selected": True, "accepted": True, "seconds": round(run["cloud_seconds"], 2),
                 "validation_seconds": round(run["validation_seconds"], 2)},
            ],
            "debug_artifacts": None, "limits": {"max_candidates": 2, "time_budget_seconds": float(settings.DEEP_IMAGE_DEADLINE_SECONDS)},
            "seconds": round(run["seconds"], 2), "analysis_seconds": round(run["analysis_seconds"], 2),
        },
        "cloud_enhancement": {
            "attempted": True, "used": True, "provider": PROVIDER, "preset": preset, "job": run["job"], "seconds": round(run["cloud_seconds"], 2),
            "upload_bytes": run["upload_bytes"], "result_bytes": run["result_bytes"], "gates": readings,
        },
    }


def enhance(image, requested_level, style, *, allow_biometric_processing, checkpoint=None, analyzer=None, client=None):
    """Enhance one BGR photograph with the configured Deep Image preset: ``(image, applied_level, parameters)`` like ``enhance_bgr``, or ``CloudDeclined``.

    Nothing is sent before the local checks that need no result are done (the photograph is analysed, and a face that could not be verified is declined with no request and no cost).
    ``checkpoint`` is the job's: it keeps the claim alive and raises to stop the wait when the job is cancelled or taken over, and that exception is not caught here.
    """
    checkpoint = checkpoint or (lambda _name: None)
    state = availability()
    if not state["available"]:
        raise CloudDeclined(state["reason"])
    from .adaptive.service import AdaptiveImageEnhancementService, IdentityChecker

    preset = settings.DEEP_IMAGE_PRESET
    started = time.perf_counter()
    sent, job = False, None
    try:
        checkpoint("cloud_analyze")
        analyzer = analyzer or AdaptiveImageEnhancementService()._analyzer()
        report0 = analyzer.analyze(image)
        identity = IdentityChecker(image, allow_biometric_processing and report0.faces != [])
        checkable = _checkable_faces(report0)
        if checkable and not identity.available:
            raise CloudDeclined("identity_unverifiable")            # a result could not be verified, so nothing is sent
        analysis_seconds = time.perf_counter() - started
        _reserve_request()
        payload = encode_upload(image)
        client = client or DeepImageClient()
        deadline = time.monotonic() + float(settings.DEEP_IMAGE_DEADLINE_SECONDS)
        checkpoint("cloud_enhance")
        sent = True
        sent_at = time.perf_counter()
        job = client.submit(payload, preset)
        data = client.download(client.wait(job, deadline, checkpoint))
        cloud_seconds = time.perf_counter() - sent_at
        result = decode_result(data)
        checkpoint("cloud_validate")
        judged_at = time.perf_counter()
        readings, small, report_after, checked = judge(image, result, identity, analyzer, checkable)
    except CloudDeclined as error:
        error.attempted = sent
        _log("declined", preset=preset, reason=error.reason, attempted=sent, job=job, seconds=round(time.perf_counter() - started, 2))
        raise
    run = {
        "preset": preset, "requested_level": requested_level, "style": style, "allow_biometric_processing": allow_biometric_processing, "job": job,
        "cloud_seconds": cloud_seconds, "validation_seconds": time.perf_counter() - judged_at, "analysis_seconds": analysis_seconds, "seconds": time.perf_counter() - started,
        "upload_bytes": len(payload), "result_bytes": len(data),
    }
    parameters = _parameters(image, result, small, report0, report_after, checked, identity, readings, run)
    _log("used", preset=preset, seconds=round(run["seconds"], 2), cloud_seconds=round(cloud_seconds, 2), upload_bytes=len(payload), result_bytes=len(data),
         size={"input": readings["upload_size"], "output": readings["result_size"]}, faces_checked=checked is not None, gates=readings)
    return result, EnhancementLevel.HIGH, parameters


def _log(outcome, **fields):
    """One structured line per photograph for log aggregation: the decision, the timings and the readings, never pixels, a key, an address or user data."""
    logger.info("cloud_enhancement %s", json.dumps({"outcome": outcome, **fields}, separators=(",", ":"), default=str))
