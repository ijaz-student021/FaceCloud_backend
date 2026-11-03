"""The histogram-based quality measurement must equal the per-pixel one exactly.

The guards and every downstream decision consume these numbers, so a faster
implementation is only acceptable if it returns identical values. The reference
below is the previous, straightforward implementation kept verbatim.
"""
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase

from .services import (
    _histogram_median,
    _histogram_percentile,
    analyze_image_quality,
    measure_blur,
)


def reference_analyze(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    luminance = float(np.mean(gray))
    contrast = float(np.std(gray))
    luminance_reference = float(gray.mean())
    measured = gray                      # a photograph darker than 90 is measured brightened to 110 (a dark photograph is not a blurry one)
    if 0.5 <= luminance_reference < 90.0:
        gamma = max(0.2, float(np.log(110.0 / 255.0) / np.log(luminance_reference / 255.0)))
        measured = cv2.LUT(gray, (255.0 * np.power(np.arange(256, dtype=np.float64) / 255.0, gamma)).astype(np.uint8))
    sharpness = float(cv2.Laplacian(measured, cv2.CV_64F).var())
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    noise = float(np.median(np.abs(gray.astype(np.float32) - smooth.astype(np.float32))))
    shadow_clipping = float(np.mean(gray <= 5) * 100)
    highlight_clipping = float(np.mean(gray >= 250) * 100)
    luminance_percentiles = np.percentile(gray, (5, 25, 50, 75, 95))
    dark_pixel_percent = float(np.mean(gray < 64) * 100)
    bright_pixel_percent = float(np.mean(gray > 224) * 100)
    height, width = gray.shape
    shortest_edge = min(width, height)
    defects = []
    if luminance < 65:
        defects.append("severe_low_light")
    elif luminance < 115:
        defects.append("low_light")
    blur = measure_blur(gray)  # its own tests: test_blur_measure.py
    if blur["verdict"] == "severe":
        defects.append("severe_blur")
    elif blur["verdict"] == "mild":
        defects.append("mild_blur")
    if noise > 5:
        defects.append("visible_noise")
    if contrast < 28:
        defects.append("low_contrast")
    if highlight_clipping > 8:
        defects.append("overexposed_highlights")
    if shortest_edge < 720:
        defects.append("low_resolution")
    restoration_required = any(d in defects for d in ("severe_blur", "low_resolution"))
    return {
        "width": width,
        "height": height,
        "mean_luminance": round(luminance, 2),
        "contrast_stddev": round(contrast, 2),
        "laplacian_sharpness": round(sharpness, 2),
        "blur": blur,
        "estimated_noise": round(noise, 2),
        "shadow_clipping_percent": round(shadow_clipping, 3),
        "highlight_clipping_percent": round(highlight_clipping, 3),
        "luminance_percentiles": {
            "p05": round(float(luminance_percentiles[0]), 2),
            "p25": round(float(luminance_percentiles[1]), 2),
            "p50": round(float(luminance_percentiles[2]), 2),
            "p75": round(float(luminance_percentiles[3]), 2),
            "p95": round(float(luminance_percentiles[4]), 2),
        },
        "dark_pixel_percent": round(dark_pixel_percent, 3),
        "bright_pixel_percent": round(bright_pixel_percent, 3),
        "defects": defects,
        "needs_ai_restoration": restoration_required,
    }


class HistogramMathTests(SimpleTestCase):
    """The building blocks against NumPy on arbitrary integer data."""

    def cumulative(self, values):
        histogram = np.bincount(values.ravel(), minlength=256).astype(np.int64)
        return np.cumsum(histogram)

    def test_percentiles_match_numpy_exactly(self):
        rng = np.random.default_rng(1)
        for trial in range(300):
            size = int(rng.integers(1, 5000))
            values = rng.integers(0, int(rng.integers(2, 257)), size=size, dtype=np.uint8)
            cumulative = self.cumulative(values)
            for percent in (0, 5, 25, 50, 75, 95, 100):
                with self.subTest(trial=trial, percent=percent):
                    self.assertEqual(
                        _histogram_percentile(cumulative, size, percent),
                        float(np.percentile(values, percent)),
                    )

    def test_median_matches_numpy_for_odd_and_even_counts(self):
        rng = np.random.default_rng(2)
        for size in list(range(1, 40)) + [1000, 1001, 4096, 4097]:
            values = rng.integers(0, 256, size=size, dtype=np.uint8)
            with self.subTest(size=size):
                self.assertEqual(
                    _histogram_median(self.cumulative(values), size),
                    float(np.median(values.astype(np.float32))),
                )


class QualityAnalysisEquivalenceTests(SimpleTestCase):
    def assertSame(self, image, label=""):
        self.assertEqual(analyze_image_quality(image), reference_analyze(image), label)

    def test_random_photos_of_many_shapes_and_brightness_profiles(self):
        rng = np.random.default_rng(3)
        for trial in range(150):
            height, width = int(rng.integers(3, 260)), int(rng.integers(3, 260))
            low, high = sorted(int(v) for v in rng.integers(0, 256, size=2))
            image = rng.integers(low, high + 1, size=(height, width, 3), dtype=np.uint8)
            self.assertSame(image, f"trial {trial} {height}x{width} [{low},{high}]")

    def test_structured_images_with_edges_gradients_and_noise(self):
        rng = np.random.default_rng(4)
        gradient = np.tile(np.linspace(0, 255, 320, dtype=np.uint8), (240, 1))
        cases = {
            "gradient": cv2.merge([gradient] * 3),
            "checkerboard": np.kron(
                (np.indices((16, 16)).sum(axis=0) % 2 * 255).astype(np.uint8), np.ones((12, 12), np.uint8)
            )[:, :, None].repeat(3, axis=2),
            "noisy dark": np.clip(rng.normal(20, 12, (200, 300, 3)), 0, 255).astype(np.uint8),
            "noisy bright": np.clip(rng.normal(235, 15, (200, 300, 3)), 0, 255).astype(np.uint8),
        }
        for label, image in cases.items():
            with self.subTest(label=label):
                self.assertSame(image, label)

    def test_degenerate_images(self):
        for label, image in {
            "all black": np.zeros((50, 60, 3), np.uint8),
            "all white": np.full((50, 60, 3), 255, np.uint8),
            "mid grey": np.full((50, 60, 3), 128, np.uint8),
            "one row": np.random.default_rng(5).integers(0, 256, (1, 40, 3), dtype=np.uint8),
            "one column": np.random.default_rng(6).integers(0, 256, (40, 1, 3), dtype=np.uint8),
            "tiny": np.random.default_rng(7).integers(0, 256, (3, 3, 3), dtype=np.uint8),
        }.items():
            with self.subTest(label=label):
                self.assertSame(image, label)

    def test_real_photographs_full_size_and_resized(self):
        root = Path(settings.BASE_DIR) / ".runtime" / "dataset_review" / "image"
        photos = sorted(root.glob("*.png")) if root.exists() else []
        if not photos:
            self.skipTest("no sample photographs available")
        for path in photos:
            image = cv2.imread(str(path))
            for label, candidate in (
                ("full", image),
                ("half", cv2.resize(image, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)),
                ("dark", cv2.convertScaleAbs(image, alpha=0.25)),
                ("bright", cv2.convertScaleAbs(image, alpha=1.0, beta=90)),
            ):
                with self.subTest(photo=path.name, variant=label):
                    self.assertSame(candidate, f"{path.name} {label}")

    def test_invalid_input_is_still_rejected(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            analyze_image_quality(None)
        with self.assertRaises(ValidationError):
            analyze_image_quality(np.zeros((0, 0, 3), np.uint8))
