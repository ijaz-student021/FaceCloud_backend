"""Decoding uploads into the working format: 8-bit sRGB, oriented, alpha kept.

Real Pillow/LittleCMS decoding of files built here. Expected values come from
independent references (the PNG sample-scaling rule; the published D50
colorants of Adobe RGB (1998) and sRGB), not from the code under test.
"""
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from .decoding import UnsupportedImageData, to_working_colour
from .test_decoding_refs import (
    ADOBE_GAMMA, ADOBE_RGB_D50, adobe_to_srgb_reference, encoded, gradient16, matrix_trc_profile,
)

class HighBitDepthTests(SimpleTestCase):
    def test_a_16_bit_grayscale_png_keeps_its_whole_tonal_range(self):
        """P1-1: it used to be clipped to three grey levels."""
        values = gradient16()
        working, notes = to_working_colour(encoded(Image.fromarray(values)))

        rgb = np.asarray(working)
        self.assertEqual(working.mode, "RGB")
        self.assertEqual(len(np.unique(rgb[..., 0])), 256)
        # PNG samples span the full range, so 16-bit v is 8-bit round(v * 255 / 65535).
        expected = np.rint(values.astype(np.float64) * 255 / 65535).astype(np.uint8)
        np.testing.assert_array_equal(rgb[..., 0], expected)
        np.testing.assert_array_equal(rgb[..., 0], rgb[..., 2])
        self.assertEqual((notes["source_mode"], notes["bits_per_sample"]), ("I;16", 16))

    def test_a_16_bit_grayscale_tiff_too(self):
        values = gradient16()
        working, _ = to_working_colour(encoded(Image.fromarray(values), "TIFF"))
        self.assertEqual(len(np.unique(np.asarray(working)[..., 0])), 256)

    def test_a_32_bit_integer_tiff_holding_16_bit_samples(self):
        values = gradient16().astype(np.int32)
        working, notes = to_working_colour(encoded(Image.fromarray(values), "TIFF"))
        self.assertEqual(len(np.unique(np.asarray(working)[..., 0])), 256)
        self.assertEqual(notes["bits_per_sample"], 16)

    def test_a_float_tiff_in_the_unit_range(self):
        values = (gradient16() / 65535).astype(np.float32)
        working, _ = to_working_colour(encoded(Image.fromarray(values), "TIFF"))
        self.assertEqual(int(np.asarray(working).max()), 255)

    def test_samples_outside_a_known_range_are_refused_not_clipped(self):
        values = gradient16().astype(np.int32) * 4
        with self.assertRaises(UnsupportedImageData):
            to_working_colour(encoded(Image.fromarray(values), "TIFF"))

    def test_8_bit_and_16_bit_rgb_are_unchanged(self):
        rng = np.random.default_rng(3)
        pixels = rng.integers(0, 256, (24, 40, 3), dtype=np.uint8)
        working, notes = to_working_colour(encoded(Image.fromarray(pixels)))
        np.testing.assert_array_equal(np.asarray(working), pixels)
        self.assertEqual(notes["colour_profile"]["embedded"], False)


class ColourProfileTests(SimpleTestCase):
    SAMPLES = np.array([[[200, 120, 80], [40, 180, 90], [90, 90, 210], [128, 128, 128]]], dtype=np.uint8)

    def tagged(self, fmt="PNG", pixels=None, **options):
        image = Image.fromarray(self.SAMPLES if pixels is None else pixels)
        profile = matrix_trc_profile(ADOBE_RGB_D50, ADOBE_GAMMA, "Test Adobe RGB (1998)")
        return encoded(image, fmt, icc_profile=profile, **options)

    def test_an_adobe_rgb_image_is_converted_to_srgb(self):
        """P2-5: the profile used to be dropped and the numbers read as sRGB."""
        working, notes = to_working_colour(self.tagged())

        converted = np.asarray(working).astype(int)
        reference = adobe_to_srgb_reference(self.SAMPLES)
        self.assertLessEqual(int(np.abs(converted - reference).max()), 1, (converted, reference))
        self.assertGreater(int(np.abs(converted - self.SAMPLES.astype(int)).max()), 10)  # it really moved
        self.assertEqual(notes["colour_profile"], {
            "embedded": True, "description": "Test Adobe RGB (1998)", "converted_to": "sRGB",
        })

    def test_a_tagged_jpeg_is_converted_too(self):
        flat = np.full((16, 16, 3), (200, 120, 80), dtype=np.uint8)
        working, _ = to_working_colour(self.tagged("JPEG", pixels=flat, quality=100, subsampling=0))
        centre = np.asarray(working)[8, 8].astype(int)
        reference = adobe_to_srgb_reference(np.array([[[200, 120, 80]]]))[0, 0]
        self.assertLessEqual(int(np.abs(centre - reference).max()), 2)

    def test_transparency_survives_the_conversion_unchanged(self):
        pixels = np.dstack([np.repeat(self.SAMPLES, 4, axis=0), np.full((4, 4), 255, np.uint8)])
        pixels[1:3, :, 3] = [0, 64, 128, 250]
        image = Image.fromarray(pixels, "RGBA")
        profile = matrix_trc_profile(ADOBE_RGB_D50, ADOBE_GAMMA, "Test Adobe RGB (1998)")
        working, _ = to_working_colour(encoded(image, icc_profile=profile))

        self.assertEqual(working.mode, "RGBA")
        np.testing.assert_array_equal(np.asarray(working)[..., 3], pixels[..., 3])
        reference = adobe_to_srgb_reference(self.SAMPLES)[0]
        self.assertLessEqual(int(np.abs(np.asarray(working)[0, :, :3].astype(int) - reference).max()), 1)

    def test_an_unreadable_profile_is_reported_not_trusted(self):
        working, notes = to_working_colour(encoded(Image.fromarray(self.SAMPLES), icc_profile=b"not a profile"))
        np.testing.assert_array_equal(np.asarray(working), self.SAMPLES)
        self.assertEqual(notes["colour_profile"]["converted_to"], None)
        self.assertIn("could not be read", notes["colour_profile"]["error"])


class OrientationAndAlphaTests(SimpleTestCase):
    def test_exif_orientation_is_applied(self):
        pixels = np.zeros((10, 30, 3), dtype=np.uint8)
        pixels[:, :5] = 255  # a white band on the left
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to display
        working, _ = to_working_colour(encoded(Image.fromarray(pixels), "JPEG", exif=exif, quality=100))
        self.assertEqual(working.size, (10, 30))
        self.assertGreater(np.asarray(working)[:5, :, 0].mean(), 200)  # the band is now at the top

    def test_palette_and_grey_alpha_images_keep_their_transparency(self):
        grey = Image.fromarray(np.dstack([np.full((4, 4), 90, np.uint8), np.full((4, 4), 30, np.uint8)]), "LA")
        working, _ = to_working_colour(encoded(grey))
        self.assertEqual(working.mode, "RGBA")
        self.assertEqual(int(np.asarray(working)[0, 0, 3]), 30)

        palette = Image.new("P", (4, 4), 1)
        palette.putpalette([0, 0, 0, 200, 50, 50] + [0] * 762)
        palette.info["transparency"] = 0
        palette.putpixel((0, 0), 0)
        working, _ = to_working_colour(encoded(palette))
        self.assertEqual(working.mode, "RGBA")
        self.assertEqual(int(np.asarray(working)[0, 0, 3]), 0)
        self.assertEqual(tuple(np.asarray(working)[1, 1]), (200, 50, 50, 255))

    def test_an_opaque_image_comes_back_rgb(self):
        working, _ = to_working_colour(encoded(Image.new("L", (3, 3), 77)))
        self.assertEqual((working.mode, working.getpixel((1, 1))), ("RGB", (77, 77, 77)))
