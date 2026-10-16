"""Put one real enhancement run into a DEMO database, so there is data to show (tables, files, logs) during a demonstration.

    DB_NAME=/path/to/demo.sqlite3 MEDIA_ROOT=/path/to/demo_media python scripts/seed_demo.py --photo some.jpg [--dark]

Point DB_NAME and MEDIA_ROOT at throwaway locations, never at a database you care about: the script creates a demo user, uploads the
photograph through the real API endpoints, runs the enhancement (Celery in eager mode, so no broker is needed) and prints where each
piece of data now lives. ``--dark`` darkens the photograph first so the low-light path has something to do. Run
``python manage.py migrate`` on the demo database first.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photo", required=True, type=Path)
    parser.add_argument("--dark", action="store_true")
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
    image = cv2.imread(str(arguments.photo), cv2.IMREAD_COLOR)
    scale = 1280 / max(image.shape[:2])
    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
    if arguments.dark:
        image = np.clip(image.astype(np.float32) * 0.25, 0, 255).astype(np.uint8)
    encoded = cv2.imencode(".png", image)[1].tobytes()

    client = APIClient()
    client.force_authenticate(user)
    upload = client.post("/api/images/upload/single/", {"file": SimpleUploadedFile("demo.png", encoded, "image/png")}, format="multipart")
    image_id = upload.json()["images"][0]["id"]
    queued = client.post(f"/api/enhancements/images/{image_id}/", {"level": "auto", "style": "natural"}, format="json")
    result = client.get(f"/api/enhancements/results/{queued.json()['id']}/").json()
    print(f"database : {settings.DATABASES['default']['NAME']}")
    print(f"files    : {settings.MEDIA_ROOT}")
    print(f"user     : {arguments.username} (id {user.pk})   image id {image_id}   result id {result['id']}")
    print(f"result   : status={result['status']} outcome={result.get('enhancement_outcome')} | {result.get('enhancement_summary')}")
    print(f"identity : {result.get('identity_status')}")


if __name__ == "__main__":
    main()
