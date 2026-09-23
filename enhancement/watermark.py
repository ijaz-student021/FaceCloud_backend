"""Optional branding on export copies (Feature 25).

A watermark is always applied to a copy at export time. The stored enhanced or
edited file is never modified, so the clean version stays available and the
same result can be exported with or without a mark.
"""
import io
import re
from dataclasses import dataclass

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

BRAND_TEXT = "FaceCloud"

MODES = ("none", "small", "facecloud", "username", "custom")
POSITIONS = ("bottom_right", "bottom_left", "top_right", "top_left", "center", "tile")

# Text height as a fraction of the image's shorter edge.
MODE_SIZE = {"small": 0.025, "facecloud": 0.05, "username": 0.05, "custom": 0.05}

MAX_CUSTOM_LENGTH = 40
DEFAULT_OPACITY = 0.5
MIN_OPACITY, MAX_OPACITY = 0.1, 1.0
# Pillow's built-in font covers Basic Latin through Latin Extended-B; anything
# else would render as empty boxes.
_SUPPORTED_TEXT = re.compile(r"^[ -~¡-ɏ]+$")


class WatermarkError(ValueError):
    """Raised for an invalid watermark request; the message is user-facing."""

    def __init__(self, message, field="watermark"):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class WatermarkSpec:
    mode: str = "none"
    position: str = "bottom_right"
    opacity: float = DEFAULT_OPACITY
    text: str = ""

    @property
    def active(self):
        return self.mode != "none"

    def as_dict(self):
        return {
            "mode": self.mode, "position": self.position,
            "opacity": self.opacity, "text": self.text,
        }


NO_WATERMARK = WatermarkSpec()


def validate_spec(mode="none", position="bottom_right", opacity=DEFAULT_OPACITY, text=""):
    if mode not in MODES:
        raise WatermarkError(f"Unknown watermark mode. Choose one of: {', '.join(MODES)}.")
    if position not in POSITIONS:
        raise WatermarkError(
            f"Unknown watermark position. Choose one of: {', '.join(POSITIONS)}.",
            field="watermark_position",
        )
    try:
        opacity = float(opacity)
    except (TypeError, ValueError) as error:
        raise WatermarkError("Watermark opacity must be a number.", field="watermark_opacity") from error
    if not MIN_OPACITY <= opacity <= MAX_OPACITY:
        raise WatermarkError(
            f"Watermark opacity must be between {MIN_OPACITY} and {MAX_OPACITY}.",
            field="watermark_opacity",
        )
    text = (text or "").strip()
    if mode == "custom":
        if not text:
            raise WatermarkError("Custom watermark text is required.", field="watermark_text")
        if len(text) > MAX_CUSTOM_LENGTH:
            raise WatermarkError(
                f"Custom watermark text can be at most {MAX_CUSTOM_LENGTH} characters.",
                field="watermark_text",
            )
        if not _SUPPORTED_TEXT.match(text):
            raise WatermarkError(
                "Custom watermark text may only use Latin letters, digits and common punctuation.",
                field="watermark_text",
            )
    else:
        text = ""
    return WatermarkSpec(mode=mode, position=position, opacity=opacity, text=text)


def watermark_label(spec, owner):
    if spec.mode in ("small", "facecloud"):
        return BRAND_TEXT
    if spec.mode == "username":
        return f"@{owner.username}"
    return spec.text


def _font(height):
    return ImageFont.load_default(size=max(10, int(height)))


def _text_layer(label, size, opacity):
    """Render the label as white text with a dark outline so it reads on any photo."""
    font = _font(size)
    left, top, right, bottom = font.getbbox(label)
    outline = max(1, size // 14)
    width = right - left + outline * 4
    height = bottom - top + outline * 4
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    alpha = int(round(255 * opacity))
    origin = (outline * 2 - left, outline * 2 - top)
    draw.text(
        origin, label, font=font, fill=(255, 255, 255, alpha),
        stroke_width=outline, stroke_fill=(0, 0, 0, alpha),
    )
    return layer


def _anchor(position, canvas, layer, margin):
    width, height = canvas
    layer_width, layer_height = layer.size
    horizontal = {
        "left": margin, "right": width - layer_width - margin,
        "center": (width - layer_width) // 2,
    }
    vertical = {
        "top": margin, "bottom": height - layer_height - margin,
        "center": (height - layer_height) // 2,
    }
    if position == "center":
        return horizontal["center"], vertical["center"]
    vertical_key, horizontal_key = position.split("_")
    return horizontal[horizontal_key], vertical[vertical_key]


def apply_watermark(image, spec, owner):
    """Return a new RGB image with the watermark; ``image`` is left untouched."""
    if not spec.active:
        return image.copy()
    label = watermark_label(spec, owner)
    if not label:
        return image.copy()
    base = image.convert("RGBA")
    shortest = min(base.size)
    size = max(10, int(shortest * MODE_SIZE[spec.mode]))
    layer = _text_layer(label, size, spec.opacity)
    margin = max(4, shortest // 50)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    if spec.position == "tile":
        step_x = layer.width + layer.width // 2
        step_y = layer.height * 3
        for row, y in enumerate(range(margin, base.height, step_y)):
            offset = (step_x // 2) if row % 2 else 0
            for x in range(margin - offset, base.width, step_x):
                if x >= 0:
                    overlay.alpha_composite(layer, (x, y))
    else:
        x, y = _anchor(spec.position, base.size, layer, margin)
        overlay.alpha_composite(layer, (max(0, x), max(0, y)))
    return Image.alpha_composite(base, overlay).convert("RGB")


def render_watermarked(file_field, output_format, spec, owner, max_edge=None):
    """Open a stored image, stamp it and return ``(bytes, content_type)``.

    ``max_edge`` shrinks the copy first (used for previews, which only need to
    show placement and legibility).
    """
    file_field.open("rb")
    try:
        with Image.open(file_field) as source:
            if source.width * source.height > settings.MAX_IMAGE_PIXELS:
                raise WatermarkError("The image is too large to watermark.")
            image = source.convert("RGB")
    finally:
        file_field.close()
    if max_edge and max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    stamped = apply_watermark(image, spec, owner)
    buffer = io.BytesIO()
    if output_format == "png":
        stamped.save(buffer, format="PNG", compress_level=6)
        return buffer.getvalue(), "image/png"
    stamped.save(buffer, format="JPEG", quality=settings.PREPROCESS_JPEG_QUALITY, subsampling=0)
    return buffer.getvalue(), "image/jpeg"
