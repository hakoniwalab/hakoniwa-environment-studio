"""Load a generic 3-D road ribbon and resolve it to shared-vertex solids."""

from __future__ import annotations

import json
import math
from pathlib import Path

from env_diagnostics import fail
from env_types import Shape, Solid

SCHEMA = "hakoniwa.road-ribbon/v1"
MAX_SECTIONS = 20_000
FACES = ((0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
         (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))


def _point(value, path):
    if (not isinstance(value, list) or len(value) != 3 or
            any(isinstance(item, bool) or not isinstance(item, (int, float)) or
                not math.isfinite(item) for item in value)):
        raise fail(path, "wrong_type", "a point must be [x, y, z] in metres", actual=value)
    return tuple(float(item) for item in value)


def load(path: Path, *, thickness_m: float, color: str, line_color: str) -> Shape:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise fail("params.ribbon_file", "unknown_reference", f"cannot read road ribbon: {exc}", actual=str(path)) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise fail("params.ribbon_file", "wrong_schema", "wrong road ribbon schema", expected=SCHEMA,
                   actual=data.get("schema") if isinstance(data, dict) else data)
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not 2 <= len(raw_sections) <= MAX_SECTIONS:
        raise fail("params.ribbon_file.sections", "out_of_range", "needs 2..20000 cross-sections",
                   expected="2..20000", actual=len(raw_sections) if isinstance(raw_sections, list) else raw_sections)
    sections, count, previous_s = [], None, -math.inf
    for index, entry in enumerate(raw_sections):
        if not isinstance(entry, dict) or set(entry) != {"s_m", "points_m"}:
            raise fail(f"params.ribbon_file.sections[{index}]", "wrong_type",
                       "a section must contain only s_m and points_m", actual=entry)
        s = entry["s_m"]
        if isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or s <= previous_s:
            raise fail(f"params.ribbon_file.sections[{index}].s_m", "out_of_range",
                       "section distance must increase", expected=f"> {previous_s}", actual=s)
        points = entry["points_m"]
        if not isinstance(points, list) or not 2 <= len(points) <= 32:
            raise fail(f"params.ribbon_file.sections[{index}].points_m", "out_of_range",
                       "needs 2..32 boundary points", actual=points)
        count = len(points) if count is None else count
        if len(points) != count:
            raise fail(f"params.ribbon_file.sections[{index}].points_m", "wrong_type",
                       "every section must have the same boundary count", expected=count, actual=len(points))
        sections.append((float(s), tuple(_point(point, f"sections[{index}].points_m") for point in points)))
        previous_s = float(s)
    markings = data.get("markings", ["none"] * count)
    if (not isinstance(markings, list) or len(markings) != count or
            any(value not in ("none", "solid", "dashed") for value in markings)):
        raise fail("params.ribbon_file.markings", "wrong_type",
                   "one none/solid/dashed value is required per boundary", expected=count, actual=markings)

    solids = []
    for interval, ((s0, first), (s1, second)) in enumerate(zip(sections, sections[1:]), 1):
        # One convex column across the whole road. Adjacent columns reuse the
        # exact same top vertices from `sections`.
        top = (first[0], first[-1], second[-1], second[0])
        vertices = top + tuple((x, y, z - thickness_m) for x, y, z in top)
        solids.append(_solid(f"slab-{interval}", vertices, FACES, color, collide=True, visible=True))
        for boundary, marking in enumerate(markings):
            if marking == "none" or (marking == "dashed" and ((s0 + s1) / 2) % 20.0 >= 8.0):
                continue
            p0, p1 = first[boundary], second[boundary]
            q0 = _across(first, boundary, 0.06)
            q1 = _across(second, boundary, 0.06)
            top_mark = (_add(p0, q0, 0.003), _add(p0, q0, -0.003),
                        _add(p1, q1, -0.003), _add(p1, q1, 0.003))
            bottom = tuple((x, y, z - 0.002) for x, y, z in top_mark)
            solids.append(_solid(f"mark-{boundary}-{interval}", top_mark + bottom, FACES,
                                 line_color, collide=False, visible=True))
    vertices = [point for solid in solids for point in solid.vertices]
    xs, ys, zs = zip(*vertices)
    return Shape(solids=tuple(solids),
                 envelope={"primitive": "box", "width_m": max(xs) - min(xs), "depth_m": max(ys) - min(ys)},
                 bottom_m=min(zs), height_m=max(zs), surface="ground", snap=False,
                 friction=1.0, layer="surface", locked=True)


def _solid(name, vertices, faces, color, *, collide, visible):
    xs, ys, zs = zip(*vertices)
    return Solid(name=name, primitive="convex", width_m=max(xs) - min(xs), depth_m=max(ys) - min(ys),
                 height_m=max(zs) - min(zs), color=color, collide=collide, visible=visible,
                 vertices=tuple(vertices), faces=tuple(faces))


def _across(points, index, half_width):
    before = points[max(0, index - 1)]
    after = points[min(len(points) - 1, index + 1)]
    dx, dy, dz = after[0] - before[0], after[1] - before[1], after[2] - before[2]
    size = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    return dx / size * half_width, dy / size * half_width, dz / size * half_width


def _add(point, delta, lift):
    return point[0] + delta[0], point[1] + delta[1], point[2] + delta[2] + lift
