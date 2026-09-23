"""Side-by-side comparison of enhancement profiles on one image (Feature 35).

Purpose: give the project (and a reviewer) evidence of what each approach
actually does to a real photograph -- measured quality, timing, whether faces
survive -- instead of asserting it. All profiles run on the same downscaled copy
of the source so the numbers are comparable and the job stays bounded on CPU.

The recommendation is an explicit, explainable rule (see ``recommend``), not a
model's opinion, and it says so when no profile improved the image.
"""
import csv
import io
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace

import cv2
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    EnhancementStatus,
    EnhancementStyle,
    ModelComparison,
    ModelComparisonOutput,
)

logger = logging.getLogger(__name__)

ORIGINAL = "original"
# A profile must beat the untouched source by at least this many score points to
# be recommended; otherwise the honest answer is "keep the original".
MIN_RECOMMENDED_GAIN = 1.0
# Profiles within this many points of the top score count as tied, and the more
# conservative one wins: a riskier profile has to be meaningfully better.
TIE_MARGIN = 1.0
# Penalties applied to a profile's quality score before ranking.
UNRESOLVED_IDENTITY_PENALTY = 20.0
REDUCED_IDENTITY_PENALTY = 2.0
LOST_FACES_PENALTY = 15.0
CLIPPING_TOLERANCE_POINTS = 5.0
MAX_CLIPPING_PENALTY = 15.0
PREVIEW_JPEG_QUALITY = 88


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    description: str
    style: str
    use_zero_dce: bool = True


PROFILES = {
    profile.key: profile
    for profile in (
        Profile(
            "opencv_baseline", "OpenCV adaptive baseline",
            "Classical exposure, denoise and sharpen only; the learned Zero-DCE model is switched off.",
            EnhancementStyle.NATURAL, use_zero_dce=False,
        ),
        Profile(
            "natural", "Natural (default pipeline)",
            "The pipeline as users get it by default; uses Zero-DCE when the photo is low-light.",
            EnhancementStyle.NATURAL,
        ),
        Profile(
            "dynamic", "Dynamic",
            "The default pipeline plus a bounded, guarded vivid contrast and colour boost.",
            EnhancementStyle.DYNAMIC,
        ),
        Profile(
            "ai_max", "AI Enhance",
            "Highest strength with adaptive denoise; licence-gated restoration models still follow platform flags.",
            EnhancementStyle.AI_MAX,
        ),
    )
}
DEFAULT_PROFILES = tuple(PROFILES)
# When two profiles tie, prefer the more conservative one.
CONSERVATIVE_ORDER = ("natural", "opencv_baseline", "dynamic", "ai_max")


def ensure_comparison_capacity(owner):
    """Comparisons run several pipelines each, so a user may only have a few at once."""
    from .services import ACTIVE_STATUSES, TooManyActiveJobs

    limit = settings.MAX_ACTIVE_COMPARISONS_PER_USER
    active = ModelComparison.objects.filter(owner=owner, status__in=ACTIVE_STATUSES).count()
    if limit and active >= limit:
        raise TooManyActiveJobs(
            f"You already have {active} model comparisons in progress (limit {limit}). "
            "Wait for one to finish, then try again."
        )


def validate_profiles(requested):
    if not requested:
        return list(DEFAULT_PROFILES)
    unknown = [key for key in requested if key not in PROFILES]
    if unknown:
        raise ValidationError({"profiles": [f"Unknown profile: {', '.join(unknown)}."]})
    return list(dict.fromkeys(requested))


def _clipping(metrics):
    return metrics["shadow_clipping_percent"] + metrics["highlight_clipping_percent"]


def recommend(source, candidates):
    """Pick the best profile with an explainable rule.

    ``source`` and each candidate carry ``overall_score``, ``metrics``,
    ``faces_detected`` and (candidates) ``identity_guard``. A candidate's
    *adjusted* score is its quality score minus penalties for: the identity guard
    failing or having to back off, faces that were detectable in the source
    disappearing, and a jump in clipped shadows/highlights. Returns
    ``(profile_or_original, reason, details)`` where ``details`` maps each
    profile to its adjusted score and penalty notes.
    """
    details = {}
    for candidate in candidates:
        penalty, notes = 0.0, []
        guard = (candidate.get("identity_guard") or {}).get("status")
        if guard == "unresolved":
            penalty += UNRESOLVED_IDENTITY_PENALTY
            notes.append("the identity guard could not keep the face recognisable")
        elif guard == "strength_reduced":
            penalty += REDUCED_IDENTITY_PENALTY
            notes.append("the identity guard had to reduce the strength")
        if source.get("faces_detected") and candidate.get("faces_detected") == 0:
            penalty += LOST_FACES_PENALTY
            notes.append("faces detectable in the original are no longer detected")
        excess = _clipping(candidate["metrics"]) - _clipping(source["metrics"]) - CLIPPING_TOLERANCE_POINTS
        if excess > 0:
            penalty += min(MAX_CLIPPING_PENALTY, excess)
            notes.append("clipped shadows or highlights increased noticeably")
        details[candidate["profile"]] = {
            "adjusted_score": round(candidate["overall_score"] - penalty, 2),
            "penalty": round(penalty, 2),
            "notes": notes,
        }
    if not details:
        return "", "No profile completed, so nothing can be recommended.", details

    def conservatism(profile):
        return CONSERVATIVE_ORDER.index(profile) if profile in CONSERVATIVE_ORDER else len(CONSERVATIVE_ORDER)

    top = max(item["adjusted_score"] for item in details.values())
    contenders = [name for name, item in details.items() if item["adjusted_score"] >= top - TIE_MARGIN]
    best = min(contenders, key=lambda name: (conservatism(name), -details[name]["adjusted_score"]))
    gain = details[best]["adjusted_score"] - source["overall_score"]
    if gain < MIN_RECOMMENDED_GAIN:
        return (
            ORIGINAL,
            f"No profile improved on the original (best was {best} at "
            f"{details[best]['adjusted_score']} vs {source['overall_score']}); keep the original.",
            details,
        )
    reason = (
        f"{best} scored {details[best]['adjusted_score']} against "
        f"{source['overall_score']} for the original (+{round(gain, 2)})."
    )
    if details[best]["adjusted_score"] < top:
        reason += (
            f" It is within {TIE_MARGIN} point of the top score ({top}), so the more "
            "conservative profile was preferred."
        )
    if details[best]["notes"]:
        reason += " Note: " + "; ".join(details[best]["notes"]) + "."
    return best, reason, details


def _scores(metrics):
    from quality_control.services import calculate_scores

    return calculate_scores(metrics)


def _count_faces(image):
    """Faces the configured detector finds, or None when detection is unavailable."""
    try:
        from validation.services import get_face_detector

        return len(get_face_detector().detect(image).boxes)
    except (ValidationError, cv2.error, OSError, ValueError):
        return None


def _shrink(image, max_edge):
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    return cv2.resize(
        image, (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _jpeg(image):
    success, encoded = cv2.imencode(
        ".jpg", _shrink(image, settings.COMPARISON_MAX_EDGE),
        [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY],
    )
    if not success:
        raise ValidationError("A comparison preview could not be encoded.")
    return encoded.tobytes()


def _store_output(comparison, profile, **fields):
    preview = fields.pop("preview", None)
    output = ModelComparisonOutput(comparison=comparison, profile=profile, **fields)
    if preview is not None:
        output.preview_file.save(f"{profile}.jpg", ContentFile(preview), save=False)
    output.save()
    return output


def process_model_comparison(comparison):
    from enhancement.services import (
        _read_enhancement_source,
        analyze_image_quality,
        enhance_bgr,
    )
    from privacy.services import biometric_processing_allowed

    with transaction.atomic():
        comparison = (
            ModelComparison.objects.select_for_update(of=("self",))
            .select_related("owner", "image", "image__preprocessing_result")
            .get(pk=comparison.pk)
        )
        if comparison.status in (EnhancementStatus.COMPLETED, EnhancementStatus.CANCELLED):
            return comparison
        comparison.status = EnhancementStatus.PROCESSING
        comparison.started_at = comparison.started_at or timezone.now()
        comparison.finished_at = None
        comparison.failure_reason = ""
        comparison.save(update_fields=["status", "started_at", "finished_at", "failure_reason", "updated_at"])
        # A retry starts clean; the unique constraint allows one output per profile.
        comparison.outputs.all().delete()

    started = time.perf_counter()
    try:
        holder = SimpleNamespace(image=comparison.image, preprocessing_result=comparison.image.preprocessing_result)
        source, _strategy = _read_enhancement_source(holder)
        source = _shrink(source, settings.COMPARISON_MAX_EDGE)
        allow_biometric = biometric_processing_allowed(comparison.owner)

        source_metrics = analyze_image_quality(source)
        source_scores = _scores(source_metrics)
        source_faces = _count_faces(source)
        _store_output(
            comparison, ORIGINAL, outcome=ModelComparisonOutput.Outcome.COMPLETED,
            preview=_jpeg(source), algorithm="none (untouched source)",
            metrics=source_metrics, scores=source_scores,
            overall_score=source_scores["overall_score"], faces_detected=source_faces,
        )
        comparison.reference_dimensions = [int(source.shape[1]), int(source.shape[0])]

        candidates = []
        for key in comparison.profiles:
            profile = PROFILES[key]
            run_started = time.perf_counter()
            try:
                enhanced, applied_level, parameters = enhance_bgr(
                    source, comparison.requested_level, style=profile.style,
                    allow_biometric_processing=allow_biometric,
                    use_zero_dce=profile.use_zero_dce,
                )
                metrics = parameters["quality_comparison"]["after"]
                scores = _scores(metrics)
                guard = parameters.get("identity_guard") or {}
                output = _store_output(
                    comparison, key, outcome=ModelComparisonOutput.Outcome.COMPLETED,
                    preview=_jpeg(enhanced), algorithm=parameters["algorithm"],
                    metrics=metrics,
                    scores={**scores, "deltas": parameters["quality_comparison"]["deltas"]},
                    overall_score=scores["overall_score"],
                    faces_detected=_count_faces(_shrink(enhanced, settings.COMPARISON_MAX_EDGE)),
                    identity_guard={
                        name: guard.get(name)
                        for name in ("status", "similarity", "strength", "faces_compared")
                    },
                    warnings=parameters.get("warnings", []),
                    processing_duration_ms=max(1, round((time.perf_counter() - run_started) * 1000)),
                )
                candidates.append({
                    "profile": key, "overall_score": output.overall_score,
                    "metrics": metrics, "faces_detected": output.faces_detected,
                    "identity_guard": output.identity_guard,
                })
            except (ValidationError, cv2.error, OSError, ValueError):
                logger.exception("Comparison profile %s failed for comparison %s.", key, comparison.pk)
                _store_output(
                    comparison, key, outcome=ModelComparisonOutput.Outcome.FAILED,
                    failure_reason="This profile could not process the image safely.",
                    processing_duration_ms=max(1, round((time.perf_counter() - run_started) * 1000)),
                )

        recommended, reason, details = recommend(
            {"overall_score": source_scores["overall_score"], "metrics": source_metrics,
             "faces_detected": source_faces},
            candidates,
        )
        for key, detail in details.items():
            output = comparison.outputs.get(profile=key)
            output.scores = {**output.scores, **detail}
            output.save(update_fields=["scores"])
        comparison.recommended_profile = recommended
        comparison.recommendation_reason = reason
        comparison.status = EnhancementStatus.COMPLETED if candidates else EnhancementStatus.FAILED
        if not candidates:
            comparison.failure_reason = "No profile could process the image safely."
    except (ValidationError, cv2.error, OSError, ValueError) as error:
        logger.exception("Model comparison %s failed.", comparison.pk)
        comparison.outputs.all().delete()
        comparison.status = EnhancementStatus.FAILED
        comparison.failure_reason = "The image could not be compared safely."
        if isinstance(error, ValidationError):
            comparison.failure_reason = "The source image could not be read safely."
    comparison.total_duration_ms = max(1, round((time.perf_counter() - started) * 1000))
    comparison.finished_at = timezone.now()
    comparison.save()
    return comparison


REPORT_COLUMNS = (
    "profile", "label", "status", "recommended", "overall_score", "adjusted_score",
    "brightness_score", "sharpness_score", "contrast_score", "noise_score",
    "exposure_score", "resolution_score", "mean_luminance", "contrast_stddev",
    "laplacian_sharpness", "estimated_noise", "shadow_clipping_percent",
    "highlight_clipping_percent", "faces_detected", "identity_guard",
    "processing_ms", "algorithm",
)


def build_report_csv(comparison):
    """The evaluation report: one row per profile plus the untouched original."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(REPORT_COLUMNS)
    for output in comparison.outputs.all():
        if output.profile == ORIGINAL:
            label = "Original (untouched)"
        else:
            label = PROFILES[output.profile].label if output.profile in PROFILES else output.profile
        scores, metrics = output.scores or {}, output.metrics or {}
        writer.writerow([
            output.profile, label, output.outcome,
            "yes" if output.profile == comparison.recommended_profile else "",
            output.overall_score if output.overall_score is not None else "",
            scores.get("adjusted_score", ""),
            scores.get("brightness_score", ""), scores.get("sharpness_score", ""),
            scores.get("contrast_score", ""), scores.get("noise_score", ""),
            scores.get("exposure_score", ""), scores.get("resolution_score", ""),
            metrics.get("mean_luminance", ""), metrics.get("contrast_stddev", ""),
            metrics.get("laplacian_sharpness", ""), metrics.get("estimated_noise", ""),
            metrics.get("shadow_clipping_percent", ""), metrics.get("highlight_clipping_percent", ""),
            "" if output.faces_detected is None else output.faces_detected,
            (output.identity_guard or {}).get("status") or "",
            output.processing_duration_ms, output.algorithm,
        ])
    return buffer.getvalue()
