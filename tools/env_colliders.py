#!/usr/bin/env python3
"""The collider view of a MuJoCo world: every colliding geom as MuJoCo
compiled it, as a GLB in the viewers' frame (X=East, Y=Up, Z=-North).

hakoniwa-urban-mobility's viewers show it as a wireframe over the World
(viewer/city-world-colliders.glb of a City World job). It is made from the
compiled model, so it shows exactly what MuJoCo collides with: meshes as their
convex hulls (MuJoCo's own), boxes, cylinders, spheres, capsules, ellipsoids,
planes and hfields at their world poses. Geoms with contype and conaffinity 0
(visual only) are left out.

    env_colliders.py --in world.xml --out colliders.glb --receipt receipt.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# MJCF (X=North, Y=-East, Z=Up) -> GLB (X=East, Y=Up, Z=-North).
MJCF_TO_GLB = ((0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (-1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
PLANE_HALF_M = 500.0  # an infinite plane (size 0) is drawn this far out
COLOR = (110, 200, 120, 255)


def _hfield_mesh(model, hfield: int):
    import numpy
    import trimesh

    nrow, ncol = int(model.hfield_nrow[hfield]), int(model.hfield_ncol[hfield])
    sx, sy, sz, _base = (float(value) for value in model.hfield_size[hfield])
    data = numpy.asarray(model.hfield_data[model.hfield_adr[hfield]:model.hfield_adr[hfield] + nrow * ncol]).reshape(nrow, ncol)
    xs = numpy.linspace(-sx, sx, ncol)
    ys = numpy.linspace(-sy, sy, nrow)
    grid_x, grid_y = numpy.meshgrid(xs, ys)  # data row r lies at y[r], column c at x[c]
    vertices = numpy.column_stack([grid_x.ravel(), grid_y.ravel(), (data * sz).ravel()])
    faces = []
    for r in range(nrow - 1):
        for c in range(ncol - 1):
            a, b, d, e = r * ncol + c, r * ncol + c + 1, (r + 1) * ncol + c, (r + 1) * ncol + c + 1
            faces += [(a, b, e), (a, e, d)]
    return trimesh.Trimesh(vertices, numpy.array(faces), process=False)


def _geom_mesh(model, geom: int):
    import mujoco
    import numpy
    import trimesh

    kind = int(model.geom_type[geom])
    size = [float(value) for value in model.geom_size[geom]]
    if kind == mujoco.mjtGeom.mjGEOM_MESH:
        mesh = int(model.geom_dataid[geom])
        vertices = model.mesh_vert[model.mesh_vertadr[mesh]:model.mesh_vertadr[mesh] + model.mesh_vertnum[mesh]]
        faces = model.mesh_face[model.mesh_faceadr[mesh]:model.mesh_faceadr[mesh] + model.mesh_facenum[mesh]]
        return trimesh.Trimesh(numpy.array(vertices), numpy.array(faces), process=False)
    if kind == mujoco.mjtGeom.mjGEOM_BOX:
        return trimesh.creation.box(extents=[2 * value for value in size])
    if kind == mujoco.mjtGeom.mjGEOM_CYLINDER:
        return trimesh.creation.cylinder(radius=size[0], height=2 * size[1], sections=32)
    if kind == mujoco.mjtGeom.mjGEOM_SPHERE:
        return trimesh.creation.icosphere(subdivisions=2, radius=size[0])
    if kind == mujoco.mjtGeom.mjGEOM_CAPSULE:
        return trimesh.creation.capsule(height=2 * size[1], radius=size[0], count=[16, 16])
    if kind == mujoco.mjtGeom.mjGEOM_ELLIPSOID:
        sphere = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
        sphere.apply_scale(size)
        return sphere
    if kind == mujoco.mjtGeom.mjGEOM_PLANE:
        half_x, half_y = (value if value > 0 else PLANE_HALF_M for value in size[:2])
        return trimesh.creation.box(extents=[2 * half_x, 2 * half_y, 0.001])
    if kind == mujoco.mjtGeom.mjGEOM_HFIELD:
        return _hfield_mesh(model, int(model.geom_dataid[geom]))
    return None


def collider_glb(source: Path, out: Path, receipt: Path) -> dict:
    """Write the collider view of a MuJoCo world and its receipt; returns the receipt."""
    import mujoco
    import numpy
    import trimesh

    model = mujoco.MjModel.from_xml_path(str(source))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    to_glb = numpy.array(MJCF_TO_GLB)
    scene = trimesh.Scene()
    counts: dict[str, int] = {}
    triangles = 0
    for geom in range(model.ngeom):
        if not (model.geom_contype[geom] or model.geom_conaffinity[geom]):
            continue
        mesh = _geom_mesh(model, geom)
        if mesh is None:
            continue
        transform = numpy.eye(4)
        transform[:3, :3] = data.geom_xmat[geom].reshape(3, 3)
        transform[:3, 3] = data.geom_xpos[geom]
        mesh.apply_transform(to_glb @ transform)
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=numpy.tile(COLOR, (len(mesh.vertices), 1)))
        scene.add_geometry(mesh, node_name=mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, geom) or f"geom-{geom}")
        name = mujoco.mjtGeom(int(model.geom_type[geom])).name.removeprefix("mjGEOM_").lower()
        counts[name] = counts.get(name, 0) + 1
        triangles += len(mesh.faces)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(scene.export(file_type="glb"))
    result = {
        "schema_version": 1,
        "source_mjcf": str(source),
        "coordinate_transform": "MJCF(X=North,Y=-East,Z=Up)->Three.js(X=East,Y=Up,Z=-North)",
        "geom_counts": dict(sorted(counts.items())),
        "triangle_count": triangles,
        "output": {"path": str(out), "bytes": out.stat().st_size, "sha256": hashlib.sha256(out.read_bytes()).hexdigest()},
        "purpose": "debug visualization only; MuJoCo remains the collision authority",
        "made_by": "hakoniwa-environment-studio tools/env_colliders.py (from the compiled MuJoCo model)",
    }
    receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--in", dest="source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    result = collider_glb(args.source.resolve(), args.out.resolve(), args.receipt.resolve())
    print(f"OK: collider view {args.out} ({result['triangle_count']} triangles, {result['geom_counts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
