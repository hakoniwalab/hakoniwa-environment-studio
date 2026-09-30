#!/usr/bin/env python3
"""What PLATEAU has in a selection, before building a City World (the map
page's PLATEAU mode).

It asks the public PLATEAU catalog through hakoniwa-envsim's client
(tools/plateau_citygml.py) for each feature the City World build uses and
reports, per feature, whether the selection has it and its highest LOD, the
municipalities, and the size of the CityGML a build would download. Nothing is
downloaded. The judgement follows the Business Pack City World Web UI's
inspection: a City World needs buildings, terrain (DEM), and roads; road
markings and bridges need LOD3.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_envsim  # noqa: E402

API_BASE_URL = "https://api.plateauview.mlit.go.jp"
# Component -> (PLATEAU feature type, lowest LOD used).
FEATURES = {
    "building": ("bldg", 1),
    "terrain": ("dem", 1),
    "road": ("tran", 1),
    "road_markings": ("frn", 1),
    "bridge": ("brid", 1),
}
REQUIRED = ("building", "terrain", "road")
LOD3_ONLY = ("road_markings", "bridge")


class InspectionError(RuntimeError):
    pass


def _capability(name: str, files: list[dict]) -> dict:
    if not files:
        return {"available": False, "usable": False, "max_lod": None, "files": 0,
                "reason": "PLATEAU has no data for it in this area"}
    max_lod = max(int(item.get("max_lod", 0)) for item in files)
    usable = not (name in LOD3_ONLY and max_lod < 3)
    return {"available": True, "usable": usable, "max_lod": max_lod, "files": len(files),
            "reason": None if usable else "the builder uses it only at LOD3"}


def inspect(center: tuple[float, float], half: tuple[float, float], client=None) -> dict:
    """The PLATEAU catalog's answer for a selection (centre lat/lon, half extents N-S, E-W in m)."""
    client = client or env_envsim.plateau_client()
    try:
        bbox = client.bounding_box(center[0], center[1], half[0], half[1])
        meshes = set(client.third_mesh_codes(bbox))
    except Exception as exc:  # noqa: BLE001 - envsim's PlateauError and bad selections
        raise InspectionError(f"the area cannot be asked about: {exc}") from exc

    def ask(component: str) -> list[dict]:
        feature_type, min_lod = FEATURES[component]
        mesh_level = 2 if feature_type == "brid" else 3
        payload = client.request_catalog(client.search_url(API_BASE_URL, feature_type, bbox, mesh_level=mesh_level),
                                         allow_not_found=True)  # 404: no file for it here
        files = client.select_files(payload, feature_type, "latest", allow_empty=True, min_lod=min_lod)
        if feature_type == "brid":  # a second-level mesh reaches beyond the area
            files = [item for item in files if str(item.get("code", ""))[:8] in meshes]
        return files

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(FEATURES)) as pool:
            futures = {component: pool.submit(ask, component) for component in FEATURES}
            selected = {component: futures[component].result() for component in FEATURES}
    except Exception as exc:  # noqa: BLE001 - network and catalog errors
        raise InspectionError(f"the PLATEAU catalog did not answer: {exc}") from exc

    capabilities = {name: _capability(name, files) for name, files in selected.items()}
    unique = {item["url"]: item for files in selected.values() for item in files}
    municipalities = sorted({(item["city_code"], item["city_name"], item["year"]) for item in unique.values()})
    missing = [name for name in REQUIRED if not capabilities[name]["available"]]
    return {
        "available": not missing,
        "missing": missing,
        # Buildings and roads but no DEM: a City World with flat ground is still possible.
        "flat_ground_possible": missing == ["terrain"],
        "capabilities": capabilities,
        "municipalities": [{"city_code": code, "city": name, "year": year} for code, name, year in municipalities],
        "download_bytes": sum(int(item.get("file_size", 0)) for item in unique.values()),
        "files": len(unique),
    }
