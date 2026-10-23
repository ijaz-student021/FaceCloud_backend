"""Download the GeoNames place-name files used by Places, and print their SHA-256.

    python scripts/fetch_gazetteer.py [DIRECTORY]

Default directory: ``.runtime/data/geonames`` (``LOCATION_GAZETTEER_DIR``). Downloads ``cities1000.zip`` (unpacked to ``cities1000.txt``), ``countryInfo.txt`` and
``admin1CodesASCII.txt`` from https://download.geonames.org/export/dump/ (CC BY 4.0; credit GeoNames). It changes nothing else and does not turn anything on: set
``LOCATION_GAZETTEER_SHA256`` to the printed ``cities1000.txt`` checksum and have the owner approve ``docs/model-licenses/location-gazetteer.md``. GeoNames refreshes these files daily,
so a new download has a new checksum.
"""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://download.geonames.org/export/dump/"
FILES = ("cities1000.zip", "countryInfo.txt", "admin1CodesASCII.txt")


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / ".runtime" / "data" / "geonames")
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        with urllib.request.urlopen(BASE + name, timeout=120) as response:  # nosec B310 - a fixed https address
            payload = response.read()
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                member = "cities1000.txt"
                (target / member).write_bytes(archive.read(member))
            name = member
        else:
            (target / name).write_bytes(payload)
        print(f"{hashlib.sha256((target / name).read_bytes()).hexdigest()}  {(target / name).stat().st_size:>10}  {target / name}")
    print("Set LOCATION_GAZETTEER_SHA256 to the cities1000.txt line above, and approve docs/model-licenses/location-gazetteer.md.")


if __name__ == "__main__":
    main()
