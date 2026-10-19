"""Timings and progress for one gallery edit (``process_image_edit``).

The edit renders a full-size photograph, which can take a long time, so two things are recorded as it goes:

* **where the time went**: milliseconds per stage (reading the source, each model slider, the tone render, the guard loop and its attempts, encoding,
  verifying, saving), kept in the edit's ``quality_metadata["timings_ms"]`` and written to the log as one line without any photo content;
* **how far it is**: a coarse stage (``reading``, ``enhancing``, ``finishing``) and a percentage that the polling client reads from the edit.

``apply_gallery_adjustments`` and ``apply_model_controls`` take an optional instrument; without one (the live preview, the advisor, the dynamic
style of the pipeline) they use ``NO_INSTRUMENT`` and behave exactly as before.
"""
import time
from contextlib import contextmanager, nullcontext

# The stages a client may see while an edit is rendering (``queued`` is the edit before a worker takes it).
READING, ENHANCING, FINISHING = "reading", "enhancing", "finishing"
# Coarse progress (percent): reading the source takes the start, the model sliders and the tone render share MODELS_FROM..MODELS_TO, encoding and saving the rest.
READING_AT, MODELS_FROM, MODELS_TO, FINISHING_AT, SAVED_AT = 5, 15, 80, 85, 95


class NoInstrument:
    """Records nothing; the default for every caller that does not ask for timings."""

    def stage(self, name):
        return nullcontext()

    def progress(self, stage, percent):
        return None

    def attempt_done(self, milliseconds):
        return None


NO_INSTRUMENT = NoInstrument()


class EditInstrument(NoInstrument):
    """Accumulates milliseconds per named stage and reports progress through ``on_progress(stage, percent)``."""

    def __init__(self, on_progress=None, clock=time.perf_counter):
        self._clock = clock
        self._on_progress = on_progress
        self._started = clock()
        self.stage_ms = {}
        self.attempt_ms = []

    @contextmanager
    def stage(self, name):
        started = self._clock()
        try:
            yield
        finally:
            self.stage_ms[name] = round(self.stage_ms.get(name, 0.0) + (self._clock() - started) * 1000.0, 1)

    def progress(self, stage, percent):
        if self._on_progress is not None:
            self._on_progress(stage, int(percent))

    def attempt_done(self, milliseconds):
        self.attempt_ms.append(round(milliseconds, 1))

    def timings_ms(self):
        """``{stage: ms, ..., "total": ms}``: every stage that ran, and the time since the instrument was made."""
        return {**self.stage_ms, "total": round((self._clock() - self._started) * 1000.0, 1)}
