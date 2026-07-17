# Feature 42: PostgreSQL Data Platform
## 1. Overview

PostgreSQL 18 is the active FaceCloud relational database. It stores accounts,
authentication events, upload metadata, processing state, enhancement jobs, and
application relationships. Image binaries remain in private file storage rather
than inside the database.

Current status: **Complete**.

## 2. Completed work

- Installed and verified PostgreSQL 18
- Created the `facecloud` database
- Created a dedicated application database role
- Configured private environment-based Django connection settings
- Retained SQLite only as an explicit development fallback
- Applied all Django migrations to PostgreSQL
- Migrated the required development records from SQLite
- Reset database sequences after importing explicit identifiers
- Compared migrated record counts and relationships
- Verified registration, login, upload, listing, and download operations
- Enabled PostgreSQL test-database execution
- Preserved ignored local rollback backups of the previous database and media

## 3. Migration workflow used

```text
Stop application writes
  -> back up SQLite and media
  -> export portable Django data
  -> configure PostgreSQL privately
  -> apply migrations
  -> import required records
  -> reset sequences
  -> validate counts and relationships
  -> run automated tests and smoke tests
```

## 4. Security and operational rules

- Database credentials remain in `.env` and are never committed.
- `.env.example` contains placeholders only.
- The application uses a dedicated role instead of the PostgreSQL superuser.
- Database backups and uploaded media are excluded from Git.
- Production deployments must use encrypted connections and managed backups.
- Tenant and ownership checks remain application requirements; PostgreSQL alone
  does not replace object-level authorization.

## 5. Storage boundary

PostgreSQL stores metadata, ownership, statuses, audit events, and relationships.
Original and generated images use private local storage during development and
will later move to private object storage without changing their ownership model.
