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

Assets made elsewhere are passed through unchanged (a City World imported
from hakoniwa-envsim keeps Envsim's accuracy): an Envsim terrain is its own
hfield file and GLB; an object's `visual` GLB and `collision` MJCF (Envsim's
P0-P3 geoms for a building) are placed by asset_frame(): exactly where they
were made while the object stays at its anchor, moved with it otherwise. An
object with its own colliders has them instead of its solids in the world
(env_validate still checks the solids, which place it).
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


_MESHES: dict = {}


def terrain_mesh(terrain: env_terrain.Terrain):
    """The ground: a grid for an hfield, a thin slab (top at z = 0) when flat.
    An hfield's mesh is made once per grid (the 3D preview asks on every edit)."""
    if terrain.kind == "hfield":
        memo = env_terrain.grid_memo(_MESHES, terrain.heights)
        key = (terrain.grid_half_east, terrain.grid_half_north)
        if key not in memo:
            memo[key] = _terrain_mesh(terrain)
        return memo[key]
    return _terrain_mesh(terrain)


def _terrain_mesh(terrain: env_terrain.Terrain):
    if terrain.kind != "hfield":
        positions, normals, indices = _box_mesh(terrain.size_east_m, terrain.size_north_m, 0.02)
        return [(x, y, z - 0.01) for x, y, z in positions], normals, indices
    nrow, ncol, heights = terrain.nrow, terrain.ncol, terrain.heights
    half_e, half_n = terrain.grid_half_east, terrain.grid_half_north
    dx, dy = 2 * half_e / (ncol - 1), 2 * half_n / (nrow - 1)
    positions, normals, indices = [], [], []
    for row in range(nrow):
        for col in range(ncol):
            positions.append((-half_e + col * dx, half_n - row * dy, heights[row][col]))
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
        self.image_ids: dict[str, int] = {}  # image sha256 -> material index

    def _view(self, data: bytes, target: int | None) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        view = {"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        self.gltf["bufferViews"].append(view)
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

    def textured_material(self, image: bytes, mime: str) -> int:
        """A material showing an image (a JPEG or PNG embedded in the GLB), one per distinct image."""
        key = hashlib.sha256(image).hexdigest()
        if key not in self.image_ids:
            for name in ("images", "textures", "samplers"):
                self.gltf.setdefault(name, [])
            if not self.gltf["samplers"]:
                self.gltf["samplers"].append({"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497})
            self.gltf["images"].append({"bufferView": self._view(image, None), "mimeType": mime})
            self.gltf["textures"].append({"sampler": 0, "source": len(self.gltf["images"]) - 1})
            self.gltf["materials"].append({
                "name": key[:12], "doubleSided": True,
                "pbrMetallicRoughness": {"baseColorTexture": {"index": len(self.gltf["textures"]) - 1},
                                         "metallicFactor": 0.0, "roughnessFactor": 1.0},
            })
            self.image_ids[key] = len(self.gltf["materials"]) - 1
        return self.image_ids[key]

    def surface_primitive(self, positions, normals, uvs, indices, material: int) -> dict:
        """A primitive already in glTF axes (x east, y up, z -north), with
        texture coordinates when `uvs` is given (a CityGML LOD2 surface)."""
        flat = [value for point in positions for value in point]
        attributes = {"POSITION": self._accessor(
            self._view(struct.pack(f"<{len(flat)}f", *flat), 34962), 5126, len(positions), "VEC3",
            min=[min(point[axis] for point in positions) for axis in range(3)],
            max=[max(point[axis] for point in positions) for axis in range(3)])}
        flat_normals = [value for normal in normals for value in normal]
        attributes["NORMAL"] = self._accessor(
            self._view(struct.pack(f"<{len(flat_normals)}f", *flat_normals), 34962), 5126, len(normals), "VEC3")
        if uvs is not None:
            flat_uv = [value for uv in uvs for value in uv]
            attributes["TEXCOORD_0"] = self._accessor(
                self._view(struct.pack(f"<{len(flat_uv)}f", *flat_uv), 34962), 5126, len(uvs), "VEC2")
        big = len(positions) > 65535
        index = self._accessor(self._view(struct.pack(f"<{len(indices)}{'I' if big else 'H'}", *indices), 34963),
                               5125 if big else 5123, len(indices), "SCALAR")
        return {"attributes": attributes, "indices": index, "material": material}

    def asset_primitives(self, glb: bytes) -> list[dict]:
        """The primitives of a GLB this builder wrote (a part's visual asset),
        copied in with their data, textures and materials; the asset's own
        node transforms are not used (its geometry is in its part's frame)."""
        document, binary = read_glb(glb)
        views = document.get("bufferViews", [])

        def data(view_index: int) -> bytes:
            view = views[view_index]
            start = view.get("byteOffset", 0)
            return binary[start:start + view["byteLength"]]

        def accessor(index: int) -> int:
            source = document["accessors"][index]
            view = views[source["bufferView"]]
            copied = {key: value for key, value in source.items() if key not in ("bufferView", "byteOffset")}
            start = source.get("byteOffset", 0)
            raw = data(source["bufferView"])
            size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[source["type"]] * {5126: 4, 5125: 4, 5123: 2, 5121: 1}[source["componentType"]]
            chunk = raw[start:start + size * source["count"]]
            self.gltf["accessors"].append({"bufferView": self._view(chunk, view.get("target")), **copied})
            return len(self.gltf["accessors"]) - 1

        def material(index: int | None, colored: bool) -> int:
            if index is None:  # vertex colours (Envsim's terrain and roads) show as they are on white
                return self.material("#ffffff" if colored else "#c9c3b6")
            source = document["materials"][index]
            texture = source.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if texture is not None:
                image = document["images"][document["textures"][texture["index"]]["source"]]
                return self.textured_material(data(image["bufferView"]), image.get("mimeType", "image/jpeg"))
            factor = source.get("pbrMetallicRoughness", {}).get("baseColorFactor", [0.7, 0.7, 0.7, 1.0])
            key = f"asset:{json.dumps(factor)}"
            if key not in self.material_ids:
                self.gltf["materials"].append({"name": key, "doubleSided": True, "pbrMetallicRoughness": {
                    "baseColorFactor": factor, "metallicFactor": 0.0, "roughnessFactor": 0.85}})
                self.material_ids[key] = len(self.gltf["materials"]) - 1
            return self.material_ids[key]

        primitives = []
        for mesh in document.get("meshes", []):
            for primitive in mesh["primitives"]:
                copied = {
                    "attributes": {name: accessor(index) for name, index in sorted(primitive["attributes"].items())},
                    "material": material(primitive.get("material"), "COLOR_0" in primitive["attributes"]),
                }
                if "indices" in primitive:
                    copied["indices"] = accessor(primitive["indices"])
                if "mode" in primitive:
                    copied["mode"] = primitive["mode"]
                primitives.append(copied)
        return primitives

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

    def node(self, name: str, pieces, translation, yaw_deg: float, extras: dict, primitives=None) -> int:
        """One node per object, its mesh one primitive per (mesh, colour) piece
        (or the given `primitives`, such as a visual asset's)."""
        if primitives is None:
            primitives = [self._primitive(to_gltf(mesh), color) for mesh, color in pieces]
        self.gltf["meshes"].append({"name": name, "primitives": primitives})
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


def read_glb(data: bytes) -> tuple[dict, bytes]:
    """(JSON document, BIN chunk) of a GLB."""
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67 or version != 2 or length != len(data):
        raise ValueError("not a glTF 2.0 binary")
    json_length, _ = struct.unpack_from("<II", data, 12)
    document = json.loads(data[20:20 + json_length])
    rest = 20 + json_length
    binary = b""
    if rest + 8 <= len(data):
        bin_length, _ = struct.unpack_from("<II", data, rest)
        binary = data[rest + 8:rest + 8 + bin_length]
    return document, binary


# --- Assets made elsewhere -------------------------------------------------------------

def _unmoved(obj: env_schema.EnvObject) -> bool:
    anchor = obj.anchor
    return (abs(obj.pose.x_m - anchor["x_m"]) < 1e-9 and abs(obj.pose.y_m - anchor["y_m"]) < 1e-9
            and abs((obj.pose.yaw_deg - anchor["yaw_deg"] + 180.0) % 360.0 - 180.0) < 1e-9)


def asset_frame(recipe: env_schema.Recipe, obj: env_schema.EnvObject) -> tuple[float, float, float, float]:
    """(x, y, z, yaw) in the environment of the frame an object's assets were
    made in (its visual GLB, its colliders).

    Without an anchor it is the object's pose. With one, and on the terrain
    the anchor names (the same Envsim hfield), the assets stay exactly where
    they were made while the object stays at its anchor: z is the anchor's
    (Envsim's height of that frame). Moved, they turn and move with it and
    rise or sink by how much the ground under its outline differs between
    the anchor and the new place. On another terrain (a flat ground chosen
    instead), the frame stands on the ground under the object."""
    pose, anchor = obj.pose, obj.anchor
    if anchor is None:
        return pose.x_m, pose.y_m, pose.z_m, pose.yaw_deg
    yaw = pose.yaw_deg - anchor["yaw_deg"]
    hfield = recipe.terrain.hfield
    if not (anchor.get("terrain") and hfield and hfield["sha256"] == anchor["terrain"]):
        return pose.x_m, pose.y_m, pose.z_m, yaw
    if _unmoved(obj):
        return anchor["x_m"], anchor["y_m"], anchor["z_m"], 0.0
    at_anchor = env_schema.replace(obj, pose=env_schema.Pose(anchor["x_m"], anchor["y_m"], 0.0, anchor["yaw_deg"]))
    rise = (recipe.terrain.highest_under(env_schema.footprint(obj))
            - recipe.terrain.highest_under(env_schema.footprint(at_anchor)))
    return pose.x_m, pose.y_m, anchor["z_m"] + rise, yaw


def environment_glb(recipe: env_schema.Recipe) -> bytes:
    builder = _GlbBuilder()
    terrain = recipe.terrain
    extras = {"terrain": recipe.terrain_item, "kind": terrain.kind}
    if terrain.visual is not None:  # Envsim's own terrain GLB, as it is
        builder.node(TERRAIN_GEOM, None, (0.0, 0.0, 0.0), 0.0, {**extras, "visual": terrain.visual.name},
                     primitives=builder.asset_primitives(terrain.visual.read_bytes()))
    else:
        builder.node(TERRAIN_GEOM, [(terrain_mesh(terrain), terrain.color)], (0.0, 0.0, 0.0), 0.0, extras)
    for obj in recipe.objects:
        extras = {"object": obj.id, "item": obj.item, "type": obj.type}
        placement = ((obj.pose.x_m, obj.pose.z_m, -obj.pose.y_m), obj.pose.yaw_deg)
        if obj.visual is not None:  # its own look (LOD2 with textures, an Envsim layer) instead of the solids
            x, y, z, yaw = asset_frame(recipe, obj)
            builder.node(obj.id, None, (x, z, -y), yaw, {**extras, "visual": obj.visual.name},
                         primitives=builder.asset_primitives(obj.visual.read_bytes()))
            continue
        pieces = [(solid_mesh(solid), solid.color) for solid in obj.solids if solid.visible]
        builder.node(obj.id, pieces, *placement, extras)
    return builder.glb()


# --- MuJoCo -------------------------------------------------------------------------

# Envsim's MuJoCo frame (x north, y west) turned into this one (x east, y north): +90 degrees about z.
ENVSIM_FRAME_QUAT = (math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5))


def _terrain_geom(root: ET.Element, world: ET.Element, terrain: env_terrain.Terrain,
                  hfield_file: str | None = None) -> None:
    rgba = _numbers((*hex_rgb(terrain.color), 1.0))
    friction = _numbers((terrain.friction, 0.005, 0.0001))
    if terrain.hfield:  # Envsim's hfield file itself, in its frame
        ET.SubElement(root.find("asset"), "hfield", {
            "name": "terrain", "file": hfield_file or terrain.hfield["path"],
            "size": " ".join(repr(float(value)) for value in terrain.hfield["size"])})
        ET.SubElement(world, "geom", {"name": TERRAIN_GEOM, "type": "hfield", "hfield": "terrain", "pos": "0 0 0",
                                      "quat": _numbers(ENVSIM_FRAME_QUAT), "rgba": rgba, "friction": friction})
        return
    if terrain.kind == "hfield":
        rows, cols, samples = env_terrain.envsim_order(terrain)
        low, high = min(samples), max(samples)
        if high - low > 1e-6:
            # Laid out as an Envsim hfield (and an Urban export, env_urban.py):
            # the same cells split along the same diagonals whichever frame the
            # world is written in. MuJoCo rescales the data to [0, 1] (the lowest
            # point to 0), so the field spans high - low and is raised by low;
            # it reads inline elevation rows from the +y edge (a file's from -y),
            # hence the rows reversed.
            asset = root.find("asset")
            ET.SubElement(asset, "hfield", {
                "name": "terrain", "nrow": str(rows), "ncol": str(cols),
                "size": _numbers((terrain.grid_half_north, terrain.grid_half_east, high - low, env_terrain.BASE_M)),
                "elevation": " ".join(repr((samples[r * cols + c] - low) / (high - low))
                                      for r in reversed(range(rows)) for c in range(cols)),
            })
            ET.SubElement(world, "geom", {"name": TERRAIN_GEOM, "type": "hfield", "hfield": "terrain",
                                          "pos": _numbers((0, 0, low)), "quat": _numbers(ENVSIM_FRAME_QUAT),
                                          "rgba": rgba, "friction": friction})
            return
    top = 0.0 if terrain.kind != "hfield" else max(h for row in terrain.heights for h in row)
    ET.SubElement(world, "geom", {
        "name": TERRAIN_GEOM, "type": "box", "rgba": rgba, "friction": friction,
        "size": _numbers((terrain.half_east, terrain.half_north, env_terrain.BASE_M / 2)),
        "pos": _numbers((0, 0, top - env_terrain.BASE_M / 2)),
    })


def _add_boundaries(world: ET.Element, half_east: float, half_north: float, top_m: float = 0.0) -> None:
    """Fixed boxes just outside each edge, from 50 m under the ground to above
    the highest object (`top_m`): an object touching one sticks out."""
    t, h = BOUNDARY_THICKNESS_M, max(BOUNDARY_HEIGHT_M, top_m + 60.0)
    for name, centre, half in (
        ("south", (0.0, -half_north - t / 2), (half_east + t, t / 2)),
        ("north", (0.0, half_north + t / 2), (half_east + t, t / 2)),
        ("west", (-half_east - t / 2, 0.0), (t / 2, half_north + t)),
        ("east", (half_east + t / 2, 0.0), (t / 2, half_north + t)),
    ):
        ET.SubElement(world, "geom", {"name": BOUNDARY_PREFIX + name, "type": "box", "rgba": "1 0 0 0.1",
                                      "pos": _numbers((*centre, h / 2 - 50.0)), "size": _numbers((*half, h / 2))})


_FRAGMENTS: dict = {}


def _fragment(obj: env_schema.EnvObject) -> tuple[list, list]:
    """(asset children, worldbody children) of an object's collision MJCF,
    read once per content."""
    key = obj.collision_sha256
    if key not in _FRAGMENTS:
        if len(_FRAGMENTS) > 4096:
            _FRAGMENTS.clear()
        root = ET.parse(obj.collision).getroot()
        _FRAGMENTS[key] = (list(root.find("asset") if root.find("asset") is not None else []),
                           list(root.find("worldbody") if root.find("worldbody") is not None else []))
    return _FRAGMENTS[key]


def euler_quaternion(degrees, sequence: str = "xyz") -> tuple[float, float, float, float]:
    """MuJoCo's euler (degrees, its default eulerseq "xyz"): lower-case axes
    turn with the frame (each rotation multiplied on the right), upper-case
    axes stay fixed (on the left)."""
    q = (1.0, 0.0, 0.0, 0.0)
    for angle, axis in zip(degrees, sequence):
        half = math.radians(angle) / 2
        c, s = math.cos(half), math.sin(half)
        r = (c, s if axis in "xX" else 0.0, s if axis in "yY" else 0.0, s if axis in "zZ" else 0.0)
        q = _qmul(q, r) if axis.islower() else _qmul(r, q)
    return q


def _qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


# Attributes of an Envsim fragment that name something (made unique per object).
_NAMED = ("name", "mesh", "material", "texture", "hfield", "class", "childclass")


def _copied(element: ET.Element, prefix: str) -> ET.Element:
    """A fragment element for this world: names prefixed with the object's id
    (a building copied twice stays two), euler degrees as a quaternion (this
    world compiles angles in radians, an exported one has no compiler at all);
    every number else as Envsim wrote it."""
    copy = ET.Element(element.tag, dict(element.attrib))
    for key in _NAMED:
        if key in copy.attrib:
            copy.set(key, f"{prefix}/{copy.get(key)}")
    if "euler" in copy.attrib:
        copy.set("quat", " ".join(repr(value) for value in euler_quaternion(
            [float(value) for value in copy.attrib.pop("euler").split()])))
    if "axisangle" in copy.attrib:  # a quaternion too: no angle is left for a compiler setting to read
        x, y, z, angle = (float(value) for value in copy.attrib.pop("axisangle").split())
        size = math.sqrt(x * x + y * y + z * z) or 1.0
        half = math.radians(angle) / 2
        copy.set("quat", " ".join(repr(value) for value in (
            math.cos(half), x / size * math.sin(half), y / size * math.sin(half), z / size * math.sin(half))))
    for child in element:
        copy.append(_copied(child, prefix))
    return copy


def _add_collision(asset: ET.Element, world: ET.Element, recipe: env_schema.Recipe, obj: env_schema.EnvObject) -> None:
    """An object's own colliders: Envsim's geoms as written (in Envsim's frame),
    carried from the anchor's frame to asset_frame(). Without an anchor (a
    Catalog's building) they are in the object's own frame, in Envsim's axes."""
    x, y, z, yaw = asset_frame(recipe, obj)
    anchor = obj.anchor or {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0}
    body = ET.SubElement(world, "body", {"name": BODY_PREFIX + obj.id, "pos": _numbers((x, y, z)),
                                         "quat": _numbers(quaternion(0, 0, yaw))})
    frame = ET.SubElement(body, "body", {"name": f"{obj.id}/envsim-frame",
                                         "pos": _numbers((-anchor["x_m"], -anchor["y_m"], -anchor["z_m"])),
                                         "quat": _numbers(ENVSIM_FRAME_QUAT)})
    assets, bodies = _fragment(obj)
    for element in assets:
        asset.append(_copied(element, obj.id))
    for element in bodies:
        frame.append(_copied(element, obj.id))


def environment_mjcf(recipe: env_schema.Recipe, *, validation: bool = False, hfield_file: str | None = None) -> str:
    """The MuJoCo world: the terrain and each object's solids (see the module
    doc). `hfield_file` is how the world names an Envsim terrain's hfield
    file (by default its absolute path)."""
    root = ET.Element("mujoco", {"model": f"environment-{recipe.path.stem}"})
    ET.SubElement(root, "compiler", {"angle": "radian"})  # every rotation is written as a quaternion
    ET.SubElement(root, "option", {"gravity": "0 0 0" if validation else "0 0 -9.81"})
    visual = ET.SubElement(root, "visual")
    ET.SubElement(visual, "global", {"offwidth": "1280", "offheight": "720"})
    asset = ET.SubElement(root, "asset")
    world = ET.SubElement(root, "worldbody")
    ET.SubElement(world, "light", {"name": "sun", "directional": "true", "pos": "0 0 50", "dir": "-0.3 0.2 -1"})
    _terrain_geom(root, world, recipe.terrain, hfield_file)
    if validation:
        top = max([recipe.terrain.max_height_m] + [obj.pose.z_m + obj.shape.height_m for obj in recipe.objects])
        _add_boundaries(world, recipe.terrain.half_east, recipe.terrain.half_north, top)
    for obj in recipe.objects:
        if obj.collision is not None and not validation:
            _add_collision(asset, world, recipe, obj)
            continue
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
            if (validation or obj.visual is not None) and not solid.collide:
                continue  # its GLB is its look (a layer's outlines only draw it on the plan)
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
    payload = {"generator": GENERATOR_VERSION, "resolved": env_schema.resolved_json(recipe),
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
    hfield_file = None
    if recipe.terrain.hfield:  # the world loads Envsim's hfield file itself, copied beside it
        source = Path(recipe.terrain.hfield["path"])
        paths["hfield"] = out_dir / "environment-terrain.hf"
        paths["hfield"].write_bytes(source.read_bytes())
        hfield_file = paths["hfield"].name
    paths["mjcf"].write_text(environment_mjcf(recipe, hfield_file=hfield_file), encoding="utf-8")
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
