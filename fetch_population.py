"""
Fetch district-level population from CENEPRED's SIGRID ArcGIS REST service.

Source layer: "Mapa de pobreza monetaria 2018" (Informacion_Complementaria /
MapServer / 6130400). Despite the poverty-map name, the layer carries a
population field per district (pob_2020), sourced from INEI's 2018 poverty
map study (published 2020).

Known limitation: this is population projected to 2020, not 2026. Six years
stale. District population is relatively stable for most of Peru (rural
areas especially), but fast-growing urban districts will be undercounted.
Use this as a reasonable denominator for rate calculations, not as a precise
current count.

Output:
  - population_por_distrito.json   {ubigeo: {departamento, provincia, distrito, pob_2020}}
    Attribute-only (returnGeometry=false) - we don't need the poverty
    polygons, just the population figure per district.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = (
    "https://sigrid.cenepred.gob.pe/arcgis/rest/services/"
    "Informacion_Complementaria/MapServer/6130400/query"
)

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
OUT_FILE = OUT_DIR / "population_por_distrito.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

FIELDS = "ubigeo,departamen,provincia,distrito,pob_2020,fecha_act"


def fetch_all(page_size: int = 1000) -> list:
    records = []
    offset = 0

    while True:
        params = {
            "where": "1=1",
            "outFields": FIELDS,
            "returnGeometry": "false",  # attributes only - no polygon geometry needed
            "f": "json",
            "resultRecordCount": page_size,
            "resultOffset": offset,
        }
        resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("features", [])
        records.extend(batch)
        print(f"  pulled {len(batch)} (total {len(records)})")

        if not batch or not data.get("exceededTransferLimit", False):
            break

        offset += page_size
        time.sleep(0.2)

    return records


if __name__ == "__main__":
    print("Fetching district population from CENEPRED SIGRID...")
    raw = fetch_all()

    population_by_ubigeo = {}
    for rec in raw:
        attrs = rec["attributes"]
        ubigeo = attrs.get("ubigeo")
        if not ubigeo:
            continue
        population_by_ubigeo[ubigeo] = {
            "departamento": attrs.get("departamen"),
            "provincia": attrs.get("provincia"),
            "distrito": attrs.get("distrito"),
            "pob_2020": attrs.get("pob_2020"),
        }

    print(f"\n  {len(population_by_ubigeo)} districts with population data")
    OUT_FILE.write_text(
        json.dumps(population_by_ubigeo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  saved -> {OUT_FILE.name}")
