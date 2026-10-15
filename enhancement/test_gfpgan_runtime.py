"""The GFPGAN stack must import on a current torchvision.

Skipped where torchvision is not installed (it lives only in the worker
environment). Real-checkpoint validation is scripts/validate_models.py.
"""
import importlib.util
import sys
import unittest

from django.test import SimpleTestCase

TORCHVISION = (
    importlib.util.find_spec("torch") is not None and importlib.util.find_spec("torchvision") is not None
)


@unittest.skipUnless(TORCHVISION, "torchvision is not installed here.")
class RemovedTorchvisionModuleTests(SimpleTestCase):
    """BasicSR imports a private torchvision module that current torchvision lacks."""

    name = "torchvision.transforms.functional_tensor"

    def setUp(self):
        self.saved = sys.modules.pop(self.name, None)
        self.addCleanup(self.restore)

    def restore(self):
        sys.modules.pop(self.name, None)
        if self.saved is not None:
            sys.modules[self.name] = self.saved

    def really_missing(self):
        return importlib.util.find_spec(self.name) is None

    def test_the_missing_name_is_supplied_from_its_current_home(self):
        from torchvision.transforms import functional

        from .gfpgan import _restore_removed_torchvision_module

        if not self.really_missing():
            self.skipTest("this torchvision still ships the module")
        self.assertTrue(_restore_removed_torchvision_module())
        module = importlib.import_module(self.name)
        self.assertIs(module.rgb_to_grayscale, functional.rgb_to_grayscale)

    def test_it_does_nothing_when_the_module_is_already_there(self):
        import types

        from .gfpgan import _restore_removed_torchvision_module

        existing = types.ModuleType(self.name)
        sys.modules[self.name] = existing
        self.assertFalse(_restore_removed_torchvision_module())
        self.assertIs(sys.modules[self.name], existing)

    def test_the_supplied_function_works(self):
        import torch

        from .gfpgan import _restore_removed_torchvision_module

        _restore_removed_torchvision_module()
        gray = importlib.import_module(self.name).rgb_to_grayscale(torch.rand(1, 3, 8, 8))
        self.assertEqual(tuple(gray.shape), (1, 1, 8, 8))
