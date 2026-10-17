"""Run every learned model for real and say plainly which ones worked.

Each component is loaded from its real checkpoint and run on an input that suits
it, in its own process so its time and peak memory are its own. Nothing is
mocked. For each one the script records:

* integrity: the file's SHA-256 against the pinned value (settings, else the
  manifest);
* direct inference: the adapter called on a suitable image, with the output's
  shape, datatype, finiteness and difference from the input checked, and the
  result encoded and read back;
* pipeline integration: ``enhance_bgr`` with only that stage switched on, on an
  input that should route to it, reporting the stage's status, device and
  fallback reason. A stage the router skipped is reported as skipped, never as
  a success.

    .runtime/venvs/gfpgan/bin/python scripts/validate_models.py \\
        --portrait ~/Downloads/portrait.jpg --name first-run

Use the worker environment: it is the only one with every runtime (PyTorch and
GFPGAN). Output goes to ``.runtime/model_validation/<name>/``: ``report.md``,
``results.json`` and before/after images. The exit code is non-zero unless every
component passed both checks.

A pass means the model loaded, ran and returned a well-formed image through the
pipeline. It is not a judgement that the result looks good; look at the images.
"""
import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Model validation exercises each learned model on purpose, so it selects the
# learned_v1 rendering policy (enhancement/policy.py); the default policy
# would keep every one of them off.
OFF = dict(
    ENHANCEMENT_RENDER_POLICY="learned_v1",
    ZERO_DCE_ENABLED=False, AI_RESTORATION_ENABLED=False, HD_RESTORATION_ENABLED=False,
    NAFNET_DEBLUR_ENABLED=False, PURESCALE_ENABLED=False, PUREVISION_ENABLED=False,
    GFPGAN_ENABLED=False, COLORIZATION_ENABLED=False, IDENTITY_GUARD_ENABLED=False,
)
# component -> (checkpoint settings, pinned-checksum setting or manifest key, stage it drives)
COMPONENTS = {
    "zero_dce": (["ZERO_DCE_MODEL_PATH"], ["manifest:models/zero_dce_epoch99.onnx"], "exposure_enhancement"),
    "edsr": (["AI_RESTORATION_EDSR_MODEL_PATH"], ["manifest:models/EDSR_x2.pb"], "low_resolution_restoration"),
    "nafnet": (["NAFNET_MODEL_PATH"], ["NAFNET_MODEL_SHA256"], "hd_restoration"),
    "realesrgan": (["REALESRGAN_MODEL_PATH"], ["REALESRGAN_MODEL_SHA256"], "hd_restoration"),
    "purevision": (["PUREVISION_MODEL_PATH"], ["PUREVISION_MODEL_SHA256"], "purevision_restoration"),
    "purescale": (["PURESCALE_MODEL_PATH"], ["PURESCALE_MODEL_SHA256"], "purescale_super_resolution"),
    "gfpgan": (
        ["GFPGAN_MODEL_PATH", "GFPGAN_DETECTION_MODEL_PATH", "GFPGAN_PARSING_MODEL_PATH"],
        ["GFPGAN_MODEL_SHA256", "GFPGAN_DETECTION_MODEL_SHA256", "GFPGAN_PARSING_MODEL_SHA256"],
        "face_restoration",
    ),
    "colorization": (["COLORIZATION_MODEL_PATH"], ["COLORIZATION_MODEL_SHA256"], "colorization"),
    "yunet": (["YUNET_MODEL_PATH"], ["YUNET_MODEL_SHA256"], "face_fidelity"),
    "sface": (["IDENTITY_MODEL_PATH"], ["IDENTITY_MODEL_SHA256"], "identity_guard"),
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity(component):
    """Each checkpoint's digest against its pinned value; never adjusts the pin."""
    from django.conf import settings

    manifest = json.loads((ROOT / "docs" / "gpu" / "model_manifest.json").read_text())["roots"]
    files = []
    path_settings, pins, _stage = COMPONENTS[component]
    for path_setting, pin in zip(path_settings, pins):
        path = Path(str(getattr(settings, path_setting))).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if pin.startswith("manifest:"):
            root, name = pin.split(":", 1)[1].split("/", 1)
            expected = manifest[root][name]["sha256"]
            source = "manifest"
        else:
            expected = str(getattr(settings, pin)).strip().lower()
            source = "settings"
        record = {"file": path.name, "expected_sha256": expected, "pinned_in": source, "exists": path.is_file()}
        if record["exists"]:
            record["sha256"] = sha256(path)
            record["size"] = path.stat().st_size
            record["matches"] = bool(expected) and record["sha256"] == expected
        else:
            record["matches"] = False
        files.append(record)
    return files


def working_image(portrait, longest):
    import cv2

    scale = longest / max(portrait.shape[:2])
    if scale >= 1:
        return portrait
    return cv2.resize(portrait, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def suitable_input(component, portrait):
    """An image this component is meant for, derived from the portrait."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(5)
    if component == "zero_dce":
        base = working_image(portrait, 1400).astype(np.float32) / 255.0
        dark = (base ** 2.2 * 0.12) ** (1 / 2.2) * 255.0 + rng.normal(0, 2.0, base.shape)
        return np.clip(dark, 0, 255).astype(np.uint8), "portrait darkened about three stops, light grain"
    if component == "edsr":
        return working_image(portrait, 360), "portrait reduced to 360 px (genuinely low resolution)"
    if component == "nafnet":
        kernel = np.zeros((15, 15), np.float32)
        kernel[7, :] = 1 / 15
        return cv2.filter2D(working_image(portrait, 900), -1, kernel), "portrait at 900 px with 15 px horizontal motion blur"
    if component in {"realesrgan", "purescale"}:
        return working_image(portrait, 640), "portrait reduced to 640 px"
    if component == "purevision":
        small = working_image(portrait, 900)
        return cv2.imdecode(cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 12])[1], cv2.IMREAD_COLOR), \
            "portrait at 900 px, JPEG quality 12"
    if component == "gfpgan":
        small = cv2.GaussianBlur(working_image(portrait, 420), (0, 0), 1.6)
        return cv2.imdecode(cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 35])[1], cv2.IMREAD_COLOR), \
            "portrait at 420 px, blurred and compressed (a degraded face)"
    if component == "colorization":
        small = working_image(portrait, 1000)
        return cv2.cvtColor(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR), "portrait at 1000 px, grayscale"
    return working_image(portrait, 1400), "portrait at 1400 px"


def well_formed(output, expected_shape, reference=None):
    import numpy as np

    checks = {
        "is_uint8_image": isinstance(output, np.ndarray) and output.dtype == np.uint8 and output.ndim == 3,
        "shape": list(output.shape) if hasattr(output, "shape") else None,
        "expected_shape": list(expected_shape),
    }
    checks["shape_ok"] = checks["shape"] == checks["expected_shape"]
    checks["finite"] = bool(np.isfinite(output).all()) if checks["is_uint8_image"] else False
    checks["not_constant"] = bool(output.std() > 1.0) if checks["is_uint8_image"] else False
    if reference is not None and checks["shape_ok"]:
        checks["mean_absolute_change"] = round(float(np.abs(output.astype(np.int16) - reference.astype(np.int16)).mean()), 3)
        checks["changed_input"] = checks["mean_absolute_change"] > 0.05
    return checks


def encoded_ok(image):
    from types import SimpleNamespace

    from enhancement.services import _encode_output, _verify_encoded_output

    payload, _extension, output_format = _encode_output(image, SimpleNamespace(output_format="jpeg"))
    return _verify_encoded_output(payload, image, output_format)


def direct_inference(component, image, portrait):
    """Call the adapter itself. Returns ``(output or None, record)``."""
    import cv2
    import numpy as np
    from django.conf import settings

    height, width = image.shape[:2]
    record = {}
    if component == "zero_dce":
        from enhancement.zero_dce import enhance_with_zero_dce
        output, metadata = enhance_with_zero_dce(image)
        expected = image.shape
    elif component == "edsr":
        from enhancement.services import analyze_image_quality, restore_low_resolution
        output, metadata = restore_low_resolution(image, analyze_image_quality(image))
        if not metadata.get("applied"):
            raise RuntimeError(metadata.get("fallback_reason") or "EDSR did not run")
        expected = (height * 2, width * 2, 3)
    elif component == "nafnet":
        from enhancement.hd_restoration import deblur_with_nafnet
        output, metadata = deblur_with_nafnet(image)
        expected = image.shape
    elif component == "realesrgan":
        from enhancement.hd_restoration import upscale_with_realesrgan
        output, metadata = upscale_with_realesrgan(image)
        scale = int(settings.HD_RESTORATION_OUTPUT_SCALE)
        expected = (height * scale, width * scale, 3)
    elif component == "purevision":
        from enhancement.purevision import restore_with_purevision
        output, metadata = restore_with_purevision(image)
        expected = image.shape
    elif component == "purescale":
        from enhancement.purescale import upscale_with_purescale
        output, metadata = upscale_with_purescale(image)
        expected = (height * 2, width * 2, 3)
    elif component == "gfpgan":
        from enhancement.gfpgan import restore_faces
        output, metadata = restore_faces(image)
        expected = image.shape
    elif component == "colorization":
        from enhancement.colorization import colorize_with_siggraph17
        output, metadata = colorize_with_siggraph17(image)
        expected = image.shape
        lab = cv2.cvtColor(output, cv2.COLOR_BGR2LAB).astype(np.float32)
        record["mean_chroma_added"] = round(float(np.abs(lab[:, :, 1:] - 128).mean()), 3)
    elif component == "yunet":
        from validation.services import OpenCVYuNetFaceDetector
        detection = OpenCVYuNetFaceDetector().detect(image)
        record.update({
            "faces": len(detection.boxes), "scores": detection.scores,
            "five_landmarks_each": all(len(points) == 5 for points in detection.landmarks),
            "device": "cpu",
        })
        record["passed"] = record["faces"] >= 1 and record["five_landmarks_each"]
        annotated = image.copy()
        for (x, y, w, h), points in zip(detection.boxes, detection.landmarks):
            cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 3)
            for px, py in points:
                cv2.circle(annotated, (int(px), int(py)), 6, (0, 0, 255), -1)
        return annotated, record
    elif component == "sface":
        from identities.providers import get_identity_provider
        from validation.services import OpenCVYuNetFaceDetector
        provider = get_identity_provider()
        provider.healthcheck()
        detector = OpenCVYuNetFaceDetector()

        def embed(picture):
            found = detector.detect(picture)
            x, y, w, h = found.boxes[0]
            aligned = provider.prepare_face(picture, {"x": x, "y": y, "width": w, "height": h}, found.landmarks[0])
            return provider.validate_embedding(provider.embed(aligned)), aligned

        same, aligned = embed(image)
        brighter, _ = embed(cv2.convertScaleAbs(image, alpha=1.15, beta=8))
        blurred, _ = embed(cv2.GaussianBlur(image, (0, 0), 6))
        mirrored, _ = embed(cv2.flip(image, 1))
        record.update({
            "provider": provider.name, "device": "cpu", "embedding_dimension": int(len(same)),
            "embedding_finite": bool(np.isfinite(same).all()),
            "similarity_to_itself": round(float(provider.similarity(same, same)), 4),
            "similarity_to_brightened_copy": round(float(provider.similarity(same, brighter)), 4),
            "similarity_to_mirrored_copy": round(float(provider.similarity(same, mirrored)), 4),
            "similarity_to_heavily_blurred_copy": round(float(provider.similarity(same, blurred)), 4),
        })
        record["passed"] = (
            record["embedding_dimension"] == int(settings.IDENTITY_EMBEDDING_DIMENSION)
            and record["embedding_finite"]
            and record["similarity_to_itself"] > 0.99
            and record["similarity_to_brightened_copy"] > record["similarity_to_heavily_blurred_copy"]
        )
        return aligned, record
    else:
        raise ValueError(component)

    record.update({key: metadata[key] for key in ("provider", "device", "tile_size", "memory_retries", "faces_restored")
                   if key in metadata})
    record["output"] = well_formed(output, expected, image if tuple(expected) == image.shape else None)
    record["encoded"] = encoded_ok(output) if record["output"]["is_uint8_image"] else None
    record["passed"] = bool(
        record["output"]["shape_ok"] and record["output"]["finite"] and record["output"]["not_constant"]
        and record["output"].get("changed_input", True) and record["encoded"] and record["encoded"]["verified"]
    )
    return output, record


def pipeline_settings(component):
    """Only this component's stage switched on."""
    from django.conf import settings

    on = dict(OFF)
    if component == "zero_dce":
        on["ZERO_DCE_ENABLED"] = True
    elif component == "edsr":
        on["AI_RESTORATION_ENABLED"] = True
    elif component == "nafnet":
        on.update(HD_RESTORATION_ENABLED=True, NAFNET_DEBLUR_ENABLED=True,
                  # Deblur alone: an impossible upscale limit leaves NAFNet as the stage's whole result.
                  HD_RESTORATION_MAX_OUTPUT_PIXELS=1)
    elif component == "realesrgan":
        on["HD_RESTORATION_ENABLED"] = True
    elif component == "purevision":
        on["PUREVISION_ENABLED"] = True
    elif component == "purescale":
        on["PURESCALE_ENABLED"] = True
    elif component == "gfpgan":
        on.update(GFPGAN_ENABLED=True, IDENTITY_GUARD_ENABLED=True)
    elif component == "colorization":
        on["COLORIZATION_ENABLED"] = True
    elif component == "yunet":
        on["AI_RESTORATION_ENABLED"] = bool(settings.AI_RESTORATION_ENABLED)
    elif component == "sface":
        on["IDENTITY_GUARD_ENABLED"] = True
    return on


def pipeline_run(component, image, level="auto", style="natural", extra=None):
    """``enhance_bgr`` with one stage on; reports what that stage actually did."""
    from django.test.utils import override_settings

    from enhancement.services import enhance_bgr

    stage = COMPONENTS[component][2]
    with override_settings(**{**pipeline_settings(component), **(extra or {})}):
        output, applied_level, parameters = enhance_bgr(image, level, style=style)
    entry = [item for item in parameters["stage_reports"] if item["stage"] == stage][-1]
    record = {
        "stage": stage, "status": entry["status"], "changed_pixels": entry["changed_pixels"],
        "duration_ms": entry["duration_ms"], "applied_level": str(applied_level),
        "fallback_reason": entry.get("fallback_reason", ""),
        "output_shape": list(output.shape), "warnings": parameters["warnings"],
        "identity_status": parameters["identity_status"],
        "algorithm": parameters["algorithm"],
    }
    for key in ("provider", "device", "tile_size", "memory_retries", "models", "outcome"):
        if key in entry:
            record[key] = entry[key]
    if component == "zero_dce":
        record["zero_dce_applied"] = bool(parameters["zero_dce"].get("applied"))
        record["device"] = parameters["zero_dce"].get("device")
        record["ran_model"] = record["zero_dce_applied"]
    elif component == "nafnet":
        record["ran_model"] = bool(parameters["hd_restoration"]["deblur"].get("applied"))
        record["device"] = parameters["hd_restoration"]["deblur"].get("device")
    elif component == "yunet":
        record["faces_checked"] = parameters["face_fidelity"].get("faces_checked")
        record["denoise_faces_checked"] = parameters["adaptive_denoise"].get("faces_checked")
        record["ran_model"] = entry["outcome"] in {"passed", "strength_reduced", "reverted"} or bool(record["denoise_faces_checked"])
    elif component == "sface":
        guard = parameters["identity_guard"]
        record.update({key: guard.get(key) for key in ("status", "similarity", "faces_compared", "threshold")})
        record["ran_model"] = parameters["identity_status"] in {"verified", "failed"}
    elif component == "gfpgan":
        record["face_restoration"] = {
            key: parameters["face_restoration"].get(key)
            for key in ("applied", "identity_gate", "withdrawn", "output_blend", "faces_restored", "device", "fallback_reason")
        }
        record["identity_verification"] = parameters["identity_verification"]
        record["ran_model"] = bool(parameters["face_restoration"].get("applied") or parameters["face_restoration"].get("withdrawn")
                                   or parameters["face_restoration"].get("candidate_guard"))
    else:
        record["ran_model"] = entry["status"] in {"accepted", "partially_accepted"}
    record["passed"] = bool(record["ran_model"])
    return output, record, parameters


def face_crop(image, box_fraction, pad=0.35):
    height, width = image.shape[:2]
    x, y, w, h = box_fraction
    left, top = int(max(0, (x - w * pad) * width)), int(max(0, (y - h * pad) * height))
    right, bottom = int(min(width, (x + w * (1 + pad)) * width)), int(min(height, (y + h * (1 + pad)) * height))
    return image[top:bottom, left:right]


def side_by_side(images, labels, height=900):
    import cv2
    import numpy as np

    panels = []
    for image, label in zip(images, labels):
        scale = height / image.shape[0]
        panel = cv2.resize(image, (max(1, round(image.shape[1] * scale)), height),
                           interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        cv2.rectangle(panel, (0, 0), (panel.shape[1], 34), (0, 0, 0), -1)
        cv2.putText(panel, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        panels.append(panel)
    return np.hstack(panels)


def peak_memory_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024)


def device_memory_mb():
    torch = sys.modules.get("torch")
    if torch is None:
        return None
    if torch.cuda.is_available():
        return round(torch.cuda.max_memory_allocated() / (1024 * 1024))
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available() and hasattr(torch, "mps"):
        return round(torch.mps.driver_allocated_memory() / (1024 * 1024))
    return None


def run_component(component, portrait_path, output_dir):
    """Runs in its own process: everything about one component."""
    import cv2
    import django

    django.setup()
    portrait = cv2.imread(str(portrait_path), cv2.IMREAD_COLOR)
    image, description = suitable_input(component, portrait)
    result = {"component": component, "input": description, "input_shape": list(image.shape)}
    result["integrity"] = integrity(component)
    result["integrity_ok"] = all(item["matches"] for item in result["integrity"])
    baseline = peak_memory_mb()

    direct_output = None
    started = time.perf_counter()
    try:
        direct_output, result["direct"] = direct_inference(component, image, portrait)
    except Exception as error:  # every failure is a result, not a crash
        result["direct"] = {"passed": False, "error": f"{type(error).__name__}: {error}"[:400]}
    result["direct"]["seconds"] = round(time.perf_counter() - started, 2)
    result["direct"]["peak_host_memory_mb"] = peak_memory_mb()
    result["direct"]["host_memory_before_mb"] = baseline
    result["direct"]["device_memory_mb"] = device_memory_mb()

    started = time.perf_counter()
    pipeline_output = None
    try:
        pipeline_output, result["pipeline"], _parameters = pipeline_run(component, image)
    except Exception as error:
        result["pipeline"] = {"passed": False, "error": f"{type(error).__name__}: {error}"[:400]}
    result["pipeline"]["seconds"] = round(time.perf_counter() - started, 2)

    if direct_output is not None:
        label = "detected" if component == "yunet" else "aligned face" if component == "sface" else "model output"
        if component == "sface":
            cv2.imwrite(str(output_dir / f"{component}__aligned_face.jpg"), direct_output)
        else:
            cv2.imwrite(str(output_dir / f"{component}__direct.jpg"),
                        side_by_side([image, direct_output], ["input", label]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    if pipeline_output is not None:
        cv2.imwrite(str(output_dir / f"{component}__pipeline.jpg"),
                    side_by_side([image, pipeline_output], ["input", "pipeline output"]), [cv2.IMWRITE_JPEG_QUALITY, 92])
        try:
            from enhancement.face_fidelity import _detect_boxes
            boxes = _detect_boxes(image)
            if boxes:
                cv2.imwrite(
                    str(output_dir / f"{component}__face_crop.jpg"),
                    side_by_side([face_crop(image, boxes[0]), face_crop(pipeline_output, boxes[0])],
                                 ["input face", "pipeline face"], height=700),
                    [cv2.IMWRITE_JPEG_QUALITY, 93],
                )
        except Exception:  # a crop is a convenience; its absence is not a model failure
            pass
    print(json.dumps(result, default=str))


def run_combinations(portrait_path, output_dir):
    """Real models together, and real models made to fail. Runs in one process."""
    import cv2
    import django
    import numpy as np

    django.setup()
    from unittest.mock import patch

    from django.test.utils import override_settings

    from enhancement import hd_restoration
    from enhancement.services import enhance_bgr

    portrait = cv2.imread(str(portrait_path), cv2.IMREAD_COLOR)
    rows = []

    def stage(parameters, name):
        return [item for item in parameters["stage_reports"] if item["stage"] == name][-1]

    def record(name, expectation, passed, facts, image=None, output=None):
        rows.append({"case": name, "expects": expectation, "passed": bool(passed), **facts})
        if image is not None and output is not None:
            cv2.imwrite(str(output_dir / f"combo__{name}.jpg"), side_by_side([image, output], ["input", "output"]),
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"{name:34s} {'PASS' if passed else 'FAIL'}  {json.dumps(facts, default=str)[:260]}")

    def run(image, overrides, level="auto", style="natural", **kwargs):
        started = time.perf_counter()
        with override_settings(**{**OFF, **overrides}):
            output, applied, parameters = enhance_bgr(image, level, style=style, **kwargs)
        return output, parameters, round(time.perf_counter() - started, 2)

    # 1. Natural mode: learned exposure, then denoise decided on measured noise.
    dark, _ = suitable_input("zero_dce", portrait)
    dark = np.clip(dark.astype(np.float32) + np.random.default_rng(3).normal(0, 6, dark.shape), 0, 255).astype(np.uint8)
    output, parameters, seconds = run(dark, {"ZERO_DCE_ENABLED": True, "IDENTITY_GUARD_ENABLED": True})
    denoise = parameters["adaptive_denoise"]
    record(
        "natural_exposure_and_denoise", "Zero-DCE applied; denoise decided by measurement; identity checked",
        parameters["zero_dce"].get("applied") and denoise["acceptance"]["status"] in {"accepted", "strength_reduced", "rejected", "not_attempted"},
        {"zero_dce": parameters["zero_dce"].get("applied"), "denoise": denoise["acceptance"], "denoise_benefit": denoise.get("benefit"),
         "luminance": [parameters["quality_before"]["mean_luminance"], parameters["quality_after"]["mean_luminance"]],
         "identity_status": parameters["identity_status"], "similarity": parameters["identity_guard"].get("similarity"),
         "seconds": seconds}, dark, output,
    )

    # 2. Deblur plus exactly one upscaler, with every other upscaler also switched on.
    blurred, _ = suitable_input("nafnet", portrait)
    blurred = working_image(blurred, 480)
    output, parameters, seconds = run(blurred, {
        "HD_RESTORATION_ENABLED": True, "NAFNET_DEBLUR_ENABLED": True, "PURESCALE_ENABLED": True, "AI_RESTORATION_ENABLED": True,
    })
    upscalers = [name for name, key in (("edsr", "restoration"), ("realesrgan", "hd_restoration"), ("purescale", "purescale"))
                 if (parameters[key]["super_resolution"] if key == "hd_restoration" else parameters[key]).get("applied")]
    scale = output.shape[0] / blurred.shape[0]
    record(
        "deblur_plus_one_upscaler", "NAFNet then Real-ESRGAN only; output exactly 2x",
        parameters["hd_restoration"]["deblur"].get("applied") and upscalers == ["realesrgan"] and scale == 2.0,
        {"deblur": parameters["hd_restoration"]["deblur"].get("applied"), "upscalers_applied": upscalers, "scale": scale,
         "purescale_reason": parameters["purescale"].get("fallback_reason"), "edsr_reason": parameters["restoration"].get("fallback_reason"),
         "devices": stage(parameters, "hd_restoration").get("models"), "guard_b": parameters["quality_guards"]["guard_b"]["reasons"],
         "seconds": seconds}, blurred, output,
    )

    # 3. Guarded GFPGAN with real identity inference, and its refusals.
    degraded, _ = suitable_input("gfpgan", portrait)
    gfpgan_on = {"GFPGAN_ENABLED": True, "IDENTITY_GUARD_ENABLED": True}
    output, parameters, seconds = run(degraded, gfpgan_on)
    verification = parameters["identity_verification"]
    record(
        "gfpgan_with_real_identity", "restored only with a real, verified identity comparison",
        parameters["face_restoration"].get("applied") and verification["verified"]
        and parameters["face_restoration"]["identity_gate"] == "passed",
        {"applied": parameters["face_restoration"].get("applied"), "gate": parameters["face_restoration"].get("identity_gate"),
         "blend": parameters["face_restoration"].get("output_blend"), "identity_status": verification["status"],
         "similarity": parameters["identity_guard"].get("similarity"), "threshold": parameters["identity_guard"].get("threshold"),
         "face_fidelity": {k: parameters["face_fidelity"].get(k) for k in ("status", "faces_checked", "faces_adjusted")},
         "seconds": seconds}, degraded, output,
    )
    _output, parameters, _seconds = run(degraded, gfpgan_on, allow_biometric_processing=False)
    record(
        "gfpgan_without_consent", "refused: nothing to verify against",
        not parameters["face_restoration"].get("applied") and parameters["face_restoration"].get("identity_gate") == "refused"
        and parameters["identity_status"] == "unavailable",
        {"applied": parameters["face_restoration"].get("applied"), "gate": parameters["face_restoration"].get("identity_gate"),
         "identity_status": parameters["identity_status"], "reason": parameters["face_restoration"].get("fallback_reason")},
    )
    _output, parameters, _seconds = run(degraded, gfpgan_on, style="ai_max", allow_biometric_processing=False)
    verification = parameters["identity_verification"]
    record(
        "gfpgan_ai_max_bypass", "restored, reported as unverified, never as verified",
        parameters["face_restoration"].get("applied") and not verification["verified"]
        and verification["face_restoration_unverified"] and bool(verification["notice"]),
        {"applied": parameters["face_restoration"].get("applied"), "gate": parameters["face_restoration"].get("identity_gate"),
         "identity_status": verification["status"], "bypass": verification["requirement_bypass"], "notice": verification["notice"][:90]},
    )

    # 4. Colorization on a grayscale photo, and not on a colour one.
    gray, _ = suitable_input("colorization", portrait)
    output, parameters, seconds = run(gray, {"COLORIZATION_ENABLED": True})
    colour, _parameters_colour, _ = run(working_image(portrait, 1000), {"COLORIZATION_ENABLED": True})
    record(
        "colorization_grayscale_only", "applied to the grayscale photo, skipped for the colour one",
        parameters["colorization"].get("applied") and not _parameters_colour["colorization"].get("applied"),
        {"grayscale_applied": parameters["colorization"].get("applied"), "device": parameters["colorization"].get("device"),
         "colour_photo_applied": _parameters_colour["colorization"].get("applied"),
         "colour_photo_reason": _parameters_colour["colorization"].get("fallback_reason"), "seconds": seconds}, gray, output,
    )

    # 5. Failure handling, with the real models.
    small, _ = suitable_input("realesrgan", portrait)
    small = working_image(small, 360)
    output, parameters, _seconds = run(small, {"HD_RESTORATION_ENABLED": True, "REALESRGAN_MODEL_PATH": "/nonexistent/RealESRGAN_x4plus.pth"})
    record(
        "checkpoint_missing", "falls back; the image keeps its size and the reason is recorded",
        not parameters["hd_restoration"].get("applied") and output.shape == small.shape,
        {"status": stage(parameters, "hd_restoration")["status"], "reason": parameters["hd_restoration"].get("fallback_reason"),
         "output_shape": list(output.shape)},
    )
    with override_settings(REALESRGAN_MODEL_SHA256="0" * 64):
        hd_restoration._MODEL_CACHE.clear()
        output, parameters, _seconds = run(small, {"HD_RESTORATION_ENABLED": True, "REALESRGAN_MODEL_SHA256": "0" * 64})
    record(
        "checkpoint_fails_integrity", "a file that does not match its pinned checksum is never loaded",
        not parameters["hd_restoration"].get("applied") and "integrity" in parameters["hd_restoration"].get("fallback_reason", ""),
        {"reason": parameters["hd_restoration"].get("fallback_reason")},
    )
    hd_restoration._MODEL_CACHE.clear()
    real_tiled = hd_restoration._tiled_inference
    calls = []

    def out_of_memory_once(image, model, device, scale, tile, overlap, output_scale=None):
        calls.append(tile)
        if len(calls) == 1:
            raise RuntimeError("MPS backend out of memory (injected for this test)")
        return real_tiled(image, model, device, scale, tile, overlap, output_scale)

    with patch.object(hd_restoration, "_tiled_inference", side_effect=out_of_memory_once):
        output, parameters, seconds = run(small, {"HD_RESTORATION_ENABLED": True})
    sr = parameters["hd_restoration"]["super_resolution"]
    record(
        "out_of_memory_then_smaller_tiles", "injected out-of-memory on the first try; the real model then succeeds at half the tile",
        sr.get("applied") and sr.get("memory_retries") == 1 and calls == [256, 128] and output.shape[0] == small.shape[0] * 2,
        {"tiles_tried": calls, "memory_retries": sr.get("memory_retries"), "tile_size": sr.get("tile_size"), "device": sr.get("device"),
         "applied": sr.get("applied"), "seconds": seconds},
    )
    with patch.object(hd_restoration, "_tiled_inference", side_effect=RuntimeError("MPS backend out of memory (injected, every time)")):
        output, parameters, _seconds = run(small, {"HD_RESTORATION_ENABLED": True})
    record(
        "out_of_memory_every_time", "bounded retries, then the last safe image at its original size",
        not parameters["hd_restoration"].get("applied") and output.shape == small.shape
        and "out of memory" in parameters["hd_restoration"].get("fallback_reason", ""),
        {"reason": parameters["hd_restoration"].get("fallback_reason"), "output_shape": list(output.shape)},
    )
    with patch.object(hd_restoration, "upscale_with_realesrgan", side_effect=ValueError("unexpected tensor shape (injected)")), \
            patch("enhancement.services.upscale_with_realesrgan", side_effect=ValueError("unexpected tensor shape (injected)")):
        output, parameters, _seconds = run(small, {"HD_RESTORATION_ENABLED": True})
    record(
        "inference_error", "an unexpected error in a model stage keeps the previous image and is reported as failed",
        stage(parameters, "hd_restoration")["status"] == "failed" and output.shape == small.shape,
        {"status": stage(parameters, "hd_restoration")["status"], "warning": [w for w in parameters["warnings"] if "failed safely" in w][:1]},
    )
    seen = []

    def cancel_before_upscale(stage_name):
        seen.append(stage_name)
        if stage_name == "hd_restoration":
            from enhancement.services import EnhancementCancelled
            raise EnhancementCancelled()

    cancelled = False
    try:
        with override_settings(**{**OFF, "HD_RESTORATION_ENABLED": True}):
            enhance_bgr(small, "auto", checkpoint=cancel_before_upscale)
    except Exception as error:
        cancelled = type(error).__name__ == "EnhancementCancelled"
    record("cancel_before_a_model_stage", "cancellation stops the pipeline before the model runs",
           cancelled, {"stopped_at": seen[-1] if seen else None})

    (output_dir / "combinations.json").write_text(json.dumps(rows, indent=2, default=str))
    lines = ["", "## Combinations and failure handling", "",
             "Real checkpoints throughout. Where a failure is marked *injected*, the error was raised deliberately "
             "around the real model; everything else is the model's own behaviour.", "",
             "| Case | Expected | Result |", "|---|---|---|"]
    lines += [f"| {row['case']} | {row['expects']} | {'**PASS**' if row['passed'] else '**FAIL**'} |" for row in rows]
    with open(output_dir / "report.md", "a") as handle:
        handle.write("\n".join(lines) + "\n")
    return 0 if all(row["passed"] for row in rows) else 1


def verdict(result):
    if not result["integrity_ok"]:
        missing = [item["file"] for item in result["integrity"] if not item["exists"]]
        return "BLOCKED" if missing else "CHECKSUM MISMATCH"
    if result["direct"].get("passed") and result["pipeline"].get("passed"):
        return "PASS"
    if result["direct"].get("passed"):
        return "RUNS, NOT USED BY PIPELINE"
    return "FAIL"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portrait", required=True)
    parser.add_argument("--components", default=",".join(COMPONENTS))
    parser.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--output", default=str(ROOT / ".runtime" / "model_validation"))
    parser.add_argument("--one", help=argparse.SUPPRESS)
    parser.add_argument("--combinations", action="store_true",
                        help="Also run the models together and the failure-handling cases.")
    parser.add_argument("--only-combinations", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    output_dir = Path(arguments.output) / arguments.name
    output_dir.mkdir(parents=True, exist_ok=True)
    if arguments.one:
        run_component(arguments.one, Path(arguments.portrait).expanduser(), output_dir)
        return 0
    if arguments.only_combinations:
        return run_combinations(Path(arguments.portrait).expanduser(), output_dir)

    names = [name.strip() for name in arguments.components.split(",") if name.strip()]
    unknown = [name for name in names if name not in COMPONENTS]
    if unknown:
        raise SystemExit(f"Unknown component(s): {', '.join(unknown)}")
    results = []
    for name in names:
        completed = subprocess.run(
            [sys.executable, __file__, "--portrait", arguments.portrait, "--name", arguments.name,
             "--output", arguments.output, "--one", name],
            capture_output=True, text=True, cwd=ROOT,
        )
        line = next((text for text in reversed(completed.stdout.splitlines()) if text.startswith("{")), None)
        if line is None:
            result = {"component": name, "integrity": [], "integrity_ok": False, "input": "",
                      "direct": {"passed": False, "error": (completed.stderr or completed.stdout)[-400:]},
                      "pipeline": {"passed": False, "error": "the component process did not finish"}}
        else:
            result = json.loads(line)
        result["verdict"] = verdict(result)
        results.append(result)
        direct, pipeline = result["direct"], result["pipeline"]
        print(f"{name:13s} {result['verdict']:28s} direct: {'ok' if direct.get('passed') else 'no':2s} "
              f"{direct.get('seconds', '-')}s on {direct.get('device', '-')} | pipeline: "
              f"{pipeline.get('status', pipeline.get('error', '-'))} on {pipeline.get('device', '-')} "
              f"{pipeline.get('fallback_reason') or direct.get('error') or ''}"[:230])

    (output_dir / "results.json").write_text(json.dumps(results, indent=2, default=str))
    lines = [
        f"# Model validation `{arguments.name}`", "",
        f"Portrait: `{Path(arguments.portrait).name}`. Every row is a real checkpoint run in its own process; "
        "nothing is mocked. PASS means it loaded, ran and returned a well-formed image through the pipeline, "
        "not that the image looks good.", "",
        "| Component | Verdict | Checksum | Direct inference | Device | Time (s) | Peak host MB | Pipeline stage | Pipeline status | Fallback / blocker |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        direct, pipeline = result["direct"], result["pipeline"]
        checksum = "match" if result["integrity_ok"] else "; ".join(
            f"{item['file']}: {'missing' if not item['exists'] else 'differs from ' + item['pinned_in']}"
            for item in result["integrity"] if not item["matches"]
        ) or "not checked"
        lines.append(
            f"| {result['component']} | **{result['verdict']}** | {checksum} | "
            f"{'ok' if direct.get('passed') else 'failed'} | {direct.get('device', pipeline.get('device', '-'))} | "
            f"{direct.get('seconds', '-')} | {direct.get('peak_host_memory_mb', '-')} | {pipeline.get('stage', '-')} | "
            f"{pipeline.get('status', '-')} | {pipeline.get('fallback_reason') or direct.get('error') or pipeline.get('error') or ''} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n")
    combinations_failed = 0
    if arguments.combinations:
        print("\n-- combinations and failure handling --")
        combinations_failed = subprocess.run(
            [sys.executable, __file__, "--portrait", arguments.portrait, "--name", arguments.name,
             "--output", arguments.output, "--only-combinations"], cwd=ROOT,
        ).returncode
    print(f"\nWrote {output_dir}")
    return 0 if all(result["verdict"] == "PASS" for result in results) and not combinations_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
