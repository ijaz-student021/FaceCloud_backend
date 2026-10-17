"""Decide whether a denoised candidate is worth keeping.

Denoising trades grain for detail. This module measures both sides of that trade
against the image as it was immediately before denoising, and accepts the
candidate only when it removed real noise and left the structure alone.

All measurements are on the luminance channel, in 32-pixel blocks. The blocks
are ranked by how much large-scale structure they hold; the flattest quarter is
where noise is measured and the most structured quarter is where detail is.
Near-black and near-white blocks are left out, because grain is clipped there.

* **Noise removed.** Fine-band energy (what a one-pixel blur takes away) in the
  flat blocks, before and after, in grey levels. Flat regions hold almost no
  detail, so what disappears there is noise.
* **Structured detail kept.** Mid-band energy (one octave coarser) in the
  structured blocks, *minus the same energy in the flat blocks*. The flat blocks
  give the noise floor in that band, so the difference is structure alone. This
  is what makes the measure independent of how strong or how correlated the
  noise is: removing noise lowers both terms equally and leaves it unchanged.
* **Edges kept.** The same, for gradient energy.

Evidence (real CPU processing; grain was synthetic and Gaussian). On 110
ground-truth cases from 22 photographs, clean and with added grain:

* The noise-removed and noise-share limits were fixed on four photographs and
  then applied unchanged to the other eighteen. The residual-whiteness limit
  could not be: no case on the four photographs depends on it, so it was chosen
  with the eighteen in view and has no held-out check. Together they kept 50 of
  the 59 held-out cases where denoising moved the result toward the clean
  original and refused 18 of the 21 where it moved it away.
* The pipeline's edge-aware denoiser kept 1.00 of the structured detail in every
  one of those cases, helpful or not, so the detail limits never fired on it. A
  plain Gaussian blur given as the candidate kept 0.26 to 0.82 and was refused
  every time. The detail limits are therefore a guard against a destructive
  candidate, verified against one, not a tuned quality threshold.

What this cannot see: fine-band texture (skin pores, fabric weave) in flat
regions is indistinguishable from grain by any measure here, and real sensor
noise is signal-dependent, which a flat-region noise floor only approximates.
The limits need re-checking on full-resolution real photographs.
"""
import cv2
import numpy as np
from django.conf import settings

# Candidate strengths tried in order: the full candidate, then blends back toward
# the pre-denoise reference.
BLEND_STRENGTHS = (1.0, 0.85, 0.7, 0.55, 0.4)
_WINDOW = 1024       # analysis windows keep the cost bounded on large photos
_GRID = 3            # ...at most a 3x3 spread of them
_BLOCK = 32
_USABLE_RANGE = (16.0, 239.0)  # mean grey level outside which grain is clipped
_MINIMUM_STRUCTURE = 1.0  # squared grey levels: below this there is no structure to protect


def _windows(height, width):
    """Up to nine windows spread over the frame (the whole frame if it is small)."""
    if height * width <= _GRID * _GRID * _WINDOW * _WINDOW:
        return [(slice(0, height), slice(0, width))]
    window_h, window_w = min(_WINDOW, height), min(_WINDOW, width)
    tops = np.linspace(0, height - window_h, _GRID).astype(int)
    lefts = np.linspace(0, width - window_w, _GRID).astype(int)
    return [(slice(t, t + window_h), slice(l, l + window_w)) for t in tops for l in lefts]


def _bands(gray):
    """Squared ``(fine, mid, edge)`` detail and the smooth base they were taken from."""
    gray = gray.astype(np.float32)
    blur_fine = cv2.GaussianBlur(gray, (0, 0), 1.0)
    smooth = cv2.GaussianBlur(gray, (0, 0), 2.5)
    fine = np.abs(gray - blur_fine)
    mid = (blur_fine - smooth) ** 2
    edge = _gradient(blur_fine) ** 2
    return fine, mid, edge, smooth


def _block_means(array, block):
    height = (array.shape[0] // block) * block
    width = (array.shape[1] // block) * block
    if height == 0 or width == 0:
        return np.array([float(array.mean())], dtype=np.float32)
    return array[:height, :width].reshape(height // block, block, width // block, block).mean(axis=(1, 3)).ravel()


def _gradient(image):
    return cv2.magnitude(
        cv2.Sobel(image, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(image, cv2.CV_32F, 0, 1, ksize=3),
    )


class _Region:
    """One analysis window of the reference, with its flat and structured blocks chosen."""

    def __init__(self, gray, block=_BLOCK):
        self.block = block
        fine, mid, edge, smooth = _bands(gray)
        structure = _block_means(_gradient(smooth), block)
        level = _block_means(smooth, block)
        usable = np.flatnonzero((level >= _USABLE_RANGE[0]) & (level <= _USABLE_RANGE[1]))
        if len(usable) < 8:
            usable = np.arange(len(structure))
        order = usable[np.argsort(structure[usable])]
        quarter = max(1, len(order) // 4)
        self.flat, self.structured = order[:quarter], order[-quarter:]
        self.before = self._energies(fine, mid, edge)

    def _energies(self, fine, mid, edge):
        fine, mid, edge = (_block_means(band, self.block) for band in (fine, mid, edge))
        return {
            "noise": float(fine[self.flat].mean()),
            # Structure alone: the band's energy where there is structure, less
            # its energy where there is only noise.
            "detail": float(mid[self.structured].mean() - mid[self.flat].mean()),
            "detail_floor": float(mid[self.flat].mean()),
            "edges": float(edge[self.structured].mean() - edge[self.flat].mean()),
            "edges_floor": float(edge[self.flat].mean()),
        }

    def compare(self, reference_gray, candidate_gray):
        fine, mid, edge, _smooth = _bands(candidate_gray)
        after = self._energies(fine, mid, edge)
        # What was taken out, split into the same two bands. Grain is nearly all
        # fine-band; texture has as much energy one octave down.
        removed = reference_gray.astype(np.float32) - candidate_gray.astype(np.float32)
        removed_fine_blur = cv2.GaussianBlur(removed, (0, 0), 1.0)
        removed_smooth = cv2.GaussianBlur(removed, (0, 0), 2.5)
        return {f"{key}_before": value for key, value in self.before.items()} | {
            f"{key}_after": value for key, value in after.items()
        } | {
            "removed_fine": float(np.mean((removed - removed_fine_blur) ** 2)),
            "removed_mid": float(np.mean((removed_fine_blur - removed_smooth) ** 2)),
        }


def _retention(before, after, floor):
    """Share of structured energy kept, as an amplitude; ``None`` when there was none to keep.

    Structure weaker than the noise floor it sits on cannot be told from an
    uneven floor, so it is not judged.
    """
    if before <= max(floor, _MINIMUM_STRUCTURE):
        return None
    return round(float(np.sqrt(max(after, 0.0) / before)), 3)


def _summarise(parts):
    """Combine per-window measurements into the figures the decision uses."""
    total = {key: float(np.mean([part[key] for part in parts])) for key in parts[0]}
    removed = total["noise_before"] - total["noise_after"]
    return {
        "noise_before": round(total["noise_before"], 3),
        "noise_after": round(total["noise_after"], 3),
        "noise_removed": round(removed, 3),
        "noise_reduction": round(removed / max(total["noise_before"], 1e-3), 3),
        # How noise-like the removed part is: about 3.5 for pure grain, about 1.8
        # when what was removed was mostly texture.
        "removed_residual_whiteness": round(
            float(np.sqrt(total["removed_fine"] / max(total["removed_mid"], 1e-6))), 3,
        ),
        "detail_retention": _retention(total["detail_before"], total["detail_after"], total["detail_floor_before"]),
        "edge_retention": _retention(total["edges_before"], total["edges_after"], total["edges_floor_before"]),
    }


class DenoiseReference:
    """The pre-denoise image, analysed once and compared with each candidate."""

    def __init__(self, image):
        self.image = image
        self._gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        height, width = self._gray.shape
        self._windows = _windows(height, width)
        self._regions = [_Region(self._gray[window]) for window in self._windows]
        self._faces = []
        # Grain measured where there is nothing else to measure: the flat regions.
        self.noise_level = round(float(np.mean([region.before["noise"] for region in self._regions])), 3)

    def set_faces(self, face_boxes):
        """Add faces (boxes as fractions of the frame) to be measured on their own."""
        height, width = self._gray.shape
        self._faces = []
        for box in face_boxes:
            window = self._face_window(box, height, width)
            if window is not None:
                rows, columns = window
                block = _BLOCK if min(rows.stop - rows.start, columns.stop - columns.start) >= 256 else 16
                self._faces.append((window, _Region(self._gray[window], block)))
        return len(self._faces)

    @staticmethod
    def _face_window(box, height, width):
        """The central part of a face box, capped at the analysis window size."""
        x, y, w, h = box
        left, top = int(x * width), int(y * height)
        right, bottom = int((x + w) * width), int((y + h) * height)
        if right - left < 64 or bottom - top < 64:
            return None
        if right - left > _WINDOW:
            left = (left + right - _WINDOW) // 2
            right = left + _WINDOW
        if bottom - top > _WINDOW:
            top = (top + bottom - _WINDOW) // 2
            bottom = top + _WINDOW
        return slice(max(0, top), min(height, bottom)), slice(max(0, left), min(width, right))

    def measure(self, candidate):
        """Whole-frame measurements, plus one entry per face (same figures, face only)."""
        gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY)
        frame = _summarise([
            region.compare(self._gray[window], gray[window])
            for window, region in zip(self._windows, self._regions)
        ])
        faces = []
        for window, region in self._faces:
            face = _summarise([region.compare(self._gray[window], gray[window])])
            faces.append({key: face[key] for key in ("detail_retention", "edge_retention")})
        return frame, faces


def limits():
    return {
        "minimum_noise_removed": float(settings.DENOISE_MIN_NOISE_REMOVED),
        "minimum_noise_reduction": float(settings.DENOISE_MIN_NOISE_REDUCTION),
        "minimum_removed_residual_whiteness": float(settings.DENOISE_MIN_RESIDUAL_WHITENESS),
        "minimum_detail_retention": float(settings.DENOISE_MIN_DETAIL_RETENTION),
        "minimum_edge_retention": float(settings.DENOISE_MIN_EDGE_RETENTION),
    }


def _below(value, minimum):
    return value is not None and value < minimum


def violations(frame, faces, bounds):
    """Why a candidate is not acceptable; empty when it is."""
    found = []
    if frame["noise_removed"] < bounds["minimum_noise_removed"]:
        found.append("too_little_noise_removed")
    # Texture mistaken for noise shows in two ways: only a small share of it can
    # be removed, and what is removed is not noise-like. A grainy photo whose
    # denoise was only partly effective fails the first test but not the second.
    if (
        frame["noise_reduction"] < bounds["minimum_noise_reduction"]
        and frame["removed_residual_whiteness"] < bounds["minimum_removed_residual_whiteness"]
    ):
        found.append("removed_texture_not_noise")
    if _below(frame["detail_retention"], bounds["minimum_detail_retention"]):
        found.append("detail_removed")
    if _below(frame["edge_retention"], bounds["minimum_edge_retention"]):
        found.append("edges_softened")
    if any(_below(face["detail_retention"], bounds["minimum_detail_retention"]) for face in faces):
        found.append("facial_detail_removed")
    if any(_below(face["edge_retention"], bounds["minimum_edge_retention"]) for face in faces):
        found.append("facial_edges_softened")
    return found


def judge_candidate(reference, candidate):
    """Return ``(image, verdict)``: the candidate, a weaker blend of it, or the reference.

    Each strength is measured on its real blended pixels. A weaker blend keeps
    more detail but also removes less noise, so it is accepted only if it still
    clears the noise-benefit limits: there is no strength at which a candidate
    that mostly removes detail becomes acceptable.
    """
    bounds = limits()
    attempts = []
    for strength in BLEND_STRENGTHS:
        trial = candidate if strength >= 1.0 else cv2.addWeighted(
            candidate, strength, reference.image, 1.0 - strength, 0,
        )
        frame, faces = reference.measure(trial)
        failed = violations(frame, faces, bounds)
        attempts.append({"blend": strength, "frame": frame, "faces": faces, "violations": failed})
        if not failed:
            return trial, {
                "accepted": True,
                "applied_blend": strength,
                "status": "accepted" if strength >= 1.0 else "strength_reduced",
                "reason": (
                    "Removed measurable noise and kept the structured detail."
                    if strength >= 1.0 else
                    "Accepted at reduced strength: the full candidate removed structured detail."
                ),
                "limits": bounds,
                "attempts": attempts,
            }
    first = attempts[0]["violations"]
    return reference.image, {
        "accepted": False,
        "applied_blend": 0.0,
        "status": "rejected",
        "reason": _REJECTION_TEXT.get(first[0], "The candidate did not meet the denoise limits."),
        "rejection_reasons": first,
        "limits": bounds,
        "attempts": attempts,
    }


_REJECTION_TEXT = {
    "too_little_noise_removed": "Denoising removed too little noise to justify changing the image.",
    "removed_texture_not_noise": (
        "What looked like noise is mostly texture: only a small share could be removed, "
        "and what was removed was not noise-like."
    ),
    "detail_removed": "Denoising removed structured detail, not only noise.",
    "edges_softened": "Denoising softened the image's edges.",
    "facial_detail_removed": "Denoising removed structured facial detail, not only noise.",
    "facial_edges_softened": "Denoising softened facial edges.",
}
