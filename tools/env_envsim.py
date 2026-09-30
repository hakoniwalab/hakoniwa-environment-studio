"""The one place Environment Studio reaches into hakoniwa-envsim.

Envsim is a source repository of the Studio's Business Pack Recipe
(recipes/business-pack/environment-studio.yaml, which configure materializes;
$HAKONIWA_ENVSIM_ROOT, else ../hakoniwa-envsim; tools/env_workspace.py).
Its pipeline modules import each other by flat name, so its directories go on
sys.path once, here, and nowhere else.
"""

from __future__ import annotations

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


def osm2citygml():
    """Envsim's OpenStreetMap / GeoJSON -> CityGML LOD1 converter."""
    pipeline()
    import osm2citygml as module

    return module


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
