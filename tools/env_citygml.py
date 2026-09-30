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

Each part keeps where it came from (gml:id, source file, OSM tags when the
CityGML came from OpenStreetMap) in its `source`, so later stages can attach
the same building's LOD2 geometry and move it with the part.

    env_citygml.py --citygml DIR --center LAT,LON --half-extent NS,EW --out work/recipes/x.yaml

Envsim is found at $HAKONIWA_ENVSIM_ROOT or ../hakoniwa-envsim (the Workspace
Recipe hakoniwa/recipes/citygml-parts.yaml materializes it there).
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
ITEMS = {"building": "building-footprint", "road": "road-area"}
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
            dem: Path | None = None, visuals: dict | None = None) -> tuple[dict, dict]:
    """(Recipe mapping, report) of the CityGML under `source` for a selection
    centred on (lat, lon) with (north_south, east_west) half extents.

    `prepared` gives buildings Envsim already extracted for this selection
    (its <name>-lod1.json records, by source file), so large mesh files are
    only streamed for their attributes. `dem` (an Envsim terrain-receipt.json)
    makes the ground that City World's terrain (item city-dem). `visuals`
    ({asset_dir, base_dir, textures}) gives each building part its LOD2 look."""
    items = {**ITEMS, **(items or {})}
    geodesy, extract, roads_probe = envsim_modules()
    lat0, lon0 = center
    ns_m, ew_m = half_extent
    report = {"buildings": 0, "roads": 0, "courtyards_filled": 0, "skipped": [], "notes": []}
    objects, used, sources = [], set(), []
    crs_seen, providers = set(), set()
    reach_e, reach_n = ew_m, ns_m
    seen_ids: set[str] = set()
    grounds: dict[str, tuple[Path, float, int]] = {}  # part id -> (CityGML file, ground altitude, EPSG)

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
            if record.get("interior_rings"):
                report["courtyards_filled"] += 1
            crs_seen.add(record.get("source_crs", "EPSG:6697"))
            # Heights above the ground: CityGML from osm2citygml says where its
            # base is (base_m); otherwise (PLATEAU altitudes) the building's own
            # bottom is the ground under it (the Studio ground is flat).
            base_m = info["gen"].get("base_m")
            ground = record["zmin"] - float(base_m) if base_m is not None else record["zmin"]
            height = min(max(_mm(record["zmax"] - ground), 1.0), 500.0)
            base = _mm(min(max(record["zmin"] - ground, 0.0), height - 0.5)) if base_m is not None else 0.0
            pose, footprint = _placed(ring)
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
            objects.append({"id": part_id, "item": items["building"], "pose": pose,
                            "params": {"footprint": footprint, "height_m": height,
                                       **({"min_height_m": base} if base > 0 else {})},
                            "source": source_record})
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
            for suffix, piece in _road_pieces(polygon, tiled=dem is not None):
                # Envsim gives MuJoCo axes (x north, y west); back to east / north.
                shape = piece.simplify(ROAD_SIMPLIFY_M)
                tolerance = 2 * ROAD_SIMPLIFY_M
                while len(shape.exterior.coords) - 1 > MAX_ROAD_POINTS:
                    shape, tolerance = piece.simplify(tolerance), tolerance * 2
                ring = _clean_ring([(-y, x) for x, y in list(shape.exterior.coords)[:-1]])
                if ring is None:
                    report["skipped"].append({"source": road_id + suffix, "kind": "road", "reason": "not a simple polygon"})
                    continue
                pose, outline = _placed(ring)
                objects.append({"id": _part_id(road_id + suffix, used),
                                "item": items["road"], "pose": pose, "params": {"outline": outline},
                                "source": {"provider": "citygml", "kind": "citygml", "id": gml_id, "note": path.name}})
                report["roads"] += 1

    if not report["buildings"]:
        raise fail("citygml", "missing_field", "no LOD1 building in the selection", actual=str(source))
    report["lod2_visuals"] = 0
    if visuals is not None:
        report["lod2_visuals"] = attach_visuals(objects, grounds, center, visuals.get("textures") or {},
                                                visuals["asset_dir"], visuals["base_dir"])
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
        "terrain": {"item": "city-dem", "params": {"dem": str(dem)}} if dem is not None else {"item": terrain_item},
        "objects": objects,
    }
    report["size_m"] = recipe["size_m"]
    report["terrain"] = "dem" if dem is not None else "flat"
    report["provider"] = provider
    return recipe, report


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


def convert_build(build: Path, use_dem: bool = True, recipe_path: Path | None = None, **options) -> tuple[dict, dict]:
    """Parts of an Envsim build: its selection, its extracted buildings, its
    roads, (when it has one and `use_dem`) its DEM terrain as the ground, and
    (given `recipe_path`) each building's LOD2 look as a visual asset."""
    info = read_envsim_build(build)
    visuals = None
    if recipe_path is not None:
        visuals = {"asset_dir": asset_dir_for(recipe_path), "base_dir": recipe_path.parent,
                   "textures": build_textures(build)}
    recipe, report = convert(info["source"], info["center"], info["half_extent"], prepared=info["prepared"],
                             dem=info["dem"] if use_dem else None, visuals=visuals, **options)
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
                                           name=args.name, terrain_item=args.terrain,
                                           recipe_path=None if args.no_visuals else out)
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
