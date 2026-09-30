#!/usr/bin/env python3
"""Export an Environment Recipe as a hakoniwa-urban-mobility World (#8).

Urban Mobility takes a World as a City World job (its
schemas/city-world-job.yaml; docs/asset-contract.md 6.3), registered with
`tools/urban_assets.py register-city`. This writes a saved Recipe in that
layout:

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

    env_urban.py export work/recipes/course.yaml [--out DIR] [--register]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
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
from env_diagnostics import DiagnosticError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "work/urban"
MJCF_FRAME = "X=North,Y=-East,Z=Up"
GLB_FRAME = "X=East,Y=Up,Z=-North"
# The Studio's frame (x east, y north) inside Urban's (x north, y west): -90 degrees about z.
STUDIO_FRAME_QUAT = (math.sqrt(0.5), 0.0, 0.0, -math.sqrt(0.5))
# A flat ground as an hfield (as Urban's plain Worlds): MuJoCo needs a positive height range.
FLAT_HEIGHT_M = 0.001
FLAT_BASE_M = 0.1


class ExportError(RuntimeError):
    pass


def urban_root() -> Path:
    """hakoniwa-urban-mobility: $HAKONIWA_URBAN_MOBILITY_ROOT, else next to this repository."""
    configured = os.environ.get("HAKONIWA_URBAN_MOBILITY_ROOT")
    return Path(configured).expanduser().resolve() if configured else (ROOT.parent / "hakoniwa-urban-mobility").resolve()


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


def export(recipe_path: Path, job: Path | None = None) -> dict:
    """Write a saved Recipe as an Urban City World job; returns what was written."""
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
        if job.exists():
            shutil.rmtree(job)
        staging.replace(job)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    world_dir, terrain_dir = job / "build/world", job / "build/components/terrain"
    mjcf, glb, hfield = world_dir / "city-world.xml", world_dir / "city-world.glb", terrain_dir / "terrain.hf"
    north_south, east_west = ground["half"]
    (terrain_dir / "terrain-receipt.json").write_text(json.dumps({
        "schema_version": 1, "half_extent_m": {"north_south": north_south, "east_west": east_west},
        "coordinate_system": MJCF_FRAME, "nrow": ground["nrow"], "ncol": ground["ncol"],
        "minimum_altitude_m": ground["low"], "maximum_altitude_m": ground["high"],
        "altitude_offset_m": ground["offset"], "hfield": {"path": str(hfield), "sha256": _sha256(hfield)},
        "mjcf": str(terrain_dir / "terrain.xml"),
    }, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(glb, job / "viewer/city-world.glb")
    env_colliders.collider_glb(mjcf, job / "viewer/city-world-colliders.glb",
                               job / "viewer/city-world-colliders-receipt.json")
    origin = (recipe.geo or {}).get("origin")
    receipt = {
        "schema_version": 1,
        "kind": "city" if origin else "plain",
        "world_frame": str(world_dir),
        "coordinate_frame": {
            "schema_version": 1,
            "origin": {"latitude": origin["lat_deg"] if origin else 0.0, "longitude": origin["lon_deg"] if origin else 0.0,
                       "altitude_offset_m": ground["offset"]},
            "half_extent_m": {"north_south": north_south, "east_west": east_west},
            "coordinate_systems": {"mjcf": MJCF_FRAME, "glb": GLB_FRAME},
            "altitude_reference": ("the Envsim terrain's lowest altitude" if recipe.terrain.hfield
                                   else "the environment's ground datum at z = 0"),
        },
        "mjcf": {"path": str(mjcf), "sha256": _sha256(mjcf)},
        "glb": {"path": str(glb), "bytes": glb.stat().st_size, "sha256": _sha256(glb)},
        "components": {"terrain_xml": str(terrain_dir / "terrain.xml"), "extra_mjcf": []},
        "producer": {"tool": "hakoniwa-environment-studio", "version": env_version.version()
                     if hasattr(env_version, "version") else None,
                     "recipe": str(recipe_path), "name": recipe.name,
                     "fingerprint": env_generate.fingerprint(recipe)},
    }
    receipt_path = world_dir / "city-world-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"job": str(job), "receipt": str(receipt_path), "kind": receipt["kind"],
            "fingerprint": receipt["producer"]["fingerprint"], "check": check(receipt_path)}


def check(receipt: Path) -> dict | None:
    """Urban's own check of the job (tools/city_world_job.py), when Urban is next to the Studio."""
    tools = urban_root() / "tools"
    if not (tools / "city_world_job.py").is_file():
        return None
    sys.path.insert(0, str(tools))
    try:
        import city_world_job
    finally:
        sys.path.remove(str(tools))
    _job, problems = city_world_job.check(receipt, workspace=urban_root().parent)
    return {"ok": not any(problem.severity == "error" for problem in problems),
            "problems": [problem.as_json() for problem in problems]}


def register(receipt: Path, precompile: bool = True) -> dict:
    """Register the job as an Urban City Asset (urban_assets.py register-city)."""
    urban = urban_root()
    command = [sys.executable, str(urban / "tools/urban_assets.py"), "register-city", "--receipt", str(receipt),
               *([] if precompile else ["--no-precompile"])]
    done = subprocess.run(command, cwd=urban, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return {"ok": done.returncode == 0, "output": (done.stdout + done.stderr).strip().splitlines()[-20:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="write a Recipe as an Urban City World job")
    command.add_argument("recipe", type=Path)
    command.add_argument("--out", type=Path, help=f"the job folder (default {EXPORTS}/<recipe id>)")
    command.add_argument("--register", action="store_true", help="also register it with urban_assets.py register-city")
    command.add_argument("--no-precompile", action="store_true", help="with --register: skip the height model")
    args = parser.parse_args(argv)
    try:
        result = export(args.recipe, args.out)
    except (ExportError, OSError, subprocess.CalledProcessError) as exc:
        print(f"NG  {exc}", file=sys.stderr)
        return 1
    if args.register:
        result["register"] = register(Path(result["receipt"]), precompile=not args.no_precompile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = (result["check"] is None or result["check"]["ok"]) and result.get("register", {"ok": True})["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
