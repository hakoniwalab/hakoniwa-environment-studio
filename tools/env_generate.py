#!/usr/bin/env python3
"""Generate the Three.js GLB and the MuJoCo world of an Environment Recipe (#3).

Both come from the same resolved Recipe (env_schema.py): every object's
solids with the same size, position and tilt, and the same terrain. The output
is deterministic: the same Recipe always gives the same files.

Frames:
  Recipe / MuJoCo: ENU, x east, y north, z up, metres, origin at the centre.
  GLB (glTF):      x = east, y = up, z = -north.

Names: each object keeps its Recipe id. The GLB node is named after it (item
and type in its extras); in MuJoCo it is the body object:<id> with one geom
per solid, geom:<id>/<solid>. Solids that take no part in collision (paint,
lamps) are geoms with contype and conaffinity 0 in group 2, which rays and
contacts pass through. Solids of "surface" layer objects (roads) have contype
2 and conaffinity 1: they collide with everything but one another. The terrain is the geom `terrain` (an hfield, or a slab
whose top is z = 0 for flat ground).

environment_mjcf(validation=True) gives the world env_validate.py checks:
objects become free bodies (MuJoCo does not collide bodies fixed to the world)
and fixed boundary boxes stand just outside the environment's edges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_schema  # noqa: E402
import env_terrain  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402
from env_types import Solid, rotation_matrix  # noqa: E402

GENERATOR_VERSION = "1"
CIRCLE_SEGMENTS = env_schema.CIRCLE_SEGMENTS
BODY_PREFIX = "object:"
GEOM_PREFIX = "geom:"
BOUNDARY_PREFIX = "boundary:"
TERRAIN_GEOM = "terrain"
VISUAL_GROUP = 2
SURFACE_CONTYPE = 2
# Thickness of the validation boundary boxes, metres; they stand this high.
BOUNDARY_THICKNESS_M = 1.0
BOUNDARY_HEIGHT_M = 200.0


def geom_name(object_id: str, solid: str) -> str:
    """geom:<object id>/<solid name>; object ids have no "/"."""
    return f"{GEOM_PREFIX}{object_id}/{solid}"


def geom_object(name: str) -> str:
    return name.removeprefix(GEOM_PREFIX).rsplit("/", 1)[0]


def srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def hex_rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5))


def _numbers(values) -> str:
    return " ".join(f"{round(value, 9) + 0.0:.9g}" for value in values)


def quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> tuple[float, float, float, float]:
    """(w, x, y, z) of yaw about z, then pitch about y, then roll about x."""
    cr, sr = math.cos(math.radians(roll_deg) / 2), math.sin(math.radians(roll_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg) / 2), math.sin(math.radians(pitch_deg) / 2)
    cy, sy = math.cos(math.radians(yaw_deg) / 2), math.sin(math.radians(yaw_deg) / 2)
    return (cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy)


def wedge_vertices(solid: Solid) -> list[tuple[float, float, float]]:
    """A wedge's six corners about its centre (base w x d, rising along +y)."""
    hw, hd, hh = solid.width_m / 2, solid.depth_m / 2, solid.height_m / 2
    return [(-hw, -hd, -hh), (hw, -hd, -hh), (-hw, hd, -hh), (hw, hd, -hh), (-hw, hd, hh), (hw, hd, hh)]


# --- Meshes in a local ENU frame (x, y, z up) ---------------------------------------

def _faces_mesh(vertices, faces):
    """Flat-shaded triangles from polygons (counter-clockwise seen from outside)."""
    positions, normals, indices = [], [], []
    for face in faces:
        points = [vertices[index] for index in face]
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[0], points[1], points[2]
        ux, uy, uz, vx, vy, vz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        size = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        base = len(positions)
        positions += points
        normals += [(nx / size, ny / size, nz / size)] * len(points)
        indices += [index for k in range(1, len(points) - 1) for index in (base, base + k, base + k + 1)]
    return positions, normals, indices


def _box_mesh(w, d, h):
    x, y, z = w / 2, d / 2, h / 2
    v = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z), (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    return _faces_mesh(v, [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])


def prism_vertices(solid: Solid) -> list[tuple[float, float, float]]:
    """A prism's corners about its centre: the outline at the bottom, then at the top."""
    hh = solid.height_m / 2
    return [(x, y, -hh) for x, y in solid.points] + [(x, y, hh) for x, y in solid.points]


def _prism_mesh(solid: Solid):
    v = prism_vertices(solid)
    n = len(solid.points)
    sides = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    return _faces_mesh(v, [tuple(reversed(range(n))), tuple(range(n, 2 * n)), *sides])


def _wedge_mesh(solid: Solid):
    v = wedge_vertices(solid)
    # bottom, high end (+y), slope, and the two sides.
    return _faces_mesh(v, [(0, 2, 3, 1), (2, 4, 5, 3), (0, 1, 5, 4), (0, 4, 2), (1, 3, 5)])


def _cylinder_mesh(diameter, height, segments=CIRCLE_SEGMENTS):
    r, hz = diameter / 2, height / 2
    positions, normals, indices = [], [], []
    for i in range(segments + 1):
        angle = 2 * math.pi * i / segments
        c, s = math.cos(angle), math.sin(angle)
        positions += [(r * c, r * s, -hz), (r * c, r * s, hz)]
        normals += [(c, s, 0.0)] * 2
    for i in range(segments):
        a, b, c2, d = 2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 3
        indices += [a, c2, d, a, d, b]
    for z, up in ((-hz, -1.0), (hz, 1.0)):
        centre = len(positions)
        positions.append((0.0, 0.0, z))
        normals.append((0.0, 0.0, up))
        for i in range(segments):
            angle = 2 * math.pi * i / segments
            positions.append((r * math.cos(angle), r * math.sin(angle), z))
            normals.append((0.0, 0.0, up))
        for i in range(segments):
            j, k = centre + 1 + i, centre + 1 + (i + 1) % segments
            indices += [centre, j, k] if up > 0 else [centre, k, j]
    return positions, normals, indices


def solid_mesh(solid: Solid):
    """The solid's mesh in its object's ENU frame: tilted about its centre, at its centre."""
    if solid.primitive == "box":
        mesh = _box_mesh(solid.width_m, solid.depth_m, solid.height_m)
    elif solid.primitive == "cylinder":
        mesh = _cylinder_mesh(solid.width_m, solid.height_m)
    elif solid.primitive == "prism":
        mesh = _prism_mesh(solid)
    else:
        mesh = _wedge_mesh(solid)
    r = solid.rotation()
    positions, normals, indices = mesh

    def turn(p):
        return (r[0][0] * p[0] + r[0][1] * p[1] + r[0][2] * p[2], r[1][0] * p[0] + r[1][1] * p[1] + r[1][2] * p[2],
                r[2][0] * p[0] + r[2][1] * p[1] + r[2][2] * p[2])

    positions = [tuple(a + b for a, b in zip(turn(p), (solid.x_m, solid.y_m, solid.z_m))) for p in positions]
    return positions, [turn(n) for n in normals], indices


def terrain_mesh(terrain: env_terrain.Terrain):
    """The ground: a grid for an hfield, a thin slab (top at z = 0) when flat."""
    if terrain.kind != "hfield":
        positions, normals, indices = _box_mesh(terrain.size_east_m, terrain.size_north_m, 0.02)
        return [(x, y, z - 0.01) for x, y, z in positions], normals, indices
    nrow, ncol, heights = terrain.nrow, terrain.ncol, terrain.heights
    dx, dy = terrain.size_east_m / (ncol - 1), terrain.size_north_m / (nrow - 1)
    positions, normals, indices = [], [], []
    for row in range(nrow):
        for col in range(ncol):
            positions.append((-terrain.half_east + col * dx, terrain.half_north - row * dy, heights[row][col]))
            # Normal from central differences (rows run south).
            hx = (heights[row][min(col + 1, ncol - 1)] - heights[row][max(col - 1, 0)]) / (
                dx * (min(col + 1, ncol - 1) - max(col - 1, 0)))
            hy = (heights[max(row - 1, 0)][col] - heights[min(row + 1, nrow - 1)][col]) / (
                dy * (min(row + 1, nrow - 1) - max(row - 1, 0)))
            size = math.sqrt(hx * hx + hy * hy + 1)
            normals.append((-hx / size, -hy / size, 1 / size))
    for row in range(nrow - 1):
        for col in range(ncol - 1):
            a, b = row * ncol + col, row * ncol + col + 1
            c, d = (row + 1) * ncol + col, (row + 1) * ncol + col + 1
            indices += [a, c, d, a, d, b]  # counter-clockwise seen from above
    return positions, normals, indices


def to_gltf(mesh):
    """ENU (x, y, z up) to glTF (x, y up, z = -north)."""
    positions, normals, indices = mesh
    return [(x, z, -y) for x, y, z in positions], [(x, z, -y) for x, y, z in normals], indices


# --- GLB ----------------------------------------------------------------------------

class _GlbBuilder:
    def __init__(self):
        self.binary = bytearray()
        self.gltf = {
            "asset": {"version": "2.0", "generator": f"hakoniwa-environment-studio env_generate.py {GENERATOR_VERSION}"},
            "scene": 0, "scenes": [{"nodes": []}],
            "nodes": [], "meshes": [], "materials": [], "accessors": [], "bufferViews": [],
            "buffers": [{"byteLength": 0}],
        }
        self.material_ids: dict[str, int] = {}

    def _view(self, data: bytes, target: int) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        self.gltf["bufferViews"].append({"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data), "target": target})
        self.binary.extend(data)
        return len(self.gltf["bufferViews"]) - 1

    def _accessor(self, view: int, component: int, count: int, kind: str, **extra) -> int:
        self.gltf["accessors"].append({"bufferView": view, "componentType": component, "count": count, "type": kind, **extra})
        return len(self.gltf["accessors"]) - 1

    def material(self, color: str) -> int:
        if color not in self.material_ids:
            r, g, b = (srgb_to_linear(value) for value in hex_rgb(color))
            self.gltf["materials"].append({
                "name": color,
                "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.85},
            })
            self.material_ids[color] = len(self.gltf["materials"]) - 1
        return self.material_ids[color]

    def _primitive(self, mesh, color: str) -> dict:
        positions, normals, indices = mesh
        flat = [value for point in positions for value in point]
        position = self._accessor(
            self._view(struct.pack(f"<{len(flat)}f", *flat), 34962), 5126, len(positions), "VEC3",
            min=[min(point[axis] for point in positions) for axis in range(3)],
            max=[max(point[axis] for point in positions) for axis in range(3)],
        )
        flat_normals = [value for normal in normals for value in normal]
        normal = self._accessor(self._view(struct.pack(f"<{len(flat_normals)}f", *flat_normals), 34962), 5126, len(normals), "VEC3")
        big = len(positions) > 65535
        index = self._accessor(
            self._view(struct.pack(f"<{len(indices)}{'I' if big else 'H'}", *indices), 34963),
            5125 if big else 5123, len(indices), "SCALAR")
        return {"attributes": {"POSITION": position, "NORMAL": normal}, "indices": index, "material": self.material(color)}

    def node(self, name: str, pieces, translation, yaw_deg: float, extras: dict) -> int:
        """One node per object, its mesh one primitive per (mesh, colour) piece."""
        self.gltf["meshes"].append({"name": name, "primitives": [self._primitive(to_gltf(mesh), color) for mesh, color in pieces]})
        half = math.radians(yaw_deg) / 2.0
        self.gltf["nodes"].append({
            "name": name, "mesh": len(self.gltf["meshes"]) - 1, "translation": list(translation),
            # Yaw counter-clockwise about up is the same angle about glTF +Y.
            "rotation": [0.0, math.sin(half), 0.0, math.cos(half)], "extras": extras,
        })
        self.gltf["scenes"][0]["nodes"].append(len(self.gltf["nodes"]) - 1)
        return len(self.gltf["nodes"]) - 1

    def glb(self) -> bytes:
        while len(self.binary) % 4:
            self.binary.append(0)
        self.gltf["buffers"][0]["byteLength"] = len(self.binary)
        document = json.dumps(self.gltf, separators=(",", ":")).encode("utf-8")
        document += b" " * (-len(document) % 4)
        total = 12 + 8 + len(document) + 8 + len(self.binary)
        return b"".join((
            struct.pack("<III", 0x46546C67, 2, total),
            struct.pack("<II", len(document), 0x4E4F534A), document,
            struct.pack("<II", len(self.binary), 0x004E4942), bytes(self.binary),
        ))


def environment_glb(recipe: env_schema.Recipe) -> bytes:
    builder = _GlbBuilder()
    builder.node(TERRAIN_GEOM, [(terrain_mesh(recipe.terrain), recipe.terrain.color)], (0.0, 0.0, 0.0), 0.0,
                 {"terrain": recipe.terrain_item, "kind": recipe.terrain.kind})
    for obj in recipe.objects:
        pieces = [(solid_mesh(solid), solid.color) for solid in obj.solids if solid.visible]
        builder.node(obj.id, pieces, (obj.pose.x_m, obj.pose.z_m, -obj.pose.y_m), obj.pose.yaw_deg,
                     {"object": obj.id, "item": obj.item, "type": obj.type})
    return builder.glb()


# --- MuJoCo -------------------------------------------------------------------------

def _terrain_geom(root: ET.Element, world: ET.Element, terrain: env_terrain.Terrain) -> None:
    rgba = _numbers((*hex_rgb(terrain.color), 1.0))
    friction = _numbers((terrain.friction, 0.005, 0.0001))
    if terrain.kind == "hfield":
        flat = [h for row in terrain.heights for h in row]
        low, high = min(flat), max(flat)
        if high - low > 1e-6:
            # MuJoCo rescales the data to [0, 1] (the lowest point to 0), so the
            # field spans high - low and is raised by low: heights stay absolute.
            asset = root.find("asset")
            ET.SubElement(asset, "hfield", {
                "name": "terrain", "nrow": str(terrain.nrow), "ncol": str(terrain.ncol),
                "size": _numbers((terrain.half_east, terrain.half_north, high - low, env_terrain.BASE_M)),
                "elevation": " ".join(f"{(h - low) / (high - low):.6g}" for h in flat),
            })
            ET.SubElement(world, "geom", {"name": TERRAIN_GEOM, "type": "hfield", "hfield": "terrain",
                                          "pos": _numbers((0, 0, low)), "rgba": rgba, "friction": friction})
            return
    top = 0.0 if terrain.kind != "hfield" else max(h for row in terrain.heights for h in row)
    ET.SubElement(world, "geom", {
        "name": TERRAIN_GEOM, "type": "box", "rgba": rgba, "friction": friction,
        "size": _numbers((terrain.half_east, terrain.half_north, env_terrain.BASE_M / 2)),
        "pos": _numbers((0, 0, top - env_terrain.BASE_M / 2)),
    })


def _add_boundaries(world: ET.Element, half_east: float, half_north: float) -> None:
    """Fixed boxes just outside each edge: an object touching one sticks out."""
    t, h = BOUNDARY_THICKNESS_M, BOUNDARY_HEIGHT_M
    for name, centre, half in (
        ("south", (0.0, -half_north - t / 2), (half_east + t, t / 2)),
        ("north", (0.0, half_north + t / 2), (half_east + t, t / 2)),
        ("west", (-half_east - t / 2, 0.0), (t / 2, half_north + t)),
        ("east", (half_east + t / 2, 0.0), (t / 2, half_north + t)),
    ):
        ET.SubElement(world, "geom", {"name": BOUNDARY_PREFIX + name, "type": "box", "rgba": "1 0 0 0.1",
                                      "pos": _numbers((*centre, h / 2 - 50.0)), "size": _numbers((*half, h / 2))})


def environment_mjcf(recipe: env_schema.Recipe, *, validation: bool = False) -> str:
    """The MuJoCo world: the terrain and each object's solids (see the module doc)."""
    root = ET.Element("mujoco", {"model": f"environment-{recipe.path.stem}"})
    ET.SubElement(root, "compiler", {"angle": "radian"})  # every rotation is written as a quaternion
    ET.SubElement(root, "option", {"gravity": "0 0 0" if validation else "0 0 -9.81"})
    visual = ET.SubElement(root, "visual")
    ET.SubElement(visual, "global", {"offwidth": "1280", "offheight": "720"})
    asset = ET.SubElement(root, "asset")
    world = ET.SubElement(root, "worldbody")
    ET.SubElement(world, "light", {"name": "sun", "directional": "true", "pos": "0 0 50", "dir": "-0.3 0.2 -1"})
    _terrain_geom(root, world, recipe.terrain)
    if validation:
        _add_boundaries(world, recipe.terrain.half_east, recipe.terrain.half_north)
    for obj in recipe.objects:
        body = ET.SubElement(world, "body", {
            "name": BODY_PREFIX + obj.id, "pos": _numbers((obj.pose.x_m, obj.pose.y_m, obj.pose.z_m)),
            "quat": _numbers(quaternion(0, 0, obj.pose.yaw_deg)),
        })
        if validation:
            ET.SubElement(body, "freejoint", {"name": f"joint:{obj.id}"})
            # Explicit, as a body's geoms may all be visual-only.
            ET.SubElement(body, "inertial", {"pos": "0 0 0", "mass": "1", "diaginertia": "1 1 1"})
        friction = _numbers((obj.shape.friction, 0.005, 0.0001))
        for solid in obj.solids:
            if validation and not solid.collide:
                continue
            attributes = {"name": geom_name(obj.id, solid.name), "rgba": _numbers((*hex_rgb(solid.color), 1.0)),
                          "pos": _numbers((solid.x_m, solid.y_m, solid.z_m)),
                          "quat": _numbers(quaternion(solid.roll_deg, solid.pitch_deg, solid.yaw_deg)),
                          "friction": friction}
            if solid.primitive == "box":
                attributes.update(type="box", size=_numbers((solid.width_m / 2, solid.depth_m / 2, solid.height_m / 2)))
            elif solid.primitive == "cylinder":
                attributes.update(type="cylinder", size=_numbers((solid.width_m / 2, solid.height_m / 2)))
            else:
                mesh = f"mesh:{obj.id}/{solid.name}"
                vertices = prism_vertices(solid) if solid.primitive == "prism" else wedge_vertices(solid)
                ET.SubElement(asset, "mesh", {"name": mesh, "vertex": _numbers([v for p in vertices for v in p])})
                attributes.update(type="mesh", mesh=mesh)
            if not solid.collide:
                attributes.update(contype="0", conaffinity="0", group=str(VISUAL_GROUP))
            elif obj.shape.layer == "surface":
                # Surface objects (roads, markings) do not collide with one
                # another, only with everything else (contype 2 & conaffinity 1).
                attributes.update(contype=str(SURFACE_CONTYPE), conaffinity="1")
            ET.SubElement(body, "geom", attributes)
    if not len(asset):
        root.remove(asset)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


# --- Metadata and the command -------------------------------------------------------

def fingerprint(recipe: env_schema.Recipe) -> str:
    """sha256 of what the outputs are made from (the resolved Recipe, the terrain
    grid and this generator's version): it changes exactly when they do."""
    import envstudio

    payload = {"generator": GENERATOR_VERSION, "resolved": envstudio.resolved_json(recipe),
               "heights": [list(row) for row in recipe.terrain.heights]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def manifest(recipe: env_schema.Recipe, files: dict[str, str] | None = None) -> dict:
    return {
        "schema": "hakoniwa.environment-generated/v1",
        "generator": GENERATOR_VERSION,
        "name": recipe.name,
        "recipe": str(recipe.path),
        "fingerprint": fingerprint(recipe),
        "frame": {"units": "m", "mjcf": "X=East,Y=North,Z=Up", "glb": "X=East,Y=Up,Z=-North", "origin": "centre"},
        "size_m": {"east": recipe.size_east_m, "north": recipe.size_north_m},
        "half_extent_m": {"east_west": recipe.size_east_m / 2, "north_south": recipe.size_north_m / 2},
        "terrain": {"item": recipe.terrain_item, **recipe.terrain.as_json(with_heights=False),
                    "mjcf_geom": TERRAIN_GEOM, "glb_node": TERRAIN_GEOM},
        "objects": [{
            "id": obj.id, "item": obj.item, "type": obj.type, "glb_node": obj.id, "mjcf_body": BODY_PREFIX + obj.id,
            "mjcf_geoms": [geom_name(obj.id, solid.name) for solid in obj.solids],
            "pose": {"x_m": obj.pose.x_m, "y_m": obj.pose.y_m, "z_m": obj.pose.z_m, "yaw_deg": obj.pose.yaw_deg},
            **({"source": obj.source} if obj.source else {}),
        } for obj in recipe.objects],
        **({"geo": recipe.geo} if recipe.geo else {}),
        "files": files or {},
    }


def generate(recipe: env_schema.Recipe, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"glb": out_dir / "environment.glb", "mjcf": out_dir / "environment.xml", "manifest": out_dir / "environment.json"}
    paths["glb"].write_bytes(environment_glb(recipe))
    paths["mjcf"].write_text(environment_mjcf(recipe), encoding="utf-8")
    files = {kind: {"path": paths[kind].name, "sha256": hashlib.sha256(paths[kind].read_bytes()).hexdigest()}
             for kind in ("glb", "mjcf")}
    paths["manifest"].write_text(json.dumps(manifest(recipe, files), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        paths = generate(env_schema.load_recipe(args.recipe), args.out_dir)
    except DiagnosticError as error:
        for item in error.diagnostics:
            print(f"NG  {item.path}: {item.reason}", file=sys.stderr)
        return 1
    for kind, path in paths.items():
        print(f"{kind:8} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
