#!/usr/bin/env python3
"""Builds the 箱庭 town parts of the starter Catalog: 箱庭屋台 (a street stall)
and 箱庭オープンスペース (a small square with parasols, benches and trees).

One description per part writes both
- its type (types/food-stall.yaml, types/open-space.yaml): boxes and
  cylinders that collide, draw it on the plan and stand in for its look, and
- its looks (catalogs/starter/assets/*.glb): the same part with rounded
  edges, lanterns, parasols and the Japanese lettering as textures, which the
  Catalog items name as their `visual`.

Positions are written as seen from the front: u to the right, y towards the
viewer (the part faces +y), z up; the object's x is -u.

Run it in the Business Pack Workspace from this repository:
    python3.12 ../hakoniwa-business-pack/tools/workspace.py run -- \\
        python catalogs/starter/build_town_assets.py
The lettering needs Noto Sans CJK JP (SIL Open Font License); set
HAKONIWA_JP_FONT to its Bold face if it is not in a usual font folder. Only
the rendered letters go into the GLBs, not the font.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent / "assets"

DARK = "#3a3d42"
FRAME = "#4a4e54"
WHITE = "#f4f4f2"
BLUE = "#3d6fb6"
GREY = "#b8bcc2"
GREEN = "#5aa83c"
LEAF = "#6dbd4a"
LAMP = "#fbf8ef"
WOOD = "#c8925a"
TRUNK = "#8a5a3b"
CONCRETE = "#c9ccd0"
INK = "#2f3237"
BULB = "#ffe7b0"
LIGHT = "#fff3c4"
# Lights glow (emissive) so they show in the viewer's night mode: colour -> emission strength.
GLOWS = {LAMP: 0.85, BULB: 1.0, LIGHT: 1.0}

STALL_COLOURS = {"orange": "#f26b1d", "red": "#d8352a", "blue": "#2f6fc0"}
OPEN_SPACE_ACCENT = "#f26b1d"


# --- Lettering and marks (textures) -------------------------------------------------

def _font_path(weight: str) -> str:
    given = os.environ.get("HAKONIWA_JP_FONT", "").strip()
    if given and weight == "Bold":
        return given
    names = [f"NotoSansCJKjp-{weight}.otf", f"NotoSansCJK-{weight}.ttc", f"NotoSansJP-{weight}.otf"]
    folders = [Path.home() / "Library/Fonts", Path("/Library/Fonts"), Path("/usr/share/fonts/opentype/noto"),
               Path("/usr/share/fonts/noto-cjk"), Path("/usr/share/fonts/truetype/noto")]
    for folder in folders:
        for name in names:
            if (folder / name).is_file():
                return str(folder / name)
    raise SystemExit(f"Noto Sans CJK JP {weight} not found; set HAKONIWA_JP_FONT")


FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(px: float, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    key = (weight, max(1, round(px)))
    if key not in FONTS:
        FONTS[key] = ImageFont.truetype(_font_path(weight), key[1])
    return FONTS[key]


class Canvas:
    """A texture for a w_m × h_m panel; drawing in metres from its top-left."""

    def __init__(self, w_m: float, h_m: float, background: str, ppm: float = 700):
        self.ppm = ppm
        self.image = Image.new("RGB", (max(8, round(w_m * ppm)), max(8, round(h_m * ppm))), background)
        self.draw = ImageDraw.Draw(self.image)

    def p(self, v: float) -> float:
        return v * self.ppm

    def text(self, x, y, text, size, fill, weight="Bold", anchor="lm", spacing=0.0):
        self.draw.text((self.p(x), self.p(y)), text, font=font(self.p(size), weight), fill=fill, anchor=anchor,
                       spacing=self.p(spacing))

    def rect(self, x0, y0, x1, y1, fill, radius=0.0):
        self.draw.rounded_rectangle((self.p(x0), self.p(y0), self.p(x1), self.p(y1)), radius=self.p(radius), fill=fill)

    def house(self, cx, cy, s, fill, window):
        """The 箱庭 mark: a house of width and height s with a window."""
        x0, y0 = cx - s / 2, cy - s / 2
        pts = [(x0, y0 + 0.46 * s), (cx, y0), (x0 + s, y0 + 0.46 * s), (x0 + s, y0 + s), (x0, y0 + s)]
        self.draw.polygon([(self.p(x), self.p(y)) for x, y in pts], fill=fill)
        w = 0.44 * s
        self.rect(cx - w / 2, y0 + 0.5 * s, cx + w / 2, y0 + 0.5 * s + w, window, radius=0.07 * s)

    def face(self, cx, cy, s, fill, ink):
        """The smiling square of the 箱庭 town."""
        self.rect(cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2, fill, radius=0.18 * s)
        for dx in (-0.17, 0.17):
            self.rect(cx + dx * s - 0.04 * s, cy - 0.2 * s, cx + dx * s + 0.04 * s, cy + 0.0 * s, ink, radius=0.04 * s)
        box = (self.p(cx - 0.2 * s), self.p(cy - 0.05 * s), self.p(cx + 0.2 * s), self.p(cy + 0.25 * s))
        self.draw.arc(box, 20, 160, fill=ink, width=max(2, round(self.p(0.06 * s))))

    def png(self) -> bytes:
        out = io.BytesIO()
        self.image.save(out, "PNG", optimize=True)
        return out.getvalue()


# --- Meshes ---------------------------------------------------------------------------------

def rotation(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """yaw about z, then pitch about y, then roll about x (as env_types)."""
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


@dataclass
class Mesh:
    positions: np.ndarray
    normals: np.ndarray
    indices: np.ndarray
    uvs: np.ndarray | None = None


def oriented(positions, normals, triangles) -> np.ndarray:
    """Triangles turned so each faces the way its vertex normals point."""
    tri = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    a, b, c = (positions[tri[:, i]] for i in range(3))
    facing = np.einsum("ij,ij->i", np.cross(b - a, c - a), normals[tri].sum(axis=1))
    flip = facing < 0
    tri[flip] = tri[flip][:, ::-1]
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    return tri[area > 1e-12]


def rounded_box(w, d, h, r, arc=3) -> Mesh:
    """A box with its edges and corners rounded by r (a plain box when r = 0)."""
    half = np.array([w, d, h]) / 2
    r = min(r, *(half * 0.98))
    inner = half - r
    positions, normals, triangles = [], [], []
    for axis in range(3):
        b, c = [i for i in range(3) if i != axis]
        for sign in (-1, 1):
            def samples(i):
                if r <= 0:
                    return [-half[i], half[i]]
                ks = [inner[i] + r * math.cos(math.pi / 2 * k / arc) for k in range(arc + 1)]
                return [-v for v in ks] + ks[::-1]
            tb, tc = samples(b), samples(c)
            start = len(positions)
            for vb in tb:
                for vc in tc:
                    q = np.zeros(3)
                    q[axis], q[b], q[c] = sign * half[axis], vb, vc
                    centre = np.clip(q, -inner, inner)
                    offset = q - centre
                    length = np.linalg.norm(offset)
                    if r > 0 and length > 1e-12:
                        positions.append(centre + r * offset / length)
                        normals.append(offset / length)
                    else:
                        positions.append(q)
                        n = np.zeros(3)
                        n[axis] = sign
                        normals.append(n)
            nc = len(tc)
            for i in range(len(tb) - 1):
                for j in range(nc - 1):
                    v = start + i * nc + j
                    triangles += [v, v + nc, v + 1, v + 1, v + nc, v + nc + 1]
    positions, normals = np.array(positions), np.array(normals)
    return Mesh(positions, normals, oriented(positions, normals, triangles))


def lathe(profile, segments=32, a0=0.0, a1=360.0) -> Mesh:
    """A surface of revolution about z: profile [(radius, z, n_radial, n_z)]."""
    positions, normals, triangles = [], [], []
    steps = max(1, round(segments * (a1 - a0) / 360))
    for k in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * k / steps)
        ca, sa = math.cos(a), math.sin(a)
        for radius, z, nr, nz in profile:
            positions.append((radius * ca, radius * sa, z))
            n = np.array([nr * ca, nr * sa, nz])
            normals.append(n / (np.linalg.norm(n) or 1))
    m = len(profile)
    for k in range(steps):
        for i in range(m - 1):
            v = k * m + i
            triangles += [v, v + m, v + 1, v + 1, v + m, v + m + 1]
    positions, normals = np.array(positions, dtype=float), np.array(normals)
    return Mesh(positions, normals, oriented(positions, normals, triangles))


def cylinder_profile(diameter, h, r=0.0, arc=3):
    radius = diameter / 2
    r = min(r, radius * 0.98, h / 2 * 0.98)
    if r <= 0:
        return [(0, -h / 2, 0, -1), (radius, -h / 2, 0, -1), (radius, -h / 2, 1, 0), (radius, h / 2, 1, 0),
                (radius, h / 2, 0, 1), (0, h / 2, 0, 1)]
    profile = [(0, -h / 2, 0, -1)]
    for k in range(arc + 1):
        phi = -math.pi / 2 + math.pi / 2 * k / arc
        profile.append((radius - r + r * math.cos(phi), -h / 2 + r + r * math.sin(phi), math.cos(phi), math.sin(phi)))
    for k in range(arc + 1):
        phi = math.pi / 2 * k / arc
        profile.append((radius - r + r * math.cos(phi), h / 2 - r + r * math.sin(phi), math.cos(phi), math.sin(phi)))
    profile.append((0, h / 2, 0, 1))
    return profile


def ellipsoid_profile(rx, rz, rings=12):
    out = []
    for k in range(rings + 1):
        phi = -math.pi / 2 + math.pi * k / rings
        out.append((rx * math.cos(phi), rz * math.sin(phi), math.cos(phi) / rx, math.sin(phi) / rz))
    return out


def canopy_profile(radius, rise, rim=0.05):
    """A parasol: a shallow cone from its apex to the rim, a drop at the rim,
    and an underside; centred on the rim's height."""
    slope = (rise, radius)  # outward normal of the cone (radial, z) before normalising
    return [(0.0, rise, *slope), (radius, 0.0, *slope), (radius, 0.0, 1, 0), (radius, -rim, 1, 0),
            (radius, -rim, -rise, -radius), (0.0, rise - rim * 1.2, -rise, -radius)]


def quad(w, h) -> Mesh:
    """A panel facing +y (the viewer), image right along +u (-x)."""
    positions = np.array([[w / 2, 0, -h / 2], [-w / 2, 0, -h / 2], [-w / 2, 0, h / 2], [w / 2, 0, h / 2]], float)
    normals = np.tile([0.0, 1.0, 0.0], (4, 1))
    uvs = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], float)
    return Mesh(positions, normals, oriented(positions, normals, [0, 1, 2, 0, 2, 3]), uvs)


# --- A part: its solids (type) and its looks (GLB) --------------------------------------------

def _num(v: float) -> str:
    v = round(float(v), 4)
    return str(int(v)) if v == int(v) else repr(v)


@dataclass
class Asset:
    name: str
    accent: str
    variant: str = ""
    shapes: list = field(default_factory=list)       # type solids (dicts), in the object's frame
    looks: list = field(default_factory=list)        # (Mesh in object frame, colour or PNG bytes)
    only: str | None = None                          # the layout the next pieces belong to

    def _place(self, mesh: Mesh, u, y, z, roll=0.0, pitch=0.0, yaw=0.0) -> Mesh:
        rot = rotation(roll, pitch, yaw)
        positions = mesh.positions @ rot.T + np.array([-u, y, z])
        return Mesh(positions, mesh.normals @ rot.T, mesh.indices, mesh.uvs)

    def _keep(self) -> bool:
        return self.only is None or self.only == self.variant

    def _solid(self, name, primitive, w, d, h, u, y, z, colour, collide, roll, pitch, yaw):
        solid = {"name": name, "primitive": primitive, "w": w, "d": d, "h": h, "x": -u, "y": y, "z": z}
        for key, value in (("roll", roll), ("pitch", pitch), ("yaw", yaw)):
            if value:
                solid[key] = value
        if colour is not None:
            solid["color"] = colour
        if not collide:
            solid["collide"] = False
        if self.only:
            solid["when"] = f'$layout == "{self.only}"'
        self.shapes.append(solid)

    def box(self, name, w, d, h, u=0.0, y=0.0, z=None, colour=None, collide=False, r=0.0,
            roll=0.0, pitch=0.0, yaw=0.0, solid=True, look=True):
        z = h / 2 if z is None else z
        if solid:
            self._solid(name, "box", w, d, h, u, y, z, colour, collide, roll, pitch, yaw)
        if look and self._keep():
            self.looks.append((self._place(rounded_box(w, d, h, r), u, y, z, roll, pitch, yaw), colour or self.accent))

    def cylinder(self, name, diameter, h, u=0.0, y=0.0, z=None, colour=None, collide=False, r=0.0,
                 roll=0.0, pitch=0.0, yaw=0.0, solid=True, look=True, segments=32):
        z = h / 2 if z is None else z
        if solid:
            self._solid(name, "cylinder", diameter, diameter, h, u, y, z, colour, collide, roll, pitch, yaw)
        if look and self._keep():
            mesh = lathe(cylinder_profile(diameter, h, r), segments)
            self.looks.append((self._place(mesh, u, y, z, roll, pitch, yaw), colour or self.accent))

    def shape(self, mesh: Mesh, colour, u=0.0, y=0.0, z=0.0, roll=0.0, pitch=0.0, yaw=0.0):
        """Looks only (a lantern, a parasol, a bush)."""
        if self._keep():
            self.looks.append((self._place(mesh, u, y, z, roll, pitch, yaw), colour or self.accent))

    def panel(self, canvas: Canvas, w, h, u, y, z, roll=0.0, pitch=0.0, yaw=0.0):
        """A printed panel (lettering, marks) facing +y unless turned."""
        if self._keep():
            self.looks.append((self._place(quad(w, h), u, y, z, roll, pitch, yaw), canvas.png()))

    # --- Output ------------------------------------------------------------------------

    def yaml_shapes(self) -> str:
        lines = []
        for solid in self.shapes:
            parts = []
            for key, value in solid.items():
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif isinstance(value, (int, float)):
                    value = _num(value)
                elif key == "color":
                    value = f'"{value}"'
                elif key == "when":
                    value = f"'{value}'"
                parts.append(f"{key}: {value}")
            lines.append("      - {" + ", ".join(parts) + "}")
        return "\n".join(lines)

    def glb(self) -> bytes:
        """The looks as one GLB in glTF axes (x east, y up, z -north); the
        same colour's pieces share one primitive."""
        groups: dict[object, list[Mesh]] = {}
        for mesh, material in self.looks:
            groups.setdefault(material, []).append(mesh)
        writer = GlbWriter()
        for material, meshes in groups.items():
            positions, normals, uvs, indices, base = [], [], [], [], 0
            for mesh in meshes:
                p = mesh.positions
                positions.append(np.column_stack([p[:, 0], p[:, 2], -p[:, 1]]))
                n = mesh.normals
                normals.append(np.column_stack([n[:, 0], n[:, 2], -n[:, 1]]))
                if mesh.uvs is not None:
                    uvs.append(mesh.uvs)
                indices.append(mesh.indices.reshape(-1) + base)
                base += len(p)
            writer.primitive(np.vstack(positions), np.vstack(normals), np.concatenate(indices),
                             np.vstack(uvs) if isinstance(material, bytes) else None, material)
        return writer.bytes()


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


class GlbWriter:
    def __init__(self):
        self.binary = bytearray()
        self.doc = {"asset": {"version": "2.0", "generator": "hakoniwa-environment-studio build_town_assets"},
                    "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                    "meshes": [{"primitives": []}], "buffers": [], "bufferViews": [], "accessors": [],
                    "materials": []}

    def _view(self, data: bytes, target=None) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        view = {"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(data)}
        if target:
            view["target"] = target
        self.binary += data
        self.doc["bufferViews"].append(view)
        return len(self.doc["bufferViews"]) - 1

    def _accessor(self, array: np.ndarray, kind: str, target, component=5126, bounds=False) -> int:
        data = array.astype(np.float32 if component == 5126 else np.uint32).tobytes()
        accessor = {"bufferView": self._view(data, target), "componentType": component,
                    "count": len(array), "type": kind}
        if bounds:
            accessor["min"] = array.min(axis=0).tolist()
            accessor["max"] = array.max(axis=0).tolist()
        self.doc["accessors"].append(accessor)
        return len(self.doc["accessors"]) - 1

    def _material(self, material) -> int:
        if isinstance(material, bytes):
            self.doc.setdefault("samplers", [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}])
            self.doc.setdefault("images", []).append({"bufferView": self._view(material), "mimeType": "image/png"})
            self.doc.setdefault("textures", []).append({"sampler": 0, "source": len(self.doc["images"]) - 1})
            entry = {"pbrMetallicRoughness": {"baseColorTexture": {"index": len(self.doc["textures"]) - 1},
                                              "metallicFactor": 0.0, "roughnessFactor": 0.85}, "doubleSided": True}
        else:
            rgb = [srgb_to_linear(int(material[i:i + 2], 16) / 255) for i in (1, 3, 5)]
            entry = {"name": material, "pbrMetallicRoughness": {"baseColorFactor": [*rgb, 1.0],
                                                                "metallicFactor": 0.0, "roughnessFactor": 0.85}}
            if material in GLOWS:
                entry["emissiveFactor"] = [round(c * GLOWS[material], 4) for c in rgb]
        self.doc["materials"].append(entry)
        return len(self.doc["materials"]) - 1

    def primitive(self, positions, normals, indices, uvs, material):
        attributes = {"POSITION": self._accessor(positions, "VEC3", 34962, bounds=True),
                      "NORMAL": self._accessor(normals, "VEC3", 34962)}
        if uvs is not None:
            attributes["TEXCOORD_0"] = self._accessor(uvs, "VEC2", 34962)
        self.doc["meshes"][0]["primitives"].append({
            "attributes": attributes, "material": self._material(material),
            "indices": self._accessor(indices, "SCALAR", 34963, component=5125)})

    def bytes(self) -> bytes:
        while len(self.binary) % 4:
            self.binary.append(0)
        self.doc["buffers"] = [{"byteLength": len(self.binary)}]
        text = json.dumps(self.doc, separators=(",", ":")).encode()
        text += b" " * (-len(text) % 4)
        return (struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + 8 + len(self.binary))
                + struct.pack("<II", len(text), 0x4E4F534A) + text
                + struct.pack("<II", len(self.binary), 0x004E4942) + bytes(self.binary))


# --- Shared pieces ---------------------------------------------------------------------------

def sign_board(a: Asset, u, y, title, lines, prefix="sign"):
    """An A-frame board facing +y with the mark, a title and small lines."""
    tilt, height, width, z = 12, 0.90, 0.56, 0.45
    s, c = math.sin(math.radians(tilt)), math.cos(math.radians(tilt))
    for side, tag in ((1, "front"), (-1, "back")):
        a.box(f"{prefix}-{tag}", width, 0.03, height, u, y + side * 0.09, z, INK, collide=True, r=0.012,
              roll=side * tilt)
    canvas = Canvas(width - 0.06, height - 0.08, INK)
    w = width - 0.06
    canvas.house(w / 2, 0.15, 0.17, a.accent, INK)
    canvas.text(w / 2, 0.36, title, 0.085, "#ffffff", anchor="mm")
    for i, line in enumerate(lines):
        canvas.text(w / 2, 0.47 + i * 0.06, line, 0.042, "#ffffff", weight="Medium", anchor="mm")
    canvas.rect(w / 2 - 0.08, 0.68, w / 2 + 0.08, 0.70, a.accent, radius=0.01)
    a.panel(canvas, w, height - 0.08, u, y + 0.09 + 0.016 * c, z + 0.016 * s, roll=tilt)


def house_canvas(size, background, accent, window=None) -> Canvas:
    canvas = Canvas(size, size, background, ppm=500)
    canvas.house(size / 2, size / 2 + 0.02 * size, size * 0.74, accent, window or background)
    return canvas


# --- 箱庭屋台 ----------------------------------------------------------------------------------

def stall(accent: str, variant: str) -> Asset:
    a = Asset("food_stall", accent, variant)

    # Frame and roof.
    for su, sy, tag in ((-1, 1, "front-left"), (1, 1, "front-right"), (-1, -1, "rear-left"), (1, -1, "rear-right")):
        a.box(f"pole-{tag}", 0.08, 0.08, 2.36, su * 1.12, sy * 0.62, colour=DARK, collide=True, r=0.02)
    roof_z, roof_h, roof_w = 2.53, 0.34, 2.36
    a.box("roof", roof_w, 1.62, roof_h, 0, 0.0, roof_z, WHITE, collide=True, r=0.07)
    for su, tag in ((-1, "left"), (1, "right")):
        a.box(f"roof-end-{tag}", 0.14, 1.66, roof_h + 0.03, su * (roof_w / 2 + 0.06), 0.0, roof_z, collide=True, r=0.06)
    # A strip of bulbs under the front of the roof (they glow at night).
    a.box("roof-light", roof_w - 0.2, 0.05, 0.03, 0, 0.74, roof_z - roof_h / 2 - 0.02, BULB, r=0.012, solid=False)
    sign = Canvas(2.0, 0.21, WHITE)
    sign.house(0.12, 0.105, 0.16, accent, WHITE)
    sign.text(0.24, 0.085, "箱庭屋台", 0.125, INK)
    sign.text(0.25, 0.18, "Hakoniwa Stall Asset", 0.04, "#55595f", weight="Medium")
    a.panel(sign, 2.0, 0.21, -0.05, 0.811, roof_z)

    # Noren: the mark, the name, the smiling square.
    for i, u in enumerate((-0.68, 0.0, 0.68), start=1):
        a.box(f"noren-{i}", 0.64, 0.02, 0.56, u, 0.70, roof_z - roof_h / 2 - 0.28, r=0.008)
        cloth = Canvas(0.6, 0.52, accent, ppm=500)
        if i == 1:
            cloth.house(0.3, 0.26, 0.26, "#ffffff", accent)
        elif i == 2:
            cloth.text(0.3, 0.17, "箱庭", 0.15, "#ffffff", anchor="mm")
            cloth.text(0.3, 0.35, "屋台", 0.15, "#ffffff", anchor="mm")
        else:
            cloth.face(0.3, 0.26, 0.26, "#ffffff", accent)
        a.panel(cloth, 0.6, 0.52, u, 0.711, roof_z - roof_h / 2 - 0.28)

    # Lanterns at the front corners.
    for su, tag in ((-1, "left"), (1, "right")):
        u, y, z = su * 1.34, 0.62, 1.88
        a.cylinder(f"lantern-{tag}-cord", 0.02, 0.15, u, y, 2.285, DARK)
        a.cylinder(f"lantern-{tag}-top", 0.2, 0.06, u, y, z + 0.24, DARK, r=0.02)
        a.cylinder(f"lantern-{tag}", 0.3, 0.42, u, y, z, LAMP, look=False)
        a.shape(lathe(ellipsoid_profile(0.16, 0.24)), LAMP, u, y, z)
        a.cylinder(f"lantern-{tag}-bottom", 0.2, 0.06, u, y, z - 0.24, DARK, r=0.02)
        a.box(f"lantern-{tag}-arm", 0.22, 0.04, 0.04, su * 1.23, y, 2.32, DARK, r=0.01)
        a.panel(house_canvas(0.12, LAMP, accent), 0.12, 0.12, u, y + 0.163, z)

    # Counter.
    cy, cd = 0.32, 0.55
    a.box("counter", 2.2, cd, 0.85, 0, cy, 0.525, WHITE, collide=True, r=0.04)
    a.box("counter-base", 2.3, cd + 0.04, 0.10, 0, cy, 0.05, DARK, collide=True, r=0.02)
    a.box("counter-top", 2.34, cd + 0.08, 0.05, 0, cy + 0.01, 0.975, FRAME, collide=True, r=0.02)
    for su, tag in ((-1, "left"), (1, "right")):
        a.box(f"counter-post-{tag}", 0.06, cd + 0.04, 0.85, su * 1.13, cy, 0.525, DARK, r=0.02)
    front = Canvas(2.0, 0.62, WHITE, ppm=500)
    front.text(0.1, 0.26, "小さな たのしい しあわせを。", 0.085, INK)
    front.rect(0.1, 0.36, 0.4, 0.385, accent, radius=0.012)
    front.face(1.72, 0.3, 0.2, INK, "#ffffff")
    a.panel(front, 2.0, 0.62, 0, cy + cd / 2 + 0.002, 0.53)

    # On the counter.
    top = 1.0
    a.box("register", 0.30, 0.26, 0.14, -0.78, 0.26, top + 0.07, DARK, r=0.02)
    a.box("register-screen", 0.24, 0.03, 0.16, -0.78, 0.16, top + 0.20, "#2b2e33", r=0.01, roll=-20)
    a.box("menu-board", 0.34, 0.04, 0.44, -0.32, 0.44, top + 0.22, DARK, r=0.015)
    menu = Canvas(0.28, 0.36, WHITE, ppm=900)
    menu.text(0.14, 0.05, "MENU", 0.05, INK, anchor="mm")
    for i, (item, price) in enumerate((("コーヒー", "200"), ("ドリンク", "200"), ("やき菓子", "150"))):
        menu.text(0.03, 0.13 + i * 0.075, item, 0.03, INK, weight="Medium")
        menu.text(0.25, 0.13 + i * 0.075, price, 0.03, INK, weight="Medium", anchor="rm")
    a.panel(menu, 0.28, 0.36, -0.32, 0.462, top + 0.22)
    for i, (u, y, h) in enumerate(((-0.02, 0.24, 0.24), (0.10, 0.24, 0.18), (0.04, 0.12, 0.30)), start=1):
        a.cylinder(f"cup-stack-{i}", 0.10, h, u, y, top + h / 2, "#5d6168", r=0.01, segments=20)
    a.box("pot", 0.18, 0.18, 0.15, 0.36, 0.40, top + 0.075, WHITE, r=0.03)
    a.box("plant", 0.26, 0.26, 0.22, 0.36, 0.40, top + 0.26, GREEN, r=0.05, yaw=20)
    a.box("plant-top", 0.17, 0.17, 0.14, 0.38, 0.38, top + 0.43, LEAF, r=0.04, yaw=50)
    a.box("tray-1", 0.34, 0.24, 0.14, 0.68, 0.22, top + 0.07, BLUE, r=0.02)
    a.box("tray-2", 0.30, 0.22, 0.12, 0.94, 0.42, top + 0.06, "#e6e7e9", r=0.02)

    # Shelf behind the counter.
    sy, sd = -0.36, 0.42
    for z, tag in ((0.04, "bottom"), (0.46, "middle"), (0.88, "top")):
        a.box(f"shelf-{tag}", 2.10, sd, 0.04, 0, sy, z, DARK, collide=True, r=0.01)
    for u, tag in ((-1.03, "left"), (0.0, "centre"), (1.03, "right")):
        a.box(f"shelf-side-{tag}", 0.04, sd, 0.86, u, sy, 0.47, DARK, collide=True, r=0.01)
    for i, (u, z, c) in enumerate(((-0.52, 0.64, GREY), (0.52, 0.64, BLUE), (-0.52, 0.22, BLUE), (0.52, 0.22, GREY)), 1):
        a.box(f"bin-{i}", 0.80, 0.34, 0.30, u, sy, z, c, r=0.03)
    for i, (u, c) in enumerate(((-0.60, GREY), (-0.25, DARK), (0.15, BLUE)), start=1):
        a.box(f"stock-{i}", 0.26, 0.26, 0.16, u, sy, 0.98, c, r=0.025)

    # The crate by its side and the board out front.
    a.box("crate", 0.55, 0.50, 0.55, 1.46, -0.12, colour=DARK, collide=True, r=0.03)
    sign_board(a, -1.62, 1.12, "箱庭屋台", ["Hakoniwa", "Stall Asset"])
    return a


# --- 箱庭オープンスペース ------------------------------------------------------------------------

def bush(a: Asset, u, y, z, size, seed):
    rng = random.Random(seed)
    for k in range(4):
        s = size * rng.uniform(0.45, 0.65)
        a.shape(rounded_box(s, s, s * 0.9, s * 0.2), LEAF if k % 2 else GREEN,
                u + rng.uniform(-0.3, 0.3) * size, y + rng.uniform(-0.25, 0.25) * size, z + s * 0.45,
                yaw=rng.uniform(0, 90))


def tree(a: Asset, name, u, y):
    a.cylinder(f"{name}-trunk", 0.18, 1.7, u, y, colour=TRUNK, collide=True, r=0.02, segments=16)
    a.box(f"{name}-crown", 1.4, 1.4, 1.3, u, y, 2.25, GREEN, collide=True, look=False)
    rng = random.Random(name)
    for k, (du, dy, dz, s) in enumerate(((0, 0, 2.2, 0.9), (-0.38, 0.2, 1.95, 0.62), (0.4, -0.15, 2.0, 0.66),
                                        (0.1, 0.38, 2.55, 0.6), (-0.2, -0.32, 2.6, 0.58), (0.05, 0.0, 2.85, 0.5))):
        a.shape(rounded_box(s, s, s, s * 0.18), LEAF if k % 2 else GREEN, u + du, y + dy, dz,
                yaw=rng.uniform(0, 90))


def parasol_table(a: Asset, name, u, y, floor, radius):
    a.cylinder(f"{name}-foot", 0.46, 0.04, u, y, floor + 0.02, DARK, collide=True, r=0.015)
    a.cylinder(f"{name}-post", 0.08, 0.68, u, y, floor + 0.38, DARK, collide=True)
    a.cylinder(f"{name}-top", 0.9, 0.05, u, y, floor + 0.745, WOOD, collide=True, r=0.02, segments=40)
    a.cylinder(f"{name}-pole", 0.05, 1.6, u, y, floor + 1.57, DARK, segments=12)
    a.cylinder(f"{name}-canopy", radius * 2, 0.3, u, y, floor + 2.25, WHITE, collide=True, look=False)
    sectors = 8
    for k in range(sectors):
        mesh = lathe(canopy_profile(radius, 0.32), 48, 360 / sectors * k, 360 / sectors * (k + 1))
        a.shape(mesh, a.accent if k % 2 == 0 else WHITE, u, y, floor + 2.1)
    a.shape(lathe(ellipsoid_profile(0.045, 0.06)), DARK, u, y, floor + 2.46)
    for k in range(4):
        angle = math.radians(45 + 90 * k)
        su, sy = u + 0.72 * math.cos(angle), y + 0.72 * math.sin(angle)
        a.box(f"{name}-stool-{k + 1}", 0.32, 0.32, 0.36, su, sy, floor + 0.18, DARK, collide=True, r=0.04)
        a.box(f"{name}-stool-{k + 1}-seat", 0.36, 0.36, 0.08, su, sy, floor + 0.40, collide=True, r=0.03)


def open_space(variant: str) -> Asset:
    a = Asset("open_space", OPEN_SPACE_ACCENT, variant)
    w, d, floor = 6.0, 5.0, 0.3

    # The deck: a kerb of dark blocks and a floor of light tiles.
    a.box("deck", w, d, floor, colour=DARK, collide=True, r=0.04)
    a.box("floor", w - 0.3, d - 0.3, 0.02, 0, 0, floor + 0.01, "#e9eaec", collide=True)
    tiles = Canvas(w - 0.3, d - 0.3, "#e9eaec", ppm=160)
    rng = random.Random(7)
    step = 0.6
    for i in range(int((w - 0.3) / step) + 1):
        for j in range(int((d - 0.3) / step) + 1):
            if rng.random() < 0.18:
                tiles.rect(i * step, j * step, (i + 1) * step, (j + 1) * step, "#dfe1e4")
    for i in range(int((w - 0.3) / step) + 1):
        tiles.rect(i * step - 0.006, 0, i * step + 0.006, d - 0.3, "#cfd2d6")
    for j in range(int((d - 0.3) / step) + 1):
        tiles.rect(0, j * step - 0.006, w - 0.3, j * step + 0.006, "#cfd2d6")
    a.panel(tiles, w - 0.3, d - 0.3, 0, 0, floor + 0.021, roll=90)
    for su in (-1, 1):
        for sy in (-1, 1):
            a.box(f"corner-{'r' if su > 0 else 'l'}{'f' if sy > 0 else 'b'}", 0.42, 0.42, 0.38,
                  su * (w / 2 - 0.21), sy * (d / 2 - 0.21), colour=DARK, collide=True, r=0.04)
    a.box("front-panel", 3.1, 0.02, 0.23, -0.2, d / 2 + 0.005, 0.15, WHITE, r=0.01)
    words = Canvas(3.0, 0.21, WHITE, ppm=600)
    words.text(0.1, 0.085, "すてきな時間が、ここにある。", 0.1, INK)
    words.rect(0.1, 0.165, 0.38, 0.185, a.accent, radius=0.008)
    words.face(2.84, 0.105, 0.16, INK, "#ffffff")
    a.panel(words, 3.0, 0.21, -0.2, d / 2 + 0.017, 0.15)

    # Parasols with tables and stools.
    parasol_table(a, "table-1", -0.9, 0.35, floor, 1.05)
    parasol_table(a, "table-2", 1.25, -0.45, floor, 0.95)

    # A bench, planters and trees.
    bu, by = -1.4, -1.95
    for k in range(3):
        a.box(f"bench-seat-{k + 1}", 1.5, 0.12, 0.05, bu, by + 0.14 - 0.13 * k, floor + 0.44, WOOD, collide=True, r=0.015)
    for k in range(2):
        a.box(f"bench-back-{k + 1}", 1.5, 0.04, 0.11, bu, by - 0.24, floor + 0.62 + 0.14 * k, WOOD, collide=True, r=0.012)
    for su in (-1, 1):
        a.box(f"bench-leg-{'r' if su > 0 else 'l'}", 0.06, 0.5, 0.42, bu + su * 0.65, by - 0.02, floor + 0.21, DARK,
              collide=True, r=0.015)
        a.box(f"bench-arm-{'r' if su > 0 else 'l'}", 0.06, 0.06, 0.88, bu + su * 0.65, by - 0.24, floor + 0.44, DARK,
              r=0.015)
    planters = [(0.6, -2.0, 1.0), (2.35, 1.55, 0.9), (-2.45, 0.85, 0.9), (2.4, -0.2, 0.9)]
    for i, (u, y, length) in enumerate(planters, start=1):
        if i > 2:
            a.only = "lively"
        alongside = abs(u) > 2.0
        pw, pd = (0.5, length) if alongside else (length, 0.5)
        a.box(f"planter-{i}", pw, pd, 0.45, u, y, floor + 0.225, CONCRETE, collide=True, r=0.04)
        bush(a, u, y, floor + 0.45, 0.55, f"planter-{i}")
        a.only = None
    tree(a, "tree-left", -2.45, -1.95)
    tree(a, "tree-right", 2.45, -1.95)

    # Garden lamps, a bin.
    for su, tag in ((-1, "left"), (1, "right")):
        u, y = su * 2.62, 2.05
        a.box(f"lamp-{tag}", 0.14, 0.14, 0.62, u, y, floor + 0.31, DARK, collide=True, r=0.025)
        a.box(f"lamp-{tag}-light", 0.11, 0.11, 0.16, u, y, floor + 0.70, LIGHT, r=0.02)
        a.box(f"lamp-{tag}-cap", 0.17, 0.17, 0.05, u, y, floor + 0.805, DARK, r=0.015)
    a.box("bin", 0.46, 0.46, 0.8, 1.85, 2.0, floor + 0.4, DARK, collide=True, r=0.05)
    a.box("bin-lid", 0.5, 0.5, 0.05, 1.85, 2.0, floor + 0.825, "#2b2e33", r=0.02)
    label = Canvas(0.26, 0.36, WHITE, ppm=600)
    label.rect(0.08, 0.1, 0.18, 0.24, INK, radius=0.012)
    label.rect(0.065, 0.075, 0.195, 0.095, INK, radius=0.006)
    for x in (0.11, 0.15):
        label.rect(x - 0.005, 0.125, x + 0.005, 0.215, WHITE, radius=0.004)
    a.panel(label, 0.26, 0.36, 1.85, 2.0 + 0.232, floor + 0.46)

    # にぎやか: light poles with a line of flags, a banner, the board.
    a.only = "lively"
    for su, tag in ((-1, "left"), (1, "right")):
        a.box(f"light-pole-{tag}", 0.14, 0.14, 3.3, su * 1.9, -2.25, floor + 1.65, DARK, collide=True, r=0.03)
        a.box(f"light-pole-{tag}-cap", 0.2, 0.2, 0.08, su * 1.9, -2.25, floor + 3.34, DARK, r=0.02)
    span, sag, top = 3.8, 0.35, floor + 3.15
    points = [(-1.9 + span * k / 12, top - sag * (1 - ((k - 6) / 6) ** 2)) for k in range(13)]
    for k, ((u0, z0), (u1, z1)) in enumerate(zip(points, points[1:])):
        length = math.hypot(u1 - u0, z1 - z0)
        a.shape(lathe(cylinder_profile(0.015, length), 8), DARK, (u0 + u1) / 2, -2.25, (z0 + z1) / 2,
                pitch=math.degrees(math.atan2(-(u1 - u0), z1 - z0)))
        # A bulb at every joint of the line (string lights, glowing at night).
        a.shape(lathe(ellipsoid_profile(0.045, 0.06), 12), BULB, u1, -2.25, z1 - 0.05)
        if k == 0:
            continue
        colour = a.accent if k % 2 else WHITE
        a.box(f"flag-{k}", 0.16, 0.02, 0.2, u0, -2.25, z0 - 0.12, colour, r=0.01, solid=False)
        if k % 2:
            a.panel(house_canvas(0.12, a.accent, "#ffffff", a.accent), 0.11, 0.11, u0, -2.25 + 0.012, z0 - 0.12)
    a.box("banner-arm", 0.5, 0.04, 0.04, 1.9 - 0.25, -2.25, floor + 2.6, DARK, r=0.01)
    a.box("banner", 0.42, 0.02, 0.6, 1.9 - 0.3, -2.25, floor + 2.27, r=0.01)
    a.panel(house_canvas(0.4, a.accent, "#ffffff", a.accent), 0.36, 0.36, 1.9 - 0.3, -2.25 + 0.012, floor + 2.27)
    sign_board(a, -2.3, 1.6, "箱庭広場", ["Hakoniwa", "Open Space"], prefix="sign")
    a.only = None
    return a


# --- Writing ---------------------------------------------------------------------------------

STALL_HEADER = '''# 箱庭屋台 (Hakoniwa Stall): a small street stall to line up for a market or
# a festival street. Facing +y: the counter, the noren (three cloths under the
# roof) and the lanterns are on the customers' side. Written by
# catalogs/starter/build_town_assets.py with its looks (the GLB that the
# Catalog items name as their visual: rounded edges and the lettering); edit
# that script, not this file.
#
# Only the frame, the roof, the counter, the shelf, the crate and the board
# collide; the goods, the cloths and the lanterns are looks only.
schema: hakoniwa.environment-types/v1
types:
  - id: food_stall
    extends: object
    label: 屋台
    description: A small street stall (2.4 m wide, 2.7 m high) with a counter, noren and lanterns, facing its y.
    id_prefix: stall
    params:
      color: {kind: color, label: 差し色, default: "#f26b1d", level: item,
              description: "The roof ends, the noren and the marks (its look has the same colour)."}
      visual: {kind: text, label: 見た目（GLB）, default: "", level: item,
               description: "Its look with rounded edges and lettering, in the part's frame; relative to the Catalog."}
    shapes:
'''

OPEN_SPACE_HEADER = '''# 箱庭オープンスペース (Hakoniwa Open Space): a small square on a deck with
# parasols, tables and stools, a bench, planters and trees; にぎやか adds
# light poles with a line of flags, a banner and a board. Facing +y (the
# lettering on the kerb). Written by catalogs/starter/build_town_assets.py
# with its looks (the GLB that the Catalog items name as their visual); edit
# that script, not this file.
schema: hakoniwa.environment-types/v1
types:
  - id: open_space
    extends: object
    label: 広場
    description: A small square (6 m x 5 m) on a low deck with parasols, a bench, planters and trees, facing its y.
    id_prefix: square
    params:
      color: {kind: color, label: 差し色, default: "#f26b1d", level: item,
              description: "The parasols, the stools and the flags (its look has the same colour)."}
      layout: {kind: enum, label: 並べ方, values: [simple, lively], default: simple, level: item,
               description: "simple: two parasols, a bench, planters and trees. lively: also light poles with flags, a banner and a board."}
      visual: {kind: text, label: 見た目（GLB）, default: "", level: item,
               description: "Its look with rounded edges and lettering, in the part's frame; relative to the Catalog."}
    shapes:
'''


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    stalls = {variant: stall(colour, variant) for variant, colour in STALL_COLOURS.items()}
    (ROOT / "types/food-stall.yaml").write_text(STALL_HEADER + stalls["orange"].yaml_shapes() + "\n", encoding="utf-8")
    for variant, asset in stalls.items():
        (ASSETS / f"hakoniwa-stall-{variant}.glb").write_bytes(asset.glb())
    # The type carries every layout's solids (the lively ones under `when`).
    lively = open_space("lively")
    (ROOT / "types/open-space.yaml").write_text(OPEN_SPACE_HEADER + lively.yaml_shapes() + "\n", encoding="utf-8")
    for variant in ("simple", "lively"):
        (ASSETS / f"hakoniwa-open-space-{variant}.glb").write_bytes(open_space(variant).glb())
    for path in sorted(ASSETS.glob("*.glb")):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
