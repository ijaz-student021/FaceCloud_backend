"""ImageQualityAnalyzer: what is actually wrong with this photograph, from several independent signals.

It looks at the image *before* any enhancement and returns a structured report: exposure (of the scene and, where there
is a face, of the face), noise, blur and its type, resolution, compression, colour, and each detected face. The router
(``router.py``) decides what to do with the report; nothing here changes a pixel.

Why several signals. A single brightness threshold calls every night photograph defective, and a single Laplacian
variance cannot tell blur from a plain, smooth subject or motion from defocus. So:

* **Exposure** is judged on the face when there is one (a face that is crushed into the shadows has a compressed
  luminance range and many near-black pixels; a well-lit dark-skinned face does not), and only on the whole frame when
  there is not. A dark scene with a visible subject is reported as ``dark_scene`` and is *not* a defect.
* **Blur** has a detail-retention signal (how much fine-scale gradient survives a small extra blur) and a direction
  signal (motion blur removes gradient energy along one direction; defocus removes it in every direction).
* **Noise** is the existing flat-region estimator (``analyze_image_quality``), banded by thresholds measured on
  photographs with known added noise.
* **Faces** carry their own size, sharpness, luminance range and a verdict, because a face can be too small or too
  degraded for restoration however good the rest of the photograph is.

Thresholds are in ``Thresholds`` with their provenance. They were measured on a small set of real photographs with
synthetic degradations (``scripts/calibrate_analyzer.py``, results in ``docs/evaluation/``); that set is small and not
demographically diverse, and real blur and noise differ from synthetic, so treat every status as a measurement with
error, not a fact. Each report lists its own limitations.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass(frozen=True)
class Thresholds:
    # --- blur (measured at a 1024 px long edge) ---
    # Fraction of fine-scale gradient energy that survives an extra 2 px blur: sharp photographs keep about a third,
    # blurred ones most of it. Measured: sharp median 0.35 (p90 0.46), Gaussian blur 0.86, disk blur 0.80, motion 0.67.
    blur_retention_blurred: float = 0.60
    blur_retention_soft: float = 0.50
    # Directional deficit of the gradient-orientation histogram over strong edges (min bin / mean bin, 12 bins).
    # Measured: motion median 0.12 (p90 0.25); sharp 0.41, Gaussian 0.36, disk 0.38 (p10 about 0.23-0.32).
    motion_ratio_max: float = 0.22
    # Below this ratio almost no sharp photograph in the measured set lies (sharp p10 0.32), so it is motion blur even
    # when the overall retention is under the blurred threshold (a high-contrast, edge-rich scene).
    motion_ratio_strong: float = 0.10
    motion_retention_min: float = 0.38
    defocus_ratio_min: float = 0.30
    # --- noise (the existing flat-region estimator, in 8-bit levels) ---
    noise_low: float = 1.5
    noise_moderate: float = 4.0
    noise_high: float = 9.0
    # --- exposure ---
    face_dynamic_range_poor: float = 70.0     # p95 - p5 of face luminance below this is a compressed range
    face_median_dark: float = 75.0            # ...and only when the face is also this dark
    face_crushed_fraction_poor: float = 0.30  # more than this share of face pixels is near black
    crushed_level: float = 14.0
    # A face is only called under-exposed when the whole frame is dark too: a dark face in a bright frame is back-lit, which a
    # global lift would not fix and would damage (found by the benchmark: a blurred copy of a well-exposed night photo was lifted).
    subject_scene_median_max: float = 90.0
    scene_median_dark: float = 60.0
    scene_p95_dark: float = 150.0
    highlight_clip_percent: float = 8.0
    # --- resolution ---
    image_short_edge_low: int = 480
    face_short_edge_too_small: int = 48
    face_short_edge_low: int = 96
    # --- face quality ---
    # Laplacian variance of the face crop resized to 128 px. Measured on 12 portraits: sharp median 119 (p10 81);
    # Gaussian blur 1.5 px -> 21, 3 px -> 7; a 2.5x smaller (low-resolution) face -> 36 (p90 48); JPEG q15 -> 90.
    face_detail_poor: float = 50.0
    face_detail_acceptable: float = 75.0
    min_face_confidence: float = 0.5


@dataclass(frozen=True)
class FaceObservation:
    box: tuple              # x, y, width, height in pixels
    confidence: float
    landmarks: list | None
    area_ratio: float
    short_edge: int
    luminance: dict         # p5, p50, p95, dynamic_range, crushed_fraction
    detail: float | None    # Laplacian variance of the face at 128 px (higher = more detail)
    visibility: str         # good | poor
    quality: str            # good | acceptable | poor | too_small


@dataclass
class QualityReport:
    width: int
    height: int
    exposure: dict
    noise: dict
    blur: dict
    resolution: dict
    compression: dict
    colour: dict
    faces: list = field(default_factory=list)
    conditions: list = field(default_factory=list)
    recommended_operations: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    base: dict = field(default_factory=dict)  # the classic analyze_image_quality result, for the guards that use it

    @property
    def face_count(self):
        return len(self.faces)

    @property
    def main_face(self):
        return max(self.faces, key=lambda face: face.box[2] * face.box[3]) if self.faces else None

    def as_dict(self):
        faces = [
            {"box": list(face.box), "confidence": round(face.confidence, 3), "area_ratio": round(face.area_ratio, 4),
             "short_edge": face.short_edge, "luminance": face.luminance, "detail": face.detail,
             "visibility": face.visibility, "quality": face.quality}
            for face in self.faces
        ]
        main = self.main_face
        return {
            "width": self.width, "height": self.height,
            "brightness_status": self.exposure["status"], "noise_status": self.noise["status"],
            "blur_status": self.blur["status"], "blur_type": self.blur["type"],
            "resolution_status": self.resolution["status"], "face_count": self.face_count,
            "face_quality": main.quality if main else "none",
            "exposure": self.exposure, "noise": self.noise, "blur": self.blur, "resolution": self.resolution,
            "compression": self.compression, "colour": self.colour, "faces": faces, "conditions": self.conditions,
            "recommended_operations": self.recommended_operations, "limitations": self.limitations,
        }


LIMITATIONS = [
    "Thresholds were measured on a small set of photographs with synthetic degradations; real blur and noise differ.",
    "Face exposure uses the luminance range and crushed-shadow share, not the average, to avoid penalising dark skin; "
    "it has not been validated across a demographically diverse set.",
    "Blur type needs strong edges; a photograph without them reports an unknown type.",
    "No statistic here measures how a person will judge the photograph.",
]


def _luminance(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _bounded(gray, long_edge=1024):
    scale = long_edge / max(gray.shape[:2])
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray


def blur_signals(gray):
    """``(retention, direction_ratio)`` of a grayscale image, or ``(None, None)`` without enough strong edges.

    ``retention`` is the share of fine-scale gradient energy that survives an extra 2 px blur (about a third for a sharp
    photograph, most of it for a blurred one). ``direction_ratio`` is the weakest orientation bin over the mean bin of the
    gradient-orientation histogram on strong edges: low means one direction has lost its edges (motion blur).
    """
    gray = _bounded(gray).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.hypot(gx, gy)
    strong = magnitude > max(float(np.percentile(magnitude, 90)), 8.0)
    if int(strong.sum()) < 500:
        return None, None
    angle = (np.arctan2(gy, gx) % np.pi)[strong]
    weight = magnitude[strong] ** 2
    histogram, _edges = np.histogram(angle, bins=12, range=(0, np.pi), weights=weight)
    histogram = histogram / histogram.sum()
    ratio = float(histogram.min() / histogram.mean())
    blurred = cv2.GaussianBlur(gray, (0, 0), 2.0)
    coarse = np.hypot(cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3))
    retention = float(np.mean(coarse[strong]) / max(float(np.mean(magnitude[strong])), 1e-6))
    return retention, ratio


class ImageQualityAnalyzer:
    def __init__(self, detector=None, thresholds=None, max_faces=5):
        self.detector = detector
        self.t = thresholds or Thresholds()
        self.max_faces = max_faces

    # -- faces ------------------------------------------------------------------------------------------------
    def _detector(self):
        if self.detector is None:
            from validation.services import get_face_detector

            self.detector = get_face_detector()
        return self.detector

    def _face_region(self, image, box):
        height, width = image.shape[:2]
        x, y, w, h = box
        pad_x, pad_y = int(0.15 * w), int(0.15 * h)
        x0, y0 = max(0, x + pad_x), max(0, y + pad_y)
        x1, y1 = min(width, x + w - pad_x), min(height, y + h - pad_y)
        return image[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else None

    def _observe_face(self, image, gray, box, confidence, landmarks):
        height, width = image.shape[:2]
        x, y, w, h = box
        region = self._face_region(image, box)
        luminance = {"p5": None, "p50": None, "p95": None, "dynamic_range": None, "crushed_fraction": None}
        detail = None
        if region is not None and region.size:
            values = _luminance(region).astype(np.float32)
            p5, p50, p95 = (float(np.percentile(values, q)) for q in (5, 50, 95))
            luminance = {
                "p5": round(p5, 1), "p50": round(p50, 1), "p95": round(p95, 1), "dynamic_range": round(p95 - p5, 1),
                "crushed_fraction": round(float((values < self.t.crushed_level).mean()), 3),
            }
            if min(region.shape[:2]) >= 16:
                standard = cv2.resize(values, (128, 128), interpolation=cv2.INTER_AREA)
                detail = round(float(cv2.Laplacian(standard, cv2.CV_32F).var()), 1)
        short_edge = int(min(w, h))
        # (Blur also narrows a face's range, and a face's detail cannot tell blur from darkness: a contrast-normalised detail
        # measure was tried and overlaps too much, sharp p10 0.025 against blurred 0.02. The frame decides instead, below.)
        poor_visibility = (
            luminance["dynamic_range"] is not None and (
                (luminance["dynamic_range"] < self.t.face_dynamic_range_poor and luminance["p50"] < self.t.face_median_dark)
                or luminance["crushed_fraction"] > self.t.face_crushed_fraction_poor
            )
        )
        if short_edge < self.t.face_short_edge_too_small:
            quality = "too_small"
        elif poor_visibility or (detail is not None and detail < self.t.face_detail_poor) or confidence < self.t.min_face_confidence:
            quality = "poor"
        elif short_edge < self.t.face_short_edge_low or (detail is not None and detail < self.t.face_detail_acceptable):
            quality = "acceptable"
        else:
            quality = "good"
        return FaceObservation(
            box=(int(x), int(y), int(w), int(h)), confidence=float(confidence), landmarks=landmarks,
            area_ratio=float(w * h) / float(width * height), short_edge=short_edge, luminance=luminance, detail=detail,
            visibility="poor" if poor_visibility else "good", quality=quality,
        )

    def _faces(self, image, gray):
        try:
            detection = self._detector().detect(image)
        except Exception:  # a detector failure is "no faces measured", reported as a limitation, never a crash
            return [], "face detection failed"
        observations = []
        for box, score, landmarks in zip(detection.boxes, detection.scores, detection.landmarks):
            score = 1.0 if score is None else float(score)
            observations.append(self._observe_face(image, gray, box, min(score, 1.0) if score <= 1.0 else 1.0, landmarks))
        observations.sort(key=lambda face: face.box[2] * face.box[3], reverse=True)
        return observations[: self.max_faces], ""

    # -- the report -------------------------------------------------------------------------------------------
    def analyze(self, image):
        from enhancement.services import analyze_image_quality, measure_compression_artifacts

        if image is None or image.size == 0 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("The image cannot be analysed: expected a BGR colour image.")
        t = self.t
        height, width = image.shape[:2]
        gray = _luminance(image)
        base = analyze_image_quality(image)
        faces, face_error = self._faces(image, gray)
        report = QualityReport(width=width, height=height, exposure={}, noise={}, blur={}, resolution={}, compression={},
                               colour={}, faces=faces, base=base)

        # exposure: the face when there is one, the frame otherwise
        percentiles = base["luminance_percentiles"]
        median = float(percentiles.get("p50", base["mean_luminance"]))
        p95 = float(percentiles.get("p95", 255.0))
        crushed = float((gray < t.crushed_level).mean())
        main = report.main_face
        scene_dark = median < t.scene_median_dark and p95 < t.scene_p95_dark
        if main is not None and main.quality != "too_small":
            if main.visibility == "poor" and median < t.subject_scene_median_max:
                status = "low_light_subject"
            elif main.visibility == "poor":
                status = "backlit_subject"  # a dark face in a bright frame: not a global-lift problem, left alone
            elif scene_dark or median < t.scene_median_dark:
                status = "dark_scene"  # the subject is visible: a dark scene is not a defect
            else:
                status = "normal"
        elif main is not None:
            status = "dark_scene" if scene_dark else "normal"
        else:
            status = "low_light" if scene_dark else ("dark_scene" if median < t.scene_median_dark else "normal")
        if base["highlight_clipping_percent"] > t.highlight_clip_percent and p95 >= 250:
            status = "overexposed" if status == "normal" else status
        report.exposure = {
            "status": status, "scene_median": round(median, 1), "scene_p95": round(p95, 1),
            "crushed_fraction": round(crushed, 3), "highlight_clipping_percent": base["highlight_clipping_percent"],
            "judged_on": "face" if main is not None and main.quality != "too_small" else "frame",
        }

        # noise
        noise = float(base["estimated_noise"])
        noise_status = ("none" if noise < t.noise_low else "low" if noise < t.noise_moderate
                        else "moderate" if noise < t.noise_high else "high")
        report.noise = {"status": noise_status, "estimate": round(noise, 2)}

        # blur
        retention, ratio = blur_signals(gray)
        if retention is None:
            blur = {"status": "unknown", "type": "unknown", "retention": None, "direction_ratio": None}
        else:
            status = "high" if retention >= t.blur_retention_blurred else "low" if retention >= t.blur_retention_soft else "none"
            if status == "none" and ratio <= t.motion_ratio_strong and retention >= t.motion_retention_min:
                status = "low"  # one direction has lost nearly all its edges: motion blur, whatever the overall retention
            blur_type = "none"
            if status != "none":
                blur_type = "motion" if ratio <= t.motion_ratio_max else "defocus" if ratio >= t.defocus_ratio_min else "mixed"
            blur = {"status": status, "type": blur_type, "retention": round(retention, 3), "direction_ratio": round(ratio, 3)}
        report.blur = blur

        # resolution, compression, colour
        short_edge = min(width, height)
        report.resolution = {
            "status": "low" if short_edge < t.image_short_edge_low else "normal", "short_edge": short_edge,
            "face_short_edge": main.short_edge if main else None,
        }
        compression = measure_compression_artifacts(image)
        report.compression = {"status": "heavy" if compression["detected"] else "none", "blockiness": compression["blockiness"]}
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
        cast = float(np.hypot(lab[:, :, 1].mean() - 128.0, lab[:, :, 2].mean() - 128.0))
        report.colour = {
            "status": "cast" if cast > 18.0 else "normal", "cast": round(cast, 1),
            "saturation": round(float(np.hypot(lab[:, :, 1] - 128.0, lab[:, :, 2] - 128.0).mean()), 1),
        }

        self._conclude(report, face_error)
        return report

    def _conclude(self, report, face_error):
        conditions = []
        if report.exposure["status"] in ("low_light", "low_light_subject"):
            conditions.append("low_light")
        if report.exposure["status"] == "dark_scene":
            conditions.append("dark_scene_not_a_defect")
        if report.exposure["status"] == "overexposed":
            conditions.append("overexposed_highlights")
        if report.noise["status"] in ("moderate", "high"):
            conditions.append("visible_noise")
        if report.blur["status"] in ("low", "high"):
            conditions.append(f"{report.blur['type']}_blur")
        if report.resolution["status"] == "low":
            conditions.append("low_resolution")
        if report.compression["status"] == "heavy":
            conditions.append("compression_artifacts")
        if report.face_count == 0:
            conditions.append("no_face")
        if report.face_count > 1:
            conditions.append("multiple_faces")
        main = report.main_face
        if main is not None and main.quality in ("too_small", "poor"):
            conditions.append(f"face_{main.quality}")
        report.conditions = conditions

        operations = []
        if report.exposure["status"] in ("low_light", "low_light_subject"):
            operations.append("low_light_enhancement")
        if "visible_noise" in conditions:
            operations.append("denoising")
        if report.blur["status"] == "high" and report.blur["type"] in ("motion", "mixed") \
                and "compression_artifacts" not in conditions:
            operations.append("motion_deblurring")
        if main is not None and main.quality == "poor" and main.short_edge >= self.t.face_short_edge_too_small:
            operations.append("face_restoration")
        if report.resolution["status"] == "low":
            operations.append("super_resolution")
        report.recommended_operations = operations
        report.limitations = list(LIMITATIONS) + ([face_error + ": no face information was used"] if face_error else [])
