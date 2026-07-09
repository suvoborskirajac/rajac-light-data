#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIIRS pixel JSON exporter for protected areas Serbia.

This exporter creates JSON files compatible with the older Rajac SVG heatmap
renderer. It exports pixel centroids, approximate pixel bbox, value and class.

Outputs:
  public/results/protected-areas/pixels/months/YYYY-MM/<pa_id>.json
  public/results/protected-areas/pixels/years/YYYY/<pa_id>.json
  public/results/protected-areas/pixels/index.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import statistics
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ee

DATASET_ID = "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG"
BAND_RAD = "avg_rad"
BAND_CF = "cf_cvg"
JSON_FLAGS = dict(ensure_ascii=False, indent=2)

CLASS_COLORS = {
    "врло ниско": "#1e442e",
    "ниско": "#4d7b38",
    "умерено": "#c7de59",
    "повишено": "#e9bd55",
    "високо": "#ee6a55",
}


def slugify(value: str, fallback: str = "area") -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or fallback


def ym_to_date(month: str) -> dt.date:
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise ValueError(f"Month must be YYYY-MM, got {month!r}")
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1)


def add_month(d: dt.date, n: int = 1) -> dt.date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return dt.date(y, m, 1)


def ym(d: dt.date) -> str:
    return d.strftime("%Y-%m")


def month_range(start_ym: str, end_ym_inclusive: str) -> Iterable[str]:
    cur = ym_to_date(start_ym)
    end = ym_to_date(end_ym_inclusive)
    while cur <= end:
        yield ym(cur)
        cur = add_month(cur, 1)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, **JSON_FLAGS) + "\n", encoding="utf-8")
    tmp.replace(path)


def init_ee(args: argparse.Namespace) -> None:
    key_file = args.key_file
    temp_file = None
    if args.key_json_env:
        key_json = os.environ.get(args.key_json_env)
        if not key_json:
            raise RuntimeError(f"Missing ENV {args.key_json_env!r} with service account JSON")
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


def load_areas(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for idx, feat in enumerate(data.get("features", []), start=1):
        props = dict(feat.get("properties") or {})
        base = slugify(props.get("name_lat") or props.get("name") or f"area-{idx}", f"area-{idx}")
        seen[base] = seen.get(base, 0) + 1
        pa_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        props.setdefault("pa_id", pa_id)
        props.setdefault("pa_order", idx)
        out.append({"type": "Feature", "properties": props, "geometry": feat.get("geometry")})
    return out


def viirs_collection() -> ee.ImageCollection:
    return ee.ImageCollection(DATASET_ID)


def latest_dataset_month() -> str:
    first = ee.Image(viirs_collection().sort("system:time_start", False).first())
    millis = first.get("system:time_start").getInfo()
    if millis is None:
        raise RuntimeError("Cannot read latest VIIRS month from Earth Engine")
    d = dt.datetime.utcfromtimestamp(int(millis) / 1000).date().replace(day=1)
    return ym(d)


def monthly_image(period: str, min_cf_cvg: int) -> ee.Image:
    start = ym_to_date(period)
    end = add_month(start, 1)
    img = ee.Image(viirs_collection().filterDate(start.isoformat(), end.isoformat()).first())
    rad = img.select(BAND_RAD).max(ee.Image.constant(0)).rename("avg_rad")
    cf = img.select(BAND_CF).rename("cf_cvg")
    return rad.updateMask(cf.gte(min_cf_cvg)).set({"period": period, "period_kind": "month"})


def annual_image(year: str, min_cf_cvg: int, min_months_year: int) -> ee.Image:
    y = int(year)
    coll = viirs_collection().filterDate(f"{y}-01-01", f"{y+1}-01-01")

    def prep(img: ee.Image) -> ee.Image:
        cf = img.select(BAND_CF).rename("cf_cvg")
        valid = cf.gte(min_cf_cvg)
        rad = img.select(BAND_RAD).max(ee.Image.constant(0)).rename("avg_rad").updateMask(valid)
        return rad.addBands(valid.rename("valid_month"))

    prepared = coll.map(prep)
    rad = prepared.select("avg_rad").mean().rename("avg_rad")
    months = prepared.select("valid_month").sum().rename("months_used")
    return rad.updateMask(months.gte(min_months_year)).set({"period": year, "period_kind": "year"})


def image_for(kind: str, period: str, args: argparse.Namespace) -> ee.Image:
    if kind == "month":
        return monthly_image(period, args.min_cf_cvg)
    return annual_image(period, args.min_cf_cvg, args.min_months_year)


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
    half_lat = (scale_m / 2.0) / 111320.0
    cos_lat = max(0.1, math.cos(math.radians(lat)))
    half_lon = (scale_m / 2.0) / (111320.0 * cos_lat)
    return [
        round(lon - half_lon, 6),
        round(lat - half_lat, 6),
        round(lon + half_lon, 6),
        round(lat + half_lat, 6),
    ]


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] * (c - k) + vals[c] * (k - f)


def make_stats(pixels: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = [float(p["value"]) for p in pixels if p.get("value") is not None]
    if not vals:
        return {
            "overall": {
                "min": None,
                "max": None,
                "mean": None,
                "median": None,
                "p90": None,
                "count": 0,
                "sum": 0,
            },
            "hotspots": [],
        }

    hotspots = sorted(pixels, key=lambda p: float(p.get("value") or 0), reverse=True)[:5]

    return {
        "overall": {
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "mean": round(sum(vals) / len(vals), 4),
            "median": round(statistics.median(vals), 4),
            "p90": round(percentile(vals, 90) or 0, 4),
            "count": len(vals),
            "sum": round(sum(vals), 4),
        },
        "hotspots": [
            {
                "rank": i + 1,
                "lon": h["lon"],
                "lat": h["lat"],
                "value": h["value"],
                "class": h["class"],
            }
            for i, h in enumerate(hotspots)
        ],
    }


def sampling_geometry(geom: ee.Geometry, args: argparse.Namespace) -> ee.Geometry:
    """Return geometry used only for choosing VIIRS pixel centres.

    The old Rajac SVG map included edge pixels whose VIIRS cells intersect the
    protected-area boundary. Earth Engine sample(region=geom) keeps only points
    whose centres are inside the polygon, so border pixels are missing.

    We therefore sample from a buffered geometry, while the front-end SVG still
    clips the visual heatmap to the real protected-area boundary.
    """
    buffer_m = args.edge_buffer_meters
    if buffer_m is None or buffer_m < 0:
        buffer_m = float(args.scale) * 0.55
    if buffer_m <= 0:
        return geom
    return geom.buffer(buffer_m)


def sample_pixels(image: ee.Image, geom: ee.Geometry, args: argparse.Namespace) -> List[Dict[str, Any]]:
    sample_region = sampling_geometry(geom, args)
    fc = image.select("avg_rad").sample(
        region=sample_region,
        scale=args.scale,
        geometries=True,
        tileScale=args.tile_scale,
    )

    if args.max_pixels_per_area > 0:
        fc = fc.limit(args.max_pixels_per_area)

    info = fc.getInfo()
    pixels: List[Dict[str, Any]] = []

    for feat in info.get("features", []):
        props = feat.get("properties") or {}
        geom_info = feat.get("geometry") or {}
        coords = geom_info.get("coordinates") or []

        if len(coords) < 2:
            continue

        value = props.get("avg_rad")

        try:
            value_f = float(value)
        except Exception:
            continue

        if not math.isfinite(value_f):
            continue

        lon = round(float(coords[0]), 6)
        lat = round(float(coords[1]), 6)
        v = round(value_f, args.value_digits)

        pixels.append(
            {
                "lon": lon,
                "lat": lat,
                "bbox": pixel_bbox(lon, lat, args.scale),
                "value": v,
                "class": pixel_class(v),
            }
        )

    pixels.sort(key=lambda p: (-p["lat"], p["lon"]))

    for i, p in enumerate(pixels, start=1):
        p["id"] = f"px-{i:04d}"

    return pixels


def area_name(area: Dict[str, Any]) -> str:
    p = area.get("properties") or {}
    return p.get("name") or p.get("pa_name") or p.get("name_lat") or p.get("pa_id") or "Подручје"


def export_area(kind: str, period: str, area: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    props = dict(area.get("properties") or {})
    pa_id = str(props.get("pa_id"))

    folder = "years" if kind == "year" else "months"
    out = Path(args.results_dir) / "pixels" / folder / period / f"{pa_id}.json"

    if out.exists() and not args.overwrite:
        print(f"SKIP existing: {out}")
        return {
            "status": "skipped",
            "period_kind": kind,
            "period": period,
            "pa_id": pa_id,
            "file": str(out),
        }

    geom = ee.Geometry(area["geometry"], None, False)

    if args.simplify_meters > 0:
        geom = geom.simplify(maxError=args.simplify_meters)

    image = image_for(kind, period, args)
    pixels = sample_pixels(image, geom, args)
    stats = make_stats(pixels)

    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    label = period[5:7] + "/" + period[:4] if kind == "month" and len(period) == 7 else period

    edge_buffer = (
        args.edge_buffer_meters
        if args.edge_buffer_meters is not None and args.edge_buffer_meters >= 0
        else round(float(args.scale) * 0.55, 3)
    )

    result = {
        "ok": True,
        "area": {
            "slug": pa_id,
            "pa_id": pa_id,
            "name": area_name(area),
            "name_lat": props.get("name_lat") or props.get("pa_name_lat") or "",
            "type": props.get("pa_type") or props.get("type") or props.get("pa_type_clean") or "",
            "group": "Заштићена подручја Србије",
        },
        "meta": {
            "id": period,
            "label": label,
            "kind": "monthly" if kind == "month" else "annual",
            "source": "NOAA / EOG VIIRS DNB monthly cloud-free composite, stray-light corrected",
            "dataset": DATASET_ID,
            "band": BAND_RAD,
            "unit": "nW/cm²/sr",
            "scale_m": args.scale,
            "edge_buffer_m": edge_buffer,
            "pixel_sampling": "buffered_centres_to_include_boundary_intersecting_viirs_pixels",
            "created_at": now,
            "placeholder": False,
            "interpretation": (
                "SVG heatmap prikaz je izglađena vizuelizacija iz centara VIIRS piksela; "
                "sirovi pikseli ostaju u JSON-u za statistiku."
            ),
        },
        "stats": stats,
        "pixels": pixels,
    }

    write_json(out, result)
    print(f"OK: {out} ({len(pixels)} pixels)")

    return {
        "status": "ok",
        "period_kind": kind,
        "period": period,
        "pa_id": pa_id,
        "pixels": len(pixels),
        "file": str(out),
    }


def build_periods(args: argparse.Namespace) -> List[Tuple[str, str]]:
    latest = latest_dataset_month() if args.end_month == "auto" or args.period == "auto" else None

    if args.range_mode == "single":
        if args.period_kind == "month":
            return [("month", latest if args.period == "auto" else args.period)]
        return [("year", args.period)]

    if args.range_mode == "all_years":
        latest_year = int((latest or latest_dataset_month())[:4])
        return [("year", str(y)) for y in range(args.start_year, latest_year + 1)]

    if args.range_mode == "months_range":
        end = latest if args.end_month == "auto" else args.end_month
        return [("month", p) for p in month_range(args.start_month, end)]

    raise ValueError(f"Unknown range_mode: {args.range_mode}")


def update_index(results_dir: Path, records: List[Dict[str, Any]]) -> None:
    idx_path = results_dir / "pixels" / "index.json"
    old: Dict[str, Any] = {"records": []}

    if idx_path.exists():
        try:
            old = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    merged = {
        (r.get("period_kind"), r.get("period"), r.get("pa_id")): r
        for r in old.get("records", [])
    }

    for r in records:
        merged[(r.get("period_kind"), r.get("period"), r.get("pa_id"))] = r

    out = {
        "status": "ok",
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source_dataset": DATASET_ID,
        "description": "Pixel JSON files for SVG Rajac-style heatmap rendering.",
        "records": sorted(
            merged.values(),
            key=lambda x: (
                str(x.get("period_kind")),
                str(x.get("period")),
                str(x.get("pa_id")),
            ),
        ),
    }

    write_json(idx_path, out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate VIIRS pixel JSON for protected areas Serbia")

    p.add_argument("--range-mode", choices=["single", "all_years", "months_range"], default="single")
    p.add_argument("--period-kind", choices=["month", "year"], default="month")
    p.add_argument("--period", default="auto", help="YYYY-MM for month, YYYY for year, or auto for latest month")

    p.add_argument("--start-year", type=int, default=2014)
    p.add_argument("--start-month", default="2026-01")
    p.add_argument("--end-month", default="auto")

    p.add_argument("--area", default="rajac", help="pa_id or all")
    p.add_argument("--max-areas", type=int, default=60, help="Safety limit for number of areas to process")
    p.add_argument("--max-pixels-per-area", type=int, default=5000)
    p.add_argument("--overwrite", action="store_true")

    p.add_argument("--areas-geojson", default="protected_areas/zasticena_podrucja_srbije_gee.geojson")
    p.add_argument("--results-dir", default="public/results/protected-areas")

    p.add_argument("--project", default=os.environ.get("EE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    p.add_argument("--service-account", default=os.environ.get("EE_SERVICE_ACCOUNT") or None)
    p.add_argument("--key-file", default=os.environ.get("EE_KEY_FILE") or None)
    p.add_argument("--key-json-env", default=os.environ.get("EE_KEY_JSON_ENV") or "")

    p.add_argument("--ee-deadline", type=int, default=300000)
    p.add_argument("--scale", type=float, default=463.83)
    p.add_argument(
        "--edge-buffer-meters",
        type=float,
        default=260.0,
        help=(
            "Buffer used only for sampling pixel centres; "
            "restores boundary-intersecting pixels like the old Rajac SVG map."
        ),
    )
    p.add_argument("--tile-scale", type=int, default=4)
    p.add_argument("--min-cf-cvg", type=int, default=1)
    p.add_argument("--min-months-year", type=int, default=3)
    p.add_argument("--simplify-meters", type=float, default=0)
    p.add_argument("--value-digits", type=int, default=2)

    return p.parse_args()


def main() -> int:
    args = parse_args()
    init_ee(args)

    areas = load_areas(Path(args.areas_geojson))

    if args.area != "all":
        areas = [a for a in areas if str(a["properties"].get("pa_id")) == args.area]
        if not areas:
            raise RuntimeError(f"Area not found: {args.area}")

    if len(areas) > args.max_areas:
        raise RuntimeError(f"Safety stop: {len(areas)} areas requested, max-areas={args.max_areas}")

    periods = build_periods(args)
    records: List[Dict[str, Any]] = []

    print(f"Pixel export plan: {len(areas)} area(s) × {len(periods)} period(s)")

    for kind, period in periods:
        for area in areas:
            records.append(export_area(kind, period, area, args))

    update_index(Path(args.results_dir), records)

    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
