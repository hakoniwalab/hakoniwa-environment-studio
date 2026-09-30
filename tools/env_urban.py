#!/usr/bin/env python3
"""Export an Environment Recipe or a City World as a City World job (#8).

A City World job is the World format hakoniwa-urban-mobility takes (its
schemas/city-world-job.yaml; docs/asset-contract.md 6.3). The Studio does not
call Urban: it writes jobs to the export folder it is started with
(env_studio.py start --export-dir DIR); Urban Mobility, which starts it with
its own folder, registers what appears there. Each job is written next to
its place and moved in when complete, and carries job.json (title, source).

export writes a saved Recipe in that layout:

  <job>/build/world/city-world-receipt.json   the receipt (frames, origin, extent, files)
  <job>/build/world/city-world.xml            the World MJCF, in its frame X=North, Y=-East, Z=Up
  <job>/build/world/city-world.glb            the Studio's GLB (X=East, Y=Up, Z=-North, as it is)
  <job>/build/components/terrain/             terrain.hf, terrain.xml, terrain-receipt.json
  <job>/viewer/city-world.glb, city-world-colliders.glb (+ receipt; env_colliders.py)

The MJCF is the Studio's generated world with the Studio's own frame turned
into Urban's: the ground is an hfield at the top level (Envsim's own file for
an Envsim terrain, the Studio's grid for hills, a flat 2 x 2 field
otherwise), everything else stands in one body turned -90 degrees about z.
Visual-only geoms are left out (the GLB shows them; Urban's World MJCF is what
collides, as an Envsim one is), and so are lights (the vehicles bring theirs).
Only size, asset and worldbody are written: every rotation is a quaternion, so
no compiler setting of the models it is composed into can change it.

A Recipe with a geographic origin (imported from a map or a City World) is a
City (kind city); one without is a plain World (kind plain, origin 0, 0). The
receipt also names the Recipe and its fingerprint, so a changed environment
is a changed World.

    env_urban.py export course.yaml [--out DIR]

export_city_world writes an Envsim City World build as a small job instead:
its receipt (naming the build's files by absolute path), the building
outlines, and the viewer folder.

From the command line without --out, exports go to urban/<id>/ in the Studio's Recipe workspace
($HAKONIWA_WORK_DIR/recipes/environment-studio, tools/env_workspace.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_colliders  # noqa: E402
import env_generate  # noqa: E402
import env_schema  # noqa: E402
import env_version  # noqa: E402
import env_workspace  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

EXPORTS = env_workspace.recipe_workspace() / "urban"
MJCF_FRAME = "X=North,Y=-East,Z=Up"
GLB_FRAME = "X=East,Y=Up,Z=-North"
# The Studio's frame (x east, y north) inside Urban's (x north, y west): -90 degrees about z.
STUDIO_FRAME_QUAT = (math.sqrt(0.5), 0.0, 0.0, -math.sqrt(0.5))
# A flat ground as an hfield (as Urban's plain Worlds): MuJoCo needs a positive height range.
FLAT_HEIGHT_M = 0.001
FLAT_BASE_M = 0.1


class ExportError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _numbers(values) -> str:
    return " ".join(repr(float(value)) for value in values)


def terrain_samples(terrain) -> dict:
    """The ground as Urban reads it: hfield samples in Envsim's order (data row
    r at y = -east_west + ..., column c at x = -north_south + ..., x north,
    y west), the MuJoCo size, the altitude MJCF z = 0 stands for, and the
    height the hfield geom stands at (so MJCF heights equal the Studio's)."""
    if terrain.hfield:  # Envsim's own file, as it is
        data = Path(terrain.hfield["path"]).read_bytes()
        nrow, ncol = struct.unpack_from("<ii", data, 0)
        values = struct.unpack_from(f"<{nrow * ncol}f", data, 8)
        low = min(values)
        return {"bytes": data, "size": tuple(terrain.hfield["size"]), "offset": low, "pos_z": 0.0,
                "half": (terrain.grid_half_north, terrain.grid_half_east), "nrow": nrow, "ncol": ncol,
                "low": low, "high": max(values)}
    if terrain.kind == "hfield":  # the Studio's grid (rows north to south, columns west to east)
        rows, cols, values = env_generate.env_terrain.envsim_order(terrain)
        low, high = min(values), max(values)
        return {"bytes": struct.pack(f"<ii{rows * cols}f", rows, cols, *values),
                "size": (terrain.grid_half_north, terrain.grid_half_east, max(high - low, FLAT_HEIGHT_M),
                         env_generate.env_terrain.BASE_M),
                "offset": 0.0, "pos_z": low, "half": (terrain.grid_half_north, terrain.grid_half_east),
                "nrow": rows, "ncol": cols, "low": low, "high": high}
    half = (terrain.half_north, terrain.half_east)
    return {"bytes": struct.pack("<ii4f", 2, 2, 0.0, 0.0, 0.0, 0.0), "size": (*half, FLAT_HEIGHT_M, FLAT_BASE_M),
            "offset": 0.0, "pos_z": 0.0, "half": half, "nrow": 2, "ncol": 2, "low": 0.0, "high": 0.0}


def world_mjcf(recipe: env_schema.Recipe, hfield_file: str, ground: dict) -> str:
    """The World MJCF in Urban's frame (see the module doc)."""
    studio = ET.fromstring(env_generate.environment_mjcf(recipe))
    root = ET.Element("mujoco", {"model": f"studio_{recipe.path.stem}".replace("-", "_")})
    asset = ET.SubElement(root, "asset")
    for element in studio.find("asset") if studio.find("asset") is not None else []:
        if element.tag == "hfield" and element.get("name") == "terrain":
            continue  # replaced by the ground below
        asset.append(element)
    ET.SubElement(asset, "hfield", {"name": "terrain", "file": hfield_file, "size": _numbers(ground["size"])})
    world = ET.SubElement(root, "worldbody")
    terrain = recipe.terrain
    ET.SubElement(world, "geom", {
        "name": env_generate.TERRAIN_GEOM, "type": "hfield", "hfield": "terrain", "pos": _numbers((0, 0, ground["pos_z"])),
        "rgba": _numbers((*env_generate.hex_rgb(terrain.color), 1.0)), "friction": _numbers((terrain.friction, 0.005, 0.0001))})
    frame = ET.SubElement(world, "body", {"name": "studio-frame", "quat": _numbers(STUDIO_FRAME_QUAT)})
    for element in studio.find("worldbody"):
        if element.tag == "geom" and element.get("name") == env_generate.TERRAIN_GEOM:
            continue
        frame.append(element)
    # Visual-only geoms (paint, lamps) are the GLB's: the World MJCF holds what
    # collides. Lights are the vehicles' (Urban keeps each vehicle model's
    # lighting, named sun / fill_light, when it composes the World in).
    for parent in list(frame.iter()):
        for child in list(parent):
            if child.tag == "light" or (child.tag == "geom" and child.get("contype") == "0"
                                        and child.get("conaffinity") == "0"):
                parent.remove(child)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


JOB_INFO = "job.json"


def _publish(staging: Path, job: Path) -> None:
    """Move a finished job folder into place in one step, so a reader of the
    export folder never sees a half-written job. The staging folder sits next
    to the job; paths recorded while it was written name the final place."""
    for path in staging.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        if str(staging) in text:
            path.write_text(text.replace(str(staging), str(job)), encoding="utf-8")
    if job.exists():
        shutil.rmtree(job)
    staging.replace(job)


def _write_job_info(staging: Path, title: str, source: dict) -> None:
    (staging / JOB_INFO).write_text(json.dumps({
        "schema_version": 1, "title": title,
        "producer": {"tool": "hakoniwa-environment-studio", "version": env_version.VERSION,
                     "commit": env_version.build_info()["commit"]},
        "source": source,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export(recipe_path: Path, job: Path | None = None) -> dict:
    """Write a saved Recipe as a City World job (the format of
    hakoniwa-urban-mobility's schemas/city-world-job.yaml); returns what was
    written. The job is complete when it appears (written next to it, then
    moved into place)."""
    recipe_path = recipe_path.resolve()
    try:
        recipe = env_schema.load_recipe(recipe_path)
    except DiagnosticError as exc:
        raise ExportError(f"the Recipe is not valid: {exc}") from exc
    job = (job or EXPORTS / recipe_path.stem).resolve()
    staging = job.with_name(job.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    world_dir, terrain_dir = staging / "build/world", staging / "build/components/terrain"
    for folder in (world_dir, terrain_dir, staging / "viewer"):
        folder.mkdir(parents=True)

    def final(path: Path) -> str:
        return str(job / path.relative_to(staging))

    try:
        ground = terrain_samples(recipe.terrain)
        (terrain_dir / "terrain.hf").write_bytes(ground["bytes"])
        (terrain_dir / "terrain.xml").write_text(
            '<mujoco model="studio_terrain">\n'
            f'  <asset>\n    <hfield name="terrain" file="terrain.hf" size="{_numbers(ground["size"])}"/>\n  </asset>\n'
            f'  <worldbody>\n    <geom name="terrain" type="hfield" hfield="terrain" pos="0 0 {ground["pos_z"]!r}"/>\n'
            '  </worldbody>\n</mujoco>\n', encoding="utf-8")
        (world_dir / "city-world.xml").write_text(world_mjcf(recipe, "../components/terrain/terrain.hf", ground),
                                                  encoding="utf-8")
        (world_dir / "city-world.glb").write_bytes(env_generate.environment_glb(recipe))
        mjcf, glb, hfield = world_dir / "city-world.xml", world_dir / "city-world.glb", terrain_dir / "terrain.hf"
        north_south, east_west = ground["half"]
        (terrain_dir / "terrain-receipt.json").write_text(json.dumps({
            "schema_version": 1, "half_extent_m": {"north_south": north_south, "east_west": east_west},
            "coordinate_system": MJCF_FRAME, "nrow": ground["nrow"], "ncol": ground["ncol"],
            "minimum_altitude_m": ground["low"], "maximum_altitude_m": ground["high"],
            "altitude_offset_m": ground["offset"], "hfield": {"path": final(hfield), "sha256": _sha256(hfield)},
            "mjcf": final(terrain_dir / "terrain.xml"),
        }, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(glb, staging / "viewer/city-world.glb")
        env_colliders.collider_glb(mjcf, staging / "viewer/city-world-colliders.glb",
                                   staging / "viewer/city-world-colliders-receipt.json")
        origin = (recipe.geo or {}).get("origin")
        receipt = {
            "schema_version": 1,
            "kind": "city" if origin else "plain",
            "world_frame": final(world_dir),
            "coordinate_frame": {
                "schema_version": 1,
                "origin": {"latitude": origin["lat_deg"] if origin else 0.0,
                           "longitude": origin["lon_deg"] if origin else 0.0,
                           "altitude_offset_m": ground["offset"]},
                "half_extent_m": {"north_south": north_south, "east_west": east_west},
                "coordinate_systems": {"mjcf": MJCF_FRAME, "glb": GLB_FRAME},
                "altitude_reference": ("the Envsim terrain's lowest altitude" if recipe.terrain.hfield
                                       else "the environment's ground datum at z = 0"),
            },
            "mjcf": {"path": final(mjcf), "sha256": _sha256(mjcf)},
            "glb": {"path": final(glb), "bytes": glb.stat().st_size, "sha256": _sha256(glb)},
            "components": {"terrain_xml": final(terrain_dir / "terrain.xml"), "extra_mjcf": []},
            "producer": {"tool": "hakoniwa-environment-studio", "version": env_version.VERSION,
                         "recipe": str(recipe_path), "name": recipe.name,
                         "fingerprint": env_generate.fingerprint(recipe)},
        }
        (world_dir / "city-world-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                                           encoding="utf-8")
        _write_job_info(staging, recipe.name, {"kind": "recipe", "path": str(recipe_path)})
        _publish(staging, job)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"job": str(job), "receipt": str(job / "build/world/city-world-receipt.json"), "kind": receipt["kind"],
            "name": recipe.name, "fingerprint": receipt["producer"]["fingerprint"]}


def city_world_receipt(build: Path) -> Path:
    """The receipt of an Envsim City World build (<job>/build)."""
    return build / "world" / "city-world-receipt.json"


def export_city_world(build: Path, export_dir: Path, title: str | None = None) -> dict:
    """Write an Envsim City World build to the export folder as it is, as a
    small City World job: its receipt (whose absolute paths keep naming the
    build's MJCF and GLB), the building outlines (build/city-world-lod1.json),
    and the viewer folder. The job folder is named after the build's."""
    build = build.resolve()
    receipt = city_world_receipt(build)
    viewer = build.parent / "viewer"
    if not receipt.is_file():
        raise ExportError(f"not a City World build (no {receipt})")
    if not (viewer / "city-world-colliders.glb").is_file():
        raise ExportError(f"the City World build has no collider view ({viewer / 'city-world-colliders.glb'})")
    job = export_dir.resolve() / build.parent.name
    staging = job.with_name(job.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        (staging / "build/world").mkdir(parents=True)
        shutil.copy2(receipt, staging / "build/world/city-world-receipt.json")
        outlines = build / "city-world-lod1.json"
        if outlines.is_file():
            shutil.copy2(outlines, staging / "build/city-world-lod1.json")
        shutil.copytree(viewer, staging / "viewer")
        _write_job_info(staging, title or build.parent.name, {"kind": "city-world", "path": str(build)})
        _publish(staging, job)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"id": job.name, "job": str(job), "receipt": str(job / "build/world/city-world-receipt.json")}


def exported(export_dir: Path) -> dict[str, dict]:
    """The jobs in the export folder by the path they came from (a City World
    build or a Recipe): {source path: {id, title, job}}."""
    found = {}
    if not export_dir.is_dir():
        return found
    for info_path in sorted(export_dir.glob(f"*/{JOB_INFO}")):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            source = str(Path(info["source"]["path"]).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found[source] = {"id": info_path.parent.name, "title": info.get("title") or info_path.parent.name,
                         "job": str(info_path.parent)}
    return found


def remove_export(export_dir: Path, job_id: str) -> None:
    """Delete a job this Studio wrote to the export folder (not the build it came from)."""
    job = export_dir.resolve() / job_id
    if job.parent != export_dir.resolve() or not (job / JOB_INFO).is_file():
        raise ExportError(f"no job {job_id} written by this Studio in {export_dir}")
    shutil.rmtree(job)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="write a Recipe as a City World job")
    command.add_argument("recipe", type=Path)
    command.add_argument("--out", type=Path, help=f"the job folder (default {EXPORTS}/<recipe id>)")
    args = parser.parse_args(argv)
    try:
        result = export(args.recipe, args.out)
    except (ExportError, OSError, subprocess.CalledProcessError) as exc:
        print(f"NG  {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
