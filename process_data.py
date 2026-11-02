"""
Filter the raw INDECI event log (SINPAD) down to flood-relevant phenomena
and aggregate it to district level, producing a lightweight GeoJSON that
is actually usable as a heatmap source layer.

Input:
  - registro_emergencias_indeci.geojson  (raw, ~195 MB, 121k events, all hazard types)
  - puntos_criticos_inundacion.geojson   (raw, CENEPRED validated risk points)

Output:
  - emergencias_inundacion_filtered.geojson     (point-level, flood-only events)
  - emergencias_por_distrito.geojson            (district-level counts, nationwide layer)
  - emergencias_distrito_costa_nino.geojson     (same districts, filtered to the
                                                   north-coast departments El Nino
                                                   Costero actually hits - this is
                                                   the second map layer)

Known data limitation (confirm against source before presenting results):
  The 'fecha' field is historical only - coverage effectively ends around
  2021-12-05 (parsed from DD/MM/YYYY strings; do not trust min()/max() on
  the raw string, it sorts lexicographically, not chronologically). This
  dataset is a historical baseline, not a live feed for the current
  2026-2027 El Nino event.

Region split, not a year split:
  The coastal layer is a GEOGRAPHIC filter (departments under ENFEN's Nino
  1+2 monitoring zone), not a filter by "official El Nino years." We do not
  have a verified year-by-year ENFEN classification for 2003-2021 to tag
  events with, and guessing which years counted would reintroduce exactly
  the unvalidated-labeling problem this project is trying to avoid. The 2017
  spike (900 coastal events vs a ~130-200 baseline) already shows up on its
  own from the geographic + yearly breakdown - that is real signal, not an
  assumption baked into the pipeline.
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

FLOOD_PHENOMENA = {
    "LLUVIAS INTENSAS",
    "INUNDACION",
    "INUNDACIÓN POR DESBORDE DE RIO",
    "INUNDACIÓN POR DESBORDE DE CANALES",
    "INUNDACIÓN POR DESBORDE LAGO O LAGUNA",
    "INUNDACIÓN POR DESBORDE EN LA RUPTURA DE DIQUES",
    "HUAYCOS",
    "DESLIZAMIENTO",
    "DERRUMBES",
    "DERRUMBE CERROS",
    "RIADA",
    "EROSION FLUVIAL",
    "TEMPORALES (VIENTOS CON LLUVIAS)",
}

# Departments under ENFEN's Nino 1+2 coastal monitoring zone - the area
# El Nino Costero specifically affects. Source: ENFEN official communiques
# (ENFEN, 2026) and INDECI emergency reports, consistently naming these four
# as the north-coast departments. NOT a guess at "which years were El Nino."
COSTA_NORTE = {"TUMBES", "PIURA", "LAMBAYEQUE", "LA LIBERTAD"}


def parse_fecha(raw: str):
    """Parse the DD/MM/YYYY date string used by this dataset. Returns None if unparseable."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d/%m/%Y")
    except ValueError:
        return None


def load_geojson(path: Path) -> list:
    with path.open(encoding="utf-8") as f:
        return json.load(f)["features"]


def filter_flood_events(features: list) -> list:
    """Keep only flood-relevant phenomena, and attach a parsed datetime for later filtering."""
    kept = []
    for feat in features:
        props = feat["properties"]
        if props.get("fenomeno") in FLOOD_PHENOMENA:
            props["fecha_parsed"] = parse_fecha(props.get("fecha"))
            kept.append(feat)
    return kept


def aggregate_by_district(features: list) -> dict:
    """
    Collapse point events into one feature per district, with the event count
    and total affected population - this is the weighted heatmap input.
    """
    buckets = defaultdict(lambda: {
        "count": 0,
        "total_afectados": 0,
        "total_damnificados": 0,
        "total_fallecidos": 0,
        "departamento": None,
        "provincia": None,
        "distrito": None,
        "lats": [],
        "lons": [],
    })

    for feat in features:
        props = feat["properties"]
        ubigeo = props.get("cod_ubigeo")
        if not ubigeo:
            continue

        b = buckets[ubigeo]
        b["count"] += 1
        b["total_afectados"] += props.get("safecta") or 0
        b["total_damnificados"] += props.get("sdamni") or 0
        b["total_fallecidos"] += props.get("sfalle") or 0
        b["departamento"] = props.get("departamen")
        b["provincia"] = props.get("provincia")
        b["distrito"] = props.get("distrito")

        lon, lat = feat["geometry"]["coordinates"]
        b["lats"].append(lat)
        b["lons"].append(lon)

    out_features = []
    for ubigeo, b in buckets.items():
        lat_centroid = sum(b["lats"]) / len(b["lats"])
        lon_centroid = sum(b["lons"]) / len(b["lons"])
        out_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_centroid, lat_centroid]},
            "properties": {
                "cod_ubigeo": ubigeo,
                "departamento": b["departamento"],
                "provincia": b["provincia"],
                "distrito": b["distrito"],
                "event_count": b["count"],
                "total_afectados": b["total_afectados"],
                "total_damnificados": b["total_damnificados"],
                "total_fallecidos": b["total_fallecidos"],
                "region": "costa_norte" if b["departamento"] in COSTA_NORTE else "otro",
            },
        })

    return {"type": "FeatureCollection", "features": out_features}


def split_coastal_layer(district_fc: dict) -> dict:
    """Return a second FeatureCollection with only the north-coast (El Nino Costero) districts."""
    costa_features = [
        f for f in district_fc["features"] if f["properties"]["region"] == "costa_norte"
    ]
    return {"type": "FeatureCollection", "features": costa_features}


if __name__ == "__main__":
    print("Loading raw event log...")
    raw_events = load_geojson(DATA_DIR / "registro_emergencias_indeci.geojson")
    print(f"  {len(raw_events)} total events (all hazard types)")

    flood_events = filter_flood_events(raw_events)
    print(f"  {len(flood_events)} flood-relevant events")

    parsed_dates = [f["properties"]["fecha_parsed"] for f in flood_events if f["properties"]["fecha_parsed"]]
    if parsed_dates:
        print(f"  date range: {min(parsed_dates).date()} -> {max(parsed_dates).date()}")

    # Save the filtered point-level file (still useful for drill-down)
    for feat in flood_events:
        feat["properties"]["fecha_parsed"] = (
            feat["properties"]["fecha_parsed"].isoformat() if feat["properties"]["fecha_parsed"] else None
        )
    filtered_fc = {"type": "FeatureCollection", "features": flood_events}
    (DATA_DIR / "emergencias_inundacion_filtered.geojson").write_text(
        json.dumps(filtered_fc, ensure_ascii=False), encoding="utf-8"
    )
    print("  saved -> emergencias_inundacion_filtered.geojson")

    # Save the district-aggregated file (layer 1: nationwide flood history)
    district_fc = aggregate_by_district(flood_events)
    print(f"\n  {len(district_fc['features'])} districts with flood history")
    (DATA_DIR / "emergencias_por_distrito.geojson").write_text(
        json.dumps(district_fc, ensure_ascii=False), encoding="utf-8"
    )
    print("  saved -> emergencias_por_distrito.geojson  (layer 1: nacional)")

    # Save the coastal-only subset (layer 2: El Nino Costero zone)
    costa_fc = split_coastal_layer(district_fc)
    print(f"  {len(costa_fc['features'])} districts in the Nino Costero coastal zone")
    (DATA_DIR / "emergencias_distrito_costa_nino.geojson").write_text(
        json.dumps(costa_fc, ensure_ascii=False), encoding="utf-8"
    )
    print("  saved -> emergencias_distrito_costa_nino.geojson  (layer 2: costa norte)")
