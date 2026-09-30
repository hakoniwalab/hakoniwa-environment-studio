"""The one place Environment Studio reaches into hakoniwa-envsim.

Envsim is a sibling checkout ($HAKONIWA_ENVSIM_ROOT, else ../hakoniwa-envsim,
which the Workspace Recipe hakoniwa/recipes/citygml-parts.yaml materializes).
Its pipeline modules import each other by flat name, so its directories go on
sys.path once, here, and nowhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from env_diagnostics import fail

ROOT = Path(__file__).resolve().parents[1]


def root() -> Path:
    found = Path(os.environ.get("HAKONIWA_ENVSIM_ROOT") or ROOT.parent / "hakoniwa-envsim").resolve()
    if not (found / "src/city_pipeline/gml_lod1_extract.py").is_file():
        raise fail("envsim", "missing_field",
                   "hakoniwa-envsim is needed for CityGML (set HAKONIWA_ENVSIM_ROOT or clone it next to this repository)",
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
