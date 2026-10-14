"""Decode an uploaded image into the pipeline's working format.

The working format is 8 bits per channel, sRGB, in display orientation, with
any transparency kept as an alpha channel (mode ``RGB`` or ``RGBA``). Every
reader of an original -- preprocessing, enhancement, face metrics, thumbnails
-- goes through ``to_working_colour``, so they all see the same pixels. The
stored original is never modified.

* Higher bit depths are converted, not preserved: the whole pipeline (OpenCV,
  the models, the encoders) works in 8 bits. A 16-bit sample ``v`` becomes
  ``round(v * 255 / 65535)``, the full range of one mapped onto the other, so
  no tonal range is clipped. (Pillow already reduces 16-bit RGB to 8 bits when
  it opens the file; 16-bit grayscale it leaves as ``I;16``, which a plain
  ``convert("RGB")`` clips to 0-255 -- a full-range gradient became 3 levels.)
* Samples whose range cannot be known (32-bit integers above 65535, floats
  outside 0-1) are refused rather than guessed or clipped.
* An embedded ICC profile is converted to sRGB (relative colorimetric with
  black-point compensation, the usual choice for photographs). Outputs are
  written untagged, which viewers read as sRGB. A profile that cannot be read
  or does not fit the image is reported and the numbers are treated as sRGB,
  as before.
"""
import io

import numpy as np
from PIL import Image, ImageCms, ImageOps

SIXTEEN_BIT_MODES = {"I;16", "I;16L", "I;16B", "I;16N"}
_SRGB = ImageCms.createProfile("sRGB")
_FLAGS = ImageCms.Flags.BLACKPOINTCOMPENSATION


class UnsupportedImageData(OSError):
    """The file decodes, but its samples cannot be mapped to 8 bits without guessing.

    An ``OSError``, so every reader treats it like any other undecodable file.
    """


def _to_eight_bits(image, notes):
    mode = image.mode
    if mode in SIXTEEN_BIT_MODES or mode == "I":
        samples = np.asarray(image).astype(np.int64)
        if samples.size and (samples.min() < 0 or samples.max() > 65535):
            raise UnsupportedImageData(f"{mode} samples outside the 16-bit range cannot be converted safely.")
        notes["bits_per_sample"] = 16
        eight = np.rint(samples * (255.0 / 65535.0)).astype(np.uint8)
        return Image.fromarray(eight, "L")
    if mode == "F":
        samples = np.asarray(image, dtype=np.float64)
        if samples.size and (not np.isfinite(samples).all() or samples.min() < 0 or samples.max() > 1):
            raise UnsupportedImageData("Floating-point samples outside 0-1 cannot be converted safely.")
        notes["bits_per_sample"] = 32
        return Image.fromarray(np.rint(samples * 255.0).astype(np.uint8), "L")
    return image


def _split_alpha(image):
    """``(colour, alpha)``: colour in a mode LittleCMS understands, alpha or None."""
    if image.mode in {"RGBA", "LA", "PA", "La", "RGBa"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA") if image.mode != "LA" else image
        alpha = rgba.getchannel("A")
        colour = image.convert("L") if image.mode == "LA" else rgba.convert("RGB")
        return colour, alpha
    if image.mode in {"RGB", "L", "CMYK"}:
        return image, None
    if image.mode == "1":
        return image.convert("L"), None
    return image.convert("RGB"), None


def _from_profile(colour, icc, notes):
    """Convert ``colour`` from its embedded profile to sRGB, or report why not."""
    record = notes["colour_profile"]
    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        record["description"] = (ImageCms.getProfileDescription(profile) or "").strip()
    except (OSError, ImageCms.PyCMSError) as error:
        record["error"] = f"The embedded colour profile could not be read ({error}); treated as sRGB."
        return colour
    try:
        converted = ImageCms.profileToProfile(
            colour, profile, _SRGB, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            outputMode="RGB", flags=_FLAGS,
        )
    except (OSError, ValueError, ImageCms.PyCMSError) as error:
        record["error"] = f"The embedded colour profile does not fit this image ({error}); treated as sRGB."
        return colour
    record["converted_to"] = "sRGB"
    return converted


def to_working_colour(image):
    """``(image, notes)``: ``image`` oriented, 8-bit sRGB, mode ``RGB`` or ``RGBA``.

    ``notes`` says what was converted: the source mode and bit depth, and what
    happened to an embedded colour profile.
    """
    icc = image.info.get("icc_profile")
    notes = {
        "source_mode": image.mode,
        "bits_per_sample": 8,
        "colour_profile": {"embedded": bool(icc), "description": None, "converted_to": None},
    }
    working = ImageOps.exif_transpose(image)
    working = _to_eight_bits(working, notes)
    colour, alpha = _split_alpha(working)
    if icc:
        colour = _from_profile(colour, icc, notes)
    colour = colour if colour.mode == "RGB" else colour.convert("RGB")
    if alpha is None:
        return colour, notes
    colour.putalpha(alpha)
    return colour, notes
