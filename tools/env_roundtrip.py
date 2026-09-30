#!/usr/bin/env python3
"""Check that a City World imported from hakoniwa-envsim comes back as Envsim
made it: generate the Recipe's MuJoCo world and GLB and compare them with the
Envsim build's own (world/city-world.xml and world/city-world.glb).

MuJoCo world (both compiled by MuJoCo; Envsim's frame x north, y west turned
into the Studio's x east, y north):

* every Envsim geom has a Studio geom of the same name (the Studio prefixes it
  with its object's id) with the same type, size, contact bits and friction,
  the same place and orientation in the world (within TOLERANCE_M), and for a
  mesh the same vertices; the terrain's hfield has the same size and data;
* colliders the Studio added that Envsim does not have are listed.

GLB: the vertices of each layer (terrain, roads, other layers, buildings) in
the world: each Envsim vertex has a Studio vertex within TOLERANCE_GLB_M and
back.

    env_roundtrip.py --envsim-build BUILD --recipe sapporo.yaml

Without --recipe the build is imported into a temporary directory first. The
exit status is 0 only when everything matches.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_citygml  # noqa: E402
import env_generate  # noqa: E402
import env_schema  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

TOLERANCE_M = 1e-6
TOLERANCE_GLB_M = 0.001
ENVSIM_GROUND = "plateau_ground"


def _to_studio(vector):
    """Envsim's MuJoCo frame (x north, y west) to the Studio's (x east, y north)."""
    x, y, z = vector
    return (-y, x, z)


def _compiled(xml: str | None = None, path: Path | None = None):
    import mujoco

    model = mujoco.MjModel.from_xml_path(str(path)) if path else mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return model, data


def compare_mjcf(envsim_xml: Path, studio_xml: str) -> dict:
    import mujoco
    import numpy as np

    em, ed = _compiled(path=envsim_xml)
    sm, sd = _compiled(xml=studio_xml)
    name = lambda model, index: mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, index) or ""  # noqa: E731
    studio = {}
    for index in range(sm.ngeom):
        geom = name(sm, index)
        studio[geom.split("/", 1)[1] if "/" in geom else geom] = index
    turn = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])  # _to_studio as a matrix
    report = {"envsim_geoms": em.ngeom, "matched": 0, "missing": [], "different": [], "added_colliders": [],
              "max_position_error_m": 0.0, "max_orientation_error": 0.0}
    matched = set()
    for index in range(em.ngeom):
        geom = name(em, index)
        other = studio.get(env_generate.TERRAIN_GEOM if geom == ENVSIM_GROUND else geom)
        if other is None:
            report["missing"].append(geom)
            continue
        matched.add(other)
        problems = []
        if em.geom_type[index] != sm.geom_type[other]:
            problems.append("type")
        for field in ("geom_contype", "geom_conaffinity"):
            if getattr(em, field)[index] != getattr(sm, field)[other]:
                problems.append(field[5:])
        if not np.allclose(em.geom_friction[index], sm.geom_friction[other], atol=1e-9):
            problems.append("friction")
        if em.geom_type[index] != mujoco.mjtGeom.mjGEOM_MESH and not np.allclose(
                em.geom_size[index], sm.geom_size[other], atol=1e-9):
            problems.append("size")
        position = float(np.max(np.abs(turn @ ed.geom_xpos[index] - sd.geom_xpos[other])))
        orientation = float(np.max(np.abs(turn @ ed.geom_xmat[index].reshape(3, 3) - sd.geom_xmat[other].reshape(3, 3))))
        report["max_position_error_m"] = max(report["max_position_error_m"], position)
        report["max_orientation_error"] = max(report["max_orientation_error"], orientation)
        if position > TOLERANCE_M:
            problems.append(f"position {position:.3g} m")
        if orientation > TOLERANCE_M:
            problems.append(f"orientation {orientation:.3g}")
        if em.geom_type[index] == mujoco.mjtGeom.mjGEOM_MESH:
            a, b = em.geom_dataid[index], sm.geom_dataid[other]
            va = em.mesh_vert[em.mesh_vertadr[a]:em.mesh_vertadr[a] + em.mesh_vertnum[a]]
            vb = sm.mesh_vert[sm.mesh_vertadr[b]:sm.mesh_vertadr[b] + sm.mesh_vertnum[b]]
            if va.shape != vb.shape or not np.allclose(va, vb, atol=TOLERANCE_M):
                problems.append("mesh vertices")
        if em.geom_type[index] == mujoco.mjtGeom.mjGEOM_HFIELD:
            a, b = em.geom_dataid[index], sm.geom_dataid[other]
            if (em.hfield_nrow[a], em.hfield_ncol[a]) != (sm.hfield_nrow[b], sm.hfield_ncol[b]) or not np.allclose(
                    em.hfield_size[a], sm.hfield_size[b], atol=1e-12):
                problems.append("hfield size")
            else:
                count = em.hfield_nrow[a] * em.hfield_ncol[a]
                da = em.hfield_data[em.hfield_adr[a]:em.hfield_adr[a] + count]
                db = sm.hfield_data[sm.hfield_adr[b]:sm.hfield_adr[b] + count]
                if not np.array_equal(da, db):
                    problems.append("hfield data")
        if problems:
            report["different"].append({"geom": geom, "problems": problems})
        else:
            report["matched"] += 1
    for index in range(sm.ngeom):
        if index not in matched and (sm.geom_contype[index] or sm.geom_conaffinity[index]):
            report["added_colliders"].append(name(sm, index))
    return report


# --- GLB ------------------------------------------------------------------------------

def _quat_rotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def glb_vertices(data: bytes, group) -> dict[str, list[tuple[float, float, float]]]:
    """World positions of a GLB's vertices by group(node) (root nodes only
    carry transforms here: Envsim's have none, the Studio's are flat)."""
    import numpy as np

    document, binary = env_generate.read_glb(data)
    views = document.get("bufferViews", [])
    groups: dict[str, list] = {}

    def positions(index):
        accessor = document["accessors"][index]
        view = views[accessor["bufferView"]]
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        stride = view.get("byteStride") or 12
        raw = np.frombuffer(binary, dtype=np.uint8, count=stride * accessor["count"], offset=start)
        return np.ndarray((accessor["count"], 3), dtype="<f4", buffer=raw.tobytes(), strides=(stride, 4))

    def walk(index, parent):
        node = document["nodes"][index]
        translation = node.get("translation", [0.0, 0.0, 0.0])
        rotation = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
        key = group(node) or parent
        if "mesh" in node and key:
            bucket = groups.setdefault(key, [])
            for primitive in document["meshes"][node["mesh"]]["primitives"]:
                for point in positions(primitive["attributes"]["POSITION"]):
                    x, y, z = _quat_rotate(rotation, [float(value) for value in point])
                    bucket.append((x + translation[0], y + translation[1], z + translation[2]))
        for child in node.get("children", []):
            walk(child, key)

    for root in document["scenes"][document.get("scene", 0)]["nodes"]:
        walk(root, None)
    return groups


def _coverage(points, others, tolerance: float) -> tuple[float, float]:
    """(share of points with an other point within tolerance, the largest
    distance to the nearest found for the rest, capped at 10 tolerances)."""
    cell = tolerance
    grid = {}
    for x, y, z in others:
        grid.setdefault((math.floor(x / cell), math.floor(y / cell), math.floor(z / cell)), []).append((x, y, z))
    covered, worst = 0, 0.0
    for x, y, z in points:
        ix, iy, iz = math.floor(x / cell), math.floor(y / cell), math.floor(z / cell)
        best = math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for other in grid.get((ix + dx, iy + dy, iz + dz), ()):
                        best = min(best, math.dist((x, y, z), other))
        if best <= tolerance:
            covered += 1
        else:
            worst = max(worst, min(best, 10 * tolerance))
    return (covered / len(points) if points else 1.0), worst


def compare_glb(envsim_glb: Path, studio_glb: bytes, recipe: env_schema.Recipe) -> dict:
    layers = {obj.id: obj.source.get("id") for obj in recipe.objects
              if obj.type == "city_layer" and obj.source and obj.source.get("id")}

    def envsim_group(node):  # component-<n>-<layer>-<mesh>
        name = node.get("name", "")
        if not name.startswith("component-"):
            return None
        layer = name.split("-", 2)[2]
        for known in ("terrain", "roads", "buildings", "road-markings", "bridges"):
            if layer.startswith(known + "-"):
                return known
        return layer.rsplit("-", 1)[0]

    def studio_group(node):
        extras = node.get("extras") or {}
        if node.get("name") == env_generate.TERRAIN_GEOM:
            return "terrain"
        if extras.get("type") == "city_layer":
            return layers.get(extras.get("object"), extras.get("object"))
        if extras.get("type") == "building_footprint":
            return "buildings"
        return "other"

    ours = glb_vertices(studio_glb, studio_group)
    theirs = glb_vertices(envsim_glb.read_bytes(), envsim_group)
    report = {}
    for group in sorted(set(theirs) | set(ours)):
        a, b = theirs.get(group, []), ours.get(group, [])
        forward, forward_worst = _coverage(a, b, TOLERANCE_GLB_M)
        backward, backward_worst = _coverage(b, a, TOLERANCE_GLB_M)
        report[group] = {"envsim_vertices": len(a), "studio_vertices": len(b),
                         "envsim_covered": round(forward, 6), "studio_covered": round(backward, 6),
                         "worst_m": round(max(forward_worst, backward_worst), 6)}
    return report


def check(build: Path, recipe_path: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        if recipe_path is None:
            recipe_path = Path(directory) / "roundtrip.yaml"
            recipe_data, _report = env_citygml.convert_build(
                build, recipe_path=recipe_path,
                catalog=str(Path(env_citygml.DEFAULT_CATALOG).resolve()))
            env_citygml.write_recipe(recipe_data, recipe_path)
        recipe = env_schema.load_recipe(recipe_path)
        mjcf = compare_mjcf(build / "world" / "city-world.xml", env_generate.environment_mjcf(recipe))
        glb = compare_glb(build / "world" / "city-world.glb", env_generate.environment_glb(recipe), recipe)
    ok = (not mjcf["missing"] and not mjcf["different"] and not mjcf["added_colliders"]
          and all(entry["envsim_covered"] == 1.0 and entry["studio_covered"] == 1.0
                  for group, entry in glb.items() if group != "other"))
    return {"ok": ok, "build": str(build), "recipe": str(recipe_path), "mjcf": mjcf, "glb": glb}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--envsim-build", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, help="an imported Recipe (default: import the build now)")
    args = parser.parse_args(argv)
    try:
        result = check(args.envsim_build.resolve(), args.recipe.resolve() if args.recipe else None)
    except DiagnosticError as error:
        print(json.dumps({"ok": False, "diagnostics": [item.as_json() for item in error.diagnostics]},
                         ensure_ascii=False, indent=2))
        return 1
    mjcf = result["mjcf"]
    for key in ("missing", "added_colliders"):
        if len(mjcf[key]) > 20:
            mjcf[key] = mjcf[key][:20] + [f"... {len(mjcf[key]) - 20} more"]
    if len(mjcf["different"]) > 20:
        mjcf["different"] = mjcf["different"][:20] + [{"more": len(mjcf["different"]) - 20}]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
