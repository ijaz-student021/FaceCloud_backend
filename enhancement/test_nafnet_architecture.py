"""NAFNet's forward pass must match the published architecture.

Real PyTorch code with random weights (no checkpoint), so it is skipped where
PyTorch is not installed. Real-checkpoint validation is scripts/validate_models.py.
"""
import importlib.util
import unittest

from django.test import SimpleTestCase

TORCH = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH, "PyTorch is not installed here.")
class NAFNetArchitectureTests(SimpleTestCase):
    """NAFNet's channel attention must re-weight the features, not replace them."""

    def setUp(self):
        import torch
        from torch import nn
        from torch.nn import functional

        from .hd_restoration import _build_nafnet

        torch.manual_seed(4)
        self.torch = torch
        self.model = _build_nafnet(nn, functional).eval()

    def test_a_block_keeps_spatial_variation_through_its_attention(self):
        """The bug: the block added one value per channel to the whole tile."""
        torch = self.torch
        block = self.model.encoders[0][0]
        with torch.no_grad():
            block.beta.fill_(1.0)
            block.gamma.zero_()
            source = torch.randn(1, 64, 32, 32)
            change = block(source) - source
        per_channel_spread = change.flatten(2).std(dim=2)
        self.assertGreater(float(per_channel_spread.min()), 1e-4)

    def test_the_attention_scales_features_by_their_pooled_summary(self):
        torch = self.torch
        block = self.model.encoders[0][0]
        with torch.no_grad():
            block.beta.fill_(1.0)
            block.gamma.zero_()
            source = torch.randn(1, 64, 16, 16)
            gated = block.sg(block.conv2(block.conv1(block.norm1(source))))
            expected = source + block.conv3(gated * block.sca(gated))
            self.assertTrue(torch.allclose(block(source), expected, atol=1e-5))

    def test_the_network_is_a_residual_on_its_input(self):
        """With the output layer zeroed, the model must return its input unchanged."""
        torch = self.torch
        with torch.no_grad():
            self.model.ending.weight.zero_()
            self.model.ending.bias.zero_()
            source = torch.rand(1, 3, 40, 56)  # not a multiple of 16: padding must be cropped away
            output = self.model(source)
        self.assertEqual(tuple(output.shape), (1, 3, 40, 56))
        self.assertTrue(torch.allclose(output, source, atol=1e-6))

    def test_the_checkpoint_layout_is_unchanged_by_the_fix(self):
        """The fix changed the forward pass only; the pinned checkpoint must still load."""
        keys = list(self.model.state_dict())
        self.assertEqual(len(keys), 664)
        self.assertIn("encoders.3.27.sca.1.weight", keys)
        self.assertIn("ups.0.0.weight", keys)
