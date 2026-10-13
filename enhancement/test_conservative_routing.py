"""A correction needs a measured defect, and compressed photos are not sharpened.

Real CPU processing: these tests run the actual OpenCV pipeline (no learned
model is involved or mocked) on synthetic images built to have, or not have,
a specific defect.
"""
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from .services import enhance_bgr, measure_compression_artifacts
from .test_pipeline_regressions import NO_MODELS, textured


def detailed(height=720, width=960, base=135, seed=3):
    """A well-lit photo-like image with real edges and mid-frequency detail."""
    rng = np.random.default_rng(seed)
    image = textured(height, width, base=base).astype(np.float32)
    for _ in range(60):
        x, y = int(rng.integers(0, width - 120)), int(rng.integers(0, height - 120))
        w, h = int(rng.integers(30, 120)), int(rng.integers(30, 120))
        image[y:y + h, x:x + w] += rng.uniform(-45, 45, 3)
    # Fine detail that is correlated between neighbouring pixels, as real
    # texture is: enough to read as sharp, not enough to read as noise.
    fine = cv2.GaussianBlur(rng.normal(0, 1, (height, width)).astype(np.float32), (0, 0), 1.0)
    image += (fine / fine.std() * 14)[:, :, None]
    return np.clip(image, 0, 255).astype(np.uint8)


def jpeg(image, quality):
    return cv2.imdecode(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)


def grain(image, sigma, seed=9):
    rng = np.random.default_rng(seed)
    return np.clip(image.astype(np.float32) + rng.normal(0, sigma, image.shape), 0, 255).astype(np.uint8)


def fine_texture(height=720, width=960, seed=4):
    """Gravel-like detail: strong, but correlated across neighbouring pixels."""
    rng = np.random.default_rng(seed)
    field = cv2.GaussianBlur(rng.normal(0, 1, (height, width)).astype(np.float32), (0, 0), 1.0)
    field = 135 + field / field.std() * 30
    return cv2.cvtColor(np.clip(field, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


class CompressionDetectionTests(SimpleTestCase):
    def test_an_uncompressed_photo_is_not_flagged(self):
        measured = measure_compression_artifacts(detailed())
        self.assertFalse(measured["detected"])
        self.assertLess(measured["blockiness"][0], 1.2)

    def test_heavy_jpeg_compression_is_flagged_and_light_compression_is_not(self):
        clean = detailed()
        for quality in (10, 20, 35):
            self.assertTrue(measure_compression_artifacts(jpeg(clean, quality))["detected"], quality)
        self.assertFalse(measure_compression_artifacts(jpeg(clean, 95))["detected"])

    def test_blockiness_grows_as_quality_falls(self):
        clean = detailed()
        strengths = [measure_compression_artifacts(jpeg(clean, q))["blockiness"][0] for q in (90, 50, 20, 10)]
        self.assertEqual(strengths, sorted(strengths))

    def test_a_crop_or_rotation_of_a_compressed_photo_is_still_recognised(self):
        compressed = jpeg(detailed(), 20)
        self.assertTrue(measure_compression_artifacts(np.ascontiguousarray(compressed[3:, 5:]))["detected"])
        self.assertTrue(measure_compression_artifacts(cv2.rotate(compressed[:, 2:], cv2.ROTATE_90_CLOCKWISE))["detected"])

    def test_stripes_on_one_axis_are_not_mistaken_for_compression(self):
        striped = detailed()
        striped[:, ::8] = np.clip(striped[:, ::8].astype(np.int16) + 40, 0, 255).astype(np.uint8)
        measured = measure_compression_artifacts(striped)
        self.assertGreater(measured["blockiness"][0], 1.35)  # one axis looks blocky...
        self.assertFalse(measured["detected"])               # ...but JPEG shows on both

    def test_a_tiny_image_is_not_judged(self):
        self.assertFalse(measure_compression_artifacts(np.zeros((40, 40, 3), dtype=np.uint8))["detected"])


@override_settings(**NO_MODELS)
class DefectDrivenRoutingTests(SimpleTestCase):
    def test_a_photo_with_no_defect_is_returned_unchanged(self):
        good = detailed()
        output, level, parameters = enhance_bgr(good, "auto")

        np.testing.assert_array_equal(output, good)
        self.assertIsNot(output, good)  # still never the caller's own array
        self.assertTrue(parameters["no_correction_needed"])
        self.assertTrue(parameters["plan"]["no_correction_needed"])
        self.assertFalse(parameters["plan"]["steps"]["exposure"]["planned"])
        decisions = {(entry["stage"], entry["decision"]) for entry in parameters["plan"]["routing"]}
        self.assertEqual(decisions, {("exposure_enhancement", "skipped"), ("sharpen", "skipped")})
        self.assertFalse(any(entry["changed_pixels"] for entry in parameters["stage_reports"]))

    def test_an_explicit_level_or_style_is_still_applied_to_a_good_photo(self):
        good = detailed()
        for arguments in ({"requested_level": "medium"}, {"style": "dynamic"}):
            output, _level, parameters = enhance_bgr(good, **arguments)
            self.assertFalse(parameters["no_correction_needed"], arguments)
            self.assertFalse(np.array_equal(output, good), arguments)

    def test_a_soft_but_well_lit_photo_is_sharpened_and_not_re_exposed(self):
        soft = cv2.GaussianBlur(detailed(), (0, 0), 2.2)
        output, _level, parameters = enhance_bgr(soft, "auto")

        self.assertTrue({"mild_blur", "severe_blur"} & set(parameters["quality_before"]["defects"]))
        changed = [entry["stage"] for entry in parameters["stage_reports"] if entry["changed_pixels"]]
        self.assertIn("sharpen", changed)
        self.assertNotIn("exposure_enhancement", changed)
        self.assertGreater(parameters["quality_after"]["laplacian_sharpness"], parameters["quality_before"]["laplacian_sharpness"])
        self.assertAlmostEqual(
            parameters["quality_after"]["mean_luminance"], parameters["quality_before"]["mean_luminance"], delta=0.5,
        )

    def test_a_dark_photo_is_still_corrected(self):
        dark = detailed(base=45)
        output, _level, parameters = enhance_bgr(dark, "auto")
        self.assertFalse(parameters["no_correction_needed"])
        self.assertGreater(float(output.mean()), float(dark.mean()) + 5)

    def test_a_compressed_photo_is_never_sharpened_or_given_local_contrast(self):
        compressed = jpeg(cv2.GaussianBlur(detailed(base=70), (0, 0), 1.5), 15)  # dark, soft and blocky
        for arguments in ({"requested_level": "auto"}, {"requested_level": "high"}, {"style": "dynamic"}):
            _output, _level, parameters = enhance_bgr(compressed, **arguments)
            self.assertTrue(parameters["plan"]["compression_artifacts"], arguments)
            self.assertEqual(parameters["sharpen"], 0, arguments)
            self.assertFalse(parameters["use_clahe"], arguments)
            reasons = " ".join(entry["reason"] for entry in parameters["plan"]["routing"])
            self.assertIn("block edges", reasons)
            self.assertEqual(
                [e["status"] for e in parameters["stage_reports"] if e["stage"] == "sharpen"], ["skipped"], arguments,
            )

    def test_compression_artifacts_are_not_made_worse(self):
        """The regression the evaluation found: blocks got stronger after enhancement."""
        compressed = jpeg(detailed(), 15)
        output, _level, parameters = enhance_bgr(compressed, "auto")

        before = measure_compression_artifacts(compressed)["blockiness"][0]
        after = measure_compression_artifacts(output)["blockiness"][0]
        self.assertLessEqual(after, before + 0.01)
        np.testing.assert_array_equal(output, compressed)  # well lit: nothing to correct, nothing amplified

    def test_a_dark_compressed_photo_is_brightened_without_stronger_blocks(self):
        compressed = jpeg(detailed(base=50), 15)
        output, _level, parameters = enhance_bgr(compressed, "auto")

        self.assertGreater(float(output.mean()), float(compressed.mean()) + 5)
        # What local contrast and sharpening would have done to the same input.
        def measure_only(context):
            context.input_quality["compression_artifacts"] = measure_compression_artifacts(context.source_image)
            return []

        with patch("enhancement.services._apply_conservative_routing", side_effect=measure_only):
            unrouted_output, _level, _parameters = enhance_bgr(compressed, "auto")
        routed = measure_compression_artifacts(output)["blockiness"][0]
        unrouted = measure_compression_artifacts(unrouted_output)["blockiness"][0]
        self.assertLess(routed, unrouted)


@override_settings(**NO_MODELS)
class TextureIsNotNoiseTests(SimpleTestCase):
    def test_a_well_lit_finely_textured_photo_is_not_denoised(self):
        texture = fine_texture()
        output, level, parameters = enhance_bgr(texture, "auto")

        denoise = parameters["adaptive_denoise"]
        self.assertEqual(level, "low")
        self.assertGreaterEqual(denoise["input_noise_score"], 5.0)  # it *looks* noisy
        self.assertFalse(denoise["applied"])
        self.assertEqual(denoise["reason"], "candidate_rejected")
        self.assertIn("removed_texture_not_noise", denoise["candidate_guard"]["rejection_reasons"])
        np.testing.assert_array_equal(output, texture)

    def test_a_well_lit_grainy_photo_is_still_denoised(self):
        grainy = grain(textured(720, 960, base=135), 16)
        _output, _level, parameters = enhance_bgr(grainy, "auto")
        self.assertTrue(parameters["adaptive_denoise"]["applied"])
        self.assertEqual(parameters["adaptive_denoise"]["reason"], "measured_noise_accepted")
