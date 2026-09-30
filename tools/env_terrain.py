"""The ground of an environment: flat, or a height field (MuJoCo hfield).

A terrain covers the whole environment, centred on the origin. A height field
is a grid of heights in metres at `resolution_m` spacing; it is made by a
named generator from the terrain type's parameters and is deterministic (the
same parameters give the same grid, so an AI or a person can rebuild it).

Grid order is MuJoCo's (checked against MuJoCo in tests/test_env_terrain.py):
row 0 is the north edge (+y), the last row the south edge; column 0 is the
west edge (-x), the last column the east edge.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random

from env_diagnostics import fail

# Grid sizes accepted (cells per side), a sanity bound for the browser and MuJoCo.
MAX_GRID = 1001
# Thickness below z = 0 of the solid under the terrain (MuJoCo hfield base, flat slab).
BASE_M = 0.5


@dataclass(frozen=True)
class Terrain:
    kind: str  # flat | hfield
    color: str
    friction: float
    size_east_m: float
    size_north_m: float
    nrow: int = 0
    ncol: int = 0
    heights: tuple[tuple[float, ...], ...] = ()  # metres, rows north to south
    max_height_m: float = 0.0

    @property
    def half_east(self) -> float:
        return self.size_east_m / 2

    @property
    def half_north(self) -> float:
        return self.size_north_m / 2

    def height_at(self, x: float, y: float) -> float:
        """The ground height under (x, y); bilinear between grid points, 0 outside."""
        if self.kind != "hfield":
            return 0.0
        if abs(x) > self.half_east or abs(y) > self.half_north:
            return 0.0
        col = (x + self.half_east) / self.size_east_m * (self.ncol - 1)
        row = (self.half_north - y) / self.size_north_m * (self.nrow - 1)
        c0, r0 = min(int(col), self.ncol - 2), min(int(row), self.nrow - 2)
        fc, fr = col - c0, row - r0
        h = self.heights
        top = h[r0][c0] * (1 - fc) + h[r0][c0 + 1] * fc
        bottom = h[r0 + 1][c0] * (1 - fc) + h[r0 + 1][c0 + 1] * fc
        return top * (1 - fr) + bottom * fr

    def highest_under(self, polygon: list[tuple[float, float]]) -> float:
        """The highest ground under a convex outline (its corners, edges and the
        grid points inside), so an object set on it never reaches into it."""
        if self.kind != "hfield":
            return 0.0
        step = min(self.size_east_m / (self.ncol - 1), self.size_north_m / (self.nrow - 1))
        points = list(polygon)
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
            count = max(1, int(math.hypot(x2 - x1, y2 - y1) / step) + 1)
            points += [(x1 + (x2 - x1) * i / count, y1 + (y2 - y1) * i / count) for i in range(count)]
        xs, ys = [x for x, _ in polygon], [y for _, y in polygon]
        c_lo = max(0, math.floor((min(xs) + self.half_east) / self.size_east_m * (self.ncol - 1)))
        c_hi = min(self.ncol - 1, math.ceil((max(xs) + self.half_east) / self.size_east_m * (self.ncol - 1)))
        r_lo = max(0, math.floor((self.half_north - max(ys)) / self.size_north_m * (self.nrow - 1)))
        r_hi = min(self.nrow - 1, math.ceil((self.half_north - min(ys)) / self.size_north_m * (self.nrow - 1)))
        for row in range(r_lo, r_hi + 1):
            for col in range(c_lo, c_hi + 1):
                x = -self.half_east + col / (self.ncol - 1) * self.size_east_m
                y = self.half_north - row / (self.nrow - 1) * self.size_north_m
                if _inside(polygon, (x, y)):
                    points.append((x, y))
        return max(self.height_at(x, y) for x, y in points)

    def as_json(self, with_heights: bool = True) -> dict:
        data = {"kind": self.kind, "color": self.color, "friction": self.friction,
                "size_m": {"east": self.size_east_m, "north": self.size_north_m}}
        if self.kind == "hfield":
            data.update(nrow=self.nrow, ncol=self.ncol, max_height_m=self.max_height_m)
            if with_heights:
                data["heights"] = [list(row) for row in self.heights]
        return data


def _inside(polygon, point) -> bool:
    x, y = point
    inside = False
    for (xi, yi), (xj, yj) in zip(polygon, polygon[-1:] + polygon[:-1]):
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def _grid(size_east: float, size_north: float, resolution: float, path: str) -> tuple[int, int]:
    if resolution <= 0:
        raise fail(f"{path}.resolution_m", "out_of_range", "must be greater than zero", expected="> 0", actual=resolution)
    ncol = int(round(size_east / resolution)) + 1
    nrow = int(round(size_north / resolution)) + 1
    if max(nrow, ncol) > MAX_GRID:
        raise fail(f"{path}.resolution_m", "out_of_range", f"makes a grid over {MAX_GRID} points a side",
                   expected=f">= {max(size_east, size_north) / (MAX_GRID - 1):.3f}", actual=resolution)
    return max(nrow, 2), max(ncol, 2)


def _hills(params: dict, size_east: float, size_north: float, path: str) -> tuple[int, int, list[list[float]]]:
    """Gaussian hills at seeded places: `hills` of them, about `radius_m` wide,
    up to `max_height_m`, sampled every `resolution_m`."""
    nrow, ncol = _grid(size_east, size_north, params["resolution_m"], path)
    rng = random.Random(params["seed"])
    radius, peak = params["radius_m"], params["max_height_m"]
    bumps = []
    for _ in range(params["hills"]):
        bumps.append((
            rng.uniform(-size_east / 2, size_east / 2), rng.uniform(-size_north / 2, size_north / 2),
            radius * rng.uniform(0.6, 1.4), peak * rng.uniform(0.4, 1.0),
        ))
    heights = []
    for row in range(nrow):
        y = size_north / 2 - row / (nrow - 1) * size_north
        line = []
        for col in range(ncol):
            x = -size_east / 2 + col / (ncol - 1) * size_east
            h = sum(a * math.exp(-((x - bx) ** 2 + (y - by) ** 2) / (2 * r * r)) for bx, by, r, a in bumps)
            line.append(round(min(h, peak), 4))
        heights.append(line)
    return nrow, ncol, heights


def _envsim_dem(params: dict, size_east: float, size_north: float, path: str,
                base_dir: Path | None) -> tuple[int, int, list[list[float]]]:
    """The ground of a City World that hakoniwa-envsim built (its terrain
    hfield, from PLATEAU DEM): `dem` names its terrain-receipt.json (absolute,
    or relative to the Recipe). Sampled with Envsim's own reader every
    `resolution_m` (coarser when the grid would exceed MAX_GRID), relative to
    its lowest point; outside the DEM the edge heights continue."""
    text = str(params.get("dem") or "").strip()
    if not text:
        raise fail(f"{path}.dem", "missing_field", "the terrain-receipt.json of an Envsim City World",
                   expected="path to components/terrain/terrain-receipt.json")
    receipt_path = Path(text).expanduser()
    if not receipt_path.is_absolute() and base_dir is not None:
        receipt_path = base_dir / receipt_path
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        hfield = Path(receipt["hfield"]["path"])
        if not hfield.is_absolute():
            hfield = receipt_path.parent / hfield
        data = hfield.read_bytes()
    except (OSError, ValueError, KeyError) as exc:
        raise fail(f"{path}.dem", "unknown_reference", f"cannot read the Envsim terrain: {exc}", actual=text) from exc
    if receipt["hfield"].get("sha256") and hashlib.sha256(data).hexdigest() != receipt["hfield"]["sha256"]:
        raise fail(f"{path}.dem", "invalid_shape", "the hfield file differs from its receipt (sha256)", actual=str(hfield))
    import env_citygml  # Envsim's reader (imported only for this generator)

    _geodesy, _extract, probe = env_citygml.envsim_modules()
    rows, cols, samples = probe.read_hfield(hfield)
    ns_m, ew_m = float(receipt["half_extent_m"]["north_south"]), float(receipt["half_extent_m"]["east_west"])
    offset = min(samples)
    resolution = max(float(params.get("resolution_m", 1.0)), max(size_east, size_north) / (MAX_GRID - 1))
    nrow, ncol = _grid(size_east, size_north, resolution, path)
    heights = []
    for row in range(nrow):
        north = size_north / 2 - row / (nrow - 1) * size_north
        line = []
        for col in range(ncol):
            east = -size_east / 2 + col / (ncol - 1) * size_east
            # Envsim's hfield axes are MuJoCo's city frame: x = north, y = -east.
            line.append(round(probe.terrain_height(north, -east, samples, rows, cols, ns_m, ew_m) - offset, 4))
        heights.append(line)
    return nrow, ncol, heights


def make_terrain(settings: dict, params: dict, size_east: float, size_north: float, path: str,
                 base_dir: Path | None = None) -> Terrain:
    """The terrain of a terrain type's settings (env_types.terrain_settings);
    `base_dir` is where relative data paths (the envsim generator's) start."""
    base = dict(color=settings["color"], friction=settings["friction"], size_east_m=size_east, size_north_m=size_north)
    if settings["kind"] == "flat":
        return Terrain("flat", **base)
    if settings["generator"] == "hills":
        nrow, ncol, heights = _hills(params, size_east, size_north, path)
    elif settings["generator"] == "envsim":
        nrow, ncol, heights = _envsim_dem(params, size_east, size_north, path, base_dir)
    else:
        raise fail(f"{path}.generator", "not_one_of", "no generator for an hfield", expected=["envsim", "hills"],
                   actual=settings["generator"])
    return Terrain("hfield", **base, nrow=nrow, ncol=ncol, heights=tuple(tuple(row) for row in heights),
                   max_height_m=max(max(row) for row in heights))
