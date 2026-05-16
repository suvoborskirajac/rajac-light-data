#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions processor for Rajac Light Pollution Monitor.

Outputs static JSON files into public/results/:
- index.json
- YYYY-MM.json for each processed month
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ee

ROOT = Path(__file__).resolve().parent
BOUNDARY = ROOT / "public" / "boundaries" / "pio-rajac.geojson"
RESULTS = ROOT / "public" / "results"
DATASET = "NASA/VIIRS/002/VNP46A2"
BAND = "Gap_Filled_DNB_BRDF_Corrected_NTL"
SCALE_M = 500


@dataclass
class Period:
    id: str
    label: str
    start: str
    end: str


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)


def prev_month_start(d: date) -> date:
    first = date(d.year, d.month, 1)
    prev_last = first - timedelta(days=1)
    return date(prev_last.year, prev_last.month, 1)


def last_n_complete_months(n: int) -> List[Period]:
    cursor = date.today().replace(day=1)
    periods: List[Period] = []
    for _ in range(max(1, n)):
        start = prev_month_start(cursor)
        end = cursor
        periods.append(
            Period(
                id=f"{start.year}-{start.month:02d}",
                label=f"{start.month:02d}/{start.year}",
                start=start.isoformat(),
                end=end.isoformat(),
            )
        )
        cursor = start
    return periods


def geojson_to_ee_geometry(gj: Dict[str, Any]) -> ee.Geometry:
    if gj.get("type") == "FeatureCollection":
        geoms = [
            feat.get("geometry")
            for feat in gj.get("features", [])
            if feat.get("geometry")
        ]
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


def classify_value(value: float) -> str:
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


def init_ee() -> None:
    project = os.environ.get("GEE_PROJECT", "def-epigram-414409").strip()
    secret = os.environ.get("GEE_SERVICE_ACCOUNT_JSON", "").strip()

    if not secret:
        raise RuntimeError("Nedostaje GitHub secret GEE_SERVICE_ACCOUNT_JSON.")

    try:
        key = json.loads(secret)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GEE_SERVICE_ACCOUNT_JSON nije validan JSON tekst.") from exc

    email = key.get("client_email")
    if not email:
        raise RuntimeError("Service account JSON nema client_email.")

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", delete=False
    ) as tmp:
        json.dump(key, tmp)
        tmp_path = tmp.name

    credentials = ee.ServiceAccountCredentials(email, key_file=tmp_path)
    ee.Initialize(credentials, project=project)


def build_masked_composite(period: Period, region: ee.Geometry) -> ee.Image:
    collection = (
        ee.ImageCollection(DATASET)
        .filterDate(period.start, period.end)
        .filterBounds(region)
    )

    def mask_image(img):
        ntl = img.select(BAND)
        quality = img.select("Mandatory_Quality_Flag").lte(1)
        no_snow = img.select("Snow_Flag").eq(0)
        cloud_qf = img.select("QF_Cloud_Mask")
        cloud_state = cloud_qf.rightShift(6).bitwiseAnd(3).lte(1)
        return (
            ntl.updateMask(quality)
            .updateMask(no_snow)
            .updateMask(cloud_state)
            .copyProperties(img, ["system:time_start"])
        )

    return collection.map(mask_image).mean().clip(region).rename(BAND)


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


def reduce_stats(image: ee.Image, geom: ee.Geometry) -> Dict[str, Any]:
    reducer = (
        ee.Reducer.minMax()
        .combine(ee.Reducer.mean(), sharedInputs=True)
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.percentile([90]), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
        .combine(ee.Reducer.sum(), sharedInputs=True)
    )

    info = (
        image.reduceRegion(
            reducer=reducer,
            geometry=geom,
            scale=SCALE_M,
            maxPixels=1_000_000,
            bestEffort=True,
            tileScale=4,
        ).getInfo()
        or {}
    )

    def pick(suffix: str):
        return info.get(f"{BAND}_{suffix}") if f"{BAND}_{suffix}" in info else info.get(suffix)

    return {
        "min": safe_round(pick("min")),
        "max": safe_round(pick("max")),
        "mean": safe_round(pick("mean")),
        "median": safe_round(pick("median")),
        "p90": safe_round(pick("p90")),
        "count": int(pick("count") or 0),
        "sum": safe_round(pick("sum")),
    }


def sample_pixels(image: ee.Image, geom: ee.Geometry) -> List[Dict[str, Any]]:
    fc = image.sample(region=geom, scale=SCALE_M, geometries=True, tileScale=4)
    data = fc.getInfo()
    features = data.get("features", []) if isinstance(data, dict) else []

    pixels: List[Dict[str, Any]] = []
    for i, feat in enumerate(features, 1):
        val = (feat.get("properties") or {}).get(BAND)
        coords = (feat.get("geometry") or {}).get("coordinates") or []

        if val is None or len(coords) < 2:
            continue

        lon, lat = float(coords[0]), float(coords[1])
        value = float(val)

        pixels.append(
            {
                "id": f"px-{i:04d}",
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "bbox": lonlat_bbox_from_center(lon, lat, SCALE_M),
                "value": round(value, 4),
                "class": classify_value(value),
            }
        )

    return pixels


def bearing_from_peak(lon: float, lat: float, peak: Tuple[float, float]) -> float:
    return math.degrees(math.atan2(lon - peak[0], lat - peak[1])) % 360


def compute_direction_summary(pixels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    peak = (20.224918, 44.136333)

    targets = {
        "Љиг": (20.238, 44.226),
        "Мионица": (20.081, 44.252),
        "Ваљево": (19.890, 44.270),
        "Ибарска магистрала": (20.218, 44.170),
    }

    out = []
    for name, target in targets.items():
        tb = bearing_from_peak(target[0], target[1], peak)
        selected = []

        for p in pixels:
            b = bearing_from_peak(float(p["lon"]), float(p["lat"]), peak)
            diff = abs((b - tb + 180) % 360 - 180)

            if diff <= 35:
                selected.append(p)

        if selected:
            vals = [float(p["value"]) for p in selected]
            out.append(
                {
                    "name": name,
                    "bearing": round(tb, 1),
                    "pixels": len(vals),
                    "mean": round(sum(vals) / len(vals), 4),
                    "max": round(max(vals), 4),
                }
            )

    return out


def build_result(period: Period, region: ee.Geometry) -> Dict[str, Any]:
    image = build_masked_composite(period, region)
    stats = reduce_stats(image, region)
    pixels = sample_pixels(image, region)

    return {
        "ok": True,
        "meta": {
            "id": period.id,
            "label": period.label,
            "source": "NASA Black Marble / VIIRS VNP46A2",
            "dataset": DATASET,
            "band": BAND,
            "scale_m": SCALE_M,
            "date_start": period.start,
            "date_end": period.end,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "placeholder": False,
            "interpretation": (
                "Vrednosti su noćna radijansa u nW/cm²/sr, a ne SQM mag/arcsec². "
                "Javni heatmap prikaz je izglađena vizuelizacija."
            ),
        },
        "stats": {
            "overall": stats,
            "directions": compute_direction_summary(pixels),
        },
        "pixels": pixels,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12)
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)

    # Earth Engine mora biti inicijalizovan pre kreiranja ee.Geometry objekta.
    init_ee()

    boundary = load_json(BOUNDARY)
    region = geojson_to_ee_geometry(boundary)

    periods = last_n_complete_months(args.months)
    index_periods = []

    for period in periods:
        print(f"Processing {period.id}...")
        result = build_result(period, region)
        write_json(RESULTS / f"{period.id}.json", result)
        index_periods.append(
            {
                "id": period.id,
                "label": period.label,
                "source": "NASA Black Marble VNP46A2",
            }
        )
        print(f"OK {period.id}: {len(result['pixels'])} pixels")

    index = {
        "ok": True,
        "latest": periods[0].id,
        "periods": index_periods,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "coverage": {
            "count": len(index_periods),
            "first": periods[0].id,
            "last": periods[-1].id,
        },
        "note": "Generated by GitHub Actions processor for PIO Rajac light pollution monitor.",
    }

    write_json(RESULTS / "index.json", index)
    print(f"DONE: {len(periods)} periods written to {RESULTS}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
