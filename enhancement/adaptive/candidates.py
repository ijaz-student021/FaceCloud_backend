"""Enhancement candidates: every image the engine considers, with how it was made and why it was kept or dropped.

The original is always candidate 0 and is never modified. Each stage proposes a few variants of the current best image;
the validator accepts or rejects each, and the engine keeps at most one per stage. Nothing here decides: it records.
The pool is bounded (``max_candidates``) so a pathological photograph cannot cause a combinatorial explosion.
"""
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Candidate:
    id: str
    image: np.ndarray
    parent: str | None
    operation: str                  # original | low_light_enhancement | denoising | motion_deblurring | face_restoration | super_resolution
    variant: str = ""               # e.g. "blend 0.6" or "fidelity 0.7"
    models: list = field(default_factory=list)   # [{"model", "checkpoint", "device", "seconds", ...}]
    verdict: dict | None = None
    selected: bool = False
    seconds: float = 0.0            # the model's own time for this candidate
    validation_seconds: float = 0.0  # the validator's time for it (every gate, the face and identity checks included)
    size: tuple | None = None       # (width, height), kept when the pixels are released

    def __post_init__(self):
        if self.size is None and self.image is not None:
            self.size = (int(self.image.shape[1]), int(self.image.shape[0]))

    def release(self):
        """Free the pixels of a candidate the validator refused: it is never used again, so it neither holds memory nor counts against the pool's limit."""
        self.image = None

    def describe(self):
        """A JSON-friendly summary, without the pixels."""
        return {
            "id": self.id, "parent": self.parent, "operation": self.operation, "variant": self.variant,
            "dimensions": [int(self.size[0]), int(self.size[1])], "models": self.models,
            "seconds": round(self.seconds, 3), "validation_seconds": round(self.validation_seconds, 3), "selected": self.selected, "verdict": self.verdict,
        }


class PoolFull(Exception):
    """The candidate limit was reached; the engine stops proposing and keeps what it has."""


class CandidatePool:
    TOTAL_FACTOR = 6          # at most this many times ``max_candidates`` candidates are ever proposed, released ones included

    def __init__(self, original, max_candidates=8):
        self.max_candidates = max(2, int(max_candidates))
        self.items = [Candidate(id="c0_original", image=original, parent=None, operation="original", selected=True)]

    @property
    def original(self):
        return self.items[0]

    def add(self, image, parent, operation, variant="", models=None, seconds=0.0):
        # The limit is on the images held at once: a refused candidate is released and does not count. The work stays bounded because the total is capped as well.
        if sum(1 for item in self.items if item.image is not None) >= self.max_candidates or len(self.items) >= self.max_candidates * self.TOTAL_FACTOR:
            raise PoolFull(f"The candidate limit ({self.max_candidates}) was reached.")
        candidate = Candidate(
            id=f"c{len(self.items)}_{operation}", image=image, parent=parent.id, operation=operation, variant=variant,
            models=list(models or []), seconds=seconds,
        )
        self.items.append(candidate)
        return candidate

    def select(self, candidate):
        for item in self.items:
            item.selected = item is candidate
        return candidate

    def describe(self):
        return [item.describe() for item in self.items]
