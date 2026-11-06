"""Which photograph the engine is working on, for its log lines.

A context variable, so that neither the engine's signature nor ``enhance_bgr``'s changes (the tests that replace ``enhance_bgr`` keep their fixed signatures): the task that enhances a stored
result wraps the call in ``photo(label)``, the engine reads ``PHOTO.get()`` when it logs that it skipped a model. Outside a task it reads ``"n/a"``.
"""
import contextlib
import contextvars

PHOTO = contextvars.ContextVar("adaptive_photo", default="n/a")


@contextlib.contextmanager
def photo(label):
    token = PHOTO.set(str(label))
    try:
        yield
    finally:
        PHOTO.reset(token)
