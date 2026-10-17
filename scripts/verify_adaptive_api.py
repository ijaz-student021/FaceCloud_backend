"""Verify the adaptive engine end to end through the real HTTP API, with the real models, on a disposable test database.

    python scripts/verify_adaptive_api.py --images folder_of_photos --out report_dir [--max-edge 640]

For each case a clean photograph is degraded in a known way and then sent through the same endpoints a client uses:

    POST /api/images/upload/single/          (the standard upload and preprocessing)
    POST /api/enhancements/images/{id}/      (Auto Enhance: level auto, style natural)
    GET  /api/enhancements/results/{id}/     (status, enhancement_outcome, enhancement_summary, output size)
    GET  <download_url>                      (the one final image)

in **production mode** (debug artifacts off), and it checks that exactly one output file exists per result, that the downloaded
bytes decode to the reported size, that an ``original_preserved`` result is pixel-identical to its input, and that no intermediate or debug
image was stored. It then re-runs the engine in debug mode on the same input to record which models actually executed (including
candidates the validator rejected), and scores input and output against the clean photograph (PSNR, SSIM, LPIPS when available,
ArcFace and SFace similarity).

The database is a throwaway test database; media goes to a temporary folder; nothing is uploaded anywhere. The photographs are not
modified and the report folder stays on this machine. Face restoration is left OFF (its default). Models are the ones switched on below.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-edge", type=int, default=640)
    parser.add_argument("--second-photo", type=int, default=1, help="Index of a second photograph for the fallback cases.")
    arguments = parser.parse_args()

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test.runner import DiscoverRunner
    from django.test.utils import override_settings, setup_test_environment
    from rest_framework.test import APIClient

    from benchmark_adaptive import degrade
    from enhancement.adaptive.validator import reference_metrics
    from enhancement.models import ImageEnhancementResult
    from enhancement.services import enhance_bgr

    media = tempfile.mkdtemp(prefix="facecloud-verify-")
    flags = dict(
        MEDIA_ROOT=media, CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True,
        ENHANCEMENT_RENDER_POLICY="adaptive_v1", RETINEXFORMER_ENABLED=True, ZERO_DCE_ENABLED=False, NAFNET_DENOISE_ENABLED=True,
        NAFNET_DEBLUR_ENABLED=True, HD_RESTORATION_ENABLED=True, CODEFORMER_ENABLED=False, GFPGAN_ENABLED=False,
        ADAPTIVE_DEBUG_ARTIFACTS=False, IMAGE_ENCRYPTION_ENABLED=False, STORAGE_BACKEND="filesystem",
    )
    paths = sorted(p for p in arguments.images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})

    def load(path):
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        scale = arguments.max_edge / max(image.shape[:2])
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image

    first, second = load(paths[0]), load(paths[arguments.second_photo])
    rng = np.random.default_rng(9)

    def defocused(image):
        return degrade("defocus", image, rng)

    cases = [
        ("dark_portrait", "first", first, degrade("dark", first, rng)),
        ("noisy_portrait", "first", first, degrade("noisy", first, rng)),
        ("motion_blur", "first", first, degrade("motion", first, rng)),
        ("dark_and_defocused", "first", first, defocused(degrade("dark", first, rng))),
        ("clean_photograph", "first", first, first.copy()),
        ("motion_blur_second_photo", "second", second, degrade("motion", second, rng)),
    ]

    identity = {}
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

    def enhanced_files():
        return {p for p in (Path(media) / "users").rglob("*") if p.is_file() and "/enhanced/" in str(p)}

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

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old_databases = runner.setup_databases()
    arguments.out.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        with override_settings(**flags):
            user = get_user_model().objects.create_user(username="verify", email="verify@example.com", password="ForestRiver!4821",
                                                        is_email_verified=True)
            client = APIClient()
            client.force_authenticate(user)
            for name, which, clean, degraded in cases:
                row = {"case": name, "checks": {}}
                started = time.perf_counter()
                encoded = cv2.imencode(".png", degraded)[1].tobytes()
                upload = client.post("/api/images/upload/single/", {"file": SimpleUploadedFile(f"{name}.png", encoded, "image/png")},
                                     format="multipart")
                row["upload_status"] = upload.status_code
                from uploads.models import ImageUpload

                image_id = ImageUpload.objects.filter(owner=user).latest("id").pk
                files_before = enhanced_files()
                queued = client.post(f"/api/enhancements/images/{image_id}/", {"level": "auto", "style": "natural"}, format="json")
                row["enhance_status_code"] = queued.status_code
                result_id = queued.json()["id"]
                detail = client.get(f"/api/enhancements/results/{result_id}/").json()
                row["seconds"] = round(time.perf_counter() - started, 1)
                row.update(status=detail["status"], outcome=detail.get("enhancement_outcome"), summary=detail.get("enhancement_summary"),
                           output=[detail.get("output_width"), detail.get("output_height")], identity_status=detail.get("identity_status"),
                           algorithm=detail["applied_parameters"].get("algorithm"))
                download = client.get(urlparse(detail["download_url"]).path) if detail.get("download_url") else None
                output = None
                if download is not None and download.status_code == 200:
                    data = b"".join(download.streaming_content)
                    output = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                result = ImageEnhancementResult.objects.get(pk=result_id)
                new_files = enhanced_files() - files_before
                debug_files = list((Path(media) / "debug").rglob("*")) if (Path(media) / "debug").exists() else []
                checks = row["checks"]
                checks["http_202_then_completed"] = queued.status_code == 202 and detail["status"] == "completed"
                checks["download_ok"] = output is not None
                checks["one_output_file"] = len(new_files) == 1 and Path(result.enhanced_file.name).name in {p.name for p in new_files}
                checks["output_size_matches_report"] = output is not None and [output.shape[1], output.shape[0]] == row["output"]
                checks["no_debug_or_intermediate_files"] = not debug_files
                checks["outcome_is_one_of_three"] = row["outcome"] in ("enhanced", "partially_enhanced", "original_preserved")
                if row["outcome"] == "original_preserved" and output is not None:
                    checks["preserved_is_pixel_identical_to_input"] = bool(np.array_equal(output, degraded))
                if row["outcome"] in ("enhanced", "partially_enhanced") and output is not None:
                    checks["changed_output_differs_from_input"] = not np.array_equal(output, degraded)
                row["all_checks_pass"] = all(checks.values())
                if output is not None:
                    row["input_vs_clean"] = reference_metrics(clean, degraded)
                    row["output_vs_clean"] = reference_metrics(clean, output)
                    row["identity_input"], row["identity_output"] = similarity(clean, degraded), similarity(clean, output)
                    height = 360
                    tiles = [cv2.resize(i, (int(i.shape[1] * height / i.shape[0]), height)) for i in (clean, degraded, output)]
                    for tile, label in zip(tiles, ("clean original", f"degraded: {name}", f"FaceCloud: {row['outcome']}")):
                        cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
                    cv2.imwrite(str(arguments.out / f"{name}.jpg"), np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 90])
                # the same input again in debug mode, only to record which models really ran (rejected candidates included)
                with override_settings(ADAPTIVE_DEBUG_ARTIFACTS=True):
                    _out, _level, debug_parameters = enhance_bgr(degraded, "auto")
                adaptive = debug_parameters["adaptive"]
                row["same_decision_in_debug_mode"] = adaptive["outcome"] == row["outcome"]
                row["models_executed"] = [
                    {"candidate": c["id"], "operation": c["operation"], "variant": c["variant"], "accepted": (c["verdict"] or {}).get("accepted"),
                     "rejected_for": (c["verdict"] or {}).get("reasons"), "models": [m.get("model") for m in c["models"] if m.get("model")],
                     "device": next((m.get("device") for m in c["models"] if m.get("device")), None), "seconds": c["seconds"]}
                    for c in adaptive["candidates"] if c["operation"] != "original"
                ]
                row["operations_skipped"] = adaptive["operations_skipped"]
                row["unresolved_defects"] = adaptive["unresolved_defects"]
                shutil.rmtree(Path(media) / "debug", ignore_errors=True)
                rows.append(row)
                print(f"{name:26s} {row['outcome']!s:20s} checks={'PASS' if row['all_checks_pass'] else 'FAIL'} {row['seconds']}s", flush=True)
    finally:
        runner.teardown_databases(old_databases)
        shutil.rmtree(media, ignore_errors=True)
    (arguments.out / "verification.json").write_text(json.dumps(rows, indent=2, default=str))
    print(json.dumps({"cases": len(rows), "all_checks_pass": all(r["all_checks_pass"] for r in rows),
                      "outcomes": sorted({r["outcome"] for r in rows})}))


if __name__ == "__main__":
    main()
