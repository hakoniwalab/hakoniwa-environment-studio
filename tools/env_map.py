#!/usr/bin/env python3
"""Bootstrap an Environment Recipe from open map data (#10).

Map data is not turned into a model: buildings and roads become ordinary
Recipe objects (building-footprint, road-path items) that people and agents
then edit like any other. The flow:

  bbox (south, west, north, east in degrees)
    -> map features: OpenStreetMap through the Overpass API, an Overpass JSON
       file, or a GeoJSON FeatureCollection
    -> local metres: ENU about the bbox centre (a local tangent plane with the
       WGS84 radii there; well under 0.1 % off within a few kilometres)
    -> Recipe: size from the bbox, a building per footprint (clipped to the
       area), a road per way (clipped; split where it leaves and comes back)

Missing values are filled with fixed defaults (a building's height from its
levels, else from its kind; a road's lanes and width from its highway class),
so the same data always gives the same Recipe. Where each object came from
(provider, OSM element, tags) is kept in its `source`, and the origin, bbox,
projection and attribution in the Recipe's `geo`.

    env_map.py --bbox S,W,N,E --overpass --out work/recipes/my-block.yaml
    env_map.py --bbox S,W,N,E --osm-json data.json --out recipe.yaml
    env_map.py --geojson data.geojson --out recipe.yaml    (bbox from the data)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import env_polygon  # noqa: E402
import env_schema  # noqa: E402
from env_diagnostics import DiagnosticError, fail  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "catalogs/starter/catalog.yaml"
# The public Overpass instance; HAKONIWA_OVERPASS_URL (or --endpoint) points elsewhere.
DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90
ATTRIBUTION = "© OpenStreetMap contributors"
LICENSE = "ODbL-1.0"
PROJECTION = "local tangent plane about the bbox centre (WGS84 radii), x east, y north, metres"

# A sanity bound on the area imported at once, metres per side.
MAX_SIDE_M = 2000.0
# Points closer than this are merged; smaller buildings and shorter roads are dropped.
MIN_STEP_M = 0.05
MIN_BUILDING_AREA_M2 = 4.0
MIN_ROAD_LENGTH_M = 1.0

# WGS84.
_A = 6378137.0
_E2 = 6.69437999014e-3

# Heights: a building's own height tag, else levels x this, else by its kind.
LEVEL_HEIGHT_M = 3.0
# A roof without walls (building=roof: a canopy) becomes a slab this thick under its height.
ROOF_SLAB_M = 0.5
DEFAULT_HEIGHT_M = 9.0
HEIGHTS_BY_KIND = {
    "house": 6.0, "detached": 6.0, "semidetached_house": 6.0, "terrace": 6.0, "bungalow": 4.0,
    "residential": 9.0, "apartments": 15.0, "dormitory": 12.0, "hotel": 20.0,
    "commercial": 12.0, "office": 15.0, "retail": 6.0, "supermarket": 6.0,
    "industrial": 8.0, "warehouse": 8.0, "factory": 10.0,
    "school": 12.0, "university": 15.0, "hospital": 18.0, "public": 12.0, "civic": 12.0,
    "church": 12.0, "temple": 8.0, "shrine": 6.0,
    "garage": 3.0, "garages": 3.0, "carport": 3.0, "shed": 3.0, "hut": 3.0, "kiosk": 3.0, "roof": 4.0,
}
# Roads: lanes (both directions) by highway class; width = width tag, else lanes x LANE_WIDTH_M.
LANE_WIDTH_M = 3.25
LANES_BY_CLASS = {
    "motorway": 4, "trunk": 4, "primary": 2, "secondary": 2, "tertiary": 2, "unclassified": 2,
    "residential": 2, "living_street": 1, "service": 1, "road": 2,
    "motorway_link": 1, "trunk_link": 1, "primary_link": 1, "secondary_link": 1, "tertiary_link": 1,
}
MAX_LANES = 4  # what the road-path type draws
MIN_ROAD_WIDTH_M, MAX_ROAD_WIDTH_M = 2.5, 60.0


@dataclass(frozen=True)
class Box:
    south: float
    west: float
    north: float
    east: float

    @staticmethod
    def parse(text: str) -> "Box":
        try:
            south, west, north, east = (float(part) for part in text.split(","))
        except ValueError as exc:
            raise fail("bbox", "wrong_type", "must be south,west,north,east in degrees", actual=text) from exc
        return Box.of(south, west, north, east)

    @staticmethod
    def of(south, west, north, east) -> "Box":
        box = Box(float(south), float(west), float(north), float(east))
        if not (-90 <= box.south < box.north <= 90 and -180 <= box.west < box.east <= 180):
            raise fail("bbox", "out_of_range", "south < north within ±90, west < east within ±180",
                       actual=[south, west, north, east])
        return box

    def as_json(self) -> dict:
        return {"south": self.south, "west": self.west, "north": self.north, "east": self.east}


class LocalFrame:
    """Degrees to local ENU metres about an origin (and the area's size)."""

    def __init__(self, box: Box):
        self.lat0 = (box.south + box.north) / 2
        self.lon0 = (box.west + box.east) / 2
        phi = math.radians(self.lat0)
        w = math.sqrt(1 - _E2 * math.sin(phi) ** 2)
        self.north_per_deg = math.radians(1) * _A * (1 - _E2) / w ** 3  # meridian radius
        self.east_per_deg = math.radians(1) * _A / w * math.cos(phi)  # prime vertical radius x cos
        self.size_east = round((box.east - box.west) * self.east_per_deg, 3)
        self.size_north = round((box.north - box.south) * self.north_per_deg, 3)

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.east_per_deg, (lat - self.lat0) * self.north_per_deg)


@dataclass
class Feature:
    """One map feature in degrees: a building's outer rings or a road's lines."""

    kind: str  # "building" or "road"
    provider: str
    source_kind: str  # way, relation, feature
    source_id: str
    tags: dict
    rings: list[list[tuple[float, float]]] = field(default_factory=list)  # (lat, lon)
    lines: list[list[tuple[float, float]]] = field(default_factory=list)


@dataclass
class Report:
    buildings: int = 0
    roads: int = 0
    skipped: list[dict] = field(default_factory=list)
    assumed: dict = field(default_factory=lambda: {"building_height": 0, "road_width": 0, "road_lanes": 0})
    notes: list[str] = field(default_factory=list)

    def skip(self, feature: Feature, reason: str) -> None:
        self.skipped.append({"source": f"{feature.source_kind}/{feature.source_id}", "kind": feature.kind,
                             "reason": reason})

    def as_json(self) -> dict:
        return {"buildings": self.buildings, "roads": self.roads, "skipped": self.skipped,
                "assumed": self.assumed, "notes": self.notes}


# --- Reading map data ---------------------------------------------------------------

EXCLUDED_HIGHWAYS = {
    "footway", "path", "cycleway", "steps", "pedestrian", "track", "bridleway", "corridor", "platform",
    "construction", "proposed", "abandoned", "bus_stop", "elevator", "via_ferrata", "raceway", "escape",
    "bus_guideway", "services", "rest_area",
}


def _is_road(tags: dict) -> bool:
    highway = tags.get("highway")
    return bool(highway) and highway not in EXCLUDED_HIGHWAYS and tags.get("area") != "yes"


def _is_building(tags: dict) -> bool:
    return bool(tags.get("building")) and tags.get("building") != "no"


def features_from_overpass(data: dict) -> list[Feature]:
    """Buildings (closed ways, multipolygon relations' outer rings) and roads
    (highway ways) from Overpass API JSON (`out body; >; out skel qt;`)."""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise fail("osm", "wrong_type", "Overpass JSON has an elements list", expected="{elements: [...]}")
    nodes, ways = {}, {}
    for element in data["elements"]:
        if element.get("type") == "node" and "lat" in element:
            nodes[element["id"]] = (element["lat"], element["lon"])
        elif element.get("type") == "way":
            ways[element["id"]] = element
    features: list[Feature] = []

    def points(way) -> list[tuple[float, float]] | None:
        found = [nodes.get(node) for node in way.get("nodes", [])]
        return None if any(point is None for point in found) else found

    for element in data["elements"]:
        tags = element.get("tags") or {}
        if element.get("type") == "way":
            line = points(element)
            if _is_building(tags):
                feature = Feature("building", "openstreetmap", "way", str(element["id"]), tags)
                if line is None or len(line) < 4 or line[0] != line[-1]:
                    feature.rings = []
                else:
                    feature.rings = [line]
                features.append(feature)
            elif _is_road(tags):
                feature = Feature("road", "openstreetmap", "way", str(element["id"]), tags)
                feature.lines = [line] if line else []
                features.append(feature)
        elif element.get("type") == "relation" and _is_building(tags) and tags.get("type") == "multipolygon":
            outer = [points(ways[m["ref"]]) for m in element.get("members", [])
                     if m.get("type") == "way" and m.get("role") == "outer" and m.get("ref") in ways]
            feature = Feature("building", "openstreetmap", "relation", str(element["id"]), tags)
            feature.rings = _rings([line for line in outer if line])
            if any(m.get("role") == "inner" for m in element.get("members", [])):
                feature.tags = {**tags, "_inner_ignored": "yes"}
            features.append(feature)
    return features


def _rings(lines: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join way pieces end to end into closed rings (multipolygon members)."""
    lines = [list(line) for line in lines]
    rings = []
    while lines:
        ring = lines.pop(0)
        while ring[0] != ring[-1]:
            for index, line in enumerate(lines):
                if line[0] == ring[-1]:
                    ring += line[1:]
                elif line[-1] == ring[-1]:
                    ring += list(reversed(line))[1:]
                else:
                    continue
                del lines[index]
                break
            else:
                break  # an open ring: left out
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def features_from_geojson(data: dict) -> list[Feature]:
    """Buildings (Polygon / MultiPolygon with a building property) and roads
    (LineString / MultiLineString with a highway property); coordinates are
    [lon, lat] as GeoJSON has them."""
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise fail("geojson", "wrong_type", "must be a GeoJSON FeatureCollection", expected="FeatureCollection")
    features = []
    for index, item in enumerate(data.get("features") or []):
        geometry = item.get("geometry") or {}
        tags = {key: value for key, value in (item.get("properties") or {}).items()
                if isinstance(value, (str, int, float)) and not isinstance(value, bool)}
        raw_id = item.get("id", tags.get("@id", tags.get("id", index)))
        match = re.fullmatch(r"(way|relation|node)/(\d+)", str(raw_id))
        source_kind, source_id = (match.group(1), match.group(2)) if match else ("feature", str(raw_id))
        kind = geometry.get("type")
        latlon = lambda coords: [(float(lat), float(lon)) for lon, lat, *_ in coords]  # noqa: E731
        if _is_building(tags) and kind in ("Polygon", "MultiPolygon"):
            polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"]
            feature = Feature("building", "geojson", source_kind, source_id, tags,
                              rings=[latlon(polygon[0]) for polygon in polygons if polygon])
            if any(len(polygon) > 1 for polygon in polygons):
                feature.tags = {**tags, "_inner_ignored": "yes"}
            features.append(feature)
        elif _is_road(tags) and kind in ("LineString", "MultiLineString"):
            lines = [geometry["coordinates"]] if kind == "LineString" else geometry["coordinates"]
            features.append(Feature("road", "geojson", source_kind, source_id, tags,
                                    lines=[latlon(line) for line in lines]))
    return features


def geojson_box(data: dict) -> Box:
    """The bbox of every coordinate in a FeatureCollection (or its own bbox)."""
    if isinstance(data.get("bbox"), list) and len(data["bbox"]) == 4:
        west, south, east, north = data["bbox"]
        return Box.of(south, west, north, east)
    lats, lons = [], []

    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            lons.append(coords[0])
            lats.append(coords[1])
        else:
            for item in coords:
                walk(item)

    for item in data.get("features") or []:
        walk((item.get("geometry") or {}).get("coordinates") or [])
    if not lats:
        raise fail("geojson", "missing_field", "no coordinates to take the area from; pass a bbox")
    return Box.of(min(lats), min(lons), max(lats), max(lons))


def overpass_query(box: Box) -> str:
    area = f"{box.south},{box.west},{box.north},{box.east}"
    return (f"[out:json][timeout:{OVERPASS_TIMEOUT_S - 10}];\n"
            f"(\n  way[\"building\"]({area});\n  relation[\"building\"][\"type\"=\"multipolygon\"]({area});\n"
            f"  way[\"highway\"]({area});\n);\nout body;\n>;\nout skel qt;")


def fetch_overpass(box: Box, endpoint: str | None = None) -> dict:
    """The buildings and roads in the bbox from an Overpass API instance."""
    import env_version

    endpoint = endpoint or os.environ.get("HAKONIWA_OVERPASS_URL") or DEFAULT_OVERPASS
    request = Request(endpoint, data=urlencode({"data": overpass_query(box)}).encode(), method="POST", headers={
        "User-Agent": f"hakoniwa-environment-studio/{env_version.VERSION} (map import)",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=OVERPASS_TIMEOUT_S) as response:
            return json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise fail("overpass", "fetch_error",
                   f"cannot get map data from {endpoint}: {exc}", actual=endpoint) from exc


# --- Converting to a Recipe -----------------------------------------------------------

_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.IGNORECASE)


def parse_length(value) -> float | None:
    """'12', '12 m', '12.5m', "40'" (feet) -> metres; None when unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _NUMBER.match(str(value or "").replace(",", "."))
    if not match:
        return None
    number = float(match.group(1))
    return number * 0.3048 if (match.group(2) or "").lower() in ("ft", "feet", "'") else number


def building_height(tags: dict) -> tuple[float, bool]:
    """(height, assumed): the height tag, else levels, else by kind."""
    height = parse_length(tags.get("height"))
    if height and height > 0:
        return height, False
    levels = parse_length(tags.get("building:levels"))
    if levels and levels > 0:
        return levels * LEVEL_HEIGHT_M + (1.0 if tags.get("roof:shape") not in (None, "flat") else 0.0), True
    return HEIGHTS_BY_KIND.get(str(tags.get("building")), DEFAULT_HEIGHT_M), True


def building_base(tags: dict, height: float) -> float:
    """Where a building starts above the ground: min_height, else its lowest
    level, else just under its roof for a canopy (building=roof); else 0."""
    base = parse_length(tags.get("min_height"))
    if base is None:
        levels = parse_length(tags.get("building:min_level"))
        base = levels * LEVEL_HEIGHT_M if levels is not None else None
    if base is None and tags.get("building") == "roof":
        base = height - ROOF_SLAB_M
    return max(0.0, min(base or 0.0, height - ROOF_SLAB_M))


def road_size(tags: dict) -> tuple[float, int, bool, bool]:
    """(width, lanes, width assumed, lanes assumed) of a road."""
    lanes = parse_length(tags.get("lanes"))
    lanes_assumed = not (lanes and lanes >= 1)
    lanes = int(lanes) if not lanes_assumed else LANES_BY_CLASS.get(str(tags.get("highway")), 2)
    width = parse_length(tags.get("width"))
    width_assumed = not (width and width > 0)
    if width_assumed:
        width = lanes * LANE_WIDTH_M
    return (min(max(width, MIN_ROAD_WIDTH_M), MAX_ROAD_WIDTH_M), min(max(lanes, 1), MAX_LANES),
            width_assumed, lanes_assumed)


def _mm(value: float) -> float:
    return round(value, 3) + 0.0


def _placed(points: list[tuple[float, float]]) -> tuple[dict, list[list[float]]]:
    """A pose at the middle of the points' box and the points about it."""
    xs, ys = [x for x, _ in points], [y for _, y in points]
    cx, cy = _mm((min(xs) + max(xs)) / 2), _mm((min(ys) + max(ys)) / 2)
    return {"x_m": cx, "y_m": cy, "yaw_deg": 0}, [[_mm(x - cx), _mm(y - cy)] for x, y in points]


def _tags(tags: dict) -> dict:
    """The tags worth keeping with an object (its kind, name, size, levels)."""
    keep = ("building", "highway", "name", "height", "min_height", "building:levels", "building:min_level",
            "roof:shape", "lanes", "width", "oneway",
            "surface", "bridge", "layer", "_inner_ignored")
    return {key: tags[key] for key in keep if key in tags}


def to_recipe(features: list[Feature], box: Box, *, name: str | None = None, catalog: str = "",
              items: dict | None = None, terrain_item: str = "city-ground", provider: str = "openstreetmap",
              data_timestamp: str | None = None, query: str | None = None) -> tuple[dict, Report]:
    """A Recipe mapping (and what was assumed or left out) from map features."""
    items = {"building": "building-footprint", "road": "road-path", **(items or {})}
    frame = LocalFrame(box)
    if max(frame.size_east, frame.size_north) > MAX_SIDE_M:
        raise fail("bbox", "out_of_range", f"the area is at most {MAX_SIDE_M:.0f} m on a side",
                   expected=f"<= {MAX_SIDE_M} m", actual={"east_m": frame.size_east, "north_m": frame.size_north})
    half_e, half_n = frame.size_east / 2, frame.size_north / 2
    area = (-half_e, -half_n, half_e, half_n)
    report = Report()
    objects = []
    used: set[str] = set()

    def unique(base: str) -> str:
        candidate, number = base, 2
        while candidate in used:
            candidate, number = f"{base}-{number}", number + 1
        used.add(candidate)
        return candidate

    order = {"building": 0, "road": 1}
    for feature in sorted(features, key=lambda f: (order[f.kind], f.source_kind, int(f.source_id)
                                                     if f.source_id.isdigit() else 0, f.source_id)):
        prefix = "building" if feature.kind == "building" else "road"
        base_id = re.sub(r"[^a-z0-9_-]", "-", f"{prefix}-{feature.source_kind[0] if feature.source_kind == 'relation' else ''}"
                         f"{feature.source_id}".lower())[:56]
        source = {"provider": feature.provider, "kind": feature.source_kind, "id": feature.source_id}
        tags = _tags(feature.tags)
        if tags:
            source["tags"] = tags
        if feature.kind == "building":
            if feature.tags.get("_inner_ignored"):
                report.notes.append(f"{feature.source_kind}/{feature.source_id}: courtyards (inner rings) are not cut out")
            if not feature.rings:
                report.skip(feature, "its outline is incomplete in the data")
                continue
            height, assumed = building_height(feature.tags)
            height = min(max(_mm(height), 1.0), 500.0)
            base = _mm(building_base(feature.tags, height))
            for number, ring in enumerate(feature.rings, 1):
                points = env_polygon.cleaned([frame.xy(lat, lon) for lat, lon in ring], True, MIN_STEP_M)
                if len(points) >= 3 and env_polygon.signed_area(points) < 0:
                    points.reverse()
                points = env_polygon.cleaned(env_polygon.clip_polygon(points, area), True, MIN_STEP_M)
                if len(points) < 3:
                    report.skip(feature, "outside the area")
                    continue
                pose, footprint = _placed(points)
                local = [(x, y) for x, y in footprint]
                if abs(env_polygon.signed_area(local)) < MIN_BUILDING_AREA_M2:
                    report.skip(feature, f"smaller than {MIN_BUILDING_AREA_M2} m² in the area")
                    continue
                if not env_polygon.is_simple(local):
                    report.skip(feature, "its outline crosses itself once clipped to the area")
                    continue
                objects.append({"id": unique(base_id if len(feature.rings) == 1 else f"{base_id}-{number}"),
                                "item": items["building"], "pose": pose,
                                "params": {"footprint": footprint, "height_m": height,
                                           **({"min_height_m": base} if base > 0 else {})},
                                "source": source})
                report.buildings += 1
                report.assumed["building_height"] += int(assumed)
        else:
            width, lanes, width_assumed, lanes_assumed = road_size(feature.tags)
            # Clipped half its width inside the edges, so its ends stay in the area.
            inset = width / 2 + 0.001
            inner = (area[0] + inset, area[1] + inset, area[2] - inset, area[3] - inset)
            parts = [part for line in feature.lines
                     for part in env_polygon.clip_polyline([frame.xy(lat, lon) for lat, lon in line], inner)]
            kept = 0
            for part in parts:
                points = env_polygon.cleaned(part, False, MIN_STEP_M)
                length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
                if len(points) < 2 or length < MIN_ROAD_LENGTH_M:
                    continue
                pose, centerline = _placed(points)
                kept += 1
                objects.append({"id": unique(base_id if len(parts) == 1 else f"{base_id}-{kept}"),
                                "item": items["road"], "pose": pose,
                                "params": {"centerline": centerline, "width_m": _mm(width), "lanes": lanes},
                                "source": source})
            if not kept:
                report.skip(feature, "outside the area" if not parts else f"shorter than {MIN_ROAD_LENGTH_M} m in the area")
                continue
            report.roads += kept
            report.assumed["road_width"] += int(width_assumed) * kept
            report.assumed["road_lanes"] += int(lanes_assumed) * kept
    geo = {"provider": provider, "origin": {"lat_deg": round(frame.lat0, 9), "lon_deg": round(frame.lon0, 9)},
           "bbox_deg": box.as_json(), "projection": PROJECTION}
    if provider == "openstreetmap":
        geo.update(attribution=ATTRIBUTION, license=LICENSE)
    if data_timestamp:
        geo["data_timestamp"] = data_timestamp
    if query:
        geo["query"] = query
    recipe = {
        "schema": env_schema.RECIPE_SCHEMA,
        "name": name or f"地図から（{box.south:.5f}, {box.west:.5f}）",
        "description": f"Buildings and roads from {provider} map data ({ATTRIBUTION if provider == 'openstreetmap' else provider}).",
        "catalog": catalog,
        "geo": geo,
        "size_m": {"east": frame.size_east, "north": frame.size_north},
        "terrain": {"item": terrain_item},
        "objects": objects,
    }
    return recipe, report


def import_map(box: Box | None, *, overpass: bool = False, osm_json: dict | None = None,
               geojson: dict | None = None, endpoint: str | None = None, **options) -> tuple[dict, Report, dict]:
    """(Recipe mapping, report, the raw map data) from one of the sources."""
    if geojson is not None:
        box = box or geojson_box(geojson)
        features = features_from_geojson(geojson)
        recipe, report = to_recipe(features, box, provider=options.pop("provider", "geojson"), **options)
        return recipe, report, geojson
    if box is None:
        raise fail("bbox", "missing_field", "the area to import, south,west,north,east")
    data = osm_json if osm_json is not None else fetch_overpass(box, endpoint) if overpass else None
    if data is None:
        raise fail("source", "missing_field", "a map data source: --overpass, --osm-json or --geojson")
    timestamp = (data.get("osm3s") or {}).get("timestamp_osm_base")
    recipe, report = to_recipe(features_from_overpass(data), box, data_timestamp=timestamp,
                               query=overpass_query(box) if overpass else None, **options)
    return recipe, report, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bbox", help="south,west,north,east in degrees")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--overpass", action="store_true", help="fetch OpenStreetMap data from the Overpass API")
    source.add_argument("--osm-json", type=Path, help="an Overpass JSON file")
    source.add_argument("--geojson", type=Path, help="a GeoJSON FeatureCollection")
    parser.add_argument("--endpoint", help=f"Overpass API URL (default {DEFAULT_OVERPASS} or HAKONIWA_OVERPASS_URL)")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--terrain", default="city-ground", help="the terrain item")
    parser.add_argument("--name")
    parser.add_argument("--out", type=Path, required=True, help="the Recipe YAML to write")
    parser.add_argument("--save-data", type=Path, help="also keep the map data as fetched (to re-import later)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        box = Box.parse(args.bbox) if args.bbox else None
        out = args.out.resolve()
        catalog = Path(os.path.relpath(args.catalog.resolve(), out.parent)).as_posix()
        read = lambda path: json.loads(path.read_text(encoding="utf-8"))  # noqa: E731
        recipe, report, data = import_map(
            box, overpass=args.overpass, osm_json=read(args.osm_json) if args.osm_json else None,
            geojson=read(args.geojson) if args.geojson else None, endpoint=args.endpoint,
            name=args.name, catalog=catalog, terrain_item=args.terrain)
        env_schema.parse_recipe(recipe, out)  # the Recipe must be valid before it is written
    except DiagnosticError as error:
        print(json.dumps({"ok": False, "diagnostics": [item.as_json() for item in error.diagnostics]},
                         ensure_ascii=False, indent=2))
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    if args.save_data:
        args.save_data.parent.mkdir(parents=True, exist_ok=True)
        args.save_data.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = {"ok": True, "recipe": str(out), "size_m": recipe["size_m"], **report.as_json()}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"OK  {out}: {report.buildings} buildings, {report.roads} roads, "
              f"{recipe['size_m']['east']} x {recipe['size_m']['north']} m; skipped {len(report.skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
