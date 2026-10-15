"""Compare local-only enhancement settings with advisor-assisted ones on real photographs.

    # no network, no key: the local conservative recipe only (always safe to run)
    python scripts/evaluate_advisor.py --images DIR --out OUT --mode local

    # PAID: sends a downscaled preview of each image to OpenAI
    OPENAI_ADVISOR_ENABLED=true OPENAI_API_KEY=... OPENAI_ADVISOR_MODEL=... \\
    python scripts/evaluate_advisor.py --images DIR --out OUT --mode advisor \\
        --confirm-images-authorised --max-requests 20 --max-total-tokens 60000

The advisor mode refuses to run unless ALL of these hold: the advisor is enabled
and a key and model are configured; the operator confirmed with
``--confirm-images-authorised`` that every image in DIR may be sent to an
external service; ``--max-requests`` (a hard cap on paid requests) is given. It
makes one attempt per image (no retries), stops when either cap is reached, and
records tokens so the cost can be worked out from the provider's current price.

Images are never written to the output unless ``--save-images`` is given (they
may be personal). The database is not touched.
"""
import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import cv2  # noqa: E402
import django  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from enhancement import advisor_client, advisor_service as service  # noqa: E402
from enhancement.advisor_schema import (  # noqa: E402
    InvalidAdvice, build_user_text, diagnostics_from_quality, validate_advice,
)
from enhancement.controls import CONTROL_NAMES, recommend_local_adjustments  # noqa: E402
from enhancement.services import (  # noqa: E402
    analyze_image_quality, apply_gallery_adjustments, compare_image_quality, measure_compression_artifacts,
)
from uploads.decoding import to_working_colour  # noqa: E402

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def load(path):
    """The pipeline's own colour-managed decoding: ICC to sRGB, orientation, 8 bit."""
    with Image.open(path) as handle:
        working, _notes = to_working_colour(handle)
    array = np.asarray(working)
    if array.shape[2] == 4:
        return cv2.cvtColor(array[:, :, :3], cv2.COLOR_RGB2BGR), array[:, :, 3].copy()
    return cv2.cvtColor(array, cv2.COLOR_RGB2BGR), None


def metrics(source, rendered, metadata):
    comparison = compare_image_quality(source, rendered)
    ssim_like = float(np.mean(cv2.absdiff(source, rendered)))
    after = comparison["after"]
    return {
        "outcome": metadata["outcome"],
        "applied": metadata["applied"],
        "safety_strength": metadata["safety_strength"],
        "guard_reductions": len(metadata["guard_reductions"]),
        "mean_luminance": [comparison["before"]["mean_luminance"], after["mean_luminance"]],
        "contrast_stddev": [comparison["before"]["contrast_stddev"], after["contrast_stddev"]],
        "highlight_clipping_percent": [comparison["before"]["highlight_clipping_percent"], after["highlight_clipping_percent"]],
        "estimated_noise": [comparison["before"]["estimated_noise"], after["estimated_noise"]],
        "mean_abs_pixel_change": round(ssim_like, 3),
    }


def render(bgr, adjustments):
    started = time.perf_counter()
    rendered, metadata = apply_gallery_adjustments(bgr, "manual", dict(adjustments))
    return rendered, metadata, round((time.perf_counter() - started) * 1000, 1)


def peak_memory_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def check_advisor_allowed(args):
    problems = []
    state = advisor_client.availability()
    if not state["available"]:
        problems.append(f"the advisor is not usable here ({state['reason']}); set OPENAI_ADVISOR_ENABLED, OPENAI_API_KEY and OPENAI_ADVISOR_MODEL")
    if not args.confirm_images_authorised:
        problems.append("pass --confirm-images-authorised to state that every image may be sent to OpenAI")
    if not args.max_requests or args.max_requests < 1:
        problems.append("pass --max-requests N: a hard cap on paid requests")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mode", choices=["local", "advisor"], default="local")
    parser.add_argument("--intent", choices=["auto", "preserve", "brighten"], default="auto")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--confirm-images-authorised", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--max-total-tokens", type=int, default=0)
    parser.add_argument("--save-images", action="store_true")
    args = parser.parse_args()

    if args.mode == "advisor":
        problems = check_advisor_allowed(args)
        if problems:
            print("Refusing to call the advisor:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
    paths = sorted(p for p in args.images.iterdir() if p.suffix.lower() in EXTENSIONS)[: args.limit]
    if not paths:
        print(f"No images found in {args.images}", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    client = advisor_client.build_client() if args.mode == "advisor" else None
    rows, requests_made, tokens_used = [], 0, 0
    for path in paths:
        row = {"image": path.name, "intent": args.intent}
        try:
            bgr_full, alpha = load(path)
        except Exception as error:  # an unreadable file is reported, not fatal
            row["error"] = f"{type(error).__name__}"
            rows.append(row)
            continue
        bgr, jpeg, had_transparency = service.prepare_advisor_image(bgr_full, alpha)
        quality = analyze_image_quality(bgr)
        row["size"] = [bgr_full.shape[1], bgr_full.shape[0]]
        row["measured"] = {key: quality[key] for key in ("mean_luminance", "contrast_stddev", "estimated_noise", "defects")}

        local = recommend_local_adjustments(quality, args.intent, blockiness_detected=measure_compression_artifacts(bgr)["detected"])
        rendered, metadata, local_ms = render(bgr, local["controls"])
        row["local_only"] = {"action": local["action"], "adjustments": local["controls"], "rationale": local["rationale"],
                             "caution_flags": local["caution_flags"], "render_ms": local_ms, **metrics(bgr, rendered, metadata)}
        if args.save_images:
            cv2.imwrite(str(args.out / f"{path.stem}__local.jpg"), rendered)

        if args.mode == "advisor":
            if requests_made >= args.max_requests or (args.max_total_tokens and tokens_used >= args.max_total_tokens):
                row["advisor_assisted"] = {"state": "not_run_cap_reached"}
                rows.append(row)
                continue
            import base64

            started = time.perf_counter()
            requests_made += 1
            try:
                reply = advisor_client.request_advice(
                    client, image_data_url="data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    user_text=build_user_text(args.intent, diagnostics_from_quality(quality), local, had_transparency=had_transparency),
                )
                tokens_used += reply.input_tokens + reply.output_tokens
                advice = validate_advice(reply.payload, args.intent)
            except (advisor_client.TransientProviderError, advisor_client.ProviderError, advisor_client.ReplyRefused,
                    advisor_client.ReplyUnusable, InvalidAdvice) as error:
                tokens_used += sum(getattr(error, "tokens", (0, 0)))
                row["advisor_assisted"] = {"state": "fell_back", "reason": getattr(error, "reason", type(error).__name__)}
                rows.append(row)
                continue
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            final, info, rejected = (advice["adjustments"], {}, "")
            if advice["action"] == "apply":
                final, info, rejected = service.guard_suggestion(bgr, quality, advice["adjustments"], args.intent)
            entry = {"state": "guard_rejected" if rejected else "ok", "action": advice["action"], "suggested": advice["adjustments"],
                     "rationale": advice["rationale"], "caution_flags": advice["caution_flags"], "notes": advice["notes"],
                     "guard": info, "tokens": [reply.input_tokens, reply.output_tokens], "latency_ms": latency_ms}
            if final is not None and any(final.values()):
                rendered_a, metadata_a, ms = render(bgr, {name: final[name] for name in CONTROL_NAMES})
                entry.update({"adjustments": final, "render_ms": ms, **metrics(bgr, rendered_a, metadata_a)})
                if args.save_images:
                    cv2.imwrite(str(args.out / f"{path.stem}__advisor.jpg"), rendered_a)
            row["advisor_assisted"] = entry
        rows.append(row)

    report = {
        "mode": args.mode, "intent": args.intent, "images": len(rows), "model": settings.OPENAI_ADVISOR_MODEL or None,
        "paid_requests_made": requests_made, "tokens_used": tokens_used, "peak_memory_mb": peak_memory_mb(),
        "renderer": "local_tone_renderer_v2", "rows": rows,
    }
    (args.out / "results.json").write_text(json.dumps(report, indent=2, default=str))
    changed_local = sum(1 for r in rows if r.get("local_only", {}).get("outcome") == "rendered")
    lines = [f"# Advisor evaluation ({args.mode})", "", f"Images: {len(rows)}; local-only changed: {changed_local}; "
             f"paid requests: {requests_made}; tokens: {tokens_used}; peak memory {report['peak_memory_mb']} MB", ""]
    if args.mode == "local":
        lines.append("Advisor-assisted: **pending** (no key, no authorised images, no cost cap were provided).")
    open(args.out / "summary.md", "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
