"""Which place is a position in? Answered from a file on this machine; nothing is sent anywhere.

The file is GeoNames ``cities1000.txt`` (every place with more than 1,000 people, about 171,000; tab-separated, CC BY 4.0, https://www.geonames.org), with the optional
``countryInfo.txt`` and ``admin1CodesASCII.txt`` beside it for country and region names. It is not part of the repository. Place names are **off** unless the file is there, its SHA-256
equals ``LOCATION_GAZETTEER_SHA256``, and the owner's approval is recorded in ``docs/model-licenses/location-gazetteer.md`` (``Owner approval: approved``, naming that checksum):
``status()`` says which of these is missing.

A position belongs to the nearest listed place within ``LOCATION_MAX_DISTANCE_KM``; farther than that (open sea, desert) it has coordinates but no place. Two refinements, so a
photograph is named the way a person would name it:

* parts of a town (feature code ``PPLX``: neighbourhoods and "sections of a populated place") and abandoned or historical places are never chosen;
* when the nearest place has fewer than ``BIG_POPULATION`` people and a place with at least that many lies within ``BIG_KM``, the bigger place is chosen (a suburb or village next to a
  city is named after the city).
Both were chosen by trying alternatives on 40 named landmarks (``docs/evaluation/2026-10-22-places.md``), which makes the reported accuracy optimistic.
"""
import hashlib
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings

CITIES = "cities1000.txt"
COUNTRIES = "countryInfo.txt"
REGIONS = "admin1CodesASCII.txt"
EARTH_RADIUS_KM = 6371.0088
NOT_A_PLACE = frozenset({"PPLX", "PPLQ", "PPLH", "PPLW", "PPLCH", "PPLR"})       # a part of a town, abandoned, destroyed, historical, a religious settlement
BIG_POPULATION = 20000
BIG_KM = 15.0
ATTRIBUTION = "Place names: GeoNames (https://www.geonames.org), licensed CC BY 4.0."


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _verify(directory, expected, record, file_key, record_key):
    """``(ok, reason)`` for one combination of settings and file states (so the 32 MB file is hashed once, not per photograph)."""
    path = Path(directory) / CITIES
    if not path.is_file():
        return False, "The place-name file is not installed."
    if len(expected) != 64:
        return False, "LOCATION_GAZETTEER_SHA256 is not set."
    if _sha256(path) != expected.lower():
        return False, "The place-name file is not the one whose checksum is configured."
    try:
        text = Path(record).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "The licence record is missing."
    if expected.lower() not in text.lower():
        return False, "The licence record does not list this file's checksum."
    if not re.search(r"^Owner approval:\s*approved\b", text, flags=re.IGNORECASE | re.MULTILINE):
        return False, "The owner has not approved the place-name licence record."
    return True, ""


def _stamp(path):
    try:
        info = Path(path).stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def status():
    """``(usable, reason)``: whether place names can be produced now, and if not, why not."""
    if not settings.LOCATION_ENABLED:
        return False, "Location is switched off (LOCATION_ENABLED)."
    directory = str(settings.LOCATION_GAZETTEER_DIR or "")
    return _verify(directory, settings.LOCATION_GAZETTEER_SHA256, str(settings.LOCATION_LICENSE_RECORD_PATH or ""), _stamp(Path(directory) / CITIES), _stamp(settings.LOCATION_LICENSE_RECORD_PATH))


def available():
    return status()[0]


def _read_table(path, columns):
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) > max(columns):
                    rows.append([parts[index] for index in columns])
    except OSError:
        return []
    return rows


@lru_cache(maxsize=2)
def _load(directory, file_key):
    base = Path(directory)
    countries = {code: name for code, name in _read_table(base / COUNTRIES, (0, 4))}
    regions = {key: name for key, name in _read_table(base / REGIONS, (0, 2))}
    ids, names, country_codes, admin1, latitudes, longitudes, populations, usable = [], [], [], [], [], [], [], []
    with open(base / CITIES, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split("\t")
            if len(parts) < 15:
                continue
            try:
                latitude, longitude, geoname_id = float(parts[4]), float(parts[5]), int(parts[0])
                population = int(parts[14] or 0)
            except ValueError:
                continue
            latitudes.append(latitude)
            longitudes.append(longitude)
            ids.append(geoname_id)
            populations.append(population)
            usable.append(parts[7] not in NOT_A_PLACE)
            names.append(parts[1])
            country_codes.append(parts[8])
            admin1.append(parts[10])
    return {
        "ids": ids, "names": names, "country_codes": country_codes, "admin1": admin1, "countries": countries, "regions": regions,
        "lat": np.radians(np.array(latitudes)), "lon": np.radians(np.array(longitudes)), "lat_deg": latitudes, "lon_deg": longitudes,
        "population": np.array(populations), "usable": np.array(usable, dtype=bool),
    }


@lru_cache(maxsize=20000)
def _nearest(latitude, longitude, directory, file_key, max_km):
    data = _load(directory, file_key)
    lat, lon = math.radians(latitude), math.radians(longitude)
    delta_lat = data["lat"] - lat
    delta_lon = data["lon"] - lon
    a = np.sin(delta_lat / 2) ** 2 + np.cos(lat) * np.cos(data["lat"]) * np.sin(delta_lon / 2) ** 2
    distance = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.minimum(a, 1.0)))
    candidates = np.where(data["usable"], distance, np.inf)
    index = int(np.argmin(candidates))
    if float(candidates[index]) > max_km:
        return None
    if data["population"][index] < BIG_POPULATION:
        bigger = np.flatnonzero(data["usable"] & (data["population"] >= BIG_POPULATION) & (distance <= min(BIG_KM, max_km)))
        if bigger.size:
            index = int(bigger[np.argmin(distance[bigger])])
    code = data["country_codes"][index]
    return {
        "geoname_id": data["ids"][index], "name": data["names"][index], "country_code": code, "country": data["countries"].get(code, code),
        "region": data["regions"].get(f"{code}.{data['admin1'][index]}", ""), "latitude": data["lat_deg"][index], "longitude": data["lon_deg"][index],
        "distance_km": round(float(distance[index]), 1),
    }


def nearest(latitude, longitude):
    """The nearest listed place to a position (rounded to about 110 m for the cache), or ``None`` when names are off or nothing is within the distance limit."""
    if not available():
        return None
    directory = str(settings.LOCATION_GAZETTEER_DIR)
    return _nearest(round(latitude, 3), round(longitude, 3), directory, _stamp(Path(directory) / CITIES), float(settings.LOCATION_MAX_DISTANCE_KM))
