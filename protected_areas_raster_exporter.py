#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VIIRS PNG raster exporter for protected areas Serbia.

Creates Earth Engine thumbnail PNG maps for one protected area or all areas,
for a selected month/year or for all years.

Outputs:
  public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>.png
  public/results/protected-areas/rasters/years/YYYY/<pa_id>.png
  public/results/protected-areas/rasters/index.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ee
import requests

DATASET_ID = "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG"
BAND_RAD = "avg_rad"
BAND_CF = "cf_cvg"
PALETTE = ["233b2a", "71945e", "d6e46d", "f1b457", "ee7249", "b53d35"]


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


def render_image(img: ee.Image, geom: ee.Geometry, args: argparse.Namespace) -> ee.Image:
    clipped = img.select("avg_rad").clip(geom)
    vis = clipped.visualize(min=args.vis_min, max=args.vis_max, palette=PALETTE, forceRgbOutput=True)
    outline = ee.Image().byte().paint(ee.FeatureCollection([ee.Feature(geom)]), 1, args.outline_width).selfMask().visualize(palette=["ffffff"], forceRgbOutput=True)
    return vis.blend(outline)


def download_png(url: str, out: Path, retries: int = 3) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 200 and len(r.content) > 100:
                out.write_bytes(r.content)
                return
            last = RuntimeError(f"HTTP {r.status_code}, bytes={len(r.content)}")
        except Exception as exc:
            last = exc
        time.sleep(2 * attempt)
    raise RuntimeError(f"Cannot download PNG: {url}: {last}")


def export_one(kind: str, period: str, area: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    pa_id = area["properties"].get("pa_id")
    name = area["properties"].get("name") or area["properties"].get("name_lat") or pa_id
    folder = "years" if kind == "year" else "months"
    out = Path(args.results_dir) / folder / period / f"{pa_id}.png"
    if out.exists() and not args.overwrite:
        print(f"SKIP existing: {out}")
        return {"status": "skipped", "period_kind": kind, "period": period, "pa_id": pa_id, "name": name, "file": str(out)}
    geom = ee.Geometry(area["geometry"], None, False)
    if args.simplify_meters and args.simplify_meters > 0:
        geom = geom.simplify(maxError=args.simplify_meters)
    img = image_for(kind, period, args)
    rendered = render_image(img, geom, args)
    region = geom.bounds(maxError=1)
    url = rendered.getThumbURL({
        "region": region,
        "dimensions": args.dimensions,
        "format": "png",
        "crs": "EPSG:4326",
    })
    download_png(url, out)
    print(f"OK: {out}")
    return {"status": "ok", "period_kind": kind, "period": period, "pa_id": pa_id, "name": name, "file": str(out)}


def build_periods(args: argparse.Namespace) -> List[Tuple[str, str]]:
    latest = latest_dataset_month() if args.end_month == "auto" or args.period == "auto" else None
    if args.range_mode == "single":
        if args.period_kind == "month":
            p = latest if args.period == "auto" else args.period
            return [("month", p)]
        return [("year", args.period)]
    if args.range_mode == "all_years":
        latest_year = int((latest or latest_dataset_month())[:4])
        return [("year", str(y)) for y in range(args.start_year, latest_year + 1)]
    if args.range_mode == "months_range":
        end = latest if args.end_month == "auto" else args.end_month
        return [("month", p) for p in month_range(args.start_month, end)]
    raise ValueError(f"Unknown range_mode: {args.range_mode}")


def update_index(results_dir: Path, records: List[Dict[str, Any]]) -> None:
    idx_path = results_dir / "index.json"
    old: Dict[str, Any] = {"records": []}
    if idx_path.exists():
        try:
            old = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    merged = {(r.get("period_kind"), r.get("period"), r.get("pa_id")): r for r in old.get("records", [])}
    for r in records:
        merged[(r.get("period_kind"), r.get("period"), r.get("pa_id"))] = r
    out = {
        "status": "ok",
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source_dataset": DATASET_ID,
        "records": sorted(merged.values(), key=lambda x: (str(x.get("period_kind")), str(x.get("period")), str(x.get("pa_id")))),
    }
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate VIIRS PNG raster maps for protected areas Serbia")
    p.add_argument("--range-mode", choices=["single", "all_years", "months_range"], default="single")
    p.add_argument("--period-kind", choices=["month", "year"], default="month")
    p.add_argument("--period", default="auto", help="YYYY-MM for month, YYYY for year, or auto for latest month")
    p.add_argument("--start-year", type=int, default=2014)
    p.add_argument("--start-month", default="2026-01")
    p.add_argument("--end-month", default="auto")
    p.add_argument("--area", default="rajac", help="pa_id or all")
    p.add_argument("--max-images", type=int, default=60, help="Safety limit. Set higher for all areas/all years.")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--areas-geojson", default="protected_areas/zasticena_podrucja_srbije_gee.geojson")
    p.add_argument("--results-dir", default="public/results/protected-areas/rasters")
    p.add_argument("--project", default=os.environ.get("EE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    p.add_argument("--service-account", default=os.environ.get("EE_SERVICE_ACCOUNT") or None)
    p.add_argument("--key-file", default=os.environ.get("EE_KEY_FILE") or None)
    p.add_argument("--key-json-env", default=os.environ.get("EE_KEY_JSON_ENV") or "")
    p.add_argument("--ee-deadline", type=int, default=300000)
    p.add_argument("--min-cf-cvg", type=int, default=1)
    p.add_argument("--min-months-year", type=int, default=3)
    p.add_argument("--dimensions", type=int, default=1400)
    p.add_argument("--vis-min", type=float, default=0.0)
    p.add_argument("--vis-max", type=float, default=3.0)
    p.add_argument("--outline-width", type=int, default=2)
    p.add_argument("--simplify-meters", type=float, default=0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    init_ee(args)
    areas = load_areas(Path(args.areas_geojson))
    if args.area != "all":
        areas = [a for a in areas if str(a["properties"].get("pa_id")) == args.area]
        if not areas:
            raise RuntimeError(f"Area not found: {args.area}")
    periods = build_periods(args)
    total = len(areas) * len(periods)
    print(f"Raster export plan: {len(areas)} area(s) × {len(periods)} period(s) = {total} PNG")
    if total > args.max_images:
        raise RuntimeError(f"Safety stop: requested {total} images, max-images={args.max_images}. Increase max-images intentionally.")
    records: List[Dict[str, Any]] = []
    for kind, period in periods:
        for area in areas:
            records.append(export_one(kind, period, area, args))
    update_index(Path(args.results_dir), records)
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
