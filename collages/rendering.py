"""Draw a collage from decoded photos. No identity data is read or written here."""
import cv2
import numpy as np

from .layouts import CANVAS_HEIGHT, CANVAS_WIDTH, layout_cells, smart_crop

BACKGROUND_BGR = (24, 24, 24)
JPEG_QUALITY = 90


def render_collage(tiles):
    """``tiles`` is a list of ``(bgr_image, face_boxes)``; returns ``(jpeg_bytes, crops)``.

    ``crops`` records, per tile, the source rectangle used and whether every
    detected face stayed inside it.
    """
    cells = layout_cells(len(tiles))
    canvas = np.full((CANVAS_HEIGHT, CANVAS_WIDTH, 3), BACKGROUND_BGR, dtype=np.uint8)
    crops = []
    for (image, faces), (x, y, width, height) in zip(tiles, cells):
        image_height, image_width = image.shape[:2]
        crop = smart_crop(image_width, image_height, width / height, faces)
        window = image[crop["y"]:crop["y"] + crop["height"], crop["x"]:crop["x"] + crop["width"]]
        canvas[y:y + height, x:x + width] = cv2.resize(window, (width, height), interpolation=cv2.INTER_AREA)
        crops.append(crop)
    ok, encoded = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("The collage could not be encoded.")
    return encoded.tobytes(), crops
