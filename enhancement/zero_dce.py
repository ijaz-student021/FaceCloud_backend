"""OpenCV-DNN inference for the official Zero-DCE curve-estimation network."""

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


def enhance_with_zero_dce(image):
    """Enhance BGR uint8 input using official Zero-DCE learned curve parameters."""
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

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    for index in range(8):
        curve = curves[0, index * 3:(index + 1) * 3].transpose(1, 2, 0)
        if curve.shape[:2] != (height, width):
            curve = cv2.resize(curve, (width, height), interpolation=cv2.INTER_LINEAR)
        rgb = rgb + curve * (rgb * rgb - rgb)
        rgb = np.clip(rgb, 0.0, 1.0)
    output = cv2.cvtColor(np.rint(rgb * 255.0).astype(np.uint8), cv2.COLOR_RGB2BGR)
    return output, {
        "provider": "official_zero_dce_onnx_opencv_dnn",
        "model_path": Path(settings.ZERO_DCE_MODEL_PATH).name,
        "input_dimensions": [width, height],
        "inference_dimensions": [infer_width, infer_height],
        "curve_iterations": 8,
        "license": "CC-BY-NC-4.0 academic/non-commercial",
    }
