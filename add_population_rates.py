"""
Join district population (from fetch_population.py) into the two flood
layers and add population-normalized fields, so the heatmap stops favoring
districts that simply report more / are more populous.

Input:
  - population_por_distrito.json
  - emergencias_por_distrito.geojson
  - emergencias_distrito_costa_nino.geojson

Output (overwrites the same two files, adding fields):
  - poblacion_2020
  - event_rate_per_1000       (event_count / population * 1000)
  - afectados_rate_per_1000   (total_afectados / population * 1000)
  - note: when a district has no population match, these fields are null
    rather than 0 - do not treat missing population as zero population.
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

LAYER_FILES = [
    "emergencias_por_distrito.geojson",
    "emergencias_distrito_costa_nino.geojson",
]


def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def add_rates(geojson: dict, population: dict) -> tuple[dict, int]:
    missing = 0
    for feat in geojson["features"]:
        props = feat["properties"]
        ubigeo = props.get("cod_ubigeo")
        pop_entry = population.get(ubigeo)

        if not pop_entry or not pop_entry.get("pob_2020"):
            props["poblacion_2020"] = None
            props["event_rate_per_1000"] = None
            props["afectados_rate_per_1000"] = None
            missing += 1
            continue

        pob = pop_entry["pob_2020"]
        props["poblacion_2020"] = pob
        props["event_rate_per_1000"] = round(props["event_count"] / pob * 1000, 3)
        props["afectados_rate_per_1000"] = round(props["total_afectados"] / pob * 1000, 3)

    return geojson, missing


if __name__ == "__main__":
    population = load_json(DATA_DIR / "population_por_distrito.json")
    print(f"Loaded population for {len(population)} districts")

    for filename in LAYER_FILES:
        path = DATA_DIR / filename
        geojson = load_json(path)

        geojson, missing = add_rates(geojson, population)
        print(f"\n{filename}:")
        print(f"  {len(geojson['features'])} districts, {missing} without a population match")

        path.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
        print(f"  updated -> {filename}")
