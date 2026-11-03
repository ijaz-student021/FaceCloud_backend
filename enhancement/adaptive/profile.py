"""What a style and a strength mean to the adaptive engine.

Until this module the adaptive engine ignored both: "Natural", "Dynamic, more punch" and "AI Enhance, strongest restoration" (and Light, Moderate, Strong) all produced the
same result, so choosing the strongest setting on a photograph could change nothing at all. A ``Profile`` says what a choice changes. It only changes **when an operation
is tried and how strongly**; every safety gate of the validator (data consistency, colour, artifacts, identity, "did it improve") judges every candidate exactly as before.

* ``natural`` (the default, and what automatic enhancement after upload uses): keeps the photograph as it is. Operations the photograph's own defects call for, and a gentle
  polish when nothing is wrong (if ``ADAPTIVE_POLISH_ENABLED``). A black-and-white photograph stays black and white.
* ``dynamic``: the same, with the polish at its stronger "punch" setting, also for a deliberately dark photograph (polish never lifts shadows), and a black-and-white
  photograph is colourized (DDColor, if ``DDCOLOR_ENABLED``).
* ``ai_max``: everything ``dynamic`` does, a stronger polish (x1.25), a dark scene whose subject is visible is lifted, noise is treated from the "low" level, and a photograph
  whose long edge is under 1280 px is enlarged.

The strength scales the polish and decides how far the engine goes: ``low`` (Light) uses gentler blends and 0.6x polish; ``auto`` and ``medium`` leave the style as it is;
``high`` (Strong) polishes 1.4x, treats low noise as noise, lifts a dark scene whose subject is visible (a "dark scene" is otherwise left as photographed) and enlarges
a photograph under 960 px. A colourization is never scaled (it is one prediction, already damped).
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Profile:
    style: str = "natural"
    level: str = "auto"
    colourize: bool = False             # a black-and-white photograph becomes colour: a creative change, so never for "natural"
    polish_style: str | None = "natural"    # a key of operations.POLISH_STRENGTH; None = no polish
    polish_scale: float = 1.0           # multiplies the polish shares (the router caps the result)
    polish_dark_scenes: bool = False    # a deliberately dark photograph may be polished (contrast and sharpness only; polish never lifts shadows)
    lift_dark_scenes: bool = True       # a dark scene with a visible subject is treated as low light (the owner: "the darkness should be removed"; the validator's gates still decide)
    treat_low_noise: bool = False       # noise at the "low" level is denoised too
    upscale_below_edge: int = 0         # a photograph whose long edge is under this is enlarged (0: only when the analyzer calls it low resolution)
    variant_blends: tuple = (1.0, 0.65)     # the blends tried for a lift or a denoise, strongest first

    def describe(self):
        return {"style": self.style, "level": self.level, "colourize": self.colourize, "polish_style": self.polish_style, "polish_scale": self.polish_scale,
                "polish_dark_scenes": self.polish_dark_scenes, "lift_dark_scenes": self.lift_dark_scenes, "treat_low_noise": self.treat_low_noise,
                "upscale_below_edge": self.upscale_below_edge, "variant_blends": list(self.variant_blends)}


DEFAULT = Profile()

_STYLES = {
    "natural": DEFAULT,
    "dynamic": Profile(style="dynamic", colourize=True, polish_style="dynamic", polish_dark_scenes=True),
    "ai_max": Profile(style="ai_max", colourize=True, polish_style="dynamic", polish_scale=1.25, polish_dark_scenes=True, lift_dark_scenes=True, treat_low_noise=True,
                      upscale_below_edge=1280),
}


def profile_for(style, level="auto"):
    """The profile for a style (``natural``, ``dynamic``, ``ai_max``, or the enum that carries one) and a strength (``auto``, ``low``, ``medium``, ``high``)."""
    style = str(getattr(style, "value", style) or "natural")
    level = str(getattr(level, "value", level) or "auto")
    profile = replace(_STYLES.get(style, DEFAULT), style=style if style in _STYLES else "natural", level=level)
    if level == "low":
        profile = replace(profile, polish_scale=round(profile.polish_scale * 0.6, 4), variant_blends=(0.65, 0.4))
    elif level == "high":
        profile = replace(profile, polish_scale=round(profile.polish_scale * 1.4, 4), lift_dark_scenes=True, treat_low_noise=True,
                          upscale_below_edge=max(profile.upscale_below_edge, 960))
    return profile
