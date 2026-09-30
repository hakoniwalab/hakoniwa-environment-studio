#!/usr/bin/env python3
"""Plane polygons and polylines for footprints and centre lines (#10).

Points are (x, y) in metres. A polygon is a closed ring without its closing
point repeated; a polyline is an open chain. MuJoCo collides meshes as their
convex hulls, so a concave footprint (an L-shaped building) is split into
convex pieces: an ear-clipping triangulation whose triangles are then merged
while the result stays convex (Hertel-Mehlhorn), which gives at most four
times the fewest pieces possible and is deterministic.
"""

from __future__ import annotations

import math

EPSILON = 1e-9


def signed_area(points: list[tuple[float, float]]) -> float:
    """Positive when counter-clockwise."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Whether two segments touch or cross (shared end points of neighbours are left to the caller)."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    if ((d1 > EPSILON and d2 < -EPSILON) or (d1 < -EPSILON and d2 > EPSILON)) and \
            ((d3 > EPSILON and d4 < -EPSILON) or (d3 < -EPSILON and d4 > EPSILON)):
        return True

    def on(p, a, b, d):
        return abs(d) <= EPSILON and min(a[0], b[0]) - EPSILON <= p[0] <= max(a[0], b[0]) + EPSILON \
            and min(a[1], b[1]) - EPSILON <= p[1] <= max(a[1], b[1]) + EPSILON

    return on(p1, q1, q2, d1) or on(p2, q1, q2, d2) or on(q1, p1, p2, d3) or on(q2, p1, p2, d4)


def is_simple(points: list[tuple[float, float]]) -> bool:
    """No two edges of the ring touch except neighbours at their shared corner."""
    n = len(points)
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _segments_cross(*edges[i], *edges[j]):
                return False
    return True


def cleaned(points: list[tuple[float, float]], closed: bool, min_step: float = 0.0) -> list[tuple[float, float]]:
    """Without repeated points (closer than min_step), a repeated closing point,
    or (for a ring) corners on a straight line."""
    result: list[tuple[float, float]] = []
    for point in points:
        if not result or math.dist(result[-1], point) > max(min_step, EPSILON):
            result.append(point)
    if closed:
        while len(result) > 1 and math.dist(result[0], result[-1]) <= max(min_step, EPSILON):
            result.pop()
        changed = True
        while changed and len(result) > 3:
            changed = False
            for i in range(len(result)):
                a, b, c = result[i - 1], result[i], result[(i + 1) % len(result)]
                if abs(_cross(a, b, c)) <= 1e-9 * max(1.0, math.dist(a, c) ** 2):
                    del result[i]
                    changed = True
                    break
    return result


def counter_clockwise(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return list(points) if signed_area(points) > 0 else list(reversed(points))


def is_convex(points: list[tuple[float, float]]) -> bool:
    """For a counter-clockwise ring: every corner turns left (or goes straight)."""
    n = len(points)
    return all(_cross(points[i - 1], points[i], points[(i + 1) % n]) >= -EPSILON for i in range(n))


def _inside_triangle(p, a, b, c) -> bool:
    return _cross(a, b, p) >= -EPSILON and _cross(b, c, p) >= -EPSILON and _cross(c, a, p) >= -EPSILON


def triangulate(points: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """Ear clipping of a simple counter-clockwise ring: triangles as corner indices."""
    remaining = list(range(len(points)))
    triangles: list[tuple[int, int, int]] = []
    while len(remaining) > 3:
        for k in range(len(remaining)):
            i, j, l = remaining[k - 1], remaining[k], remaining[(k + 1) % len(remaining)]
            a, b, c = points[i], points[j], points[l]
            if _cross(a, b, c) <= EPSILON:
                continue  # a reflex (or flat) corner is not an ear
            if any(_inside_triangle(points[m], a, b, c) for m in remaining if m not in (i, j, l)):
                continue
            triangles.append((i, j, l))
            del remaining[k]
            break
        else:
            raise ValueError("the polygon cannot be triangulated (is it simple?)")
    triangles.append(tuple(remaining))
    return triangles


def convex_pieces(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """A simple counter-clockwise ring split into convex counter-clockwise pieces."""
    if is_convex(points):
        return [list(points)]
    pieces = [list(triangle) for triangle in triangulate(points)]
    merged = True
    while merged:
        merged = False
        for a in range(len(pieces)):
            for b in range(a + 1, len(pieces)):
                joined = _join(pieces[a], pieces[b])
                if joined is not None and is_convex([points[i] for i in joined]):
                    pieces[a] = joined
                    del pieces[b]
                    merged = True
                    break
            if merged:
                break
    return [[points[i] for i in piece] for piece in pieces]


def _join(first: list[int], second: list[int]) -> list[int] | None:
    """Two index rings sharing one edge (i -> j in first, j -> i in second), joined along it."""
    for k in range(len(first)):
        i, j = first[k], first[(k + 1) % len(first)]
        if j in second:
            m = second.index(j)
            if second[(m + 1) % len(second)] == i:
                # first up to i, then second from after i round to before j.
                rest = [second[(m + 2 + step) % len(second)] for step in range(len(second) - 2)]
                return first[:k + 1] + rest + first[k + 1:]
    return None


def clip_polygon(points: list[tuple[float, float]], box: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a ring to (min_x, min_y, max_x, max_y)."""
    min_x, min_y, max_x, max_y = box
    edges = [(lambda p: p[0] >= min_x, lambda a, b: _at_x(a, b, min_x)),
             (lambda p: p[0] <= max_x, lambda a, b: _at_x(a, b, max_x)),
             (lambda p: p[1] >= min_y, lambda a, b: _at_y(a, b, min_y)),
             (lambda p: p[1] <= max_y, lambda a, b: _at_y(a, b, max_y))]
    result = list(points)
    for inside, cut in edges:
        source, result = result, []
        for index, current in enumerate(source):
            previous = source[index - 1]
            if inside(current):
                if not inside(previous):
                    result.append(cut(previous, current))
                result.append(current)
            elif inside(previous):
                result.append(cut(previous, current))
        if not result:
            break
    return result


def _at_x(a, b, x):
    t = (x - a[0]) / (b[0] - a[0])
    return (x, a[1] + t * (b[1] - a[1]))


def _at_y(a, b, y):
    t = (y - a[1]) / (b[1] - a[1])
    return (a[0] + t * (b[0] - a[0]), y)


def clip_polyline(points: list[tuple[float, float]], box: tuple[float, float, float, float]) -> list[list[tuple[float, float]]]:
    """The parts of a polyline inside (min_x, min_y, max_x, max_y) (Liang-Barsky per segment)."""
    min_x, min_y, max_x, max_y = box
    parts: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, a[0] - min_x), (dx, max_x - a[0]), (-dy, a[1] - min_y), (dy, max_y - a[1])):
            if abs(p) < EPSILON:
                if q < 0:
                    t0, t1 = 1.0, 0.0
                continue
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
        if t0 > t1:
            if len(current) >= 2:
                parts.append(current)
            current = []
            continue
        start = (a[0] + t0 * dx, a[1] + t0 * dy)
        end = (a[0] + t1 * dx, a[1] + t1 * dy)
        if not current or math.dist(current[-1], start) > 1e-6:
            if len(current) >= 2:
                parts.append(current)
            current = [start]
        current.append(end)
        if t1 < 1.0:
            parts.append(current)
            current = []
    if len(current) >= 2:
        parts.append(current)
    return parts


def convex_overlap(a: list[tuple[float, float]], b: list[tuple[float, float]], tolerance: float = 1e-6) -> bool:
    """Whether two convex counter-clockwise rings overlap by more than the
    tolerance (separating axis test on their edges)."""
    for polygon, other in ((a, b), (b, a)):
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
            nx, ny = y2 - y1, x1 - x2  # outward normal of a counter-clockwise edge
            size = math.hypot(nx, ny)
            if size < EPSILON:
                continue
            edge = (nx * x1 + ny * y1) / size
            if min((nx * x + ny * y) / size for x, y in other) >= edge - tolerance:
                return False
    return True
