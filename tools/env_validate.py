#!/usr/bin/env python3
"""Check an Environment Recipe as a physical world with MuJoCo (#6).

The validation world (env_generate.environment_mjcf(validation=True): objects
as free bodies, fixed boundary boxes just outside the edges) is compiled and
evaluated once with mj_forward, and its contacts are read:

  object - object     overlap        two objects penetrate each other
  object - boundary   outside        an object reaches outside the environment
  object - terrain    below_terrain  an object reaches into the ground
  compile failure     compile_error  MuJoCo cannot load the generated world

A contact up to the tolerance (1 mm) is touching, which is fine. Problems come
back as diagnostics (env_diagnostics.py): path objects[i], the depth in metres
as `actual`, and for an overlap the other object in `related`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_generate  # noqa: E402
import env_schema  # noqa: E402
from env_diagnostics import Collector, DiagnosticError  # noqa: E402

TOLERANCE_M = 0.001
# Floating-point slack, so a contact exactly at the tolerance counts as within it.
EPSILON_M = 1e-9


def _mujoco():
    import mujoco  # imported here so the schema tools work without MuJoCo

    return mujoco


def available() -> bool:
    try:
        _mujoco()
    except ModuleNotFoundError:
        return False
    return True


def check(recipe: env_schema.Recipe, tolerance_m: float = TOLERANCE_M) -> list:
    """The physical problems of a resolved Recipe, as diagnostics."""
    mujoco = _mujoco()
    problems = Collector()
    try:
        model = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(recipe, validation=True))
    except ValueError as exc:
        problems.add("", "compile_error", f"MuJoCo cannot load the generated world: {exc}", actual=str(exc))
        return problems.items
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    index = {obj.id: position for position, obj in enumerate(recipe.objects)}
    names = [mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, geom) or "" for geom in range(model.ngeom)]
    overlaps: dict[tuple[str, str], float] = {}
    outside: dict[tuple[str, str], float] = {}
    sunk: dict[str, float] = {}
    for number in range(data.ncon):
        contact = data.contact[number]
        depth = -contact.dist
        if depth <= tolerance_m + EPSILON_M:
            continue
        first, second = names[contact.geom1], names[contact.geom2]
        objects = [env_generate.geom_object(name) for name in (first, second) if name.startswith(env_generate.GEOM_PREFIX)]
        others = [name for name in (first, second) if not name.startswith(env_generate.GEOM_PREFIX)]
        if len(objects) == 2:
            key = tuple(sorted(objects, key=index.get))
            overlaps[key] = max(overlaps.get(key, 0.0), depth)
        elif len(objects) == 1 and others[0].startswith(env_generate.BOUNDARY_PREFIX):
            key = (objects[0], others[0].removeprefix(env_generate.BOUNDARY_PREFIX))
            outside[key] = max(outside.get(key, 0.0), depth)
        elif len(objects) == 1 and others[0] == env_generate.TERRAIN_GEOM:
            sunk[objects[0]] = max(sunk.get(objects[0], 0.0), depth)
    expected = f"<= {tolerance_m} m"
    for (a, b), depth in sorted(overlaps.items(), key=lambda entry: (index[entry[0][0]], index[entry[0][1]])):
        problems.add(f"objects[{index[a]}]", "overlap", f"{a} and {b} penetrate each other by {depth * 1000:.0f} mm",
                     expected=expected, actual=round(depth, 4), related=(f"objects[{index[b]}]",))
    for (obj, edge), depth in sorted(outside.items(), key=lambda entry: index[entry[0][0]]):
        problems.add(f"objects[{index[obj]}]", "outside",
                     f"{obj} reaches {depth * 1000:.0f} mm outside the {edge} edge", expected=expected,
                     actual={"edge": edge, "depth_m": round(depth, 4)})
    for obj, depth in sorted(sunk.items(), key=lambda entry: index[entry[0]]):
        problems.add(f"objects[{index[obj]}]", "below_terrain", f"{obj} reaches {depth * 1000:.0f} mm into the ground",
                     expected=expected, actual=round(depth, 4))
    return problems.items


def validate(recipe: env_schema.Recipe, tolerance_m: float = TOLERANCE_M) -> dict:
    diagnostics = [item.as_json() for item in check(recipe, tolerance_m)]
    return {"ok": not any(item["severity"] == "error" for item in diagnostics), "tolerance_m": tolerance_m,
            "diagnostics": diagnostics}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate(env_schema.load_recipe(args.recipe))
    except DiagnosticError as error:
        result = {"ok": False, "diagnostics": [item.as_json() for item in error.diagnostics]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["diagnostics"]:
            print(f"NG  {item['path']}: {item['reason']}")
        if result["ok"]:
            print("OK  重なり・はみ出し・地面へのめり込みはありません（MuJoCo、許容 1 mm）")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
