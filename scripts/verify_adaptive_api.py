"""Verify the adaptive engine end to end through the real HTTP API, with the real models, on a disposable test database, and keep reproducible evidence.

    python scripts/verify_adaptive_api.py --images folder_of_consented_photos --out evidence_dir [--max-edge 640]
        [--consent granted|absent] [--quality-underexposed reject|review] [--identity-policy limited|fail_closed]
        [--second-photo 1] [--label name] [--save-tiles]

For each case a clean photograph is degraded in a known way and then sent through the same endpoints a client uses:

    POST /api/images/upload/single/          (the standard upload; in this script's eager mode it also preprocesses and runs the quality check)
    POST /api/enhancements/images/{id}/      (Auto Enhance: level auto, style natural)
    GET  /api/enhancements/results/{id}/     (status, enhancement_outcome, enhancement_summary, output size, identity_verification)
    GET  <download_url>                      (the one final image)

in **production mode** (debug artifacts off). Every case is reported as exactly one of

    enhanced | partially_enhanced | original_preserved | failed | expected_rejection

``expected_rejection`` is a photograph the quality check rejected: the request is refused with HTTP 400 and the stock sentence, no enhancement is created,
no output file is stored, and the stored original is unchanged. It is never counted, or described, as enhanced. ``failed`` is anything else that went wrong
(including a 400 for any other reason, or a job that did not complete). The exit status is non-zero if any case failed or any of its checks did not hold.

What the run checks, besides the classification: exactly one output file per enhanced result; the downloaded bytes decode to the reported size; an
``original_preserved`` output is pixel-identical to its input; no debug or intermediate image was stored; the stored original upload is byte-identical
before and after; and ``identity_verification.verified`` is true only when ``state`` is ``verified`` (or the photograph was not changed).

With ``--quality-underexposed reject`` and/or ``--identity-policy fail_closed`` the run also checks that each case obeyed those settings (``policy_*`` checks): a rejected photograph is
refused and never enhanced; a very dark case is rejected for ``severe_underexposure``; and whenever identity was unavailable or inconclusive nothing was applied, the original came back
pixel for pixel, and the stored upload is unchanged.

The consent switch matters: with ``--consent absent`` the user has no biometric consent (BIOMETRIC_CONSENT_REQUIRED is forced on), so the engine cannot
verify identity and says so; the default grants consent, as a real account that agreed would have. The settings that decide what happens to a very
dark photograph and to unverifiable identity are explicit flags, defaulting to the project defaults (``reject`` and ``limited``).

Evidence (nothing in it is a photograph): ``evidence.json`` has the commit and branch, the command line, the environment, every setting the run forced, the
checkpoint files with their SHA-256 and whether they match the pinned value, the size and a short hash of each source photograph, and for each case the HTTP
statuses, the API responses (download links redacted; the bulky measurement reports inside ``applied_parameters`` are left out unless ``--full-responses``),
the quality and face-check decisions, the identity block and the metrics. ``verification.json`` is the
same cases as a plain list. Tiles (clean | degraded | output side by side) are written only with ``--save-tiles``: they are your photographs and your faces,
so keep them off GitHub. The photographs are not modified and nothing is uploaded anywhere; the database is a throwaway test database and media goes to a
temporary folder.

Test photographs: any folder of JPEG/PNG/WebP portraits you have the right to use, one person or several (the first two, in sorted file-name order, are used;
``--second-photo`` picks another). They should show a clear, front-facing face, ideally 600 pixels or more on the long side. They are not in the repository.
Face restoration is left OFF.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

CLASSIFICATIONS = ("enhanced", "partially_enhanced", "original_preserved", "failed", "expected_rejection")
REFUSAL_SENTENCE = "This photo was rejected by the checks, so it is not enhanced."
ENGINE_OUTCOMES = ("enhanced", "partially_enhanced", "original_preserved")

# The settings this run forces or reports, so the evidence does not depend on a developer's .env (identity pins come from the committed manifest when unset).
RECORDED_SETTINGS = (
    "ENHANCEMENT_RENDER_POLICY", "RETINEXFORMER_ENABLED", "ZERO_DCE_ENABLED", "NAFNET_DENOISE_ENABLED", "NAFNET_DEBLUR_ENABLED", "HD_RESTORATION_ENABLED",
    "CODEFORMER_ENABLED", "GFPGAN_ENABLED", "ADAPTIVE_DEBUG_ARTIFACTS", "ADAPTIVE_TIME_BUDGET_SECONDS", "ENHANCEMENT_MODEL_RESIDENCY",
    "QUALITY_UNDEREXPOSED_FACE_ACTION", "QUALITY_REJECT_MIN_LUMINANCE", "QUALITY_REJECT_CONTRAST", "ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY",
    "BIOMETRIC_CONSENT_REQUIRED", "FACE_DETECTOR_PROVIDER", "IDENTITY_PROVIDER", "IDENTITY_GUARD_ENABLED", "CELERY_TASK_ALWAYS_EAGER",
    "IMAGE_ENCRYPTION_ENABLED", "STORAGE_BACKEND",
)
# (setting prefix, label) of the checkpoints a run can load.
CHECKPOINTS = (
    ("RETINEXFORMER", "retinexformer"), ("NAFNET", "nafnet_gopro"), ("NAFNET_SIDD", "nafnet_sidd"), ("REALESRGAN", "realesrgan"),
    ("ZERO_DCE", "zero_dce"), ("YUNET", "yunet"), ("IDENTITY", "sface"),
)


# -- pure helpers (importable without Django, and tested in enhancement/test_verification_script.py) ------------------------------------------------
def classify_case(enhance_status, enhance_body, detail, quality_decision, refusal=REFUSAL_SENTENCE):
    """One of ``CLASSIFICATIONS`` for what the API did with a case. A refused photograph is never anything but ``expected_rejection`` or ``failed``."""
    if enhance_status == 400:
        return "expected_rejection" if quality_decision == "rejected" and enhance_body == [refusal] else "failed"
    if enhance_status != 202 or not isinstance(detail, dict) or detail.get("status") != "completed":
        return "failed"
    outcome = detail.get("enhancement_outcome")
    return outcome if outcome in ENGINE_OUTCOMES else "failed"


def redact(value):
    """A copy of an API response with download links removed (the evidence never carries a link to a stored photograph)."""
    if isinstance(value, dict):
        return {key: ("<redacted>" if key in ("download_url", "preview_url", "thumbnail_url") and item else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


TRIMMED_PARAMETERS = ("algorithm", "identity_status", "identity_verification", "warnings", "stage_reports", "resolution_contract", "pipeline_version")
TRIMMED_ADAPTIVE = ("outcome", "summary", "operations_applied", "operations_skipped", "stages", "unresolved_defects", "limits", "seconds")


def trim_response(body):
    """An enhancement result as the evidence keeps it: every field of the response except the bulky measurement reports inside ``applied_parameters``.

    What stays is what decides and explains the outcome (the algorithm, the identity block, the warnings, the stage list and the adaptive engine's own
    decisions). ``--full-responses`` keeps the whole body; the file then holds every measurement the API returned. A body that is not an enhancement
    result (for example the refusal list) is returned as it is.
    """
    if not isinstance(body, dict) or not isinstance(body.get("applied_parameters"), dict):
        return body
    parameters = body["applied_parameters"]
    kept = {key: parameters[key] for key in TRIMMED_PARAMETERS if key in parameters}
    if isinstance(parameters.get("adaptive"), dict):
        kept["adaptive"] = {key: parameters["adaptive"][key] for key in TRIMMED_ADAPTIVE if key in parameters["adaptive"]}
    kept["trimmed"] = True
    return {**body, "applied_parameters": kept}


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity_block_is_honest(block):
    """``verified`` may be true only when a comparison held (state ``verified``) or the photograph was not changed; a missing block makes no claim."""
    if not isinstance(block, dict) or not block.get("verified"):
        return True
    return block.get("state") in ("verified", "not_applicable")


def applied_operations(detail):
    """The operations the engine applied, from a (possibly trimmed) enhancement result; ``None`` when the result does not say."""
    adaptive = ((detail or {}).get("applied_parameters") or {}).get("adaptive")
    return adaptive.get("operations_applied") if isinstance(adaptive, dict) else None


def policy_checks(row, *, consent_granted, quality_action, identity_policy):
    """Did this case obey the settings of the run? ``{check name: bool}``; only the checks that apply to the combination are present.

    * ``quality_action == "reject"``: a photograph the quality check rejected is refused (400, nothing created), and a very dark case is rejected for
      ``severe_underexposure``; it is never enhanced.
    * ``identity_policy == "fail_closed"``: no result is ever labelled ``limited_unverified``; pixels change only when identity was verified; whenever identity
      was unavailable or inconclusive (no consent, no model, a face that cannot be compared) nothing was applied and the original was returned; the
      ``failed_closed`` mode ran no model at all; and without consent nothing is changed.
    """
    checks = {}
    quality = row["quality_decision"]
    classification = row["classification"]
    if quality_action == "reject":
        checks["policy_rejected_photo_is_refused_never_enhanced"] = quality["decision"] != "rejected" or classification == "expected_rejection"
        if row["case"].startswith("dark"):
            checks["policy_dark_case_rejected_for_underexposure"] = (
                quality["decision"] == "rejected" and "severe_underexposure" in quality["reasons"] and row["http"]["enhance"] == 400
                and classification == "expected_rejection")
    if identity_policy == "fail_closed" and classification in ENGINE_OUTCOMES:
        block = row.get("identity_verification") or {}
        detail = row["http"]["result_detail"]
        operations = applied_operations(detail)
        stages = (detail or {}).get("applied_stages")
        unverified = block.get("status") in ("unavailable", "inconclusive") or block.get("state") in ("not_verifiable", "uncertain")
        changed = classification in ("enhanced", "partially_enhanced")
        checks["policy_fail_closed_never_limited_unverified"] = block.get("mode") != "limited_unverified"
        checks["policy_fail_closed_changes_only_a_verified_identity"] = (not changed) or (block.get("mode") == "verified" and block.get("verified") is True)
        if unverified:
            checks["policy_unverified_identity_applied_nothing"] = (
                classification == "original_preserved" and operations == [] and stages in ([], None)
                and row["checks"].get("preserved_is_pixel_identical_to_input") is True)
        if block.get("mode") == "failed_closed":
            checks["policy_failed_closed_ran_no_model"] = row.get("models_executed") == []
        if not consent_granted:
            checks["policy_no_consent_means_no_change"] = classification == "original_preserved"
    return checks


def summarise(rows):
    counts = {name: sum(1 for row in rows if row["classification"] == name) for name in CLASSIFICATIONS}
    return {"cases": len(rows), "by_classification": counts, "all_checks_pass": bool(rows) and all(row["all_checks_pass"] for row in rows),
            "failed_cases": [row["case"] for row in rows if not row["all_checks_pass"]]}


def command_for_evidence(argv):
    """The command line as it is recorded: the script name and its options, with the two folder arguments replaced (they are paths on one person's machine)."""
    recorded, replace_next = [Path(argv[0]).name], None
    placeholders = {"--images": "<folder_of_consented_photos>", "--out": "<evidence_dir>"}
    for argument in argv[1:]:
        if replace_next:
            recorded.append(replace_next)
            replace_next = None
            continue
        key, _, value = argument.partition("=")
        if key in placeholders:
            if value:
                recorded.append(f"{key}={placeholders[key]}")
            else:
                recorded.append(key)
                replace_next = placeholders[key]
            continue
        recorded.append(argument)
    return recorded


def git_info(root):
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ""

    return {"commit": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def environment_info():
    info = {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count()}
    try:
        info["memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    for module in ("django", "cv2", "numpy", "torch", "lpips"):
        try:
            imported = __import__(module)
            info[module] = getattr(imported, "__version__", "installed")
        except Exception:
            info[module] = None
    try:
        import torch

        info["devices"] = {"cuda": bool(torch.cuda.is_available()), "mps": bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())}
    except Exception:
        info["devices"] = None
    return info


def manifest_pins():
    path = ROOT / "docs" / "gpu" / "model_manifest.json"
    try:
        return json.loads(path.read_text())["roots"]["models"]
    except Exception:
        return {}


def checkpoint_report(settings):
    """Each checkpoint this run may load: file name, size, SHA-256, the pinned value and whether they agree."""
    rows = []
    for prefix, label in CHECKPOINTS:
        path = Path(getattr(settings, f"{prefix}_MODEL_PATH", "") or "")
        pinned = (getattr(settings, f"{prefix}_MODEL_SHA256", "") or "").lower()
        row = {"model": label, "setting": f"{prefix}_MODEL_PATH", "file": path.name, "pinned_sha256": pinned or None}
        if path.is_file():
            row.update(bytes=path.stat().st_size, sha256=sha256_file(path))
            row["matches_pin"] = (row["sha256"] == pinned) if pinned else None
        else:
            row.update(bytes=None, sha256=None, matches_pin=None, note="file not present")
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--second-photo", type=int, default=1, help="Index of a second photograph for the fallback cases.")
    parser.add_argument("--consent", choices=("granted", "absent"), default="granted")
    parser.add_argument("--quality-underexposed", choices=("reject", "review"), default="reject", dest="quality_underexposed")
    parser.add_argument("--identity-policy", choices=("limited", "fail_closed"), default="limited", dest="identity_policy")
    parser.add_argument("--label", default="run")
    parser.add_argument("--full-responses", action="store_true", help="Keep the whole API response (the bulky measurement reports included) in the evidence.")
    parser.add_argument("--save-tiles", action="store_true", help="Also write side-by-side images (they contain your photographs: do not commit them).")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test.runner import DiscoverRunner
    from django.test.utils import override_settings, setup_test_environment
    from rest_framework.test import APIClient

    from benchmark_adaptive import degrade
    from enhancement.adaptive.validator import reference_metrics
    from enhancement.eligibility import REFUSAL
    from enhancement.models import ImageEnhancementResult
    from enhancement.services import enhance_bgr
    from privacy.services import biometric_processing_allowed
    from privacy.testing import grant_face_consent, withdraw_face_consent
    from quality_control.models import ImageQualityDecision
    from uploads.models import ImageUpload
    from validation.models import FaceValidationResult

    pins = manifest_pins()
    licence = Path(settings.IDENTITY_LICENSE_RECORD_PATH) if settings.IDENTITY_LICENSE_RECORD_PATH else None
    if licence is None or not licence.is_file():
        fallback = ROOT / ".runtime" / "licenses" / "identity-models.md"
        licence = fallback if fallback.is_file() else licence
    media = tempfile.mkdtemp(prefix="facecloud-verify-")
    flags = dict(
        MEDIA_ROOT=media, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True,
        ENHANCEMENT_RENDER_POLICY="adaptive_v1", RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=True,
        NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True, CODEFORMER_ENABLED=False, GFPGAN_ENABLED=False,
        ADAPTIVE_DEBUG_ARTIFACTS=False, IMAGE_ENCRYPTION_ENABLED=False, STORAGE_BACKEND="filesystem",
        BIOMETRIC_CONSENT_REQUIRED=True, FACE_DETECTOR_PROVIDER="yunet_v1", IDENTITY_PROVIDER="sface_2021dec_v1",
        QUALITY_UNDEREXPOSED_FACE_ACTION=arguments.quality_underexposed, ENHANCEMENT_IDENTITY_UNVERIFIED_POLICY=arguments.identity_policy,
    )
    if not settings.YUNET_MODEL_SHA256 and pins.get("yunet_s_dynamic.onnx"):
        flags["YUNET_MODEL_SHA256"] = pins["yunet_s_dynamic.onnx"]["sha256"]
    if not settings.IDENTITY_MODEL_SHA256 and pins.get("face_recognition_sface_2021dec.onnx"):
        flags["IDENTITY_MODEL_SHA256"] = pins["face_recognition_sface_2021dec.onnx"]["sha256"]
    if licence is not None and licence.is_file():
        flags["IDENTITY_LICENSE_RECORD_PATH"] = str(licence)

    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if len(paths) < 2:
        raise SystemExit(f"{arguments.images} needs at least two images (JPEG, PNG or WebP).")

    def load(path):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        original_size = [int(image.shape[1]), int(image.shape[0])]
        scale = arguments.max_edge / max(image.shape[:2])
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
        return image, original_size

    (first, first_size), (second, second_size) = load(paths[0]), load(paths[arguments.second_photo])
    sources = [
        {"index": 0, "role": "first", "sha256_prefix": sha256_file(paths[0])[:16], "original_size": first_size, "used_size": [int(first.shape[1]), int(first.shape[0])]},
        {"index": arguments.second_photo, "role": "second", "sha256_prefix": sha256_file(paths[arguments.second_photo])[:16], "original_size": second_size,
         "used_size": [int(second.shape[1]), int(second.shape[0])]},
    ]
    rng = np.random.default_rng(9)

    def defocused(image):
        return degrade("defocus", image, rng)

    cases = [
        ("dark_portrait", "first", first, degrade("dark", first, rng), "synthetic: x0.25 light + Gaussian noise sigma 2"),
        ("noisy_portrait", "first", first, degrade("noisy", first, rng), "synthetic: Gaussian noise sigma 18"),
        ("motion_blur", "first", first, degrade("motion", first, rng), "synthetic: 19-pixel horizontal linear motion kernel"),
        ("dark_and_defocused", "first", first, defocused(degrade("dark", first, rng)), "synthetic: dark (x0.25) then 11-pixel disk defocus"),
        ("clean_photograph", "first", first, first.copy(), "none: the untouched photograph"),
        ("motion_blur_second_photo", "second", second, degrade("motion", second, rng), "synthetic: 19-pixel horizontal linear motion kernel"),
    ]

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    arguments.out.mkdir(parents=True, exist_ok=True)
    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old_databases = runner.setup_databases()
    rows, run_settings, checkpoints, consent_effective = [], {}, [], None
    try:
        with override_settings(**flags):
            run_settings = {name: getattr(settings, name, None) for name in RECORDED_SETTINGS}
            run_settings = {name: (str(value) if isinstance(value, Path) else value) for name, value in run_settings.items()}
            checkpoints = checkpoint_report(settings)

            detector, identity = None, {}
            try:
                from identities.providers import ArcFaceOnnxProvider, SFaceOnnxProvider
                from validation.services import OpenCVYuNetFaceDetector

                detector = OpenCVYuNetFaceDetector()
                for label, provider in (("sface", SFaceOnnxProvider()), ("arcface", ArcFaceOnnxProvider())):
                    try:
                        provider.healthcheck()
                        identity[label] = provider
                    except Exception:
                        pass
            except Exception:
                detector = None

            def similarity(clean, image):
                values = {}
                for label, provider in identity.items():
                    try:
                        def embed(picture):
                            found = detector.detect(picture)
                            index = int(np.argmax([b[2] * b[3] for b in found.boxes]))
                            x, y, w, h = found.boxes[index]
                            return provider.embed(provider.prepare_face(picture, {"x": x, "y": y, "width": w, "height": h}, found.landmarks[index]))

                        resized = cv2.resize(image, (clean.shape[1], clean.shape[0]), interpolation=cv2.INTER_AREA)
                        values[label] = round(float(provider.similarity(embed(clean), embed(resized))), 4)
                    except Exception:
                        values[label] = None
                return values

            def enhanced_files():
                return {p for p in (Path(media) / "users").rglob("*") if p.is_file() and "/enhanced/" in str(p)}

            def stored_original_digest(image):
                image.original_file.open("rb")
                try:
                    return sha256_bytes(image.original_file.read())
                finally:
                    image.original_file.close()

            user = get_user_model().objects.create_user(username="verify", email="verify@example.com", password="ForestRiver!4821", is_email_verified=True)
            (grant_face_consent if arguments.consent == "granted" else withdraw_face_consent)(user)
            consent_effective = biometric_processing_allowed(user)
            client = APIClient()
            client.force_authenticate(user)
            for name, which, clean, degraded, degradation in cases:
                row = {"case": name, "source": which, "degradation": degradation, "input_size": [int(degraded.shape[1]), int(degraded.shape[0])], "checks": {}}
                started = time.perf_counter()
                encoded = cv2.imencode(".png", degraded)[1].tobytes()
                upload = client.post("/api/images/upload/single/", {"file": SimpleUploadedFile(f"{name}.png", encoded, "image/png")}, format="multipart")
                image = ImageUpload.objects.filter(owner=user).latest("id")
                original_before = stored_original_digest(image)
                quality = ImageQualityDecision.objects.filter(image=image).first()
                face_check = FaceValidationResult.objects.filter(image=image).first()
                row["quality_decision"] = {
                    "decision": getattr(quality, "decision", None), "reasons": list(getattr(quality, "rejection_reasons", None) or []),
                    "mean_luminance": (getattr(quality, "measurements", None) or {}).get("mean_luminance"),
                    "contrast_stddev": (getattr(quality, "measurements", None) or {}).get("contrast_stddev"),
                }
                row["face_check"] = {"decision": getattr(face_check, "decision", None), "face_count": getattr(face_check, "face_count", None),
                                     "rejection_reason": getattr(face_check, "rejection_reason", "") or ""}
                files_before = enhanced_files()
                queued = client.post(f"/api/enhancements/images/{image.pk}/", {"level": "auto", "style": "natural"}, format="json")
                enhance_body = queued.json()
                detail, output, download = None, None, None
                if queued.status_code == 202 and isinstance(enhance_body, dict):
                    detail = client.get(f"/api/enhancements/results/{enhance_body['id']}/").json()
                    if detail.get("download_url"):
                        download = client.get(urlparse(detail["download_url"]).path)
                        if download.status_code == 200:
                            output = cv2.imdecode(np.frombuffer(b"".join(download.streaming_content), np.uint8), cv2.IMREAD_COLOR)
                row["seconds"] = round(time.perf_counter() - started, 1)
                shape = (lambda body: redact(body)) if arguments.full_responses else (lambda body: redact(trim_response(body)))
                row["http"] = {"upload": upload.status_code, "enhance": queued.status_code, "enhance_body": shape(enhance_body),
                               "result_detail": shape(detail), "download": getattr(download, "status_code", None)}
                classification = classify_case(queued.status_code, enhance_body, detail, row["quality_decision"]["decision"], REFUSAL)
                row["classification"] = classification
                row["summary"] = (detail or {}).get("enhancement_summary")
                row["identity_status"] = (detail or {}).get("identity_status")
                block = ((detail or {}).get("applied_parameters") or {}).get("identity_verification")
                row["identity_verification"] = block
                new_files = enhanced_files() - files_before
                results = ImageEnhancementResult.objects.filter(image=image)
                checks = row["checks"]
                checks["upload_ok"] = upload.status_code == 201
                checks["stored_original_unchanged"] = stored_original_digest(image) == original_before
                checks["identity_never_claimed_without_a_comparison"] = identity_block_is_honest(block)
                if classification == "expected_rejection":
                    checks["refused_with_400_and_the_stock_sentence"] = queued.status_code == 400 and enhance_body == [REFUSAL]
                    checks["quality_decision_is_rejected_with_reasons"] = row["quality_decision"]["decision"] == "rejected" and bool(row["quality_decision"]["reasons"])
                    checks["no_enhancement_result_created"] = not results.exists()
                    checks["no_output_file_stored"] = not new_files
                    row["input_vs_clean"] = reference_metrics(clean, degraded)
                elif classification in ENGINE_OUTCOMES:
                    checks["http_202_then_completed"] = queued.status_code == 202 and detail["status"] == "completed"
                    checks["download_ok"] = output is not None
                    result = results.latest("id")
                    checks["one_output_file"] = len(new_files) == 1 and Path(result.enhanced_file.name).name in {p.name for p in new_files}
                    checks["output_size_matches_report"] = output is not None and [output.shape[1], output.shape[0]] == [detail.get("output_width"), detail.get("output_height")]
                    checks["no_debug_or_intermediate_files"] = not ((Path(media) / "debug").exists() and list((Path(media) / "debug").rglob("*")))
                    if classification == "original_preserved" and output is not None:
                        checks["preserved_is_pixel_identical_to_input"] = bool(np.array_equal(output, degraded))
                    if classification in ("enhanced", "partially_enhanced") and output is not None:
                        checks["changed_output_differs_from_input"] = not np.array_equal(output, degraded)
                    row["algorithm"] = detail["applied_parameters"].get("algorithm")
                    row["output_size"] = [detail.get("output_width"), detail.get("output_height")]
                else:
                    checks["completed_without_error"] = False
                if output is not None:
                    row["input_vs_clean"] = reference_metrics(clean, degraded)
                    row["output_vs_clean"] = reference_metrics(clean, output)
                    row["identity_input_vs_clean"], row["identity_output_vs_clean"] = similarity(clean, degraded), similarity(clean, output)
                    if arguments.save_tiles:
                        height = 360
                        tiles = [cv2.resize(i, (int(i.shape[1] * height / i.shape[0]), height)) for i in (clean, degraded, output)]
                        for tile, label in zip(tiles, ("clean original", f"degraded: {name}", f"FaceCloud: {classification}")):
                            cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                        cv2.imwrite(str(arguments.out / f"{name}.jpg"), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 90])
                if classification in ENGINE_OUTCOMES:
                    # the same input again in debug mode, only to record which models really ran (rejected candidates included); the same consent as the API call
                    with override_settings(ADAPTIVE_DEBUG_ARTIFACTS=True):
                        _out, _level, debug_parameters = enhance_bgr(degraded, "auto", allow_biometric_processing=consent_effective)
                    adaptive = debug_parameters["adaptive"]
                    row["same_decision_in_debug_mode"] = adaptive["outcome"] == classification
                    row["models_executed"] = [
                        {"candidate": c["id"], "operation": c["operation"], "variant": c["variant"], "accepted": (c["verdict"] or {}).get("accepted"),
                         "rejected_for": (c["verdict"] or {}).get("reasons"), "models": [m.get("model") for m in c["models"] if m.get("model")],
                         "device": next((m.get("device") for m in c["models"] if m.get("device")), None), "seconds": c["seconds"]}
                        for c in adaptive["candidates"] if c["operation"] != "original"
                    ]
                    row["operations_skipped"] = adaptive["operations_skipped"]
                    row["unresolved_defects"] = adaptive["unresolved_defects"]
                    shutil.rmtree(Path(media) / "debug", ignore_errors=True)
                checks.update(policy_checks(row, consent_granted=bool(consent_effective), quality_action=arguments.quality_underexposed,
                                            identity_policy=arguments.identity_policy))
                row["all_checks_pass"] = classification != "failed" and all(checks.values())
                rows.append(row)
                print(f"{name:26s} {classification:20s} checks={'PASS' if row['all_checks_pass'] else 'FAIL'} {row['seconds']}s", flush=True)
    finally:
        runner.teardown_databases(old_databases)
        shutil.rmtree(media, ignore_errors=True)

    summary = summarise(rows)
    evidence = {
        "schema": 1, "label": arguments.label, "started_at": started_at, "command": command_for_evidence(sys.argv), "git": git_info(ROOT),
        "environment": environment_info(), "settings": run_settings, "consent": {"requested": arguments.consent, "effective": consent_effective},
        "checkpoints": checkpoints, "sources": sources, "max_edge": arguments.max_edge, "cases": rows, "summary": summary,
        "notes": ["Synthetic degradations of the supplied photographs; the clean photograph is the reference. PSNR/SSIM are whole-image, SSIM is the project's luminance proxy; "
                  "identity similarities compare the largest face of the clean photograph with that of the degraded/output one. No photograph or face crop is stored here."],
    }
    (arguments.out / "evidence.json").write_text(json.dumps(evidence, indent=1, default=str))
    (arguments.out / "verification.json").write_text(json.dumps(rows, indent=1, default=str))
    print(json.dumps(summary))
    sys.exit(0 if summary["all_checks_pass"] else 1)


if __name__ == "__main__":
    main()
