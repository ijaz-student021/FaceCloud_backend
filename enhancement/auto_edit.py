"""Auto for the gallery editor: read what kind of photograph this is, then choose the sliders for it.

The earlier Auto (``controls.recommend_local_adjustments``) fixes clear technical faults only and never touches the exposure, because "whether a dark or bright photo is meant to be so
cannot be told from its pixels". That is true of the mean brightness and false of the histogram: a photograph with a large black area and a lit subject (a portrait on a black background) is
dark on purpose, and a photograph that is dark everywhere is under-exposed. This module tells them apart from the measurements below, solves the sliders for the target with the renderer's own
formulas (``tone.py``) instead of guessing, and falls back to the earlier recipe for every photograph it has no reason to treat differently, so an ordinary photograph gets exactly what it got.

Measurements are in the renderer's units: CIE L on the 0-255 scale and a/b of float LAB, over a copy reduced to 512 px.

Natures:

* ``low_key``: at least half of the pixels are near black (L below 40), there is a lit subject (at least 5 % of the pixels above L 60, the 95th percentile above 120) that sits on a dark SURROUND (at least half of the outer 15 % of the frame is near black). The automatic enhancement
  tends to lift the black floor of such a photograph (measured on the owner's portrait: the dark mass went from L 8.7 in the upload to 23.7, a milky background). Auto compares the floor with the
  unenhanced photograph (``baseline``): only a floor the enhancement LIFTED is put back (``brightness``: the way down divided by the renderer's 0.35, to 15 % of the original floor, as the owner did),
  so a photograph that is naturally dark grey is not crushed to black; without the unenhanced photograph the exposure is left alone. The darkest tones are deepened (``shadows``), the subject
  is brought to a target (``contrast``) and, when the lit part is coloured and warm, enriched (``saturation``, ``warmth``).
* ``balanced``: anything else: the earlier recipe, unchanged, which never changes the exposure or the colour. A photograph that is dark everywhere (nothing lit) is NOT lifted and a bright one is NOT darkened:
  there is no evidence yet of what the owner wants for them (``intent`` "brighten" still lifts).

The constants marked "owner" were read from one edit the owner made by hand on one photograph (brightness -65, contrast 18, shadows -18, highlights 1, saturation 14, warmth 9); see
docs/evaluation/2026-11-04-auto-edit.md for what this recipe returns for that photograph and what has not been checked. Only tone sliders are chosen: the model-backed ones stay manual (their
measured share of that edit was 0.8 of 255).
"""
import cv2
import numpy as np

from .controls import CONTROL_NAMES, recommend_local_adjustments

# Where this recipe may put each slider. The AUTO range in controls.py (brightness +-16, shadows 0-18, ...) is too small for it and still bounds the cloud advisor.
ADAPTIVE_RANGE = {
    "brightness": (-70.0, 30.0), "contrast": (-10.0, 25.0), "shadows": (-25.0, 20.0), "highlights": (-25.0, 0.0), "saturation": (-12.0, 20.0),
    "warmth": (-10.0, 12.0), "sharpness": (0.0, 20.0), "lowlight": (0.0, 0.0), "denoise": (0.0, 0.0), "deblur": (0.0, 0.0), "face_restore": (0.0, 0.0),
}

WORK_EDGE = 512
DARK_LEVEL = 40.0                 # L below this is "near black"
LIT_LEVEL = 60.0                  # L above this is the lit part of the photograph
BRIGHTNESS_K = 0.35               # tone.py: brightness adds value * 0.35 to L
CONTRAST_K = 0.005                # tone.py: contrast scales L about the median by 1 + value * 0.005
SHADOWS_K = 0.45                  # tone.py: shadows adds value * 0.45 * (1 - L/255)^2

LOW_KEY_DARK_SHARE = 0.50
LOW_KEY_MIN_P95 = 120.0           # a lit subject that is properly exposed: the owner's portrait is at 136 in the upload; a dim night photograph of a person (95th percentile 97) is under-exposed, not low-key
LOW_KEY_MIN_LIT_SHARE = 0.05
LOW_KEY_MIN_BORDER_DARK = 0.5         # the dark part must be the SURROUND of the frame (a black backdrop); a dark subject against a bright background is backlit (the backlit portrait is at 0.33, the owner's at 0.91)
LOW_KEY_MAX_ORIGINAL_FLOOR = 12.0     # a BLACK background: the owner's portrait is at 8.7 in the upload; a night photograph whose dark part is 15 and shows a wall and trees is not
LOW_KEY_BLACK_RATIO = 0.15        # owner: the dark mass's median after the edit, over its median in the upload (1.2 / 8.7 = 0.14)
LOW_KEY_DEADBAND = 3.0            # a floor the enhancement raised by no more than this is not touched
LOW_KEY_SUBJECT = 155.0           # owner: the 95th percentile after the edit (152.6 by hand)
LOW_KEY_SHADOWS_PER_BRIGHTNESS = 0.28   # owner: shadows -18 with brightness -65
LOW_KEY_SATURATION = 14.0         # owner, at lit-pixel chroma 40 and above, scaled down below it
LOW_KEY_WARMTH = 9.0              # owner, at lit-pixel warmth 30 and above, scaled down below it



def measure_scene(bgr):
    """What the recipe reads from a photograph (L on the 0-255 scale, a/b of float LAB), computed on a copy of at most ``WORK_EDGE`` px."""
    height, width = bgr.shape[:2]
    if max(height, width) > WORK_EDGE:
        scale = WORK_EDGE / float(max(height, width))
        bgr = cv2.resize(bgr, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0] * (255.0 / 100.0)
    chroma = np.hypot(lab[:, :, 1], lab[:, :, 2])
    dark, lit = lightness < DARK_LEVEL, lightness > LIT_LEVEL
    edge = max(1, int(round(0.15 * min(lightness.shape))))
    frame = np.zeros(lightness.shape, bool)
    frame[:edge, :] = frame[-edge:, :] = frame[:, :edge] = frame[:, -edge:] = True
    p01, p05, p25, p50, p75, p95, p99 = (float(value) for value in np.percentile(lightness, [1, 5, 25, 50, 75, 95, 99]))
    return {
        "p01": p01, "p05": p05, "p25": p25, "p50": p50, "p75": p75, "p95": p95, "p99": p99, "mean": float(lightness.mean()),
        "dark_share": float(dark.mean()), "border_dark": float(dark[frame].mean()), "dark_median": float(np.median(lightness[dark])) if dark.any() else None,
        "lit_share": float(lit.mean()), "chroma_lit": float(chroma[lit].mean()) if lit.any() else 0.0,
        "warmth_lit": float(lab[:, :, 2][lit].mean()) if lit.any() else 0.0,
    }


def classify_scene(scene):
    if scene["dark_share"] >= LOW_KEY_DARK_SHARE and scene["p95"] >= LOW_KEY_MIN_P95 and scene["lit_share"] >= LOW_KEY_MIN_LIT_SHARE and scene["border_dark"] >= LOW_KEY_MIN_BORDER_DARK:
        return "low_key"
    return "balanced"


def clamp_to_adaptive_range(controls):
    return {name: round(min(max(float(controls.get(name, 0.0)), ADAPTIVE_RANGE[name][0]), ADAPTIVE_RANGE[name][1]), 2) for name in CONTROL_NAMES}


def _low_key(scene, baseline_scene, controls, rationale):
    floor = scene["dark_median"]
    original = None if baseline_scene is None else baseline_scene["dark_median"]
    brightness = 0.0
    if original is None:
        rationale.append("A dark photograph with a lit subject; the unenhanced photograph was not available to tell whether its blacks were lifted, so the exposure was left alone.")
    elif original > LOW_KEY_MAX_ORIGINAL_FLOOR:
        rationale.append("A dark photograph with a lit subject, but its dark part is not black in the unenhanced photograph (it shows detail), so the exposure was left alone.")
    elif floor - original > LOW_KEY_DEADBAND:
        target = LOW_KEY_BLACK_RATIO * original
        brightness = -min((floor - target) / BRIGHTNESS_K, -ADAPTIVE_RANGE["brightness"][0])
        controls["shadows"] = brightness * LOW_KEY_SHADOWS_PER_BRIGHTNESS
        rationale.append(f"A dark photograph with a lit subject, so it is meant to be dark: the enhancement had lifted its black level (L {original:.0f} to {floor:.0f}); brought it back to about {target:.1f}.")
    else:
        rationale.append("A dark photograph with a lit subject; its blacks are as photographed, so the exposure was left alone.")
    controls["brightness"] = brightness
    # The subject: where its 95th percentile will be after the shift, and the contrast (about the median, which in a low-key photograph is the black floor) that brings it to the target.
    shifted_subject = scene["p95"] + BRIGHTNESS_K * brightness
    pivot = max(0.0, scene["p50"] + BRIGHTNESS_K * brightness)
    if shifted_subject > pivot + 1.0 and shifted_subject < LOW_KEY_SUBJECT:
        controls["contrast"] = ((LOW_KEY_SUBJECT - shifted_subject) / (shifted_subject - pivot)) / CONTRAST_K
        rationale.append("Added contrast so the subject stands out from the black.")
    if scene["chroma_lit"] >= 10.0:
        controls["saturation"] = LOW_KEY_SATURATION * min(1.0, scene["chroma_lit"] / 40.0)
        if scene["warmth_lit"] > 0:
            controls["warmth"] = LOW_KEY_WARMTH * min(1.0, scene["warmth_lit"] / 30.0)
        rationale.append("Enriched the colour of the lit part.")


def _baseline_scene(baseline):
    """The measurements of the unenhanced photograph, from an image or a loader, or ``None`` when there is none (a loader that fails is the same as no loader)."""
    try:
        image = baseline() if callable(baseline) else baseline
    except Exception:
        return None
    return None if image is None or getattr(image, "size", 0) == 0 else measure_scene(image)


def recommend_adaptive_adjustments(source, quality, *, blockiness_detected=False, baseline=None):
    """``{"action", "controls", "rationale", "caution_flags", "intent", "nature", "scene"}`` for the editor's Auto (``intent`` "auto").

    ``source`` is the photograph being edited (8-bit BGR) and ``quality`` is ``analyze_image_quality`` of it. ``baseline`` is the unenhanced photograph, or a function that reads it (called only
    for a low-key photograph). The earlier recipe supplies the technical-fault controls (clipped highlights, flat contrast,
    softness); the nature of the photograph decides the exposure, the contrast and the colour of a low-key subject.
    """
    base = recommend_local_adjustments(quality, "auto", blockiness_detected=blockiness_detected)
    controls = dict(base["controls"])
    rationale, flags = list(base["rationale"]), list(base["caution_flags"])
    scene = measure_scene(source)
    nature = classify_scene(scene)
    if nature == "low_key":
        if "dark_photo_not_lifted" in flags:
            flags.remove("dark_photo_not_lifted")
        rationale = [line for line in rationale if "was not brightened" not in line]
        _low_key(scene, _baseline_scene(baseline), controls, rationale)
    controls = clamp_to_adaptive_range(controls)
    action = "apply" if any(controls.values()) else "no_change"
    if action == "apply":      # the earlier recipe's "nothing was changed" and "was not brightened" no longer describe what is returned
        rationale = [line for line in rationale if "nothing was changed" not in line and "was not brightened" not in line]
    elif not rationale:
        rationale = ["No clear technical fault was measured; nothing was changed."]
    return {"action": action, "controls": controls, "rationale": rationale, "caution_flags": flags, "intent": "auto", "nature": nature,
            "scene": {key: (None if value is None else round(value, 2)) for key, value in scene.items()}}


def is_black_backdrop_low_key(image):
    """Whether ``image`` (the UPLOAD) is a low-key photograph on a BLACK background: a lit, properly exposed subject (95th percentile at least ``LOW_KEY_MIN_P95``) and a near-black dark part
    (median at most ``LOW_KEY_MAX_ORIGINAL_FLOOR``). The automatic enhancement does not brighten such a photograph: the low-light step lifts the black floor into a milky grey (measured on the
    owner's portrait, L 8.7 to 23.4 or 23.7) and the owner pulled it back by hand. A night photograph of a person (dark part 15 showing a wall, subject at 97) is under-exposed, not low-key.
    """
    scene = measure_scene(image)
    return classify_scene(scene) == "low_key" and scene["dark_median"] is not None and scene["dark_median"] <= LOW_KEY_MAX_ORIGINAL_FLOOR
