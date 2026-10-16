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
MARK_COLORS = {"standard": "#f4f4f4", "white": "#f4f4f4", "yellow": "#f1c232",
               "blue": "#4a86e8", "green": "#6aa84f", "red": "#cc0000", "orange": "#e69138"}
LANE_COLORS = {
    "driving": "#4a4d52", "entry": "#4e5359", "exit": "#4e5359", "onRamp": "#535860",
    "offRamp": "#535860", "connectingRamp": "#535860", "shoulder": "#62666b",
    "border": "#696d72", "restricted": "#765f50", "stop": "#6c6260", "sidewalk": "#8d8d88",
    "biking": "#765b50", "parking": "#606b73", "median": "#777b70", "none": "#343638",
}


def _point(value, path):
    if (not isinstance(value, list) or len(value) != 3 or
            any(isinstance(item, bool) or not isinstance(item, (int, float)) or
                not math.isfinite(item) for item in value)):
        raise fail(path, "wrong_type", "a point must be [x, y, z] in metres", actual=value)
    return tuple(float(item) for item in value)


def load(path: Path, *, thickness_m: float, color: str, line_color: str, friction: float = 1.0) -> Shape:
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
    # "collider": false marks a section for drawing only (a dashed line's end): the road's
    # collision pieces span it, the lane faces and lines are cut at it. The ends always collide.
    shape_sections = []
    for index, entry in enumerate(raw_sections):
        if not isinstance(entry, dict) or not {"s_m", "points_m"} <= set(entry) <= {"s_m", "points_m", "collider"} \
                or entry.get("collider", False) is not False:
            raise fail(f"params.ribbon_file.sections[{index}]", "wrong_type",
                       "a section must contain s_m and points_m, and may say collider: false", actual=entry)
        if "collider" not in entry or index in (0, len(raw_sections) - 1):
            shape_sections.append(index)
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
    if isinstance(markings, list) and len(markings) == count and all(
            value in ("none", "solid", "dashed") for value in markings):
        markings = [{"boundary": boundary, "start_s_m": sections[0][0], "end_s_m": sections[-1][0],
                     "color": "standard", "lines": ([] if value == "none" else [{
                         "pattern": value, "t_offset_m": 0.0, "width_m": 0.12,
                         "length_m": 8.0, "space_m": 12.0 if value == "dashed" else 0.0,
                         "s_offset_m": 0.0}])} for boundary, value in enumerate(markings)]
    if not isinstance(markings, list) or any(not _valid_marking(value, count) for value in markings):
        raise fail("params.ribbon_file.markings", "wrong_type",
                   "marking segments with valid boundary, range, color and lines are required", actual=markings)
    lanes = data.get("lanes", [])
    if not isinstance(lanes, list) or any(not _valid_lane(value, count) for value in lanes):
        raise fail("params.ribbon_file.lanes", "wrong_type", "lane spans and kinds are required", actual=lanes)

    # Lane faces covering the whole width hide the slab's top: drawing both, 0.5 mm apart,
    # flickered (z-fighting) when seen from afar. The slab still collides; it is just not drawn.
    covered = set()
    for lane in lanes:
        covered.update(range(lane["left_boundary"], lane["right_boundary"]))
    slab_visible = covered != set(range(count - 1))

    solids = []
    for interval, (a, b) in enumerate(zip(shape_sections, shape_sections[1:]), 1):
        # One convex column across the whole road. Adjacent columns reuse the
        # exact same top vertices from `sections`.
        first, second = sections[a][1], sections[b][1]
        top = (first[0], first[-1], second[-1], second[0])
        vertices = top + tuple((x, y, z - thickness_m) for x, y, z in top)
        solids.append(_solid(f"slab-{interval}", vertices, FACES, color, collide=True, visible=slab_visible))
    for interval, ((s0, first), (s1, second)) in enumerate(zip(sections, sections[1:]), 1):
        for lane in lanes:
            left, right = lane["left_boundary"], lane["right_boundary"]
            lane_top = tuple(_add(point, (0, 0, 0), 0.0005) for point in
                             (first[left], first[right], second[right], second[left]))
            lane_bottom = tuple((x, y, z - 0.0004) for x, y, z in lane_top)
            solids.append(_solid(f"lane-{lane['id']}-{interval}", lane_top + lane_bottom, FACES,
                                 LANE_COLORS[lane["kind"]], collide=False, visible=True))
        middle_s = (s0 + s1) / 2
        for marking in markings:
            if not marking["start_s_m"] <= middle_s < marking["end_s_m"]:
                continue
            boundary = marking["boundary"]
            for line_index, line in enumerate(marking["lines"]):
                period = line["length_m"] + line["space_m"]
                phase = middle_s - marking["start_s_m"] - line["s_offset_m"]
                if line["pattern"] == "dashed" and period > 0 and phase % period >= line["length_m"]:
                    continue
                p0, p1 = (_offset(first, boundary, line["t_offset_m"]),
                          _offset(second, boundary, line["t_offset_m"]))
                q0 = _across(first, boundary, line["width_m"] / 2)
                q1 = _across(second, boundary, line["width_m"] / 2)
                # A flat stripe line_width wide, 3 mm above the road, ordered like the lane faces
                # (start-left, start-right, end-right, end-left; +q points to the right) so its top
                # faces up. It used to be four points on one side lifted +-3 mm: a vertical sliver.
                minus = lambda q: (-q[0], -q[1], -q[2])
                top_mark = (_add(p0, minus(q0), 0.003), _add(p0, q0, 0.003),
                            _add(p1, q1, 0.003), _add(p1, minus(q1), 0.003))
                bottom = tuple((x, y, z - 0.002) for x, y, z in top_mark)
                solids.append(_solid(f"mark-{boundary}-{line_index}-{interval}", top_mark + bottom, FACES,
                                     MARK_COLORS[marking["color"]], collide=False, visible=True))
    vertices = [point for solid in solids for point in solid.vertices]
    xs, ys, zs = zip(*vertices)
    return Shape(solids=tuple(solids),
                 envelope={"primitive": "box", "width_m": max(xs) - min(xs), "depth_m": max(ys) - min(ys)},
                 bottom_m=min(zs), height_m=max(zs), surface="ground", snap=False,
                 friction=friction, layer="surface", locked=True)


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


def _offset(points, index, distance):
    delta = _across(points, index, distance)
    return _add(points[index], delta, 0.0)


def _valid_marking(value, boundary_count):
    if not isinstance(value, dict) or not isinstance(value.get("boundary"), int):
        return False
    if not 0 <= value["boundary"] < boundary_count or value.get("color") not in MARK_COLORS:
        return False
    if not all(isinstance(value.get(key), (int, float)) for key in ("start_s_m", "end_s_m")):
        return False
    lines = value.get("lines")
    return isinstance(lines, list) and all(
        isinstance(line, dict) and line.get("pattern") in ("solid", "dashed") and
        all(isinstance(line.get(key), (int, float)) for key in
            ("t_offset_m", "width_m", "length_m", "space_m", "s_offset_m")) and
        line["width_m"] > 0 and line["length_m"] >= 0 and line["space_m"] >= 0
        for line in lines)


def _valid_lane(value, boundary_count):
    return (isinstance(value, dict) and isinstance(value.get("id"), int) and
            value.get("kind") in LANE_COLORS and
            isinstance(value.get("left_boundary"), int) and isinstance(value.get("right_boundary"), int) and
            0 <= value["left_boundary"] < value["right_boundary"] < boundary_count)


def _add(point, delta, lift):
    return point[0] + delta[0], point[1] + delta[1], point[2] + delta[2] + lift


PLAN_OUTLINE_POINTS = 400
_PLAN_CACHE: dict[tuple[str, float], dict] = {}


def plan_outline(path: Path) -> dict:
    """The ribbon seen from above for a 2-D plan: one polygon (the left edge forward, the
    right edge back, thinned to PLAN_OUTLINE_POINTS) and its height range, in the object's
    frame. Read from the sections' end points only, so large networks stay quick; kept
    until the file changes."""
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime)
    if key in _PLAN_CACHE:
        return _PLAN_CACHE[key]
    data = json.loads(path.read_text(encoding="utf-8"))
    sections = [section["points_m"] for section in data.get("sections", []) if len(section.get("points_m", [])) >= 2]
    if len(sections) < 2:
        raise ValueError(f"{path.name}: a ribbon needs two sections of two points")
    left = [points[0] for points in sections]
    right = [points[-1] for points in sections]
    step = max(1, math.ceil(len(left) / (PLAN_OUTLINE_POINTS // 2)))
    keep = lambda points: points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    ring = keep(left) + keep(right)[::-1]
    heights = [point[2] for points in sections for point in (points[0], points[-1])]
    result = {"outline": [[round(x, 3), round(y, 3)] for x, y, _ in ring],
              "z_range_m": [round(min(heights), 6), round(max(heights), 6)]}
    _PLAN_CACHE[key] = result
    return result
