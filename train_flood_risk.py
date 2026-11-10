"""
Riesgo predicho (baseline ML) para los distritos de la zona El Nino Costero.

Entrena un modelo de regresion sobre la tasa historica de eventos por 1,000
habitantes (`event_rate_per_1000`, 2003-2021) a partir de features fisicas por
distrito, y escribe una capa nueva para el visor.

Input
  - data/emergencias_distrito_costa_nino.geojson   (target + centroides)
  - data/puntos_criticos_inundacion.geojson        (CENEPRED, para la feature de cercania)

Features (por centroide del distrito)
  - Precipitacion historica: NASA POWER, PRECTOTCORR diaria 2003-2021
    (resolucion nativa MERRA-2, ~0.5 x 0.625 grados: varios distritos comparten celda).
  - Elevacion: Copernicus DEM GLO-30 (COG en AWS Open Data) -> cota del punto y
    desviacion estandar en ~1 km (relieve).
  - CENEPRED: numero de puntos criticos a 10 y 25 km, distancia al mas cercano y
    familias expuestas a 10 km.

Output
  - data/features_riesgo_costa_nino.csv        features por distrito (cache versionada)
  - data/riesgo_predicho_costa_nino.geojson    misma estructura que la capa de origen
                                               + `riesgo_predicho` (‰, fuera de muestra)
                                               + `riesgo_predicho_percentil` (0-100)
  - models/flood_risk_<modelo>.json            modelo final (entrenado con los 199 distritos)
  - models/flood_risk_metrics.json             metricas, ablaciones y configuracion

Decisiones que importan (ver README, seccion "Modelo de riesgo predicho")
  - Target = regresion sobre log1p(tasa). Un target binario `event_count > 0` seria
    constante: la capa de origen solo contiene distritos con >= 1 evento.
  - Validacion espacial: se deja fuera provincias completas. Un split aleatorio
    pone distritos vecinos (casi identicos) a ambos lados y sobreestima el desempeno.
  - `riesgo_predicho` es la prediccion FUERA DE MUESTRA (promedio de varias
    repeticiones de CV por provincia), no el ajuste del modelo a sus propios datos.
  - Es una linea base historica suavizada. No es un pronostico del evento 2026-2027.

Uso
  python train_flood_risk.py                      # usa data/features_riesgo_costa_nino.csv si existe
  python train_flood_risk.py --refresh-features   # vuelve a consultar NASA POWER y el DEM
  python train_flood_risk.py --model lightgbm
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy.stats import spearmanr
from sklearn.dummy import DummyRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, GroupShuffleSplit, KFold
from sklearn.neighbors import BallTree
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
CACHE_DIR = DATA_DIR / "_cache" / "power"          # ignorado por git

SRC_GEOJSON = DATA_DIR / "emergencias_distrito_costa_nino.geojson"
CRIT_GEOJSON = DATA_DIR / "puntos_criticos_inundacion.geojson"
FEATURES_CSV = DATA_DIR / "features_riesgo_costa_nino.csv"
OUT_GEOJSON = DATA_DIR / "riesgo_predicho_costa_nino.geojson"
METRICS_JSON = MODELS_DIR / "flood_risk_metrics.json"

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_START, POWER_END = "20030101", "20211231"      # misma ventana que el historial de eventos
POWER_YEARS = list(range(2003, 2022))
DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
DEM_HALF_WINDOW_PX = 17                              # ~500 m a cada lado (30 m/px)
CRIT_RADII_KM = (10, 25)
EARTH_RADIUS_KM = 6371.0088

SEED = 42
N_FOLDS = 5
N_REPEATS = 20            # repeticiones de CV por provincia (modelo principal)
N_REPEATS_ABLATION = 10
TEST_SIZE = 0.2

PRECIP_FEATURES = ["precip_anual_mm", "precip_p95_mm", "precip_max_diaria_mm",
                   "dias_lluvia_fuerte_anual", "precip_max_anual_media_mm", "precip_cv_anual"]
ELEV_FEATURES = ["elev_m", "elev_std_1km"]
CRIT_FEATURES = ["n_criticos_10km", "n_criticos_25km", "dist_critico_km", "familias_exp_10km"]
FEATURES = PRECIP_FEATURES + ELEV_FEATURES + CRIT_FEATURES


# --------------------------------------------------------------------------- datos

def load_districts() -> pd.DataFrame:
    fc = json.loads(SRC_GEOJSON.read_text(encoding="utf-8"))
    rows = []
    for f in fc["features"]:
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f["properties"]
        rows.append({**p, "lon": lon, "lat": lat})
    df = pd.DataFrame(rows)
    df["cod_ubigeo"] = df["cod_ubigeo"].astype(str).str.zfill(6)
    df["provincia_id"] = df["cod_ubigeo"].str[:4]     # departamento + provincia
    return df


# --------------------------------------------------------------------------- features: NASA POWER

def fetch_power(cod: str, lat: float, lon: float, refresh: bool) -> dict[str, float]:
    """Serie diaria PRECTOTCORR 2003-2021 en el centroide (con cache en disco) -> agregados."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{cod}.json"
    if cache.exists() and not refresh:
        series = json.loads(cache.read_text(encoding="utf-8"))
    else:
        params = dict(parameters="PRECTOTCORR", community="AG", longitude=round(lon, 5),
                      latitude=round(lat, 5), start=POWER_START, end=POWER_END, format="JSON")
        last_err = None
        for attempt in range(4):
            try:
                r = requests.get(POWER_URL, params=params, timeout=120)
                r.raise_for_status()
                series = r.json()["properties"]["parameter"]["PRECTOTCORR"]
                break
            except Exception as e:                      # red / 429 / 5xx: backoff exponencial
                last_err = e
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"NASA POWER fallo para {cod}: {last_err}")
        cache.write_text(json.dumps(series), encoding="utf-8")
        time.sleep(0.3)                                 # cortesia con el servicio

    s = pd.Series(series, dtype="float64")
    s.index = pd.to_datetime(s.index, format="%Y%m%d")
    s = s.where(s > -990)                               # fill_value = -999
    annual_sum = s.groupby(s.index.year).sum(min_count=300).reindex(POWER_YEARS)
    annual_max = s.groupby(s.index.year).max().reindex(POWER_YEARS)
    heavy_days = (s >= 10).groupby(s.index.year).sum().reindex(POWER_YEARS)
    return {
        "precip_anual_mm": annual_sum.mean(),
        "precip_p95_mm": s.quantile(0.95),
        "precip_max_diaria_mm": s.max(),
        "dias_lluvia_fuerte_anual": heavy_days.mean(),
        "precip_max_anual_media_mm": annual_max.mean(),
        "precip_cv_anual": annual_sum.std() / annual_sum.mean() if annual_sum.mean() else np.nan,
    }


# --------------------------------------------------------------------------- features: elevacion

def _dem_url(lat: float, lon: float) -> str:
    la, lo = math.floor(lat), math.floor(lon)
    name = (f"Copernicus_DSM_COG_10_{'N' if la >= 0 else 'S'}{abs(la):02d}_00_"
            f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}_00_DEM")
    return f"/vsicurl/{DEM_BASE}/{name}/{name}.tif"


def elevation_features(df: pd.DataFrame) -> pd.DataFrame:
    import rasterio
    from rasterio.windows import Window

    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    for var in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):   # proxy con CA propia
        if os.environ.get(var):
            env["CURL_CA_BUNDLE"] = os.environ[var]
            break

    out, open_tiles = [], {}
    n = DEM_HALF_WINDOW_PX
    with rasterio.Env(**env):
        for r in df.itertuples():
            url = _dem_url(r.lat, r.lon)
            if url not in open_tiles:
                open_tiles[url] = rasterio.open(url)
            ds = open_tiles[url]
            row, col = ds.index(r.lon, r.lat)
            win = Window(col - n, row - n, 2 * n + 1, 2 * n + 1)
            arr = ds.read(1, window=win, boundless=True, fill_value=-32768).astype("float64")
            arr[arr < -1000] = np.nan
            out.append({"cod_ubigeo": r.cod_ubigeo, "elev_m": arr[n, n],
                        "elev_std_1km": np.nanstd(arr) if np.isfinite(arr).any() else np.nan})
        for ds in open_tiles.values():
            ds.close()
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- features: CENEPRED

def critical_point_features(df: pd.DataFrame) -> pd.DataFrame:
    fc = json.loads(CRIT_GEOJSON.read_text(encoding="utf-8"))["features"]
    pts, fam = [], []
    for f in fc:
        g = f.get("geometry")
        if g and isinstance(g.get("coordinates"), list) and len(g["coordinates"]) >= 2:
            lon, lat = g["coordinates"][:2]
            if np.isfinite(lon) and np.isfinite(lat):
                pts.append((lat, lon))
                v = f["properties"].get("familias_e")
                fam.append(float(v) if isinstance(v, (int, float)) and np.isfinite(v) else 0.0)
    pts_rad = np.radians(np.array(pts))
    fam = np.array(fam)
    tree = BallTree(pts_rad, metric="haversine")
    q = np.radians(df[["lat", "lon"]].to_numpy())

    res = pd.DataFrame({"cod_ubigeo": df["cod_ubigeo"].to_numpy()})
    dist, _ = tree.query(q, k=1)
    res["dist_critico_km"] = dist[:, 0] * EARTH_RADIUS_KM
    for km in CRIT_RADII_KM:
        idx = tree.query_radius(q, r=km / EARTH_RADIUS_KM)
        res[f"n_criticos_{km}km"] = [len(i) for i in idx]
        if km == 10:
            res["familias_exp_10km"] = [fam[i].sum() for i in idx]
    return res


# --------------------------------------------------------------------------- features: orquestacion

def build_features(df: pd.DataFrame, refresh: bool) -> pd.DataFrame:
    if FEATURES_CSV.exists() and not refresh:
        feats = pd.read_csv(FEATURES_CSV, dtype={"cod_ubigeo": str})
        feats["cod_ubigeo"] = feats["cod_ubigeo"].str.zfill(6)
        if set(df["cod_ubigeo"]) <= set(feats["cod_ubigeo"]) and all(c in feats for c in FEATURES):
            print(f"features: leidas de {FEATURES_CSV.name} ({len(feats)} distritos)")
            return feats

    print("features: consultando NASA POWER (~1 s por distrito)...")
    prec = []
    for i, r in enumerate(df.itertuples(), 1):
        prec.append({"cod_ubigeo": r.cod_ubigeo, **fetch_power(r.cod_ubigeo, r.lat, r.lon, refresh)})
        if i % 25 == 0 or i == len(df):
            print(f"  precipitacion {i}/{len(df)}")
    prec = pd.DataFrame(prec)
    print("features: leyendo elevacion (Copernicus DEM 30 m)...")
    elev = elevation_features(df)
    print("features: puntos criticos CENEPRED cercanos...")
    crit = critical_point_features(df)
    feats = prec.merge(elev, on="cod_ubigeo").merge(crit, on="cod_ubigeo")
    feats.round(4).to_csv(FEATURES_CSV, index=False)
    print(f"  -> {FEATURES_CSV.name}")
    return feats


# --------------------------------------------------------------------------- modelos

def make_model(kind: str):
    if kind == "dummy":
        return DummyRegressor(strategy="mean")
    if kind == "ridge":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=10.0))
    if kind == "xgboost":
        from xgboost import XGBRegressor
        return XGBRegressor(n_estimators=300, learning_rate=0.03, max_depth=3, min_child_weight=5,
                            subsample=0.8, colsample_bytree=0.8, reg_lambda=5.0,
                            tree_method="hist", n_jobs=1, random_state=SEED)
    if kind == "lightgbm":
        from lightgbm import LGBMRegressor
        return LGBMRegressor(n_estimators=300, learning_rate=0.03, num_leaves=8, min_child_samples=10,
                             subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=5.0,
                             n_jobs=1, random_state=SEED, verbose=-1)
    raise ValueError(kind)


def fit_predict(kind, Xtr, ytr, wtr, Xte):
    m = make_model(kind)
    if kind == "ridge":
        m.fit(Xtr, ytr, ridge__sample_weight=wtr)
    else:
        m.fit(Xtr, ytr, sample_weight=wtr)
    return m.predict(Xte)


def metrics(y_log: np.ndarray, pred_log: np.ndarray, rank: bool = True) -> dict[str, float]:
    """`rank=False` para el baseline constante: su Spearman agrupado no esta definido."""
    y, p = np.expm1(y_log), np.expm1(np.clip(pred_log, 0, None))
    rho = spearmanr(y_log, pred_log).statistic if rank and np.std(pred_log) > 0 else float("nan")
    return {"rmse_log": float(np.sqrt(mean_squared_error(y_log, pred_log))),
            "rmse_permil": float(np.sqrt(mean_squared_error(y, p))),
            "mae_permil": float(mean_absolute_error(y, p)),
            "r2_log": float(r2_score(y_log, pred_log)),
            "spearman": float(rho)}


def summarize(runs: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    out = {}
    for k in runs[0]:
        v = np.array([r[k] for r in runs], dtype="float64")
        ok = np.isfinite(v).any()                       # None (no NaN) para que el JSON sea valido
        out[k] = {"mean": float(np.nanmean(v)) if ok else None, "sd": float(np.nanstd(v)) if ok else None}
    return out


def nan_to_none(obj):
    """Recorre el payload y cambia NaN/inf por None: json.dumps escribiria `NaN`, que no es JSON valido."""
    if isinstance(obj, dict):
        return {k: nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [nan_to_none(v) for v in obj]
    return None if isinstance(obj, float) and not math.isfinite(obj) else obj


def grouped_cv(kind, X, y, w, groups, n_repeats, features=None, shuffle_rows=False):
    """CV repetida. Por defecto deja provincias completas fuera (validacion espacial).
    Devuelve metricas por repeticion y la prediccion fuera de muestra promediada."""
    Xs = X if features is None else X[features]
    runs, oof_all = [], []
    for rep in range(n_repeats):
        oof = np.full(len(y), np.nan)
        if shuffle_rows:                                  # split aleatorio (solo para contraste)
            splitter = KFold(N_FOLDS, shuffle=True, random_state=rep).split(Xs)
        else:
            splitter = GroupKFold(N_FOLDS, shuffle=True, random_state=rep).split(Xs, y, groups)
        for tr, te in splitter:
            oof[te] = fit_predict(kind, Xs.iloc[tr], y[tr], w[tr], Xs.iloc[te])
        runs.append(metrics(y, oof, rank=kind != "dummy"))
        oof_all.append(oof)
    return runs, np.mean(oof_all, axis=0)


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["xgboost", "lightgbm"], default="xgboost")
    ap.add_argument("--refresh-features", action="store_true", help="ignora la cache y vuelve a consultar las fuentes")
    ap.add_argument("--features-only", action="store_true", help="solo genera data/features_riesgo_costa_nino.csv")
    args = ap.parse_args()

    df = load_districts()
    print(f"{len(df)} distritos, {df['provincia_id'].nunique()} provincias")
    if (df["event_count"] <= 0).sum() == 0:
        print("nota: todos los distritos tienen >= 1 evento -> `event_count > 0` seria un target constante; "
              "se usa regresion sobre la tasa.")

    feats = build_features(df, args.refresh_features)
    if args.features_only:
        return
    df = df.merge(feats, on="cod_ubigeo", how="left", validate="1:1")

    n_series = int(df["precip_anual_mm"].round(3).nunique())
    print(f"series de precipitacion distintas: {n_series} de {len(df)} distritos "
          f"(NASA POWER/MERRA-2 es de baja resolucion: distritos vecinos comparten celda)")

    X = df[FEATURES].astype("float64")
    y = np.log1p(df["event_rate_per_1000"].to_numpy(dtype="float64"))
    w = np.log10(df["poblacion_2020"].to_numpy(dtype="float64").clip(10))   # tasas de poblaciones chicas son mas ruidosas
    groups = df["provincia_id"].to_numpy()

    # ---- 1) un split train/test explicito, por provincias
    gss = GroupShuffleSplit(n_splits=1, test_size=TEST_SIZE, random_state=SEED)
    tr, te = next(gss.split(X, y, groups))
    split = {"n_train": int(len(tr)), "n_test": int(len(te)),
             "provincias_test": sorted(set(groups[te])), "modelos": {}}
    for kind in ("dummy", "ridge", args.model):
        pred_te = fit_predict(kind, X.iloc[tr], y[tr], w[tr], X.iloc[te])
        pred_tr = fit_predict(kind, X.iloc[tr], y[tr], w[tr], X.iloc[tr])
        split["modelos"][kind] = {"test": metrics(y[te], pred_te, rank=kind != "dummy"),
                                  "train": metrics(y[tr], pred_tr, rank=kind != "dummy")}
    print("\nSplit por provincias (80/20) - metricas en test:")
    for kind, m in split["modelos"].items():
        t = m["test"]
        print(f"  {kind:9} RMSE(log)={t['rmse_log']:.3f}  RMSE={t['rmse_permil']:.2f}‰  Spearman={t['spearman']:.2f}"
              f"  (train Spearman={m['train']['spearman']:.2f})")

    # ---- 2) CV repetida por provincias: dummy / ridge / modelo principal
    cv, oof_main = {}, None
    for kind in ("dummy", "ridge", args.model):
        runs, oof = grouped_cv(kind, X, y, w, groups, N_REPEATS)
        cv[kind] = summarize(runs)
        if kind == args.model:
            oof_main = oof
    runs_rand, _ = grouped_cv(args.model, X, y, w, groups, N_REPEATS, shuffle_rows=True)
    cv[f"{args.model}_split_aleatorio"] = summarize(runs_rand)
    print(f"\nCV repetida x{N_REPEATS} por provincias (mean ± sd):")
    for kind, m in cv.items():
        rho = m['spearman']
        rho_txt = "n/a" if rho['mean'] is None else f"{rho['mean']:.2f}±{rho['sd']:.2f}"
        print(f"  {kind:26} RMSE(log)={m['rmse_log']['mean']:.3f}±{m['rmse_log']['sd']:.3f}  "
              f"RMSE={m['rmse_permil']['mean']:.2f}‰  R2(log)={m['r2_log']['mean']:.2f}  Spearman={rho_txt}")

    # ---- 3) ablacion por grupo de features
    ablation = {}
    for name, cols in {"solo_precipitacion": PRECIP_FEATURES, "solo_elevacion": ELEV_FEATURES,
                       "solo_cenepred": CRIT_FEATURES, "sin_cenepred": PRECIP_FEATURES + ELEV_FEATURES,
                       "todas": FEATURES}.items():
        runs, _ = grouped_cv(args.model, X, y, w, groups, N_REPEATS_ABLATION, features=cols)
        ablation[name] = summarize(runs)
    print(f"\nAblacion ({args.model}, CV por provincias x{N_REPEATS_ABLATION}):")
    for name, m in ablation.items():
        print(f"  {name:20} Spearman={m['spearman']['mean']:.2f}±{m['spearman']['sd']:.2f}  "
              f"RMSE(log)={m['rmse_log']['mean']:.3f}")

    # ---- 4) modelo final (todos los distritos) + importancias
    final = make_model(args.model)
    final.fit(X, y, sample_weight=w)
    MODELS_DIR.mkdir(exist_ok=True)
    model_path = MODELS_DIR / f"flood_risk_{args.model}.json"
    final.save_model(model_path)
    importance = dict(zip(FEATURES, (float(v) for v in final.feature_importances_)))
    importance = dict(sorted(importance.items(), key=lambda kv: -kv[1]))
    print("\nImportancia (modelo final):", {k: round(v, 3) for k, v in importance.items()})

    # ---- 5) salida: prediccion FUERA DE MUESTRA por distrito
    pred_permil = np.expm1(np.clip(oof_main, 0, None))
    pctl = pd.Series(pred_permil).rank(pct=True).to_numpy() * 100
    src = json.loads(SRC_GEOJSON.read_text(encoding="utf-8"))
    by_code = {c: i for i, c in enumerate(df["cod_ubigeo"])}
    for f in src["features"]:
        i = by_code[str(f["properties"]["cod_ubigeo"]).zfill(6)]
        f["properties"]["riesgo_predicho"] = round(float(pred_permil[i]), 3)
        f["properties"]["riesgo_predicho_percentil"] = round(float(pctl[i]), 1)
    OUT_GEOJSON.write_text(json.dumps(src, ensure_ascii=False), encoding="utf-8")
    print(f"\n{OUT_GEOJSON.name}: {len(src['features'])} distritos; riesgo_predicho "
          f"min={pred_permil.min():.2f}‰ max={pred_permil.max():.2f}‰ "
          f"(observado max={df['event_rate_per_1000'].max():.2f}‰)")

    # ---- 6) confusion con la poblacion: la tasa = eventos / poblacion, asi que distritos chicos tienen tasas altas
    obs = df["event_rate_per_1000"].to_numpy(dtype="float64")
    pop = df["poblacion_2020"].to_numpy(dtype="float64")
    big = pop >= 20_000
    rho = lambda a, b: float(spearmanr(a, b).statistic)
    confusion = {"spearman_tasa_vs_poblacion": rho(obs, pop),
                 "spearman_elevacion_vs_poblacion": rho(df["elev_m"], pop),
                 "spearman_predicho_vs_poblacion": rho(pred_permil, pop),
                 "spearman_observado_vs_predicho": rho(obs, pred_permil),
                 "distritos_20k_hab_n": int(big.sum()),
                 "spearman_observado_vs_predicho_20k_hab": rho(obs[big], pred_permil[big])}
    print("\nConfusion con poblacion:", {k: round(v, 2) if isinstance(v, float) else v for k, v in confusion.items()})

    METRICS_JSON.write_text(json.dumps(nan_to_none({
        "generado": date.today().isoformat(),
        "modelo": args.model,
        "n_distritos": int(len(df)), "n_provincias": int(df["provincia_id"].nunique()),
        "target": "log1p(event_rate_per_1000), eventos de lluvia/inundacion 2003-2021",
        "pesos": "log10(poblacion_2020)",
        "features": FEATURES,
        "series_precipitacion_distintas": n_series,
        "validacion": {"split_por_provincias": split,
                       "cv_repetida": {"folds": N_FOLDS, "repeticiones": N_REPEATS, "resultados": cv},
                       "ablacion": {"repeticiones": N_REPEATS_ABLATION, "resultados": ablation}},
        "importancia_modelo_final": importance,
        "prediccion_max_permil": float(pred_permil.max()),
        "confusion_con_poblacion": confusion,
        "semilla": SEED,
    }), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"metricas -> {METRICS_JSON.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
