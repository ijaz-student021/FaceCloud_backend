#!/usr/bin/env bash
# One command for the defence: a separate demo database + files + log, an admin login, one enhanced photo, then the server.
#   bash scripts/defense_demo.sh [path/to/a/photo.jpg]        (no photo given: the first .jpg/.jpeg in ~/Desktop/images)
# Nothing here touches the project's own db.sqlite3 or media/ folder. Demo login (local use only):
#   admin site  http://127.0.0.1:8000/admin/     username: admin     password: DefenseDemo!2026
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO="$HOME/FaceCloud_defense_db"
mkdir -p "$DEMO"
export DB_NAME="$DEMO/demo.sqlite3" MEDIA_ROOT="$DEMO/media" LOG_FILE="$DEMO/facecloud.log"
export IMAGE_ENCRYPTION_ENABLED=True IMAGE_ENCRYPTION_KEY="defence-demo-encryption-key-not-for-real-use-0123456789"
export ENHANCEMENT_RENDER_POLICY=adaptive_v1 ZERO_DCE_ENABLED=True RETINEXFORMER_ENABLED=False NAFNET_DENOISE_ENABLED=False NAFNET_DEBLUR_ENABLED=False HD_RESTORATION_ENABLED=False
PY="${PYTHON:-venv/bin/python}"
"$PY" manage.py migrate -v 0
DJANGO_SUPERUSER_USERNAME=admin DJANGO_SUPERUSER_EMAIL=admin@example.com DJANGO_SUPERUSER_PASSWORD='DefenseDemo!2026' \
  "$PY" manage.py createsuperuser --noinput 2>/dev/null || "$PY" manage.py shell -c "from django.contrib.auth import get_user_model as g; u=g().objects.get(username='admin'); u.set_password('DefenseDemo!2026'); u.is_email_verified=True; u.save()"
"$PY" manage.py shell -c "from django.contrib.auth import get_user_model as g; u=g().objects.get(username='admin'); u.is_email_verified=True; u.save()"
PHOTO="${1:-$(ls "$HOME"/Desktop/images/*.jp*g 2>/dev/null | head -1)}"
if [ -n "${PHOTO:-}" ]; then "$PY" scripts/seed_demo.py --photo "$PHOTO" --dark; else echo "No photo found: pass one as an argument to seed data."; fi
echo
echo "Database : $DB_NAME"; echo "Files    : $MEDIA_ROOT"; echo "Log file : $LOG_FILE   (watch it with: tail -f \"$LOG_FILE\")"
echo "Admin    : http://127.0.0.1:8000/admin/   username: admin   password: DefenseDemo!2026"
echo "API docs : http://127.0.0.1:8000/api/docs/"
exec "$PY" manage.py runserver 127.0.0.1:8000
