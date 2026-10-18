"""What was measured, what was planned and done, and one honest sentence about it (the ``diagnosis``, ``plan`` and summary of a result)."""
from . import operations as ops

STAGE = {
    ops.LOW_LIGHT: "low_light", ops.DENOISE: "denoise", ops.DEBLUR: "deblur", ops.FACE: "face_restore", ops.TONE: "tone", ops.UPSCALE: "upscale",
}
DOING = {
    "low_light": "brightened the dark subject", "denoise": "reduced noise", "deblur": "reduced blur", "face_restore": "restored a degraded face",
    "tone": "gently improved contrast and detail",
}
NEEDED = {   # noun phrase for "<x> was needed but is not available on this server"
    "low_light": "Brightening", "denoise": "Denoising", "deblur": "Deblurring", "face_restore": "Face restoration", "tone": "The tone and detail pass",
    "upscale": "HD enlargement",
}


def build_diagnosis(report):
    """The analyzer's measurements of the uploaded photograph, with its status words, for display."""
    data = report.as_dict()
    main = report.main_face
    return {
        "size": [report.width, report.height], "long_edge": max(report.width, report.height),
        "exposure": data["exposure"], "noise": data["noise"], "blur": data["blur"], "face_blur": data["face_blur"], "resolution": data["resolution"],
        "contrast": report.base.get("contrast_stddev"), "faces": report.face_count,
        "main_face": None if main is None else {"short_edge": main.short_edge, "detail": main.detail, "quality": main.quality,
                                                  "luminance": main.luminance},
        "conditions": data["conditions"], "recommended_operations": [STAGE.get(op, op) for op in data["recommended_operations"]],
    }


def build_plan(stages, skipped, original_shape, final_shape, hd_target):
    """The operations the engine ran or tried (engine, strength, reason, outcome), the ones it skipped (reason), and the HD result."""
    operations = []
    for record in stages:
        operations.append({
            "stage": STAGE.get(record["operation"], record["operation"]), "status": record["status"],
            "engine": next(iter(record.get("models_run") or []), None) or (record.get("engines") or [None])[0],
            "strength": record.get("strength"), "planned_strength": record.get("planned_strength"), "variant": record.get("variant"),
            "reason": record.get("reason", ""), "measure": record.get("measure") or {},
            "skipped_engines": record.get("skipped_engines", []), "rejection_reasons": record.get("rejection_reasons", []),
        })
    long_in, long_out = max(original_shape[:2]), max(final_shape[:2])
    hd = {"target_long_edge": hd_target or None, "source_long_edge": long_in, "output_long_edge": long_out,
          "output_size": [final_shape[1], final_shape[0]], "scale": round(long_out / long_in, 2) if long_in else 1.0,
          "reached": bool(hd_target) and long_out >= hd_target}
    return {
        "operations": operations,
        "skipped": [{"stage": STAGE.get(item["operation"], item["operation"]), "reason": item["reason"]} for item in skipped],
        "hd": hd,
    }


def _why_not(stage, plan):
    """The sentence for a stage that was needed and did not happen: why, in the words a user can read."""
    noun = NEEDED[stage]
    record = next((item for item in plan["operations"] if item["stage"] == stage), None)
    reason = next((item["reason"] for item in plan["skipped"] if item["stage"] == stage), "")
    if record is not None:
        if record["status"] in ("unavailable", "no_engine"):
            return f"{noun} was needed but is not available on this server."
        if record["status"] == "rejected":
            return f"{noun} was tried but its result did not pass the safety checks, so it was not used."
    if "time budget" in reason:
        return f"{noun} did not finish within the time budget."
    if "switched on" in reason:
        return f"{noun} was needed but is not available on this server."
    if "defocus" in reason:
        return "Defocus blur cannot be corrected reliably, so it was left."
    if "too small" in reason:
        return "The face is too small to restore without inventing it."
    if "Identity" in reason or "Biometric" in reason:
        return "Face restoration was skipped because the face could not be checked against the original."
    return f"{noun} was needed but was not done" + (f" ({reason.rstrip('.')})." if reason else ".")


def compose_summary(outcome, plan, report0, accepted_stages):
    """One honest plain sentence (never empty): what was done, and what was needed but could not be."""
    done = []
    for stage in accepted_stages:
        if stage == "upscale":
            upscale = next(item for item in plan["operations"] if item["stage"] == "upscale" and item["status"] == "accepted")
            width, height = plan["hd"]["output_size"]
            if upscale["engine"] == "lanczos":
                done.append(f"enlarged it to {width}x{height} with a conventional filter (no new detail was added)")
            else:
                done.append(f"enlarged it to {width}x{height}" + (" (HD)" if plan["hd"]["reached"] else ""))
        else:
            done.append(DOING[stage])
    needed = [STAGE.get(op, op) for op in [o for o in report0.recommended_operations] if STAGE.get(op, op) not in accepted_stages]
    if plan["hd"]["target_long_edge"] and not plan["hd"]["reached"] and plan["hd"]["source_long_edge"] < plan["hd"]["target_long_edge"] \
            and "upscale" not in accepted_stages and "upscale" not in needed:
        needed.append("upscale")
    problems = [_why_not(stage, plan) for stage in dict.fromkeys(needed)]
    if plan["hd"]["target_long_edge"] and "upscale" in accepted_stages and not plan["hd"]["reached"]:
        problems.append(f"HD ({plan['hd']['target_long_edge']} px on the long edge) was not reached with one enlargement.")
    if done:
        text = "Enhanced: " + ", ".join(done) + "."
    elif problems:
        text = "The original photograph was kept."
    else:
        text = "The photograph did not need enhancement, so it was left as it is."
    return " ".join([text, *problems]).strip()
