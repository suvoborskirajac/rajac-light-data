#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIO Rajac light pollution processor for GitHub Actions.

Creates static JSON files for the WordPress viewer:
- public/results/index.json
- public/results/YYYY-MM.json

Default dataset is NOAA monthly VIIRS DNB stray-light corrected composite
(NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG), because it is already monthly,
stable for this first phase, and contains average radiance + cloud-free coverage.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ee

ROOT = Path(__file__).resolve().parent
BOUNDARY = ROOT / "public" / "boundaries" / "pio-rajac.geojson"
RESULTS = ROOT / "public" / "results"

DATASETS = {
    "monthly-vcmsl": {
        "id": "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG",
        "band": "avg_rad",
        "coverage_band": "cf_cvg",
        "scale_m": 463.83,
        "label": "NOAA VIIRS DNB Monthly V1 VCMSLCFG",
        "source": "NOAA / EOG VIIRS DNB monthly cloud-free composite, stray-light corrected",
        "unit": "nW/cm²/sr",
    },
    "black-marble-daily": {
        "id": "NASA/VIIRS/002/VNP46A2",
        "band": "Gap_Filled_DNB_BRDF_Corrected_NTL",
        "coverage_band": None,
        "scale_m": 500.0,
        "label": "NASA Black Marble VNP46A2 daily monthly mean",
        "source": "NASA Black Marble / VIIRS VNP46A2 daily mean composite",
        "unit": "nW/cm²/sr",
    },
}

@dataclass
class Period:
    id: str
    label: str
    start: str
    end: str


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)


def previous_month_start(d: date) -> date:
    first = date(d.year, d.month, 1)
    prev_last = first - timedelta(days=1)
    return date(prev_last.year, prev_last.month, 1)


def month_after(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def period_from_month_start(start: date) -> Period:
    end = month_after(start)
    return Period(
        id=f"{start.year}-{start.month:02d}",
        label=f"{start.month:02d}/{start.year}",
        start=start.isoformat(),
        end=end.isoformat(),
    )


def yearly_period(year: int) -> Period:
    return Period(
        id=f"{year}",
        label=f"{year}. (годишњи просек)",
        start=f"{year}-01-01",
        end=f"{year + 1}-01-01",
    )


def yearly_candidate_periods(start_year: int, end_year: int) -> List[Period]:
    """Years from end_year down to start_year, newest first."""
    return [yearly_period(y) for y in range(end_year, start_year - 1, -1)]


def latest_candidate_periods(n: int, extra_back: int = 8) -> List[Period]:
    cursor = date.today().replace(day=1)
    periods: List[Period] = []
    for _ in range(max(1, n + extra_back)):
        start = previous_month_start(cursor)
        periods.append(period_from_month_start(start))
        cursor = start
    return periods


def geojson_to_ee_geometry(gj: Dict[str, Any]) -> ee.Geometry:
    if gj.get("type") == "FeatureCollection":
        geoms = [feat.get("geometry") for feat in gj.get("features", []) if feat.get("geometry")]
        if not geoms:
            raise ValueError("GeoJSON FeatureCollection nema geometrije.")
        if len(geoms) == 1:
            return ee.Geometry(geoms[0])
        polys: List[Any] = []
        for geom in geoms:
            if geom.get("type") == "Polygon":
                polys.append(geom["coordinates"])
            elif geom.get("type") == "MultiPolygon":
                polys.extend(geom["coordinates"])
        return ee.Geometry.MultiPolygon(polys)
    if gj.get("type") == "Feature":
        return ee.Geometry(gj["geometry"])
    return ee.Geometry(gj)


def init_ee() -> None:
    project = os.environ.get("GEE_PROJECT", "deft-epigram-414409").strip()
    secret = os.environ.get("GEE_SERVICE_ACCOUNT_JSON", "").strip()
    if not secret:
        raise RuntimeError("Nedostaje GitHub secret GEE_SERVICE_ACCOUNT_JSON. Bez njega se ne mogu izračunati stvarni satelitski rezultati.")
    try:
        key = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GEE_SERVICE_ACCOUNT_JSON nije validan JSON tekst.") from exc
    email = key.get("client_email")
    if not email:
        raise RuntimeError("Service-account JSON nema client_email.")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tmp:
        json.dump(key, tmp)
        tmp_path = tmp.name
    credentials = ee.ServiceAccountCredentials(email, key_file=tmp_path)
    ee.Initialize(credentials, project=project)
    log(f"Earth Engine initialized for project: {project}; service account: {email}")


def safe_round(v: Any, ndigits: int = 4) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, ndigits)
    except Exception:
        return None


def classify_value(value: float) -> str:
    # Conservative thresholds for rural VIIRS DNB radiance around protected landscapes.
    if value <= 0.15:
        return "врло ниско"
    if value <= 0.35:
        return "ниско"
    if value <= 0.75:
        return "умерено"
    if value <= 1.50:
        return "повишено"
    return "високо"


def lonlat_bbox_from_center(lon: float, lat: float, scale_m: float) -> List[float]:
    dlat = scale_m / 111_320.0
    dlon = scale_m / (111_320.0 * max(0.15, math.cos(math.radians(lat))))
    return [
        round(lon - dlon / 2, 6),
        round(lat - dlat / 2, 6),
        round(lon + dlon / 2, 6),
        round(lat + dlat / 2, 6),
    ]


def image_collection_for_period(dataset: Dict[str, Any], period: Period, region: ee.Geometry) -> ee.ImageCollection:
    return ee.ImageCollection(dataset["id"]).filterDate(period.start, period.end).filterBounds(region)


def has_images(dataset: Dict[str, Any], period: Period, region: ee.Geometry) -> bool:
    try:
        size = image_collection_for_period(dataset, period, region).size().getInfo()
        return int(size or 0) > 0
    except Exception as exc:
        log(f"WARN: Ne mogu da proverim dostupnost za {period.id}: {exc}")
        return False


def select_available_periods(dataset: Dict[str, Any], region: ee.Geometry, months: int) -> List[Period]:
    chosen: List[Period] = []
    for period in latest_candidate_periods(months, extra_back=10):
        if has_images(dataset, period, region):
            chosen.append(period)
            log(f"Selected available month: {period.id}")
        else:
            log(f"Skipped unavailable month: {period.id}")
        if len(chosen) >= months:
            break
    if not chosen:
        raise RuntimeError("Nijedan mesečni period nije dostupan u Earth Engine kolekciji za izabrani dataset.")
    return chosen


def build_monthly_image(dataset: Dict[str, Any], period: Period, region: ee.Geometry) -> ee.Image:
    """Build an image for a single calendar month."""
    coll = image_collection_for_period(dataset, period, region)
    band = dataset["band"]
    if dataset["id"] == "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG":
        img = ee.Image(coll.first())
        radiance = img.select(band).rename(band)
        coverage = img.select(dataset["coverage_band"])
        # cf_cvg >= 1 keeps actual observed cloud-free pixels and removes no-data cells.
        return radiance.updateMask(coverage.gte(1)).clip(region)

    def mask_black_marble(img):
        ntl = img.select(band)
        quality = img.select("Mandatory_Quality_Flag").lte(1)
        no_snow = img.select("Snow_Flag").eq(0)
        cloud_qf = img.select("QF_Cloud_Mask")
        cloud_state = cloud_qf.rightShift(6).bitwiseAnd(3).lte(1)
        return ntl.updateMask(quality).updateMask(no_snow).updateMask(cloud_state).copyProperties(img, ["system:time_start"])

    return coll.map(mask_black_marble).mean().clip(region).rename(band)


def build_yearly_image(dataset: Dict[str, Any], period: Period, region: ee.Geometry) -> ee.Image:
    """Build an image for a full calendar year (mean of 12 cloud-masked monthly composites)."""
    coll = image_collection_for_period(dataset, period, region)
    band = dataset["band"]
    if dataset["id"] == "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG":
        cov_band = dataset["coverage_band"]

        def mask_month(img):
            return img.select(band).updateMask(img.select(cov_band).gte(1))

        return coll.map(mask_month).mean().clip(region).rename(band)

    # Black Marble (and any other future dataset) — same masking, just mean over the year.
    return build_monthly_image(dataset, period, region)


def reduce_stats(image: ee.Image, geom: ee.Geometry, band: str, scale_m: float) -> Dict[str, Any]:
    reducer = (
        ee.Reducer.minMax()
        .combine(ee.Reducer.mean(), sharedInputs=True)
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.percentile([90]), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
        .combine(ee.Reducer.sum(), sharedInputs=True)
    )
    info = image.reduceRegion(
        reducer=reducer,
        geometry=geom,
        scale=scale_m,
        maxPixels=1_000_000,
        bestEffort=True,
        tileScale=4,
    ).getInfo() or {}

    def pick(suffix: str):
        return info.get(f"{band}_{suffix}") if f"{band}_{suffix}" in info else info.get(suffix)

    return {
        "min": safe_round(pick("min")),
        "max": safe_round(pick("max")),
        "mean": safe_round(pick("mean")),
        "median": safe_round(pick("median")),
        "p90": safe_round(pick("p90")),
        "count": int(pick("count") or 0),
        "sum": safe_round(pick("sum")),
    }


def sample_pixels(image: ee.Image, geom: ee.Geometry, band: str, scale_m: float) -> List[Dict[str, Any]]:
    fc = image.sample(region=geom, scale=scale_m, geometries=True, tileScale=4)
    data = fc.getInfo()
    features = data.get("features", []) if isinstance(data, dict) else []
    pixels: List[Dict[str, Any]] = []
    for i, feat in enumerate(features, 1):
        props = feat.get("properties") or {}
        val = props.get(band)
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if val is None or len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        value = float(val)
        if not math.isfinite(value):
            continue
        pixels.append({
            "id": f"px-{i:04d}",
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "bbox": lonlat_bbox_from_center(lon, lat, scale_m),
            "value": round(value, 4),
            "class": classify_value(value),
        })
    # Stable order for viewer/statistics.
    pixels.sort(key=lambda p: (p["lat"], p["lon"]))
    return pixels


def compute_hotspots(pixels: List[Dict[str, Any]], top_n: int = 5) -> List[Dict[str, Any]]:
    """Top-N pixels with highest radiance inside the PIO boundary.
    These are real, geographically located hotspots — no inference about
    where the light originates from, just where it is brightest *within* PIO.
    """
    ranked = sorted(pixels, key=lambda p: float(p.get("value") or 0), reverse=True)
    out = []
    for i, p in enumerate(ranked[:top_n], 1):
        out.append({
            "rank": i,
            "lon": p["lon"],
            "lat": p["lat"],
            "value": p["value"],
            "class": p["class"],
        })
    return out


def build_result(dataset: Dict[str, Any], period: Period, region: ee.Geometry, kind: str = "monthly") -> Dict[str, Any]:
    """kind: 'monthly' or 'yearly' — selects the image-building strategy."""
    band = dataset["band"]
    scale_m = float(dataset["scale_m"])
    if kind == "yearly":
        image = build_yearly_image(dataset, period, region)
    else:
        image = build_monthly_image(dataset, period, region)
    stats = reduce_stats(image, region, band, scale_m)
    pixels = sample_pixels(image, region, band, scale_m)
    if stats["count"] <= 0 or not pixels:
        raise RuntimeError(f"Period {period.id} nema validne piksele u granici PIO Rajac.")
    return {
        "ok": True,
        "meta": {
            "id": period.id,
            "label": period.label,
            "kind": kind,
            "source": dataset["source"],
            "dataset": dataset["id"],
            "band": band,
            "unit": dataset["unit"],
            "scale_m": scale_m,
            "date_start": period.start,
            "date_end": period.end,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "placeholder": False,
            "interpretation": "Vrednosti su satelitska noćna radijansa u nW/cm²/sr, a ne SQM mag/arcsec². Javni heatmap prikaz je izglađena vizuelizacija; sirovi pikseli ostaju u JSON-u za statistiku.",
        },
        "stats": {"overall": stats, "hotspots": compute_hotspots(pixels, top_n=5)},
        "pixels": pixels,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12, help="Number of latest available complete months to process.")
    parser.add_argument("--dataset", choices=sorted(DATASETS.keys()), default="monthly-vcmsl")
    parser.add_argument("--yearly", action="store_true", help="Also generate yearly averaged composites.")
    parser.add_argument("--yearly-from", type=int, default=2013, help="Earliest year to include in yearly composites (default 2013, when VCMSLCFG has full coverage).")
    parser.add_argument("--yearly-to", type=int, default=date.today().year - 1, help="Latest year to include (defaults to last complete calendar year).")
    args = parser.parse_args()

    if not BOUNDARY.exists():
        raise RuntimeError(f"Nedostaje granica: {BOUNDARY}")

    dataset = DATASETS[args.dataset]
    RESULTS.mkdir(parents=True, exist_ok=True)

    init_ee()
    boundary = load_json(BOUNDARY)
    region = geojson_to_ee_geometry(boundary)

    # --- Monthly composites ---
    periods = select_available_periods(dataset, region, max(1, args.months))
    index_periods = []
    for period in periods:
        log(f"Processing month {period.id} from {dataset['id']}...")
        result = build_result(dataset, period, region, kind="monthly")
        write_json(RESULTS / f"{period.id}.json", result)
        index_periods.append({
            "id": period.id,
            "label": period.label,
            "source": dataset["label"],
            "dataset": dataset["id"],
            "kind": "monthly",
        })
        log(f"OK {period.id}: {len(result['pixels'])} pixels, mean={result['stats']['overall']['mean']}, max={result['stats']['overall']['max']}")

    # --- Yearly composites (optional) ---
    index_yearly: List[Dict[str, Any]] = []
    if args.yearly:
        candidates = yearly_candidate_periods(args.yearly_from, args.yearly_to)
        for year_period in candidates:
            if not has_images(dataset, year_period, region):
                log(f"Skipped year {year_period.id} (no images in collection)")
                continue
            log(f"Processing year {year_period.id} from {dataset['id']}...")
            try:
                result = build_result(dataset, year_period, region, kind="yearly")
            except RuntimeError as exc:
                log(f"WARN year {year_period.id}: {exc}")
                continue
            write_json(RESULTS / f"{year_period.id}.json", result)
            index_yearly.append({
                "id": year_period.id,
                "label": year_period.label,
                "source": dataset["label"],
                "dataset": dataset["id"],
                "kind": "yearly",
            })
            log(f"OK year {year_period.id}: {len(result['pixels'])} pixels, mean={result['stats']['overall']['mean']}, max={result['stats']['overall']['max']}")

    index = {
        "ok": True,
        "latest": periods[0].id,
        "periods": index_periods,
        "yearlyPeriods": index_yearly,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "coverage": {
            "count": len(index_periods),
            "first": periods[-1].id,
            "last": periods[0].id,
            "yearlyCount": len(index_yearly),
            "yearlyFirst": index_yearly[-1]["id"] if index_yearly else None,
            "yearlyLast": index_yearly[0]["id"] if index_yearly else None,
        },
        "note": "Generated by GitHub Actions processor for PIO Rajac light pollution monitor. These are real satellite-derived VIIRS nighttime radiance JSON results.",
    }
    write_json(RESULTS / "index.json", index)
    log(f"DONE: {len(periods)} monthly + {len(index_yearly)} yearly periods written to {RESULTS}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
