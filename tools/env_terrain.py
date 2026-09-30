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
    # The area the grid covers, centred on the origin, when it is not the whole
    # environment (an Envsim terrain covers the area Envsim built it for; a
    # building reaching past it widens the environment, not the grid).
    grid_east_m: float = 0.0
    grid_north_m: float = 0.0
    # An Envsim terrain: its hfield as MuJoCo loads it ({path, sha256, size,
    # nrow, ncol}), so the generated world uses the file itself; and its GLB.
    hfield: dict | None = None
    visual: Path | None = None
    visual_sha256: str | None = None

    @property
    def half_east(self) -> float:
        return self.size_east_m / 2

    @property
    def half_north(self) -> float:
        return self.size_north_m / 2

    @property
    def grid_half_east(self) -> float:
        return (self.grid_east_m or self.size_east_m) / 2

    @property
    def grid_half_north(self) -> float:
        return (self.grid_north_m or self.size_north_m) / 2

    def height_at(self, x: float, y: float) -> float:
        """The ground height under (x, y); bilinear between grid points; beyond
        the grid (an environment wider than an Envsim terrain) its edge continues."""
        if self.kind != "hfield":
            return 0.0
        half_e, half_n = self.grid_half_east, self.grid_half_north
        x, y = min(max(x, -half_e), half_e), min(max(y, -half_n), half_n)
        col = (x + half_e) / (2 * half_e) * (self.ncol - 1)
        row = (half_n - y) / (2 * half_n) * (self.nrow - 1)
        c0, r0 = min(int(col), self.ncol - 2), min(int(row), self.nrow - 2)
        fc, fr = col - c0, row - r0
        h = self.heights
        top = h[r0][c0] * (1 - fc) + h[r0][c0 + 1] * fc
        bottom = h[r0 + 1][c0] * (1 - fc) + h[r0 + 1][c0 + 1] * fc
        return top * (1 - fr) + bottom * fr

    def highest_under(self, polygon: list[tuple[float, float]]) -> float:
        """The highest ground under a convex outline, never under MuJoCo's
        surface: the highest corner of every grid cell the outline touches
        (MuJoCo triangulates each cell, so its surface never rises above the
        cell's corners). An object set on it may float by the relief inside
        a cell, never reach into the ground."""
        if self.kind != "hfield":
            return 0.0
        # The Studio resolves the whole Recipe on every edit, and usually one
        # object moved: remembered per grid (make_terrain gives the same one
        # while the terrain is unchanged) and outline.
        answers = grid_memo(_UNDER, self.heights)
        key = (self.grid_half_east, self.grid_half_north, tuple(polygon))
        if key not in answers:
            if len(answers) > 100_000:
                answers.clear()
            answers[key] = self._highest_under(polygon)
        return answers[key]

    def _highest_under(self, polygon: list[tuple[float, float]]) -> float:
        import env_polygon

        half_e, half_n = self.grid_half_east, self.grid_half_north
        dx = 2 * half_e / (self.ncol - 1)
        dy = 2 * half_n / (self.nrow - 1)
        xs, ys = [x for x, _ in polygon], [y for _, y in polygon]
        c_lo = max(0, math.floor((min(xs) + half_e) / dx))
        c_hi = min(self.ncol - 2, math.floor((max(xs) + half_e) / dx))
        r_lo = max(0, math.floor((half_n - max(ys)) / dy))
        r_hi = min(self.nrow - 2, math.floor((half_n - min(ys)) / dy))
        convex = env_polygon.counter_clockwise(list(polygon))
        best = None
        h = self.heights
        for row in range(r_lo, r_hi + 1):
            north_top = half_n - row * dy
            for col in range(c_lo, c_hi + 1):
                west = -half_e + col * dx
                cell = [(west, north_top - dy), (west + dx, north_top - dy), (west + dx, north_top), (west, north_top)]
                if not env_polygon.convex_overlap(cell, convex, tolerance=0.0) and not _inside(convex, cell[0]):
                    continue
                top = max(h[row][col], h[row][col + 1], h[row + 1][col], h[row + 1][col + 1])
                best = top if best is None else max(best, top)
        if best is None:  # wholly beyond the grid: its nearest edge continues
            h = self.heights
            best = max(max(h[row][col], h[row][col + 1], h[row + 1][col], h[row + 1][col + 1])
                       for row in range(min(r_lo, self.nrow - 2), max(r_hi, 0) + 1)
                       for col in range(min(c_lo, self.ncol - 2), max(c_hi, 0) + 1))
        return best

    def as_json(self, with_heights: bool = True) -> dict:
        data = {"kind": self.kind, "color": self.color, "friction": self.friction,
                "size_m": {"east": self.size_east_m, "north": self.size_north_m}}
        if self.kind == "hfield":
            data.update(nrow=self.nrow, ncol=self.ncol, max_height_m=self.max_height_m,
                        grid_m={"east": 2 * self.grid_half_east, "north": 2 * self.grid_half_north})
            if self.hfield:
                data["hfield_sha256"] = self.hfield["sha256"]
            if with_heights:
                data["heights"] = [list(row) for row in self.heights]
        if self.visual_sha256:
            data["visual_sha256"] = self.visual_sha256
        return data


def envsim_order(terrain: Terrain) -> tuple[int, int, list[float]]:
    """(nrow, ncol, samples) of a grid in the order an Envsim hfield file keeps
    it (MuJoCo's frame x north, y west; data row r at y = -size_y + r * step,
    row 0 the east edge; column c at x = -size_x + c * step, column 0 the south
    edge): the inverse of _read_envsim_hfield's mapping."""
    rows, cols = terrain.ncol, terrain.nrow
    heights = terrain.heights
    return rows, cols, [heights[cols - 1 - c][rows - 1 - r] for r in range(rows) for c in range(cols)]


# highest_under's answers, per grid.
_UNDER: dict = {}


def grid_memo(store: dict, heights, grids: int = 4) -> dict:
    """The memo for a height grid in `store` (the few grids used lately, each
    kept with its grid so its id stays its own): a dict to fill."""
    entry = store.get(id(heights))
    if entry is None or entry[0] is not heights:
        if len(store) >= grids:
            store.pop(next(iter(store)))
        entry = store[id(heights)] = (heights, {})
    return entry[1]


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


def _envsim_terrain(params: dict, base: dict, path: str, base_dir: Path | None) -> "Terrain":
    """The ground of a City World that hakoniwa-envsim built: `dem` names its
    terrain-receipt.json (absolute, or relative to the Recipe). Its hfield is
    used as it is: the same grid over the area Envsim built it for, heights
    as MuJoCo computes them from the file (relative to its lowest point), so
    the generated world can load the file itself. `visual` is its GLB."""
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
        ns_m, ew_m = float(receipt["half_extent_m"]["north_south"]), float(receipt["half_extent_m"]["east_west"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise fail(f"{path}.dem", "unknown_reference", f"cannot read the Envsim terrain: {exc}", actual=text) from exc
    sha = hashlib.sha256(data).hexdigest()
    if receipt["hfield"].get("sha256") and sha != receipt["hfield"]["sha256"]:
        raise fail(f"{path}.dem", "invalid_shape", "the hfield file differs from its receipt (sha256)", actual=str(hfield))
    key = (sha, ns_m, ew_m, receipt_path.resolve())
    if key not in _DEM_CACHE:
        if len(_DEM_CACHE) >= 8:
            _DEM_CACHE.clear()
        _DEM_CACHE[key] = _read_envsim_hfield(hfield, data, receipt, receipt_path, ns_m, ew_m, path)
    nrow, ncol, heights, size = _DEM_CACHE[key]
    visual = visual_sha = None
    if str(params.get("visual") or "").strip():
        visual = Path(str(params["visual"])).expanduser()
        if not visual.is_absolute() and base_dir is not None:
            visual = (base_dir / visual).resolve()
        try:
            glb = visual.read_bytes()
        except OSError as exc:
            raise fail(f"{path}.visual", "unknown_reference", f"cannot read the terrain's GLB: {exc}",
                       actual=str(params["visual"])) from exc
        if glb[:4] != b"glTF":
            raise fail(f"{path}.visual", "wrong_type", "the terrain's look is a GLB file", actual=str(params["visual"]))
        visual_sha = hashlib.sha256(glb).hexdigest()
    return Terrain("hfield", **base, nrow=nrow, ncol=ncol, heights=heights,
                   max_height_m=max(max(row) for row in heights), grid_east_m=2 * ew_m, grid_north_m=2 * ns_m,
                   hfield={"path": str(hfield.resolve()), "sha256": sha, "size": size, "nrow": ncol, "ncol": nrow},
                   visual=visual, visual_sha256=visual_sha)


# Envsim terrains read (the file content, area and receipt): every request
# resolves the Recipe again.
_DEM_CACHE: dict = {}


def _read_envsim_hfield(hfield: Path, data: bytes, receipt: dict, receipt_path: Path, ns_m: float, ew_m: float,
                        path: str) -> tuple:
    """(nrow, ncol, heights, MuJoCo size) of an Envsim hfield in this module's
    grid order. Envsim writes it in its MuJoCo frame (x north, y west): data
    row r lies at y = -size_y + r * step (row 0 the east edge), column c at
    x = -size_x + c * step (column 0 the south edge). So this grid's row (from
    the north) is the data's column from the last, and its column (from the
    west) the data's row from the last. Heights are what MuJoCo makes of the
    file: (value - lowest) / (highest - lowest) * size_z."""
    import env_envsim  # Envsim's reader (imported only for this generator)

    _geodesy, _extract, probe = env_envsim.pipeline()
    rows, cols, samples = probe.read_hfield(hfield)
    size = None
    mjcf = receipt.get("mjcf")
    if mjcf:  # the size Envsim wrote (its z scale is the rounded height span)
        mjcf_path = Path(mjcf) if Path(mjcf).is_absolute() else receipt_path.parent / mjcf
        try:
            import xml.etree.ElementTree as ET

            element = ET.parse(mjcf_path).getroot().find("asset/hfield")
            size = tuple(float(value) for value in element.get("size").split())
        except (OSError, ET.ParseError, AttributeError, ValueError):
            size = None
    low, high = min(samples), max(samples)
    if size is None or len(size) != 4:
        size = (ns_m, ew_m, max(high - low, 1e-6), 1.0)
    if abs(size[0] - ns_m) > 1e-6 or abs(size[1] - ew_m) > 1e-6:
        raise fail(f"{path}.dem", "invalid_shape", "the hfield's size differs from its receipt's area",
                   expected=[ns_m, ew_m], actual=list(size[:2]))
    span = high - low
    heights = tuple(
        tuple(((samples[(rows - 1 - col) * cols + (cols - 1 - row)] - low) / span * size[2]) if span > 0 else 0.0
              for col in range(rows))
        for row in range(cols))
    return cols, rows, heights, size


def make_terrain(settings: dict, params: dict, size_east: float, size_north: float, path: str,
                 base_dir: Path | None = None) -> Terrain:
    """The terrain of a terrain type's settings (env_types.terrain_settings);
    `base_dir` is where relative data paths (the envsim generator's) start.
    The same terrain comes back (the same object) while nothing it is made
    from changes, so what is remembered per grid stays."""
    try:
        key = json.dumps([settings, params, size_east, size_north, str(base_dir)], sort_keys=True)
    except (TypeError, ValueError):
        return _make_terrain(settings, params, size_east, size_north, path, base_dir)
    terrain = _TERRAINS.get(key)
    fresh = _make_terrain(settings, params, size_east, size_north, path, base_dir) if (
        terrain is None or settings.get("generator") == "envsim") else terrain
    if terrain is not None and fresh is not terrain and fresh == terrain:
        return terrain  # an Envsim terrain read again (its files checked): unchanged
    if len(_TERRAINS) >= 8:
        _TERRAINS.pop(next(iter(_TERRAINS)))
    _TERRAINS[key] = fresh
    return fresh


_TERRAINS: dict = {}


def _make_terrain(settings: dict, params: dict, size_east: float, size_north: float, path: str,
                  base_dir: Path | None = None) -> Terrain:
    base = dict(color=settings["color"], friction=settings["friction"], size_east_m=size_east, size_north_m=size_north)
    if settings["kind"] == "flat":
        return Terrain("flat", **base)
    if settings["generator"] == "hills":
        nrow, ncol, heights = _hills(params, size_east, size_north, path)
    elif settings["generator"] == "envsim":
        return _envsim_terrain(params, base, path, base_dir)
    else:
        raise fail(f"{path}.generator", "not_one_of", "no generator for an hfield", expected=["envsim", "hills"],
                   actual=settings["generator"])
    if not isinstance(heights, tuple):
        heights = tuple(tuple(row) for row in heights)
    return Terrain("hfield", **base, nrow=nrow, ncol=ncol, heights=heights,
                   max_height_m=max(max(row) for row in heights))
