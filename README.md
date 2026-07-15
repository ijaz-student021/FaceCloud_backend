# FaceCloud Backend

FaceCloud is an AI-powered facial image enhancement, validation, organization, and secure storage platform. This repository contains the Django REST Framework backend for authentication, role-based authorization, secure image ingestion, processing workflows, identity management, moderation, analytics, and protected storage.

## Current implementation

- Secure registration, email verification, and password recovery
- JWT login with access/refresh rotation and token revocation
- User, moderator, and administrator authorization
- Profile, logout, account deletion, suspension, and audit events
- Single, multiple, and ZIP image uploads
- Content-based JPG, JPEG, and PNG validation
- ZIP-slip, ZIP-bomb, nested archive, and encrypted-entry protection
- Owner-only image listing, downloads, and deletion
- Secure UUID storage paths and physical cleanup
- PostgreSQL 18 development database
- Automated authentication, ownership, upload, and storage tests

## Planned capabilities

- Image preprocessing and standardization
- OpenCV and Zero-DCE enhancement
- Quality scoring and face validation
- Background batch processing and live progress
- Face recognition and identity grouping
- Moderator review and reporting
- Live camera face detection and enhancement comparison
- Private cloud storage and production deployment

## Technology

- Python and Django
- Django REST Framework
- PostgreSQL
- Simple JWT
- Pillow and OpenCV
- Planned Celery and Redis processing
- Planned AWS S3 storage

## Local setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

Update `.env` with local PostgreSQL credentials before running migrations. Never commit `.env`, local databases, media, backups, or the virtual environment.

## Verification

```powershell
python manage.py test accounts uploads --verbosity 2
python manage.py check
python manage.py makemigrations --check --dry-run
```

## Documentation

Feature-level implementation documentation is maintained in [`docs/features`](docs/features). The documentation index and database migration notes are available in [`docs/README.md`](docs/README.md).
