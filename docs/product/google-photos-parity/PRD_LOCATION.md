# Feature 9: Places (where a photograph was taken)

Status: written 2026-10-22 before the work, after the project owner pointed out that the location work was still open (the main PRD listed place names as out of scope). Companion of [PRD.md](PRD.md).

## Story

As a member, I tap "Places" and see where my photographs were taken ("Lahore", "Murree", "Paris"), open one, and see only the photographs from there; I can search a city name; and I can remove the
location of a photograph I do not want placed. Nobody but me ever sees the coordinates.

## What the backend does today

Nothing: only the EXIF capture **time** is read (`uploads/capture.py`). GPS is never read or stored. Thumbnails, standardized copies and enhanced copies are re-encoded and carry no EXIF; the original
file is stored as uploaded (and is only ever returned to its owner).

## Decisions taken for the owner (reversible, listed for review)

| # | Decision | Why | Where it can be changed |
|---|---|---|---|
| L1 | GPS is read from the EXIF of each upload, locally, and stored **rounded to 4 decimals (about 11 m)**; `(0, 0)`, out-of-range and malformed values are ignored. | Enough for a city and a map pin; the exact spot of a home is not needed. | `uploads/capture.py`, `GPS_DECIMALS` |
| L2 | Place names come from an **offline** gazetteer (GeoNames `cities1000`, about 171,000 places with more than 1,000 people, CC BY 4.0). A photograph is placed in the nearest place within 50 km, else it has coordinates but no place. **No coordinate is ever sent to a third party** (no online geocoder). | Privacy and cost; the data is public. | `LOCATION_GAZETTEER_DIR`, `LOCATION_MAX_DISTANCE_KM` |
| L3 | The gazetteer is **not in the repository** and place names stay off until the owner approves its licence record (`docs/model-licenses/location-gazetteer.md`, `Owner approval:`), its SHA-256 is configured and the file matches. Credit to GeoNames is returned with the places list and must be shown. | Same rule as the search model: nothing is switched on whose licence is not recorded by the owner. | `LOCATION_GAZETTEER_SHA256`, the record |
| L4 | Coordinates are exposed **only** to the owner, on `GET /api/images/{id}/location/` and in the data export. Photograph rows carry `has_location` and `place` (names), never coordinates. Moderator, administrator, share-link, ZIP and organization-export outputs carry none (a test crawls the organization admin's view for a photograph's coordinates). | Location is among the most sensitive data a photograph has. | serializers |
| L5 | A person can **remove** a photograph's location (`DELETE /api/images/{id}/location/`, or several at once). It is removed from FaceCloud's records and views and is not read again; it is **not** removed from the stored original file, which still contains it if it had it. | Honest limit: rewriting the original is a separate decision (it would change the file the person uploaded). | - |
| L6 | The whole feature is a switch (`LOCATION_ENABLED`, on by default): off, no GPS is read. | Additive, reversible. | settings |

## API

- `GET /api/places/?q=&limit=` (default 24, at most 100): `{attribution, places: [{id, name, region, country, country_code, photo_count, cover_thumbnail_url, latitude, longitude}]}`, most photographs first.
  `latitude`/`longitude` are the **place's** centre from the gazetteer, not any photograph's. `GET /api/places/{id}/` is one of them.
- `GET /api/images/my/?place=<id>` and `?country=<ISO code>`; photograph rows gain `has_location` and `place` (`{id, name, region, country, country_code}` or `null`).
- `GET /api/images/{id}/location/` -> `{latitude, longitude, place}` (404 when none); `DELETE` removes it; `POST /api/images/location/remove/ {image_ids}` removes several.
- `GET /api/search/?q=` gains `places`.
- `python manage.py backfill_location` reads the GPS of photographs uploaded before this feature, and places those that have coordinates once the gazetteer is installed. Safe to repeat.

## Acceptance

GPS from real EXIF (degrees/minutes/seconds, both hemispheres) parsed correctly and bad values ignored; places resolved from a gazetteer file with the nearest-place rule and the distance limit; places inert
without an approved, checksummed gazetteer; owner scope and trash on every count; no coordinate in any other role's output; the removal endpoints; the backfill idempotent; thumbnails and processed copies free of GPS;
accuracy measured on named landmarks with the result and its size reported.
