# SQLite to PostgreSQL Migration Plan

## Current position

PostgreSQL 18 is the active FaceCloud development database. The application settings support PostgreSQL through private `.env` values and retain SQLite as an explicit fallback. The original SQLite database and uploaded media are preserved in the ignored `backups/` directory for rollback.

Migration completed on 2026-07-15:

- PostgreSQL database: `facecloud`
- Dedicated role: `facecloud_user`
- Migrated records: 3 users, 4 authentication events, 1 upload batch, 1 image record
- Sequences reset: 4
- Django system check: passed
- Migration drift check: passed
- Combined authentication and upload suite: 29 tests passed against PostgreSQL

## Completed migration sequence

1. Stop application writes.
2. Back up `db.sqlite3` and the `media/` directory.
3. Export portable Django data from SQLite.
4. Install and start PostgreSQL.
5. Create the `facecloud` database and a dedicated `facecloud_user` role.
6. Create `.env` from `.env.example` with a strong local password.
7. Run all Django migrations against PostgreSQL.
8. Import the portable data if current development records must be retained.
9. Reset PostgreSQL sequences after importing explicit IDs.
10. Compare record counts and relationships.
11. Run the complete automated suite against PostgreSQL.
12. Perform registration, login, upload, listing, and download smoke tests.

## Storage boundary

PostgreSQL stores users, upload batches, image metadata, processing state, audit events, and relationships. Image files remain in `media/` during development and will later move to private object storage such as AWS S3.

## Rollback

If PostgreSQL becomes unavailable during development, temporarily rename `.env` and explicitly confirm the SQLite fallback before running commands. Do not delete the SQLite and media backup until final deployment and backup verification are complete.
