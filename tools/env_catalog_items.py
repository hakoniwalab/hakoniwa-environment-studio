#!/usr/bin/env python3
"""A building of a city, registered as a Catalog item ("マイカタログ").

A building taken in from a City World is a building-footprint object whose
look (a GLB, CityGML LOD2 with textures) and colliders (Envsim's MJCF, P0-P3)
were made where it stood: the GLB in the frame of its anchor (the place it
was made for, at Envsim's height there), the MJCF in Envsim's frame of the
whole city. Registering it copies both beside the user's Catalog, carried
into the building's own frame -- its origin at the object's origin, its
lowest point at height 0 -- so the item stands on any ground, anywhere,
as many times as it is placed. Its outline, heights and provenance (the
source building, the data's attribution and licence) go into the item.

The user's Catalog is <ws>/catalogs/my/catalog.yaml. It includes the starter
Catalog, so a Recipe made with the starter Catalog can switch to it and keep
every part.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import struct
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import env_rules  # noqa: E402
import env_schema  # noqa: E402
import env_workspace  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
USER_CATALOGS = env_workspace.recipe_workspace() / "catalogs"
MY_CATALOG_ID = "my"
STARTER = ROOT / "catalogs" / "starter" / "catalog.yaml"
CATEGORY = "登録した建物"


class RegisterError(RuntimeError):
    pass


def catalog_path(catalog_id: str = MY_CATALOG_ID) -> Path:
    return USER_CATALOGS / catalog_id / "catalog.yaml"


def _new_catalog(path: Path) -> dict:
    import os

    return {
        "schema": env_schema.CATALOG_SCHEMA,
        "catalog": {
            "id": MY_CATALOG_ID, "name": "マイカタログ",
            "description": "The starter parts and the buildings registered from cities (each with its source).",
            "includes": [Path(os.path.relpath(STARTER.resolve(), path.parent.resolve())).as_posix()],
        },
        "items": [],
    }


# --- The look: a GLB moved so its lowest point is at height 0 -----------------------------

def _matrix(node: dict) -> list[list[float]]:
    if "matrix" in node:
        m = node["matrix"]
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    qx, qy, qz, qw = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    rot = [[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
           [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
           [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]]
    scale = (sx, sy, sz)
    return [[rot[r][0] * scale[0], rot[r][1] * scale[1], rot[r][2] * scale[2], (tx, ty, tz)[r]] for r in range(3)] \
        + [[0.0, 0.0, 0.0, 1.0]]


def _multiply(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def glb_lowest_y(document: dict) -> float:
    """The lowest glTF y (up) of a GLB's meshes, from their POSITION bounds and
    the node transforms."""
    lowest = math.inf
    scene = document.get("scenes", [{}])[document.get("scene", 0)]
    nodes = document.get("nodes", [])

    def visit(index: int, parent) -> None:
        nonlocal lowest
        node = nodes[index]
        matrix = _multiply(parent, _matrix(node))
        if "mesh" in node:
            for primitive in document["meshes"][node["mesh"]]["primitives"]:
                accessor = document["accessors"][primitive["attributes"]["POSITION"]]
                low, high = accessor.get("min"), accessor.get("max")
                if low is None or high is None:
                    continue
                for corner in ((x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])):
                    y = sum(matrix[1][k] * corner[k] for k in range(3)) + matrix[1][3]
                    lowest = min(lowest, y)
        for child in node.get("children", []):
            visit(child, matrix)

    identity = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    for root in scene.get("nodes", []):
        visit(root, identity)
    if not math.isfinite(lowest):
        raise RegisterError("the look (GLB) has no mesh with bounds")
    return lowest


def shifted_glb(data: bytes, lift: float) -> bytes:
    """The GLB with its scene under one node raised by `lift` metres (glTF y)."""
    import env_generate

    document, binary = env_generate.read_glb(data)
    scene = document.setdefault("scenes", [{}])[document.get("scene", 0)]
    nodes = document.setdefault("nodes", [])
    nodes.append({"name": "catalog-frame", "translation": [0.0, lift, 0.0], "children": list(scene.get("nodes", []))})
    scene["nodes"] = [len(nodes) - 1]
    text = json.dumps(document, separators=(",", ":")).encode("utf-8")
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = [struct.pack("<II", len(text), 0x4E4F534A), text]
    if binary:
        chunks += [struct.pack("<II", len(binary), 0x004E4942), binary]
    body = b"".join(chunks)
    return struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


# --- The colliders: Envsim's MJCF in the building's own frame ---------------------------

def _mjcf_lowest_z(root: ET.Element) -> float | None:
    """The lowest Envsim z of an MJCF's mesh vertices and of its geoms' centres
    less their half heights (boxes), or None."""
    values = []
    for mesh in root.iter("mesh"):
        numbers = [float(value) for value in (mesh.get("vertex") or "").split()]
        values += numbers[2::3]
    for geom in root.iter("geom"):
        if geom.get("pos") and geom.get("type") == "box" and geom.get("size"):
            z = float(geom.get("pos").split()[2])
            values.append(z - float(geom.get("size").split()[2]))
    return min(values) if values else None


def framed_mjcf(text: bytes, anchor: dict, lowest_env_z: float, name: str) -> str:
    """The colliders as a fragment in the building's own frame, in Envsim's
    axes: its worldbody under one body moved from the anchor to the origin
    and down to the building's lowest point."""
    root = ET.fromstring(text)
    worldbody = root.find("worldbody")
    if worldbody is None:
        raise RegisterError("the colliders (MJCF) have no worldbody")
    # The environment's offset (east, north, up) in Envsim's axes: x = north, y = -east.
    east, north, up = -anchor["x_m"], -anchor["y_m"], -(anchor["z_m"] + lowest_env_z)
    frame = ET.Element("body", {"name": "catalog-frame", "pos": f"{north!r} {-east!r} {up!r}"})
    for child in list(worldbody):
        worldbody.remove(child)
        frame.append(child)
    worldbody.append(frame)
    root.set("model", name)
    return ET.tostring(root, encoding="unicode")


# --- Registering -------------------------------------------------------------------

def item_id_for(obj: env_schema.EnvObject) -> str:
    """bldg-<the first part of its source id>, in the Catalog's id characters."""
    source = (obj.source or {}).get("id") or obj.id
    stem = re.sub(r"[^a-z0-9]+", "-", str(source).lower()).strip("-")
    stem = re.sub(r"^bldg-?", "", stem)[:24].strip("-") or "building"
    return f"bldg-{stem}"


def register(recipe: env_schema.Recipe, object_id: str, name: str, item_id: str | None = None,
             catalog: Path | None = None) -> dict:
    """Add the object (a building) to the user's Catalog; returns the item."""
    path = catalog or catalog_path()
    obj = next((item for item in recipe.objects if item.id == object_id), None)
    if obj is None:
        raise RegisterError(f"{object_id!r} is not an object of the environment")
    if obj.type != "building_footprint":
        raise RegisterError(f"{object_id} は建物（building-footprint）ではありません")
    anchor = obj.anchor
    if anchor is not None and abs(anchor.get("yaw_deg", 0.0)) > 1e-9:
        raise RegisterError(f"{object_id} は取り込んだときに回してあるので、まだ登録できません")
    if (obj.visual is not None or obj.collision is not None) and anchor is None:
        raise RegisterError(f"{object_id} の見た目・当たり判定がどこで作られたものか（anchor）が分かりません")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else _new_catalog(path)
    items = data.setdefault("items", [])
    item_id = item_id or item_id_for(obj)
    if not env_rules.ID_PATTERN.match(item_id):
        raise RegisterError(f"{item_id!r} is not an id (lower case letters, digits, - and _)")
    if any(entry.get("id") == item_id for entry in items) or item_id in _included_ids(path, data):
        raise RegisterError(f"マイカタログにはもう {item_id} があります")
    assets = path.parent / "assets"
    params = {key: obj.params[key] for key in ("footprint", "holes", "height_m", "min_height_m", "color")
              if key in obj.params}
    lift = 0.0
    if obj.visual is not None:
        data_glb = obj.visual.read_bytes()
        import env_generate

        lowest = glb_lowest_y(env_generate.read_glb(data_glb)[0])
        lift = -lowest
        assets.mkdir(parents=True, exist_ok=True)
        (assets / f"{item_id}.glb").write_bytes(shifted_glb(data_glb, lift))
        params["visual"] = f"assets/{item_id}.glb"
    if obj.collision is not None:
        text = obj.collision.read_bytes()
        # The same lift as the look; without a look, the colliders' own lowest point.
        lowest_env_z = -lift if obj.visual is not None else (
            (_mjcf_lowest_z(ET.fromstring(text)) or anchor["z_m"]) - anchor["z_m"])
        assets.mkdir(parents=True, exist_ok=True)
        (assets / f"{item_id}.xml").write_text(framed_mjcf(text, anchor, lowest_env_z, item_id), encoding="utf-8")
        params["collision"] = f"assets/{item_id}.xml"
    geo = recipe.geo or {}
    source = {key: value for key, value in (obj.source or {}).items() if key != "tags"}
    source.update({key: geo[key] for key in ("attribution", "license", "provider") if geo.get(key) and key not in source})
    source["recipe"] = recipe.path.stem
    item = {"id": item_id, "type": "building_footprint", "name": name or item_id, "category": CATEGORY,
            "params": params, "source": source}
    items.append(item)
    env_schema.save_yaml(data, path)
    env_schema.load_catalog(path)  # it parses (assets readable, the outline valid)
    return {"catalog": MY_CATALOG_ID, "path": str(path), "item": item}


def _included_ids(path: Path, data: dict) -> set[str]:
    ids = set()
    for entry in (data.get("catalog") or {}).get("includes", []):
        try:
            ids |= set(env_schema.load_catalog((path.parent / entry).resolve()).items)
        except Exception:  # noqa: BLE001 - an unreadable include is reported when the Catalog is parsed
            continue
    return ids
