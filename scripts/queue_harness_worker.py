"""Start a Celery worker for ``check_queue_integration.py``, with an optional fault.

Test tooling only; nothing in the application imports it. The fault is read
from ``HARNESS_FAULT`` and wraps the real ``enhance_bgr`` *after* the result has
been claimed, so the worker behaves exactly like a production worker except:

* ``sleep:N``            one stage that takes N seconds with no checkpoint in it
                         (the claim stays alive only if it is renewed);
* ``sleep_then_fail:N``  the same, then the stage raises an unexpected error.

Arguments are passed to ``celery`` unchanged::

    python scripts/queue_harness_worker.py -A config worker --pool=solo -Q celery
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def install_fault(spec):
    from enhancement import services

    kind, _, seconds = spec.partition(":")
    if kind not in {"sleep", "sleep_then_fail"}:
        raise SystemExit(f"Unknown HARNESS_FAULT {spec!r}")
    real = services.enhance_bgr

    def faulty(*args, **kwargs):
        print(f"HARNESS_FAULT {kind}: one stage of {seconds} s starts", flush=True)
        time.sleep(float(seconds))
        if kind == "sleep_then_fail":
            print("HARNESS_FAULT: raising the injected failure", flush=True)
            raise RuntimeError("injected failure (queue harness)")
        return real(*args, **kwargs)

    services.enhance_bgr = faulty


if __name__ == "__main__":
    import django

    django.setup()
    if os.environ.get("HARNESS_FAULT"):
        install_fault(os.environ["HARNESS_FAULT"])
    from celery.__main__ import main

    sys.argv = ["celery", *sys.argv[1:]]
    main()
