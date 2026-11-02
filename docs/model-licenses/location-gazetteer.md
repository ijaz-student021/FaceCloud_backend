# Places: the offline place-name file (GeoNames) and its licence record

Owner approval: pending

> Change the line above to `Owner approval: approved` only after you have read this record and decided that this data may be used. Until then place names stay off (photographs keep
> their coordinates but are not placed in a named place) and `python manage.py check` shows warning `places.W001` if the file is installed. This file is the engineering record of
> what was checked; it is not legal advice and it makes no licence decision for the project owner.

Checked on 2026-10-22; each statement was read from the source named next to it.

## The data

| | |
|---|---|
| Source | GeoNames, `https://download.geonames.org/export/dump/` (`cities1000.zip`, `countryInfo.txt`, `admin1CodesASCII.txt`) |
| Content | `cities1000`: "all cities with a population > 1000 or seats of adm div down to PPLA3 (ca 130.000)" (the snapshot checked has 171,572 rows); tab-separated, 19 columns (geonameid, name, asciiname, alternatenames, latitude, longitude, ..., country code, admin1 code, ..., population, ..., timezone, modification date) |
| Licence stated by the source | "This work is licensed under a Creative Commons Attribution 4.0 License, see https://creativecommons.org/licenses/by/4.0/" (the dump's readme); the export page says "cc-by licence" and "You should give credit to GeoNames when using data or web services with a link or another reference to GeoNames." |
| Obligation | credit GeoNames wherever place names are shown. The places API returns the sentence to show in `attribution`: "Place names: GeoNames (https://www.geonames.org), licensed CC BY 4.0." |
| Use here | read from a local folder only; no coordinate or photograph is ever sent to GeoNames or any other service |

### The snapshot that was evaluated (downloaded 2026-10-22)

GeoNames refreshes these files daily and keeps no fixed address for an old version, so the checksum names **one downloaded copy**. Keep the copy you approve (with your backups); a newer
download has a different checksum and needs this record updated.

| File | Bytes | SHA-256 |
|---|---|---|
| `cities1000.txt` (**the checksum the application verifies**) | 32,022,137 | `db45fbaae0eab7c88d2265fa179dd21dc7f7cfdb581fb60bb06379976262a074` |
| `countryInfo.txt` (country names, optional) | 31,690 | `c55f638cfaafb4fe45a9aa15120a0f1f78e8a34a8e1847d7b9320cf740fbc9ab` |
| `admin1CodesASCII.txt` (region names, optional) | 151,583 | `b577baa3476bbe94f0454545e517e1835401d79c5b1105c8e02cf1d4a505e8ec` |

To install: `python scripts/fetch_gazetteer.py` (downloads the three files into `LOCATION_GAZETTEER_DIR` and prints their checksums), or copy your approved copy there; set
`LOCATION_GAZETTEER_SHA256` to the `cities1000.txt` checksum; then `python manage.py backfill_location` places photographs that are already uploaded.

## How it is used, and what that means for a person

- A photograph is placed in the **nearest listed place within 50 km** (`LOCATION_MAX_DISTANCE_KM`); farther away it has coordinates but no place. "Nearest" is not "the district the photo is
  in": a photograph in a village beside a big city may be named after the village.
- Coordinates are stored rounded to about 11 m and shown only to the photograph's owner. The places list shows each place's own centre from this file, not any photograph's position.
- A person can remove a photograph's location in FaceCloud; that does not rewrite the stored original file.

## What was not verified

- Legal advice was not taken; the owner decides whether CC BY 4.0 with attribution is acceptable for their use.
- The completeness and correctness of the data (GeoNames is community-maintained); the accuracy measured on named landmarks is in `docs/evaluation/2026-10-22-places.md`.
- Names are shown as GeoNames spells them (ASCII/English-leaning `name`), not localised.

## Review

Revalidate when the file, its checksum or source, or the way it is used changes.
