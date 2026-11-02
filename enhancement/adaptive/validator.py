"""FinalImageQualityValidator: accept or reject an enhancement candidate, with reasons.

It never scores a candidate on one number. A candidate must pass **gates**, and only candidates that pass are compared:

1. *Geometry*: the size is what the operation promises (unchanged, or the requested scale).
2. *Data consistency*: shrunk back to the parent's size and softened, the candidate must still look like the parent.
   This is the main defence against hallucination: a model that invents structure changes the low frequencies too.
3. *Colour*: the low-frequency chroma may not drift (skin tone, clothing and background colour are not for a
   restoration model to change). A low-light lift changes brightness by design, so it is judged on the chromaticity of linear
   light instead: the colours must not turn pale or grey, over-saturate, or rotate in hue.
4. *Artifacts*: no pixel overshoot beyond the parent's local range (halos, ringing), no growth in highlight clipping,
   and no more noise than the parent unless the operation is a lift that a denoise stage will follow.
5. *Identity*, for every candidate when the original has a face that could be embedded: the worst per-face embedding
   similarity must stay above the provider's threshold, and the facial landmarks must not move.
6. *Does it fix what it was meant to fix?* Operation-specific: a lift must make the subject visible without
   over-brightening; a denoise must lower the noise estimate without flattening detail; a deblur must restore edge energy;
   a face restoration must sharpen the face; an upscale must add detail without leaving the data-consistency limit.

Among the candidates that pass, the one that fixes the most for the least change is preferred (``improvement`` minus a
change cost), never "the sharpest" or "the brightest". The limits are in ``Limits`` with their provenance. They are
conservative starting values measured on a small set of photographs (docs/evaluation/); the gates reject more often than
they could, because returning the original is a safe answer and returning an altered face is not.

Reference-based metrics (PSNR, SSIM, optional LPIPS) exist for tests and benchmarks where a clean reference is known
(``reference_metrics``); a photograph in the wild has none, and no no-reference number here replaces looking at it.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np
from django.conf import settings

from .colour import lift_colour_metrics


@dataclass(frozen=True)
class Limits:
    consistency_psnr_min: float = 27.0      # dB, on both images softened (sigma 2) at the parent's size
    consistency_psnr_min_lowlight: float = 12.0   # a lift changes brightness by design; structure is checked instead
    structure_ssim_min_lowlight: float = 0.70     # SSIM of brightness-normalised luminance
    chroma_shift_max: float = 6.0           # mean |delta| of the low-frequency a and b channels (8-bit LAB units)
    chroma_shift_max_lowlight: float = 12.0   # kept for reporting; a lift is judged by the two linear-light limits below instead
    # A lift is judged on chromaticity in linear light (colour.lift_colour_metrics), which an exposure change leaves unchanged.
    # Measured on the calibration photographs (scripts/evaluate_lowlight_colour.py, 16 photos x 4 darkening levels): the colour
    # method in use gives a saturation ratio 0.92-0.99, the raw network 0.62-0.80, the earlier square-root chroma gain 0.46-0.89 and
    # an over-strong gain 1.14-2.19. The pale limit sits between the raw network's maximum and the used method's minimum; the
    # over-saturation limit is its reciprocal. Held-out photographs agreed (used method 0.94-0.99, raw 0.63-0.80).
    lift_saturation_min: float = 0.85
    lift_saturation_max: float = 1.18
    lift_hue_shift_max: float = 15.0        # degrees; the used method's mean is 4-5 (its p90 about 7), the other methods' 12-14 at worst
    overshoot_max: float = 0.02             # share of pixels outside the parent's local 5x5 range by more than 10 levels
    overshoot_max_sharpening: float = 0.04
    highlight_clip_growth_max: float = 3.0  # percentage points
    noise_growth_max: float = 1.15          # noise after / before, for operations that must not add noise
    noise_growth_max_lowlight: float = 3.0
    # per operation
    lowlight_scene_median_target: float = 105.0
    lowlight_scene_median_ceiling: float = 175.0
    lowlight_min_gain: float = 8.0
    lowlight_face_range_target: float = 110.0
    denoise_min_noise_reduction: float = 0.15
    # Structure energy after / before. The parent's own noise inflates its structure energy, so a good denoise of a noisy
    # photograph reads 0.5-0.9 (measured: NLM on sigma-22 noise 0.52 of the noisy, 0.69 of the clean) while a flattened one
    # reads about 0.05. The calibrated denoise guard (denoise_guard.py) is the finer judge of lost detail.
    denoise_min_detail_retention: float = 0.40
    deblur_min_edge_gain: float = 1.10
    deblur_max_edge_gain: float = 2.50
    face_min_detail_gain: float = 1.25      # face detail after / before
    face_deblur_min_detail_gain: float = field(default_factory=lambda: float(getattr(settings, "ADAPTIVE_FACE_DEBLUR_MIN_DETAIL_GAIN", 1.25)))   # a deblur chosen for a blurred *face* must raise the face's own detail at least this much
    face_max_detail: float = 260.0          # above this (sharp faces: median 119, p90 175) the detail is probably invented
    face_landmark_shift_max: float = 0.04   # of the face width, mean over the five landmarks
    # On a tiny face the detector's own jitter is a large share of the face width: a 21 px silhouette measured 0.078 (1.6 px) for a super-resolution
    # candidate that passed every other gate (data consistency 49.6 dB, detail 1.25x), so a photograph with a speck of a "face" could never be
    # enlarged. A displacement under this many pixels of the ORIGINAL photograph is detection noise, not a changed face. It never applies to a
    # generative face restoration (always strict) nor to polish (its own 0.02 gate), and a face that is no longer found is still a loss.
    landmark_pixel_floor: float = field(default_factory=lambda: float(getattr(settings, "ADAPTIVE_LANDMARK_PIXEL_FLOOR", 2.0)))
    # An upscaled candidate's landmarks are found again on an interpolated image, so a little jitter is not a changed face: plain bicubic
    # enlargement of three 240 px portraits already shifted them by 0.019, 0.026 and 0.037 (measured on real photographs).
    face_landmark_shift_max_upscale: float = field(default_factory=lambda: float(getattr(settings, "ADAPTIVE_UPSCALE_LANDMARK_SHIFT_MAX", 0.06)))
    upscale_detail_gain_max: float = 3.0
    # polish (a gentle improvement of a photograph with nothing to fix): it must be clearly the same photograph and measurably better
    polish_min_gain: float = 1.05                  # the main face's detail, or the frame's contrast (std of luminance), must rise at least 5 percent
    polish_clip_growth_max: float = 0.5            # percentage points of newly clipped highlight pixels
    polish_noise_growth_max: float = 1.10          # noise estimate after / before (plus 0.02 of slack on the "moderate = 1.0" scale)
    polish_delta_e_max: float = 3.0                # mean CIELAB distance from the parent (76, at the 1024 px working scale)
    polish_face_landmark_shift_max: float = 0.02   # of the face width (the other operations allow 0.04)
    polish_face_detail_ceiling: float = 175.0      # a sharp face's 90th percentile (face_sharpness.py): polish never pushes a face past it,
    polish_face_growth_max: float = 1.10           # unless the face was already above it, and then by at most this factor


def _gray(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _to_size(image, shape):
    if image.shape[:2] == shape[:2]:
        return image
    return cv2.resize(image, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def consistency_psnr(parent, candidate):
    a = cv2.GaussianBlur(parent, (0, 0), 2.0)
    b = cv2.GaussianBlur(_to_size(candidate, parent.shape), (0, 0), 2.0)
    return float(cv2.PSNR(a, b))


def structure_ssim(parent, candidate):
    from enhancement.services import _structural_similarity

    def normalised(image):
        gray = _gray(_to_size(image, parent.shape)).astype(np.float32)
        value = (gray - gray.mean()) / max(float(gray.std()), 1e-6)
        return np.clip(value * 40 + 128, 0, 255).astype(np.uint8)[..., None].repeat(3, axis=2)

    return float(_structural_similarity(normalised(parent), normalised(candidate)))


def chroma_shift(parent, candidate):
    def lowpass_ab(image):
        lab = cv2.cvtColor(cv2.GaussianBlur(_to_size(image, parent.shape), (0, 0), 3.0), cv2.COLOR_BGR2LAB).astype(np.float32)
        return lab[:, :, 1:]

    return float(np.mean(np.abs(lowpass_ab(parent) - lowpass_ab(candidate))))


def overshoot_fraction(parent, candidate):
    """Share of pixels more than 10 levels outside the parent's local 5x5 range: halos and ringing."""
    gray_parent = _gray(parent)
    gray_candidate = _gray(_to_size(candidate, parent.shape)).astype(np.int16)
    kernel = np.ones((5, 5), np.uint8)
    high = cv2.dilate(gray_parent, kernel).astype(np.int16) + 10
    low = cv2.erode(gray_parent, kernel).astype(np.int16) - 10
    return float(((gray_candidate > high) | (gray_candidate < low)).mean())


def structure_energy(image, long_edge=1024):
    """Mean |Laplacian| after a small blur: structure, not grain (plain edge energy counts noise as detail)."""
    gray = _gray(image)
    scale = long_edge / max(gray.shape[:2])
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return float(np.mean(np.abs(cv2.Laplacian(cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.2), cv2.CV_32F))))


def edge_energy(image, long_edge=1024):
    gray = _gray(image)
    scale = long_edge / max(gray.shape[:2])
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    laplacian = cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F)
    return float(np.mean(np.abs(laplacian)))


def landmark_shift(faces_before, faces_after, scale=1.0):
    """Mean landmark displacement over the face width, matching each original face to the nearest candidate face.

    ``scale`` is the candidate's size over the original's (2.0 after a x2 upscale): the original's faces are brought onto the
    candidate's pixel grid first. Without it every face of an upscaled candidate looked "lost" (its centre had moved away by
    the enlargement itself), so a super-resolution result was never accepted for a photograph that had a face.
    """
    shifts = []
    for before in faces_before:
        if not before.landmarks or len(before.landmarks) != 5:
            continue
        centre = scale * np.array([before.box[0] + before.box[2] / 2, before.box[1] + before.box[3] / 2])
        width = scale * before.box[2]
        best = None
        for after in faces_after:
            if not after.landmarks or len(after.landmarks) != 5:
                continue
            distance = float(np.linalg.norm(centre - np.array([after.box[0] + after.box[2] / 2, after.box[1] + after.box[3] / 2])))
            if distance < width and (best is None or distance < best[0]):
                best = (distance, after)
        if best is None:
            return None  # a face that was there is no longer found
        shifts.append(float(np.mean(np.linalg.norm(scale * np.array(before.landmarks) - np.array(best[1].landmarks), axis=1)) / width))
    return float(np.mean(shifts)) if shifts else 0.0


def local_contrast(image, long_edge=1024, tile=16):
    """Mean local standard deviation of luminance over ``tile`` x ``tile`` windows at the working scale: what a local-contrast operation changes."""
    gray = _gray(image).astype(np.float32)
    scale = long_edge / max(gray.shape[:2])
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    mean = cv2.blur(gray, (tile, tile))
    variance = cv2.blur(gray * gray, (tile, tile)) - mean * mean
    return float(np.sqrt(np.maximum(variance, 0.0)).mean())


def mean_delta_e(parent, candidate, long_edge=1024):
    """Mean CIELAB distance (CIE76) between two same-sized images at the working scale: 0 is the same picture, about 2.3 is a just-noticeable difference."""
    scale = long_edge / max(parent.shape[:2])

    def lab(image):
        if scale < 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(image.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)

    return float(np.sqrt(((lab(parent) - lab(_to_size(candidate, parent.shape))) ** 2).sum(axis=2)).mean())


def reference_metrics(clean, image):
    """PSNR, SSIM and (when the optional ``lpips`` package and its weights are available) LPIPS against a clean reference.

    Images are brought to the reference's size first; a mismatch that needs resizing is reported, not hidden.
    """
    from enhancement.services import _structural_similarity

    resized = image.shape[:2] != clean.shape[:2]
    candidate = _to_size(image, clean.shape)
    metrics = {"psnr": round(float(cv2.PSNR(clean, candidate)), 3), "ssim": round(float(_structural_similarity(clean, candidate)), 4),
               "resized_to_reference": resized, "lpips": None}
    try:
        import lpips
        import torch

        network = reference_metrics.__dict__.setdefault("_lpips", lpips.LPIPS(net="alex", verbose=False))

        def tensor(picture):
            rgb = cv2.cvtColor(picture, cv2.COLOR_BGR2RGB).astype(np.float32) / 127.5 - 1.0
            return torch.from_numpy(rgb.transpose(2, 0, 1)).unsqueeze(0)

        with torch.inference_mode():
            metrics["lpips"] = round(float(network(tensor(clean), tensor(candidate)).item()), 4)
    except Exception as error:  # LPIPS is optional; its absence is stated, not hidden
        metrics["lpips_unavailable"] = f"{type(error).__name__}"
    return metrics


class FinalImageQualityValidator:
    """Judge candidates against the original and the image they were made from."""

    def __init__(self, analyzer, original_report, identity=None, limits=None):
        self.analyzer = analyzer
        self.original_report = original_report
        self.identity = identity            # an object with .check(image) -> dict, or None
        self.limits = limits or Limits()

    def evaluate(self, parent, candidate, operation, parent_report, expected_scale=1, candidate_report=None):
        """``{accepted, reasons, gates, metrics, improvement, change_cost}`` for one candidate."""
        limits = self.limits
        lowlight = operation == "low_light_enhancement"
        polish = operation == "polish"
        verdict = {"accepted": False, "reasons": [], "gates": {}, "metrics": {}, "improvement": 0.0, "change_cost": 0.0}

        def gate(name, passed, detail, reason=None):
            verdict["gates"][name] = {"passed": bool(passed), **detail}
            if not passed:
                verdict["reasons"].append(reason or name)

        expected = (parent.shape[0] * expected_scale, parent.shape[1] * expected_scale)
        gate("geometry", candidate.shape[:2] == expected and candidate.dtype == np.uint8,
             {"expected": [expected[1], expected[0]], "actual": [candidate.shape[1], candidate.shape[0]]}, "wrong_dimensions")
        if not verdict["gates"]["geometry"]["passed"]:
            return verdict
        if not np.isfinite(candidate.astype(np.float32)).all():
            gate("finite", False, {}, "non_finite_pixels")
            return verdict

        if lowlight:
            ssim = structure_ssim(parent, candidate)
            gate("data_consistency", ssim >= limits.structure_ssim_min_lowlight, {"structure_ssim": round(ssim, 3)}, "structure_changed")
        else:
            psnr = consistency_psnr(parent, candidate)
            gate("data_consistency", psnr >= limits.consistency_psnr_min, {"psnr": round(psnr, 2)}, "content_not_consistent_with_input")
        shift = chroma_shift(parent, candidate)
        if lowlight:
            colour = lift_colour_metrics(parent, candidate)
            hue, saturation = colour["hue_shift"], colour["saturation_ratio"]
            detail = {"hue_shift": None if hue is None else round(hue, 2),
                      "saturation_ratio": None if saturation is None else round(saturation, 3), "chroma_shift": round(shift, 2)}
            if hue is not None and hue > limits.lift_hue_shift_max:
                gate("colour", False, detail, "colour_drift")
            elif saturation is not None and saturation < limits.lift_saturation_min:
                gate("colour", False, detail, "colour_washed_out")
            elif saturation is not None and saturation > limits.lift_saturation_max:
                gate("colour", False, detail, "colour_oversaturated")
            else:
                gate("colour", True, detail)
        else:
            gate("colour", shift <= limits.chroma_shift_max, {"chroma_shift": round(shift, 2)}, "colour_drift")
        if lowlight:
            gate("artifacts", True, {"overshoot": "not applicable to a lift (it raises amplitude by design)"})
        else:
            overshoot = overshoot_fraction(parent, candidate) if expected_scale == 1 else overshoot_fraction(
                cv2.resize(parent, (candidate.shape[1], candidate.shape[0]), interpolation=cv2.INTER_CUBIC), candidate)
            sharpening = operation in ("motion_deblurring", "face_restoration", "super_resolution", "polish")
            gate("artifacts", overshoot <= (limits.overshoot_max_sharpening if sharpening else limits.overshoot_max),
                 {"overshoot": round(overshoot, 4)}, "halo_or_ringing")

        candidate_report = candidate_report or self.analyzer.analyze(candidate)
        verdict["metrics"]["candidate_conditions"] = candidate_report.conditions
        clip_growth = candidate_report.base["highlight_clipping_percent"] - parent_report.base["highlight_clipping_percent"]
        gate("highlights", clip_growth <= (limits.polish_clip_growth_max if polish else limits.highlight_clip_growth_max),
             {"clipping_growth": round(clip_growth, 2)}, "highlight_clipping_increased")
        # "level" is the larger of the two noise measures in units of the "moderate" threshold (1.0 = moderate); the 0.25 allowance
        # matches the earlier one level of the 3x3 estimator against its moderate threshold of 4.
        noise_before, noise_after = parent_report.noise["level"], candidate_report.noise["level"]
        allowed = limits.noise_growth_max_lowlight if lowlight else limits.noise_growth_max
        # A lift may take grain up to the "moderate" level (1.0), where the denoise stage that follows takes over; the 3x growth bound
        # still applies beyond it.
        if polish:   # a polish may not add noise beyond 10 percent (and nothing absolute: it is for photographs that have none to spare)
            gate("noise", noise_after <= noise_before * limits.polish_noise_growth_max + 0.02,
                 {"before": noise_before, "after": noise_after}, "noise_increased")
            delta_e = mean_delta_e(parent, candidate)
            gate("same_photograph", delta_e <= limits.polish_delta_e_max, {"delta_e": round(delta_e, 2)}, "photograph_changed_too_much")
        else:
            gate("noise", noise_after <= max(noise_before * allowed, noise_before + 0.25, 1.0 if lowlight else 0.0),
                 {"before": noise_before, "after": noise_after}, "noise_increased")

        self._identity_gate(verdict, candidate, candidate_report, operation)
        improvement = self._improvement(verdict, operation, parent, candidate, parent_report, candidate_report, expected_scale)
        verdict["improvement"] = round(float(improvement), 4)
        mean_change = float(np.mean(np.abs(_gray(_to_size(candidate, parent.shape)).astype(np.float32) - _gray(parent).astype(np.float32)))) / 255.0
        verdict["change_cost"] = round(mean_change, 4)
        verdict["accepted"] = all(item["passed"] for item in verdict["gates"].values())
        return verdict

    # -- identity ---------------------------------------------------------------------------------------------
    def _identity_gate(self, verdict, candidate, candidate_report, operation):
        if not self.original_report.faces:
            verdict["gates"]["identity"] = {"passed": True, "status": "no_face"}
            return
        scale = candidate_report.width / self.original_report.width if self.original_report.width else 1.0
        shift = landmark_shift(self.original_report.faces, candidate_report.faces, scale)
        shift_max = self.limits.face_landmark_shift_max_upscale if operation == "super_resolution" else self.limits.face_landmark_shift_max
        if operation == "polish":
            shift_max = self.limits.polish_face_landmark_shift_max
        elif operation != "face_restoration" and self.limits.landmark_pixel_floor > 0:
            widths = [face.box[2] for face in self.original_report.faces if face.landmarks and len(face.landmarks) == 5 and face.box[2] > 0]
            if widths:
                shift_max = max(shift_max, self.limits.landmark_pixel_floor / (sum(widths) / len(widths)))
        landmarks_ok = shift is not None and shift <= shift_max
        status = {"passed": landmarks_ok, "landmark_shift": None if shift is None else round(shift, 4)}
        if not landmarks_ok:
            verdict["reasons"].append("face_lost" if shift is None else "face_geometry_changed")
        if self.identity is None:
            status.update(similarity=None, embedding="unavailable")
            if operation == "face_restoration":  # a generative restoration is never accepted unverified
                status["passed"] = False
                verdict["reasons"].append("identity_unverifiable")
        else:
            checked = self.identity.check(candidate)
            status.update(embedding=checked["status"], similarity=checked.get("similarity"), threshold=checked.get("threshold"))
            if checked["status"] == "failed":
                status["passed"] = False
                verdict["reasons"].append("identity_changed")
            elif checked["status"] != "verified" and operation == "face_restoration":
                status["passed"] = False
                verdict["reasons"].append("identity_unverifiable")
        verdict["gates"]["identity"] = status

    # -- does it fix what it was for ---------------------------------------------------------------------------
    def _improvement(self, verdict, operation, parent, candidate, before, after, scale):
        limits = self.limits

        def gate(name, passed, detail, reason):
            verdict["gates"][name] = {"passed": bool(passed), **detail}
            if not passed:
                verdict["reasons"].append(reason)

        if operation == "low_light_enhancement":
            face_before, face_after = before.main_face, after.main_face
            median_before, median_after = before.exposure["scene_median"], after.exposure["scene_median"]
            if face_before is not None and face_after is not None and face_before.luminance["dynamic_range"] is not None:
                range_before, range_after = face_before.luminance["dynamic_range"], face_after.luminance["dynamic_range"]
                gain = (range_after - range_before) / max(limits.lowlight_face_range_target - range_before, 1.0)
                crushed_ok = face_after.luminance["crushed_fraction"] <= face_before.luminance["crushed_fraction"]
                gate("improves_defect", gain > 0.15 and crushed_ok and median_after <= limits.lowlight_scene_median_ceiling,
                     {"face_range_before": range_before, "face_range_after": range_after}, "subject_not_clearer_or_too_bright")
                return float(np.clip(gain, 0, 1))
            gain = (median_after - median_before) / max(limits.lowlight_scene_median_target - median_before, 1.0)
            gate("improves_defect", median_after - median_before >= limits.lowlight_min_gain
                 and median_after <= limits.lowlight_scene_median_ceiling,
                 {"median_before": median_before, "median_after": median_after}, "no_useful_lift_or_too_bright")
            return float(np.clip(gain, 0, 1))
        if operation == "denoising":
            reduction = 1.0 - after.noise["level"] / max(before.noise["level"], 0.125)
            detail = structure_energy(candidate) / max(structure_energy(parent), 1e-6)
            from enhancement.denoise_guard import DenoiseReference, judge_candidate

            _blended, guard = judge_candidate(DenoiseReference(parent), candidate)
            full_strength = bool(guard["accepted"]) and float(guard.get("applied_blend", 1.0)) >= 0.99
            gate("improves_defect", reduction >= limits.denoise_min_noise_reduction and detail >= limits.denoise_min_detail_retention,
                 {"noise_reduction": round(reduction, 3), "detail_retention": round(detail, 3)}, "noise_not_reduced_or_detail_lost")
            gate("denoise_guard", full_strength, {"status": guard.get("status"), "applied_blend": guard.get("applied_blend")},
                 "denoise_guard_rejected")
            return float(np.clip(reduction, 0, 1))
        if operation == "motion_deblurring":
            gain = edge_energy(candidate) / max(edge_energy(parent), 1e-6)
            retention_before, retention_after = before.blur["retention"], after.blur["retention"]
            if before.face_blur.get("status") == "high" and after.face_blur.get("detail") is not None:
                # Chosen for a blurred face, so judged on the face: its own detail must rise clearly (and not past what a real
                # face holds), while the frame as a whole is not over-sharpened.
                face_before, face_after = before.face_blur["detail"], after.face_blur["detail"]
                face_gain = face_after / max(face_before, 1.0)
                gate("improves_defect",
                     face_gain >= limits.face_deblur_min_detail_gain and face_after <= limits.face_max_detail
                     and gain <= limits.deblur_max_edge_gain,
                     {"face_detail_before": face_before, "face_detail_after": face_after, "face_detail_gain": round(face_gain, 3),
                      "edge_gain": round(gain, 3)}, "face_not_clearer_or_over_sharpened")
                return float(np.clip((face_gain - 1.0) / 3.0, 0, 1))
            gate("improves_defect", limits.deblur_min_edge_gain <= gain <= limits.deblur_max_edge_gain,
                 {"edge_gain": round(gain, 3), "retention_before": retention_before, "retention_after": retention_after},
                 "edges_not_restored_or_over_sharpened")
            return float(np.clip((gain - 1.0) / 0.6, 0, 1))
        if operation == "face_restoration":
            detail_before = [f.detail for f in before.faces if f.detail is not None]
            detail_after = [f.detail for f in after.faces if f.detail is not None]
            gain = float(np.mean(detail_after) / max(np.mean(detail_before), 1.0)) if detail_before and detail_after else 0.0
            ceiling = float(np.max(detail_after)) if detail_after else 0.0
            gate("improves_defect", gain >= limits.face_min_detail_gain and ceiling <= limits.face_max_detail,
                 {"detail_gain": round(gain, 3), "detail_after": round(ceiling, 1)}, "face_not_clearer_or_detail_invented")
            return float(np.clip((gain - 1.0) / 2.0, 0, 1))
        if operation == "polish":
            face_before, face_after = before.main_face, after.main_face
            face_gain = None
            if face_before is not None and face_after is not None and face_before.detail and face_after.detail:
                face_gain = face_after.detail / face_before.detail
            local_gain = local_contrast(candidate) / max(local_contrast(parent), 1e-6)
            global_gain = after.base["contrast_stddev"] / max(before.base["contrast_stddev"], 1e-6)
            frame_gain = structure_energy(candidate) / max(structure_energy(parent), 1e-6)
            # "Improves" is judged on the main face's detail and on the frame's contrast (its standard deviation), never on local contrast:
            # a local-contrast operation always raises that, so it would pass itself. A photograph that is already balanced and sharp
            # gains less than 5 percent on both, and stays as it is.
            gains = [value for value in (face_gain, global_gain) if value is not None]
            improves = max(gains) >= limits.polish_min_gain
            nothing_worse = (face_gain is None or face_gain >= 1.0) and local_gain >= 1.0 and global_gain >= 1.0 and frame_gain >= 1.0
            ceiling = limits.polish_face_detail_ceiling
            face_ok = face_gain is None or face_after.detail <= max(ceiling, face_before.detail * limits.polish_face_growth_max)
            detail = {"face_detail_gain": None if face_gain is None else round(face_gain, 3), "local_contrast_gain": round(local_gain, 3),
                      "frame_contrast_gain": round(global_gain, 3), "frame_sharpness_gain": round(frame_gain, 3)}
            reason = ("no_measurable_improvement" if not improves else "something_got_worse" if not nothing_worse
                      else "face_over_sharpened")
            gate("improves_defect", improves and nothing_worse and face_ok, detail, reason)
            return float(np.clip((max(gains) - 1.0) / 0.25, 0, 1))
        if operation == "super_resolution":
            detail = edge_energy(candidate) / max(edge_energy(cv2.resize(parent, (candidate.shape[1], candidate.shape[0]),
                                                                           interpolation=cv2.INTER_CUBIC)), 1e-6)
            gate("improves_defect", 1.0 <= detail <= limits.upscale_detail_gain_max, {"detail_ratio_vs_bicubic": round(detail, 3)},
                 "no_added_detail_or_invented_detail")
            return float(np.clip((detail - 1.0) / 1.0, 0, 1))
        return 0.0

    # -- choosing ---------------------------------------------------------------------------------------------
    @staticmethod
    def preference(verdict):
        """Higher is better: what it fixes, minus a cost for how much it changed the photograph."""
        return verdict["improvement"] - 2.0 * verdict["change_cost"]
