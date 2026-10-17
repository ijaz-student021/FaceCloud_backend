from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from .hd_restoration import HDRestorationUnavailable
from .test_blur_measure import blurred_photo
from .services import enhance_bgr


class HDRestorationPipelineTests(SimpleTestCase):
    @override_settings(
        HD_RESTORATION_ENABLED=False,
        GFPGAN_ENABLED=False,
    )
    def test_disabled_hd_restoration_preserves_existing_pipeline(self):
        source = np.full((64, 96, 3), 100, dtype=np.uint8)
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, source.shape)
        self.assertFalse(parameters["hd_restoration"]["applied"])

    @override_settings(
        HD_RESTORATION_ENABLED=True,
        NAFNET_DEBLUR_ENABLED=True,
        HD_RESTORATION_OUTPUT_SCALE=2,
        GFPGAN_ENABLED=False,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.upscale_with_realesrgan")
    @patch("enhancement.services.deblur_with_nafnet")
    def test_enabled_hd_restoration_returns_auditable_2x_output(self, deblur, upscale):
        source = blurred_photo(64, 96)  # deblurring runs only on measured blur
        deblur.return_value = (source.copy(), {"provider": "nafnet_gopro_width64"})
        upscaled = np.full((128, 192, 3), 110, dtype=np.uint8)
        upscale.return_value = (upscaled, {"provider": "realesrgan_x4plus", "output_scale": 2})
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, (128, 192, 3))
        self.assertTrue(parameters["hd_restoration"]["applied"])
        self.assertIn("nafnet_gopro+realesrgan_x4plus", parameters["algorithm"])

    @override_settings(
        HD_RESTORATION_ENABLED=True,
        NAFNET_DEBLUR_ENABLED=True,
        GFPGAN_ENABLED=False,
    ENHANCEMENT_RENDER_POLICY="learned_v1")
    @patch("enhancement.services.upscale_with_realesrgan")
    @patch("enhancement.services.deblur_with_nafnet")
    def test_deblur_failure_still_allows_safe_super_resolution(self, deblur, upscale):
        source = blurred_photo(64, 96)
        deblur.side_effect = HDRestorationUnavailable("checkpoint unavailable")
        upscaled = np.full((128, 192, 3), 100, dtype=np.uint8)
        upscale.return_value = (upscaled, {"provider": "realesrgan_x4plus"})
        output, _level, parameters = enhance_bgr(source, "auto")
        self.assertEqual(output.shape, (128, 192, 3))
        self.assertTrue(parameters["hd_restoration"]["applied"])
        self.assertFalse(parameters["hd_restoration"]["deblur"]["applied"])
        self.assertEqual(
            parameters["hd_restoration"]["deblur"]["fallback_reason"],
            "checkpoint unavailable",
        )
