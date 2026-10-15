"""The editing controls: one definition of names, ranges, neutral values, mapping
to Pillow factors, validation and the local (no-cloud) adjustment recipe.

Everything that accepts, documents, renders or suggests adjustments uses this
module, so the editor, the API schema, the local recipe and (later) the cloud
advisor cannot disagree about what a control means.

Signed controls and Pillow factors
----------------------------------
Every control is a signed number whose neutral value is 0 (``intensity``, the
blend with the unenhanced photo, is 0-100 with 100 neutral). Pillow's
``ImageEnhance`` uses *factors* whose neutral value is 1.0. The mapping is
linear, ``factor = 1 + k * value``, with k below. The renderer works in CIE
LAB (see ``tone.py``), not on RGB as Pillow does, so a Pillow factor is an
approximation of a control, except contrast and sharpness, which are the same
operator on the L channel (a test measures the differences). Shadows,
highlights, warmth and the model-backed sliders have no Pillow equivalent.

Manual and automatic ranges are separate. Manual input may use the whole
range; the local recipe and the cloud advisor are held to the much smaller
AUTO range.
"""
import math
from dataclasses import dataclass

from rest_framework import serializers

RENDERER_VERSION = "local_tone_renderer_v2"
RENDERER_POLICY_NAME = "non_destructive_gallery_editor_v1"  # kept for earlier clients

NEUTRAL_VALUE = 0.0
INTENSITY_NEUTRAL = 100.0
INTENTS = ("auto", "preserve", "brighten")


@dataclass(frozen=True)
class Control:
    name: str
    label: str
    manual: tuple          # (minimum, maximum) a person may set
    auto: tuple            # (minimum, maximum) the local recipe and the advisor may set
    description: str       # exact renderer semantics, as given to the advisor
    pillow_k: float = None  # factor = 1 + pillow_k * value; None: no Pillow equivalent
    pillow_enhancer: str = ""
    model: str = ""         # a model-backed slider (enhancement/model_controls.py); empty for a tone control
    neutral: float = NEUTRAL_VALUE
    recommended: tuple = (-50.0, 50.0)


CONTROLS = {
    control.name: control
    for control in (
        Control(
            "brightness", "Brightness", (-100.0, 100.0), (-16.0, 16.0),
            "Adds value * 0.35 to the LAB lightness L (0-255 scale) of every pixel.",
            pillow_k=0.35 / 128.0, pillow_enhancer="Brightness",
        ),
        Control(
            "contrast", "Contrast", (-100.0, 100.0), (-8.0, 20.0),
            "Scales lightness around its median by 1 + value * 0.005.",
            pillow_k=0.005, pillow_enhancer="Contrast",
        ),
        Control(
            "shadows", "Shadows", (-100.0, 100.0), (0.0, 18.0),
            "Adds value * 0.45 to lightness, weighted by (1 - L/255)^2: it lifts or deepens the darkest tones.",
        ),
        Control(
            "highlights", "Highlights", (-100.0, 100.0), (-18.0, 0.0),
            "Adds value * 0.40 to lightness, weighted by (L/255)^2: it recovers or pushes the brightest tones.",
        ),
        Control(
            "saturation", "Saturation", (-100.0, 100.0), (-8.0, 8.0),
            "Scales the LAB colour channels a and b by 1 + value * 0.004.",
            pillow_k=0.004, pillow_enhancer="Color",
        ),
        Control(
            "warmth", "Warmth", (-100.0, 100.0), (-8.0, 8.0),
            "Adds value * 0.30 to LAB b (positive is warmer, more yellow).",
        ),
        Control(
            "sharpness", "Sharpness", (-100.0, 100.0), (0.0, 20.0),
            "Unsharp mask on lightness: L + (value / 100) * (L - blur(L)). Negative values soften. "
            "The blur radius is proportional to the image's longer edge (sigma 0.68 px at 1024 px, never "
            "below 0.5 px), so a preview and a full-resolution save look alike.",
            pillow_k=0.01, pillow_enhancer="Sharpness",
        ),
        Control(
            "lowlight", "Low-light enhancement", (0.0, 100.0), (0.0, 0.0),
            "Blends in a learned low-light result by value / 100: Retinexformer when it is switched on, otherwise "
            "Zero-DCE, otherwise an OpenCV gamma lift. Runs before every other control. 0 is off. Manual only: the "
            "local recipe and the advisor never set it.",
            model="lowlight", recommended=(0.0, 60.0),
        ),
        Control(
            "denoise", "Noise reduction", (0.0, 100.0), (0.0, 0.0),
            "Blends in a denoised copy by value / 100: NAFNet-SIDD (real sensor noise) when it is switched on, "
            "otherwise an edge-preserving OpenCV bilateral filter. Runs after low light. 0 is off. Manual only.",
            model="denoise", recommended=(0.0, 60.0),
        ),
        Control(
            "deblur", "Deblur", (0.0, 100.0), (0.0, 0.0),
            "Blends in a deblurred copy by value / 100: NAFNet-GoPro (motion blur) when it is switched on, "
            "otherwise an OpenCV unsharp mask. It cannot restore detail that was never captured. 0 is off. Manual only.",
            model="deblur", recommended=(0.0, 50.0),
        ),
        Control(
            "face_restore", "Face restoration", (0.0, 100.0), (0.0, 0.0),
            "Blends in CodeFormer's restored faces by value / 100, only where a face is found. It invents plausible "
            "detail, so it is checked by the pipeline's identity gate when it runs there. Needs CodeFormer to be switched "
            "on; with no engine it is reported as not applied. 0 is off. Manual only.",
            model="face_restore", recommended=(0.0, 50.0),
        ),
    )
}
CONTROL_NAMES = tuple(CONTROLS)
INTENSITY = Control(
    "intensity", "Enhancement intensity", (0.0, 100.0), (0.0, 100.0),
    "Blends the result with the unenhanced (preprocessed) photo: 100 is the full result, 0 the unenhanced photo.",
    neutral=INTENSITY_NEUTRAL, recommended=(0.0, 100.0),
)


# -- validation ---------------------------------------------------------------

class StrictFloatField(serializers.FloatField):
    """A finite number: booleans, NaN and infinity are refused, not coerced."""

    default_error_messages = {
        "not_finite": "Enter a finite number.",
        "boolean": "Enter a number, not true or false.",
    }

    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail("boolean")
        value = super().to_internal_value(data)
        if not math.isfinite(value):
            self.fail("not_finite")
        return value


def _manual_field(control):
    low, high = control.manual
    return StrictFloatField(
        min_value=low, max_value=high, default=control.neutral,
        help_text=f"{control.description} Range {low:g} to {high:g}; neutral {control.neutral:g}.",
    )


class ControlsSerializer(serializers.Serializer):
    """Manual control values. Unknown names are an error: a misspelt control must not be ignored."""

    brightness = _manual_field(CONTROLS["brightness"])
    contrast = _manual_field(CONTROLS["contrast"])
    shadows = _manual_field(CONTROLS["shadows"])
    saturation = _manual_field(CONTROLS["saturation"])
    highlights = _manual_field(CONTROLS["highlights"])
    warmth = _manual_field(CONTROLS["warmth"])
    sharpness = _manual_field(CONTROLS["sharpness"])
    lowlight = _manual_field(CONTROLS["lowlight"])
    denoise = _manual_field(CONTROLS["denoise"])
    deblur = _manual_field(CONTROLS["deblur"])
    face_restore = _manual_field(CONTROLS["face_restore"])
    intensity = _manual_field(INTENSITY)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({"non_field_errors": ["Send an object of control values."]})
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({
                name: ["This control is not supported. Use: " + ", ".join(self.fields) + "."] for name in unknown
            })
        return super().to_internal_value(data)


def validate_controls(raw):
    """``{control: float}`` for a manual request, or a DRF ValidationError with one message per field."""
    serializer = ControlsSerializer(data=raw if raw is not None else {})
    serializer.is_valid(raise_exception=True)
    return {name: float(value) for name, value in serializer.validated_data.items()}


def neutral_settings():
    """What "reset" means: every control neutral, intensity at 100 (a reset edit changes nothing)."""
    return {**{name: CONTROLS[name].neutral for name in CONTROL_NAMES}, "intensity": INTENSITY_NEUTRAL}


def is_neutral(controls):
    return all(float(controls.get(name, 0.0)) == 0.0 for name in CONTROL_NAMES)


def clamp_to_auto_range(controls):
    """Automatic suggestions are held to the automatic range, never the manual one."""
    return {
        name: round(min(max(float(controls.get(name, 0.0)), CONTROLS[name].auto[0]), CONTROLS[name].auto[1]), 2)
        for name in CONTROL_NAMES
    }


def pillow_factors(controls):
    """Approximate Pillow ``ImageEnhance`` factors for the controls that have one (neutral 1.0).

    ``None`` for the controls with no Pillow equivalent (shadows, highlights, warmth and the model-backed sliders).
    """
    return {
        control.name: (1.0 + control.pillow_k * float(controls.get(control.name, 0.0)) if control.pillow_k else None)
        for control in CONTROLS.values()
    }


# -- the local recipe ---------------------------------------------------------

def recommend_local_adjustments(quality, intent="auto", *, blockiness_detected=False):
    """A conservative recipe from measurements alone; no network, no model.

    ``quality`` is ``analyze_image_quality`` output. The outcome is
    ``{"action": "apply" | "no_change", "controls", "rationale", "caution_flags"}``.

    * ``auto`` (default): fixes clear technical faults only: blown highlights,
      flat contrast, measured softness. It never lifts or darkens the photo's
      overall exposure, because whether a dark or bright photo is meant to be
      so cannot be told from its pixels.
    * ``brighten``: the person asked for the lift, so a dark photo is lifted.
    * ``preserve``: tonal controls stay at zero; only measured softness is addressed.
    """
    if intent not in INTENTS:
        raise ValueError(f"intent must be one of {INTENTS}")
    controls = {name: 0.0 for name in CONTROL_NAMES}
    rationale, flags = [], []
    defects = set(quality.get("defects", ()))
    percentiles = quality["luminance_percentiles"]

    if intent != "preserve":
        if "overexposed_highlights" in defects:
            controls["highlights"] = -max(0.0, (percentiles["p95"] - 224.0) * 0.45)
            rationale.append("Highlights are clipped; recovered a little.")
        if "low_contrast" in defects:
            controls["contrast"] = max(0.0, (48.0 - quality["contrast_stddev"]) * 0.65)
            rationale.append("The photo is flat; added a little contrast.")
    if intent == "brighten":
        controls["brightness"] = max(0.0, (135.0 - quality["mean_luminance"]) * 0.22)
        controls["shadows"] = max(0.0, (68.0 - percentiles["p25"]) * 0.35)
        if controls["brightness"] or controls["shadows"]:
            rationale.append("You asked to brighten; lifted the darker tones.")
    elif quality["mean_luminance"] < 115 and intent == "auto":
        flags.append("dark_photo_not_lifted")
        rationale.append("The photo is dark; it was not brightened because that may be intended. Choose brighten to lift it.")

    blur = (quality.get("blur") or {}).get("verdict")
    if blur in ("mild", "severe"):
        if "visible_noise" in defects:
            flags.append("noise_present_not_sharpened")
        elif blockiness_detected:
            flags.append("compression_artifacts_not_sharpened")
        else:
            controls["sharpness"] = 12.0 if blur == "mild" else 8.0
            rationale.append("The photo is slightly soft; added light sharpening." if blur == "mild"
                             else "The photo is blurred; sharpening cannot restore lost detail, so only a little was added.")
    if blur == "severe":
        flags.append("blur_not_recoverable")

    controls = clamp_to_auto_range(controls)
    action = "apply" if any(controls.values()) else "no_change"
    if action == "no_change":
        rationale = rationale or ["No clear technical fault was measured; nothing was changed."]
    return {"action": action, "controls": controls, "rationale": rationale, "caution_flags": flags, "intent": intent}


# -- the contract, as served to clients and (in Phase C) to the advisor --------

def contract():
    from .model_controls import engine_status

    engines = engine_status()
    controls = {}
    for control in (*CONTROLS.values(), INTENSITY):
        controls[control.name] = {
            "label": control.label,
            "minimum": control.manual[0], "maximum": control.manual[1],
            "step": 1, "default": control.neutral, "neutral": control.neutral,
            "recommended_minimum": control.recommended[0], "recommended_maximum": control.recommended[1],
            "auto_minimum": control.auto[0], "auto_maximum": control.auto[1],
            "description": control.description,
            "pillow_enhancer": control.pillow_enhancer or None,
            "pillow_factor_per_unit": control.pillow_k,
            # Model-backed sliders say what will run, so a client can label or disable them.
            "model_backed": bool(control.model),
            "engine": engines[control.model]["engine"] if control.model else None,
            "available": engines[control.model]["available"] if control.model else True,
            "uses_model": engines[control.model]["uses_model"] if control.model else False,
        }
    return controls
