#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build compact NASA Black Marble VNP46A2 pixel tiles for all of Serbia.

This national product is deliberately separate from the protected-area outputs.
It reuses the same quality filtering and monthly/annual composite functions from
``black_marble/build_black_marble.py`` but samples one Serbia-wide grid.

Outputs
-------
public/results/serbia/index.json
public/results/serbia/{months|years}/<period>/index.json
public/results/serbia/{months|years}/<period>/tiles/<tile_id>.json

Each tile JSON stores compact pixel triples ``[lon, lat, radiance]`` so a full
country product remains practical to serve in a web map. Values are
``nW/cm²/sr`` and are *not* SQM measurements.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import ee

# GitHub Actions often executes this file directly (``python black_marble/build_serbia_viirs.py``).
# In that mode Python puts ``black_marble/`` rather than the repository root on
# sys.path, so the package-style import below would fail.  Add the repo root
# explicitly; module execution (``python -m ...``) continues to work as well.
import sys
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import black_marble.build_black_marble as bm

JSON_FLAGS = dict(ensure_ascii=False, indent=2)
COMPACT_FLAGS = dict(ensure_ascii=False, separators=(",", ":"))


def write_json(path: Path, payload: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    flags = COMPACT_FLAGS if compact else JSON_FLAGS
    tmp.write_text(json.dumps(payload, **flags) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_country(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if not features:
            raise RuntimeError(f"No feature found in {path}")
        feature = features[0]
    elif data.get("type") == "Feature":
        feature = data
    else:
        feature = {"type": "Feature", "properties": {}, "geometry": data}
    if not feature.get("geometry"):
        raise RuntimeError(f"No geometry found in {path}")
    return feature


def coord_iter(value: Any) -> Iterable[Tuple[float, float]]:
    if isinstance(value, list) and len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
        yield float(value[0]), float(value[1])
    elif isinstance(value, list):
        for item in value:
            yield from coord_iter(item)


def feature_bbox(feature: Dict[str, Any]) -> List[float]:
    coords = list(coord_iter(feature["geometry"]["coordinates"]))
    if not coords:
        raise RuntimeError("Country geometry has no coordinates")
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def class_defs() -> List[Dict[str, Any]]:
    out = []
    for _km2, _pct, lower, upper, class_id, label in bm.CLASS_DEFS:
        out.append({"id": class_id, "label": label, "min": lower, "max": upper})
    return out


def classify(value: float) -> Tuple[str, str]:
    for item in class_defs():
        upper = item["max"]
        if value >= item["min"] and (upper is None or value < upper):
            return item["id"], item["label"]
    return "no_data", "нема података"


def tile_grid(bbox: Sequence[float], step: float) -> List[Dict[str, Any]]:
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    cols = int(math.ceil((max_lon - min_lon) / step))
    rows = int(math.ceil((max_lat - min_lat) / step))
    tiles: List[Dict[str, Any]] = []
    for row in range(rows):
        south = min_lat + row * step
        north = min(max_lat, south + step)
        for col in range(cols):
            west = min_lon + col * step
            east = min(max_lon, west + step)
            tiles.append({
                "id": f"r{row:02d}-c{col:02d}",
                "row": row,
                "col": col,
                "bbox": [round(west, 6), round(south, 6), round(east, 6), round(north, 6)],
            })
    return tiles


def sample_compact(image: ee.Image, geometry: ee.Geometry, args: argparse.Namespace) -> List[List[float]]:
    sampled = image.select(bm.OUTPUT_RAD).sample(
        region=geometry,
        scale=args.scale,
        geometries=True,
        tileScale=args.tile_scale,
        dropNulls=True,
    )
    total = int(sampled.size().getInfo())
    if args.max_pixels_per_tile > 0 and total > args.max_pixels_per_tile:
        raise RuntimeError(
            f"Tile contains {total} pixels, above --max-pixels-per-tile={args.max_pixels_per_tile}. "
            "Use a smaller --grid-deg or raise the safety limit."
        )
    batch_size = max(1, min(int(args.pixel_batch_size), 4500))
    pixels: List[List[float]] = []
    for offset in range(0, total, batch_size):
        count = min(batch_size, total - offset)
        features = sampled.toList(count, offset).getInfo()
        if not isinstance(features, list):
            continue
        for feature in features:
            props = (feature or {}).get("properties") or {}
            coords = ((feature or {}).get("geometry") or {}).get("coordinates") or []
            value = bm.safe_float(props.get(bm.OUTPUT_RAD))
            if value is None or len(coords) < 2:
                continue
            pixels.append([
                round(float(coords[0]), 6),
                round(float(coords[1]), 6),
                round(float(value), args.value_digits),
            ])
    pixels.sort(key=lambda p: (-p[1], p[0]))
    return pixels


def compact_stats(pixels: Sequence[Sequence[float]]) -> Dict[str, Any]:
    vals = [float(p[2]) for p in pixels]
    if not vals:
        return {"count": 0, "min": None, "max": None, "mean": None, "classes": {}}
    counts = {item["id"]: 0 for item in class_defs()}
    for value in vals:
        class_id, _label = classify(value)
        counts[class_id] = counts.get(class_id, 0) + 1
    total = len(vals)
    return {
        "count": total,
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "mean": round(sum(vals) / total, 4),
        "classes": {
            key: {"count": count, "pct": round(count / total * 100, 3)}
            for key, count in counts.items()
        },
    }


def country_row(image: ee.Image, feature: Dict[str, Any], kind: str, period: str, args: argparse.Namespace) -> Dict[str, Any]:
    record = {
        "type": "Feature",
        "properties": {
            "pa_id": "serbia",
            "pa_order": 1,
            "name": "Република Србија",
            "name_lat": "Serbia",
            "pa_type": "државна територија",
            "rajac": False,
        },
        "geometry": feature["geometry"],
    }
    fc = bm.to_feature_collection([record], 0)
    period_kind = "annual" if kind == "year" else "monthly"
    rows = bm.compute_rows(image, fc, period_kind, period, args)
    return rows[0] if rows else {}


def image_for(kind: str, period: str, latest_complete: str, args: argparse.Namespace) -> Tuple[ee.Image, List[str]]:
    if kind == "month":
        return bm.monthly_image(period, args), [period]
    image, months = bm.annual_image(int(period), latest_complete, args)
    return image, months


def resolve_period(kind: str, period: str, latest_complete: str) -> str:
    if period != "auto":
        return period
    if kind == "month":
        return latest_complete
    # Stable baseline: latest fully completed calendar year.
    return str(int(latest_complete[:4]) - 1)


def update_root_index(root: Path, kind: str, period: str, period_index: Dict[str, Any]) -> None:
    path = root / "index.json"
    index = read_json(path, {}) or {}
    records = [r for r in (index.get("records") or []) if not (r.get("kind") == kind and str(r.get("period")) == period)]
    records.append({
        "kind": kind,
        "period": period,
        "label": period_index["meta"]["label"],
        "file": f"{'years' if kind == 'year' else 'months'}/{period}/index.json",
        "created_at": period_index["meta"]["created_at"],
        "tiles": len(period_index.get("tiles") or []),
        "pixels": period_index.get("stats", {}).get("pixel_count", 0),
    })
    records.sort(key=lambda r: (r["kind"], str(r["period"])), reverse=True)
    months = sorted([str(r["period"]) for r in records if r["kind"] == "month"])
    years = sorted([str(r["period"]) for r in records if r["kind"] == "year"])
    payload = {
        "ok": True,
        "generated_at": bm.utc_now(),
        "source": bm.SOURCE_LONG,
        "dataset": bm.DATASET_ID,
        "band": bm.BAND_RAD,
        "unit": bm.UNIT,
        "scale_m": period_index["meta"]["scale_m"],
        "grid_deg": period_index["meta"]["grid_deg"],
        "country": "Република Србија",
        "boundary": "public/boundaries/granica_srbije.geojson",
        "classes": class_defs(),
        "latest_month": months[-1] if months else None,
        "latest_year": years[-1] if years else None,
        "months": months,
        "years": years,
        "records": records,
        "note": "VIIRS/Black Marble radiance is satellite-observed upward nighttime radiance, not SQM sky brightness.",
    }
    write_json(path, payload)


def process(kind: str, period: str, latest_complete: str, country: Dict[str, Any], args: argparse.Namespace) -> None:
    image, months = image_for(kind, period, latest_complete, args)
    country_geometry = ee.Geometry(country["geometry"], None, False)
    bbox = feature_bbox(country)
    tiles = tile_grid(bbox, args.grid_deg)
    folder = "years" if kind == "year" else "months"
    period_root = Path(args.results_dir) / folder / period
    index_path = period_root / "index.json"
    if index_path.exists() and not args.overwrite:
        print(f"SKIP {kind} {period}: {index_path} already exists")
        return

    manifests: List[Dict[str, Any]] = []
    all_values: List[float] = []
    for position, tile in enumerate(tiles, start=1):
        west, south, east, north = tile["bbox"]
        tile_geometry = ee.Geometry.Rectangle([west, south, east, north], None, False)
        intersection = country_geometry.intersection(tile_geometry, ee.ErrorMargin(50))
        area_m2 = float(intersection.area(maxError=100).getInfo())
        if area_m2 <= 1:
            continue
        pixels = sample_compact(image, intersection, args)
        if not pixels:
            continue
        stats = compact_stats(pixels)
        all_values.extend(float(p[2]) for p in pixels)
        tile_payload = {
            "ok": True,
            "meta": {
                "kind": kind,
                "period": period,
                "source": bm.SOURCE_LONG,
                "dataset": bm.DATASET_ID,
                "band": bm.BAND_RAD,
                "unit": bm.UNIT,
                "scale_m": args.scale,
                "created_at": bm.utc_now(),
                "compact_pixel_format": ["lon", "lat", "radiance"],
            },
            "tile": tile,
            "stats": stats,
            "pixels": pixels,
        }
        tile_path = period_root / "tiles" / f"{tile['id']}.json"
        write_json(tile_path, tile_payload, compact=True)
        manifests.append({
            **tile,
            "file": f"tiles/{tile['id']}.json",
            "pixels": stats["count"],
            "mean": stats["mean"],
            "min": stats["min"],
            "max": stats["max"],
        })
        print(f"TILE {position}/{len(tiles)} {tile['id']}: {stats['count']} pixels")

    all_count = len(all_values)
    if not all_count:
        raise RuntimeError(f"No national pixels generated for {kind} {period}")

    country_stats = country_row(image, country, kind, period, args)
    period_payload = {
        "ok": True,
        "meta": {
            "kind": kind,
            "period": period,
            "label": f"{period}. (годишњи просек)" if kind == "year" else period,
            "source": bm.SOURCE_LONG,
            "dataset": bm.DATASET_ID,
            "band": bm.BAND_RAD,
            "unit": bm.UNIT,
            "scale_m": args.scale,
            "grid_deg": args.grid_deg,
            "created_at": bm.utc_now(),
            "months_in_composite": months,
            "interpretation": "Сателитска ноћна радијанса у nW/cm²/sr; није SQM mag/arcsec².",
        },
        "country": {"name": "Република Србија", "bbox": bbox},
        "classes": class_defs(),
        "stats": {
            "pixel_count": all_count,
            "sample_mean": round(sum(all_values) / all_count, 4),
            "sample_min": round(min(all_values), 4),
            "sample_max": round(max(all_values), 4),
            "earth_engine": country_stats,
        },
        "tiles": manifests,
    }
    write_json(index_path, period_payload)
    update_root_index(Path(args.results_dir), kind, period, period_payload)
    print(f"OK {kind} {period}: tiles={len(manifests)}, pixels={all_count}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact Serbia-wide VIIRS Black Marble tile JSON.")
    parser.add_argument("--period-kind", choices=["month", "year"], default="year")
    parser.add_argument("--period", default="auto", help="YYYY-MM, YYYY, or auto")
    parser.add_argument("--country-geojson", default="public/boundaries/granica_srbije.geojson")
    parser.add_argument("--results-dir", default="public/results/serbia")
    parser.add_argument("--grid-deg", type=float, default=0.4)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--project", default=os.environ.get("EE_PROJECT_ID", ""))
    parser.add_argument("--service-account", default=os.environ.get("EE_SERVICE_ACCOUNT", ""))
    parser.add_argument("--key-file", default="")
    parser.add_argument("--key-json-env", default="EE_SERVICE_ACCOUNT_KEY_JSON")
    parser.add_argument("--ee-deadline", type=int, default=300_000)
    parser.add_argument("--scale", type=float, default=500.0)
    parser.add_argument("--tile-scale", type=int, default=4)
    parser.add_argument("--min-source-days", type=int, default=15)
    parser.add_argument("--min-observations", type=int, default=3)
    parser.add_argument("--min-months-year", type=int, default=3)
    parser.add_argument("--min-cloud-mask-quality", type=int, choices=[0,1,2,3], default=2)
    parser.add_argument("--max-cloud-detection", type=int, choices=[0,1,2,3], default=1)
    parser.add_argument("--include-ephemeral", action="store_true")
    parser.add_argument("--max-pixels-per-tile", type=int, default=9000)
    parser.add_argument("--pixel-batch-size", type=int, default=4000)
    parser.add_argument("--value-digits", type=int, default=4)
    # Compatibility attributes used by imported build_black_marble helpers.
    parser.add_argument("--simplify-meters", type=float, default=0)
    parser.add_argument("--max-pixels-per-area", type=int, default=20000)
    parser.add_argument("--pause-seconds", type=float, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.grid_deg <= 0 or args.grid_deg > 1:
        raise RuntimeError("--grid-deg must be > 0 and <= 1 degree")
    bm.init_ee(args)
    latest_complete = bm.latest_complete_month()
    period = resolve_period(args.period_kind, args.period, latest_complete)
    if args.period_kind == "month" and bm.month_to_date(period) > bm.month_to_date(latest_complete):
        raise RuntimeError(f"Month {period} is not complete; latest publishable month is {latest_complete}")
    country = load_country(Path(args.country_geojson))
    process(args.period_kind, period, latest_complete, country, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
