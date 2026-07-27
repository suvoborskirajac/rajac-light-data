#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NASA Black Marble VNP46A2 monthly processor for PIO Rajac and protected areas.

The script keeps the existing public JSON contracts used by:
  * public/results/<YYYY-MM>.json and public/results/index.json
  * public/results/protected-areas/{months,years,index,latest,rajac}.json
  * public/results/protected-areas/pixels/{months,years}/.../*.json

Data source:
  NASA/VIIRS/002/VNP46A2 (daily, Collection 2, 500 m)

Monthly composites use quality-filtered, persistent nighttime lights only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import ee
except Exception as exc:  # pragma: no cover
    print("ERROR: earthengine-api is not installed.", file=sys.stderr)
    raise

DATASET_ID = "NASA/VIIRS/002/VNP46A2"
BAND_RAD = "DNB_BRDF_Corrected_NTL"
BAND_MQF = "Mandatory_Quality_Flag"
BAND_CLOUD = "QF_Cloud_Mask"
BAND_SNOW = "Snow_Flag"
OUTPUT_RAD = "avg_rad"
OUTPUT_COVERAGE = "cf_cvg"  # backwards-compatible name; now valid daily observation count
OUTPUT_MONTHS = "months_used"
SOURCE_LABEL = "NASA Black Marble VNP46A2 Collection 2"
SOURCE_LONG = (
    "NASA Black Marble VNP46A2 Collection 2 — monthly mean of quality-filtered "
    "daily persistent nighttime-light observations"
)
UNIT = "nW/cm²/sr"
JSON_FLAGS = dict(ensure_ascii=False, indent=2)

# Existing protected-area statistical classes.
CLASS_DEFS = [
    ("km2_000_025", "pct_000_025", 0.0, 0.25, "very_dark_0_0.25", "врло тамно"),
    ("km2_025_050", "pct_025_050", 0.25, 0.50, "dark_0.25_0.50", "тамно"),
    ("km2_050_100", "pct_050_100", 0.50, 1.00, "low_0.50_1.00", "ниско"),
    ("km2_100_300", "pct_100_300", 1.00, 3.00, "moderate_1.00_3.00", "умерено"),
    ("km2_300_1000", "pct_300_1000", 3.00, 10.00, "elevated_3.00_10.00", "повишено"),
    ("km2_gt_1000", "pct_gt_1000", 10.00, None, "high_gt_10.00", "високо"),
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, fallback: str = "area") -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or fallback


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "da", "да", "rajac", "рајац"}


def safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def rounded(value: Any, digits: int = 6) -> Optional[float]:
    number = safe_float(value)
    return None if number is None else round(number, digits)


def month_to_date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise ValueError(f"Month must be YYYY-MM, got {value!r}")
    year, month = map(int, value.split("-"))
    return dt.date(year, month, 1)


def add_month(value: dt.date, count: int = 1) -> dt.date:
    absolute = value.year * 12 + value.month - 1 + count
    return dt.date(absolute // 12, absolute % 12 + 1, 1)


def month_id(value: dt.date) -> str:
    return value.strftime("%Y-%m")


def month_range(start: str, end_inclusive: str) -> Iterable[str]:
    current = month_to_date(start)
    end = month_to_date(end_inclusive)
    while current <= end:
        yield month_id(current)
        current = add_month(current)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, **JSON_FLAGS) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def init_ee(args: argparse.Namespace) -> None:
    key_file = args.key_file
    temporary: Optional[tempfile.NamedTemporaryFile] = None
    if args.key_json_env:
        raw = os.environ.get(args.key_json_env)
        if not raw:
            raise RuntimeError(f"Missing environment variable {args.key_json_env!r}.")
        temporary = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8")
        temporary.write(raw)
        temporary.close()
        key_file = temporary.name
    try:
        if args.service_account and key_file:
            credentials = ee.ServiceAccountCredentials(args.service_account, key_file)
            ee.Initialize(credentials, project=args.project)
        else:
            ee.Initialize(project=args.project)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary.name)
            except OSError:
                pass
    try:
        ee.data.setDeadline(args.ee_deadline)
    except Exception:
        pass


def collection() -> ee.ImageCollection:
    return ee.ImageCollection(DATASET_ID)


def latest_source_date() -> dt.date:
    image = ee.Image(collection().sort("system:time_start", False).first())
    milliseconds = image.get("system:time_start").getInfo()
    if milliseconds is None:
        raise RuntimeError("Cannot read the latest VNP46A2 acquisition date.")
    return dt.datetime.fromtimestamp(int(milliseconds) / 1000, tz=dt.timezone.utc).date()


def latest_complete_month() -> str:
    # Never publish the currently incomplete calendar month.
    latest = latest_source_date().replace(day=1)
    return month_id(add_month(latest, -1))


def quality_daily(image: ee.Image, args: argparse.Namespace) -> ee.Image:
    image = ee.Image(image)
    qf = image.select(BAND_CLOUD)
    mandatory = image.select(BAND_MQF)

    night = qf.bitwiseAnd(1).eq(0)
    cloud_mask_quality = qf.rightShift(4).bitwiseAnd(3).gte(args.min_cloud_mask_quality)
    cloud_detection = qf.rightShift(6).bitwiseAnd(3).lte(args.max_cloud_detection)
    no_shadow = qf.rightShift(8).bitwiseAnd(1).eq(0)
    no_cirrus = qf.rightShift(9).bitwiseAnd(1).eq(0)
    no_snow_qf = qf.rightShift(10).bitwiseAnd(1).eq(0)
    no_snow_band = image.select(BAND_SNOW).eq(0)
    quality = mandatory.lte(1) if args.include_ephemeral else mandatory.eq(0)

    valid = (
        night
        .And(cloud_mask_quality)
        .And(cloud_detection)
        .And(no_shadow)
        .And(no_cirrus)
        .And(no_snow_qf)
        .And(no_snow_band)
        .And(quality)
    )
    radiance = image.select(BAND_RAD).max(ee.Image.constant(0)).rename(OUTPUT_RAD).updateMask(valid)
    valid_observation = ee.Image.constant(1).rename("valid_observation").updateMask(valid)
    return radiance.addBands(valid_observation).copyProperties(image, ["system:time_start"])


def monthly_image(period: str, args: argparse.Namespace) -> ee.Image:
    start = month_to_date(period)
    end = add_month(start)
    source = collection().filterDate(start.isoformat(), end.isoformat())
    source_count = int(source.size().getInfo())
    if source_count < args.min_source_days:
        raise RuntimeError(
            f"{period}: only {source_count} source days are present; minimum is {args.min_source_days}."
        )
    prepared = source.map(lambda image: quality_daily(ee.Image(image), args))
    radiance = prepared.select(OUTPUT_RAD).mean().rename(OUTPUT_RAD)
    coverage = prepared.select("valid_observation").count().rename(OUTPUT_COVERAGE)
    valid = coverage.gte(args.min_observations)
    return (
        radiance.updateMask(valid)
        .addBands(coverage.updateMask(valid))
        .set({
            "period": period,
            "period_kind": "month",
            "source_days": source_count,
            "dataset": DATASET_ID,
        })
    )


def year_months(year: int, latest_complete: str) -> List[str]:
    limit = month_to_date(latest_complete)
    return [
        f"{year}-{month:02d}"
        for month in range(1, 13)
        if dt.date(year, month, 1) <= limit
    ]


def annual_image(year: int, latest_complete: str, args: argparse.Namespace) -> Tuple[ee.Image, List[str]]:
    periods = year_months(year, latest_complete)
    if len(periods) < args.min_months_year:
        raise RuntimeError(
            f"{year}: only {len(periods)} complete calendar months are available; "
            f"minimum is {args.min_months_year}."
        )
    monthly = [monthly_image(period, args).select([OUTPUT_RAD, OUTPUT_COVERAGE]) for period in periods]
    collection_monthly = ee.ImageCollection.fromImages(monthly)
    radiance = collection_monthly.select(OUTPUT_RAD).mean().rename(OUTPUT_RAD)
    coverage = collection_monthly.select(OUTPUT_COVERAGE).mean().rename(OUTPUT_COVERAGE)
    months_used = collection_monthly.select(OUTPUT_RAD).count().rename(OUTPUT_MONTHS)
    valid = months_used.gte(args.min_months_year)
    image = (
        radiance.updateMask(valid)
        .addBands(coverage.updateMask(valid))
        .addBands(months_used.updateMask(valid))
        .set({"period": str(year), "period_kind": "year", "dataset": DATASET_ID})
    )
    return image, periods


def load_area_records(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for position, feature in enumerate(payload.get("features", []), start=1):
        properties = dict(feature.get("properties") or {})
        fallback = f"area-{position}"
        base = str(properties.get("pa_id") or "").strip() or slugify(
            properties.get("name_lat") or properties.get("name") or fallback,
            fallback,
        )
        seen[base] = seen.get(base, 0) + 1
        pa_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        properties["pa_id"] = pa_id
        properties.setdefault("pa_order", position)
        properties["rajac"] = truthy(properties.get("rajac")) or pa_id == "rajac"
        records.append({
            "type": "Feature",
            "properties": properties,
            "geometry": feature.get("geometry"),
        })
    if not records:
        raise RuntimeError(f"No features found in {path}.")
    return records


def to_feature_collection(records: Sequence[Dict[str, Any]], simplify_meters: float) -> ee.FeatureCollection:
    features = []
    for record in records:
        geometry = ee.Geometry(record["geometry"], None, False)
        if simplify_meters > 0:
            geometry = geometry.simplify(maxError=simplify_meters)
        properties = dict(record["properties"])
        properties.setdefault("pa_name", properties.get("name") or properties.get("pa_name") or "")
        properties.setdefault("pa_name_lat", properties.get("name_lat") or properties.get("pa_name_lat") or "")
        properties.setdefault("pa_type_clean", properties.get("pa_type") or properties.get("type") or "")
        features.append(ee.Feature(geometry, properties))
    return ee.FeatureCollection(features)


def classify_mean(value: Any) -> Tuple[str, str]:
    number = safe_float(value)
    if number is None:
        return "no_data", "нема података"
    for _km2, _pct, lower, upper, class_id, label in CLASS_DEFS:
        if upper is None and number >= lower:
            return class_id, label
        if upper is not None and lower <= number < upper:
            return class_id, label
    return "no_data", "нема података"


def compute_rows(
    image: ee.Image,
    areas: ee.FeatureCollection,
    period_kind: str,
    period: str,
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    radiance = image.select(OUTPUT_RAD)
    coverage = image.select(OUTPUT_COVERAGE)
    valid_area = ee.Image.pixelArea().divide(1_000_000).updateMask(radiance.mask()).rename("valid_km2")
    rad_x_area = radiance.multiply(valid_area).rename("rad_x_km2")
    class_bands = []
    for km2_key, _pct_key, lower, upper, _class_id, _label in CLASS_DEFS:
        condition = radiance.gte(lower) if upper is None else radiance.gte(lower).And(radiance.lt(upper))
        class_bands.append(valid_area.updateMask(condition).rename(km2_key))
    area_image = valid_area.addBands(rad_x_area).addBands(ee.Image.cat(class_bands))
    stat_image = radiance.addBands(coverage)
    has_months = period_kind == "annual"
    if has_months:
        stat_image = stat_image.addBands(image.select(OUTPUT_MONTHS))
    reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.max(), sharedInputs=True)
        .combine(ee.Reducer.stdDev(), sharedInputs=True)
        .combine(ee.Reducer.percentile([90, 95]), sharedInputs=True)
    )

    def one_area(feature: ee.Feature) -> ee.Feature:
        feature = ee.Feature(feature)
        geometry = feature.geometry()
        stats = stat_image.reduceRegion(
            reducer=reducer,
            geometry=geometry,
            scale=args.scale,
            maxPixels=1e13,
            tileScale=args.tile_scale,
        )
        sums = area_image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geometry,
            scale=args.scale,
            maxPixels=1e13,
            tileScale=args.tile_scale,
        )
        area_km2 = geometry.area(maxError=1).divide(1_000_000)
        return feature.set(stats).set(sums).set({"area_km2": area_km2})

    info = areas.map(one_area).getInfo()
    rows: List[Dict[str, Any]] = []
    for feature in info.get("features", []):
        props = dict(feature.get("properties") or {})
        area_km2 = safe_float(props.get("area_km2")) or 0.0
        valid_km2 = safe_float(props.get("valid_km2")) or 0.0
        rad_x_km2 = safe_float(props.get("rad_x_km2")) or 0.0
        mean = props.get(f"{OUTPUT_RAD}_mean")
        class_id, class_label = classify_mean(mean)
        row: Dict[str, Any] = {
            "pa_id": str(props.get("pa_id") or ""),
            "pa_name": props.get("pa_name") or props.get("name") or "",
            "pa_name_lat": props.get("pa_name_lat") or props.get("name_lat") or "",
            "pa_type_clean": props.get("pa_type_clean") or props.get("pa_type") or props.get("type") or "",
            "rajac": truthy(props.get("rajac")) or str(props.get("pa_id")) == "rajac",
            "period_type": period_kind,
            "date_ym": period if period_kind == "monthly" else None,
            "year": int(period[:4]),
            "month": int(period[5:7]) if period_kind == "monthly" else 0,
            "area_km2": round(area_km2, 3),
            "valid_km2": round(valid_km2, 3),
            "valid_pct_of_area": round((valid_km2 / area_km2 * 100) if area_km2 else 0, 3),
            "avg_rad_mean": rounded(mean),
            "avg_rad_median": rounded(props.get(f"{OUTPUT_RAD}_median")),
            "avg_rad_max": rounded(props.get(f"{OUTPUT_RAD}_max")),
            "avg_rad_p90": rounded(props.get(f"{OUTPUT_RAD}_p90")),
            "avg_rad_p95": rounded(props.get(f"{OUTPUT_RAD}_p95")),
            "avg_rad_stdDev": rounded(props.get(f"{OUTPUT_RAD}_stdDev")),
            "cf_cvg_mean": rounded(props.get(f"{OUTPUT_COVERAGE}_mean"), 3),
            "rad_x_km2": round(rad_x_km2, 6),
            "rad_area_index": round((rad_x_km2 / area_km2) if area_km2 else 0, 6),
            "mean_rad_area_weighted": round((rad_x_km2 / valid_km2) if valid_km2 else 0, 6),
            "months_used": rounded(props.get(f"{OUTPUT_MONTHS}_mean"), 2) if has_months else 1,
            "light_pollution_class_mean": class_id,
            "light_pollution_class_label": class_label,
        }
        for km2_key, pct_key, _lower, _upper, _class_id, _label in CLASS_DEFS:
            km2_value = safe_float(props.get(km2_key)) or 0.0
            row[km2_key] = round(km2_value, 3)
            row[pct_key] = round((km2_value / valid_km2 * 100) if valid_km2 else 0, 3)
        rows.append(row)
    rows.sort(key=lambda row: (0 if row["rajac"] else 1, row["pa_name_lat"] or row["pa_name"]))
    return rows


def protected_payload(period_kind: str, period: str, rows: List[Dict[str, Any]], months: List[str]) -> Dict[str, Any]:
    rajac = next((row for row in rows if row.get("rajac")), None)
    return {
        "status": "ok",
        "generated_at": utc_now(),
        "source_dataset": DATASET_ID,
        "period": period,
        "total_areas": len(rows),
        "rajac": rajac,
        "rows": rows,
        "meta": {
            "source": SOURCE_LONG,
            "dataset": DATASET_ID,
            "band": BAND_RAD,
            "unit": UNIT,
            "period_type": period_kind,
            "months_in_composite": months,
            "quality_filter": {
                "persistent_only": True,
                "night_only": True,
                "cloud_mask_quality": "medium_or_high",
                "cloud_detection": "confident_or_probably_clear",
                "shadow": "excluded",
                "cirrus": "excluded",
                "snow_ice": "excluded",
            },
            "comparability_note": (
                "VNP46A2 is not numerically identical to the former NOAA/EOG monthly VCMSLCFG product. "
                "Periods regenerated by this processor explicitly carry the new dataset identifier."
            ),
        },
    }


def pixel_class(value: float) -> str:
    if value <= 0.15:
        return "врло ниско"
    if value <= 0.35:
        return "ниско"
    if value <= 0.75:
        return "умерено"
    if value <= 1.50:
        return "повишено"
    return "високо"


def pixel_bbox(lon: float, lat: float, scale_m: float) -> List[float]:
    half_lat = (scale_m / 2) / 111_320.0
    half_lon = (scale_m / 2) / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return [
        round(lon - half_lon, 6),
        round(lat - half_lat, 6),
        round(lon + half_lon, 6),
        round(lat + half_lat, 6),
    ]


def percentile(values: Sequence[float], percentage: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def pixel_stats(pixels: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    values = [float(pixel["value"]) for pixel in pixels if safe_float(pixel.get("value")) is not None]
    if not values:
        return {
            "overall": {"min": None, "max": None, "mean": None, "median": None, "p90": None, "count": 0, "sum": 0},
            "hotspots": [],
        }
    hotspots = sorted(pixels, key=lambda pixel: float(pixel.get("value") or 0), reverse=True)[:5]
    return {
        "overall": {
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(sum(values) / len(values), 4),
            "median": round(statistics.median(values), 4),
            "p90": round(percentile(values, 90) or 0, 4),
            "count": len(values),
            "sum": round(sum(values), 4),
        },
        "hotspots": [
            {
                "rank": rank,
                "lon": item["lon"],
                "lat": item["lat"],
                "value": item["value"],
                "class": item["class"],
            }
            for rank, item in enumerate(hotspots, start=1)
        ],
    }


def sample_pixels(image: ee.Image, geometry: ee.Geometry, args: argparse.Namespace) -> List[Dict[str, Any]]:
    collection_pixels = image.select(OUTPUT_RAD).sample(
        region=geometry,
        scale=args.scale,
        geometries=True,
        tileScale=args.tile_scale,
    )
    if args.max_pixels_per_area > 0:
        collection_pixels = collection_pixels.limit(args.max_pixels_per_area)
    info = collection_pixels.getInfo()
    pixels: List[Dict[str, Any]] = []
    for feature in info.get("features", []):
        props = feature.get("properties") or {}
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        value = safe_float(props.get(OUTPUT_RAD))
        if value is None or len(coordinates) < 2:
            continue
        lon, lat = round(float(coordinates[0]), 6), round(float(coordinates[1]), 6)
        value = round(value, args.value_digits)
        pixels.append({
            "lon": lon,
            "lat": lat,
            "bbox": pixel_bbox(lon, lat, args.scale),
            "value": value,
            "class": pixel_class(value),
        })
    pixels.sort(key=lambda pixel: (-pixel["lat"], pixel["lon"]))
    for index, pixel in enumerate(pixels, start=1):
        pixel["id"] = f"px-{index:04d}"
    return pixels


def area_display(record: Dict[str, Any]) -> Dict[str, str]:
    props = record["properties"]
    return {
        "slug": props["pa_id"],
        "pa_id": props["pa_id"],
        "name": props.get("name") or props.get("pa_name") or props["pa_id"],
        "name_lat": props.get("name_lat") or props.get("pa_name_lat") or "",
        "type": props.get("pa_type") or props.get("type") or props.get("pa_type_clean") or "",
        "group": "Заштићена подручја Србије",
    }


def pixel_payload(
    record: Dict[str, Any],
    period_kind: str,
    period: str,
    pixels: List[Dict[str, Any]],
    months: List[str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    label = f"{period[5:7]}/{period[:4]}" if period_kind == "monthly" else period
    return {
        "ok": True,
        "area": area_display(record),
        "meta": {
            "id": period,
            "label": label,
            "kind": "monthly" if period_kind == "monthly" else "annual",
            "source": SOURCE_LONG,
            "dataset": DATASET_ID,
            "band": BAND_RAD,
            "unit": UNIT,
            "scale_m": args.scale,
            "created_at": utc_now(),
            "placeholder": False,
            "months_in_composite": months,
            "interpretation": (
                "The public heatmap is a smoothed visualization from VNP46A2 pixel centres; "
                "raw sampled pixels remain in JSON for statistics."
            ),
        },
        "stats": pixel_stats(pixels),
        "pixels": pixels,
    }


def root_payload(period_kind: str, period: str, pixels: List[Dict[str, Any]], months: List[str], args: argparse.Namespace) -> Dict[str, Any]:
    start = month_to_date(period) if period_kind == "monthly" else dt.date(int(period), 1, 1)
    end = add_month(start) if period_kind == "monthly" else dt.date(int(period) + 1, 1, 1)
    label = f"{period[5:7]}/{period[:4]}" if period_kind == "monthly" else f"{period}. (годишњи просек)"
    return {
        "ok": True,
        "area": {
            "slug": "pio-rajac",
            "name": "ПИО Рајац",
            "group": "Заштићена подручја",
            "public_url": "https://piorajac.rs/monitoring-svetlosno-zagadjenje/",
        },
        "meta": {
            "id": period,
            "label": label,
            "kind": "monthly" if period_kind == "monthly" else "yearly",
            "source": SOURCE_LONG,
            "dataset": DATASET_ID,
            "band": BAND_RAD,
            "unit": UNIT,
            "scale_m": args.scale,
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
            "created_at": utc_now(),
            "placeholder": False,
            "months_in_composite": months,
            "interpretation": (
                "Vrednosti su satelitska noćna radijansa u nW/cm²/sr, a ne SQM mag/arcsec². "
                "Javni heatmap prikaz je izglađena vizuelizacija; sirovi pikseli ostaju u JSON-u za statistiku."
            ),
        },
        "stats": pixel_stats(pixels),
        "pixels": pixels,
    }


def update_root_index(root: Path, period_kind: str, period: str) -> None:
    path = root / "index.json"
    index = read_json(path, {}) or {}
    index.setdefault("ok", True)
    index.setdefault("area", {
        "slug": "pio-rajac",
        "name": "ПИО Рајац",
        "group": "Заштићена подручја",
        "public_url": "https://piorajac.rs/monitoring-svetlosno-zagadjenje/",
    })
    key = "periods" if period_kind == "monthly" else "yearlyPeriods"
    items = [item for item in index.get(key, []) if str(item.get("id")) != period]
    items.append({
        "id": period,
        "label": f"{period[5:7]}/{period[:4]}" if period_kind == "monthly" else f"{period}. (годишњи просек)",
        "source": SOURCE_LABEL,
        "dataset": DATASET_ID,
        "kind": "monthly" if period_kind == "monthly" else "yearly",
    })
    items.sort(key=lambda item: str(item.get("id")), reverse=True)
    index[key] = items
    monthly_ids = sorted(str(item.get("id")) for item in index.get("periods", []) if re.fullmatch(r"\d{4}-\d{2}", str(item.get("id"))))
    yearly_ids = sorted(str(item.get("id")) for item in index.get("yearlyPeriods", []) if re.fullmatch(r"\d{4}", str(item.get("id"))))
    if monthly_ids:
        index["latest"] = monthly_ids[-1]
    index["updated_at"] = utc_now()
    index["coverage"] = {
        "count": len(monthly_ids),
        "first": monthly_ids[0] if monthly_ids else None,
        "last": monthly_ids[-1] if monthly_ids else None,
        "yearlyCount": len(yearly_ids),
        "yearlyFirst": yearly_ids[0] if yearly_ids else None,
        "yearlyLast": yearly_ids[-1] if yearly_ids else None,
    }
    index["note"] = (
        "Satellite-derived VIIRS nighttime-radiance JSON results. Entries identify their source dataset; "
        "new periods are NASA Black Marble VNP46A2 Collection 2."
    )
    write_json(path, index)


def update_protected_index(root: Path, period_kind: str, period: str, payload: Dict[str, Any]) -> None:
    path = root / "index.json"
    index = read_json(path, {}) or {}
    index.update({"status": "ok", "generated_at": utc_now(), "source_dataset": DATASET_ID})
    if period_kind == "monthly":
        months = set(index.get("months") or [])
        months.add(period)
        index["months"] = sorted(months)
        index["latest_month"] = max(index["months"])
        index["latest_file"] = "latest.json"
        index["rajac_file"] = "rajac.json"
        if period == index["latest_month"]:
            latest = dict(payload)
            latest["latest_month"] = period
            latest["archive_file"] = f"months/{period}.json"
            write_json(root / "latest.json", latest)
            if payload.get("rajac"):
                write_json(root / "rajac.json", payload["rajac"])
    else:
        years = set(index.get("years") or [])
        years.add(period)
        index["years"] = sorted(years)
        index["latest_year"] = max(index["years"])
    write_json(path, index)


def update_pixel_index(root: Path, period_kind: str, period: str, records: Sequence[Dict[str, Any]]) -> None:
    path = root / "pixels" / "index.json"
    index = read_json(path, {}) or {}
    index.update({
        "ok": True,
        "updated_at": utc_now(),
        "source_dataset": DATASET_ID,
        "areas": [record["properties"]["pa_id"] for record in records],
    })
    key = "months" if period_kind == "monthly" else "years"
    values = set(index.get(key) or [])
    values.add(period)
    index[key] = sorted(values)
    if key == "months":
        index["latest_month"] = max(index[key])
    else:
        index["latest_year"] = max(index[key])
    write_json(path, index)


def select_records(records: Sequence[Dict[str, Any]], area: str) -> List[Dict[str, Any]]:
    if area == "all":
        return list(records)
    selected = [record for record in records if record["properties"]["pa_id"] == area]
    if not selected:
        raise RuntimeError(f"Area {area!r} was not found in GeoJSON.")
    return selected


def image_and_months(period_kind: str, period: str, latest_complete: str, args: argparse.Namespace) -> Tuple[ee.Image, List[str]]:
    if period_kind == "monthly":
        return monthly_image(period, args), [period]
    return annual_image(int(period), latest_complete, args)


def process_period(
    period_kind: str,
    period: str,
    latest_complete: str,
    records_all: Sequence[Dict[str, Any]],
    records_selected: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> None:
    print(f"PROCESS {period_kind} {period}")
    image, months = image_and_months(period_kind, period, latest_complete, args)
    protected_root = Path(args.protected_results_dir)
    root_results = Path(args.root_results_dir)
    archive_folder = "months" if period_kind == "monthly" else "years"
    archive_path = protected_root / archive_folder / f"{period}.json"
    root_path = root_results / f"{period}.json"

    # Statistics and the public protected-area table must always contain all areas.
    # The --area option limits only the optional detailed pixel export.
    fc = to_feature_collection(records_all, args.simplify_meters)
    rows = compute_rows(image, fc, period_kind, period, args)
    protected = protected_payload(period_kind, period, rows, months)
    write_json(archive_path, protected)
    update_protected_index(protected_root, period_kind, period, protected)

    rajac_record = next((record for record in records_all if record["properties"].get("rajac")), None)
    if rajac_record is None:
        raise RuntimeError("Rajac feature was not found in GeoJSON.")
    rajac_geom = ee.Geometry(rajac_record["geometry"], None, False)
    rajac_pixels = sample_pixels(image, rajac_geom, args)
    write_json(root_path, root_payload(period_kind, period, rajac_pixels, months, args))
    update_root_index(root_results, period_kind, period)

    if args.pixels != "none":
        pixel_records = list(records_selected) if args.pixels == "all" else [rajac_record]
        folder = "months" if period_kind == "monthly" else "years"
        for position, record in enumerate(pixel_records, start=1):
            pa_id = record["properties"]["pa_id"]
            out = protected_root / "pixels" / folder / period / f"{pa_id}.json"
            if out.exists() and not args.overwrite:
                print(f"  SKIP pixels {pa_id}: exists")
                continue
            geometry = ee.Geometry(record["geometry"], None, False)
            pixels = sample_pixels(image, geometry, args)
            write_json(out, pixel_payload(record, period_kind, period, pixels, months, args))
            print(f"  PIXELS {position}/{len(pixel_records)} {pa_id}: {len(pixels)}")
            if args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
        update_pixel_index(protected_root, period_kind, period, pixel_records)

    print(f"OK {period}: protected rows={len(rows)}, Rajac pixels={len(rajac_pixels)}")


def periods_to_process(args: argparse.Namespace, latest_complete: str) -> List[Tuple[str, str]]:
    if args.mode == "latest":
        return [("monthly", latest_complete)]
    if args.mode == "single":
        if args.period_kind == "year":
            return [("annual", args.period)]
        return [("monthly", args.period)]
    end = latest_complete if args.end == "auto" else args.end
    periods = [("monthly", value) for value in month_range(args.start, end)]
    if args.yearly:
        years = sorted({value[:4] for _kind, value in periods})
        periods.extend(("annual", year) for year in years)
    return periods


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NASA Black Marble VNP46A2 monthly products for Rajac.")
    parser.add_argument("--mode", choices=["latest", "range", "single"], default="latest")
    parser.add_argument("--start", default="2026-01")
    parser.add_argument("--end", default="auto")
    parser.add_argument("--period-kind", choices=["month", "year"], default="month")
    parser.add_argument("--period", default="auto")
    parser.add_argument("--yearly", action="store_true")
    parser.add_argument("--area", default="all", help="all, rajac, or another pa_id")
    parser.add_argument("--pixels", choices=["all", "rajac", "none"], default="all")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--areas-geojson", default="protected_areas/zasticena_podrucja_srbije_gee.geojson")
    parser.add_argument("--root-results-dir", default="public/results")
    parser.add_argument("--protected-results-dir", default="public/results/protected-areas")
    parser.add_argument("--project", default=os.environ.get("EE_PROJECT_ID", ""))
    parser.add_argument("--service-account", default=os.environ.get("EE_SERVICE_ACCOUNT", ""))
    parser.add_argument("--key-file", default="")
    parser.add_argument("--key-json-env", default="EE_SERVICE_ACCOUNT_KEY_JSON")
    parser.add_argument("--ee-deadline", type=int, default=300_000)
    parser.add_argument("--scale", type=float, default=500.0)
    parser.add_argument("--tile-scale", type=int, default=4)
    parser.add_argument("--simplify-meters", type=float, default=0)
    parser.add_argument("--min-source-days", type=int, default=15)
    parser.add_argument("--min-observations", type=int, default=3)
    parser.add_argument("--min-months-year", type=int, default=3)
    parser.add_argument("--min-cloud-mask-quality", type=int, choices=[0, 1, 2, 3], default=2)
    parser.add_argument("--max-cloud-detection", type=int, choices=[0, 1, 2, 3], default=1)
    parser.add_argument("--include-ephemeral", action="store_true")
    parser.add_argument("--max-pixels-per-area", type=int, default=20_000)
    parser.add_argument("--value-digits", type=int, default=4)
    parser.add_argument("--pause-seconds", type=float, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "single" and args.period == "auto":
        args.period = latest_complete_month()
    init_ee(args)
    latest_complete = latest_complete_month()
    print(f"Dataset: {DATASET_ID}")
    print(f"Latest source date: {latest_source_date().isoformat()}")
    print(f"Latest publishable complete month: {latest_complete}")

    records_all = load_area_records(Path(args.areas_geojson))
    records_selected = select_records(records_all, args.area)
    periods = periods_to_process(args, latest_complete)
    for period_kind, period in periods:
        if period_kind == "monthly" and month_to_date(period) > month_to_date(latest_complete):
            raise RuntimeError(f"Refusing to publish incomplete/unavailable month {period}; latest complete is {latest_complete}.")
        process_period(period_kind, period, latest_complete, records_all, records_selected, args)
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
