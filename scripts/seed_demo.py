"""Put one real enhancement run into a DEMO database, so there is data to show (tables, files, logs) during a demonstration.

    DB_NAME=/path/to/demo.sqlite3 MEDIA_ROOT=/path/to/demo_media python scripts/seed_demo.py --photo some.jpg [--dark]

Point DB_NAME and MEDIA_ROOT at throwaway locations, never at a database you care about: the script creates a demo user, uploads the
photograph through the real API endpoints, runs the enhancement (Celery in eager mode, so no broker is needed) and prints where each
piece of data now lives. ``--dark`` darkens the photograph first so the low-light path has something to do: to x0.40 of its light (a normal photograph, brightness about 70,
becomes about 28, which is measured to be brightened roughly twofold), or further only for a photograph so bright that x0.40 would leave it above brightness 30 (see ``darken``). Each photograph's
quality verdict and reasons are printed, and a photograph the checks refuse is reported and skipped instead of stopping the run. Run ``python manage.py migrate`` on the demo database first.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

DARK_FACTOR = 0.40          # the light left in the demo's dark photograph
DARK_CEILING_MEAN = 30.0    # the brightest (0-255) the darkened photograph may be: a brighter one is darkened further, because the lift is measured to double at 28, not at 35


def darken(image):
    """``(darker image, factor used)``: x0.40, or a smaller factor when that would leave the photograph brighter than ``DARK_CEILING_MEAN``.

    The quality check refuses a photograph that is practically black (mean brightness under about 6), and the low-light enhancement leaves one that is only a little dark alone,
    so the demo needs a photograph that is clearly dark and still has signal: x0.40 of a normal portrait (brightness about 70) is about 28, which is measured to be lifted.
    """
    import cv2
    import numpy as np

    mean = float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean())
    factor = min(DARK_FACTOR, DARK_CEILING_MEAN / mean) if mean > 0 else DARK_FACTOR
    return np.clip(image.astype(np.float32) * factor, 0, 255).astype(np.uint8), factor


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photo", required=True, type=Path, nargs="+", help="One or more photographs.")
    parser.add_argument("--dark", action="store_true", help="Darken every photograph.")
    parser.add_argument("--mix", action="store_true", help="Cycle dark / noisy / untouched so the results show different outcomes.")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="DemoPassw0rd!7391")
    arguments = parser.parse_args()
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "True"

    import cv2
    import django
    import numpy as np

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from rest_framework.test import APIClient

    user_model = get_user_model()
    user, _created = user_model.objects.get_or_create(username=arguments.username, defaults={"email": f"{arguments.username}@example.com",
                                                                                              "is_email_verified": True})
    user.set_password(arguments.password)
    user.is_email_verified = True
    user.save()
    client = APIClient()
    login = client.post("/api/auth/login/", {"identifier": arguments.username, "password": arguments.password}, format="json")
    print(f"login    : HTTP {login.status_code} (writes an auth event)")
    client.force_authenticate(user)
    rng = np.random.default_rng(7)
    variants = ("dark", "noisy", "untouched") if arguments.mix else (("dark",) if arguments.dark else ("untouched",))
    for index, photo in enumerate(arguments.photo):
        image = cv2.imread(str(photo), cv2.IMREAD_COLOR)
        scale = 1280 / max(image.shape[:2])
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
        variant = variants[index % len(variants)]
        factor = None
        if variant == "dark":
            image, factor = darken(image)
        elif variant == "noisy":
            image = np.clip(image.astype(np.float32) + rng.normal(0, 18.0, image.shape), 0, 255).astype(np.uint8)
        encoded = cv2.imencode(".png", image)[1].tobytes()
        upload = client.post("/api/images/upload/single/", {"file": SimpleUploadedFile(f"demo_{variant}_{index + 1}.png", encoded, "image/png")},
                             format="multipart")
        image_id = upload.json()["images"][0]["id"]
        brightness = float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean())
        quality = client.get(f"/api/quality/images/{image_id}/")
        verdict = quality.json() if quality.status_code == 200 else {}
        reasons = ", ".join(verdict.get("rejection_reasons") or []) or "none"
        darkened = f" (darkened x{factor:.2f})" if factor else ""
        print(f"photo {index + 1}  : {variant:9s} image id {image_id}  brightness {brightness:.0f}{darkened}  verdict {verdict.get('decision', '?')}  reasons: {reasons}")
        queued = client.post(f"/api/enhancements/images/{image_id}/", {"level": "auto", "style": "natural"}, format="json")
        if queued.status_code >= 400:
            print(f"           not enhanced: HTTP {queued.status_code} {queued.json()}")
            continue
        result = client.get(f"/api/enhancements/results/{queued.json()['id']}/").json()
        print(f"           result id {result['id']}  {result['status']} / {result.get('enhancement_outcome')}")
    print(f"database : {settings.DATABASES['default']['NAME']}")
    print(f"files    : {settings.MEDIA_ROOT}")
    print(f"user     : {arguments.username} (id {user.pk}), password: {arguments.password}")


if __name__ == "__main__":
    main()
