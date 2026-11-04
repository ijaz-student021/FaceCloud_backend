"""The restoration chain: deblur, face restoration, upscale to HD. What it needs, whether it can run here, and what a result records.

The chain is the adaptive engine's DEBLUR -> FACE -> UPSCALE operations (``enhancement/adaptive/``), each with its own model and a
fallback, each gated by the validator. This module answers three questions without running a model:

* ``stage_status()``: for every stage, is it switched on, is its checkpoint on disk, is PyTorch importable (the runtime check, also
  printed by ``manage.py check_restoration``);
* ``restoration_check(image)``: does *this* photograph need restoration, and could this server do it (used to send such a photograph
  through the chain when the rendering policy would otherwise leave it to the lightweight pipeline);
* ``describe_stages(parameters)``: what a stored result did, as the ``applied_stages`` / ``skipped_stages`` / ``hd_unavailable_reason``
  the API returns, so a barely changed photograph is never presented as restored.

Licences (docs/model-licenses/): NAFNet is MIT and Real-ESRGAN BSD-3-Clause, so they are switched on by default. CodeFormer (S-Lab
non-commercial) and GFPGAN are off by default and the production guard refuses them unless NONCOMMERCIAL_MODELS_ACKNOWLEDGED.
"""
import importlib.util
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# stage name in the API -> the adaptive engine's operation name
OPERATION_STAGE = {
    "low_light_enhancement": "low_light", "denoising": "denoise", "motion_deblurring": "deblur",
    "face_restoration": "face_restore", "super_resolution": "upscale", "polish": "polish",
    "detail_restoration": "detail", "colourization": "colourize",       # the detail model (also the final layer of every photograph) and DDColor; a missing name here sent `stage: null`
}
POLISH_REFUSED_REASON = "Already balanced and sharp: nothing to improve."
RESTORATION_OPERATIONS = ("motion_deblurring", "face_restoration", "super_resolution")

# stage -> engines in preference order: (engine, enabled setting, checkpoint path setting, checksum setting, licence)
STAGE_ENGINES = {
    "deblur": [("nafnet_gopro", "NAFNET_DEBLUR_ENABLED", "NAFNET_MODEL_PATH", "NAFNET_MODEL_SHA256", "MIT")],
    "face_restore": [
        ("codeformer", "CODEFORMER_ENABLED", "CODEFORMER_MODEL_PATH", "CODEFORMER_MODEL_SHA256", "S-Lab 1.0 (non-commercial)"),
        ("gfpgan", "GFPGAN_ENABLED", "GFPGAN_MODEL_PATH", "", "non-commercial"),
    ],
    "upscale": [("realesrgan", "HD_RESTORATION_ENABLED", "REALESRGAN_MODEL_PATH", "REALESRGAN_MODEL_SHA256", "BSD-3-Clause")],
}


def torch_importable():
    return importlib.util.find_spec("torch") is not None


def engine_status(engine, enabled_setting, path_setting, checksum_setting, licence):
    enabled = bool(getattr(settings, enabled_setting, False))
    path = Path(str(getattr(settings, path_setting, "") or "")).expanduser()
    present = path.is_file()
    problems = []
    if not enabled:
        problems.append(f"switched off ({enabled_setting}=False)")
    if not present:
        problems.append(f"checkpoint not found at {path}")
    if not torch_importable():
        problems.append("PyTorch is not installed in this environment")
    return {
        "engine": engine, "enabled_setting": enabled_setting, "enabled": enabled, "checkpoint": str(path),
        "checkpoint_present": present, "checkpoint_bytes": path.stat().st_size if present else None,
        "checksum_setting": checksum_setting or None, "licence": licence, "torch": torch_importable(),
        "available": enabled and present and torch_importable(), "problems": problems,
    }


def stage_status():
    """``{stage: {"available": bool, "engines": [...], "reason": str}}``: a stage is available when any of its engines can run."""
    status = {}
    for stage, engines in STAGE_ENGINES.items():
        listed = [engine_status(*entry) for entry in engines]
        usable = [item for item in listed if item["available"]]
        status[stage] = {
            "available": bool(usable), "engine": usable[0]["engine"] if usable else None, "engines": listed,
            "reason": "" if usable else "; ".join(f"{item['engine']}: {', '.join(item['problems'])}" for item in listed),
        }
    return status


def restoration_check(image, analyzer=None):
    """Whether ``image`` needs restoration and whether this server could do it. Never raises: an analysis failure means "no".

    ``needed`` lists the restoration stages the photograph's own measurements call for (``face_blur`` and a low resolution above all);
    ``available`` is true when at least one needed stage has an engine that can run (the chain does what it can); a stage with none is
    listed in ``unavailable`` with its reason, and the result reports it as skipped.
    """
    check = {"needed": [], "available": False, "unavailable": {}, "analysed": False}
    try:
        if analyzer is None:
            from enhancement.adaptive.service import AdaptiveImageEnhancementService

            analyzer = AdaptiveImageEnhancementService()._analyzer()
        report = analyzer.analyze(image)
    except Exception:  # an analysis failure is "nothing known", never a reason to fail the enhancement
        logger.exception("The restoration check could not analyse the photograph.")
        return check
    check["analysed"] = True
    check["conditions"] = list(report.conditions)
    check["needed"] = [OPERATION_STAGE[op] for op in report.recommended_operations if op in RESTORATION_OPERATIONS]
    status = stage_status()
    check["unavailable"] = {stage: status[stage]["reason"] for stage in check["needed"] if not status[stage]["available"]}
    check["available"] = any(stage not in check["unavailable"] for stage in check["needed"])
    return check


def describe_stages(parameters, failed=False):
    """What a stored result did, for the API.

    ``applied_stages``: the operations that changed the pixels, each with its engine (and for ``upscale`` the scale and the output size).
    ``skipped_stages``: restoration stages the photograph called for that did not run, each with the reason.
    ``hd_unavailable_reason``: a plain sentence when restoration was needed and could not be done, else ``None``.
    """
    empty = {"applied_stages": [], "skipped_stages": [], "hd_unavailable_reason": None}
    if failed or not isinstance(parameters, dict):
        return empty
    adaptive = parameters.get("adaptive")
    if isinstance(adaptive, dict):
        return _from_adaptive(parameters, adaptive)
    return _from_staged(parameters, empty)


def _stage_name(operation):
    """The API's name for an engine operation. Never ``None``: an operation this table does not know yet is reported under its own name, so a client that reads ``stage`` as text does not break."""
    return OPERATION_STAGE.get(operation) or str(operation or "unknown")


def _from_adaptive(parameters, adaptive):
    contract = parameters.get("resolution_contract") or {}
    source, output = contract.get("source_dimensions"), contract.get("actual_output_dimensions")
    applied, skipped = [], []
    needed = {OPERATION_STAGE.get(op) for op in (adaptive.get("quality_report") or {}).get("recommended_operations", [])
              if op in RESTORATION_OPERATIONS}
    for stage in adaptive.get("stages", []):
        name = _stage_name(stage.get("operation"))
        if stage.get("status") == "accepted":
            item = {"stage": name, "engine": (stage.get("models_run") or [None])[0], "variant": stage.get("variant")}
            if name == "upscale":
                variant = str(stage.get("variant") or "")
                item["scale"] = int(variant[1:]) if variant.startswith("x") and variant[1:].isdigit() else None
                item["input_size"], item["output_size"] = source, output
            applied.append(item)
        elif name in needed or name in ("deblur", "face_restore", "upscale"):
            reasons = [f"{entry['engine']}: {entry['reason']}" for entry in stage.get("skipped_engines", [])]
            reasons += [str(note) for note in stage.get("notes", [])]
            if stage.get("status") == "rejected":
                reasons.append("the result did not pass the safety checks: " + ", ".join(stage.get("rejection_reasons", [])))
            skipped.append({"stage": name, "reason": "; ".join(reasons) or f"status {stage.get('status')}"})
    for item in adaptive.get("operations_skipped", []):
        name = _stage_name(item.get("operation"))
        if name in ("deblur", "face_restore", "upscale") and name in needed and not any(s["stage"] == name for s in skipped):
            skipped.append({"stage": name, "reason": item.get("reason", "")})
    skipped = [s for s in skipped if s["stage"] in needed or s["stage"] in {"deblur", "face_restore", "upscale"} and s["reason"]]
    unmet = [s for s in skipped if s["stage"] in needed]
    reason = None
    if unmet:
        reason = ("Restoration was needed but not completed: " + "; ".join(f"{s['stage']} ({s['reason']})" for s in unmet))
    listed = list(unmet)
    if any(stage.get("operation") == "polish" and stage.get("status") == "rejected" for stage in adaptive.get("stages", [])):
        # A polish that was tried and refused is reported, but it is not a restoration that was needed: it never feeds the reason above.
        listed.append({"stage": "polish", "reason": POLISH_REFUSED_REASON})
    return {"applied_stages": applied, "skipped_stages": listed, "hd_unavailable_reason": reason}


def _from_staged(parameters, empty):
    """Results of the staged (non-adaptive) pipeline: the legacy HD stage, and the up-front check recorded by ``enhance_bgr``."""
    result = {"applied_stages": [], "skipped_stages": [], "hd_unavailable_reason": None}
    hd = parameters.get("hd_restoration") or {}
    if (hd.get("deblur") or {}).get("applied"):
        result["applied_stages"].append({"stage": "deblur", "engine": "nafnet_gopro", "variant": None})
    if hd.get("applied"):
        contract = parameters.get("resolution_contract") or {}
        result["applied_stages"].append({
            "stage": "upscale", "engine": "realesrgan", "variant": None, "scale": settings.HD_RESTORATION_OUTPUT_SCALE,
            "input_size": contract.get("source_dimensions"), "output_size": contract.get("actual_output_dimensions"),
        })
    if (parameters.get("face_restoration") or {}).get("applied"):
        result["applied_stages"].append({"stage": "face_restore", "engine": "codeformer_or_gfpgan", "variant": None})
    check = parameters.get("restoration_check") or {}
    needed = [stage for stage in check.get("needed", []) if stage not in {s["stage"] for s in result["applied_stages"]}]
    if needed:
        unavailable = check.get("unavailable") or {}
        result["skipped_stages"] = [{"stage": stage, "reason": unavailable.get(stage, "not run")} for stage in needed]
        result["hd_unavailable_reason"] = "Restoration was needed but not available on this server: " + "; ".join(
            f"{stage} ({unavailable.get(stage, 'not run')})" for stage in needed
        )
    return result
