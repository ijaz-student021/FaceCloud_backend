"""Tests for the shared inference runtime (device choice, tiling, memory fallback).

``FakeTorch`` tests need no PyTorch and run everywhere. ``RealTorchTests`` run the
real tensor code on whatever device this machine has, with tiny stand-in models:
they prove the tiling, device and memory paths, not the quality of any checkpoint.
"""
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from . import runtime
from .hd_restoration import HDRestorationUnavailable, deblur_with_nafnet, upscale_with_realesrgan

TORCH_INSTALLED = importlib.util.find_spec("torch") is not None


class Unavailable(RuntimeError):
    pass


class FakeOutOfMemoryError(RuntimeError):
    pass


FakeOutOfMemoryError.__name__ = "OutOfMemoryError"


def fake_torch(cuda=False, mps=False, has_mps_backend=True):
    backends = SimpleNamespace()
    if has_mps_backend:
        backends.mps = SimpleNamespace(is_available=lambda: mps)

    def device(name):
        if name not in {"cpu", "cuda", "cuda:0", "mps"}:
            raise RuntimeError(f"Expected one of cpu, cuda, mps device type: {name}")
        return SimpleNamespace(type=name.split(":")[0], __str__=lambda self=None: name)

    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda), backends=backends, device=device,
    )


class DeviceSelectionTests(SimpleTestCase):
    def test_auto_prefers_cuda_then_mps_then_cpu(self):
        self.assertEqual(runtime.auto_device(fake_torch(cuda=True, mps=True)), "cuda")
        self.assertEqual(runtime.auto_device(fake_torch(mps=True)), "mps")
        self.assertEqual(runtime.auto_device(fake_torch()), "cpu")

    def test_a_runtime_built_without_mps_support_falls_to_cpu_instead_of_crashing(self):
        self.assertEqual(runtime.auto_device(fake_torch(has_mps_backend=False)), "cpu")

    def test_auto_resolves_to_the_cpu_when_there_is_no_gpu(self):
        device = runtime.resolve_device(fake_torch(), "auto", "PureScale", Unavailable)
        self.assertEqual(device.type, "cpu")

    def test_an_explicit_gpu_that_is_missing_is_refused_not_silently_replaced(self):
        with self.assertRaisesRegex(Unavailable, "CUDA device is unavailable"):
            runtime.resolve_device(fake_torch(), "cuda", "PureScale", Unavailable)
        with self.assertRaisesRegex(Unavailable, "CUDA device is unavailable"):
            runtime.resolve_device(fake_torch(), "cuda:0", "PureScale", Unavailable)
        with self.assertRaisesRegex(Unavailable, "MPS device is unavailable"):
            runtime.resolve_device(fake_torch(), "mps", "PureScale", Unavailable)

    def test_an_invalid_device_name_is_refused(self):
        with self.assertRaisesRegex(Unavailable, "device is invalid"):
            runtime.resolve_device(fake_torch(), "quantum", "PureScale", Unavailable)


class OutOfMemoryDetectionTests(SimpleTestCase):
    def test_recognises_every_form_of_out_of_memory(self):
        self.assertTrue(runtime.is_out_of_memory(MemoryError()))
        self.assertTrue(runtime.is_out_of_memory(FakeOutOfMemoryError("CUDA")))
        self.assertTrue(runtime.is_out_of_memory(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")))
        self.assertTrue(runtime.is_out_of_memory(RuntimeError("MPS backend out of memory (MPS allocated: 9 GB)")))

    def test_other_errors_are_not_mistaken_for_it(self):
        self.assertFalse(runtime.is_out_of_memory(RuntimeError("size mismatch for conv_first.weight")))
        self.assertFalse(runtime.is_out_of_memory(ValueError("out of memory")))


@override_settings(ENHANCEMENT_OOM_MAX_RETRIES=2, ENHANCEMENT_MIN_TILE_SIZE=64)
class MemoryFallbackTests(SimpleTestCase):
    def run_fallback(self, run, tile=256, overlap=24):
        return runtime.run_with_memory_fallback(
            run, device="cuda", tile=tile, overlap=overlap, label="Real-ESRGAN", error_class=Unavailable,
        )

    def test_success_records_the_device_and_tile_actually_used(self):
        output, metadata = self.run_fallback(lambda tile, overlap: "pixels")
        self.assertEqual(output, "pixels")
        self.assertEqual(metadata, {"device": "cuda", "tile_size": 256, "tile_overlap": 24, "memory_retries": 0,
                                    "tiles": 0, "tiles_rejected": 0})

    def test_out_of_memory_retries_with_smaller_tiles(self):
        tiles = []

        def run(tile, overlap):
            tiles.append((tile, overlap))
            if tile > 64:
                raise RuntimeError("CUDA out of memory")
            return "pixels"

        with self.assertLogs("enhancement.runtime", level="WARNING"):
            output, metadata = self.run_fallback(run)

        self.assertEqual(tiles, [(256, 24), (128, 24), (64, 16)])
        self.assertEqual(output, "pixels")
        self.assertEqual((metadata["tile_size"], metadata["memory_retries"]), (64, 2))

    def test_retries_are_bounded_and_end_in_the_adapters_own_error(self):
        calls = []

        def run(tile, overlap):
            calls.append(tile)
            raise FakeOutOfMemoryError("CUDA out of memory")

        with self.assertLogs("enhancement.runtime", level="WARNING"):
            with self.assertRaisesRegex(Unavailable, "ran out of memory on cuda"):
                self.run_fallback(run, tile=1024)

        self.assertEqual(calls, [1024, 512, 256])  # the first try plus two retries, no more

    def test_it_never_goes_below_the_minimum_tile(self):
        calls = []

        def run(tile, overlap):
            calls.append(tile)
            raise MemoryError()

        with self.assertLogs("enhancement.runtime", level="WARNING"):
            with self.assertRaises(Unavailable):
                self.run_fallback(run, tile=100)

        self.assertEqual(calls, [100, 64])

    def test_any_other_inference_failure_becomes_the_adapters_error(self):
        def run(tile, overlap):
            raise RuntimeError("Given groups=1, weight of size [64, 3, 3, 3], expected input to have 3 channels")

        with self.assertLogs("enhancement.runtime", level="ERROR"):
            with self.assertRaisesRegex(Unavailable, "inference failed safely"):
                self.run_fallback(run)

    def test_the_adapters_own_refusal_passes_straight_through(self):
        def run(tile, overlap):
            raise Unavailable("The checkpoint is unavailable.")

        with self.assertRaisesRegex(Unavailable, "checkpoint is unavailable"):
            self.run_fallback(run)


class AdapterMemoryPathTests(SimpleTestCase):
    """Each tiled adapter keeps the pipeline alive when the device runs out of memory."""

    source = np.full((64, 96, 3), 110, dtype=np.uint8)

    def exhausted(self, tiled_path, load_path, call, error_class):
        with (
            patch(load_path, return_value=(object(), "cuda")),
            patch(tiled_path, side_effect=RuntimeError("CUDA out of memory")) as tiled,
            self.assertLogs("enhancement.runtime", level="WARNING"),
            self.assertRaisesRegex(error_class, "ran out of memory"),
        ):
            call(self.source)
        return tiled

    def test_realesrgan(self):
        tiled = self.exhausted(
            "enhancement.hd_restoration._tiled_inference", "enhancement.hd_restoration._load_model",
            upscale_with_realesrgan, HDRestorationUnavailable,
        )
        self.assertEqual(tiled.call_count, 3)

    def test_nafnet(self):
        self.exhausted(
            "enhancement.hd_restoration._tiled_inference", "enhancement.hd_restoration._load_model",
            deblur_with_nafnet, HDRestorationUnavailable,
        )

    def test_a_retry_at_a_smaller_tile_succeeds_and_is_recorded(self):
        doubled = cv2.resize(self.source, (192, 128), interpolation=cv2.INTER_CUBIC)
        with (
            patch("enhancement.hd_restoration._load_model", return_value=(object(), "cuda")),
            patch(
                "enhancement.hd_restoration._tiled_inference",
                side_effect=[RuntimeError("CUDA out of memory"), doubled],
            ),
            self.assertLogs("enhancement.runtime", level="WARNING"),
        ):
            output, metadata = upscale_with_realesrgan(self.source)

        self.assertEqual(output.shape, (128, 192, 3))
        self.assertEqual((metadata["device"], metadata["tile_size"], metadata["memory_retries"]), ("cuda", 128, 1))

    @override_settings(HD_RESTORATION_OUTPUT_SCALE=2, HD_RESTORATION_MAX_OUTPUT_PIXELS=1000)
    def test_realesrgan_refuses_an_output_above_the_limit_before_loading_the_model(self):
        with patch("enhancement.hd_restoration._load_model") as load_model:
            with self.assertRaisesRegex(HDRestorationUnavailable, "HD output exceeds"):
                upscale_with_realesrgan(self.source)
        load_model.assert_not_called()

    @override_settings(
        HD_RESTORATION_OUTPUT_SCALE=4, HD_RESTORATION_MAX_OUTPUT_PIXELS=10**9,
        HD_RESTORATION_MAX_NATIVE_OUTPUT_PIXELS=1000,
    )
    def test_the_native_limit_still_applies_when_the_native_result_is_the_output(self):
        with patch("enhancement.hd_restoration._load_model") as load_model:
            with self.assertRaisesRegex(HDRestorationUnavailable, "native 4x output exceeds"):
                upscale_with_realesrgan(self.source)
        load_model.assert_not_called()


@unittest.skipUnless(TORCH_INSTALLED, "PyTorch is not installed here.")
class RealTorchTests(SimpleTestCase):
    """Real tensors on this machine's device, with stand-in models (not checkpoints)."""

    def setUp(self):
        import torch

        self.torch = torch
        self.device = torch.device(runtime.auto_device(torch))
        rng = np.random.default_rng(3)
        self.image = rng.integers(0, 256, (150, 210, 3), dtype=np.uint8)

    def model(self, scale):
        torch = self.torch

        class NearestUpscaler(torch.nn.Module):
            def forward(self, value):
                return torch.nn.functional.interpolate(value, scale_factor=scale, mode="nearest")

        return NearestUpscaler().eval().to(self.device)

    def test_tiles_reassemble_into_the_whole_image(self):
        output = runtime.tiled_inference(self.torch, self.image, self.model(1), self.device, 1, 64, 16)
        np.testing.assert_array_equal(output, self.image)

    def test_native_scale_output_matches_an_untiled_upscale(self):
        output = runtime.tiled_inference(self.torch, self.image, self.model(4), self.device, 4, 64, 16)
        expected = cv2.resize(self.image, (840, 600), interpolation=cv2.INTER_NEAREST)
        np.testing.assert_array_equal(output, expected)

    def test_reducing_each_tile_matches_reducing_the_whole_native_result(self):
        """The memory-safe path is the same image, within rounding, as the old one."""
        native = runtime.tiled_inference(self.torch, self.image, self.model(4), self.device, 4, 64, 16)
        whole = cv2.resize(native, (420, 300), interpolation=cv2.INTER_AREA)
        tiled = runtime.tiled_inference(
            self.torch, self.image, self.model(4), self.device, 4, 64, 16, output_scale=2,
        )
        self.assertEqual(tiled.shape, (300, 420, 3))
        self.assertLessEqual(int(np.abs(tiled.astype(np.int16) - whole.astype(np.int16)).max()), 1)

    def test_a_model_that_returns_the_wrong_size_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unexpected tile size"):
            runtime.tiled_inference(self.torch, self.image, self.model(3), self.device, 4, 64, 16)

    def test_a_real_out_of_memory_error_from_the_runtime_is_recognised(self):
        error = self.torch.OutOfMemoryError("CUDA out of memory. Tried to allocate 20.00 MiB")
        self.assertTrue(runtime.is_out_of_memory(error))

    def test_resolve_device_returns_a_device_this_runtime_accepts(self):
        device = runtime.resolve_device(self.torch, "auto", "PureScale", Unavailable)
        self.assertEqual(self.torch.zeros(1, device=device).device.type, device.type)


@unittest.skipUnless(TORCH_INSTALLED, "PyTorch is not installed here.")
class DivergedTileTests(SimpleTestCase):
    """A tile whose raw output leaves [0, 1] far behind is not clamped into noise."""

    def setUp(self):
        import torch

        self.torch = torch
        self.device = torch.device("cpu")
        rng = np.random.default_rng(5)
        self.image = rng.integers(40, 200, (100, 140, 3), dtype=np.uint8)

    def model(self, explode_where_dark=False):
        torch = self.torch

        class Identity(torch.nn.Module):
            def forward(self, value):
                if explode_where_dark and float(value.mean()) < 0.1:
                    return value * 0 + 393.0  # what NAFNet did on a near-black hair tile
                return value

        return Identity().eval()

    def test_a_well_behaved_model_has_no_rejected_tiles(self):
        output = runtime.tiled_inference(self.torch, self.image, self.model(), self.device, 1, 64, 16)
        np.testing.assert_array_equal(output, self.image)
        self.assertEqual(runtime.last_tile_stats()["tiles_rejected"], 0)
        self.assertGreater(runtime.last_tile_stats()["tiles"], 1)

    def test_a_diverged_tile_falls_back_to_its_input(self):
        image = self.image.copy()
        image[:64, :64] = 3  # one near-black tile
        output = runtime.tiled_inference(self.torch, image, self.model(explode_where_dark=True), self.device, 1, 64, 0)
        stats = runtime.last_tile_stats()
        self.assertEqual(stats["tiles_rejected"], 1)
        np.testing.assert_array_equal(output[:64, :64], image[:64, :64])  # not saturated colour noise
        np.testing.assert_array_equal(output, image)

    def test_the_rejection_count_reaches_the_adapters_metadata(self):
        image = self.image.copy()
        image[:64, :64] = 3
        model = self.model(explode_where_dark=True)
        output, metadata = runtime.run_with_memory_fallback(
            lambda tile, overlap: runtime.tiled_inference(self.torch, image, model, self.device, 1, tile, overlap),
            device=self.device, tile=64, overlap=0, label="NAFNet", error_class=Unavailable,
        )
        self.assertEqual(metadata["tiles_rejected"], 1)
        self.assertEqual(output.shape, image.shape)


class ModelResidencyTests(SimpleTestCase):
    """On a small machine only one large model stays loaded; on a large one every model may."""

    def setUp(self):
        self.first, self.second = {"a": object()}, {"b": object(), "c": object()}
        saved = list(runtime._MODEL_CACHES)
        runtime._MODEL_CACHES[:] = [self.first, self.second]
        self.addCleanup(lambda: runtime._MODEL_CACHES.__setitem__(slice(None), saved))

    @override_settings(ENHANCEMENT_MODEL_RESIDENCY="single")
    def test_single_mode_unloads_every_other_model_but_keeps_the_one_being_used(self):
        runtime.make_room(self.second, keep_key="b")
        self.assertEqual((dict(self.first), sorted(self.second)), ({}, ["b"]))

    @override_settings(ENHANCEMENT_MODEL_RESIDENCY="all")
    def test_all_mode_keeps_everything_loaded(self):
        runtime.make_room(self.second)
        self.assertEqual((sorted(self.first), sorted(self.second)), (["a"], ["b", "c"]))

    @override_settings(ENHANCEMENT_MODEL_RESIDENCY="auto")
    def test_auto_mode_follows_the_machines_memory(self):
        with patch.object(runtime, "physical_memory_gib", return_value=8.0):
            self.assertTrue(runtime.single_model_mode())
        with patch.object(runtime, "physical_memory_gib", return_value=32.0):
            self.assertFalse(runtime.single_model_mode())
        with patch.object(runtime, "physical_memory_gib", return_value=None):
            self.assertFalse(runtime.single_model_mode())

    def test_an_adapter_registers_its_cache_once(self):
        cache = {}
        runtime.register_model_cache(cache)
        runtime.register_model_cache(cache)
        self.assertEqual(sum(1 for other in runtime._MODEL_CACHES if other is cache), 1)
