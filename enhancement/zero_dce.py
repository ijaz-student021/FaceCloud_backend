"""OpenCV-DNN inference for FaceCloud's private curve-estimation network."""

import threading
from pathlib import Path

import cv2
import numpy as np
from django.conf import settings


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE = {}


class ZeroDCEUnavailable(RuntimeError):
    """Raised when genuine Zero-DCE inference cannot be used safely."""


def _load_zero_dce_model(model_path):
    path = Path(model_path)
    if not path.is_file():
        raise ZeroDCEUnavailable("The configured Zero-DCE ONNX model is unavailable.")
    key = str(path.resolve())
    if key not in _MODEL_CACHE:
        try:
            _MODEL_CACHE[key] = cv2.dnn.readNetFromONNX(str(path))
        except cv2.error as error:
            raise ZeroDCEUnavailable("The Zero-DCE ONNX model could not be loaded.") from error
    return _MODEL_CACHE[key]


def _inference_size(width, height, maximum_edge):
    if max(width, height) <= maximum_edge:
        return width, height
    scale = maximum_edge / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _resize_curve_region(curve, x_start, y_start, x_end, y_end, width, height):
    """Interpolate a curve map for one destination tile without a full-size map."""
    curve_height, curve_width = curve.shape[:2]
    target_x = np.arange(x_start, x_end, dtype=np.float32)
    target_y = np.arange(y_start, y_end, dtype=np.float32)
    source_x = ((target_x + 0.5) * curve_width / width) - 0.5
    source_y = ((target_y + 0.5) * curve_height / height) - 0.5
    map_x, map_y = np.meshgrid(source_x, source_y)
    return cv2.remap(
        curve,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _apply_curves_tiled(image, curves, tile_edge):
    height, width = image.shape[:2]
    output = np.empty_like(image)
    tile_edge = max(128, int(tile_edge))
    curve_maps = [
        curves[0, index * 3:(index + 1) * 3].transpose(1, 2, 0)
        for index in range(8)
    ]
    for y_start in range(0, height, tile_edge):
        y_end = min(height, y_start + tile_edge)
        for x_start in range(0, width, tile_edge):
            x_end = min(width, x_start + tile_edge)
            tile = cv2.cvtColor(
                image[y_start:y_end, x_start:x_end], cv2.COLOR_BGR2RGB
            ).astype(np.float32) / 255.0
            for curve in curve_maps:
                tile_curve = _resize_curve_region(
                    curve,
                    x_start,
                    y_start,
                    x_end,
                    y_end,
                    width,
                    height,
                )
                tile = tile + tile_curve * (tile * tile - tile)
                tile = np.clip(tile, 0.0, 1.0)
            output[y_start:y_end, x_start:x_end] = cv2.cvtColor(
                np.rint(tile * 255.0).astype(np.uint8), cv2.COLOR_RGB2BGR
            )
    return output


def enhance_with_zero_dce(image):
    """Predict compact curve maps and apply them to the full-resolution input."""
    if image is None or image.size == 0 or image.ndim != 3 or image.shape[2] != 3:
        raise ZeroDCEUnavailable("Zero-DCE received an invalid image.")
    height, width = image.shape[:2]
    infer_width, infer_height = _inference_size(
        width, height, settings.ZERO_DCE_MAX_INFERENCE_EDGE,
    )
    inference_image = image
    if (infer_width, infer_height) != (width, height):
        inference_image = cv2.resize(image, (infer_width, infer_height), interpolation=cv2.INTER_AREA)
    blob = cv2.dnn.blobFromImage(
        inference_image, scalefactor=1.0 / 255.0, size=(infer_width, infer_height),
        mean=(0, 0, 0), swapRB=True, crop=False,
    )
    try:
        with _MODEL_LOCK:
            network = _load_zero_dce_model(settings.ZERO_DCE_MODEL_PATH)
            network.setInput(blob)
            curves = network.forward()
    except cv2.error as error:
        raise ZeroDCEUnavailable("Zero-DCE inference failed safely.") from error
    if curves.ndim != 4 or curves.shape[0] != 1 or curves.shape[1] != 24:
        raise ZeroDCEUnavailable("Zero-DCE returned an unexpected curve tensor.")

    output = _apply_curves_tiled(
        image,
        curves,
        settings.ZERO_DCE_FULL_RESOLUTION_TILE_EDGE,
    )
    return output, {
        "provider": "facecloud_private_curve_onnx_opencv_dnn",
        "model_path": Path(settings.ZERO_DCE_MODEL_PATH).name,
        "input_dimensions": [width, height],
        "inference_dimensions": [infer_width, infer_height],
        "curve_iterations": 8,
        "model_ownership": "private_facecloud_model",
        "resolution_policy": "proxy_curve_prediction_full_resolution_application",
        "full_resolution_tile_edge": settings.ZERO_DCE_FULL_RESOLUTION_TILE_EDGE,
    }
