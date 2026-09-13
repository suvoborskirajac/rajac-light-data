#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a detailed national mask used by the Serbia-wide VIIRS product.

The website's national product covers the same territory as the existing PIO
Rajac Serbia maps.  geoBoundaries publishes detailed open geometries for SRB and
XKX separately; this helper combines them into one MultiPolygon mask without
adding a visible internal line.  The web map uses the geometry only as a clip
mask, while the Esri reference layer supplies the cartographic border drawing.

Source APIs:
  https://www.geoboundaries.org/api/current/gbOpen/SRB/ADM0/
  https://www.geoboundaries.org/api/current/gbOpen/XKX/ADM0/
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

API = {
    "SRB": "https://www.geoboundaries.org/api/current/gbOpen/SRB/ADM0/",
    "XKX": "https://www.geoboundaries.org/api/current/gbOpen/XKX/ADM0/",
}
USER_AGENT = "PIO-Rajac-VIIRS-Serbia/1.2"


def fetch_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def first_feature(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("type") == "FeatureCollection":
        features = payload.get("features") or []
        if not features:
            raise RuntimeError("Boundary FeatureCollection is empty")
        return features[0]
    if payload.get("type") == "Feature":
        return payload
    return {"type": "Feature", "properties": {}, "geometry": payload}


def polygons_from_geometry(geometry: Dict[str, Any]) -> List[Any]:
    typ = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if typ == "Polygon":
        return [coords]
    if typ == "MultiPolygon":
        return list(coords)
    raise RuntimeError(f"Unsupported boundary geometry: {typ}")


def count_vertices(value: Any) -> int:
    if isinstance(value, list) and len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
        return 1
    if isinstance(value, list):
        return sum(count_vertices(item) for item in value)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="public/boundaries/granica_srbije.geojson")
    args = parser.parse_args()

    polygons: List[Any] = []
    sources = []
    for iso, api_url in API.items():
        meta = fetch_json(api_url)
        # The official simplified geometry is still much denser than the old
        # 40-vertex Natural Earth mask, but remains light enough for Leaflet.
        geometry_url = meta.get("simplifiedGeometryGeoJSON") or meta.get("gjDownloadURL")
        if not geometry_url:
            raise RuntimeError(f"No GeoJSON URL in geoBoundaries metadata for {iso}")
        feature = first_feature(fetch_json(str(geometry_url)))
        geometry = feature.get("geometry") or {}
        polygons.extend(polygons_from_geometry(geometry))
        sources.append({
            "iso": iso,
            "boundary_id": meta.get("boundaryID"),
            "boundary_name": meta.get("boundaryName"),
            "year": meta.get("boundaryYearRepresented"),
            "source": meta.get("boundarySource"),
            "license": meta.get("boundaryLicense"),
            "api": api_url,
        })

    geometry = {"type": "MultiPolygon", "coordinates": polygons}
    vertices = count_vertices(polygons)
    if vertices < 500:
        raise RuntimeError(f"Boundary unexpectedly coarse: only {vertices} vertices")

    payload = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "name": "Република Србија",
                "purpose": "VIIRS national clip mask",
                "generated_by": "black_marble/build_serbia_boundary.py",
                "vertices": vertices,
                "sources": sources,
                "note": "Composite national clip mask used by the PIO Rajac VIIRS Serbia service.",
            },
            "geometry": geometry,
        }],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"OK boundary: {output} vertices={vertices} components={len(polygons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
