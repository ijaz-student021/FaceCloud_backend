import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .models import EnhancementLevel
from .services import (
    EnhancementPipelineContext,
    _edge_aware_denoise,
    adaptive_denoise_stage,
    analyze_original_stage,
    exposure_enhancement_stage,
    measure_post_exposure_noise_stage,
    output_guard_a_stage,
)


def noisy_blocks(sigma=14, seed=3):
    """Ten by ten flat blocks (plenty of real edges, like a photo) plus Gaussian grain."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(40, 220, size=(10, 10, 3), dtype=np.uint8)
    clean = np.kron(blocks, np.ones((20, 20, 1), dtype=np.uint8)).astype(np.float32)
    noisy = np.clip(clean + rng.normal(0, sigma, clean.shape), 0, 255).astype(np.uint8)
    interior = np.zeros(clean.shape[:2], dtype=bool)
    for row in range(10):
        for column in range(10):
            interior[row * 20 + 5:row * 20 + 15, column * 20 + 5:column * 20 + 15] = True
    return clean, noisy, interior


def context_before_denoise(source, level=EnhancementLevel.HIGH):
    context = EnhancementPipelineContext(
        source, level, style="natural", allow_biometric_processing=True, use_zero_dce=False,
    )
    for stage in (
        analyze_original_stage, exposure_enhancement_stage,
        output_guard_a_stage, measure_post_exposure_noise_stage,
    ):
        stage(context)
    return context


class EdgeAwareDenoiseTests(SimpleTestCase):
    def test_grain_in_flat_areas_is_mostly_removed_even_when_the_input_is_very_noisy(self):
        clean, noisy, interior = noisy_blocks()
        cleaned = _edge_aware_denoise(noisy, 7)
        before = float((noisy.astype(np.float32) - clean)[interior].std())
        after = float((cleaned.astype(np.float32) - clean)[interior].std())
        # Finding edges on the noisy input left ~90% of the grain (12.5 of 14.0).
        self.assertLess(after, before * 0.65)

    def test_block_edges_keep_their_contrast(self):
        clean, noisy, interior = noisy_blocks()
        cleaned = _edge_aware_denoise(noisy, 7).astype(np.float32)
        # Average block colours must stay put: no bleeding across the edges.
        error = np.abs(cleaned - clean)[interior].mean()
        self.assertLess(error, 6.0)
        # The jump across every vertical block boundary must survive.
        def mean_step(image):
            steps = []
            for column in range(20, 200, 20):
                left = image[:, column - 8:column - 2].mean(axis=1)
                right = image[:, column + 2:column + 8].mean(axis=1)
                steps.append(np.abs(right - left).mean())
            return float(np.mean(steps))

        self.assertGreater(mean_step(cleaned), 0.9 * mean_step(clean))


@override_settings(
    AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False, ADAPTIVE_DENOISE_ENABLED=True,
    ADAPTIVE_DENOISE_BREAKPOINTS=(1.0, 2.0, 3.5, 5.0, 7.0),
    ADAPTIVE_DENOISE_STRENGTHS=(0, 3, 4, 5, 6, 7),
)
class BlurProtectionTests(SimpleTestCase):
    def test_a_dark_frame_is_not_mistaken_for_a_blurry_one(self):
        # Near-black grain: almost no Laplacian variance, so the input reads as
        # "severe blur", but the brightened result is sharp and noisy.
        dark = np.random.default_rng(5).integers(0, 4, size=(180, 180, 3), dtype=np.uint8)
        context = context_before_denoise(dark)
        self.assertIn("severe_blur", context.input_quality["defects"])
        self.assertNotIn("severe_blur", context.post_exposure_noise["quality"]["defects"])
        adaptive_denoise_stage(context)
        self.assertNotEqual(context.adaptive_denoise["reason"], "severe_blur_protection")

    def test_a_genuinely_blurry_bright_photo_is_still_protected(self):
        texture = np.random.default_rng(9).integers(90, 170, size=(180, 180, 3), dtype=np.uint8)
        blurry = cv2.GaussianBlur(texture, (0, 0), 12)
        context = context_before_denoise(blurry, level=EnhancementLevel.MEDIUM)
        self.assertIn("severe_blur", context.post_exposure_noise["quality"]["defects"])
        adaptive_denoise_stage(context)
        self.assertEqual(context.adaptive_denoise["reason"], "severe_blur_protection")
        self.assertFalse(context.adaptive_denoise["applied"])
