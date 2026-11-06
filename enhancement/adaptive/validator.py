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
    geometry_ignores_tiny_faces: bool = field(default_factory=lambda: bool(getattr(settings, "ADAPTIVE_GEOMETRY_IGNORES_TINY_FACES", True)))   # a face under 48 px cannot be judged: it does not veto
    # An upscaled candidate's landmarks are found again on an interpolated image, so a little jitter is not a changed face: plain bicubic
    # enlargement of three 240 px portraits already shifted them by 0.019, 0.026 and 0.037 (measured on real photographs).
    face_landmark_shift_max_upscale: float = field(default_factory=lambda: float(getattr(settings, "ADAPTIVE_UPSCALE_LANDMARK_SHIFT_MAX", 0.06)))
    # The share limits above are measured against the ORIGINAL photograph's landmarks, but the detector places the landmarks of ANY enlargement of a small or soft face somewhere else with no model
    # involved: on 93 public LFW faces (68-134 px wide) a plain bicubic x2 alone moved them by 0.040 (median) and 0.066 (90th percentile), so 15 of the 93 enlargements were refused for a face
    # that nothing had touched (on a 114 px face of the library: plain 0.078, Real-ESRGAN 0.080). What the MODEL moved is the candidate's landmarks against the landmarks the detector finds on a plain
    # enlargement of the original to the same size: 0.005 (median) and 0.035 (99th percentile) on the same faces. A candidate refused by the share limit is accepted when it stays within this much
    # of the plain enlargement. Never stricter than before (a candidate that passed still passes); 0 switches it off; never for a generative face restoration (always strict) nor for polish (its own
    # gate); a face that is no longer found is still a loss. docs/evaluation/2026-11-05-small-face-geometry.md.
    landmark_enlargement_shift_max: float = field(default_factory=lambda: float(getattr(settings, "ADAPTIVE_LANDMARK_ENLARGEMENT_SHIFT_MAX", 0.035)))
    # Both checks above read the ORIGINAL photograph, and the original of a dark or back-lit photograph has a face the detector can hardly see: its landmarks, and those of a plain enlargement of it, are
    # guesses. Once a lift is accepted, a step those checks refuse is judged once more against the image it was GIVEN, which is lit (validator._lifted_input_opinion). Never stricter than before (a step that
    # passed still passes); never for a generative face restoration (always strict) nor for polish; a face that is no longer found is still a loss. False gives the earlier behaviour.
    baseline_after_lighting: bool = field(default_factory=lambda: bool(settings.ADAPTIVE_BASELINE_AFTER_LIGHTING))
    upscale_detail_gain_max: float = 3.0
    # polish (a gentle improvement of a photograph with nothing to fix): it must be clearly the same photograph and measurably better
    polish_min_gain: float = 1.05                  # the main face's detail, or the frame's contrast (std of luminance), must rise at least 5 percent
    polish_clip_growth_max: float = 0.5            # percentage points of newly clipped highlight pixels
    polish_noise_growth_max: float = 1.10          # noise estimate after / before (plus 0.02 of slack on the "moderate = 1.0" scale)
    polish_delta_e_max: float = 3.0                # mean CIELAB distance from the parent (76, at the 1024 px working scale)
    polish_face_landmark_shift_max: float = 0.02   # of the face width (the other operations allow 0.04)
    # Polish never moves a pixel, so a landmark shift after it is the detector's doing: on a soft, enlarged 209 px portrait it placed the landmarks 0.053 (11 px)
    # away for both polish variants although they passed every other gate (face detail x1.18, contrast x1.17, delta E 1.2), while the same detector moved 0.0054 at most
    # under harmless changes. The face's own structure is the stable evidence: structure SSIM of the padded face crop was 0.996-0.997 for polish and 0.965 for a 2 px shift, 0.874
    # for 6 px, 0.938 for a 2 percent change of scale. Polish passes the face gate when every face keeps at least this structure similarity (or when its landmarks held).
    polish_face_structure_ssim_min: float = 0.985
    polish_face_detail_ceiling: float = 175.0      # a sharp face's 90th percentile (face_sharpness.py): polish never pushes a face past it,
    # colourization (DDColor): it must leave the lightness as it was and give a plausible colour. Measured on 8 photographs (the portrait and 7 grey-converted
    # colour photographs, DDCOLOR damping on): gray PSNR 42.7-55.5 dB, mean chroma 2.3-20.8, 99th percentile 15-47, cast 0.8-19.0. The limits are backstops against a
    # runaway prediction, not a taste: the damping in colorize.py is what keeps colours natural.
    colourize_luminance_psnr_min: float = 38.0
    colourize_mean_chroma_max: float = 28.0
    colourize_p99_chroma_max: float = 58.0
    colourize_cast_max: float = 26.0
    colourize_min_mean_chroma: float = 1.5
    # detail restoration (a 4x model at 2x, shrunk back to the same size): measured on a soft, WhatsApp-compressed 1600 x 1382 portrait, structure energy x1.05-1.17 (x1.05 and x1.06 for
    # the two clean variants), blur and compression no longer found, PSNR against the original 37.5-39.2 dB. It must add real structure, but not an invented amount of it.
    detail_min_gain: float = 1.02
    detail_max_gain: float = 1.60
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

    def __init__(self, analyzer, original_report, identity=None, limits=None, original=None, source_enlarged=False):
        self.analyzer = analyzer
        self.original_report = original_report
        self.identity = identity            # an object with .check(image) -> dict, or None
        self.limits = limits or Limits()
        self.source_enlarged = bool(source_enlarged)        # the pipeline enlarged this photograph before the engine saw it (preprocessing's small-image enlargement): see the shadow-lift gate
        self.original = original            # the original's pixels (optional): the plain enlargement the geometry gate may compare an enlarged candidate with
        self._plain_enlargements = {}       # (width, height) -> the faces found on a plain bicubic enlargement of the original to that size
        self.lifted_input = False           # the service sets it once a lift is accepted: what the later steps are given is a lit photograph (ADAPTIVE_BASELINE_AFTER_LIGHTING)
        self._lifted_enlargement = None     # (the step's input, the size, the faces found on a plain enlargement of it): the last second opinion's, kept for the step's other variants

    def evaluate(self, parent, candidate, operation, parent_report, expected_scale=1, candidate_report=None, refine=False, local=None):
        """``{accepted, reasons, gates, metrics, improvement, change_cost}`` for one candidate. ``refine``: a detail-restoration candidate for a photograph with no defect (always-enhance). ``local``: ``"shadows"`` for a lift of the shadows only (a back-lit subject)."""
        limits = self.limits
        lowlight = operation == "low_light_enhancement"
        polish = operation == "polish"
        colourize = operation == "colourization"
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
        elif colourize:   # the colour changes by design, so consistency is judged on the lightness, which colourization must not touch
            psnr = float(cv2.PSNR(_gray(parent), _gray(candidate))) if (_gray(parent) != _gray(candidate)).any() else 99.0
            gate("data_consistency", psnr >= limits.colourize_luminance_psnr_min, {"luminance_psnr": round(psnr, 2)}, "lightness_changed")
        else:
            psnr = consistency_psnr(parent, candidate)
            gate("data_consistency", psnr >= limits.consistency_psnr_min, {"psnr": round(psnr, 2)}, "content_not_consistent_with_input")
        shift = chroma_shift(parent, candidate)
        if colourize:
            lab = cv2.cvtColor(candidate, cv2.COLOR_BGR2LAB).astype(np.float32)
            chroma = np.hypot(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0)
            mean, p99 = float(chroma.mean()), float(np.percentile(chroma[::4, ::4], 99))
            cast = float(np.hypot(lab[:, :, 1].mean() - 128.0, lab[:, :, 2].mean() - 128.0))
            detail = {"mean_chroma": round(mean, 1), "p99_chroma": round(p99, 1), "cast": round(cast, 1)}
            if mean > limits.colourize_mean_chroma_max or p99 > limits.colourize_p99_chroma_max:
                gate("colour", False, detail, "colour_oversaturated")
            elif cast > limits.colourize_cast_max:
                gate("colour", False, detail, "colour_cast")
            else:
                gate("colour", True, detail)
        elif lowlight:
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
            sharpening = operation in ("motion_deblurring", "face_restoration", "super_resolution", "polish", "detail_restoration")
            gate("artifacts", overshoot <= (limits.overshoot_max_sharpening if sharpening else limits.overshoot_max),
                 {"overshoot": round(overshoot, 4)}, "halo_or_ringing")

        candidate_report = candidate_report or self.analyzer.analyze(candidate)
        verdict["metrics"]["candidate_conditions"] = candidate_report.conditions
        if lowlight and local == "shadows":
            # A shadow lift raises what the shadows hold several times, so on a small, heavily compressed JPEG it makes the blocks of the compression visible (a blocky face: the library's ids 130, 182,
            # 200; docs/evaluation/2026-11-05-backlit-subject-lift.md). The analyzer's own reading of the lifted photograph decides, as it does when it refuses to sharpen a compressed photograph.
            # A photograph the pipeline ENLARGED first (a 260 px JPEG becomes 1040 px) hides what that reading looks for: its blocks are 32 px wide, not 8, so a blocky photograph reads clean (the same
            # reading, 1.19 / 1.10, as the clean library photographs at 1/1, 1/2 and 1/4 scale), and the lift of its shadows came back as a blocky face (library id 182, run through the app on 2026-11-06).
            # A photograph small enough to be enlarged is small enough to be a compressed one: its shadows are not lifted.
            blocks = candidate_report.compression
            gate("compression_blocks", blocks["status"] != "heavy" and not self.source_enlarged,
                 {"status": blocks["status"], "blockiness": blocks["blockiness"], "source_enlarged": self.source_enlarged},
                 "lift_on_enlarged_source" if self.source_enlarged else "lift_shows_compression_blocks")
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

        self._identity_gate(verdict, candidate, candidate_report, operation, parent, parent_report)
        improvement = self._improvement(verdict, operation, parent, candidate, parent_report, candidate_report, expected_scale, refine=refine)
        verdict["improvement"] = round(float(improvement), 4)
        mean_change = float(np.mean(np.abs(_gray(_to_size(candidate, parent.shape)).astype(np.float32) - _gray(parent).astype(np.float32)))) / 255.0
        verdict["change_cost"] = round(mean_change, 4)
        verdict["accepted"] = all(item["passed"] for item in verdict["gates"].values())
        return verdict

    # -- identity ---------------------------------------------------------------------------------------------
    def _face_structure_holds(self, parent, candidate):
        """Whether every original face keeps its structure (SSIM of the brightness-normalised, padded face crop) between two same-sized images."""
        if parent is None or candidate is None or parent.shape != candidate.shape:
            return None, None
        worst = 1.0
        for face in self.original_report.faces:
            x, y, w, h = face.box
            x0, y0 = max(0, int(x - 0.15 * w)), max(0, int(y - 0.15 * h))
            x1, y1 = min(parent.shape[1], int(x + 1.15 * w)), min(parent.shape[0], int(y + 1.15 * h))
            if x1 - x0 < 16 or y1 - y0 < 16:
                continue
            worst = min(worst, structure_ssim(parent[y0:y1, x0:x1], candidate[y0:y1, x0:x1]))
        return worst >= self.limits.polish_face_structure_ssim_min, worst

    def _shift_beyond_plain_enlargement(self, faces, candidate, candidate_report, scale, source=None):
        """The mean landmark shift (a share of the face width) of an enlarged candidate against the landmarks the detector finds on a plain bicubic enlargement of the ORIGINAL to the candidate's size:
        what the model moved, with the detector's own scale-dependent placement taken out. ``None`` when there is nothing to compare with: the setting is 0, the candidate is not a different size
        from the original (the plain enlargement would be the original itself), the original's pixels were not given, or a face is not found on the plain enlargement or on the candidate.
        The plain enlargement is analysed once per size, and only when the share limit has already refused the candidate.
        ``source``: the pixels to enlarge instead of the original: the lit image a step was given, once a lift is accepted (``faces`` are then that image's; ``_lifted_input_opinion``)."""
        original = self.original if source is None else source
        if (self.limits.landmark_enlargement_shift_max <= 0 or original is None or self.analyzer is None or candidate is None or not faces
                or abs(scale - 1.0) < 0.02):
            return None
        size = (int(candidate.shape[1]), int(candidate.shape[0]))
        if source is None:
            if size not in self._plain_enlargements:
                self._plain_enlargements[size] = self.analyzer.analyze(cv2.resize(original, size, interpolation=cv2.INTER_CUBIC)).faces
            plain = self._plain_enlargements[size]
        else:
            if self._lifted_enlargement is None or self._lifted_enlargement[0] is not source or self._lifted_enlargement[1] != size:
                self._lifted_enlargement = (source, size, self.analyzer.analyze(cv2.resize(source, size, interpolation=cv2.INTER_CUBIC)).faces)
            plain = self._lifted_enlargement[2]
        found = [other for other in plain if other.landmarks and len(other.landmarks) == 5]
        reference = []
        for face in faces:             # each original face, found again on the plain enlargement (the same matching as landmark_shift)
            centre = scale * np.array([face.box[0] + face.box[2] / 2, face.box[1] + face.box[3] / 2])
            near = [(float(np.linalg.norm(centre - np.array([other.box[0] + other.box[2] / 2, other.box[1] + other.box[3] / 2]))), other) for other in found]
            near = [pair for pair in near if pair[0] < scale * face.box[2]]
            if not near:
                return None
            reference.append(min(near, key=lambda pair: pair[0])[1])
        return landmark_shift(reference, candidate_report.faces, 1.0)

    def _lifted_input_opinion(self, candidate, candidate_report, parent, parent_report, shift_max):
        """The geometry of a step judged against the image it was GIVEN, once a lift was accepted (``ADAPTIVE_BASELINE_AFTER_LIGHTING``), after the checks against the ORIGINAL refused it.

        The original of a dark or back-lit photograph has a face the detector can hardly see, so its landmarks (and those of a plain enlargement of it) are guesses; the lifted photograph the step was
        given is lit, and what the step moved is read there: the candidate's landmarks against that image's at the step's own limit (``shift_max``) and, for an enlargement, against a plain enlargement of
        that image, with the detector's own placement taken out (``landmark_enlargement_shift_max``). The library's 29.jpg (a 293 px photograph; the lift and the face restoration were accepted): the enlargement
        read 0.073 against the original's landmarks (limit 0.06) and 0.046 beyond a plain enlargement of the DARK original (limit 0.035); against what it was given, 0.042 (docs/evaluation/2026-11-06-enlargement-after-lift.md).
        ``None`` when there is nothing to compare with, or a face that was there is no longer found (a loss is not rescued); otherwise the readings and ``passed``."""
        if parent is None or parent_report is None or not getattr(parent_report, "width", 0):
            return None
        faces = parent_report.faces
        if self.limits.geometry_ignores_tiny_faces:
            faces = [face for face in faces if face.quality != "too_small"]
        if not faces:
            return None
        scale = candidate_report.width / parent_report.width
        shift = landmark_shift(faces, candidate_report.faces, scale)
        if shift is None:
            return None
        opinion = {"landmark_shift_from_lifted_input": round(shift, 4), "passed": shift <= shift_max}
        if not opinion["passed"]:
            beyond = self._shift_beyond_plain_enlargement(faces, candidate, candidate_report, scale, source=parent)
            if beyond is not None:
                opinion["landmark_shift_beyond_plain_enlargement_of_lifted_input"] = round(beyond, 4)
                opinion["passed"] = beyond <= self.limits.landmark_enlargement_shift_max
        return opinion

    def _identity_gate(self, verdict, candidate, candidate_report, operation, parent=None, parent_report=None):
        if not self.original_report.faces:
            verdict["gates"]["identity"] = {"passed": True, "status": "no_face"}
            return
        scale = candidate_report.width / self.original_report.width if self.original_report.width else 1.0
        faces = self.original_report.faces
        tiny = 0
        if self.limits.geometry_ignores_tiny_faces and operation != "face_restoration":
            # A face the analyzer calls ``too_small`` (short edge under 48 px: it is not restored and its identity cannot be checked) is not held to the landmark limit either: on a silhouette or a
            # distant head the five landmarks are guesses and moved by 12 % of the face width when the photograph was merely enlarged, which refused the enlargement of a whole sunset photograph.
            kept = [face for face in faces if face.quality != "too_small"]
            tiny, faces = len(faces) - len(kept), kept
        shift = landmark_shift(faces, candidate_report.faces, scale) if faces else 0.0
        shift_max = self.limits.face_landmark_shift_max_upscale if operation == "super_resolution" else self.limits.face_landmark_shift_max
        if operation == "polish":
            shift_max = self.limits.polish_face_landmark_shift_max
        elif operation != "face_restoration" and self.limits.landmark_pixel_floor > 0:
            widths = [face.box[2] for face in faces if face.landmarks and len(face.landmarks) == 5 and face.box[2] > 0]
            if widths:
                shift_max = max(shift_max, self.limits.landmark_pixel_floor / (sum(widths) / len(widths)))
        landmarks_ok = shift is not None and shift <= shift_max
        status = {"landmark_shift": None if shift is None else round(shift, 4)}
        if tiny:
            status["tiny_faces_not_checked"] = tiny
        if not landmarks_ok and shift is not None and operation not in ("face_restoration", "polish"):
            beyond = self._shift_beyond_plain_enlargement(faces, candidate, candidate_report, scale)
            if beyond is not None:
                status["landmark_shift_beyond_plain_enlargement"] = round(beyond, 4)
                landmarks_ok = beyond <= self.limits.landmark_enlargement_shift_max
            if not landmarks_ok and self.lifted_input and self.limits.baseline_after_lighting:
                opinion = self._lifted_input_opinion(candidate, candidate_report, parent, parent_report, shift_max)
                if opinion is not None:
                    landmarks_ok = opinion.pop("passed")
                    status.update(opinion)
        if operation == "polish" and not landmarks_ok:
            holds, worst = self._face_structure_holds(parent, candidate)
            if holds is not None:
                status["face_structure_ssim"] = round(worst, 4)
                landmarks_ok = bool(holds)
        status["passed"] = landmarks_ok
        if not landmarks_ok:
            verdict["reasons"].append("face_lost" if shift is None else "face_geometry_changed")
        if self.identity is None:
            status.update(similarity=None, embedding="unavailable")
            if operation == "face_restoration":  # a generative restoration is never accepted unverified
                status["passed"] = False
                verdict["reasons"].append("identity_unverifiable")
        else:
            checked = self.identity.check(candidate, restoring=operation == "face_restoration")
            status.update(embedding=checked["status"], similarity=checked.get("similarity"), threshold=checked.get("threshold"))
            if checked.get("baseline"):
                status["baseline"] = checked["baseline"]            # after an accepted face restoration (service.RestoredBaseline): which comparison decided, the upload or the restored face
            if checked["status"] == "failed":
                status["passed"] = False
                verdict["reasons"].append("identity_changed")
            elif checked["status"] != "verified" and operation == "face_restoration":
                status["passed"] = False
                verdict["reasons"].append("identity_unverifiable")
        verdict["gates"]["identity"] = status

    # -- does it fix what it was for ---------------------------------------------------------------------------
    def _improvement(self, verdict, operation, parent, candidate, before, after, scale, refine=False):
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
            if settings.ADAPTIVE_ALWAYS_ENHANCE:
                # The finishing polish: a photograph that is already balanced and sharp cannot show a 5 percent gain, so it is judged on being SAFE and VISIBLE: no measure falls
                # below the tolerance of what it was (the other gates, the face ceiling below and the identity check still apply), and the photograph really changes.
                tolerance = float(settings.ADAPTIVE_FINISH_TOLERANCE)
                step = 4
                change = float(np.abs(candidate[::step, ::step].astype(np.int16) - parent[::step, ::step].astype(np.int16)).mean())
                improves = change >= float(settings.ADAPTIVE_FINISH_MIN_CHANGE)
                # The main face's detail is not asked to hold here: the detector can pick another face (or lose one) after a small change, so the ratio compares two different faces (measured: a
                # two-face photograph read 0.84 for every strength). A face that gets too sharp is still refused by the ceiling below, and polish never blurs a face.
                nothing_worse = min(local_gain, global_gain, frame_gain) >= tolerance
            else:
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
        if operation == "detail_restoration":
            gain = structure_energy(candidate) / max(structure_energy(parent), 1e-6)
            compression_better = before.compression["status"] == "heavy" and after.compression["status"] != "heavy"
            # A photograph too small for the blur analysis has no retention (None) before or after: that is "no evidence", never an error that fails the whole enhancement.
            retention_before, retention_after = before.blur.get("retention"), after.blur.get("retention")
            retention_better = retention_before is not None and retention_after is not None and retention_before - retention_after >= 0.02
            blur_better = (before.blur["status"] != "none" and after.blur["status"] == "none") or retention_better
            face = after.main_face
            face_ok = face is None or face.detail is None or face.detail <= limits.face_max_detail
            # The final layer (``refine``, ADAPTIVE_ALWAYS_ENHANCE): the light-photo model is applied to every photograph, so there is no defect to resolve. It is kept unless it smooths the
            # photograph by more than ADAPTIVE_REFINE_MIN_GAIN allows (the structure energy counts noise and JPEG blocks too, so on a brightened dark photograph the model's cleaning reads as a
            # loss), it adds an invented amount (> detail_max_gain), it changes nothing visible (ADAPTIVE_REFINE_MIN_CHANGE grey levels of 255) or a face grows past the ceiling or by
            # more than the polish allows (a sharp face is already above 260); every other gate (identity, face geometry, halo, clipping, colour) still applies.
            minimum, visible, change = limits.detail_min_gain, True, None
            if refine:
                face_before = before.main_face.detail if before.main_face is not None and before.main_face.detail is not None else None
                face_ok = face is None or face.detail is None or face.detail <= max(limits.face_max_detail, (face_before or 0.0) * limits.polish_face_growth_max)
                minimum = float(settings.ADAPTIVE_REFINE_MIN_GAIN)
                change = float(np.abs(candidate[::4, ::4].astype(np.int16) - parent[::4, ::4].astype(np.int16)).mean())
                visible = change >= float(settings.ADAPTIVE_REFINE_MIN_CHANGE)
            gate("improves_defect", minimum <= gain <= limits.detail_max_gain and visible and (True if refine else (compression_better or blur_better)) and face_ok,
                 {"structure_gain": round(gain, 3), "compression_before": before.compression["status"], "compression_after": after.compression["status"],
                  "blur_before": before.blur["status"], "blur_after": after.blur["status"], "face_detail_after": None if face is None else face.detail,
                  **({"mean_change": round(change, 2)} if change is not None else {})},
                 "no_added_detail_or_invented_detail")
            return float(min(1.0, np.clip((gain - 1.0) / 0.2, 0, 1) + (0.25 if compression_better else 0.0) + (0.25 if blur_better else 0.0)))
        if operation == "colourization":
            lab = cv2.cvtColor(candidate, cv2.COLOR_BGR2LAB).astype(np.float32)
            mean = float(np.hypot(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0).mean())
            gate("improves_defect", mean >= limits.colourize_min_mean_chroma, {"mean_chroma": round(mean, 1)}, "no_colour_added")
            return float(np.clip(mean / 10.0, 0, 1))
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
