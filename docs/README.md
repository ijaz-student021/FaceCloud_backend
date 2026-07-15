# FaceCloud Technical Documentation

This directory contains implementation documentation for every FaceCloud feature. Each feature is documented in a separate Markdown file so the files can later be reviewed, combined, and converted into the final Word report.

The complete approved scope and implementation roadmap are maintained in [FEATURES.md](FEATURES.md).

## Documentation workflow

For every feature:

1. Create the feature plan.
2. Implement the backend and migrations.
3. Add automated tests.
4. Run verification checks.
5. Complete the feature's documentation file.
6. Update the feature index and status.

## Feature documentation index

| No. | Feature | Backend status | Documentation |
|---:|---|---|---|
| 01 | Authentication, accounts, and authorization | Complete | [01-authentication-and-authorization.md](features/01-authentication-and-authorization.md) |
| 02 | Image upload and secure storage | Complete | [02-image-upload-and-secure-storage.md](features/02-image-upload-and-secure-storage.md) |
| 03 | Image preprocessing, format conversion, and standardization | Complete | [03-image-preprocessing-and-standardization.md](features/03-image-preprocessing-and-standardization.md) |
| 04 | OpenCV and Zero-DCE enhancement | Planned | To be created during implementation |
| 05 | Image quality assessment | Planned | To be created during implementation |
| 06 | Face detection and validation | Planned | To be created during implementation |
| 07 | Background batch processing and progress | Planned | To be created during implementation |
| 08 | Face recognition and identity grouping | Planned | To be created during implementation |
| 09 | Moderator review system | Planned | To be created during implementation |
| 10 | Reports, downloads, sharing, and watermarks | Planned | To be created during implementation |
| 11 | Admin analytics and system health | Planned | To be created during implementation |
| 12 | Live camera detection and enhancement | Planned | To be created during implementation |
| 13 | Model and live comparison pages | Planned | To be created during implementation |
| 14 | Cloud storage, deployment, and production security | Planned | To be created during implementation |

Use [FEATURE_TEMPLATE.md](features/FEATURE_TEMPLATE.md) as the starting point for each new feature document.

Database migration and rollback details are maintained in [database-migration.md](database-migration.md). PostgreSQL 18 is the active development database as of 2026-07-15.

## Final Word document preparation

At the end of development, these files can be assembled in feature order. Repeated sections such as configuration and test commands can then be consolidated into appendices, while each feature's purpose, workflow, implementation, and results remain separate chapters.
