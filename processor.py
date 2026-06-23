#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-area light pollution processor for GitHub Actions.

Creates static VIIRS JSON files for one or more protected areas / municipalities:
- public/catalog.json
- public/sites/<area-slug>/results/index.json
- public/sites/<area-slug>/results/YYYY-MM.json
- public/sites/<area-slug>/results/YYYY.json

Compatibility:
- for the area marked as "legacy_results": true, also mirrors results to:
  public/results/index.json and public/results/YYYY-MM.json
  so the existing PIO Rajac WordPress viewer keeps working without changes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import ee

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
AREAS_FILE = PUBLIC / "areas.json"
BOUNDARIES = PUBLIC / "boundaries"
LEGACY_RESULTS = PUBLIC / "results"
SITES_ROOT = PUBLIC / "sites"

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


@dataclass
class Area:
    slug: str
    name: str
    boundary_path: Path
    legacy_results: bool = False
    group: str = ""
    public_url: str = ""


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
    return [yearly_period(y) for y in range(end_year, start_year - 1, -1)]


def latest_candidate_periods(n: int, extra_back: int = 8) -> List[Period]:
    cursor = date.today().replace(day=1)
    periods: List[Period] = []
    for _ in range(max(1, n + extra_back)):
        start = previous_month_start(cursor)
        periods.append(period_from_month_start(start))
        cursor = start
    return periods


def current_year_candidate_periods(max_months: int = 12) -> List[Period]:
    """Calendar months from the current year, newest complete month first."""
    today = date.today()
    cursor = previous_month_start(today.replace(day=1))
    periods: List[Period] = []
    while cursor.year == today.year and len(periods) < max_months:
        periods.append(period_from_month_start(cursor))
        cursor = previous_month_start(cursor)
    return periods


def monthly_candidate_periods_from_year(start_year: int) -> List[Period]:
    """All complete monthly periods from January of start_year to the latest complete month, newest first."""
    today = date.today()
    if start_year < 2012:
        raise ValueError("--monthly-from не треба да буде пре 2012. године за VIIRS месечне композите.")
    if start_year > today.year:
        raise ValueError("--monthly-from не може бити у будућности.")
    cursor = previous_month_start(today.replace(day=1))
    stop = date(start_year, 1, 1)
    periods: List[Period] = []
    while cursor >= stop:
        periods.append(period_from_month_start(cursor))
        cursor = previous_month_start(cursor)
    return periods


def default_areas() -> List[Area]:
    return [
        Area(
            slug="pio-rajac",
            name="ПИО Рајац",
            boundary_path=BOUNDARIES / "pio-rajac.geojson",
            legacy_results=True,
            group="Заштићена подручја",
            public_url="https://piorajac.rs/monitoring-svetlosno-zagadjenje/",
        )
    ]


def load_areas() -> List[Area]:
    if not AREAS_FILE.exists():
        log("WARN: public/areas.json does not exist; using default PIO Rajac area only.")
        return default_areas()

    raw = load_json(AREAS_FILE)
    rows = raw.get("areas", raw if isinstance(raw, list) else [])
    areas: List[Area] = []
    for row in rows:
        if not row.get("enabled", True):
            continue
        slug = str(row.get("slug", "")).strip()
        name = str(row.get("name", slug)).strip()
        boundary = str(row.get("boundary", "")).strip()
        if not slug or not boundary:
            raise RuntimeError(f"Invalid area row in {AREAS_FILE}: missing slug or boundary")
        boundary_path = (ROOT / boundary).resolve() if not boundary.startswith("/") else Path(boundary)
        areas.append(
            Area(
                slug=slug,
                name=name,
                boundary_path=boundary_path,
                legacy_results=bool(row.get("legacy_results", False)),
                group=str(row.get("group", "")).strip(),
                public_url=str(row.get("public_url", "")).strip(),
            )
        )
    if not areas:
        raise RuntimeError(f"No enabled areas in {AREAS_FILE}")
    return areas


def geojson_to_ee_geometry(gj: Dict[str, Any]) -> ee.Geometry:
    if gj.get("type") == "FeatureCollection":
        geoms = [feat.get("geometry") for feat in gj.get("features", []) if feat.get("geometry")]
        if not geoms:
            raise ValueError("GeoJSON FeatureCollection нема геометрије.")
        if len(geoms) == 1:
            return ee.Geometry(geoms[0])
        polys: List[Any] = []
        for geom in geoms:
            if geom.get("type") == "Polygon":
                polys.append(geom["coordinates"])
            elif geom.get("type") == "MultiPolygon":
                polys.extend(geom["coordinates"])
            else:
                # Fallback for mixed geometry collections.
                return ee.FeatureCollection(gj).geometry()
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
    return select_available_periods_from_candidates(dataset, region, latest_candidate_periods(months, extra_back=10), months)


def select_current_year_available_periods(dataset: Dict[str, Any], region: ee.Geometry, months: int) -> List[Period]:
    candidates = current_year_candidate_periods(max_months=max(1, months))
    if not candidates:
        log("WARN: No complete month exists in the current calendar year yet; falling back to latest available months.")
        return select_available_periods(dataset, region, months)
    return select_available_periods_from_candidates(dataset, region, candidates, months)


def select_available_periods_from_year(dataset: Dict[str, Any], region: ee.Geometry, start_year: int) -> List[Period]:
    candidates = monthly_candidate_periods_from_year(start_year)
    if not candidates:
        raise RuntimeError(f"Нема комплетних месечних периода од {start_year}. године до данас.")
    # For a fixed archive start year we intentionally process the whole monthly archive,
    # not only the latest N months. This is needed for the public viewer to show all
    # monthly maps from 2024 onward, the same way Rajac has a historical monthly archive.
    return select_available_periods_from_candidates(dataset, region, candidates, len(candidates))


def select_available_periods_from_candidates(dataset: Dict[str, Any], region: ee.Geometry, candidates: List[Period], months: int) -> List[Period]:
    chosen: List[Period] = []
    for period in candidates:
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
    coll = image_collection_for_period(dataset, period, region)
    band = dataset["band"]
    if dataset["id"] == "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG":
        img = ee.Image(coll.first())
        radiance = img.select(band).rename(band)
        coverage = img.select(dataset["coverage_band"])
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
    coll = image_collection_for_period(dataset, period, region)
    band = dataset["band"]
    if dataset["id"] == "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG":
        cov_band = dataset["coverage_band"]

        def mask_month(img):
            return img.select(band).updateMask(img.select(cov_band).gte(1))

        return coll.map(mask_month).mean().clip(region).rename(band)
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
    pixels.sort(key=lambda p: (p["lat"], p["lon"]))
    return pixels


def compute_hotspots(pixels: List[Dict[str, Any]], top_n: int = 5) -> List[Dict[str, Any]]:
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


def build_result(dataset: Dict[str, Any], period: Period, region: ee.Geometry, area: Area, kind: str = "monthly") -> Dict[str, Any]:
    band = dataset["band"]
    scale_m = float(dataset["scale_m"])
    image = build_yearly_image(dataset, period, region) if kind == "yearly" else build_monthly_image(dataset, period, region)
    stats = reduce_stats(image, region, band, scale_m)
    pixels = sample_pixels(image, region, band, scale_m)
    if stats["count"] <= 0 or not pixels:
        raise RuntimeError(f"Period {period.id} nema validne piksele za područje {area.slug}.")
    return {
        "ok": True,
        "area": {
            "slug": area.slug,
            "name": area.name,
            "group": area.group,
            "public_url": area.public_url,
        },
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


def process_area(area: Area, dataset: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    if not area.boundary_path.exists():
        raise RuntimeError(f"Nedostaje granica za {area.slug}: {area.boundary_path}")

    boundary = load_json(area.boundary_path)
    region = geojson_to_ee_geometry(boundary)
    area_results = SITES_ROOT / area.slug / "results"
    area_results.mkdir(parents=True, exist_ok=True)

    log(f"=== AREA {area.slug}: {area.name} ===")
    if args.monthly_from:
        periods = select_available_periods_from_year(dataset, region, args.monthly_from)
    elif args.current_year:
        periods = select_current_year_available_periods(dataset, region, max(1, args.months))
    else:
        periods = select_available_periods(dataset, region, max(1, args.months))
    index_periods: List[Dict[str, Any]] = []

    for period in periods:
        log(f"Processing {area.slug} month {period.id} from {dataset['id']}...")
        result = build_result(dataset, period, region, area, kind="monthly")
        write_json(area_results / f"{period.id}.json", result)
        if area.legacy_results:
            write_json(LEGACY_RESULTS / f"{period.id}.json", result)
        index_periods.append({
            "id": period.id,
            "label": period.label,
            "source": dataset["label"],
            "dataset": dataset["id"],
            "kind": "monthly",
        })
        log(f"OK {area.slug} {period.id}: {len(result['pixels'])} pixels, mean={result['stats']['overall']['mean']}, max={result['stats']['overall']['max']}")

    index_yearly: List[Dict[str, Any]] = []
    if args.yearly:
        for year_period in yearly_candidate_periods(args.yearly_from, args.yearly_to):
            if not has_images(dataset, year_period, region):
                log(f"Skipped {area.slug} year {year_period.id} (no images in collection)")
                continue
            log(f"Processing {area.slug} year {year_period.id} from {dataset['id']}...")
            try:
                result = build_result(dataset, year_period, region, area, kind="yearly")
            except RuntimeError as exc:
                log(f"WARN {area.slug} year {year_period.id}: {exc}")
                continue
            write_json(area_results / f"{year_period.id}.json", result)
            if area.legacy_results:
                write_json(LEGACY_RESULTS / f"{year_period.id}.json", result)
            index_yearly.append({
                "id": year_period.id,
                "label": year_period.label,
                "source": dataset["label"],
                "dataset": dataset["id"],
                "kind": "yearly",
            })
            log(f"OK {area.slug} year {year_period.id}: {len(result['pixels'])} pixels, mean={result['stats']['overall']['mean']}, max={result['stats']['overall']['max']}")

    index = {
        "ok": True,
        "area": {
            "slug": area.slug,
            "name": area.name,
            "group": area.group,
            "public_url": area.public_url,
        },
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
        "note": "Generated by GitHub Actions multi-area processor. These are real satellite-derived VIIRS nighttime radiance JSON results.",
    }
    write_json(area_results / "index.json", index)
    if area.legacy_results:
        write_json(LEGACY_RESULTS / "index.json", index)
    return index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12, help="Number of latest available complete months to process.")
    parser.add_argument("--dataset", choices=sorted(DATASETS.keys()), default="monthly-vcmsl")
    parser.add_argument("--yearly", action="store_true", help="Also generate yearly averaged composites.")
    parser.add_argument("--yearly-from", type=int, default=2013, help="Earliest year to include in yearly composites.")
    parser.add_argument("--yearly-to", type=int, default=date.today().year - 1, help="Latest year to include, defaults to last complete calendar year.")
    parser.add_argument("--area", default="", help="Optional single area slug. Empty means all enabled areas.")
    parser.add_argument("--monthly-from", type=int, default=0, help="Earliest year for monthly archive. Example: --monthly-from 2024 generates every available month from 2024-01 onward.")
    parser.add_argument("--current-year", action="store_true", help="Generate monthly periods only for the current calendar year, newest available month first. Ignored when --monthly-from is used.")
    args = parser.parse_args()

    dataset = DATASETS[args.dataset]
    init_ee()

    areas = load_areas()
    if args.area:
        areas = [a for a in areas if a.slug == args.area]
        if not areas:
            raise RuntimeError(f"Area slug not found or not enabled: {args.area}")

    catalog_areas: List[Dict[str, Any]] = []
    for area in areas:
        index = process_area(area, dataset, args)
        catalog_areas.append({
            "slug": area.slug,
            "name": area.name,
            "group": area.group,
            "public_url": area.public_url,
            "results": f"public/sites/{area.slug}/results/index.json",
            "latest": index.get("latest"),
            "periods_count": index.get("coverage", {}).get("count"),
            "yearly_count": index.get("coverage", {}).get("yearlyCount"),
        })

    catalog = {
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dataset": dataset["id"],
        "unit": dataset["unit"],
        "areas": catalog_areas,
    }
    write_json(PUBLIC / "catalog.json", catalog)
    log(f"DONE: processed {len(catalog_areas)} area(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        raise

