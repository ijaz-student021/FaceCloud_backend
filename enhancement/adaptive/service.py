"""AdaptiveImageEnhancementService: analyse, route, propose candidates, validate, select ONE final image.

    original -> quality analysis -> router -> (candidates for one operation) -> validator -> best accepted or none
                      ^                                                                            |
                      +---------------- re-analyse the selected image, ask the router again --------+

The original is never modified. Every operation proposes a few variants of the current best image; the validator accepts
or rejects each (see validator.py); at most one variant per operation is kept, and the next operation starts from it.
Nothing runs that the router did not ask for, a clean photograph runs nothing, and an operation whose candidates all fail
leaves the image as it was. The final image is always one of the candidates, never a blend nobody validated.

Outcomes (``parameters["adaptive"]["outcome"]``):

* ``enhanced``: at least one candidate was accepted and the analyzer no longer finds a defect it can address;
* ``partially_enhanced``: something was accepted, but a defect remains (defocus blur, a face too small to restore, a
  rejected operation): the summary says which;
* ``original_preserved``: no acceptable candidate (or nothing needed doing), so the output *is* the original pixels.

``FAILED`` is not an outcome here: an exception propagates to the job code, which records the failure and keeps the
original untouched. The engine never reports an unchanged photograph as enhanced, and never claims a defect is gone
because a model ran: it re-measures.

Time and memory are bounded: at most ``max_candidates`` images, a wall-clock budget after which no further operation
starts, and the adapters' own tiling and out-of-memory handling.
"""
import logging
import time
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from django.conf import settings

from enhancement import policy as rendering_policy
from enhancement.models import EnhancementLevel

from . import operations as ops
from .analyzer import ImageQualityAnalyzer
from .candidates import CandidatePool, PoolFull
from .debug import DebugRecorder
from .router import AdaptiveEnhancementRouter
from .validator import FinalImageQualityValidator

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "adaptive_candidate_pipeline_v1"
ENHANCED, PARTIALLY_ENHANCED, ORIGINAL_PRESERVED = "enhanced", "partially_enhanced", "original_preserved"

# Defects the engine can address (or must report as unresolved), as the analyzer names them.
ADDRESSABLE = {"low_light", "visible_noise", "motion_blur", "mixed_blur", "defocus_blur", "low_resolution", "face_poor"}
UNADDRESSABLE_REASONS = {
    "defocus_blur": "Defocus blur cannot be recovered reliably.",
    "face_poor": "A degraded face remains that could not be restored safely.",
    "low_resolution": "The photograph is small and could not be enlarged safely.",
    "visible_noise": "Noise remains.", "low_light": "The subject is still dark.",
    "motion_blur": "Motion blur remains.", "mixed_blur": "Blur remains.",
}


@dataclass
class EngineConfig:
    max_candidates: int = 8
    time_budget_seconds: float = 180.0
    max_output_edge: int = 4096
    face_upscale_below: int = 96
    max_engines_per_step: int = 2

    @classmethod
    def from_settings(cls):
        return cls(
            max_candidates=int(settings.ADAPTIVE_MAX_CANDIDATES), time_budget_seconds=float(settings.ADAPTIVE_TIME_BUDGET_SECONDS),
            max_output_edge=int(settings.ADAPTIVE_MAX_OUTPUT_EDGE), face_upscale_below=int(settings.ADAPTIVE_FACE_UPSCALE_BELOW),
        )


class IdentityChecker:
    """The original's faces embedded once; ``check`` compares any candidate with them."""

    def __init__(self, original, allowed):
        self.reference, self.reason, self.threshold = None, "", None
        if not allowed:
            self.reason = "Biometric processing is not permitted for this account, so identity was not checked."
            return
        from enhancement.identity_guard import IdentityGuardUnavailable, build_identity_reference
        from identities.providers import thresholds_for

        try:
            self.reference, self.reason = build_identity_reference(original)
        except IdentityGuardUnavailable as error:
            self.reason = str(error)
        if self.reference is not None:
            self.threshold = thresholds_for(self.reference.provider)["guard"]

    @property
    def available(self):
        return self.reference is not None

    def faces(self, candidate):
        from enhancement.identity_guard import compare_faces

        return compare_faces(self.reference, candidate)

    def check(self, candidate):
        found = [item["similarity"] for item in self.faces(candidate) if item["similarity"] is not None]
        if not found:
            return {"status": "inconclusive", "similarity": None, "threshold": self.threshold}
        worst = round(min(found), 4)
        return {"status": "verified" if worst >= self.threshold else "failed", "similarity": worst, "threshold": self.threshold}


class AdaptiveImageEnhancementService:
    def __init__(self, analyzer=None, router=None, config=None, runners=None):
        self.config = config or EngineConfig.from_settings()
        self.analyzer = analyzer
        self.router = router or AdaptiveEnhancementRouter(self.config)
        self.runners = runners or {}   # tests may replace an operation's runner

    # -- detector -----------------------------------------------------------------------------------------------
    def _analyzer(self):
        if self.analyzer is None:
            from validation.services import get_face_detector

            detector = None
            try:
                detector = get_face_detector()
            except Exception:  # the configured detector cannot load: fall back to YuNet, then to no faces
                try:
                    from validation.services import OpenCVYuNetFaceDetector

                    detector = OpenCVYuNetFaceDetector()
                except Exception:
                    detector = None
            self.analyzer = ImageQualityAnalyzer(detector=detector) if detector is not None else ImageQualityAnalyzer()
        return self.analyzer

    # -- one operation: propose variants, validate, choose ------------------------------------------------------
    def _run(self, operation, engine, image, variant):
        if operation in self.runners:
            return self.runners[operation](image, engine, variant)
        if operation == ops.LOW_LIGHT:
            return ops.run_low_light(image, engine, variant["blend"])
        if operation == ops.DENOISE:
            return ops.run_denoise(image, engine, variant["blend"])
        if operation == ops.DEBLUR:
            return ops.run_deblur(image, engine, variant["blend"])
        if operation == ops.FACE:
            return ops.run_face_restoration(image, engine, variant["fidelity"])
        return ops.run_upscale(image, engine, variant["scale"])

    def _unavailable_errors(self):
        from enhancement.codeformer import CodeFormerUnavailable
        from enhancement.gfpgan import GFPGANUnavailable
        from enhancement.hd_restoration import HDRestorationUnavailable
        from enhancement.lowlight import LowLightUnavailable
        from enhancement.zero_dce import ZeroDCEUnavailable

        return (CodeFormerUnavailable, GFPGANUnavailable, HDRestorationUnavailable, LowLightUnavailable, ZeroDCEUnavailable)

    def _propose(self, step, current, report, pool, validator, identity, record):
        errors = self._unavailable_errors()
        scale = next((variant["scale"] for variant in step.variants if "scale" in variant), 1)
        tried, produced = 0, []
        for engine in step.engines:
            if tried >= self.config.max_engines_per_step:
                break
            engine_produced = []
            for variant in step.variants:
                started = time.perf_counter()
                try:
                    image, models = self._run(step.operation, engine, current.image, variant)
                except errors as error:
                    record["skipped_engines"].append({"engine": engine, "reason": str(error)})
                    break
                except PoolFull:
                    break
                candidate_image = image
                if step.operation == ops.FACE and report.face_count > 1 and identity is not None and identity.available:
                    candidate_image, per_face = self._per_face_recomposition(current.image, image, report, identity)
                    models = models + [{"per_face": per_face}]
                try:
                    candidate = pool.add(candidate_image, current, step.operation, variant["name"], models,
                                         seconds=time.perf_counter() - started)
                except PoolFull:
                    record["notes"].append("The candidate limit was reached.")
                    break
                candidate.verdict = validator.evaluate(
                    current.image, candidate.image, step.operation, report, expected_scale=scale if step.operation == ops.UPSCALE else 1,
                )
                engine_produced.append(candidate)
            tried += 1
            produced.extend(engine_produced)
            if any(candidate.verdict["accepted"] for candidate in engine_produced):
                break  # an engine whose candidate passed is enough; a second engine is a fallback, not a second opinion
        return produced

    def _per_face_recomposition(self, parent, restored, report, identity):
        """Keep each restored face only if that face's identity survives; the others stay as they were."""
        height, width = parent.shape[:2]
        keep = np.zeros((height, width), np.float32)
        summary = []
        for face in report.faces:
            x, y, w, h = face.box
            mask = np.zeros((height, width), np.float32)
            cv2.ellipse(mask, (int(x + w / 2), int(y + h * 0.52)), (int(w * 0.75), int(h * 0.85)), 0, 0, 360, 1.0, -1)
            mask = cv2.GaussianBlur(mask, (0, 0), max(3.0, w * 0.08))[..., None]
            trial = (parent.astype(np.float32) * (1 - mask) + restored.astype(np.float32) * mask)
            trial = np.clip(np.rint(trial), 0, 255).astype(np.uint8)
            similarities = [item["similarity"] for item in identity.faces(trial) if item["similarity"] is not None]
            passed = bool(similarities) and min(similarities) >= identity.threshold
            summary.append({"box": list(face.box), "restored": passed, "worst_similarity": round(min(similarities), 4) if similarities else None})
            if passed:
                keep = np.maximum(keep, mask[..., 0])
        keep = keep[..., None]
        merged = np.clip(np.rint(parent.astype(np.float32) * (1 - keep) + restored.astype(np.float32) * keep), 0, 255).astype(np.uint8)
        return merged, summary

    # -- the whole run ------------------------------------------------------------------------------------------
    def enhance(self, image, requested_level=EnhancementLevel.AUTO, style="natural", allow_biometric_processing=True,
                checkpoint=None, debug=None):
        from enhancement.services import analyze_image_quality, compare_image_quality

        checkpoint = checkpoint or (lambda _name: None)
        started = time.perf_counter()
        original = image
        analyzer = self._analyzer()
        recorder = DebugRecorder(settings.ADAPTIVE_DEBUG_ARTIFACTS if debug is None else debug, uuid.uuid4().hex)
        recorder.image("original", original)

        checkpoint("analyze_original")
        report0 = analyzer.analyze(original)
        recorder.json("01_quality_analysis", report0.as_dict())
        identity = IdentityChecker(original, allow_biometric_processing and report0.faces != [])
        pool = CandidatePool(original, self.config.max_candidates)
        validator = FinalImageQualityValidator(analyzer, report0, identity if identity.available else None)

        current, report = pool.original, report0
        done, stages, skipped, warnings = set(), [], [], []
        if not allow_biometric_processing and report0.faces:
            skipped.append({"operation": ops.FACE, "reason": "Biometric processing is not permitted, so faces are not restored."})
            done.add(ops.FACE)
        if report0.faces and not identity.available and allow_biometric_processing:
            warnings.append(identity.reason)

        while True:
            if time.perf_counter() - started > self.config.time_budget_seconds:
                warnings.append("The time budget was reached; remaining operations were not attempted.")
                break
            decision = self.router.next_step(report, done)
            for item in decision.skipped:
                if item not in skipped:
                    skipped.append(item)
            if decision.step is None:
                break
            step = decision.step
            if (step.operation == ops.FACE and not identity.available and allow_biometric_processing
                    and "too dark" in identity.reason and current is not pool.original):
                # The original face was too dark to embed; the validated, lifted image is the best available reference.
                lifted = IdentityChecker(current.image, True)
                if lifted.available:
                    identity, validator.identity = lifted, lifted
                    warnings.append("Identity was checked against the brightened photograph, because the original face was too dark to embed.")
            if step.operation == ops.FACE and not identity.available:
                done.add(step.operation)
                skipped.append({"operation": step.operation, "reason": "Identity could not be verified, so no generative face "
                                                                       "restoration is applied. " + identity.reason})
                continue
            done.add(step.operation)
            checkpoint(step.operation)
            record = {"operation": step.operation, "reason": step.reason, "engines": step.engines, "skipped_engines": [],
                      "notes": [], "candidates": []}
            if not step.engines:
                record["status"] = "no_engine"
                record["notes"].append("No engine for this operation is switched on.")
                stages.append(record)
                continue
            produced = self._propose(step, current, report, pool, validator, identity if identity.available else None, record)
            record["candidates"] = [candidate.id for candidate in produced]
            accepted = [candidate for candidate in produced if candidate.verdict["accepted"]]
            if not accepted:
                record["status"] = "rejected" if produced else "unavailable"
                record["rejection_reasons"] = sorted({reason for candidate in produced for reason in candidate.verdict["reasons"]})
                stages.append(record)
                continue
            chosen = max(accepted, key=lambda c: (validator.preference(c.verdict), -c.verdict["change_cost"]))
            pool.select(chosen)
            record.update(status="accepted", chosen=chosen.id, variant=chosen.variant)
            stages.append(record)
            recorder.image(self._debug_name(step.operation), chosen.image)
            current, report = chosen, analyzer.analyze(chosen.image)

        final = current.image
        accepted_operations = [s["operation"] for s in stages if s["status"] == "accepted"]
        # A defect is unresolved only if the ORIGINAL had it and the result still does. (Counting whatever the analyzer finds in the
        # result was wrong: a brightened photograph is smoother and read as "blurred", which then reported a defect that was never there.)
        remaining = [c for c in report.conditions if c in ADDRESSABLE and c in report0.conditions] if accepted_operations else []
        introduced = [c for c in report.conditions if c in ADDRESSABLE and c not in report0.conditions] if accepted_operations else []
        if not accepted_operations:
            outcome = ORIGINAL_PRESERVED
        elif remaining:
            outcome = PARTIALLY_ENHANCED
        else:
            outcome = ENHANCED
        final_identity = identity.check(final) if identity.available and accepted_operations else (
            {"status": "unavailable" if not identity.available else "verified", "similarity": None, "threshold": identity.threshold})
        recorder.image("07_final", final)
        recorder.json("candidates", pool.describe())

        applied_level = {0: EnhancementLevel.LOW, 1: EnhancementLevel.LOW, 2: EnhancementLevel.MEDIUM}.get(
            len(accepted_operations), EnhancementLevel.HIGH)
        algorithm = "adaptive_v1" + "".join(f"+{self._chosen_model(pool, s)}" for s in stages if s["status"] == "accepted")
        quality_comparison = compare_image_quality(original, final)
        summary = self._summary(outcome, stages, skipped, remaining)
        parameters = {
            "algorithm": algorithm if accepted_operations else "adaptive_v1+original_preserved",
            "pipeline_version": PIPELINE_VERSION,
            "render_policy": rendering_policy.describe(),
            "requested_level": str(requested_level), "style": str(style),
            "quality_before": analyze_image_quality(original), "quality_after": analyze_image_quality(
                cv2.resize(final, (original.shape[1], original.shape[0]), interpolation=cv2.INTER_AREA) if final.shape != original.shape else final),
            "quality_comparison": quality_comparison, "warnings": warnings,
            "identity_status": final_identity["status"],
            "identity_verification": {
                "status": final_identity["status"], "verified": final_identity["status"] == "verified",
                "similarity": final_identity.get("similarity"), "threshold": final_identity.get("threshold"),
                "face_restoration_unverified": False,
                "notice": "" if final_identity["status"] in ("verified", "unavailable") else
                          "Identity could not be confirmed for every face; face restoration was not applied without it.",
                "reason": identity.reason if not identity.available else "",
            },
            "stage_reports": [{"stage": s["operation"], "status": s["status"], "changed_pixels": s["status"] == "accepted"} for s in stages],
            "resolution_contract": {"validated": True, "source_dimensions": [original.shape[1], original.shape[0]],
                                    "actual_output_dimensions": [final.shape[1], final.shape[0]]},
            "adaptive": {
                "outcome": outcome, "summary": summary, "operations_applied": accepted_operations,
                "operations_skipped": skipped, "stages": stages, "unresolved_defects": remaining,
                "conditions_seen_only_after_enhancement": introduced,
                "quality_report": report0.as_dict(), "final_quality_report": report.as_dict(),
                # The full candidate record (models, timings, verdicts) is for development; a user-facing result keeps the gist.
                "candidates": pool.describe() if recorder.enabled else [
                    {"id": c.id, "operation": c.operation, "variant": c.variant, "selected": c.selected,
                     "accepted": None if c.verdict is None else c.verdict["accepted"]} for c in pool.items],
                "debug_artifacts": recorder.location,
                "limits": {"max_candidates": self.config.max_candidates, "time_budget_seconds": self.config.time_budget_seconds},
                "seconds": round(time.perf_counter() - started, 2),
            },
        }
        return (final.copy() if final is original else final), applied_level, parameters

    # -- small helpers ------------------------------------------------------------------------------------------
    @staticmethod
    def _debug_name(operation):
        return {ops.LOW_LIGHT: "02_low_light", ops.DENOISE: "03_denoised", ops.DEBLUR: "04_deblurred",
                ops.FACE: "05_face_restored", ops.UPSCALE: "06_upscaled"}[operation]

    @staticmethod
    def _chosen_model(pool, stage):
        chosen = next((c for c in pool.items if c.id == stage.get("chosen")), None)
        return (chosen.models[0].get("model") if chosen and chosen.models and chosen.models[0].get("model") else stage["operation"])

    @staticmethod
    def _summary(outcome, stages, skipped, remaining):
        words = {ops.LOW_LIGHT: "brightened the dark subject", ops.DENOISE: "reduced noise", ops.DEBLUR: "reduced motion blur",
                 ops.FACE: "restored a degraded face", ops.UPSCALE: "enlarged the photograph"}
        done = [words[s["operation"]] for s in stages if s["status"] == "accepted"]
        if outcome == ORIGINAL_PRESERVED:
            rejected = [s for s in stages if s["status"] in ("rejected", "unavailable", "no_engine")]
            if rejected and all(s["status"] in ("unavailable", "no_engine") for s in rejected):
                return "The enhancement models were not available, so the original photograph was kept."
            if rejected:
                return "No enhancement passed the safety checks, so the original photograph was kept."
            return "The photograph did not need enhancement, so it was left as it is."
        text = "Enhanced: " + ", ".join(done) + "."
        if remaining:
            text += " Not fully corrected: " + " ".join(sorted({UNADDRESSABLE_REASONS.get(item, item) for item in remaining})) + ""
        return text
