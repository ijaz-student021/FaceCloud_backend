"""Independent references for the decoding tests: ICC profile builder, colour maths."""
import io
import struct

import numpy as np
from PIL import Image


# Published ICC colorants (D50-adapted XYZ), columns R, G, B.
ADOBE_RGB_D50 = np.array([[0.60974, 0.20528, 0.14919],
                          [0.31111, 0.62567, 0.06322],
                          [0.01947, 0.06087, 0.74457]])
SRGB_D50 = np.array([[0.43607, 0.38515, 0.14307],
                     [0.22249, 0.71687, 0.06061],
                     [0.01392, 0.09708, 0.71410]])
ADOBE_GAMMA = 563 / 256  # Adobe RGB (1998): 2.19921875, as a u8Fixed8 curve


def s15fixed16(value):
    return struct.pack(">i", round(value * 65536))


def matrix_trc_profile(colorants, gamma, description):
    """A minimal ICC v2 RGB display profile: three colorants and one gamma curve."""
    def xyz(values):
        return b"XYZ " + b"\0" * 4 + b"".join(s15fixed16(v) for v in values)

    text = description.encode("ascii") + b"\0"
    desc = (b"desc" + b"\0" * 4 + struct.pack(">I", len(text)) + text
            + struct.pack(">II", 0, 0) + struct.pack(">HB", 0, 0) + b"\0" * 67)
    curve = b"curv" + b"\0" * 4 + struct.pack(">IH", 1, round(gamma * 256)) + b"\0\0"
    tags = [
        (b"desc", desc), (b"cprt", b"text" + b"\0" * 4 + b"test only\0"),
        (b"wtpt", xyz((0.9642, 1.0, 0.8249))),
        (b"rXYZ", xyz(colorants[:, 0])), (b"gXYZ", xyz(colorants[:, 1])), (b"bXYZ", xyz(colorants[:, 2])),
        (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, data = b"", b""
    for signature, body in tags:
        body += b"\0" * (-len(body) % 4)
        table += signature + struct.pack(">II", offset + len(data), len(body))
        data += body
    size = offset + len(data)
    header = (struct.pack(">I", size) + b"\0" * 4 + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
              + b"\0" * 12 + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
              + s15fixed16(0.9642) + s15fixed16(1.0) + s15fixed16(0.8249) + b"\0" * 48)
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + table + data


def srgb_encode(linear):
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)


def adobe_to_srgb_reference(rgb):
    """Relative-colorimetric Adobe RGB (1998) -> sRGB, from the colorants alone."""
    linear = (np.asarray(rgb, dtype=np.float64) / 255.0) ** ADOBE_GAMMA
    xyz = linear @ ADOBE_RGB_D50.T
    return np.rint(srgb_encode(xyz @ np.linalg.inv(SRGB_D50).T) * 255).astype(int)


def encoded(image, fmt="PNG", **options):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    buffer.seek(0)
    opened = Image.open(buffer)
    opened.load()
    return opened


def gradient16(height=32, width=512):
    return np.tile(np.linspace(0, 65535, width).round().astype(np.uint16), (height, 1))
