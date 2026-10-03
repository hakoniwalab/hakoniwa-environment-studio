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
# The lowest LOD the generator reads: road markings exist only at LOD3;
# bridges are drawn from LOD3 where a bridge has it, else from LOD2 (envsim
# bridge2glb/bridge2mjcf), as many cities publish them at LOD2 only.
MIN_LOD = {"road_markings": 3, "bridge": 2}


class InspectionError(RuntimeError):
    pass


def _capability(name: str, files: list[dict]) -> dict:
    if not files:
        return {"dataset_status": "not_available", "generation_status": "scoped_out", "max_lod": None,
                "source_file_count": 0, "reason": "dataset is not available in the selected bbox"}
    max_lod = max(int(item.get("max_lod", 0)) for item in files)
    needed = MIN_LOD.get(name, 1)
    candidate = max_lod >= needed
    return {"dataset_status": "available", "generation_status": "candidate" if candidate else "scoped_out",
            "max_lod": max_lod, "source_file_count": len(files),
            "reason": None if candidate else f"LOD{needed} geometry required by the current generator is not available"}


def inspect(center: tuple[float, float], half: tuple[float, float], client=None) -> dict:
    """The PLATEAU catalog's answer for a selection (centre lat/lon, half
    extents N-S, E-W in m), in the City World Web UI's inspection form
    (Business Pack tools/remote_operation/city_world/inspection.py): status,
    the query meshes, capabilities, municipalities, files and bytes. Besides,
    `missing` (the required components not there) and `flat_ground_possible`
    (only the DEM is missing: a flat ground makes it)."""
    client = client or env_envsim.plateau_client()
    try:
        bbox = client.bounding_box(center[0], center[1], half[0], half[1])
        query_meshes = []
        for code in client.third_mesh_codes(bbox):
            west, south, east, north = client.third_mesh_bounds(code)
            query_meshes.append({"code": code, "bbox": {"west": west, "south": south, "east": east, "north": north}})
    except Exception as exc:  # noqa: BLE001 - envsim's PlateauError and bad selections
        raise InspectionError(f"the area cannot be asked about: {exc}") from exc
    meshes = {item["code"] for item in query_meshes}

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
    cities = {item["city_code"]: {"city_code": item["city_code"], "city": item["city_name"], "year": item["year"],
                                  "spec": item.get("spec")} for item in unique.values()}
    missing = [name for name in REQUIRED if capabilities[name]["dataset_status"] != "available"]
    # Where the buildings are (terrain and road files reach beyond the area): the City World's name.
    building_cities = list(dict.fromkeys(item["city_name"] for item in selected["building"]))
    return {
        "status": "unavailable" if missing else "available",
        "reason": "required PLATEAU components are unavailable: " + ", ".join(missing) if missing else None,
        "missing": missing,
        "flat_ground_possible": missing == ["terrain"],
        "bbox": {"west": bbox[0], "south": bbox[1], "east": bbox[2], "north": bbox[3]},
        "query_meshes": query_meshes,
        "capabilities": capabilities,
        "municipalities": [cities[code] for code in sorted(cities)],
        "building_municipalities": building_cities,
        "source_file_count": len(unique),
        "estimated_download_bytes": sum(int(item.get("file_size", 0)) for item in unique.values()),
    }
