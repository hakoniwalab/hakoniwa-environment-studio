#!/usr/bin/env python3
"""Load and check Environment Catalogs and Recipes (docs/data-contract.md).

Type    (types/*.yaml, env_types.py): what a part is, how it behaves, its shape.
Catalog (catalogs/<id>/catalog.yaml):  items, each a type with parameter values.
Recipe  (recipes/*.yaml):              one environment: its size, its terrain
        (a terrain item) and its objects (items placed with a pose and the
        parameters their type lets a placement change).

Metres and degrees, ENU (x east, y north, z up), origin at the environment's
centre, yaw counter-clockwise from east. A check reports every problem it
finds as a diagnostic (env_diagnostics.py), not only the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import functools
import math
from pathlib import Path
import re

import yaml

import env_polygon
import env_types
from env_diagnostics import Collector, DiagnosticError, fail, mapping, only
from env_terrain import Terrain, make_terrain
from env_types import EnvType, Param, Shape

CATALOG_SCHEMA = "hakoniwa.environment-catalog/v1"
RECIPE_SCHEMA = "hakoniwa.environment-recipe/v1"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SIZE_M = 10_000.0
# Sides of the polygon that stands for a circle on the plan (web/geometry.js too).
CIRCLE_SEGMENTS = 32
ITEM_KEYS = {"id", "type", "extends", "name", "description", "category", "params", "source", "assumed"}
RECIPE_KEYS = {"schema", "name", "description", "catalog", "size_m", "terrain", "objects", "geo"}
OBJECT_KEYS = {"id", "item", "pose", "params", "source"}
# Provenance of a Recipe made from map data (#10): where its origin is on the
# Earth and where the data came from. Kept as it is; it does not change the world.
GEO_KEYS = {"provider", "origin", "bbox_deg", "projection", "attribution", "license", "data_timestamp", "query"}
# Provenance of one object (the map feature it was made from).
SOURCE_KEYS = {"provider", "kind", "id", "tags", "note"}
POSE_KEYS = {"x_m", "y_m", "yaw_deg"}


@dataclass(frozen=True)
class Item:
    """A Catalog entry: a type with its parameter values."""

    id: str
    name: str
    description: str
    type: EnvType
    params: dict  # every parameter's value (placement ones are the defaults)
    placement_params: dict[str, Param]  # what a placement may change, within these ranges
    shape: Shape | None  # resolved with params (None for a terrain item)
    category: str
    source: dict = field(default_factory=dict)
    assumed: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description, "type": self.type.id,
            "kind": "terrain" if self.type.is_terrain else "object", "category": self.category,
            "id_prefix": self.type.id_prefix, "params": self.params,
            "param_labels": {name: param.label for name, param in self.type.params.items()},
            "placement_params": [param.as_json() for param in self.placement_params.values()],
            **(self.shape.as_json() if self.shape else {}),
            "source": self.source, "assumed": self.assumed,
        }


@dataclass(frozen=True)
class Catalog:
    path: Path
    items: dict[str, Item]
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Pose:
    x_m: float
    y_m: float
    z_m: float  # the object's base above the datum: the terrain under it (+ z_m when elevated)
    yaw_deg: float


@dataclass(frozen=True)
class EnvObject:
    """A placed item resolved to a shape."""

    id: str
    item: str
    type: str
    pose: Pose
    params: dict
    shape: Shape
    source: dict | None = None

    @property
    def solids(self):
        return self.shape.solids

    @property
    def color(self) -> str:
        return self.params.get("color", "#cccccc")


@dataclass(frozen=True)
class Recipe:
    path: Path
    name: str
    description: str
    catalog: Catalog
    size_east_m: float
    size_north_m: float
    terrain_item: str
    terrain: Terrain
    objects: list[EnvObject] = field(default_factory=list)
    geo: dict | None = None


# --- Small checks ------------------------------------------------------------------

def _scalar_tags(value, path: str) -> dict:
    value = mapping(value, path)
    for key, tag in value.items():
        if not isinstance(key, str) or not isinstance(tag, (str, int, float, bool)):
            raise fail(f"{path}.{key}", "wrong_type", "tags are text keys with text or number values",
                       expected="{key: value}", actual=tag)
    return dict(value)


def _source(value, path: str) -> dict:
    value = mapping(value, path)
    only(value, SOURCE_KEYS, path)
    source = {key: value[key] for key in sorted(value) if key != "tags"}
    for key, item in source.items():
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise fail(f"{path}.{key}", "wrong_type", "must be text or a whole number", actual=item)
    if "tags" in value:
        source["tags"] = _scalar_tags(value["tags"], f"{path}.tags")
    return source


def _geo(value, path: str) -> dict:
    value = mapping(value, path)
    only(value, GEO_KEYS, path)
    geo = dict(value)
    if "origin" in geo:
        origin = mapping(geo["origin"], f"{path}.origin")
        only(origin, {"lat_deg", "lon_deg"}, f"{path}.origin")
        for key, limit in (("lat_deg", 90), ("lon_deg", 180)):
            number = origin.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or abs(number) > limit:
                raise fail(f"{path}.origin.{key}", "out_of_range", f"degrees within ±{limit}",
                           expected=f"|value| <= {limit}", actual=number)
    if "bbox_deg" in geo:
        box = mapping(geo["bbox_deg"], f"{path}.bbox_deg")
        only(box, {"south", "west", "north", "east"}, f"{path}.bbox_deg")
        if set(box) != {"south", "west", "north", "east"} or not all(
                isinstance(box[key], (int, float)) and not isinstance(box[key], bool) for key in box):
            raise fail(f"{path}.bbox_deg", "wrong_type", "south, west, north and east in degrees",
                       expected="{south, west, north, east}", actual=box)
    for key in ("provider", "projection", "attribution", "license", "data_timestamp", "query"):
        if key in geo and not isinstance(geo[key], str):
            raise fail(f"{path}.{key}", "wrong_type", "must be text", actual=geo[key])
    return geo


def _identifier(value, path: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise fail(path, "wrong_type", "must be a lower-case id ([a-z0-9_-], up to 64 characters)",
                   expected="[a-z0-9][a-z0-9_-]*", actual=value)
    return value


def _metres(value, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise fail(path, "wrong_type", "must be a number of metres", expected="number", actual=value)
    if positive and value <= 0:
        raise fail(path, "out_of_range", "must be greater than zero", expected="> 0", actual=value)
    if abs(value) > MAX_SIZE_M:
        raise fail(path, "out_of_range", f"must be within {MAX_SIZE_M} m", expected=f"<= {MAX_SIZE_M}", actual=value)
    return float(value)


def _text(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise fail(path, "wrong_type", "must be a non-empty string", expected="text", actual=value)
    return value


def _load_yaml(path: Path, label: str) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise fail(str(path), "missing_field", f"cannot read the {label}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise fail(str(path), "wrong_type", f"invalid YAML in the {label}: {exc}") from exc
    return mapping(data, label)


@functools.lru_cache(maxsize=4)
def types(directory: Path = env_types.DEFAULT_TYPES) -> env_types.TypeLibrary:
    return env_types.load_types(directory)


# --- Catalog -----------------------------------------------------------------------

def _item(value, path: str, library: env_types.TypeLibrary, raws: dict[str, dict]) -> tuple[Item, dict]:
    value = mapping(value, path)
    only(value, ITEM_KEYS, path)
    item_id = _identifier(value.get("id"), f"{path}.id")
    raw: dict = {"params": {}, "source": {}, "assumed": {}}
    if "extends" in value:
        parent = raws.get(value["extends"])
        if parent is None:
            raise fail(f"{path}.extends", "unknown_reference", "not an earlier item of this Catalog",
                       expected=sorted(raws), actual=value["extends"])
        raw = {key: (dict(item) if isinstance(item, dict) else item) for key, item in parent.items()}
    for key in ("params", "source", "assumed"):
        raw[key] = {**raw.get(key, {}), **mapping(value.get(key, {}), f"{path}.{key}")}
    for key in ("type", "name", "description", "category"):
        if key in value:
            raw[key] = value[key]
    if "type" not in raw:
        raise fail(f"{path}.type", "missing_field", "an item needs a type (or extends an item that has one)")
    env_type = library.get(raw["type"], f"{path}.type")
    params = env_types.defaults(env_type)
    placement = {name: param for name, param in env_type.params.items() if param.level == "placement"}
    problems = Collector()
    for name, given in raw["params"].items():
        param = env_type.params.get(name)
        where = f"{path}.params.{name}"
        if param is None:
            problems.add(where, "unknown_reference", f"type {env_type.id} has no parameter {name!r}",
                         expected=sorted(env_type.params), actual=name)
            continue
        if param.level == "type":
            problems.add(where, "not_allowed", f"fixed by type {env_type.id}", actual=given)
            continue
        if param.level == "placement" and isinstance(given, dict):
            narrowed = problems.check(param.narrowed, given, where)
            if narrowed is not None:
                placement[name] = narrowed
                if narrowed.default is not None:
                    params[name] = narrowed.default
        else:
            checked = problems.check(param.check, given, where)
            if checked is not None:
                params[name] = checked
                if param.level == "placement":
                    placement[name] = placement[name].with_default(checked)
    for name in sorted(set(env_type.params) - set(params)):
        problems.add(f"{path}.params.{name}", "missing_field", f"type {env_type.id} needs {name!r}",
                     expected=env_type.params[name].as_json())
    for key, reason in raw["assumed"].items():
        if key not in env_type.params:
            problems.add(f"{path}.assumed.{key}", "unknown_reference", "names no parameter", expected=sorted(env_type.params), actual=key)
        elif not isinstance(reason, str) or not reason.strip():
            problems.add(f"{path}.assumed.{key}", "missing_field", "must say why the value is assumed")
    problems.raise_if_errors()
    shape = None if env_type.is_terrain else env_types.resolve_shape(env_type, params, path)
    item = Item(
        id=item_id, name=_text(raw.get("name", item_id), f"{path}.name"),
        description=str(raw.get("description", env_type.description)), type=env_type,
        params=params, placement_params=placement, shape=shape,
        category=_text(raw.get("category", env_type.label), f"{path}.category"),
        source=dict(raw["source"]), assumed=dict(raw["assumed"]),
    )
    return item, raw


def parse_catalog(data: dict, path: Path, library: env_types.TypeLibrary | None = None) -> Catalog:
    library = library or types()
    problems = Collector()
    problems.check(only, data, {"schema", "catalog", "items"}, "")
    if data.get("schema") != CATALOG_SCHEMA:
        problems.add("schema", "wrong_schema", "wrong schema", expected=CATALOG_SCHEMA, actual=data.get("schema"))
    meta = problems.check(mapping, data.get("catalog", {}), "catalog") or {}
    problems.check(only, meta, {"id", "name", "description", "source"}, "catalog")
    entries = data.get("items")
    items: dict[str, Item] = {}
    raws: dict[str, dict] = {}
    if not isinstance(entries, list) or not entries:
        problems.add("items", "missing_field", "must be a non-empty list", expected="list")
        entries = []
    for index, entry in enumerate(entries):
        result = problems.check(_item, entry, f"items[{index}]", library, raws)
        if result is None:
            continue
        item, raw = result
        if item.id in items:
            problems.add(f"items[{index}].id", "duplicate_id", "used twice", actual=item.id)
            continue
        items[item.id] = item
        raws[item.id] = raw
    problems.raise_if_errors()
    return Catalog(path=path, items=items, meta=dict(meta))


def load_catalog(path: Path) -> Catalog:
    path = Path(path)
    return parse_catalog(_load_yaml(path, "catalog"), path.resolve())


# --- Recipe ------------------------------------------------------------------------

def placement_values(item: Item, given, path: str) -> dict:
    """The item's parameters with a placement's own values, each checked."""
    params = dict(item.params)
    problems = Collector()
    for name, value in mapping(given, path).items():
        param = item.placement_params.get(name)
        if param is None:
            problems.add(f"{path}.{name}", "not_allowed", f"{item.id} does not let a placement change {name!r}",
                         expected=sorted(item.placement_params), actual=name)
            continue
        checked = problems.check(param.check, value, f"{path}.{name}")
        if checked is not None:
            params[name] = checked
    problems.raise_if_errors()
    return params


def resolve_placement(item: Item, given, path: str) -> tuple[dict, Shape]:
    params = placement_values(item, given, path)
    shape = item.shape if params == item.params else env_types.resolve_shape(item.type, params, path)
    return params, shape


def _terrain(value, path: str, catalog: Catalog, size_east: float, size_north: float,
             base_dir: Path | None = None) -> tuple[str, Terrain]:
    value = mapping(value, path)
    only(value, {"item", "params"}, path)
    item = catalog.items.get(value.get("item"))
    terrains = sorted(key for key, entry in catalog.items.items() if entry.type.is_terrain)
    if item is None or not item.type.is_terrain:
        raise fail(f"{path}.item", "unknown_reference", "not a terrain item of the catalog", expected=terrains,
                   actual=value.get("item"))
    params = placement_values(item, value.get("params", {}), f"{path}.params")
    settings = env_types.terrain_settings(item.type, params, path)
    return item.id, make_terrain(settings, params, size_east, size_north, f"{path}.params", base_dir)


def _object(value, path: str, catalog: Catalog, terrain: Terrain) -> EnvObject:
    value = mapping(value, path)
    only(value, OBJECT_KEYS, path)
    object_id = _identifier(value.get("id"), f"{path}.id")
    item = catalog.items.get(value.get("item"))
    if item is None or item.type.is_terrain:
        raise fail(f"{path}.item", "unknown_reference", "not an object item of the catalog",
                   expected=sorted(key for key, entry in catalog.items.items() if not entry.type.is_terrain),
                   actual=value.get("item"))
    pose = mapping(value.get("pose"), f"{path}.pose")
    problems = Collector()
    problems.check(only, pose, POSE_KEYS, f"{path}.pose")
    x = problems.check(_metres, pose.get("x_m"), f"{path}.pose.x_m")
    y = problems.check(_metres, pose.get("y_m"), f"{path}.pose.y_m")
    yaw = pose.get("yaw_deg", 0)
    if isinstance(yaw, bool) or not isinstance(yaw, (int, float)) or not math.isfinite(yaw):
        problems.add(f"{path}.pose.yaw_deg", "wrong_type", "must be a number of degrees", expected="number", actual=yaw)
        yaw = 0
    resolved = problems.check(resolve_placement, item, value.get("params", {}), f"{path}.params")
    source = problems.check(_source, value["source"], f"{path}.source") if "source" in value else None
    problems.raise_if_errors()
    params, shape = resolved
    obj = EnvObject(id=object_id, item=item.id, type=item.type.id, pose=Pose(x, y, 0.0, float(yaw) % 360.0),
                    params=params, shape=shape, source=source)
    # Set on the terrain: its base at the highest ground under its outline
    # (plus its own height above the terrain when elevated). On a height field
    # it keeps HFIELD_CLEARANCE_M above that: the sampled height (bilinear,
    # edges sampled every grid step) can be a few mm under MuJoCo's triangles.
    ground = terrain.highest_under(footprint(obj))
    if terrain.kind == "hfield":
        ground += HFIELD_CLEARANCE_M
    z = ground + (params["z_m"] if shape.surface == "elevated" else 0.0)
    return replace(obj, pose=replace(obj.pose, z_m=round(z, 6)))


def _solid_outlines(obj: EnvObject) -> list[tuple[list[tuple[float, float]], float]]:
    """(outline in the environment frame, top above the object's base) of each colliding solid."""
    return [(placed(obj, solid.outline()), solid.z_m + solid.height_m / 2)
            for solid in obj.solids if solid.collide]


HFIELD_CLEARANCE_M = 0.005

# Objects on a surface object float this far above its top: exactly touching
# meshes make MuJoCo's convex collision pick a sideways normal and report a
# deep overlap. Well within the checks' 1 mm tolerance.
SURFACE_GAP_M = 0.0005


def _on_surfaces(objects: list[EnvObject]) -> list[EnvObject]:
    """Objects standing on the ground stand on top of the surface objects
    (roads) under them, as they do on the terrain: a cone on a road stands on
    the road, not in it."""
    surfaces = [(obj, _solid_outlines(obj)) for obj in objects if obj.shape.layer == "surface"]
    if not surfaces:
        return objects
    placed_objects = []
    for obj in objects:
        if obj.shape.layer == "surface" or obj.shape.surface != "ground":
            placed_objects.append(obj)
            continue
        mine = [outline for outline, _ in _solid_outlines(obj)]
        z = obj.pose.z_m
        for surface, outlines in surfaces:
            for polygon, top in outlines:
                if any(env_polygon.convex_overlap(outline, polygon) for outline in mine):
                    z = max(z, surface.pose.z_m + top + SURFACE_GAP_M)
        placed_objects.append(obj if z == obj.pose.z_m else replace(obj, pose=replace(obj.pose, z_m=round(z, 6))))
    return placed_objects


def parse_recipe(data: dict, path: Path, catalog: Catalog | None = None) -> Recipe:
    """Parse a Recipe; its catalog path resolves against the Recipe file unless given."""
    problems = Collector()
    problems.check(only, data, RECIPE_KEYS, "")
    if data.get("schema") != RECIPE_SCHEMA:
        problems.add("schema", "wrong_schema", "wrong schema", expected=RECIPE_SCHEMA, actual=data.get("schema"))
    if catalog is None:
        reference = data.get("catalog")
        if not isinstance(reference, str) or not reference:
            problems.add("catalog", "missing_field", "the path of the Catalog YAML", expected="path")
            problems.raise_if_errors()
        catalog = load_catalog((path.parent / reference).resolve())
    name = problems.check(_text, data.get("name", path.stem), "name") or path.stem
    geo = problems.check(_geo, data["geo"], "geo") if "geo" in data else None
    size = problems.check(mapping, data.get("size_m"), "size_m") or {}
    problems.check(only, size, {"east", "north"}, "size_m")
    size_east = problems.check(_metres, size.get("east"), "size_m.east", positive=True)
    size_north = problems.check(_metres, size.get("north"), "size_m.north", positive=True)
    problems.raise_if_errors()
    terrain_result = problems.check(_terrain, data.get("terrain"), "terrain", catalog, size_east, size_north,
                                    path.parent)
    problems.raise_if_errors()
    terrain_item, terrain = terrain_result
    entries = data.get("objects", [])
    objects: list[EnvObject] = []
    seen: set[str] = set()
    if not isinstance(entries, list):
        problems.add("objects", "wrong_type", "must be a list", expected="list")
        entries = []
    for index, entry in enumerate(entries):
        obj = problems.check(_object, entry, f"objects[{index}]", catalog, terrain)
        if obj is None:
            continue
        if obj.id in seen:
            problems.add(f"objects[{index}].id", "duplicate_id", "used twice", actual=obj.id)
            continue
        seen.add(obj.id)
        objects.append(obj)
    problems.raise_if_errors()
    objects = _on_surfaces(objects)
    return Recipe(path=path, name=name, description=str(data.get("description", "")), catalog=catalog,
                  size_east_m=size_east, size_north_m=size_north, terrain_item=terrain_item, terrain=terrain,
                  objects=objects, geo=geo)


def load_recipe(path: Path) -> Recipe:
    path = Path(path).resolve()
    return parse_recipe(_load_yaml(path, "recipe"), path)


# --- Geometry in the environment frame ---------------------------------------------

def _outline(primitive: str, width: float, depth: float) -> list[tuple[float, float]]:
    half_w, half_d = width / 2.0, depth / 2.0
    if primitive == "cylinder":
        return [(half_w * math.cos(2 * math.pi * i / CIRCLE_SEGMENTS), half_d * math.sin(2 * math.pi * i / CIRCLE_SEGMENTS))
                for i in range(CIRCLE_SEGMENTS)]
    return [(-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d)]


def placed(obj: EnvObject, local: list[tuple[float, float]]) -> list[tuple[float, float]]:
    yaw = math.radians(obj.pose.yaw_deg)
    cos, sin = math.cos(yaw), math.sin(yaw)
    return [(obj.pose.x_m + cos * x - sin * y, obj.pose.y_m + sin * x + cos * y) for x, y in local]


def footprint(obj: EnvObject) -> list[tuple[float, float]]:
    """The object's outline (its envelope) on the ground, counter-clockwise."""
    envelope = obj.shape.envelope
    return placed(obj, _outline(envelope["primitive"], envelope["width_m"], envelope["depth_m"]))


def main(argv: list[str] | None = None) -> int:
    import envstudio  # the command line lives in tools/envstudio.py

    return envstudio.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CATALOG_SCHEMA", "RECIPE_SCHEMA", "Catalog", "DiagnosticError", "EnvObject", "Item", "Pose", "Recipe",
           "footprint", "load_catalog", "load_recipe", "parse_catalog", "parse_recipe", "placed", "placement_values",
           "resolve_placement", "types"]
