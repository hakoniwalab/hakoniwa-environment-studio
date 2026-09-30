#!/usr/bin/env python3
"""Environment Types: what a part of an environment is, how it behaves and
how its shape (or, for a terrain, its ground) is built (docs/data-contract.md).

A type (types/*.yaml) declares
  params    the values that make an object: kind, unit, range, default, a
            description, and who sets them (level: type / item = the Catalog /
            placement = the Recipe);
  behavior  how the engine treats it: surface (ground: stands on the terrain,
            elevated: at its z_m above the terrain) and snap;
  shapes    the solids (box, upright cylinder, wedge) its collision and look
            are built from, each with a position and a tilt, written with small
            expressions of the params;
  terrain   for a terrain type instead of shapes: flat, or an hfield made by a
            named generator from the params;
  envelope  its outline on the plan, when not the solids' own.

Types extend one another. This module resolves inheritance and turns a type
with parameter values into solids (or a terrain). It is the only place
expressions are evaluated; the browser gets resolved shapes from the server.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
import math
from pathlib import Path
import re

import yaml

import env_polygon
import env_rules
from env_diagnostics import DiagnosticError, fail, mapping, only, load_yaml_text

TYPES_SCHEMA = "hakoniwa.environment-types/v1"
DEFAULT_TYPES = Path(__file__).resolve().parents[1] / "types"
ID_PATTERN = env_rules.ID_PATTERN
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
PARAM_KINDS = {"length", "angle", "number", "integer", "color", "enum", "bool", "text", "polygon", "polyline",
               "polygons"}
UNITS = {"length": "m", "angle": "deg", "polygon": "m", "polyline": "m", "polygons": "m"}
# Points a polygon (footprint) or polyline (centre line) parameter may hold.
MAX_POINTS = 2000
# Polygons a "polygons" parameter may hold (a City World's road network outlines).
MAX_POLYGONS = 20000
LEVELS = ("type", "item", "placement")
SURFACES = {"ground", "elevated"}
PRIMITIVES = {"box", "cylinder", "wedge", "prism", "ribbon"}
# Objects on the "surface" layer (roads, markings) lie on the ground and may
# overlap one another (roads meet at crossings); everything else collides.
LAYERS = {"object", "surface"}
TERRAIN_KINDS = {"flat", "hfield"}
# Terrain generators the engine provides (env_terrain.py), with the parameter
# names each one reads.
TERRAIN_GENERATORS = {"flat": set(), "hills": {"max_height_m", "hills", "radius_m", "seed", "resolution_m"},
                      "envsim": {"dem", "visual"}}
TYPE_KEYS = {"id", "label", "description", "abstract", "extends", "id_prefix", "params", "behavior", "shapes",
             "envelope", "terrain"}
PARAM_KEYS = {"kind", "level", "label", "description", "unit", "default", "min", "max", "values"}
SHAPE_KEYS = {"name", "primitive", "w", "d", "h", "x", "y", "z", "roll", "pitch", "yaw", "color", "collide",
              "visible", "when", "points", "polygons", "holes"}
BEHAVIOR_KEYS = {"surface", "snap", "friction", "layer", "locked"}
# Largest size or position accepted, in metres (a sanity bound).
MAX_M = 100_000.0


# --- Expressions ---------------------------------------------------------------------
#
# "$height_m - $top_m", "0.72 * min($width_m, $depth_m)", "cond($lit, 1, 0)".
# Numbers, strings, parameters, + - * /, comparisons, and / or / not, and the
# functions below. Parsed with ast and walked here (never eval).

_PARAM = re.compile(r"\$([a-z_][a-z0-9_]*)")
_FUNCTIONS = {
    "min": min, "max": max, "abs": abs, "sqrt": math.sqrt,
    # Trigonometry in degrees, as the rest of the contract.
    "sin": lambda deg: math.sin(math.radians(deg)), "cos": lambda deg: math.cos(math.radians(deg)),
    "tan": lambda deg: math.tan(math.radians(deg)),
}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
)


@dataclass(frozen=True)
class Expression:
    source: str
    tree: ast.Expression
    names: frozenset[str]


def compile_expression(value, path: str):
    """A literal stays as it is; a string with $parameters becomes an Expression."""
    if not isinstance(value, str) or "$" not in value:
        return value
    try:
        tree = ast.parse(_PARAM.sub(r"\1", value), mode="eval")
    except SyntaxError as exc:
        raise fail(path, "invalid_expression", f"cannot read the expression: {exc.msg}", actual=value) from exc
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or (node.func.id not in _FUNCTIONS and node.func.id != "cond") \
                    or node.keywords:
                raise fail(path, "invalid_expression", "only these functions can be called",
                           expected=sorted([*_FUNCTIONS, "cond"]), actual=value)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif not isinstance(node, _ALLOWED_NODES):
            raise fail(path, "invalid_expression", f"{type(node).__name__} is not allowed", actual=value)
    names -= {*_FUNCTIONS, "cond"}
    bare = sorted(name for name in names if f"${name}" not in value)
    if bare:
        raise fail(path, "invalid_expression", "write parameters as $name", expected=[f"${name}" for name in bare],
                   actual=value)
    return Expression(value, tree, frozenset(names))


def evaluate(value, params: dict, path: str):
    if not isinstance(value, Expression):
        return value

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in params:
                raise fail(path, "invalid_expression", f"${node.id} has no value", actual=value.source)
            return params[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = walk(node.operand)
            return not operand if isinstance(node.op, ast.Not) else (-operand if isinstance(node.op, ast.USub) else +operand)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise fail(path, "invalid_expression", "division by zero", actual=value.source)
            return left / right
        if isinstance(node, ast.BoolOp):
            values = [walk(item) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = walk(comparator)
                if not {
                    ast.Gt: left > right, ast.GtE: left >= right, ast.Lt: left < right,
                    ast.LtE: left <= right, ast.Eq: left == right, ast.NotEq: left != right,
                }[type(op)]:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if node.func.id == "cond":
                if len(node.args) != 3:
                    raise fail(path, "invalid_expression", "cond(condition, if_true, if_false) takes three values",
                               actual=value.source)
                return walk(node.args[1]) if walk(node.args[0]) else walk(node.args[2])
            return _FUNCTIONS[node.func.id](*[walk(item) for item in node.args])
        raise fail(path, "invalid_expression", "cannot evaluate", actual=value.source)

    try:
        return walk(value.tree)
    except (TypeError, ValueError) as exc:  # a colour added to a number, sqrt of a negative, ...
        raise fail(path, "invalid_expression", str(exc), actual=value.source) from exc


def _points(kind: str, value, path: str) -> list[list[float]]:
    """A footprint (polygon: a simple ring, stored counter-clockwise without a
    repeated closing point) or a centre line (polyline), as [[x, y], ...] in
    the object's frame, metres. Checked once per distinct point list (a city
    Recipe is parsed on every edit); the diagnostic carries this call's path."""
    try:
        key = (kind, tuple(tuple(point) for point in value))
        hash(key)
    except TypeError:  # not a list of pairs of numbers: let the checks say so
        return _points_checked(kind, value, path)
    result = _POINTS_CACHE.get(key)
    if result is None:
        try:
            result = _points_checked(kind, value, "<points>")
        except DiagnosticError as error:
            result = error
        if len(_POINTS_CACHE) >= _CACHE_SIZE:
            _POINTS_CACHE.clear()
        _POINTS_CACHE[key] = result
    if isinstance(result, DiagnosticError):
        found = result.diagnostics[0]
        raise fail(path, found.code, found.reason, expected=found.expected, actual=found.actual)
    return [list(point) for point in result]


_CACHE_SIZE = 20000
_POINTS_CACHE: dict = {}


def _polygons(value, path: str) -> list[list[list[float]]]:
    """Several outlines (a "polygons" parameter: a layer's outlines, a
    footprint's holes), each a polygon as _points checks it; may be empty."""
    if not isinstance(value, list):
        raise fail(path, "wrong_type", "must be a list of polygons", expected="[[[x, y], ...], ...]", actual=value)
    if len(value) > MAX_POLYGONS:
        raise fail(path, "out_of_range", f"at most {MAX_POLYGONS} polygons", expected=f"<= {MAX_POLYGONS}",
                   actual=len(value))
    return [_points("polygon", polygon, f"{path}[{index}]") for index, polygon in enumerate(value)]


def _points_checked(kind: str, value, path: str) -> list[list[float]]:
    closed = kind == "polygon"
    need = 3 if closed else 2
    shape = "[[x, y], ...]"
    if not isinstance(value, list) or not all(
            isinstance(point, list) and len(point) == 2 and all(
                not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) for v in point)
            for point in value):
        raise fail(path, "wrong_type", f"must be a list of [x, y] points in metres", expected=shape, actual=value)
    if len(value) > MAX_POINTS:
        raise fail(path, "out_of_range", f"at most {MAX_POINTS} points", expected=f"<= {MAX_POINTS}", actual=len(value))
    points = env_polygon.cleaned([(float(x), float(y)) for x, y in value], closed)
    if len(points) < need:
        raise fail(path, "invalid_shape", f"a {kind} needs at least {need} distinct points", expected=f">= {need}",
                   actual=len(points))
    if max(abs(v) for point in points for v in point) > MAX_M:
        raise fail(path, "out_of_range", f"points must be within {MAX_M} m", expected=f"<= {MAX_M}")
    if closed:
        if abs(env_polygon.signed_area(points)) < 1e-6:
            raise fail(path, "invalid_shape", "the polygon has no area")
        if not env_polygon.is_simple(points):
            raise fail(path, "invalid_shape", "the polygon's edges cross or touch each other")
        points = env_polygon.counter_clockwise(points)
    return [[round(x, 6), round(y, 6)] for x, y in points]


# --- Definitions ----------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    name: str
    kind: str
    level: str
    label: str
    description: str = ""
    unit: str = ""
    default: object = None
    min: float | None = None
    max: float | None = None
    values: tuple | None = None

    def check(self, value, path: str):
        """The value, checked against the kind and range."""
        if self.kind in ("length", "angle", "number"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise fail(path, "wrong_type", f"must be a number ({self.unit or self.kind})", expected="number",
                           actual=value)
            value = float(value) if self.kind == "length" else value
        elif self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise fail(path, "wrong_type", "must be a whole number", expected="integer", actual=value)
        elif self.kind == "color":
            if not isinstance(value, str) or not COLOR_PATTERN.match(value):
                raise fail(path, "wrong_type", "must be a #RRGGBB colour", expected="#RRGGBB", actual=value)
            value = value.lower()
        elif self.kind == "bool":
            if not isinstance(value, bool):
                raise fail(path, "wrong_type", "must be true or false", expected="bool", actual=value)
        elif self.kind == "text":
            if not isinstance(value, str):
                raise fail(path, "wrong_type", "must be text", expected="text", actual=value)
        elif self.kind in ("polygon", "polyline"):
            return _points(self.kind, value, path)
        elif self.kind == "polygons":
            return _polygons(value, path)
        if self.values is not None and value not in self.values:
            raise fail(path, "not_one_of", "must be one of the choices", expected=list(self.values), actual=value)
        if self.min is not None and value < self.min:
            raise fail(path, "out_of_range", f"must be at least {self.min}", expected=f">= {self.min}", actual=value)
        if self.max is not None and value > self.max:
            raise fail(path, "out_of_range", f"must be at most {self.max}", expected=f"<= {self.max}", actual=value)
        return value

    def narrowed(self, spec: dict, path: str) -> "Param":
        """An item's narrower range, choices or default for a placement parameter."""
        only(spec, {"default", "min", "max", "values"}, path)
        minimum = self.check(spec["min"], f"{path}.min") if "min" in spec else self.min
        maximum = self.check(spec["max"], f"{path}.max") if "max" in spec else self.max
        values = self.values
        if "values" in spec:
            if not isinstance(spec["values"], list) or not spec["values"]:
                raise fail(f"{path}.values", "wrong_type", "must be a non-empty list", expected="list",
                           actual=spec["values"])
            values = tuple(self.check(item, f"{path}.values") for item in spec["values"])
        narrowed = Param(self.name, self.kind, self.level, self.label, self.description, self.unit, self.default,
                         minimum, maximum, values)
        if "default" in spec:
            narrowed = Param(self.name, self.kind, self.level, self.label, self.description, self.unit,
                             narrowed.check(spec["default"], f"{path}.default"), minimum, maximum, values)
        return narrowed

    def with_default(self, value) -> "Param":
        return Param(self.name, self.kind, self.level, self.label, self.description, self.unit, value,
                     self.min, self.max, self.values)

    def as_json(self) -> dict:
        data = {"name": self.name, "kind": self.kind, "level": self.level, "label": self.label, "default": self.default}
        for key in ("description", "unit"):
            if getattr(self, key):
                data[key] = getattr(self, key)
        for key in ("min", "max"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.values is not None:
            data["values"] = list(self.values)
        return data


@dataclass(frozen=True)
class EnvType:
    id: str
    label: str
    description: str
    abstract: bool
    parents: tuple[str, ...]  # nearest first
    id_prefix: str
    params: dict[str, Param]
    behavior: dict
    shapes: tuple[dict, ...]
    envelope: dict | None
    terrain: dict | None

    def is_a(self, type_id: str) -> bool:
        return type_id == self.id or type_id in self.parents

    @property
    def is_terrain(self) -> bool:
        return self.terrain is not None

    def as_json(self) -> dict:
        """What describe-type returns: enough for a person or an AI to write an item."""
        return {
            "id": self.id, "label": self.label, "description": self.description, "abstract": self.abstract,
            "extends": list(self.parents), "kind": "terrain" if self.is_terrain else "object",
            "params": [param.as_json() for param in self.params.values()],
            "behavior": {key: getattr(value, "source", value) for key, value in self.behavior.items()},
            "terrain": {key: getattr(value, "source", value) for key, value in (self.terrain or {}).items()} or None,
        }


@dataclass(frozen=True)
class Solid:
    """A box, an upright cylinder, a wedge or a prism in the object's own
    frame, in metres: (x, y, z) is its centre; roll, pitch, yaw (degrees,
    applied yaw, then pitch, then roll about its centre) tilt it. A wedge's
    base is w x d and it rises along +y from 0 to h. A prism is a convex
    outline (points about its centre) raised h; w x d is its outline's box.
    (A type's "prism" and "ribbon" shapes resolve into these: see
    resolve_shape.)"""

    name: str
    primitive: str
    width_m: float
    depth_m: float
    height_m: float
    x_m: float = 0.0
    y_m: float = 0.0
    z_m: float = 0.0
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    color: str = "#cccccc"
    collide: bool = True
    visible: bool = True
    # A prism's convex outline about its centre (x, y), counter-clockwise.
    points: tuple[tuple[float, float], ...] = ()

    def as_json(self) -> dict:
        data = {key: getattr(self, key) for key in (
            "name", "primitive", "width_m", "depth_m", "height_m", "x_m", "y_m", "z_m",
            "roll_deg", "pitch_deg", "yaw_deg", "color", "collide", "visible")}
        if self.primitive == "prism":
            data["points"] = [list(point) for point in self.points]
        # For the browser, which does no tilt maths: the outline seen from above
        # and the height range, both in the object's frame.
        corners = self.corners()
        data["outline"] = [[round(x, 6), round(y, 6)] for x, y in self.outline()]
        data["z_range_m"] = [round(min(z for _, _, z in corners), 6), round(max(z for _, _, z in corners), 6)]
        return data

    def outline(self) -> list[tuple[float, float]]:
        """Seen from above, counter-clockwise: a circle's polygon for an upright
        cylinder, otherwise the convex hull of the tilted corners."""
        if self.primitive == "cylinder" and abs(self.roll_deg) < 1e-9 and abs(self.pitch_deg) < 1e-9:
            r = self.width_m / 2
            segments = env_rules.CIRCLE_SEGMENTS
            return [(self.x_m + r * math.cos(2 * math.pi * i / segments), self.y_m + r * math.sin(2 * math.pi * i / segments))
                    for i in range(segments)]
        return convex_hull([(x, y) for x, y, _ in self.corners()])

    def rotation(self) -> list[list[float]]:
        """Its rotation matrix (yaw about z, then pitch about y, then roll about x)."""
        return rotation_matrix(self.roll_deg, self.pitch_deg, self.yaw_deg)

    def corners(self) -> list[tuple[float, float, float]]:
        """Its outline's vertices in the object's frame (a cylinder by its box)."""
        hw, hd, hh = self.width_m / 2, self.depth_m / 2, self.height_m / 2
        if self.primitive == "wedge":
            local = [(sx * hw, -hd, -hh) for sx in (-1, 1)] + [(sx * hw, hd, -hh) for sx in (-1, 1)] \
                + [(sx * hw, hd, hh) for sx in (-1, 1)]
        elif self.primitive == "prism":
            local = [(x, y, z) for x, y in self.points for z in (-hh, hh)]
        else:
            local = [(sx * hw, sy * hd, sz * hh) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        r = self.rotation()
        return [(self.x_m + r[0][0] * x + r[0][1] * y + r[0][2] * z,
                 self.y_m + r[1][0] * x + r[1][1] * y + r[1][2] * z,
                 self.z_m + r[2][0] * x + r[2][1] * y + r[2][2] * z) for x, y, z in local]


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Counter-clockwise hull (Andrew's monotone chain)."""
    points = sorted(set((round(x, 9), round(y, 9)) for x, y in points))
    if len(points) <= 2:
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


def rotation_matrix(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[list[float]]:
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


@dataclass(frozen=True)
class Shape:
    """An object type resolved with its parameter values."""

    solids: tuple[Solid, ...]
    envelope: dict  # {primitive, width_m, depth_m}: the outline on the plan, centred on the object
    bottom_m: float  # lowest point of its solids above its base
    height_m: float  # highest point of its solids above its base
    surface: str
    snap: bool
    friction: float
    layer: str = "object"
    # The editor does not drag it on the plan (a City World layer under
    # everything): it is selected there and moved by its numbers.
    locked: bool = False

    def as_json(self) -> dict:
        return {
            "solids": [solid.as_json() for solid in self.solids], "envelope": self.envelope,
            "bottom_m": self.bottom_m, "height_m": self.height_m, "surface": self.surface, "snap": self.snap,
            "friction": self.friction, "layer": self.layer, **({"locked": True} if self.locked else {}),
        }


@dataclass(frozen=True)
class TypeLibrary:
    types: dict[str, EnvType]
    paths: tuple[Path, ...] = field(default_factory=tuple)

    def get(self, type_id, path: str) -> EnvType:
        env_type = self.types.get(type_id)
        if env_type is None:
            raise fail(path, "unknown_reference", "no such type", expected=sorted(
                key for key, value in self.types.items() if not value.abstract), actual=type_id)
        if env_type.abstract:
            raise fail(path, "not_allowed", "this type is abstract (only for other types to extend)", actual=type_id)
        return env_type


def _param(name: str, value, path: str) -> Param:
    value = mapping(value, path)
    only(value, PARAM_KEYS, path)
    kind = value.get("kind")
    if kind not in PARAM_KINDS:
        raise fail(f"{path}.kind", "not_one_of", "unknown parameter kind", expected=sorted(PARAM_KINDS), actual=kind)
    level = value.get("level", "item")
    if level not in LEVELS:
        raise fail(f"{path}.level", "not_one_of", "unknown level", expected=list(LEVELS), actual=level)
    values = value.get("values")
    if kind == "enum" and not values:
        raise fail(f"{path}.values", "missing_field", "an enum needs values")
    param = Param(name, kind, level, str(value.get("label", name)), str(value.get("description", "")),
                  str(value.get("unit", UNITS.get(kind, ""))), None, value.get("min"), value.get("max"),
                  tuple(values) if values else None)
    if "default" in value:
        param = param.with_default(param.check(value["default"], f"{path}.default"))
    if level == "type" and param.default is None:
        raise fail(f"{path}.default", "missing_field", "a type-level parameter needs a default")
    return param


def _raw_types(paths: list[Path]) -> dict[str, dict]:
    raw: dict[str, dict] = {}
    for path in paths:
        try:
            data = load_yaml_text(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise fail(path.name, "wrong_type", f"cannot read: {exc}") from exc
        data = mapping(data, path.name)
        if data.get("schema") != TYPES_SCHEMA:
            raise fail(f"{path.name}.schema", "wrong_schema", "wrong schema", expected=TYPES_SCHEMA, actual=data.get("schema"))
        entries = data.get("types")
        if not isinstance(entries, list):
            raise fail(f"{path.name}.types", "wrong_type", "must be a list", expected="list")
        for index, entry in enumerate(entries):
            where = f"{path.name}.types[{index}]"
            entry = mapping(entry, where)
            only(entry, TYPE_KEYS, where)
            type_id = entry.get("id")
            if not isinstance(type_id, str) or not ID_PATTERN.match(type_id):
                raise fail(f"{where}.id", "wrong_type", "must be a lower-case id", actual=type_id)
            if type_id in raw:
                raise fail(f"{where}.id", "duplicate_id", "defined twice", actual=type_id)
            raw[type_id] = {**entry, "_where": f"types.{type_id}"}
    return raw


def _resolve_type(type_id: str, raw: dict[str, dict], done: dict[str, EnvType], chain: tuple[str, ...]) -> EnvType:
    if type_id in done:
        return done[type_id]
    if type_id in chain:
        raise fail(f"types.{type_id}.extends", "invalid_shape", "a type extends itself",
                   actual=" -> ".join(chain + (type_id,)))
    entry = raw[type_id]
    where = entry["_where"]
    parent = None
    if "extends" in entry:
        if entry["extends"] not in raw:
            raise fail(f"{where}.extends", "unknown_reference", "extends an unknown type", actual=entry["extends"])
        parent = _resolve_type(entry["extends"], raw, done, chain + (type_id,))
    params = dict(parent.params) if parent else {}
    for name, value in mapping(entry.get("params", {}), f"{where}.params").items():
        if value is None:
            params.pop(name, None)  # a child type can drop a parameter it does not use
        else:
            params[name] = _param(name, value, f"{where}.params.{name}")
    behavior = dict(parent.behavior) if parent else {}
    behavior.update(mapping(entry.get("behavior", {}), f"{where}.behavior"))
    env_type = EnvType(
        id=type_id,
        label=str(entry.get("label", parent.label if parent else type_id)),
        description=str(entry.get("description", parent.description if parent else "")),
        abstract=bool(entry.get("abstract", False)),
        parents=((parent.id,) + parent.parents) if parent else (),
        id_prefix=str(entry.get("id_prefix", parent.id_prefix if parent else "object")),
        params=params,
        behavior=behavior,
        shapes=tuple(entry["shapes"]) if "shapes" in entry else (parent.shapes if parent else ()),
        envelope=entry["envelope"] if "envelope" in entry else (parent.envelope if parent else None),
        terrain=entry["terrain"] if "terrain" in entry else (parent.terrain if parent else None),
    )
    done[type_id] = _compiled(env_type, where)
    return done[type_id]


def _compiled(env_type: EnvType, where: str) -> EnvType:
    """Checked, with expressions compiled and every $parameter they use declared."""
    declared = set(env_type.params)

    def compiled(value, path):
        expression = compile_expression(value, path)
        if isinstance(expression, Expression):
            unknown = sorted(expression.names - declared)
            if unknown:
                raise fail(path, "unknown_reference", "uses undeclared parameters", expected=sorted(declared),
                           actual=unknown)
        return expression

    only(env_type.behavior, BEHAVIOR_KEYS, f"{where}.behavior")
    surface = env_type.behavior.get("surface", "ground")
    if surface not in SURFACES:
        raise fail(f"{where}.behavior.surface", "not_one_of", "unknown surface", expected=sorted(SURFACES), actual=surface)
    layer = env_type.behavior.get("layer", "object")
    if layer not in LAYERS:
        raise fail(f"{where}.behavior.layer", "not_one_of", "unknown layer", expected=sorted(LAYERS), actual=layer)
    if surface == "elevated" and "z_m" not in env_type.params:
        raise fail(f"{where}.params", "missing_field", "an elevated type needs a z_m parameter (its height above the terrain)")
    if env_type.terrain is not None and env_type.shapes:
        raise fail(f"{where}", "invalid_shape", "a terrain type has a terrain block instead of shapes")
    if not env_type.abstract and env_type.terrain is None and not env_type.shapes:
        raise fail(f"{where}.shapes", "missing_field", "a type that can be placed needs shapes (or a terrain block)")
    shapes = []
    for index, shape in enumerate(env_type.shapes):
        path = f"{where}.shapes[{index}]"
        shape = mapping(shape, path)
        only(shape, SHAPE_KEYS, path)
        # A prism takes points (or polygons) and h; a ribbon points, w and h; the others w, d and h.
        for key in ("primitive", "h", *(("w", "d") if "points" not in shape and "polygons" not in shape else ())):
            if key not in shape:
                raise fail(f"{path}.{key}", "missing_field", f"a shape needs {key}")
        shapes.append({key: compiled(value, f"{path}.{key}") for key, value in shape.items()})
    names = [shape.get("name") for shape in shapes if "name" in shape]
    if len(names) != len(set(names)):
        raise fail(f"{where}.shapes", "duplicate_id", "shape names must be unique", actual=names)
    behavior = {key: compiled(value, f"{where}.behavior.{key}") for key, value in env_type.behavior.items()}
    envelope = None
    if env_type.envelope is not None:
        envelope = mapping(env_type.envelope, f"{where}.envelope")
        only(envelope, {"primitive", "w", "d"}, f"{where}.envelope")
        envelope = {key: compiled(value, f"{where}.envelope.{key}") for key, value in envelope.items()}
    terrain = None
    if env_type.terrain is not None:
        terrain = mapping(env_type.terrain, f"{where}.terrain")
        only(terrain, {"kind", "generator", "color", "friction"}, f"{where}.terrain")
        kind = terrain.get("kind")
        if kind not in TERRAIN_KINDS:
            raise fail(f"{where}.terrain.kind", "not_one_of", "unknown terrain kind", expected=sorted(TERRAIN_KINDS), actual=kind)
        generator = terrain.get("generator", "flat")
        if generator not in TERRAIN_GENERATORS:
            raise fail(f"{where}.terrain.generator", "not_one_of", "unknown terrain generator",
                       expected=sorted(TERRAIN_GENERATORS), actual=generator)
        if (kind == "flat") != (generator == "flat"):
            raise fail(f"{where}.terrain.generator", "not_allowed",
                       "flat ground has no generator; a height field needs one",
                       expected="flat" if kind == "flat" else sorted(set(TERRAIN_GENERATORS) - {"flat"}), actual=generator)
        if not env_type.abstract:
            missing = sorted(TERRAIN_GENERATORS[generator] - declared)
            if missing:
                raise fail(f"{where}.params", "missing_field", f"the {generator} generator reads these parameters",
                           expected=sorted(TERRAIN_GENERATORS[generator]), actual=missing)
        terrain = {key: compiled(value, f"{where}.terrain.{key}") for key, value in terrain.items()}
    return EnvType(env_type.id, env_type.label, env_type.description, env_type.abstract, env_type.parents,
                   env_type.id_prefix, env_type.params, behavior, tuple(shapes), envelope, terrain)


def load_types(directory: Path = DEFAULT_TYPES) -> TypeLibrary:
    paths = sorted(Path(directory).glob("*.yaml"))
    if not paths:
        raise fail(str(directory), "missing_field", "no type definitions")
    raw = _raw_types(paths)
    done: dict[str, EnvType] = {}
    for type_id in raw:
        _resolve_type(type_id, raw, done, ())
    return TypeLibrary(types=done, paths=tuple(paths))


def defaults(env_type: EnvType) -> dict:
    return {name: param.default for name, param in env_type.params.items() if param.default is not None}


# --- Resolving an object type with values ---------------------------------------------

def _number(value, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise fail(path, "invalid_shape", "must come out as a number", actual=value)
    if abs(value) > MAX_M:
        raise fail(path, "invalid_shape", f"must be within {MAX_M} m", actual=value)
    return round(float(value), 6)


_SHAPE_CACHE: dict = {}


def resolve_shape(env_type: EnvType, params: dict, path: str) -> Shape:
    """The shape of a type with its values, resolved once per distinct values
    (Shape is frozen; errors are not kept and name this call's path)."""
    try:
        key = (id(env_type), json.dumps(params, sort_keys=True))
    except (TypeError, ValueError):
        return _resolve_shape(env_type, params, path)
    shape = _SHAPE_CACHE.get(key)
    if shape is None:
        shape = _resolve_shape(env_type, params, path)
        if len(_SHAPE_CACHE) >= _CACHE_SIZE:
            _SHAPE_CACHE.clear()
        _SHAPE_CACHE[key] = shape
    return shape


def _resolve_shape(env_type: EnvType, params: dict, path: str) -> Shape:
    """The solids, outline and behaviour of an object type with its parameter values.

    A "prism" shape (a footprint raised h) becomes one prism solid per convex
    piece of its footprint (<name>, or <name>-1, <name>-2, ... when concave). A
    "ribbon" shape (a strip w wide along a centre line, such as a road) becomes
    a box per segment (<name>-1, ...) and, when it runs along the line itself
    (x = 0), an upright cylinder filling each bend (<name>-joint-1, ...); x
    moves it sideways, to the right of the line's direction.
    """
    solids = []
    for index, shape in enumerate(env_type.shapes):
        where = f"{path} ({env_type.id}.shapes[{index}])"
        if "when" in shape and not evaluate(shape["when"], params, f"{where}.when"):
            continue
        primitive = evaluate(shape["primitive"], params, f"{where}.primitive")
        if primitive not in PRIMITIVES:
            raise fail(f"{where}.primitive", "invalid_shape", "unknown primitive", expected=sorted(PRIMITIVES), actual=primitive)
        color = evaluate(shape.get("color", params.get("color", "#cccccc")), params, f"{where}.color")
        if not isinstance(color, str) or not COLOR_PATTERN.match(color):
            raise fail(f"{where}.color", "invalid_shape", "must be a #RRGGBB colour", actual=color)

        def number(key, default=0):
            return _number(evaluate(shape.get(key, default), params, f"{where}.{key}"), f"{where}.{key}")

        def size(key):
            if key not in shape:
                raise fail(f"{where}.{key}", "missing_field", f"a {primitive} needs {key}")
            value = number(key)
            if value <= 0:
                raise fail(f"{where}.{key}", "invalid_shape", "must be greater than zero", expected="> 0", actual=value)
            return value

        name = str(shape.get("name", f"solid{index}"))
        common = {"color": color.lower(),
                  "collide": bool(evaluate(shape.get("collide", True), params, f"{where}.collide")),
                  "visible": bool(evaluate(shape.get("visible", True), params, f"{where}.visible"))}
        if primitive == "prism" and "polygons" in shape:  # one prism per outline: <name>-1, <name>-2, ...
            if any(number(key) for key in ("roll", "pitch", "yaw")):
                raise fail(where, "invalid_shape", "a prism is not tilted or turned (its points are)")
            height = size("h")
            z = number("z", height / 2)
            outlines = _polygons(evaluate(shape["polygons"], params, f"{where}.polygons"), f"{where}.polygons")
            for number_, outline in enumerate(outlines, 1):
                solids += _prism_solids(f"{name}-{number_}", [tuple(point) for point in outline],
                                        number("x"), number("y"), z, height, common)
            continue
        if primitive in ("prism", "ribbon"):
            if "points" not in shape:
                raise fail(f"{where}.points", "missing_field", f"a {primitive} needs points")
            kind = "polygon" if primitive == "prism" else "polyline"
            points = [tuple(point) for point in _points(kind, evaluate(shape["points"], params, f"{where}.points"),
                                                         f"{where}.points")]
            if any(number(key) for key in ("roll", "pitch", "yaw")):
                raise fail(where, "invalid_shape", f"a {primitive} is not tilted or turned (its points are)")
            height = size("h")
            z = number("z", height / 2)
            if primitive == "prism":
                holes = []
                if "holes" in shape:  # courtyards: left open (the pieces go round them)
                    holes = [[tuple(point) for point in hole] for hole in
                             _polygons(evaluate(shape["holes"], params, f"{where}.holes"), f"{where}.holes")]
                    if holes and env_polygon.holes_problem(points, holes):
                        raise fail(f"{where}.holes", "invalid_shape", env_polygon.holes_problem(points, holes))
                solids += _prism_solids(name, points, number("x"), number("y"), z, height, common, holes)
            else:
                solids += _ribbon_solids(name, points, size("w"), number("x"), z, height, common)
            continue
        sizes = [size(key) for key in ("w", "d", "h")]
        if primitive == "cylinder" and abs(sizes[0] - sizes[1]) > 1e-9:
            raise fail(where, "invalid_shape", "a cylinder's w and d are its diameter and must be equal", actual=sizes[:2])
        solids.append(Solid(
            name=name, primitive=primitive, width_m=sizes[0], depth_m=sizes[1], height_m=sizes[2],
            x_m=number("x"), y_m=number("y"), z_m=number("z", sizes[2] / 2),
            roll_deg=number("roll"), pitch_deg=number("pitch"), yaw_deg=number("yaw"), **common,
        ))
    if not solids:
        raise fail(path, "invalid_shape", f"type {env_type.id} makes no shape with these values")
    corners = [corner for solid in solids for corner in solid.corners()]
    bottom = round(min(z for _, _, z in corners), 6)
    if bottom < -1e-6:
        raise fail(path, "invalid_shape", f"a shape of type {env_type.id} reaches below the object's base",
                   expected=">= 0", actual=bottom)
    behavior = env_type.behavior
    friction = _number(evaluate(behavior.get("friction", 1.0), params, f"{path}.friction"), f"{path}.friction")
    return Shape(
        solids=tuple(solids), envelope=_envelope(env_type, params, corners, path),
        bottom_m=max(bottom, 0.0), height_m=round(max(z for _, _, z in corners), 6),
        surface=behavior.get("surface", "ground"),
        snap=bool(evaluate(behavior.get("snap", True), params, f"{path}.snap")),
        friction=friction, layer=behavior.get("layer", "object"),
        locked=bool(evaluate(behavior.get("locked", False), params, f"{path}.locked")),
    )


def _prism_solids(name, points, dx, dy, z, height, common, holes=()) -> list[Solid]:
    pieces = env_polygon.convex_pieces_with_holes(points, list(holes)) if holes else env_polygon.convex_pieces(points)
    solids = []
    for number, piece in enumerate(pieces, 1):
        xs, ys = [x for x, _ in piece], [y for _, y in piece]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        solids.append(Solid(
            name=name if len(pieces) == 1 else f"{name}-{number}", primitive="prism",
            width_m=round(max(xs) - min(xs), 6), depth_m=round(max(ys) - min(ys), 6), height_m=height,
            x_m=round(cx + dx, 6), y_m=round(cy + dy, 6), z_m=z,
            points=tuple((round(x - cx, 6), round(y - cy, 6)) for x, y in piece), **common))
    return solids


def _ribbon_solids(name, points, width, offset, z, height, common) -> list[Solid]:
    solids = []
    for number, ((x1, y1), (x2, y2)) in enumerate(zip(points, points[1:]), 1):
        length = math.hypot(x2 - x1, y2 - y1)
        heading = math.atan2(y2 - y1, x2 - x1)
        # Right of the direction of travel: (sin, -cos) of the heading.
        cx = (x1 + x2) / 2 + offset * math.sin(heading)
        cy = (y1 + y2) / 2 - offset * math.cos(heading)
        solids.append(Solid(
            name=f"{name}-{number}", primitive="box", width_m=width, depth_m=round(length, 6), height_m=height,
            x_m=round(cx, 6), y_m=round(cy, 6), z_m=z, yaw_deg=round(math.degrees(heading) - 90, 6), **common))
    if abs(offset) < 1e-9:
        for number, (x, y) in enumerate(points[1:-1], 1):
            solids.append(Solid(name=f"{name}-joint-{number}", primitive="cylinder", width_m=width, depth_m=width,
                                height_m=height, x_m=round(x, 6), y_m=round(y, 6), z_m=z, **common))
    return solids


def _envelope(env_type: EnvType, params: dict, corners, path: str) -> dict:
    # By default the smallest box centred on the object that holds every solid.
    half_w = max(abs(x) for x, _, _ in corners)
    half_d = max(abs(y) for _, y, _ in corners)
    envelope = {"primitive": "box", "width_m": round(2 * half_w, 6), "depth_m": round(2 * half_d, 6)}
    if env_type.envelope:
        spec = env_type.envelope
        if "primitive" in spec:
            envelope["primitive"] = evaluate(spec["primitive"], params, f"{path}.envelope.primitive")
        for key, name in (("w", "width_m"), ("d", "depth_m")):
            if key in spec:
                envelope[name] = _number(evaluate(spec[key], params, f"{path}.envelope.{key}"), f"{path}.envelope.{key}")
    if envelope["primitive"] not in ("box", "cylinder"):
        raise fail(f"{path}.envelope.primitive", "invalid_shape", "an envelope is a box or a cylinder",
                   actual=envelope["primitive"])
    return envelope


def terrain_settings(env_type: EnvType, params: dict, path: str) -> dict:
    """A terrain type's kind, generator, colour and friction with these values."""
    spec = env_type.terrain
    color = evaluate(spec.get("color", params.get("color", "#7a8b5a")), params, f"{path}.color")
    if not isinstance(color, str) or not COLOR_PATTERN.match(color):
        raise fail(f"{path}.color", "invalid_shape", "must be a #RRGGBB colour", actual=color)
    return {
        "kind": spec["kind"], "generator": spec.get("generator", "flat"), "color": color.lower(),
        "friction": _number(evaluate(spec.get("friction", 1.0), params, f"{path}.friction"), f"{path}.friction"),
    }


__all__ = ["DiagnosticError", "EnvType", "Param", "Shape", "Solid", "TypeLibrary", "compile_expression",
           "defaults", "evaluate", "load_types", "resolve_shape", "rotation_matrix", "terrain_settings"]
