"""AdaptiveEnhancementRouter: which operation, if any, is worth trying next, for the photograph as it is *now*.

It is consulted again after every accepted operation (a lift reveals noise; a denoise changes what blur looks like), so
the plan is never a fixed pipeline and a clean photograph runs nothing at all. For each operation it returns either a
``Step`` (the engine to use and the variants to try) or a recorded reason for skipping it, so "why was NAFNet not run?"
always has an answer.

Rules, each tied to a measured condition in the ``QualityReport``:

* **Low light** only when the *subject* is under-exposed (``low_light_subject``) or a face-less frame is dark and has no
  bright lights (``low_light``). A dark scene with a visible face is left alone.
* **Denoising** for moderate or high noise; the model only when it is enabled, otherwise the classical filter.
* **Deblurring** for blur that the analyzer typed as motion (or mixed), or for a *strongly blurred main face* whatever the frame
  reads (a blurred subject in front of a sharp background; a large defocus, where the model measurably helps), never for the
  frame's own defocus or a soft face, and not on a photograph with heavy compression artifacts (the model would sharpen the blocks).
* **Face restoration** only for a face whose quality is "poor" and which is large enough to restore (a smaller face is
  not reconstructed, however damaged), never for a good one; variants start at high fidelity (little change).
* **Super-resolution** only when the photograph (or its face) is genuinely small, at the smallest useful scale.
"""
from dataclasses import dataclass, field

from . import operations as ops
from . import strength


@dataclass
class Step:
    operation: str
    engines: list           # engines to try, in order of preference
    variants: list          # [{"name": ..., ...}] what to propose from the first engine that runs
    reason: str
    measure: dict = field(default_factory=dict)   # what was measured and which strength it produced (the automatic path)


@dataclass
class Decision:
    step: Step | None
    skipped: list = field(default_factory=list)   # [{"operation": ..., "reason": ...}]


class AdaptiveEnhancementRouter:
    def __init__(self, config):
        self.config = config
        self._strengths = None

    @property
    def strengths(self):
        if self._strengths is None:
            self._strengths = strength.StrengthConfig.from_settings(getattr(self.config, "style", "natural"))
        return self._strengths

    def _blend_variants(self, blend, measure):
        """Two candidates on the automatic path: the strength measured for this photograph, and a gentler one."""
        gentle = round(max(blend * 0.7, 0.3), 3)
        variants = [{"name": f"blend {blend}", "blend": blend, "strength": blend}]
        if gentle < blend:
            variants.append({"name": f"blend {gentle}", "blend": gentle, "strength": gentle})
        return variants, measure

    def next_step(self, report, done):
        """The next operation to try given the current ``report`` and the operations already attempted."""
        skipped = []
        for operation in ops.ORDER:
            if operation in done:
                continue
            step, reason = self._consider(operation, report)
            if step is not None:
                return Decision(step, skipped)
            if reason:
                skipped.append({"operation": operation, "reason": reason})
        return Decision(None, skipped)

    # -- one operation ----------------------------------------------------------------------------------------
    def _consider(self, operation, report):
        engines = ops.enabled_engines(operation)
        main = report.main_face
        if operation == ops.LOW_LIGHT:
            if report.exposure["status"] == "dark_scene":
                return None, "The scene is dark but the subject is visible: left as photographed."
            if report.exposure["status"] not in ("low_light", "low_light_subject"):
                return None, ""
            reason = f"Exposure: {report.exposure['status']} (judged on the {report.exposure['judged_on']})."
            if self.config.situational:
                variants, measure = self._blend_variants(*strength.lowlight(report, self.strengths))
                for variant in variants:
                    variant["gamma"] = measure["classical_gamma"]
                return Step(operation, engines, variants, reason, measure), ""
            return Step(operation, engines, [{"name": "blend 1.0", "blend": 1.0}, {"name": "blend 0.65", "blend": 0.65}], reason), ""
        if operation == ops.DENOISE:
            if report.noise["status"] not in ("moderate", "high"):
                return None, ""
            variants = [{"name": "blend 1.0", "blend": 1.0}, {"name": "blend 0.65", "blend": 0.65}]
            reason = f"Noise: {report.noise['status']} (estimate {report.noise['estimate']})."
            if self.config.situational:
                variants, measure = self._blend_variants(*strength.denoise(report, self.strengths))
                return Step(operation, engines, variants, reason, measure), ""
            return Step(operation, engines, variants, reason), ""
        if operation == ops.DEBLUR:
            # The main face's own blur decides first: a blurred subject in front of a sharp background reads as "no blur" for the frame,
            # and a large defocus is where the motion-deblurring model measurably helps (a small one it hurts, so only "high" counts).
            face_blurred = main is not None and report.face_blur.get("status") == "high"
            if face_blurred:
                if report.compression["status"] == "heavy":
                    return None, "Heavy compression artifacts: deblurring would sharpen the blocks."
                if not engines:
                    return None, "No deblurring model is switched on."
                reason = f"The main face is blurred (face detail {report.face_blur['detail']})."
                if self.config.situational:
                    variants, measure = self._blend_variants(*strength.deblur(report, self.strengths))
                    return Step(operation, engines, variants, reason, measure), ""
                return Step(operation, engines, [{"name": "blend 1.0", "blend": 1.0}, {"name": "blend 0.6", "blend": 0.6}], reason), ""
            if report.blur["status"] not in ("low", "high"):
                return None, ""
            if report.blur["status"] == "low" and report.blur["type"] != "motion":
                return None, ""  # mild blur without a clear motion signature is left alone
            if report.blur["type"] == "defocus":
                # No defocus-capable deblurring model is in use: Restormer's single-image defocus model (MIT code) was measured and took about
                # ten minutes for one 640 px crop on an Apple GPU without improving it (docs/evaluation/2026-10-17-restoration-chains.md), so a
                # defocused *face* is handled by face restoration and the frame's own defocus is left as photographed.
                return None, ("The blur is defocus; the motion-deblurring model is not applied to it and cannot recover it. "
                              "A defocused face is restored by face restoration when that is switched on.")
            if report.blur["type"] == "unknown":
                return None, "The blur type could not be determined."
            if report.compression["status"] == "heavy":
                return None, "Heavy compression artifacts: deblurring would sharpen the blocks."
            if not engines:
                return None, "No deblurring model is switched on."
            reason = f"Motion blur (direction ratio {report.blur['direction_ratio']})."
            if self.config.situational:
                variants, measure = self._blend_variants(*strength.deblur(report, self.strengths))
                return Step(operation, engines, variants, reason, measure), ""
            return Step(operation, engines, [{"name": "blend 1.0", "blend": 1.0}, {"name": "blend 0.6", "blend": 0.6}], reason), ""
        if operation == ops.FACE:
            if main is None:
                return None, "No face was found."
            if main.quality == "too_small":
                return None, "The face is too small to restore without inventing it."
            if main.quality != "poor":
                return None, ""
            if not engines:
                return None, "No face-restoration model is switched on."
            if self.config.situational:
                fidelity, measure = strength.face_fidelity(report, self.strengths)
                variants = [{"name": f"fidelity {fidelity}", "fidelity": fidelity, "strength": fidelity},
                            {"name": f"fidelity {min(0.95, round(fidelity + 0.15, 3))}", "fidelity": min(0.95, round(fidelity + 0.15, 3)),
                             "strength": min(0.95, round(fidelity + 0.15, 3))}]
                return Step(operation, engines, variants, "The main face is degraded.", measure), ""
            return Step(operation, engines, [{"name": "fidelity 0.85", "fidelity": 0.85}, {"name": "fidelity 0.65", "fidelity": 0.65}],
                        "The main face is degraded."), ""
        if operation == ops.TONE:
            if not (self.config.situational and self.config.tone_enabled):
                return None, ""
            if report.noise.get("level", 0.0) >= 1.5:
                return None, "The photograph is noisy; boosting detail would amplify the noise."
            value, measure = strength.tone(report, self.strengths)
            gentle = round(value * 0.6, 3)
            variants = [{"name": f"strength {value}", "strength": value}]
            if gentle < value:
                variants.append({"name": f"strength {gentle}", "strength": gentle})
            return Step(operation, engines, variants, "Gentle local contrast and detail, from this photograph's own contrast.", measure), ""
        if operation == ops.UPSCALE and self.config.hd_target_long_edge:
            longest = max(report.width, report.height)
            scale, reached, why = strength.upscale_scale(longest, self.config.hd_target_long_edge, self.config.max_output_edge)
            if not scale:
                return None, ("" if reached else why)
            if not engines:
                return None, "No super-resolution engine is switched on."
            return Step(operation, engines, [{"name": f"x{scale}", "scale": scale, "strength": scale}], f"HD: {why}",
                        {"long_edge": longest, "hd_target_long_edge": self.config.hd_target_long_edge, "scale": scale,
                         "output_long_edge": longest * scale, "reached": reached}), ""
        if operation == ops.UPSCALE:
            small_image = report.resolution["status"] == "low"
            # A small face in an otherwise adequate photograph is not a reason to enlarge (and re-render) the whole photograph:
            # that costs several times the compute and changes pixels nowhere near the face. It stays available behind
            # ADAPTIVE_FACE_UPSCALE_BELOW (0 = off, the default) for deployments that have measured a benefit.
            small_face = (self.config.face_upscale_below > 0 and main is not None and main.quality == "poor"
                          and main.short_edge < self.config.face_upscale_below)
            if not (small_image or small_face):
                if main is not None and main.quality in ("poor", "too_small") and main.short_edge < 96:
                    return None, "A small face does not justify enlarging the whole photograph."
                return None, ""
            if not engines:
                return None, "No super-resolution model is switched on."
            longest = max(report.width, report.height)
            scale = 2 if longest * 2 <= self.config.max_output_edge else 0
            if not scale:
                return None, "Upscaling would exceed the output size limit."
            return Step(operation, engines, [{"name": f"x{scale}", "scale": scale}],
                        "The photograph is small." if small_image else "The face is small."), ""
        return None, ""
