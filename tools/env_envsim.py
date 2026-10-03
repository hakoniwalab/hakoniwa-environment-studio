"""The one place Environment Studio reaches into hakoniwa-envsim.

Envsim is a source repository of the Studio's Business Pack Recipe
(recipes/business-pack/environment-studio.yaml, which configure materializes;
$HAKONIWA_ENVSIM_ROOT, else ../hakoniwa-envsim; tools/env_workspace.py).
Its pipeline modules import each other by flat name, so its directories go on
sys.path once, here, and nowhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import env_workspace
from env_diagnostics import fail


def root() -> Path:
    found = env_workspace.dependency_root("hakoniwa-envsim")
    if not (found / "src/city_pipeline/gml_lod1_extract.py").is_file():
        raise fail("envsim", "missing_field",
                   f"hakoniwa-envsim is needed for CityGML: in the Business Pack Workspace, {env_workspace.CONFIGURE}",
                   expected=str(found))
    return found


_PLATEAU_CLIENT = None


def plateau_client():
    """Envsim's PLATEAU catalog client (tools/plateau_citygml.py), loaded once."""
    global _PLATEAU_CLIENT
    if _PLATEAU_CLIENT is None:
        import importlib.util

        path = root() / "tools" / "plateau_citygml.py"
        spec = importlib.util.spec_from_file_location("hakoniwa_envsim_plateau_citygml", path)
        if spec is None or spec.loader is None:
            raise fail("envsim", "missing_field", "hakoniwa-envsim's PLATEAU client cannot be loaded", expected=str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PLATEAU_CLIENT = module
    return _PLATEAU_CLIENT


def _on_path(directory: Path) -> None:
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def pipeline():
    """(geodesy, gml_lod1_extract, road_terrain_probe): Envsim's CityGML readers."""
    _on_path(root() / "src/city_pipeline")
    import geodesy
    import gml_lod1_extract
    import road_terrain_probe

    return geodesy, gml_lod1_extract, road_terrain_probe


def passages():
    """Envsim's convex clipper (building_road_passages.py): what a くり抜き part cuts with."""
    _on_path(root() / "src/city_pipeline")
    import building_road_passages

    return building_road_passages


def osm2citygml():
    """Envsim's OpenStreetMap / GeoJSON -> CityGML LOD1 converter."""
    pipeline()
    import osm2citygml as module

    return module


# Public Overpass API instances (wiki.openstreetmap.org/wiki/Overpass_API), tried
# in turn: the main one often answers 504 or 429 when it is busy.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


def fetch_overpass(box) -> dict:
    """The box's buildings and roads from Overpass, by Envsim's osm2citygml:
    HAKONIWA_OVERPASS_URL alone when it is set, else the public instances in
    turn until one answers. Raises osm2citygml's OsmConversionError naming
    what each said."""
    osm = osm2citygml()
    configured = os.environ.get("HAKONIWA_OVERPASS_URL")
    errors = []
    for endpoint in (configured,) if configured else OVERPASS_ENDPOINTS:
        try:
            return osm.fetch_overpass(box, endpoint)
        except osm.OsmConversionError as exc:
            errors.append(str(exc))
    raise osm.OsmConversionError("; ".join(errors))


def bounding_box(center: tuple[float, float], half_extent: tuple[float, float]) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a selection, as Envsim (and the PLATEAU
    City World browser) computes it from its centre and half extents."""
    _on_path(root() / "tools")
    import plateau_citygml

    west, south, east, north = plateau_citygml.bounding_box(center[0], center[1], half_extent[0], half_extent[1])
    return south, west, north, east


def glb_helpers():
    """Envsim's CityGML-to-GLB helpers (appearance map, ring triangulation)."""
    pipeline()
    import citygml2glb

    return citygml2glb
