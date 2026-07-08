#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIIRS светлосно загађење — статистика за заштићена подручја Србије.

Намена:
- проширење постојећег GitHub/Earth Engine система `rajac-light-data`;
- чита VIIRS месечне композите из Google Earth Engine-а;
- рачуна статистику за сва заштићена подручја из GeoJSON-а;
- уписује готове JSON фајлове у `public/results/protected-areas/`.

Основни пример:
python protected_areas/protected_areas_processor.py \
  --mode latest \
  --areas-geojson protected_areas/zasticena_podrucja_srbije_gee.geojson \
  --results-dir public/results/protected-areas \
  --project deft-epigram-414409 \
  --service-account rajac-light-monitor@deft-epigram-414409.iam.gserviceaccount.com \
  --key-file /tmp/ee-key.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import ee
except Exception as exc:  # pragma: no cover
    print("ГРЕШКА: није инсталиран earthengine-api. Покрени: pip install earthengine-api", file=sys.stderr)
    raise

DATASET_ID = "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG"
BAND_RAD = "avg_rad"
BAND_CF = "cf_cvg"
DEFAULT_SCALE_METERS = 500
DEFAULT_MIN_CF_CVG = 1
DEFAULT_MIN_MONTHS_YEAR = 3
JSON_FLAGS = dict(ensure_ascii=False, indent=2)

CLASS_DEFS = [
    ("km2_000_025", "pct_000_025", 0.0, 0.25, "very_dark_0_0.25", "врло тамно"),
    ("km2_025_050", "pct_025_050", 0.25, 0.50, "dark_0.25_0.50", "тамно"),
    ("km2_050_100", "pct_050_100", 0.50, 1.00, "low_0.50_1.00", "ниско"),
    ("km2_100_300", "pct_100_300", 1.00, 3.00, "moderate_1.00_3.00", "умерено"),
    ("km2_300_1000", "pct_300_1000", 3.00, 10.00, "elevated_3.00_10.00", "повишено"),
    ("km2_gt_1000", "pct_gt_1000", 10.00, None, "high_gt_10.00", "високо"),
]


def slugify(value: str, fallback: str = "area") -> str:
    value = str(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or fallback


def month_to_date(month: str) -> dt.date:
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise ValueError(f"Месец мора бити YYYY-MM, добијено: {month!r}")
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1)


def add_month(d: dt.date, n: int = 1) -> dt.date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return dt.date(y, m, 1)


def ym(d: dt.date) -> str:
    return d.strftime("%Y-%m")


def month_range(start_ym: str, end_ym_exclusive: str) -> Iterable[dt.date]:
    cur = month_to_date(start_ym)
    end = month_to_date(end_ym_exclusive)
    while cur < end:
        yield cur
        cur = add_month(cur, 1)


def safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None


def safe_round(v: Any, digits: int = 6) -> Optional[float]:
    f = safe_float(v)
    return None if f is None else round(f, digits)


def truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "да", "rajac", "рајац"}


def classify_mean_rad(value: Any) -> Tuple[str, str]:
    f = safe_float(value)
    if f is None:
        return "no_data", "нема података"
    for km2_key, pct_key, lo, hi, class_id, label in CLASS_DEFS:
        if hi is None:
            if f >= lo:
                return class_id, label
        elif f >= lo and f < hi:
            return class_id, label
    return "no_data", "нема података"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj: Any) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, **JSON_FLAGS) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def init_ee(args: argparse.Namespace) -> None:
    """Иницијализација Earth Engine-а за локално или GitHub Actions окружење."""
    key_file = args.key_file
    temp_file: Optional[tempfile.NamedTemporaryFile] = None

    if args.key_json_env:
        key_json = os.environ.get(args.key_json_env)
        if not key_json:
            raise RuntimeError(f"Није пронађена ENV променљива {args.key_json_env!r} са service account JSON-ом.")
        temp_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8")
        temp_file.write(key_json)
        temp_file.close()
        key_file = temp_file.name

    try:
        if args.service_account and key_file:
            credentials = ee.ServiceAccountCredentials(args.service_account, key_file)
            ee.Initialize(credentials, project=args.project)
        else:
            ee.Initialize(project=args.project)
    finally:
        if temp_file is not None:
            try:
                os.unlink(temp_file.name)
            except OSError:
                pass

    try:
        ee.data.setDeadline(args.ee_deadline)
    except Exception:
        pass


def load_areas(args: argparse.Namespace) -> ee.FeatureCollection:
    if args.areas_asset:
        fc = ee.FeatureCollection(args.areas_asset)
    else:
        path = Path(args.areas_geojson)
        if not path.exists():
            raise FileNotFoundError(f"GeoJSON није пронађен: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        features = []
        seen: Dict[str, int] = {}
        for idx, feat in enumerate(data.get("features", []), start=1):
            props = dict(feat.get("properties") or {})
            name_lat = props.get("name_lat") or props.get("name") or f"area-{idx}"
            base_slug = slugify(name_lat, fallback=f"area-{idx}")
            seen[base_slug] = seen.get(base_slug, 0) + 1
            pa_id = base_slug if seen[base_slug] == 1 else f"{base_slug}-{seen[base_slug]}"
            props.setdefault("pa_id", pa_id)
            props.setdefault("pa_order", idx)
            geom = ee.Geometry(feat.get("geometry"), None, False)
            if args.simplify_meters and args.simplify_meters > 0:
                geom = geom.simplify(maxError=args.simplify_meters)
            features.append(ee.Feature(geom, props))
        fc = ee.FeatureCollection(features)

    return fc.map(lambda f: ee.Feature(f.geometry(), f.toDictionary()).set({
        "pa_name": ee.Algorithms.If(f.get("name"), f.get("name"), f.get("pa_name")),
        "pa_name_lat": ee.Algorithms.If(f.get("name_lat"), f.get("name_lat"), f.get("pa_name_lat")),
        "pa_type_clean": ee.Algorithms.If(f.get("pa_type"), f.get("pa_type"), f.get("type")),
    }))


def viirs_collection() -> ee.ImageCollection:
    return ee.ImageCollection(DATASET_ID)


def latest_dataset_month() -> str:
    coll = viirs_collection().sort("system:time_start", False)
    first = ee.Image(coll.first())
    millis = first.get("system:time_start").getInfo()
    if millis is None:
        raise RuntimeError("Не могу да прочитам најновији VIIRS месец из Earth Engine-а.")
    d = dt.datetime.utcfromtimestamp(int(millis) / 1000).date().replace(day=1)
    return ym(d)


def monthly_image(month_start: dt.date, min_cf_cvg: int) -> ee.Image:
    start = month_start.isoformat()
    end = add_month(month_start, 1).isoformat()
    coll = viirs_collection().filterDate(start, end)
    img = ee.Image(coll.first())
    rad = img.select(BAND_RAD).max(ee.Image.constant(0)).rename("avg_rad")
    cf = img.select(BAND_CF).rename("cf_cvg")
    valid = cf.gte(min_cf_cvg)
    rad = rad.updateMask(valid)
    cf = cf.updateMask(valid)
    return rad.addBands(cf).set({"date_ym": ym(month_start), "year": month_start.year, "month": month_start.month})


def annual_image(year: int, min_cf_cvg: int, min_months_year: int) -> ee.Image:
    coll = viirs_collection().filterDate(f"{year}-01-01", f"{year + 1}-01-01")

    def prep(img: ee.Image) -> ee.Image:
        cf = img.select(BAND_CF).rename("cf_cvg")
        valid = cf.gte(min_cf_cvg)
        rad = img.select(BAND_RAD).max(ee.Image.constant(0)).rename("avg_rad").updateMask(valid)
        valid_month = valid.rename("valid_month")
        return rad.addBands(cf.updateMask(valid)).addBands(valid_month)

    prepared = coll.map(prep)
    rad = prepared.select("avg_rad").mean().rename("avg_rad")
    cf = prepared.select("cf_cvg").mean().rename("cf_cvg")
    months_used = prepared.select("valid_month").sum().rename("months_used")
    mask = months_used.gte(min_months_year)
    return rad.updateMask(mask).addBands(cf.updateMask(mask)).addBands(months_used.updateMask(mask)).set({"year": year})


def analysis_feature_collection(
    image: ee.Image,
    areas: ee.FeatureCollection,
    period_props: Dict[str, Any],
    scale: int,
    tile_scale: int,
) -> ee.FeatureCollection:
    rad = image.select("avg_rad")
    cf = image.select("cf_cvg")
    valid_area = ee.Image.pixelArea().divide(1_000_000).updateMask(rad.mask()).rename("valid_km2")
    rad_x_area = rad.multiply(valid_area).rename("rad_x_km2")

    class_bands = []
    for km2_key, _pct_key, lo, hi, _class_id, _label in CLASS_DEFS:
        cond = rad.gte(lo) if hi is None else rad.gte(lo).And(rad.lt(hi))
        class_bands.append(valid_area.updateMask(cond).rename(km2_key))

    area_image = valid_area.addBands(rad_x_area).addBands(ee.Image.cat(class_bands))
    stat_bands = rad.addBands(cf)
    if "months_used" in image.bandNames().getInfo():
        stat_bands = stat_bands.addBands(image.select("months_used"))

    stat_reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.max(), sharedInputs=True)
        .combine(ee.Reducer.stdDev(), sharedInputs=True)
        .combine(ee.Reducer.percentile([90, 95]), sharedInputs=True)
    )
    sum_reducer = ee.Reducer.sum()

    def one_area(feature: ee.Feature) -> ee.Feature:
        geom = feature.geometry()
        stats = stat_bands.reduceRegion(
            reducer=stat_reducer,
            geometry=geom,
            scale=scale,
            maxPixels=1e13,
            tileScale=tile_scale,
        )
        sums = area_image.reduceRegion(
            reducer=sum_reducer,
            geometry=geom,
            scale=scale,
            maxPixels=1e13,
            tileScale=tile_scale,
        )
        area_km2 = geom.area(maxError=1).divide(1_000_000)
        base = feature.toDictionary().combine(ee.Dictionary(period_props), True).combine(ee.Dictionary({"area_km2": area_km2}), True)
        return ee.Feature(None, base.combine(ee.Dictionary(stats), True).combine(ee.Dictionary(sums), True))

    return areas.map(one_area)


def clean_row(props: Dict[str, Any], period_type: str) -> Dict[str, Any]:
    name = props.get("pa_name") or props.get("name") or props.get("NAME") or ""
    name_lat = props.get("pa_name_lat") or props.get("name_lat") or props.get("name_en") or ""
    pa_type = props.get("pa_type_clean") or props.get("pa_type") or props.get("type") or ""
    pa_id = props.get("pa_id") or slugify(name_lat or name)

    valid_km2 = safe_float(props.get("valid_km2"))
    area_km2 = safe_float(props.get("area_km2"))
    rad_x_km2 = safe_float(props.get("rad_x_km2"))
    mean_rad = safe_float(props.get("avg_rad_mean"))

    row: Dict[str, Any] = {
        "pa_id": str(pa_id),
        "pa_name": name,
        "pa_name_lat": name_lat,
        "pa_type_clean": pa_type,
        "rajac": truthy(props.get("rajac")) or ("рајац" in str(name).lower()) or ("rajac" in str(name_lat).lower()),
        "period_type": period_type,
        "date_ym": props.get("date_ym"),
        "year": int(props.get("year")) if props.get("year") is not None else None,
        "month": int(props.get("month")) if props.get("month") is not None else None,
        "area_km2": safe_round(area_km2, 3),
        "valid_km2": safe_round(valid_km2, 3),
        "valid_pct_of_area": safe_round((valid_km2 / area_km2 * 100.0) if valid_km2 and area_km2 else None, 3),
        "avg_rad_mean": safe_round(mean_rad, 6),
        "avg_rad_median": safe_round(props.get("avg_rad_median"), 6),
        "avg_rad_max": safe_round(props.get("avg_rad_max"), 6),
        "avg_rad_p90": safe_round(props.get("avg_rad_p90"), 6),
        "avg_rad_p95": safe_round(props.get("avg_rad_p95"), 6),
        "avg_rad_stdDev": safe_round(props.get("avg_rad_stdDev"), 6),
        "cf_cvg_mean": safe_round(props.get("cf_cvg_mean"), 3),
        "rad_x_km2": safe_round(rad_x_km2, 6),
        "rad_area_index": safe_round((rad_x_km2 / area_km2) if rad_x_km2 is not None and area_km2 else None, 6),
        "mean_rad_area_weighted": safe_round((rad_x_km2 / valid_km2) if rad_x_km2 is not None and valid_km2 else None, 6),
    }

    months_used = props.get("months_used_mean") or props.get("months_used_median") or props.get("months_used_max")
    if period_type == "annual":
        row["months_used"] = safe_round(months_used, 2)
    else:
        row["months_used"] = 1

    if not row["date_ym"] and row["year"] and row["month"]:
        row["date_ym"] = f"{row['year']:04d}-{row['month']:02d}"

    for km2_key, pct_key, _lo, _hi, _class_id, _label in CLASS_DEFS:
        km2 = safe_float(props.get(km2_key))
        row[km2_key] = safe_round(km2, 3)
        row[pct_key] = safe_round((km2 / valid_km2 * 100.0) if km2 is not None and valid_km2 else None, 3)

    class_id, class_label = classify_mean_rad(row.get("avg_rad_mean"))
    row["light_pollution_class_mean"] = class_id
    row["light_pollution_class_label"] = class_label
    return row


def fc_to_rows(fc: ee.FeatureCollection, period_type: str) -> List[Dict[str, Any]]:
    info = fc.getInfo()
    rows = []
    for feat in info.get("features", []):
        rows.append(clean_row(feat.get("properties") or {}, period_type=period_type))
    rows.sort(key=lambda r: (not r.get("rajac"), str(r.get("pa_type_clean") or ""), str(r.get("pa_name_lat") or r.get("pa_name") or "")))
    return rows


def find_rajac(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for row in rows:
        if row.get("rajac"):
            return row
    for row in rows:
        if "rajac" in str(row.get("pa_name_lat", "")).lower() or "рајац" in str(row.get("pa_name", "")).lower():
            return row
    return None


def collection_wrapper(rows: List[Dict[str, Any]], period: str, generated_at: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    rajac = find_rajac(rows)
    return {
        "status": "ok",
        "generated_at": generated_at,
        "source_dataset": DATASET_ID,
        "period": period,
        "total_areas": len(rows),
        "rajac": rajac,
        "rows": rows,
        "meta": meta,
    }


def process_month(args: argparse.Namespace, areas: ee.FeatureCollection, month_start: dt.date, generated_at: str) -> Optional[Path]:
    out_path = Path(args.results_dir) / "months" / f"{ym(month_start)}.json"
    if out_path.exists() and not args.overwrite:
        print(f"SKIP месец {ym(month_start)} већ постоји: {out_path}")
        return out_path
    print(f"GEE месечна статистика: {ym(month_start)}")
    img = monthly_image(month_start, min_cf_cvg=args.min_cf_cvg)
    fc = analysis_feature_collection(
        img,
        areas,
        {
            "period_type": "monthly",
            "date_ym": ym(month_start),
            "year": month_start.year,
            "month": month_start.month,
        },
        scale=args.scale,
        tile_scale=args.tile_scale,
    )
    rows = fc_to_rows(fc, period_type="monthly")
    obj = collection_wrapper(rows, period=ym(month_start), generated_at=generated_at, meta=meta_from_args(args))
    write_json(out_path, obj)
    return out_path


def process_year(args: argparse.Namespace, areas: ee.FeatureCollection, year: int, generated_at: str, force: bool = False) -> Optional[Path]:
    out_path = Path(args.results_dir) / "years" / f"{year}.json"
    if out_path.exists() and not args.overwrite and not force:
        print(f"SKIP година {year} већ постоји: {out_path}")
        return out_path
    print(f"GEE годишња статистика: {year}")
    img = annual_image(year, min_cf_cvg=args.min_cf_cvg, min_months_year=args.min_months_year)
    fc = analysis_feature_collection(
        img,
        areas,
        {
            "period_type": "annual",
            "date_ym": None,
            "year": year,
            "month": 0,
        },
        scale=args.scale,
        tile_scale=args.tile_scale,
    )
    rows = fc_to_rows(fc, period_type="annual")
    obj = collection_wrapper(rows, period=str(year), generated_at=generated_at, meta=meta_from_args(args))
    write_json(out_path, obj)
    return out_path


def meta_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "scale_meters": args.scale,
        "min_cf_cvg": args.min_cf_cvg,
        "min_months_year": args.min_months_year,
        "classes": [
            {"km2_key": k, "pct_key": p, "from": lo, "to": hi, "class_id": cid, "label": label}
            for k, p, lo, hi, cid, label in CLASS_DEFS
        ],
    }


def build_indexes(results_dir: Path, generated_at: str) -> None:
    months_dir = results_dir / "months"
    years_dir = results_dir / "years"
    month_files = sorted(months_dir.glob("????-??.json"))
    year_files = sorted(years_dir.glob("????.json"))

    months = [p.stem for p in month_files]
    years = [p.stem for p in year_files]
    latest_month = months[-1] if months else None
    latest_year = years[-1] if years else None

    latest_obj = None
    if latest_month:
        latest_obj = read_json(months_dir / f"{latest_month}.json")
        latest_obj["latest_month"] = latest_month
        latest_obj["archive_file"] = f"months/{latest_month}.json"
        write_json(results_dir / "latest.json", latest_obj)
        rajac = latest_obj.get("rajac") or {}
        write_json(results_dir / "rajac.json", rajac)
    else:
        write_json(results_dir / "latest.json", {
            "status": "pending",
            "generated_at": generated_at,
            "message": "Још нема обрађених VIIRS месеци за заштићена подручја.",
            "rows": [],
        })
        write_json(results_dir / "rajac.json", {})

    index = {
        "status": "ok" if latest_month else "pending",
        "generated_at": generated_at,
        "source_dataset": DATASET_ID,
        "latest_month": latest_month,
        "latest_year": latest_year,
        "months": months,
        "years": years,
        "latest_file": "latest.json",
        "rajac_file": "rajac.json",
    }
    write_json(results_dir / "index.json", index)

    build_area_timeseries(results_dir, month_files, year_files, generated_at)


def build_area_timeseries(results_dir: Path, month_files: List[Path], year_files: List[Path], generated_at: str) -> None:
    area_map: Dict[str, Dict[str, Any]] = {}

    def add_rows(path: Path, key: str) -> None:
        try:
            obj = read_json(path)
        except Exception:
            return
        for row in obj.get("rows", []):
            pa_id = str(row.get("pa_id") or slugify(row.get("pa_name_lat") or row.get("pa_name") or "area"))
            item = area_map.setdefault(pa_id, {
                "status": "ok",
                "generated_at": generated_at,
                "source_dataset": DATASET_ID,
                "pa_id": pa_id,
                "pa_name": row.get("pa_name"),
                "pa_name_lat": row.get("pa_name_lat"),
                "pa_type_clean": row.get("pa_type_clean"),
                "rajac": row.get("rajac"),
                "monthly": [],
                "annual": [],
            })
            item[key].append(row)

    for p in month_files:
        add_rows(p, "monthly")
    for p in year_files:
        add_rows(p, "annual")

    area_dir = results_dir / "areas"
    ensure_dir(area_dir)
    for pa_id, obj in area_map.items():
        obj["monthly"].sort(key=lambda r: str(r.get("date_ym") or ""))
        obj["annual"].sort(key=lambda r: int(r.get("year") or 0))
        write_json(area_dir / f"{pa_id}.json", obj)


def resolve_months(args: argparse.Namespace) -> Tuple[str, str, str]:
    latest = latest_dataset_month()
    if args.mode == "latest":
        start = latest
        end = ym(add_month(month_to_date(latest), 1))
    elif args.mode in {"all", "range"}:
        start = args.start
        if args.end == "auto":
            end = ym(add_month(month_to_date(latest), 1))
        else:
            # end је инклузиван у аргументу, а у филтеру га претварамо у ексклузиван месец после тога.
            end = ym(add_month(month_to_date(args.end), 1))
    else:
        raise ValueError(f"Непознат mode: {args.mode}")
    return start, end, latest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VIIRS статистика светлосног загађења за заштићена подручја Србије")
    p.add_argument("--mode", choices=["latest", "all", "range"], default="latest", help="latest=само најновији VIIRS месец; all=од start до најновијег; range=од start до end")
    p.add_argument("--start", default="2014-01", help="Почетни месец YYYY-MM за all/range")
    p.add_argument("--end", default="auto", help="Крајњи месец YYYY-MM за range/all; auto=најновији доступан у GEE")
    p.add_argument("--yearly", action="store_true", help="Рачуна и годишње JSON фајлове")
    p.add_argument("--overwrite", action="store_true", help="Поново уписује постојеће месечне/годишње фајлове")
    p.add_argument("--areas-geojson", default="protected_areas/zasticena_podrucja_srbije_gee.geojson")
    p.add_argument("--areas-asset", default="", help="Опционо: Earth Engine table asset ID. Ако је задато, има предност над GeoJSON-ом")
    p.add_argument("--results-dir", default="public/results/protected-areas")
    p.add_argument("--scale", type=int, default=DEFAULT_SCALE_METERS)
    p.add_argument("--tile-scale", type=int, default=4)
    p.add_argument("--min-cf-cvg", type=int, default=DEFAULT_MIN_CF_CVG, help="Минималан број cloud-free посматрања у месечном пикселу")
    p.add_argument("--min-months-year", type=int, default=DEFAULT_MIN_MONTHS_YEAR, help="Минималан број валидних месеци за годишњу статистику")
    p.add_argument("--simplify-meters", type=float, default=0, help="Опционо поједностављење граница у метрима, нпр. 30 или 50")
    p.add_argument("--project", default=os.environ.get("EE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    p.add_argument("--service-account", default=os.environ.get("EE_SERVICE_ACCOUNT") or None)
    p.add_argument("--key-file", default=os.environ.get("EE_KEY_FILE") or None)
    p.add_argument("--key-json-env", default=os.environ.get("EE_KEY_JSON_ENV") or "", help="Назив ENV променљиве која садржи service account JSON")
    p.add_argument("--ee-deadline", type=int, default=300)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    generated_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    results_dir = Path(args.results_dir)
    ensure_dir(results_dir / "months")
    ensure_dir(results_dir / "years")
    ensure_dir(results_dir / "areas")

    init_ee(args)
    areas = load_areas(args)
    start_ym, end_ym_exclusive, latest_ym = resolve_months(args)

    print(f"VIIRS dataset: {DATASET_ID}")
    print(f"Најновији доступан месец у GEE: {latest_ym}")
    print(f"Обрада месеци: {start_ym} → {ym(add_month(month_to_date(end_ym_exclusive), -1))}")

    processed_months: List[dt.date] = []
    for m in month_range(start_ym, end_ym_exclusive):
        path = process_month(args, areas, m, generated_at)
        if path:
            processed_months.append(m)

    if args.yearly:
        years: List[int]
        if args.mode == "latest" and processed_months:
            years = [processed_months[-1].year]
            force = True
        else:
            years = sorted({m.year for m in processed_months})
            force = False
        for y in years:
            process_year(args, areas, y, generated_at, force=force)

    build_indexes(results_dir, generated_at)
    print(f"OK: резултати су у {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
