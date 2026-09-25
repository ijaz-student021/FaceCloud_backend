"""Collage geometry: fixed layouts and the face-aware crop (Feature 49).

Pure functions with no Django or image dependencies so the maths is easy to
test. A face box is ``{"x", "y", "width", "height"}`` in the pixel space of the
image it belongs to. Face boxes only locate a face; they cannot recognise a
person, so they are image metadata under Feature 41.
"""

CANVAS_WIDTH = 1600
CANVAS_HEIGHT = 1200
GUTTER = 12

MIN_PHOTOS = 3
MAX_PHOTOS = 6

# Cells are (x, y, width, height) as fractions of the canvas.
LAYOUTS = {
    3: [(0, 0, 0.6, 1), (0.6, 0, 0.4, 0.5), (0.6, 0.5, 0.4, 0.5)],
    4: [(0, 0, 0.5, 0.5), (0.5, 0, 0.5, 0.5), (0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)],
    5: [
        (0, 0, 0.5, 0.5), (0.5, 0, 0.5, 0.5),
        (0, 0.5, 1 / 3, 0.5), (1 / 3, 0.5, 1 / 3, 0.5), (2 / 3, 0.5, 1 / 3, 0.5),
    ],
    6: [
        (0, 0, 1 / 3, 0.5), (1 / 3, 0, 1 / 3, 0.5), (2 / 3, 0, 1 / 3, 0.5),
        (0, 0.5, 1 / 3, 0.5), (1 / 3, 0.5, 1 / 3, 0.5), (2 / 3, 0.5, 1 / 3, 0.5),
    ],
}


def layout_cells(count):
    """Pixel rectangles ``(x, y, width, height)`` for ``count`` photos, gutters included."""
    if count not in LAYOUTS:
        raise ValueError(f"No collage layout for {count} photos.")
    half = GUTTER // 2
    cells = []
    for fx, fy, fw, fh in LAYOUTS[count]:
        left, right = round(fx * CANVAS_WIDTH), round((fx + fw) * CANVAS_WIDTH)
        top, bottom = round(fy * CANVAS_HEIGHT), round((fy + fh) * CANVAS_HEIGHT)
        left = left + half if fx > 0 else left + GUTTER
        top = top + half if fy > 0 else top + GUTTER
        right = right - half if right < CANVAS_WIDTH else right - GUTTER
        bottom = bottom - half if bottom < CANVAS_HEIGHT else bottom - GUTTER
        cells.append((left, top, right - left, bottom - top))
    return cells


def clean_face_boxes(faces, image_width, image_height):
    """Keep only well-formed boxes, clamped to the image. Never raises on bad data."""
    boxes = []
    for face in faces or []:
        box = face.get("standardized_box") if isinstance(face, dict) else None
        box = box or face
        try:
            x, y = float(box["x"]), float(box["y"])
            width, height = float(box["width"]), float(box["height"])
        except (TypeError, KeyError, ValueError):
            continue
        left, top = max(0.0, x), max(0.0, y)
        right, bottom = min(float(image_width), x + width), min(float(image_height), y + height)
        if right - left >= 1 and bottom - top >= 1:
            boxes.append((left, top, right, bottom))
    return boxes


def _union(boxes):
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def smart_crop(image_width, image_height, target_aspect, faces):
    """Largest crop of ``target_aspect`` (width / height) that keeps the faces in frame.

    Returns ``{"x", "y", "width", "height", "faces_in_frame"}``. The window is
    centred on the faces (all of them when they fit, otherwise the largest) and
    slid back inside the image, so a face is only ever cut when the faces
    together cannot fit the target shape at all.
    """
    if image_width / image_height > target_aspect:
        height = image_height
        width = min(image_width, max(1, round(height * target_aspect)))
    else:
        width = image_width
        height = min(image_height, max(1, round(width / target_aspect)))

    boxes = clean_face_boxes(faces, image_width, image_height)
    focus = None
    if boxes:
        union = _union(boxes)
        if union[2] - union[0] <= width and union[3] - union[1] <= height:
            focus = union
        else:
            focus = max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    if focus is None:
        center_x, center_y = image_width / 2, image_height / 2
    else:
        center_x, center_y = (focus[0] + focus[2]) / 2, (focus[1] + focus[3]) / 2
    left = round(min(max(center_x - width / 2, 0), image_width - width))
    top = round(min(max(center_y - height / 2, 0), image_height - height))

    inside = all(
        box[0] >= left and box[1] >= top and box[2] <= left + width and box[3] <= top + height
        for box in boxes
    )
    return {"x": left, "y": top, "width": width, "height": height, "faces_in_frame": inside}


def evenly_spaced(items, count):
    """``count`` items spread across ``items`` in order (all of them when fewer)."""
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[0]]
    return [items[round(index * (len(items) - 1) / (count - 1))] for index in range(count)]
