"""The backend's Zero-DCE applies the curves the way the paper says: eight iterations of ``x = x + r * (x^2 - x)`` on RGB in [0, 1], three curve channels per iteration.

The ONNX holds only DCE-Net (the official Epoch99 weights, seven convolutions, a tanh): its 24 output channels are the curve parameters, and ``enhancement/zero_dce.py`` applies them in NumPy, tile by
tile, at the photograph's full resolution (the curves are predicted at most ``ZERO_DCE_MAX_INFERENCE_EDGE`` wide and interpolated). A review on 2026-11-06 asked whether a gamma or an ``addWeighted`` stood
in for it; measured with the real network on 47.jpg, the adapter and an independent implementation of the formula differ by 0 grey levels (414x277) and 1 (the interpolating path), and these tests keep it so:
the network here is a stand-in returning known curves, the reference is written in the test.
"""
import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings
from unittest.mock import patch

from . import zero_dce
from .adaptive import operations as ops


class FakeNetwork:
    """What ``cv2.dnn`` hands back: ``forward()`` is the curve tensor (1, 24, h, w), whatever the input."""

    def __init__(self, curves):
        self.curves, self.inputs = curves, []

    def setInput(self, blob):
        self.inputs.append(blob)

    def forward(self):
        return self.curves


def run(image, curves):
    network = FakeNetwork(curves)
    with patch.object(zero_dce, "_load_zero_dce_model", return_value=network):
        output, metadata = zero_dce.enhance_with_zero_dce(image)
    return output, metadata, network


def paper(image, curves, size=None):
    """The paper's inference, written here and not in the adapter: bilinear curves to the photograph's size, eight iterations, RGB in [0, 1]."""
    height, width = image.shape[:2]
    x = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    for iteration in range(8):
        r = curves[0, 3 * iteration:3 * iteration + 3].transpose(1, 2, 0)
        if r.shape[:2] != (height, width):
            r = cv2.resize(r, (width, height), interpolation=cv2.INTER_LINEAR)
        x = np.clip(x + r * (x * x - x), 0.0, 1.0)
    return cv2.cvtColor(np.rint(x * 255.0).astype(np.uint8), cv2.COLOR_RGB2BGR)


def curves_for(height, width, seed=1, low=-0.5, high=0.15):
    return np.random.default_rng(seed).uniform(low, high, (1, 24, height, width)).astype(np.float32)


def photograph(height=48, width=64, seed=2):
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


class PaperFormulaTests(SimpleTestCase):
    def test_the_output_is_eight_iterations_of_the_curve_formula(self):
        image, curves = photograph(), curves_for(48, 64)
        output, _metadata, _network = run(image, curves)
        np.testing.assert_array_equal(output, paper(image, curves))

    def test_a_single_pixel_follows_the_recurrence_exactly(self):
        """x = 0.2 with r = -0.3 everywhere: x_{n+1} = x_n + r (x_n^2 - x_n), eight times: not a gamma, not a blend."""
        image = np.full((4, 4, 3), round(0.2 * 255), np.uint8)
        output, _metadata, _network = run(image, np.full((1, 24, 4, 4), -0.3, np.float32))
        x = np.float32(round(0.2 * 255) / 255.0)
        for _ in range(8):
            x = np.float32(x + np.float32(-0.3) * (x * x - x))
        self.assertTrue((output == int(np.rint(x * 255.0))).all(), (output[0, 0], x * 255.0))
        self.assertNotEqual(int(output[0, 0, 0]), int(round(255 * (0.2 ** 0.6))))                          # a gamma of 0.6 would give another number

    def test_it_is_not_a_gamma_curve_or_a_blend_of_the_input(self):
        image, curves = photograph(), curves_for(48, 64)
        output, _metadata, _network = run(image, curves)
        gamma = np.clip(255.0 * np.power(image / 255.0, 0.6), 0, 255).astype(np.uint8)
        blend = cv2.addWeighted(image, 0.5, output, 0.5, 0)
        self.assertGreater(int(np.abs(output.astype(np.int16) - gamma).max()), 10)
        self.assertGreater(int(np.abs(output.astype(np.int16) - blend).max()), 10)

    def test_every_one_of_the_24_channels_is_used_three_per_iteration_in_rgb_order(self):
        image = photograph()
        for channel, bgr_index in ((21, 2), (22, 1), (23, 0)):                  # the last iteration's R, G, B channels: B is the first channel of a BGR image
            with self.subTest(channel=channel):
                curves = np.zeros((1, 24, 48, 64), np.float32)
                curves[:, channel] = -0.5
                output, _metadata, _network = run(image, curves)
                changed = [bool((output[:, :, c] != image[:, :, c]).any()) for c in range(3)]
                self.assertEqual(changed, [index == bgr_index for index in range(3)])

    def test_with_no_curves_the_photograph_is_returned_as_it_is(self):
        image = photograph()
        output, _metadata, _network = run(image, np.zeros((1, 24, 48, 64), np.float32))
        np.testing.assert_array_equal(output, image)

    def test_a_negative_curve_brightens_and_a_positive_one_darkens(self):
        image = np.full((8, 8, 3), 60, np.uint8)
        brighter, *_ = run(image, np.full((1, 24, 8, 8), -0.3, np.float32))
        darker, *_ = run(image, np.full((1, 24, 8, 8), 0.3, np.float32))
        self.assertGreater(int(brighter.mean()), 60)
        self.assertLess(int(darker.mean()), 60)

    def test_the_curves_are_applied_at_the_full_resolution_whatever_the_tile_size(self):
        image, curves = photograph(150, 200), curves_for(150, 200)
        with override_settings(ZERO_DCE_FULL_RESOLUTION_TILE_EDGE=128):
            tiled, *_ = run(image, curves)
        with override_settings(ZERO_DCE_FULL_RESOLUTION_TILE_EDGE=4096):
            whole, *_ = run(image, curves)
        np.testing.assert_array_equal(tiled, whole)
        np.testing.assert_array_equal(whole, paper(image, curves))

    def test_curves_predicted_at_a_smaller_size_are_interpolated_to_the_photograph(self):
        image = photograph(48, 64)
        curves = curves_for(24, 32, seed=5)                                      # what the network returns for a 32 x 24 inference of a 64 x 48 photograph
        with override_settings(ZERO_DCE_MAX_INFERENCE_EDGE=32):
            output, metadata, network = run(image, curves)
        self.assertEqual(metadata["inference_dimensions"], [32, 24])
        self.assertEqual(network.inputs[0].shape, (1, 3, 24, 32))                 # the network saw the reduced photograph, the curves are applied to the full one
        self.assertLessEqual(int(np.abs(output.astype(np.int16) - paper(image, curves).astype(np.int16)).max()), 1)

    def test_the_inference_size_is_the_photographs_up_to_the_limit_and_reduced_beyond_it(self):
        image = photograph(48, 64)
        for limit, expected in ((64, [64, 48]), (65, [64, 48]), (63, [63, 47])):
            with self.subTest(limit=limit), override_settings(ZERO_DCE_MAX_INFERENCE_EDGE=limit):
                curves = curves_for(expected[1], expected[0])
                _output, metadata, network = run(image, curves)
                self.assertEqual(metadata["inference_dimensions"], expected)
                self.assertEqual(network.inputs[0].shape, (1, 3, expected[1], expected[0]))

    def test_the_network_gets_the_photograph_as_rgb_in_zero_to_one(self):
        image = photograph()
        _output, _metadata, network = run(image, curves_for(48, 64))
        expected = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        np.testing.assert_allclose(network.inputs[0], expected, atol=1e-6)

    def test_the_result_says_what_ran(self):
        _output, metadata, _network = run(photograph(), curves_for(48, 64))
        self.assertEqual(metadata["curve_iterations"], 8)
        self.assertEqual(metadata["input_dimensions"], [64, 48])
        self.assertEqual(metadata["resolution_policy"], "proxy_curve_prediction_full_resolution_application")


class RefusalTests(SimpleTestCase):
    def test_a_curve_tensor_that_is_not_eight_iterations_of_three_channels_is_refused(self):
        image = photograph()
        for shape in ((1, 23, 48, 64), (1, 21, 48, 64), (1, 25, 48, 64), (2, 24, 48, 64), (24, 48, 64)):
            with self.subTest(shape=shape), patch.object(zero_dce, "_load_zero_dce_model", return_value=FakeNetwork(np.zeros(shape, np.float32))):
                with self.assertRaises(zero_dce.ZeroDCEUnavailable):
                    zero_dce.enhance_with_zero_dce(image)

    def test_an_image_that_is_not_a_colour_photograph_is_refused_before_the_network_is_asked(self):
        network = FakeNetwork(np.zeros((1, 24, 8, 8), np.float32))
        for image in (None, np.zeros((0, 0, 3), np.uint8), np.zeros((8, 8), np.uint8), np.zeros((8, 8, 4), np.uint8)):
            with self.subTest(image=None if image is None else image.shape), patch.object(zero_dce, "_load_zero_dce_model", return_value=network):
                with self.assertRaises(zero_dce.ZeroDCEUnavailable):
                    zero_dce.enhance_with_zero_dce(image)
        self.assertEqual(network.inputs, [])


class ClassicalEngineTests(SimpleTestCase):
    def test_the_classical_exposure_engine_names_itself_and_is_not_called_zero_dce(self):
        """If the model cannot run the engine says so: a gamma is recorded as the classical engine it is."""
        _image, models = ops.run_low_light(np.full((16, 16, 3), 40, np.uint8), "opencv_exposure", 1.0)
        self.assertEqual([item["model"] for item in models], ["opencv_exposure"])
        self.assertEqual(models[0].get("provider", "opencv_gamma_0_6"), "opencv_gamma_0_6")
