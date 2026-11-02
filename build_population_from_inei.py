"""Build population_por_distrito.json from the INEI poverty-map dataset (pob_2020).
Fallback for fetch_population.py, since the CENEPRED SIGRID service is gone.
Source: github.com/calderonsamuel/perupobreza2018 (INEI Mapa de Pobreza Monetaria 2018, pob_2020)."""
import json
from pathlib import Path
import pyreadr

DATA = Path(__file__).parent / "data"
df = list(pyreadr.read_r(DATA / "pobreza2018.rda").values())[0]
out = {}
for r in df.itertuples():
    ub = str(r.ubigeo).zfill(6)
    out[ub] = {"departamento": r.region, "provincia": r.provincia,
               "distrito": r.distrito, "pob_2020": int(r.pob_2020)}
(DATA / "population_por_distrito.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(len(out), "districts saved")
