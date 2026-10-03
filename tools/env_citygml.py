#!/usr/bin/env python3
"""Turn CityGML into editable Environment Studio parts (the parts converter).

CityGML is the shared intermediate representation for city data: PLATEAU
delivers it, and hakoniwa-envsim's osm2citygml.py makes it from OpenStreetMap
or GeoJSON. This tool reads it with Envsim's own extractors and makes a
Recipe of parts (docs/citygml-parts.md):

* one building-footprint object per bldg:Building (its gml:id; the LOD1
  footprint and heights), whole: a building is never cut, the environment
  grows to hold every building the selection takes (Envsim's rule: its
  footprint's centroid lies in the selection);
* one road-area object per tran:Road LOD1 surface, clipped to the selection.

From an Envsim build (a City World), what Envsim made is passed through
unchanged, so importing and generating again loses nothing: its terrain
(hfield and GLB), each building's colliders (its P0-P3 geoms, by gml:id) and
its layers (the road network, road markings, bridges) as city-layer objects.
Each carries an anchor: where it was made, so it stays exactly there until
it is moved (docs/citygml-parts.md).

Each part keeps where it came from (gml:id, source file, OSM tags when the
CityGML came from OpenStreetMap) in its `source`, so later stages can attach
the same building's LOD2 geometry and move it with the part.

    env_citygml.py --citygml DIR --center LAT,LON --half-extent NS,EW --out x.yaml

Envsim is a source repository of the Studio's Business Pack Recipe
(recipes/business-pack/environment-studio.yaml; tools/env_envsim.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import env_envsim  # noqa: E402
import env_polygon  # noqa: E402
import env_rules  # noqa: E402
import env_schema  # noqa: E402
from env_diagnostics import DiagnosticError, fail  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "catalogs/starter/catalog.yaml"
CONVERTER_VERSION = "1"
ITEMS = {"building": "building-footprint", "road": "road-area", "layer": "city-layer"}
# The plan colour of a City World layer (city-layer's default is the roads'
# asphalt): bridges and markings drawn over the roads stay in sight.
LAYER_COLORS = {"bridges": "#b8bcc4", "markings": "#e6e1cc"}
# Footprint points closer than this are merged (as osm2citygml): no other point
# moves, so walls neighbours share stay shared.
MIN_STEP_M = 0.001
# Road surfaces (the surface layer, where overlaps do not matter) are simplified to this.
ROAD_SIMPLIFY_M = 0.025
# A road surface keeps at most this many corners (simplified further when it has more).
MAX_ROAD_POINTS = 400
MIN_ROAD_AREA_M2 = 1.0
# On DEM terrain a road surface is cut into tiles this big: a part stands on the
# highest ground under it, so a long road on a slope would float otherwise.
ROAD_TILE_M = 10.0
MIN_TILE_AREA_M2 = 0.05
GML_ID = "{http://www.opengis.net/gml}id"
NS = {"gml": "http://www.opengis.net/gml", "bldg": "http://www.opengis.net/citygml/building/2.0",
      "gen": "http://www.opengis.net/citygml/generics/2.0"}


# Envsim access lives in env_envsim.py; these names stay for callers of this module.
envsim_root = env_envsim.root
envsim_modules = env_envsim.pipeline
osm2citygml = env_envsim.osm2citygml
bounding_box = env_envsim.bounding_box


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(source: Path, pattern: str) -> list[Path]:
    """The CityGML files of one kind, each content once (an Envsim build keeps
    copies of its sources under build/source/)."""
    if source.is_file():
        paths = [source] if re.fullmatch(pattern.replace("*", ".*"), source.name) else []
    elif source.is_dir():
        paths = sorted(source.rglob(pattern), key=lambda path: (len(path.parts), str(path)))
    else:
        raise fail("citygml", "missing_field", "no such CityGML file or directory", actual=str(source))
    unique, seen = [], set()
    for path in paths:
        digest = _sha256(path)
        if digest not in seen:
            seen.add(digest)
            unique.append(path)
    return unique


def _part_id(gml_id: str, used: set[str]) -> str:
    """A Studio object id (lower case, digits, - and _) for a gml:id."""
    base = re.sub(r"[^a-z0-9_-]", "-", gml_id.lower()).strip("-_") or "part"
    if not base[0].isalnum():
        base = f"p{base}"
    if len(base) > 64:
        base = f"{base[:55]}-{hashlib.sha1(gml_id.encode()).hexdigest()[:8]}"
    candidate, number = base, 2
    while candidate in used:
        suffix = f"-{number}"
        candidate, number = base[:64 - len(suffix)] + suffix, number + 1
    used.add(candidate)
    return candidate


def _mm(value: float) -> float:
    return round(value, 3) + 0.0


def _placed(points) -> tuple[dict, list[list[float]]]:
    """A pose at the middle of the points' box and the points about it."""
    xs, ys = [x for x, _ in points], [y for _, y in points]
    cx, cy = _mm((min(xs) + max(xs)) / 2), _mm((min(ys) + max(ys)) / 2)
    return {"x_m": cx, "y_m": cy, "yaw_deg": 0}, [[_mm(x - cx), _mm(y - cy)] for x, y in points]


# Building attributes kept as tags (PLATEAU and CityGML in general).
BUILDING_FIELDS = ("measuredHeight", "storeysAboveGround", "usage", "class", "yearOfConstruction")


def _attributes(path: Path, ids: set[str] | None = None) -> dict[str, dict]:
    """gml:id -> {name, generic attributes, building fields} of the
    bldg:Building elements in a file (all, or those in `ids`). Streamed: a
    PLATEAU mesh file can be hundreds of megabytes."""
    found = {}
    building_tag = f"{{{NS['bldg']}}}Building"
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag != building_tag:
            continue
        gml_id = element.get(GML_ID)
        if ids is None or gml_id in ids:
            generic = {item.get("name"): item.findtext("gen:value", namespaces=NS)
                       for item in element.findall("gen:stringAttribute", NS)}
            fields = {name: element.findtext(f"bldg:{name}", namespaces=NS) for name in BUILDING_FIELDS}
            found[gml_id] = {"name": element.findtext("gml:name", namespaces=NS), "gen": generic,
                             "fields": {key: value.strip() for key, value in fields.items() if value and value.strip()}}
        element.clear()
    return found


def _clean_ring(points) -> list[tuple[float, float]] | None:
    """A simple counter-clockwise ring of the points at mm precision, or None.
    Rounded before the checks: rounding afterwards could make a thin sliver
    cross itself, and the Recipe would then refuse the whole city."""
    ring = env_polygon.cleaned([(_mm(x), _mm(y)) for x, y in points], True, MIN_STEP_M)
    if len(ring) < 3 or abs(env_polygon.signed_area(ring)) < 1e-6:
        return None
    ring = env_polygon.counter_clockwise(ring)
    return ring if env_polygon.is_simple(ring) else None


MIN_HOLE_AREA_M2 = 1.0  # smaller courtyards (light wells) are filled


def _holes(rings, outer, pose) -> tuple[list[list[list[float]]], int]:
    """(holes relative to the pose, how many were filled) of a footprint's
    interior rings: each kept when, at mm precision, it is simple, at least
    MIN_HOLE_AREA_M2 and lies inside the outline apart from the others kept."""
    kept, filled = [], 0
    for points in rings:
        ring = _clean_ring(points)
        if ring is None or abs(env_polygon.signed_area(ring)) < MIN_HOLE_AREA_M2 \
                or env_polygon.holes_problem(outer, [*kept, ring]):
            filled += 1
            continue
        kept.append(ring)
    return [[[_mm(x - pose["x_m"]), _mm(y - pose["y_m"])] for x, y in ring] for ring in kept], filled


def _road_pieces(polygon, tiled: bool):
    """(id suffix, polygon) of a road surface: itself, or its ROAD_TILE_M tiles
    (aligned to the origin) when it lies on DEM terrain."""
    if not tiled:
        return [("", polygon)]
    from shapely.geometry import box

    min_x, min_y, max_x, max_y = polygon.bounds
    pieces = []
    for ix in range(math.floor(min_x / ROAD_TILE_M), math.ceil(max_x / ROAD_TILE_M)):
        for iy in range(math.floor(min_y / ROAD_TILE_M), math.ceil(max_y / ROAD_TILE_M)):
            cut = polygon.intersection(box(ix * ROAD_TILE_M, iy * ROAD_TILE_M, (ix + 1) * ROAD_TILE_M,
                                           (iy + 1) * ROAD_TILE_M))
            parts = [cut] if cut.geom_type == "Polygon" else [g for g in getattr(cut, "geoms", []) if g.geom_type == "Polygon"]
            for number, part in enumerate(parts):
                if part.area >= MIN_TILE_AREA_M2:
                    pieces.append((f"-t{ix}_{iy}" + (f"_{number}" if len(parts) > 1 else ""), part))
    return pieces


def convert(source: Path, center: tuple[float, float], half_extent: tuple[float, float], *,
            catalog: str = "", name: str | None = None, terrain_item: str = "city-ground",
            items: dict | None = None, prepared: dict[Path, list[dict]] | None = None,
            dem: Path | None = None, visuals: dict | None = None,
            passthrough: dict | None = None) -> tuple[dict, dict]:
    """(Recipe mapping, report) of the CityGML under `source` for a selection
    centred on (lat, lon) with (north_south, east_west) half extents.

    `prepared` gives buildings Envsim already extracted for this selection
    (its <name>-lod1.json records, by source file), so large mesh files are
    only streamed for their attributes. `dem` (an Envsim terrain-receipt.json)
    makes the ground that City World's terrain (item city-dem). `visuals`
    ({asset_dir, base_dir, textures}) gives each building part its LOD2 look.
    `passthrough` (from envsim_outputs, with asset_dir and base_dir) passes
    Envsim's own outputs through: anchors, colliders, layers, the terrain."""
    items = {**ITEMS, **(items or {})}
    geodesy, extract, roads_probe = envsim_modules()
    lat0, lon0 = center
    ns_m, ew_m = half_extent
    report = {"buildings": 0, "roads": 0, "courtyards": 0, "courtyards_filled": 0, "skipped": [], "notes": []}
    objects, used, sources = [], set(), []
    crs_seen, providers = set(), set()
    reach_e, reach_n = ew_m, ns_m
    seen_ids: set[str] = set()
    grounds: dict[str, tuple[Path, float, int]] = {}  # part id -> (CityGML file, ground altitude, EPSG)
    terrain_sha = passthrough.get("terrain_sha256") if passthrough else None
    road_outlines: list[list[list[float]]] = []  # the road network layer's outlines (passthrough)

    for path in (list(prepared) if prepared is not None else _files(source, "*bldg*_op.gml")):
        sources.append({"path": str(path.resolve()), "sha256": _sha256(path)})
        issues: list = []
        if prepared is not None:
            records = prepared[path]
        else:
            records = extract.extract_buildings_lod1(path, local_origin=center,
                                                     bounds={"ns_m": ns_m, "ew_m": ew_m}, issues=issues)
        for issue in issues:
            report["skipped"].append({"source": issue.get("building_id"), "kind": "building",
                                      "reason": issue.get("reason_code") or issue.get("message")})
        attributes = _attributes(path, {record["id"].split("__part_")[0] for record in records})
        for record in records:
            if record["id"] in seen_ids:  # the same building in two files (overlapping meshes)
                continue
            seen_ids.add(record["id"])
            gml_id = record["id"].split("__part_")[0]
            info = attributes.get(gml_id, {"name": None, "gen": {}})
            ring = _clean_ring(record["vertices"])
            if ring is None:
                report["skipped"].append({"source": record["id"], "kind": "building",
                                          "reason": "its LOD1 footprint is not a simple polygon"})
                continue
            crs_seen.add(record.get("source_crs", "EPSG:6697"))
            # Heights above the ground: CityGML from osm2citygml says where its
            # base is (base_m); otherwise (PLATEAU altitudes) the building's own
            # bottom is the ground under it (the Studio ground is flat).
            base_m = info["gen"].get("base_m")
            ground = record["zmin"] - float(base_m) if base_m is not None else record["zmin"]
            height = min(max(_mm(record["zmax"] - ground), 1.0), 500.0)
            base = _mm(min(max(record["zmin"] - ground, 0.0), height - 0.5)) if base_m is not None else 0.0
            pose, footprint = _placed(ring)
            holes, filled = _holes(record.get("interior_rings") or [], ring, pose)
            report["courtyards"] += len(holes)
            report["courtyards_filled"] += filled
            reach_e = max(reach_e, *(abs(x) for x, _ in ring))
            reach_n = max(reach_n, *(abs(y) for _, y in ring))
            provider = info["gen"].get("source_provider") or ("plateau" if "6697" in record.get("source_crs", "6697")
                                                              else "citygml")
            providers.add(provider)
            tags = {key: value for key, value in info["gen"].items() if value is not None}
            tags.update(info.get("fields", {}))
            if info["name"]:
                tags["name"] = info["name"]
            source_record = {"provider": provider, "kind": "citygml", "id": record["id"], "note": path.name}
            if tags:
                source_record["tags"] = tags
            part_id = _part_id(record["id"], used)
            grounds[part_id] = (path, ground, int(str(record.get("source_crs", "EPSG:6697")).split(":")[-1]))
            part = {"id": part_id, "item": items["building"], "pose": pose,
                    "params": {"footprint": footprint, "height_m": height,
                               **({"min_height_m": base} if base > 0 else {}),
                               **({"holes": holes} if holes else {})},
                    "source": source_record}
            if passthrough is not None:  # its assets' frame: Envsim's height of its ground
                part["anchor"] = {"x_m": pose["x_m"], "y_m": pose["y_m"],
                                  "z_m": round(ground - passthrough["altitude_offset_m"], 9), "yaw_deg": 0.0,
                                  **({"terrain": terrain_sha} if terrain_sha else {})}
            objects.append(part)
            report["buildings"] += 1

    for path in _files(source, "*tran*_op.gml"):
        sources.append({"path": str(path.resolve()), "sha256": _sha256(path)})
        try:
            surfaces = roads_probe.extract_lod1_roads(path, lat0, lon0, ns_m, ew_m)
        except Exception as exc:  # noqa: BLE001 - Envsim raises when none intersect
            if "no LOD1 road polygon" in str(exc):
                continue
            raise
        crs_seen.add(geodesy.epsg_label(geodesy.file_crs(path, default=6697)))
        for road_id, polygon in surfaces:
            if polygon.area < MIN_ROAD_AREA_M2:
                continue
            gml_id = road_id.rsplit("-", 2)[0]
            as_layer = passthrough is not None and "roads" in passthrough["layers"]
            for suffix, piece in _road_pieces(polygon, tiled=dem is not None and not as_layer):
                # Envsim gives MuJoCo axes (x north, y west); back to east / north.
                shape = piece.simplify(ROAD_SIMPLIFY_M)
                tolerance = 2 * ROAD_SIMPLIFY_M
                while len(shape.exterior.coords) - 1 > MAX_ROAD_POINTS:
                    shape, tolerance = piece.simplify(tolerance), tolerance * 2
                ring = _clean_ring([(-y, x) for x, y in list(shape.exterior.coords)[:-1]])
                if ring is None:
                    report["skipped"].append({"source": road_id + suffix, "kind": "road", "reason": "not a simple polygon"})
                    continue
                if as_layer:  # drawn by the road network layer (Envsim's roads are the terrain)
                    road_outlines.append([[_mm(x), _mm(y)] for x, y in ring])
                    report["roads"] += 1
                    continue
                pose, outline = _placed(ring)
                objects.append({"id": _part_id(road_id + suffix, used),
                                "item": items["road"], "pose": pose, "params": {"outline": outline},
                                "source": {"provider": "citygml", "kind": "citygml", "id": gml_id, "note": path.name}})
                report["roads"] += 1

    if not report["buildings"]:
        raise fail("citygml", "missing_field", "no LOD1 building in the selection", actual=str(source))
    report["clipped"], report["overlaps_left"] = clip_overlaps(objects, items["building"])
    if report["clipped"]:
        report["notes"].append(f"{report['clipped']} building footprints were clipped where they overlapped a larger one in the data")
    report["lod2_visuals"] = 0
    if visuals is not None:
        report["lod2_visuals"] = attach_visuals(objects, grounds, center, visuals.get("textures") or {},
                                                visuals["asset_dir"], visuals["base_dir"])
    if passthrough is not None:
        report["passthrough"] = pass_through(objects, passthrough, road_outlines, used, items["layer"])
    # The environment holds every whole building (centred on the selection).
    def size(half: float, reach: float) -> float:
        # The selection, or (only where a building reaches past it) 0.1 m steps beyond.
        return _mm(2 * half) if reach <= half + 1e-6 else _mm(2 * math.ceil(reach * 10 - 1e-6) / 10)

    size_east, size_north = size(ew_m, reach_e), size(ns_m, reach_n)
    if size_east > _mm(2 * ew_m) or size_north > _mm(2 * ns_m):
        report["notes"].append(f"the environment was widened to {size_east} x {size_north} m to hold whole buildings")
    corners = geodesy.local_enu_to_geodetic([(-size_east / 2, -size_north / 2, 0.0), (size_east / 2, size_north / 2, 0.0)],
                                           lat0, lon0, 4326)
    provider = "openstreetmap" if providers == {"openstreetmap"} else "plateau" if providers == {"plateau"} else "citygml"
    geo = {"provider": provider, "origin": {"lat_deg": round(lat0, 9), "lon_deg": round(lon0, 9)},
           "bbox_deg": {"south": round(corners[0][0], 9), "west": round(corners[0][1], 9),
                        "north": round(corners[1][0], 9), "east": round(corners[1][1], 9)},
           "projection": f"hakoniwa-envsim local ENU about the origin ({', '.join(sorted(crs_seen)) or 'EPSG:6697'})",
           "query": json.dumps({"converter": f"env_citygml {CONVERTER_VERSION}", "sources": sources},
                               ensure_ascii=False, sort_keys=True)}
    if "openstreetmap" in providers:
        geo.update(attribution="© OpenStreetMap contributors", license="ODbL-1.0")
    if "plateau" in providers:
        geo["attribution"] = "; ".join(filter(None, [geo.get("attribution"), "PLATEAU (国土交通省)"]))
    recipe = {
        "schema": env_schema.RECIPE_SCHEMA,
        "name": name or f"CityGML（{lat0:.5f}, {lon0:.5f}）",
        "description": f"Parts from CityGML ({provider}): one building-footprint per building, road-area per road surface.",
        "catalog": catalog,
        "geo": geo,
        "size_m": {"east": size_east, "north": size_north},
        "terrain": ({"item": "city-dem", "params": passthrough["terrain_params"]}
                    if passthrough and passthrough.get("terrain_params") else
                    {"item": "city-dem", "params": {"dem": str(dem)}} if dem is not None else {"item": terrain_item}),
        "objects": objects,
    }
    report["size_m"] = recipe["size_m"]
    report["terrain"] = "dem" if dem is not None else "flat"
    report["provider"] = provider
    return recipe, report


# --- Overlaps already in the data ------------------------------------------------
#
# Map data has buildings whose footprints overlap (PLATEAU: a small building
# reaching 6.6 m² into a station complex). Static buildings never move, but the
# validation reports them and so would a vehicle meeting the seam. So the
# smaller footprint of each overlapping pair (at overlapping heights) gives up
# the part the larger one covers, kept a little apart; the part's position (the
# frame of its LOD2 look) stays. A pair that cannot be separated that way is
# left for a person and reported.

CLIP_GAP_M = 0.002  # the clipped outline stays this far inside (rounding to mm cannot touch again)
MIN_KEPT_SHARE = 0.2  # a building keeping less than this of its footprint is not clipped (reported)
MIN_OVERLAP_M2 = 1e-4


def _world_footprint(obj):
    from shapely.geometry import Polygon

    x, y = obj["pose"]["x_m"], obj["pose"]["y_m"]
    return Polygon([(x + px, y + py) for px, py in obj["params"]["footprint"]],
                   [[(x + px, y + py) for px, py in hole] for hole in obj["params"].get("holes", [])])


def _height_range(obj) -> tuple[float, float]:
    params = obj["params"]
    return float(params.get("min_height_m", 0.0)), float(params["height_m"])


def clip_overlaps(objects: list[dict], building_item: str) -> tuple[int, list[dict]]:
    """Separate overlapping building footprints (see above); returns (how many
    were clipped, the overlaps left: [{a, b, area_m2, reason}])."""
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    buildings = sorted((obj for obj in objects if obj["item"] == building_item), key=lambda obj: obj["id"])
    shapes = {obj["id"]: _world_footprint(obj) for obj in buildings}
    originals = {key: shape.area for key, shape in shapes.items()}
    tree = STRtree([shapes[obj["id"]] for obj in buildings])
    clipped, left = set(), []
    for index, obj in enumerate(buildings):
        for other_index in sorted(int(i) for i in tree.query(shapes[obj["id"]])):
            if other_index <= index:
                continue
            other = buildings[other_index]
            a, b = shapes[obj["id"]], shapes[other["id"]]
            (low_a, high_a), (low_b, high_b) = _height_range(obj), _height_range(other)
            if min(high_a, high_b) <= max(low_a, low_b):
                continue  # one above the other (a canopy over a building)
            overlap = a.intersection(b).area
            if overlap <= MIN_OVERLAP_M2:
                continue
            small, large = (obj, other) if (a.area, obj["id"]) < (b.area, other["id"]) else (other, obj)
            rest = shapes[small["id"]].difference(shapes[large["id"]].buffer(CLIP_GAP_M, join_style="mitre"))
            if rest.geom_type == "MultiPolygon":
                pieces = sorted(rest.geoms, key=lambda piece: piece.area, reverse=True)
                if pieces[1].area > 0.05 * pieces[0].area:
                    left.append({"a": small["id"], "b": large["id"], "area_m2": round(overlap, 3),
                                 "reason": "clipping would split the building"})
                    continue
                rest = pieces[0]
            if rest.geom_type != "Polygon" or rest.is_empty or rest.area < MIN_KEPT_SHARE * originals[small["id"]]:
                left.append({"a": small["id"], "b": large["id"], "area_m2": round(overlap, 3),
                             "reason": "clipping would leave too little"})
                continue
            x, y = small["pose"]["x_m"], small["pose"]["y_m"]
            ring = _clean_ring([(px - x, py - y) for px, py in list(rest.exterior.coords)[:-1]])
            holes = [_clean_ring([(px - x, py - y) for px, py in list(hole.coords)[:-1]]) for hole in rest.interiors]
            if ring is None or None in holes or (holes and env_polygon.holes_problem(ring, holes)):
                left.append({"a": small["id"], "b": large["id"], "area_m2": round(overlap, 3),
                             "reason": "the clipped outline is not a simple polygon with courtyards"})
                continue
            small["params"]["footprint"] = [[_mm(px), _mm(py)] for px, py in ring]
            small["params"].pop("holes", None)
            if holes:  # its own courtyards, and any the clipping opened
                small["params"]["holes"] = [[[_mm(px), _mm(py)] for px, py in hole] for hole in holes]
            shapes[small["id"]] = Polygon([(x + px, y + py) for px, py in ring],
                                          [[(x + px, y + py) for px, py in hole] for hole in holes])
            tags = small["source"].setdefault("tags", {})
            tags["clipped_by"] = ", ".join(filter(None, [tags.get("clipped_by"), large["source"]["id"]]))
            tags["clipped_m2"] = f"{float(tags.get('clipped_m2', 0)) + overlap:.3f}"
            clipped.add(small["id"])
    return len(clipped), left


# --- LOD2 visuals (stage B-1) ----------------------------------------------------
#
# Each building part gets a GLB of its own CityGML LOD2 surfaces (textures
# included) in the part's frame: glTF axes, origin at the part's position and
# its ground, so the Studio moves and turns it with the part. Collisions stay
# the LOD1 footprint.

APP = "{http://www.opengis.net/citygml/appearance/2.0}"
BLDG = "{http://www.opengis.net/citygml/building/2.0}"
GML = "{http://www.opengis.net/gml}"
LOD2_TAGS = (f"{BLDG}lod2MultiSurface", f"{BLDG}lod2Geometry", f"{BLDG}lod2Solid")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _buildings_and_appearances(path: Path, wanted: set[str]):
    """The wanted bldg:Building elements of a CityGML file and its appearance
    map, streamed (a PLATEAU mesh file is hundreds of megabytes)."""
    helpers = env_envsim.glb_helpers()
    buildings, appearances = {}, ET.Element("appearances")
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag == f"{BLDG}Building":
            gml_id = element.get(GML_ID)
            if gml_id in wanted:
                buildings[gml_id] = element
            else:
                element.clear()
        elif element.tag == f"{APP}Appearance":
            appearances.append(element)
    return buildings, helpers.appearance_map(appearances)


def _lod2_polygons(building) -> list:
    found, seen = [], set()
    for tag in LOD2_TAGS:
        for surface in building.iter(tag):
            for polygon in surface.iter(f"{GML}Polygon"):
                key = polygon.get(GML_ID) or id(polygon)
                if key not in seen:
                    seen.add(key)
                    found.append(polygon)
    return found


def _rings(polygon, parse_poslist) -> list[tuple[str, list]]:
    """(ring id, points) of a polygon's exterior and interiors, without repeated closing points."""
    exterior = polygon.find(f"{GML}exterior/{GML}LinearRing")
    if exterior is None:
        return []
    rings = []
    for ring in [exterior, *polygon.findall(f"{GML}interior/{GML}LinearRing")]:
        pos = ring.find(f"{GML}posList")
        if pos is None or not pos.text:
            return []
        points = parse_poslist(pos.text)
        if len(points) > 1 and all(abs(a - b) < 1e-10 for a, b in zip(points[0], points[-1])):
            points = points[:-1]
        if len(points) < 3:
            return []
        rings.append((ring.get(GML_ID, ""), points))
    return rings


def building_visual(building, appearances, gml_path: Path, textures: dict, center, epsg: int,
                    origin: tuple[float, float], ground: float) -> bytes | None:
    """The GLB of one building's LOD2 surfaces in its part's frame (None when
    it has no LOD2). `textures` maps (source file, image uri) to image files;
    an image next to the CityGML is used when it is not listed."""
    import env_generate

    geodesy, extract, _probe = env_envsim.pipeline()
    helpers = env_envsim.glb_helpers()
    groups: dict[Path | None, dict] = {}
    for polygon in _lod2_polygons(building):
        rings = _rings(polygon, extract.parse_poslist)
        if not rings:
            continue
        local = []
        for _ring_id, points in rings:
            enu = geodesy.project_to_local_enu(points, center[0], center[1], epsg)
            local.append([(east - origin[0], altitude - ground, -(north - origin[1])) for east, north, altitude in enu])
        uv_rings = None
        image = None
        found = appearances.get(polygon.get(GML_ID, ""))
        if found is not None:
            uri, uv_by_ring = found
            uv_rings = [uv_by_ring.get(ring_id) for ring_id, _ in rings]
            if any(uv is None or len(uv) != len(ring) for uv, (_, ring) in zip(uv_rings, rings)):
                uv_rings = None
            else:
                image = textures.get((str(gml_path.resolve()), uri))
                if image is None and not uri.startswith(("http:", "https:")) and ".." not in Path(uri).parts:
                    candidate = gml_path.parent / uri
                    image = candidate if candidate.is_file() else None
                if image is None:
                    uv_rings = None
        try:
            vertices, faces = helpers.triangulate_rings(local)
        except Exception:  # noqa: BLE001 - a degenerate surface is left out, as Envsim does
            continue
        uv_flat = [uv for ring in uv_rings for uv in ring] if uv_rings else None
        group = groups.setdefault(image if uv_flat else None, {"positions": [], "normals": [], "uvs": []})
        for face in faces:
            a, b, c = (vertices[index] for index in face)
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            for index in face:
                group["positions"].append(tuple(float(value) for value in vertices[index]))
                group["normals"].append((nx / length, ny / length, nz / length))
                if uv_flat:
                    group["uvs"].append(uv_flat[index])
    if not groups:
        return None
    builder = env_generate._GlbBuilder()
    primitives = []
    for image, group in sorted(groups.items(), key=lambda item: str(item[0] or "")):
        if image is not None:
            material = builder.textured_material(Path(image).read_bytes(), MIME.get(Path(image).suffix.lower(), "image/jpeg"))
        else:
            material = builder.material("#c9c3b6")
        primitives.append(builder.surface_primitive(group["positions"], group["normals"],
                                                    group["uvs"] if image is not None else None,
                                                    list(range(len(group["positions"]))), material))
    builder.node("building", None, (0.0, 0.0, 0.0), 0.0, {"source": building.get(GML_ID)}, primitives=primitives)
    return builder.glb()


def attach_visuals(objects: list[dict], grounds: dict[str, tuple[Path, float, int]], center, textures: dict,
                   asset_dir: Path, base_dir: Path) -> int:
    """Write each building part's LOD2 GLB under asset_dir and point its
    `visual` parameter at it (relative to base_dir); returns how many got one.
    `grounds` maps a part id to (source file, ground altitude, EPSG)."""
    by_file: dict[Path, list[dict]] = {}
    for obj in objects:
        if obj["id"] in grounds:
            by_file.setdefault(grounds[obj["id"]][0], []).append(obj)
    count = 0
    for path, parts in by_file.items():
        wanted = {obj["source"]["id"].split("__part_")[0] for obj in parts}
        buildings, appearances = _buildings_and_appearances(path, wanted)
        for obj in parts:
            building = buildings.get(obj["source"]["id"].split("__part_")[0])
            if building is None:
                continue
            _path, ground, epsg = grounds[obj["id"]]
            glb = building_visual(building, appearances, path, textures, center, epsg,
                                  (obj["pose"]["x_m"], obj["pose"]["y_m"]), ground)
            if glb is None:
                continue
            asset_dir.mkdir(parents=True, exist_ok=True)
            target = asset_dir / f"{obj['id']}.glb"
            target.write_bytes(glb)
            obj["params"]["visual"] = Path(os.path.relpath(target, base_dir)).as_posix()
            count += 1
    return count


# --- Envsim's own outputs, passed through --------------------------------------------
#
# A City World Envsim built already holds its accurate parts: the terrain's
# hfield and GLB, each building's colliders (P0-P3, named by gml:id) and its
# layers' GLBs. They are copied beside the Recipe as they are and placed from
# each object's anchor (env_generate.asset_frame), so an unedited import
# generates Envsim's own world again (tools/env_roundtrip.py checks it).

GML_UUID = re.compile(r"bldg_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def envsim_outputs(build: Path) -> dict | None:
    """What an Envsim build made for its City World: the altitude its heights
    are relative to, the buildings' MJCF, the terrain files and the other
    layers' GLBs (and MJCF). None when it built no City World."""
    receipt_path = build / "world" / "city-world-receipt.json"
    if not receipt_path.is_file():
        return None
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    components = receipt.get("components", {})
    offset = float(receipt.get("coordinate_frame", {}).get("origin", {}).get("altitude_offset_m", 0.0))
    terrain_dir = build / "components" / "terrain"
    terrain = None
    if (terrain_dir / "terrain-receipt.json").is_file() and (terrain_dir / "terrain.hf").is_file():
        terrain = {"receipt": terrain_dir / "terrain-receipt.json", "hf": terrain_dir / "terrain.hf",
                   "xml": terrain_dir / "terrain.xml", "glb": terrain_dir / "terrain.glb"}
    buildings_xml = Path(components["buildings_xml"]) if components.get("buildings_xml") else None
    extra_mjcf = [Path(path) for path in components.get("extra_mjcf", [])]
    layers = {}
    for glb in components.get("glb_geometry_counts", {}):
        glb = Path(glb)
        name = glb.parent.name  # components/<layer>/<layer>.glb
        if name in ("terrain", "buildings") or not glb.is_file():
            continue
        xml = next((path for path in extra_mjcf if path.parent == glb.parent and path.is_file()), None)
        layers[name] = {"glb": glb, "xml": xml}
    return {"altitude_offset_m": offset, "terrain": terrain, "layers": layers,
            "buildings_xml": buildings_xml if buildings_xml and buildings_xml.is_file() else None}


def split_colliders(mjcf: Path, wanted: set[str]) -> dict[str, tuple[list, list]]:
    """(meshes, worldbody elements) of each wanted gml:id in Envsim's buildings
    MJCF, elements as written: its top-level geoms and bodies name the
    building (geom_<id>, body_<id>, p1_surface_<id>_..., roof_<id>_...)."""
    root = ET.parse(mjcf).getroot()
    asset = root.find("asset")
    meshes = {element.get("name"): element for element in (asset if asset is not None else [])}
    found: dict[str, tuple[list, list]] = {}
    world = root.find("worldbody")
    for element in (world if world is not None else []):
        name = element.get("name") or ""
        match = GML_UUID.search(name)
        gml_id = match.group(0) if match and match.group(0) in wanted else max(
            (candidate for candidate in wanted if candidate in name), key=len, default=None)
        if gml_id is None:
            continue
        used, bodies = found.setdefault(gml_id, ([], []))
        bodies.append(element)
        for node in element.iter():
            mesh = meshes.get(node.get("mesh")) if node.get("mesh") else None
            if mesh is not None and mesh not in used:
                used.append(mesh)
    return found


def _write_fragment(path: Path, model: str, meshes: list, bodies: list) -> None:
    root = ET.Element("mujoco", {"model": model})
    if meshes:
        ET.SubElement(root, "asset").extend(meshes)
    ET.SubElement(root, "worldbody").extend(bodies)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _glb_outline(glb: Path) -> list[list[float]]:
    """A layer's extent on the plan: the box of its GLB's positions (glTF: x
    east, z = -north), in metres."""
    import env_generate

    document, _binary = env_generate.read_glb(glb.read_bytes())
    lows, highs = [], []
    for mesh in document.get("meshes", []):
        for primitive in mesh["primitives"]:
            accessor = document["accessors"][primitive["attributes"]["POSITION"]]
            if "min" in accessor:
                lows.append(accessor["min"])
                highs.append(accessor["max"])
    if not lows:
        return [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]
    west, east = min(low[0] for low in lows), max(high[0] for high in highs)
    south, north = -max(high[2] for high in highs), -min(low[2] for low in lows)
    west, east, south, north = _mm(west), _mm(max(east, west + 0.01)), _mm(south), _mm(max(north, south + 0.01))
    return [[west, south], [east, south], [east, north], [west, north]]


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Counter-clockwise hull of points (Andrew's monotone chain)."""
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _glb_outlines(glb: Path) -> list[list[list[float]]]:
    """What a layer (road markings, bridges) covers on the plan: one outline
    per connected piece of its triangles (vertices within a millimetre are one),
    each piece's hull from above (glTF: x east, z = -north), in metres. A GLB
    without triangles gives its box (_glb_outline)."""
    import struct

    import env_generate

    document, binary = env_generate.read_glb(glb.read_bytes())
    views = document.get("bufferViews", [])
    formats = {5126: "f", 5125: "I", 5123: "H", 5121: "B"}
    widths = {"SCALAR": 1, "VEC3": 3}

    def read(index: int) -> list[tuple]:
        accessor = document["accessors"][index]
        view = views[accessor["bufferView"]]
        width = widths[accessor["type"]]
        code = formats[accessor["componentType"]]
        item = struct.calcsize("<" + code * width)
        stride = view.get("byteStride") or item
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        return [struct.unpack_from("<" + code * width, binary, start + n * stride) for n in range(accessor["count"])]

    parent: dict = {}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    positions: dict = {}
    for mesh in document.get("meshes", []):
        for primitive in mesh["primitives"]:
            if primitive.get("mode", 4) != 4 or "POSITION" not in primitive["attributes"]:
                continue
            keys = []
            for x, _y, z in read(primitive["attributes"]["POSITION"]):
                key = (round(x * 1000), round(-z * 1000))
                positions[key] = (x, -z)
                parent.setdefault(key, key)
                keys.append(key)
            order = [value[0] for value in read(primitive["indices"])] if "indices" in primitive else range(len(keys))
            order = list(order)
            for n in range(0, len(order) - 2, 3):
                a, b, c = (keys[order[n + k]] for k in range(3))
                for other in (b, c):
                    root_a, root_other = find(a), find(other)
                    if root_a != root_other:
                        parent[root_a] = root_other
    pieces: dict = {}
    for key in parent:
        pieces.setdefault(find(key), []).append(positions[key])
    outlines = []
    for points in pieces.values():
        hull = _convex_hull([(_mm(x), _mm(y)) for x, y in points])
        if len(hull) >= 3:
            outlines.append([[x, y] for x, y in hull])
    return sorted(outlines) or [_glb_outline(glb)]


def copy_terrain(terrain: dict, asset_dir: Path, base_dir: Path) -> tuple[dict, str]:
    """(terrain params, hfield sha256) of an Envsim terrain copied beside the
    Recipe as it is (its receipt pointing at the copies)."""
    import shutil

    target = asset_dir / "terrain"
    target.mkdir(parents=True, exist_ok=True)
    receipt = json.loads(terrain["receipt"].read_text(encoding="utf-8"))
    shutil.copyfile(terrain["hf"], target / "terrain.hf")
    receipt["hfield"] = {**receipt.get("hfield", {}), "path": "terrain.hf"}
    if terrain["xml"].is_file():
        shutil.copyfile(terrain["xml"], target / "terrain.xml")
        receipt["mjcf"] = "terrain.xml"
    (target / "terrain-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    params = {"dem": Path(os.path.relpath(target / "terrain-receipt.json", base_dir)).as_posix()}
    if terrain["glb"].is_file():
        shutil.copyfile(terrain["glb"], target / "terrain.glb")
        params["visual"] = Path(os.path.relpath(target / "terrain.glb", base_dir)).as_posix()
    return params, _sha256(target / "terrain.hf")


def pass_through(objects: list[dict], passthrough: dict, road_outlines: list, used: set[str], layer_item: str) -> dict:
    """Attach Envsim's colliders to the building parts (the first part of each
    gml:id) and add its layers as city-layer objects; returns what was done."""
    asset_dir, base_dir = passthrough["asset_dir"], passthrough["base_dir"]
    relative = lambda path: Path(os.path.relpath(path, base_dir)).as_posix()  # noqa: E731
    report = {"colliders": 0, "layers": [], "terrain": bool(passthrough.get("terrain_params"))}
    if passthrough.get("buildings_xml"):
        by_gml: dict[str, dict] = {}
        for obj in objects:
            if "anchor" in obj and obj.get("source", {}).get("id"):
                by_gml.setdefault(obj["source"]["id"].split("__part_")[0], obj)
        for gml_id, (meshes, bodies) in split_colliders(passthrough["buildings_xml"], set(by_gml)).items():
            obj = by_gml[gml_id]
            target = asset_dir / f"{obj['id']}.xml"
            _write_fragment(target, obj["id"], meshes, bodies)
            obj["params"]["collision"] = relative(target)
            report["colliders"] += 1
    anchor = {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0, "yaw_deg": 0.0,
              **({"terrain": passthrough["terrain_sha256"]} if passthrough.get("terrain_sha256") else {})}
    import shutil

    for name, layer in sorted(passthrough["layers"].items()):
        asset_dir.mkdir(parents=True, exist_ok=True)
        glb = asset_dir / f"layer-{name}.glb"
        shutil.copyfile(layer["glb"], glb)
        outlines = road_outlines if name == "roads" and road_outlines else _glb_outlines(layer["glb"])
        params = {"outlines": outlines, "visual": relative(glb),
                  **({"color": LAYER_COLORS[name]} if name in LAYER_COLORS else {})}
        if layer["xml"] is not None:
            xml = asset_dir / f"layer-{name}.xml"
            shutil.copyfile(layer["xml"], xml)
            params["collision"] = relative(xml)
        objects.append({"id": _part_id(name, used), "item": layer_item, "pose": {"x_m": 0.0, "y_m": 0.0, "yaw_deg": 0.0},
                        "params": params, "anchor": dict(anchor),
                        "source": {"provider": "hakoniwa-envsim", "kind": "city-world-layer", "id": name,
                                   "note": layer["glb"].name}})
        report["layers"].append(name)
    return report


# --- Envsim builds already in a workspace ----------------------------------------

def read_envsim_build(build: Path) -> dict:
    """The selection, sources and extracted buildings of an Envsim build
    directory (the one holding download-manifest.json)."""
    manifest = json.loads((build / "download-manifest.json").read_text(encoding="utf-8"))
    query = manifest["query"]
    lod1_files = sorted(build.glob("*-lod1.json"))
    prepared = None
    if lod1_files:
        prepared = {}
        for record in json.loads(lod1_files[0].read_text(encoding="utf-8"))["polygons"]:
            prepared.setdefault(Path(record["source_gml"]), []).append(record)
        prepared = {path: records for path, records in prepared.items() if path.is_file()} or None
    receipt_path = build / "build-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    terrain = build / "components" / "terrain" / "terrain-receipt.json"
    dem = None
    if terrain.is_file() and json.loads(terrain.read_text(encoding="utf-8")).get("dem", "available") == "available":
        dem = terrain
    return {
        "center": (float(query["center_lat"]), float(query["center_lon"])),
        "half_extent": (float(query["ns_m"]), float(query["ew_m"])),
        "source": build / "source",
        "prepared": prepared,
        "feature_types": sorted({item.get("feature_type") for item in manifest.get("files", []) if item.get("feature_type")}),
        "buildings": sum(len(records) for records in (prepared or {}).values()),
        "world": (build / "world" / "city-world.xml").is_file(),
        "built": receipt.get("outputs", {}),
        "dem": dem,
    }


# Directories a workspace search does not enter (installs, downloads, CityGML sources).
SKIP_DIRS = {"foundation", "downloads", "cache", "source", "components", "node_modules", ".git", ".venv"}


def _manifests(root: Path, max_depth: int):
    for directory, subdirectories, files in os.walk(root):
        depth = len(Path(directory).relative_to(root).parts)
        subdirectories[:] = sorted(name for name in subdirectories
                                   if name not in SKIP_DIRS and depth < max_depth)
        if "download-manifest.json" in files:
            yield Path(directory) / "download-manifest.json"


def discover(roots: list[Path], max_depth: int = 8) -> list[dict]:
    """Envsim builds under the roots (a business-pack workspace keeps them as
    work/recipes/city-world-web-ui/runtime/jobs/<job>/build and the like)."""
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for manifest in _manifests(root, max_depth):
            build = manifest.parent
            try:
                info = read_envsim_build(build)
            except (OSError, KeyError, ValueError):
                continue
            job = build.parent / "job.json"
            title = build.parent.name if build.name == "build" else build.name
            if job.is_file():
                title = json.loads(job.read_text(encoding="utf-8")).get("job_id", title)
            found.append({
                "id": re.sub(r"[^a-z0-9_-]", "-", title.lower())[:64].strip("-") or "city",
                "title": title, "path": str(build), "root": str(root),
                "center": {"latitude": info["center"][0], "longitude": info["center"][1]},
                "half_extent_m": {"north_south": info["half_extent"][0], "east_west": info["half_extent"][1]},
                "feature_types": info["feature_types"], "buildings": info["buildings"], "world": info["world"],
                "dem": info["dem"] is not None,
            })
    return found


def build_textures(build: Path) -> dict[tuple[str, str], Path]:
    """(source CityGML, image uri) -> image file, from the City World's
    buildings GLB receipt (it records where each texture it used is)."""
    receipt = build / "components" / "buildings" / "buildings-glb-receipt.json"
    if not receipt.is_file():
        return {}
    found = {}
    for texture in json.loads(receipt.read_text(encoding="utf-8")).get("textures", []):
        if texture.get("path") and Path(texture["path"]).is_file():
            found[(str(Path(texture["source_gml"]).resolve()), texture["image_uri"])] = Path(texture["path"])
    return found


def asset_dir_for(recipe_path: Path) -> Path:
    """Where a Recipe's visual assets go: <recipe>.assets beside it."""
    return recipe_path.parent / f"{recipe_path.stem}.assets"


def convert_build(build: Path, use_dem: bool = True, recipe_path: Path | None = None, *, visuals: bool = True,
                  passthrough: bool = True, **options) -> tuple[dict, dict]:
    """Parts of an Envsim build: its selection, its extracted buildings, its
    roads, (when it has one and `use_dem`) its DEM terrain as the ground, and,
    given `recipe_path` (assets are written beside it): each building's LOD2
    look (`visuals`) and Envsim's own outputs passed through (`passthrough`:
    colliders, layers, the terrain's own files)."""
    info = read_envsim_build(build)
    looks = passed = None
    if recipe_path is not None:
        asset_dir = asset_dir_for(recipe_path)
        if visuals:
            looks = {"asset_dir": asset_dir, "base_dir": recipe_path.parent, "textures": build_textures(build)}
        outputs = envsim_outputs(build) if passthrough else None
        if outputs is not None:
            passed = {**outputs, "asset_dir": asset_dir, "base_dir": recipe_path.parent}
            if use_dem and outputs["terrain"] is not None and info["dem"] is not None:
                passed["terrain_params"], passed["terrain_sha256"] = copy_terrain(outputs["terrain"], asset_dir,
                                                                                  recipe_path.parent)
    recipe, report = convert(info["source"], info["center"], info["half_extent"], prepared=info["prepared"],
                             dem=info["dem"] if use_dem else None, visuals=looks, passthrough=passed, **options)
    report["build"] = str(build)
    return recipe, report


def write_recipe(recipe: dict, out: Path) -> None:
    env_schema.parse_recipe(recipe, out)  # valid before it is written
    out.parent.mkdir(parents=True, exist_ok=True)
    env_schema.save_yaml(recipe, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--citygml", type=Path, help="a CityGML file or a directory of *_op.gml")
    source.add_argument("--envsim-build", type=Path, help="an Envsim build directory (its selection and buildings)")
    source.add_argument("--list", type=Path, nargs="+", metavar="ROOT", help="list the Envsim builds under ROOTs")
    parser.add_argument("--center", help="LAT,LON of the selection centre (with --citygml)")
    parser.add_argument("--half-extent", help="NS,EW half extents in metres (with --citygml)")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--terrain", default="city-ground")
    parser.add_argument("--flat", action="store_true", help="with --envsim-build: flat ground even when it has a DEM")
    parser.add_argument("--no-visuals", action="store_true", help="with --envsim-build: LOD1 boxes only (no LOD2 GLBs)")
    parser.add_argument("--no-passthrough", action="store_true",
                        help="with --envsim-build: make everything from CityGML instead of using Envsim's own outputs")
    parser.add_argument("--name")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        builds = discover([root.resolve() for root in args.list])
        print(json.dumps(builds, ensure_ascii=False, indent=2) if args.json else "\n".join(
            f"{item['title']:44} {item['buildings']:5} buildings  {','.join(item['feature_types']):20} {item['path']}"
            for item in builds))
        return 0
    if args.out is None:
        parser.error("--out is required")
    if not env_rules.ID_PATTERN.match(args.out.stem):
        parser.error(f"--out file name {args.out.name!r} is not a Recipe id (lower case letters, digits, - and _; "
                     "the Studio opens a Recipe by its file name)")
    try:
        out = args.out.resolve()
        catalog = Path(os.path.relpath(args.catalog.resolve(), out.parent)).as_posix()
        if args.envsim_build:
            recipe, report = convert_build(args.envsim_build.resolve(), use_dem=not args.flat, catalog=catalog,
                                           name=args.name, terrain_item=args.terrain, recipe_path=out,
                                           visuals=not args.no_visuals, passthrough=not args.no_passthrough)
        else:
            if not args.center or not args.half_extent:
                parser.error("--citygml needs --center and --half-extent")
            lat, lon = (float(value) for value in args.center.split(","))
            ns_m, ew_m = (float(value) for value in args.half_extent.split(","))
            recipe, report = convert(args.citygml, (lat, lon), (ns_m, ew_m), catalog=catalog, name=args.name,
                                     terrain_item=args.terrain)
        write_recipe(recipe, out)
    except DiagnosticError as error:  # before ValueError: DiagnosticError is one
        print(json.dumps({"ok": False, "diagnostics": [item.as_json() for item in error.diagnostics]},
                         ensure_ascii=False, indent=2))
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    result = {"ok": True, "recipe": str(out), **report}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
          f"OK  {out}: {report['buildings']} buildings, {report['roads']} roads, "
          f"{report['size_m']['east']} x {report['size_m']['north']} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
