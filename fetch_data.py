"""
Pull official flood/emergency point data from GeoServidorPeru (MINAM)
for the El Nino Costero 2026-2027 heatmap project.

Layers used:
  - Layer 3: "Puntos criticos por inundacion" - CENEPRED/SIGRID technical
    assessments. Each point has a linked PDF source and estimated exposed
    families/homes -> use as the validated static risk base layer.
  - Layer 2: "Registro de emergencias (INDECI)" - SINPAD-sourced historical
    events, with real impact counts (deaths, affected homes, etc.)
    -> use as the dynamic layer to weight the heatmap by actual severity.

Both layers are served in EPSG:4326 when requested with f=geojson,
so no manual reprojection is needed.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = (
    "https://geoservidorperu.minam.gob.pe/arcgis/rest/services/"
    "hiroambienplanificacion/caracterizacion_territorio_riesgo_desastres/MapServer"
)

OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

LAYERS = {
    3: "puntos_criticos_inundacion.geojson",   # static, CENEPRED-validated
    2: "registro_emergencias_indeci.geojson",  # dynamic, SINPAD-sourced
}

# The server's WAF blocks the default "python-requests/x.x" User-Agent.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch_layer(layer_id: int, page_size: int = 1000, where: str = "1=1") -> dict:
    """Paginate through a MapServer layer and return a merged GeoJSON FeatureCollection."""
    features = []
    offset = 0

    while True:
        params = {
            "where": where,
            "outFields": "*",
            "f": "geojson",
            "resultRecordCount": page_size,
            "resultOffset": offset,
        }
        resp = requests.get(
            f"{BASE_URL}/{layer_id}/query", params=params, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("features", [])
        features.extend(batch)
        print(f"  layer {layer_id}: pulled {len(batch)} (total {len(features)})")

        if not batch or not data.get("exceededTransferLimit", False):
            break

        offset += page_size
        time.sleep(0.2)  # be polite to the public server

    return {"type": "FeatureCollection", "features": features}


if __name__ == "__main__":
    for layer_id, filename in LAYERS.items():
        print(f"Fetching layer {layer_id}...")
        fc = fetch_layer(layer_id)
        out_path = OUTPUT_DIR / filename
        out_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
        print(f"  saved {len(fc['features'])} features -> {out_path}\n")
