import tempfile
from unittest.mock import Mock, patch

import cv2
import numpy as np
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from uploads.models import ImagePreprocessingResult, ImageUpload, UploadBatch

from .models import (
    EditMode,
    EnhancementBatchJob,
    EnhancementLevel,
    EnhancementStyle,
    ImageEditVersion,
    ImageEnhancementResult,
)
from .services import (
    EnhancementPipelineContext,
    apply_gallery_adjustments,
    analyze_image_quality,
    analyze_original_stage,
    compare_image_quality,
    enhance_bgr,
    exposure_enhancement_stage,
    measure_post_exposure_noise_stage,
    output_guard_a_stage,
    process_enhancement_result,
    process_image_edit,
    restore_low_resolution,
)
from .tasks import (
    process_enhancement_batch,
    process_enhancement_image,
    process_gallery_edit,
)
from .zero_dce import _apply_curves_tiled
from .test_blur_measure import blurred_photo

User = get_user_model()


class EnhancementAppTests(SimpleTestCase):
    def test_enhancement_app_is_registered(self):
        config = apps.get_app_config("enhancement")

        self.assertEqual(config.name, "enhancement")
        self.assertEqual(config.verbose_name, "Image Enhancement")


class EnhancementModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="enhancement-owner",
            email="enhancement-owner@example.com",
            password="ForestRiver!4821",
            is_email_verified=True,
        )
        self.upload_batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        self.image = ImageUpload.objects.create(
            owner=self.user,
            batch=self.upload_batch,
            original_name="portrait.png",
            status="uploaded",
        )
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user,
            image=self.image,
            status="completed",
            output_format="png",
        )

    def test_batch_job_defaults_and_progress(self):
        job = EnhancementBatchJob.objects.create(
            owner=self.user,
            source_batch=self.upload_batch,
            total_images=4,
            processed_images=3,
        )

        self.assertEqual(job.requested_level, EnhancementLevel.AUTO)
        self.assertEqual(job.progress_percentage, 75)

    def test_only_one_active_enhancement_per_image(self):
        ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.MEDIUM,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ImageEnhancementResult.objects.create(
                owner=self.user,
                image=self.image,
                preprocessing_result=self.preprocessing,
                requested_level=EnhancementLevel.HIGH,
            )

    def test_completed_result_allows_a_new_enhancement(self):
        ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.LOW,
            applied_level=EnhancementLevel.LOW,
            status="completed",
        )

        second = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.HIGH,
        )

        self.assertEqual(second.status, "pending")

    def test_background_tasks_ignore_stale_database_identifiers(self):
        missing_id = 9_999_999

        self.assertEqual(
            process_enhancement_image.run(missing_id),
            {"result_id": missing_id, "status": "missing"},
        )
        self.assertEqual(
            process_enhancement_batch.run(missing_id),
            {"job_id": missing_id, "status": "missing"},
        )
        self.assertEqual(
            process_gallery_edit.run(missing_id),
            {"edit_id": missing_id, "status": "missing"},
        )


class OpenCVPolicyTests(SimpleTestCase):
    @override_settings(
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=False,
        GFPGAN_ENABLED=False,
        GFPGAN_QUEUE_ENABLED=False,
        HD_RESTORATION_ENABLED=False,
        NAFNET_DEBLUR_ENABLED=False,
        IDENTITY_GUARD_ENABLED=False,
    )
    def test_rollout_flags_default_to_safe_disabled_pipeline(self):
        source = np.full((96, 96, 3), 135, dtype=np.uint8)

        _enhanced, _level, parameters = enhance_bgr(
            source,
            EnhancementLevel.AUTO,
        )

        self.assertEqual(
            parameters["feature_flags"],
            {
                "adaptive_denoise_enabled": False,
                "gfpgan_enabled": False,
                "gfpgan_queue_enabled": False,
                "hd_restoration_enabled": False,
                "nafnet_deblur_enabled": False,
                "purevision_enabled": False,
                "purescale_enabled": False,
                "colorization_enabled": False,
                "identity_guard_enabled": False,
                "face_fidelity_enabled": True,
                "gfpgan_require_identity_check": True,
            },
        )
        self.assertFalse(parameters["adaptive_denoise"]["enabled"])
        self.assertFalse(parameters["adaptive_denoise"]["applied"])

    @override_settings(
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=False,
        IDENTITY_GUARD_ENABLED=False,
    )
    def test_pipeline_exposes_stable_stage_order_and_timings(self):
        source = np.full((96, 96, 3), 75, dtype=np.uint8)

        _enhanced, _level, parameters = enhance_bgr(
            source,
            EnhancementLevel.AUTO,
        )

        expected_order = [
            "analyze_original",
            "exposure_enhancement",
            "output_guard_a",
            "measure_post_exposure_noise",
            "adaptive_denoise",
            "purevision_restoration",
            "low_resolution_restoration",
            "hd_restoration",
            "purescale_super_resolution",
            "face_restoration",
            "sharpen",
            "output_guard_b",
            "face_fidelity",
            "colorization",
            "dynamic_style",
            "identity_guard",
            "validate_output",
        ]
        self.assertEqual(parameters["stage_order"], expected_order)
        self.assertEqual(
            list(parameters["stage_timings_ms"]),
            expected_order,
        )
        self.assertTrue(
            all(
                duration >= 0
                for duration in parameters["stage_timings_ms"].values()
            )
        )
        self.assertEqual(
            set(parameters["quality_guards"]),
            {"guard_a", "guard_b"},
        )

    @override_settings(
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
    )
    def test_individual_exposure_stages_share_a_pipeline_context(self):
        source = np.full((96, 96, 3), 55, dtype=np.uint8)
        context = EnhancementPipelineContext(
            source,
            EnhancementLevel.AUTO,
        )

        analyze_original_stage(context)
        exposure_enhancement_stage(context)
        output_guard_a_stage(context)
        measure_post_exposure_noise_stage(context)

        self.assertEqual(context.applied_level, EnhancementLevel.HIGH)
        self.assertIn("guard_a", context.quality_guards)
        self.assertEqual(
            context.post_exposure_noise["measurement_point"],
            "after_guard_a",
        )
        self.assertEqual(context.current_image.shape, source.shape)

    @override_settings(
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=True,
        ADAPTIVE_DENOISE_BREAKPOINTS=(),
        ADAPTIVE_DENOISE_STRENGTHS=(4,),
    )
    @patch("enhancement.services.cv2.fastNlMeansDenoisingColored")
    def test_adaptive_denoise_uses_post_exposure_noise_mapping(self, denoise):
        # A smooth scene with grain on it: something a denoiser can actually help.
        rng = np.random.default_rng(18)
        ramp = np.tile(np.linspace(40, 110, 320, dtype=np.float32), (320, 1))
        source = np.clip(ramp[:, :, None] + rng.normal(0, 10, (320, 320, 3)), 0, 255).astype(np.uint8)
        # A stand-in that really removes the grain, so the candidate is kept.
        denoise.side_effect = lambda image, *_args: cv2.GaussianBlur(image, (0, 0), 1.2)

        _enhanced, _level, parameters = enhance_bgr(
            source,
            EnhancementLevel.HIGH,
        )

        denoise.assert_called_once()
        metadata = parameters["adaptive_denoise"]
        self.assertTrue(metadata["enabled"])
        self.assertTrue(metadata["applied"])
        self.assertEqual(metadata["strength"], metadata["requested_strength"])
        self.assertEqual(metadata["requested_strength"], denoise.call_args.args[2])
        self.assertEqual(metadata["measurement_point"], "after_guard_a")

    @override_settings(
        AI_RESTORATION_ENABLED=True,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=True,
        ADAPTIVE_DENOISE_BREAKPOINTS=(),
        ADAPTIVE_DENOISE_STRENGTHS=(3,),
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services._load_edsr_model")
    @patch("enhancement.services.cv2.fastNlMeansDenoisingColored")
    def test_edsr_runs_after_denoise_for_still_low_resolution_image(
        self,
        denoise,
        load_model,
    ):
        events = []
        source = np.random.default_rng(19).integers(
            20,
            130,
            size=(120, 100, 3),
            dtype=np.uint8,
        )

        def apply_denoise(image, *_args):
            events.append("denoise")
            return image.copy()

        model = Mock()

        def apply_edsr(image):
            events.append("edsr")
            return cv2.resize(image, (200, 240), interpolation=cv2.INTER_CUBIC)

        denoise.side_effect = apply_denoise
        model.upsample.side_effect = apply_edsr
        load_model.return_value = model

        enhanced, _level, parameters = enhance_bgr(
            source,
            EnhancementLevel.HIGH,
        )

        self.assertEqual(events, ["denoise", "edsr"])
        self.assertEqual(enhanced.shape[:2], (240, 200))
        self.assertTrue(parameters["restoration"]["applied"])
        self.assertEqual(
            parameters["restoration"]["stage"],
            "after_adaptive_denoise",
        )

    @override_settings(
        AI_RESTORATION_ENABLED=False,
        ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=True,
        ADAPTIVE_DENOISE_BREAKPOINTS=(1.0, 2.0, 3.5, 5.0, 7.0),
        ADAPTIVE_DENOISE_STRENGTHS=(0, 3, 4, 5, 6, 7),
    )
    def test_five_scene_regression_fixtures_remain_natural_and_measurable(self):
        rng = np.random.default_rng(2026)
        extreme_dark = rng.integers(2, 28, size=(180, 240, 3), dtype=np.uint8)
        normal_portrait = np.full((180, 240, 3), (95, 135, 175), dtype=np.uint8)
        cv2.circle(normal_portrait, (120, 90), 48, (105, 150, 195), -1)
        bright_portrait = np.full((180, 240, 3), (210, 225, 238), dtype=np.uint8)
        cv2.circle(bright_portrait, (120, 90), 48, (195, 215, 232), -1)
        low_resolution = cv2.resize(
            normal_portrait,
            (90, 120),
            interpolation=cv2.INTER_AREA,
        )
        multi_face = np.full((180, 300, 3), (65, 85, 105), dtype=np.uint8)
        for center in ((60, 90), (150, 90), (240, 90)):
            cv2.circle(multi_face, center, 34, (105, 145, 185), -1)

        fixtures = {
            "extremely_dark": extreme_dark,
            "normal_portrait": normal_portrait,
            "bright_portrait": bright_portrait,
            "low_resolution_portrait": low_resolution,
            "multi_face_group": multi_face,
        }
        comparisons = {}
        for name, source in fixtures.items():
            with self.subTest(scene=name):
                enhanced, _level, parameters = enhance_bgr(
                    source,
                    EnhancementLevel.AUTO,
                )
                comparison = parameters["quality_comparison"]
                comparisons[name] = comparison
                self.assertEqual(enhanced.shape, source.shape)
                self.assertEqual(
                    comparison["reference_dimensions"],
                    [source.shape[1], source.shape[0]],
                )
                self.assertLessEqual(
                    comparison["after"]["highlight_clipping_percent"],
                    max(
                        5.0,
                        comparison["before"]["highlight_clipping_percent"]
                        + 2.1,
                    ),
                )
                self.assertGreaterEqual(
                    comparison["after"]["contrast_stddev"],
                    comparison["before"]["contrast_stddev"] * 0.74,
                )

        self.assertGreater(
            comparisons["extremely_dark"]["after"]["mean_luminance"],
            comparisons["extremely_dark"]["before"]["mean_luminance"],
        )
        self.assertLessEqual(
            comparisons["bright_portrait"]["after"]["mean_luminance"],
            comparisons["bright_portrait"]["before"]["mean_luminance"] + 1.0,
        )

    def test_zero_curve_tiles_preserve_full_pixel_grid_without_seams(self):
        source = np.random.default_rng(2026).integers(
            0, 256, size=(260, 270, 3), dtype=np.uint8
        )
        curves = np.zeros((1, 24, 32, 32), dtype=np.float32)

        output = _apply_curves_tiled(source, curves, tile_edge=128)

        self.assertEqual(output.shape, source.shape)
        np.testing.assert_array_equal(output, source)

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_style_natural_is_the_default_and_unchanged(self):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)
        _enhanced, _level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)
        self.assertEqual(parameters["style"], EnhancementStyle.NATURAL)
        self.assertFalse(parameters["dynamic_style"]["applied"])

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_style_dynamic_applies_a_bounded_vivid_boost(self):
        source = np.zeros((80, 80, 3), dtype=np.uint8)
        source[:, :40] = (60, 90, 120)
        source[:, 40:] = (180, 160, 140)

        natural, _level, natural_params = enhance_bgr(
            source, EnhancementLevel.AUTO, style=EnhancementStyle.NATURAL,
        )
        vivid, _level, vivid_params = enhance_bgr(
            source, EnhancementLevel.AUTO, style=EnhancementStyle.DYNAMIC,
        )

        self.assertTrue(vivid_params["dynamic_style"]["applied"])
        self.assertIn("dynamic_vivid_boost", vivid_params["algorithm"])
        self.assertEqual(vivid.shape, natural.shape)
        natural_saturation = cv2.cvtColor(natural, cv2.COLOR_BGR2HSV)[:, :, 1].mean()
        vivid_saturation = cv2.cvtColor(vivid, cv2.COLOR_BGR2HSV)[:, :, 1].mean()
        self.assertGreater(vivid_saturation, natural_saturation)

    @override_settings(
        AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False,
        ADAPTIVE_DENOISE_ENABLED=False,
    )
    def test_style_ai_max_forces_high_level_and_adaptive_denoise(self):
        source = np.full((80, 80, 3), 120, dtype=np.uint8)

        _enhanced, level, parameters = enhance_bgr(
            source, EnhancementLevel.LOW, style=EnhancementStyle.AI_MAX,
        )

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertTrue(parameters["adaptive_denoise"]["enabled"])

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_auto_selects_high_for_dark_image_and_improves_luminance(self):
        source = np.full((80, 80, 3), 35, dtype=np.uint8)
        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertGreater(float(np.mean(enhanced)), float(np.mean(source)))
        self.assertEqual(parameters["colour_policy"], "preserve_lab_chroma_channels")

    @override_settings(
        AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True,
        EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD=25.0,
        EXTREME_LOW_LIGHT_MAX_LUMINANCE_GAIN=90.0,
        EXTREME_LOW_LIGHT_MODEL_STRENGTH=0.85,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_extremely_dark_source_gets_a_larger_brightening_allowance(self, zero_dce):
        """A normal HIGH-level max_luminance_gain of 30 would cap a
        luminance-12 source at ~42; the extreme-low-light allowance must
        let it end up meaningfully brighter than that."""
        source = np.full((80, 80, 3), 12, dtype=np.uint8)
        zero_dce.return_value = (
            np.full_like(source, 220),
            {"provider": "official_zero_dce_onnx_opencv_dnn", "curve_iterations": 8},
        )

        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertTrue(parameters["zero_dce"]["applied"])
        self.assertGreater(float(enhanced.mean()), 60.0)

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_dark_image_routes_through_genuine_zero_dce_provider(self, zero_dce):
        source = np.full((80, 80, 3), 35, dtype=np.uint8)
        zero_dce.return_value = (
            np.full_like(source, 95),
            {"provider": "official_zero_dce_onnx_opencv_dnn", "curve_iterations": 8},
        )

        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertTrue(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["algorithm"], "zero_dce_hybrid_v1")
        self.assertEqual(parameters["colour_policy"], "learned_rgb_curves")
        self.assertGreater(float(enhanced.mean()), float(source.mean()))

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_learned_model_cannot_over_brighten_or_shift_source_chroma(
        self, zero_dce
    ):
        source = np.zeros((96, 96, 3), dtype=np.uint8)
        source[:, :] = (35, 55, 85)
        zero_dce.return_value = (
            np.full_like(source, (245, 180, 230)),
            {"provider": "private_facecloud_curve", "curve_iterations": 8},
        )

        enhanced, level, parameters = enhance_bgr(source, EnhancementLevel.MEDIUM)

        before = parameters["quality_before"]
        after = parameters["quality_after"]
        guard = parameters["quality_guard"]
        self.assertEqual(level, EnhancementLevel.MEDIUM)
        self.assertLessEqual(
            after["mean_luminance"] - before["mean_luminance"], 18.5
        )
        self.assertLessEqual(guard["mean_chroma_shift"], 3.5)
        self.assertIn("luminance_gain_limited", guard["reasons"])
        self.assertEqual(
            parameters["zero_dce"]["chroma_protection"],
            "preserve_source_lab_chroma",
        )

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_learned_model_guard_preserves_source_contrast(self, zero_dce):
        source = np.full((96, 96, 3), 40, dtype=np.uint8)
        source[:, 48:] = 100
        zero_dce.return_value = (
            np.full_like(source, 180),
            {"provider": "private_facecloud_curve", "curve_iterations": 8},
        )

        _enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.HIGH)

        before = parameters["quality_before"]["contrast_stddev"]
        after = parameters["quality_after"]["contrast_stddev"]
        self.assertGreaterEqual(after, before * 0.75)
        self.assertIn("contrast_preserved", parameters["quality_guard"]["reasons"])

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=False)
    def test_overexposed_input_is_not_brightened(self):
        source = np.full((96, 96, 3), 225, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(parameters["exposure_mode"], "darken")
        self.assertLessEqual(float(enhanced.mean()), float(source.mean()))

    @override_settings(AI_RESTORATION_ENABLED=False, ZERO_DCE_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.enhance_with_zero_dce")
    def test_zero_dce_failure_uses_auditable_opencv_fallback(self, zero_dce):
        from enhancement.zero_dce import ZeroDCEUnavailable

        zero_dce.side_effect = ZeroDCEUnavailable("model missing")
        source = np.full((80, 80, 3), 35, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertFalse(parameters["zero_dce"]["applied"])
        self.assertEqual(parameters["zero_dce"]["fallback_reason"], "model missing")
        self.assertEqual(parameters["algorithm"], "opencv_adaptive_restoration_router_v2")
        self.assertGreater(float(enhanced.mean()), float(source.mean()))

    def test_well_lit_image_uses_low_policy(self):
        source = np.full((40, 40, 3), 190, dtype=np.uint8)
        _, level, _ = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(level, EnhancementLevel.LOW)

    def test_quality_router_identifies_blur_and_low_resolution(self):
        quality = analyze_image_quality(blurred_photo(200, 200))

        self.assertIn("severe_blur", quality["defects"])
        self.assertIn("low_resolution", quality["defects"])
        self.assertTrue(quality["needs_ai_restoration"])

    def test_a_flat_frame_is_not_called_blurred(self):
        """It used to be "severe blur": a flat frame has nothing to judge focus by."""
        quality = analyze_image_quality(np.full((200, 200, 3), 90, dtype=np.uint8))

        self.assertEqual(quality["blur"]["verdict"], "uncertain")
        self.assertNotIn("severe_blur", quality["defects"])
        self.assertTrue(quality["needs_ai_restoration"])  # still low resolution

    def test_high_policy_uses_quality_guard_metadata(self):
        source = np.full((80, 80, 3), 210, dtype=np.uint8)
        _, level, parameters = enhance_bgr(source, EnhancementLevel.HIGH)

        self.assertEqual(level, EnhancementLevel.HIGH)
        self.assertIn("quality_before", parameters)
        self.assertIn("quality_after", parameters)
        self.assertIn("quality_guard", parameters)
        self.assertEqual(
            parameters["quality_guard"]["policy"],
            "adaptive_natural_output_guard_v2",
        )

    def test_gallery_manual_controls_preserve_dimensions_and_limit_clipping(self):
        source = np.zeros((120, 180, 3), dtype=np.uint8)
        source[:, :90] = (35, 55, 75)
        source[:, 90:] = (200, 215, 230)

        rendered, metadata = apply_gallery_adjustments(
            source,
            EditMode.MANUAL,
            {
                "brightness": 100,
                "contrast": 50,
                "shadows": 35,
                "saturation": 25,
                "highlights": 60,
            },
        )

        self.assertEqual(rendered.shape, source.shape)
        self.assertTrue(metadata["dimensions_preserved"])
        self.assertLessEqual(
            metadata["quality_after"]["highlight_clipping_percent"],
            max(
                5.0,
                metadata["quality_before"]["highlight_clipping_percent"] + 2.0,
            ),
        )

    def test_gallery_warmth_shifts_the_blue_yellow_axis(self):
        source = np.full((80, 80, 3), 128, dtype=np.uint8)

        warmer, _metadata = apply_gallery_adjustments(source, EditMode.MANUAL, {"warmth": 80})
        cooler, _metadata = apply_gallery_adjustments(source, EditMode.MANUAL, {"warmth": -80})

        warmer_b = cv2.cvtColor(warmer, cv2.COLOR_BGR2LAB)[:, :, 2].mean()
        cooler_b = cv2.cvtColor(cooler, cv2.COLOR_BGR2LAB)[:, :, 2].mean()
        source_b = cv2.cvtColor(source, cv2.COLOR_BGR2LAB)[:, :, 2].mean()
        self.assertGreater(warmer_b, source_b)
        self.assertLess(cooler_b, source_b)

    def test_gallery_intensity_blends_toward_the_preprocessed_original(self):
        baseline = np.full((80, 80, 3), 60, dtype=np.uint8)
        source = np.full((80, 80, 3), 200, dtype=np.uint8)

        full, full_metadata = apply_gallery_adjustments(
            source, EditMode.MANUAL, {"intensity": 100}, baseline=baseline,
        )
        half, half_metadata = apply_gallery_adjustments(
            source, EditMode.MANUAL, {"intensity": 50}, baseline=baseline,
        )
        zero, zero_metadata = apply_gallery_adjustments(
            source, EditMode.MANUAL, {"intensity": 0}, baseline=baseline,
        )

        self.assertFalse(full_metadata["intensity"]["applied"])
        self.assertTrue(half_metadata["intensity"]["applied"])
        self.assertTrue(zero_metadata["intensity"]["applied"])
        self.assertAlmostEqual(float(full.mean()), 200.0, delta=1.0)
        self.assertAlmostEqual(float(zero.mean()), 60.0, delta=1.0)
        self.assertAlmostEqual(float(half.mean()), 130.0, delta=2.0)

    def test_gallery_auto_mode_is_scene_aware_and_conservative(self):
        source = np.full((100, 100, 3), 210, dtype=np.uint8)

        rendered, metadata = apply_gallery_adjustments(source, EditMode.AUTO)

        self.assertEqual(rendered.shape, source.shape)
        self.assertLessEqual(abs(float(rendered.mean()) - float(source.mean())), 20)
        self.assertLessEqual(metadata["requested"]["brightness"], 0)

    def test_quality_comparison_normalizes_output_to_source_dimensions(self):
        source = np.zeros((90, 120, 3), dtype=np.uint8)
        cv2.rectangle(source, (20, 20), (100, 70), (220, 220, 220), 2)
        enlarged = cv2.resize(source, (240, 180), interpolation=cv2.INTER_CUBIC)

        comparison = compare_image_quality(source, enlarged)

        self.assertEqual(comparison["method"], "source_resolution_normalized_v1")
        self.assertEqual(comparison["reference_dimensions"], [120, 90])
        self.assertEqual(comparison["before"]["width"], 120)
        self.assertEqual(comparison["after"]["width"], 120)
        self.assertIn("laplacian_sharpness", comparison["deltas"])

    @override_settings(AI_RESTORATION_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services._load_edsr_model")
    def test_low_resolution_image_is_restored_at_two_times_scale(self, load_model):
        source = np.full((120, 100, 3), 90, dtype=np.uint8)
        model = Mock()
        model.upsample.return_value = cv2.resize(
            source, (200, 240), interpolation=cv2.INTER_CUBIC
        )
        load_model.return_value = model

        restored, metadata = restore_low_resolution(
            source, analyze_image_quality(source)
        )

        self.assertTrue(metadata["requested"])
        self.assertTrue(metadata["applied"])
        self.assertEqual(restored.shape[:2], (240, 200))
        self.assertEqual(metadata["input_dimensions"], [100, 120])
        self.assertEqual(metadata["output_dimensions"], [200, 240])

    @override_settings(
        AI_RESTORATION_ENABLED=True,
        AI_RESTORATION_MAX_INPUT_EDGE=480,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services._load_edsr_model")
    def test_medium_resolution_image_preserves_original_grid(self, load_model):
        source = np.full((1194, 670, 3), 90, dtype=np.uint8)
        quality = analyze_image_quality(source)
        quality["defects"] = sorted(set(quality["defects"]) | {"low_resolution"})

        restored, metadata = restore_low_resolution(source, quality)

        self.assertIs(restored, source)
        self.assertTrue(metadata["requested"])
        self.assertFalse(metadata["applied"])
        self.assertEqual(metadata["maximum_input_edge"], 480)
        self.assertIn("original pixel grid", metadata["fallback_reason"])
        load_model.assert_not_called()

    @override_settings(AI_RESTORATION_ENABLED=True, ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services._load_edsr_model")
    def test_restoration_failure_falls_back_without_breaking_enhancement(
        self, load_model
    ):
        load_model.side_effect = RuntimeError("EDSR runtime unavailable")
        source = np.full((120, 100, 3), 90, dtype=np.uint8)

        enhanced, _, parameters = enhance_bgr(source, EnhancementLevel.AUTO)

        self.assertEqual(enhanced.shape, source.shape)
        self.assertFalse(parameters["restoration"]["applied"])
        self.assertEqual(
            parameters["restoration"]["fallback_reason"],
            "EDSR runtime unavailable",
        )
        self.assertTrue(parameters["warnings"])


@override_settings(
    MEDIA_ROOT=tempfile.mkdtemp(),
    AI_RESTORATION_ENABLED=False,
    ZERO_DCE_ENABLED=False,
    GFPGAN_ENABLED=False,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class EnhancementProcessingAndAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="opencv-owner", email="opencv@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.other = User.objects.create_user(
            username="opencv-other", email="opencv-other@example.com",
            password="ForestRiver!4821", is_email_verified=True,
        )
        self.batch = UploadBatch.objects.create(owner=self.user, upload_type="single")
        success, encoded = cv2.imencode(".jpg", np.full((64, 64, 3), 40, dtype=np.uint8))
        self.assertTrue(success)
        self.image = ImageUpload.objects.create(
            owner=self.user, batch=self.batch, original_name="dark.jpg",
            file_extension=".jpg", mime_type="image/jpeg", file_size=len(encoded.tobytes()),
            width=64, height=64, status="uploaded",
        )
        self.image.original_file.save("dark.jpg", ContentFile(encoded.tobytes()))
        self.preprocessing = ImagePreprocessingResult.objects.create(
            owner=self.user, image=self.image, status="completed", output_format="jpeg",
            output_name="dark.jpg", output_width=64, output_height=64,
            processed_file_size=len(encoded.tobytes()),
        )
        self.preprocessing.processed_file.save("dark.jpg", ContentFile(encoded.tobytes()))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_processing_creates_separate_enhanced_output(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.applied_level, EnhancementLevel.HIGH)
        self.assertTrue(result.enhanced_file.name)
        self.assertNotEqual(result.enhanced_file.name, self.preprocessing.processed_file.name)
        self.assertGreater(result.output_file_size, 0)

    def test_quality_rejected_image_is_still_enhanced_with_a_warning(self):
        from quality_control.models import ImageQualityDecision

        ImageQualityDecision.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            status=ImageQualityDecision.Status.COMPLETED,
            decision=ImageQualityDecision.Decision.REJECTED,
            defects=["severe_low_light"],
            rejection_reasons=["severe_underexposure"],
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.user, image=self.image, preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.enhanced_file.name)
        self.assertTrue(result.applied_parameters["quality_gate"]["bypassed"])
        self.assertEqual(result.applied_parameters["quality_gate"]["decision"], "rejected")
        self.assertTrue(
            any("quality checks" in warning for warning in result.applied_parameters["warnings"])
        )

    def test_enhancement_uses_original_pixel_grid_instead_of_small_proxy(self):
        success, full = cv2.imencode(
            ".jpg", np.full((96, 160, 3), 90, dtype=np.uint8)
        )
        self.assertTrue(success)
        self.image.original_file.delete(save=False)
        self.image.original_file.save("full.jpg", ContentFile(full.tobytes()))
        self.image.width = 160
        self.image.height = 96
        self.image.save(update_fields=["width", "height", "updated_at"])
        success, proxy = cv2.imencode(
            ".jpg", np.full((32, 48, 3), 90, dtype=np.uint8)
        )
        self.assertTrue(success)
        self.preprocessing.processed_file.delete(save=False)
        self.preprocessing.processed_file.save(
            "proxy.jpg", ContentFile(proxy.tobytes())
        )
        self.preprocessing.output_width = 48
        self.preprocessing.output_height = 32
        self.preprocessing.save(
            update_fields=["output_width", "output_height", "updated_at"]
        )
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.LOW,
        )

        result = process_enhancement_result(result)

        self.assertEqual((result.output_width, result.output_height), (160, 96))
        self.assertEqual(
            result.applied_parameters["source_resolution_strategy"],
            "full_resolution_original",
        )

    @patch("enhancement.views.process_enhancement_image.apply_async")
    def test_owner_can_queue_enhancement_and_other_user_cannot_read_or_download(
        self, apply_async
    ):
        create = self.client.post(
            reverse("enhancement:image-enhance", kwargs={"image_id": self.image.pk}),
            {"level": "medium"}, format="json",
        )
        self.assertEqual(create.status_code, 202)
        result_id = create.data["id"]
        self.assertEqual(create.data["status"], "queued")
        self.assertTrue(create.data["task_id"])
        self.assertIsNone(create.data["download_url"])
        apply_async.assert_called_once()

        task_result = process_enhancement_image.run(result_id)
        self.assertEqual(task_result["status"], "completed")

        self.client.force_authenticate(self.other)
        detail = self.client.get(reverse("enhancement:result-detail", kwargs={"pk": result_id}))
        download = self.client.get(reverse("enhancement:result-download", kwargs={"pk": result_id}))
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(download.status_code, 404)

    def test_owner_receives_normalized_comparison_contract(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)

        response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": result.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["result_id"], result.pk)
        self.assertEqual(response.data["image_id"], self.image.pk)
        self.assertEqual(response.data["status"], "completed")
        self.assertTrue(response.data["algorithm"])
        self.assertEqual(
            response.data["comparison"]["method"],
            "source_resolution_normalized_v1",
        )
        self.assertEqual(
            response.data["comparison"]["reference_dimensions"], [64, 64]
        )
        self.assertEqual(
            set(response.data["comparison"]["metrics"]),
            {
                "brightness", "contrast", "sharpness", "noise",
                "shadow_clipping", "highlight_clipping",
            },
        )
        self.assertIn("/api/images/", response.data["original"]["download_url"])
        self.assertIn("/preprocessing/download/", response.data["standardized"]["download_url"])
        self.assertIn("/api/enhancements/results/", response.data["enhanced"]["download_url"])
        self.assertNotIn("users/", str(response.data))

    @patch("enhancement.views.process_gallery_edit.apply_async")
    def test_owner_can_render_versioned_gallery_edit_and_other_user_cannot_access(
        self, apply_async
    ):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        create = self.client.post(
            reverse(
                "enhancement:edit-list-create", kwargs={"result_id": result.pk}
            ),
            {
                "mode": "manual",
                "adjustments": {
                    "brightness": 12,
                    "contrast": 8,
                    "shadows": 10,
                    "saturation": 5,
                    "highlights": -6,
                },
            },
            format="json",
        )
        self.assertEqual(create.status_code, 202)
        self.assertEqual(create.data["status"], "queued")
        edit_id = create.data["id"]
        apply_async.assert_called_once()

        task_result = process_gallery_edit.run(edit_id)
        self.assertEqual(task_result["status"], "completed")
        edit = ImageEditVersion.objects.get(pk=edit_id)
        self.assertTrue(edit.is_current)
        self.assertEqual(
            (edit.output_width, edit.output_height),
            (result.output_width, result.output_height),
        )
        self.assertTrue(edit.edited_file)

        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.get(
                reverse("enhancement:edit-detail", kwargs={"pk": edit_id})
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("enhancement:edit-download", kwargs={"pk": edit_id})
            ).status_code,
            404,
        )

    def test_editor_configuration_exposes_auto_and_manual_controls(self):
        response = self.client.get(reverse("enhancement:editor-configuration"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["non_destructive"])
        self.assertEqual(response.data["modes"], ["auto", "manual"])
        self.assertEqual(
            set(response.data["controls"]),
            {
                "brightness", "contrast", "shadows", "saturation", "highlights",
                "warmth", "intensity",
            },
        )
        self.assertEqual(response.data["controls"]["intensity"]["default"], 100)

    def test_comparison_is_private_and_requires_authentication(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        url = reverse("enhancement:result-comparison", kwargs={"pk": result.pk})

        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_comparison_rejects_incomplete_and_missing_file_results(self):
        pending = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        pending_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(pending_response.status_code, 409)
        self.assertEqual(pending_response.data["status"], "pending")

        pending.status = "failed"
        pending.failure_reason = "Safe processing failure."
        pending.save(update_fields=["status", "failure_reason", "updated_at"])
        failed_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(failed_response.status_code, 409)
        self.assertEqual(failed_response.data["status"], "failed")
        self.assertEqual(
            failed_response.data["failure_reason"], "Safe processing failure."
        )

        pending.status = "completed"
        pending.applied_parameters = {
            "quality_comparison": {"method": "source_resolution_normalized_v1"}
        }
        pending.save(update_fields=["status", "applied_parameters", "updated_at"])
        missing_response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": pending.pk})
        )
        self.assertEqual(missing_response.status_code, 410)

    def test_comparison_requires_normalized_measurements(self):
        result = ImageEnhancementResult.objects.create(
            owner=self.user,
            image=self.image,
            preprocessing_result=self.preprocessing,
            requested_level=EnhancementLevel.AUTO,
        )
        result = process_enhancement_result(result)
        result.applied_parameters = {}
        result.save(update_fields=["applied_parameters", "updated_at"])

        response = self.client.get(
            reverse("enhancement:result-comparison", kwargs={"pk": result.pk})
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("measurements", response.data["detail"].lower())

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        CELERY_TASK_EAGER_PROPAGATES=True,
    )
    def test_batch_api_processes_ready_images_and_reports_progress(self):
        response = self.client.post(
            reverse("enhancement:batch-enhance", kwargs={"batch_id": self.batch.pk}),
            {"level": "auto"}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["total_images"], 1)
        self.assertEqual(response.data["successful_images"], 1)
        self.assertEqual(response.data["progress_percentage"], 100.0)
