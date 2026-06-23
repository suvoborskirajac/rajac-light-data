#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Serious statistics processor for multi-area VIIRS light-pollution monitoring.

Reads existing public/sites/<slug>/results/*.json files produced by processor.py and writes:
- public/sites/<slug>/statistics/summary.json
- public/sites/<slug>/statistics/observatory.json  (when AOB/AOV point exists)
- public/statistics/catalog.json

It does not call Earth Engine. It works only from already generated JSON results,
so it can safely run after the main VIIRS processor in GitHub Actions.
"""
from __future__ import annotations

import json, math, statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
SITES = PUBLIC / "sites"
STATS_ROOT = PUBLIC / "statistics"

OBSERVATORIES = {
    "zvezdara": {
        "code": "AOB",
        "name": "Астрономска опсерваторија Београд",
        "lat": 44.802153,
        "lon": 20.513504,
    },
    "prokuplje": {
        "code": "AOV",
        "name": "Астрономска опсерваторија Видојевица",
        "lat": 43.141275,
        "lon": 21.555798,
    },
}

RINGS_KM = [1, 3, 5, 10, 20]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)


def fnum(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def rnd(v: Any, nd: int = 4) -> Optional[float]:
    x = fnum(v)
    return None if x is None else round(x, nd)


def percentile(values: List[float], p: float) -> Optional[float]:
    vals = sorted(x for x in values if math.isfinite(x))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100.0
    lo = math.floor(k); hi = math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def stats_from_values(values: List[float]) -> Dict[str, Any]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None, "p75": None, "p90": None, "p95": None, "std": None, "sum": None}
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals) if len(vals) > 1 else 0.0
    return {
        "count": len(vals),
        "min": rnd(min(vals)),
        "max": rnd(max(vals)),
        "mean": rnd(mean),
        "median": rnd(statistics.median(vals)),
        "p75": rnd(percentile(vals, 75)),
        "p90": rnd(percentile(vals, 90)),
        "p95": rnd(percentile(vals, 95)),
        "std": rnd(math.sqrt(var)),
        "sum": rnd(sum(vals)),
    }


def linear_regression(years: List[int], vals: List[float]) -> Dict[str, Any]:
    pairs = [(float(x), float(y)) for x, y in zip(years, vals) if math.isfinite(float(y))]
    n = len(pairs)
    if n < 2:
        return {"ok": False, "n": n, "reason": "Недовољно година за тренд."}
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    xbar = sum(xs) / n; ybar = sum(ys) / n
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx == 0:
        return {"ok": False, "n": n, "reason": "Све године су исте."}
    slope = sum((x - xbar) * (y - ybar) for x, y in pairs) / sxx
    intercept = ybar - slope * xbar
    pred = [intercept + slope * x for x in xs]
    ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, pred))
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    base = ys[0] if ys[0] != 0 else ybar
    pct_year = (slope / base * 100) if base else None
    return {"ok": True, "n": n, "slope_abs_per_year": rnd(slope, 6), "slope_pct_per_year": rnd(pct_year, 4), "intercept": rnd(intercept, 6), "r2": rnd(r2, 4), "first_year": int(xs[0]), "last_year": int(xs[-1])}


def sen_slope(years: List[int], vals: List[float]) -> Optional[float]:
    pairs = [(int(x), float(y)) for x, y in zip(years, vals) if math.isfinite(float(y))]
    slopes = []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            dx = pairs[j][0] - pairs[i][0]
            if dx:
                slopes.append((pairs[j][1] - pairs[i][1]) / dx)
    return statistics.median(slopes) if slopes else None


def mann_kendall(years: List[int], vals: List[float]) -> Dict[str, Any]:
    ys = [float(y) for _, y in sorted(zip(years, vals)) if math.isfinite(float(y))]
    n = len(ys)
    if n < 4:
        return {"ok": False, "n": n, "reason": "За Mann–Kendall је пожељно најмање 4 вредности."}
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            if ys[j] > ys[i]: s += 1
            elif ys[j] < ys[i]: s -= 1
    # tie correction
    counts = {}
    for y in ys:
        counts[y] = counts.get(y, 0) + 1
    var_s = n * (n - 1) * (2 * n + 5)
    var_s -= sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)
    var_s /= 18.0
    if var_s <= 0:
        return {"ok": False, "n": n, "s": s, "reason": "Нулта варијанса."}
    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    # two-sided p using normal approximation
    p = math.erfc(abs(z) / math.sqrt(2))
    tau = s / (0.5 * n * (n - 1))
    trend = "растући" if z > 0 else "опадајући" if z < 0 else "без смера"
    significant = p < 0.05
    return {"ok": True, "n": n, "s": s, "z": rnd(z, 4), "p_value": rnd(p, 5), "kendall_tau": rnd(tau, 4), "trend": trend, "significant_0_05": significant}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


def class_distribution(values: List[float], scale_min: float, scale_max: float) -> List[Dict[str, Any]]:
    labels = ["врло ниско", "ниско", "умерено", "повишено", "високо"]
    bins = [0, 0, 0, 0, 0]
    span = max(1e-9, scale_max - scale_min)
    vals = [v for v in values if math.isfinite(v)]
    for v in vals:
        i = int(max(0, min(4, math.floor(((v - scale_min) / span) * 5))))
        bins[i] += 1
    total = len(vals)
    out = []
    for i, c in enumerate(bins):
        lo = scale_min + span * i / 5
        hi = scale_min + span * (i + 1) / 5
        out.append({"class": labels[i], "from": rnd(lo), "to": rnd(hi), "count": c, "pct": rnd(c / total * 100 if total else 0, 2)})
    return out


def load_result(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = read_json(path)
        if not data.get("ok"):
            return None
        return data
    except Exception as exc:
        print(f"WARN cannot read {path}: {exc}", flush=True)
        return None


def period_record(result: Dict[str, Any], rel_path: str) -> Dict[str, Any]:
    meta = result.get("meta", {})
    overall = (result.get("stats", {}) or {}).get("overall", {}) or {}
    pixels = result.get("pixels") or []
    values = [float(p.get("value")) for p in pixels if fnum(p.get("value")) is not None]
    if values:
        px_stats = stats_from_values(values)
    else:
        px_stats = {}
    st = dict(overall)
    for k in ["p75", "p95", "std"]:
        if k not in st or st.get(k) is None:
            st[k] = px_stats.get(k)
    return {
        "id": meta.get("id"),
        "label": meta.get("label"),
        "kind": meta.get("kind"),
        "date_start": meta.get("date_start"),
        "date_end": meta.get("date_end"),
        "unit": meta.get("unit", "nW/cm²/sr"),
        "json": rel_path,
        "stats": st,
        "pixel_count": len(pixels),
        "has_pixels": bool(pixels),
        "distribution": None,
    }


def process_area(slug: str) -> Dict[str, Any]:
    results_dir = SITES / slug / "results"
    index_path = results_dir / "index.json"
    index = read_json(index_path)
    area = index.get("area", {"slug": slug, "name": slug})
    monthly_records = []
    yearly_records = []
    all_values_for_scale = []
    period_results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    for row in (index.get("periods") or []) + (index.get("yearlyPeriods") or []):
        pid = row.get("id")
        if not pid: continue
        path = results_dir / f"{pid}.json"
        result = load_result(path)
        if not result: continue
        rel = str(path.relative_to(PUBLIC)).replace("\\", "/")
        rec = period_record(result, "public/" + rel)
        vals = [float(p.get("value")) for p in (result.get("pixels") or []) if fnum(p.get("value")) is not None]
        # Use robust p95 for cartographic area-scale, but keep absolute min/max too.
        all_values_for_scale.extend(vals)
        period_results.append((rec, result))
        if rec["kind"] == "yearly": yearly_records.append(rec)
        else: monthly_records.append(rec)

    if all_values_for_scale:
        abs_min = min(all_values_for_scale); abs_max = max(all_values_for_scale)
        robust_min = max(0.0, percentile(all_values_for_scale, 1) or abs_min)
        robust_max = percentile(all_values_for_scale, 99) or abs_max
        if robust_max <= robust_min:
            robust_min, robust_max = abs_min, abs_max
    else:
        abs_min = 0.0; abs_max = 1.0; robust_min = 0.0; robust_max = 1.0

    scale = {
        "absolute_min": rnd(abs_min), "absolute_max": rnd(abs_max),
        "visual_min": rnd(robust_min), "visual_max": rnd(robust_max),
        "method": "p01–p99 из свих пиксела свих периода изабраног подручја; екстреми се не губе у статистици",
    }
    for rec, result in period_results:
        vals = [float(p.get("value")) for p in (result.get("pixels") or []) if fnum(p.get("value")) is not None]
        rec["distribution"] = class_distribution(vals, float(scale["visual_min"]), float(scale["visual_max"]))

    # Trends from yearly means, sorted ascending.
    yearly_sorted = sorted([r for r in yearly_records if str(r.get("id", "")).isdigit()], key=lambda r: int(r["id"]))
    years = [int(r["id"]) for r in yearly_sorted]
    means = [float(r["stats"].get("mean")) for r in yearly_sorted if fnum(r["stats"].get("mean")) is not None]
    years_for_means = [int(r["id"]) for r in yearly_sorted if fnum(r["stats"].get("mean")) is not None]
    trend_mean = linear_regression(years_for_means, means)
    sen = sen_slope(years_for_means, means)
    mk = mann_kendall(years_for_means, means)
    forecast = []
    if trend_mean.get("ok"):
        last = max(years_for_means)
        slope = float(trend_mean["slope_abs_per_year"])
        intercept = float(trend_mean["intercept"])
        sen_s = sen if sen is not None else slope
        base_last = next((float(r["stats"].get("mean")) for r in yearly_sorted if int(r["id"]) == last), None)
        for y in range(last + 1, last + 6):
            forecast.append({
                "year": y,
                "linear_mean": rnd(intercept + slope * y),
                "sen_mean": rnd((base_last or 0) + sen_s * (y - last)),
                "note": "Пројекција није мерење; важи само ако се досадашњи тренд настави."
            })

    # Monthly seasonal baselines.
    monthly_by_month: Dict[str, List[float]] = {}
    for r in monthly_records:
        pid = r.get("id") or ""
        if len(pid) == 7 and fnum(r["stats"].get("mean")) is not None:
            monthly_by_month.setdefault(pid[5:7], []).append(float(r["stats"].get("mean")))
    seasonal = {m: stats_from_values(vals) for m, vals in sorted(monthly_by_month.items())}

    summary = {
        "ok": True,
        "created_at": now_iso(),
        "area": area,
        "unit": "nW/cm²/sr",
        "scale": scale,
        "coverage": {
            "monthly_count": len(monthly_records),
            "yearly_count": len(yearly_records),
            "monthly_first": monthly_records[-1]["id"] if monthly_records else None,
            "monthly_last": monthly_records[0]["id"] if monthly_records else None,
            "yearly_first": yearly_sorted[0]["id"] if yearly_sorted else None,
            "yearly_last": yearly_sorted[-1]["id"] if yearly_sorted else None,
        },
        "monthly": monthly_records,
        "yearly": yearly_sorted,
        "trend": {
            "mean_linear": trend_mean,
            "mean_sen_slope_abs_per_year": rnd(sen, 6),
            "mann_kendall_mean": mk,
            "forecast_next_5_years": forecast,
        },
        "seasonality": seasonal,
        "limits": [
            "VIIRS радијанса није SQM вредност у mag/arcsec².",
            "Јавна топлотна мапа је интерполисана визуелизација; статистика се рачуна из сирових пиксела.",
            "Месечни просеци се не смеју мешати са годишњим трендом; за тренд користити годишње просеке.",
        ]
    }
    write_json(SITES / slug / "statistics" / "summary.json", summary)

    # Observatory buffers.
    obs = OBSERVATORIES.get(slug)
    if obs:
        buffer_rows = []
        for rec, result in period_results:
            pixels = result.get("pixels") or []
            rings = []
            prev = 0.0
            for rkm in RINGS_KM:
                vals = []
                for p in pixels:
                    lat = fnum(p.get("lat")); lon = fnum(p.get("lon")); val = fnum(p.get("value"))
                    if lat is None or lon is None or val is None: continue
                    d = haversine_km(obs["lat"], obs["lon"], lat, lon)
                    if prev < d <= rkm:
                        vals.append(val)
                rings.append({"ring_km": f"{prev:g}–{rkm:g}", "stats": stats_from_values(vals)})
                prev = float(rkm)
            buffer_rows.append({"id": rec["id"], "label": rec["label"], "kind": rec["kind"], "rings": rings})
        obs_out = {"ok": True, "created_at": now_iso(), "area": area, "observatory": obs, "rings_km": RINGS_KM, "periods": buffer_rows}
        write_json(SITES / slug / "statistics" / "observatory.json", obs_out)

    return {"slug": slug, "name": area.get("name", slug), "statistics": f"public/sites/{slug}/statistics/summary.json", "observatory": bool(obs)}


def main() -> int:
    if not SITES.exists():
        raise RuntimeError("public/sites does not exist. Run processor.py first.")
    catalog = []
    for site in sorted(p for p in SITES.iterdir() if p.is_dir()):
        if (site / "results" / "index.json").exists():
            print(f"Statistics for {site.name}", flush=True)
            catalog.append(process_area(site.name))
    write_json(STATS_ROOT / "catalog.json", {"ok": True, "created_at": now_iso(), "areas": catalog})
    print(f"DONE statistics for {len(catalog)} areas.", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
