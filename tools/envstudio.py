#!/usr/bin/env python3
"""Environment Studio's command line: the contract people, scripts and AI
agents use to discover types and items, and to check and resolve Recipes
(docs/ai-contract.md).

    envstudio.py contract                     contract and schema versions, commands, diagnostic codes
    envstudio.py types                        placeable types (--all: abstract ones too)
    envstudio.py describe-type <type>         a type's parameters, behaviour, terrain
    envstudio.py catalog <catalog.yaml>       a Catalog's items
    envstudio.py describe-item <catalog.yaml> <item>
    envstudio.py validate <file | ->          a Recipe (or a Catalog): {ok, diagnostics}; a Recipe is
                                              also checked with MuJoCo (--no-physics to skip)
    envstudio.py resolve <recipe | ->         a Recipe resolved: terrain, objects, solids
    envstudio.py inspect <recipe | ->         where each object is: bounds, ground under it, the nearest
                                              object and the room to each edge (for placing and repairs)
    envstudio.py generate <recipe | -> --out-dir DIR   environment.glb / .xml / .json

--json prints JSON (the default when the output is not a terminal is still
text; pass --json in scripts). A file may be YAML or JSON; "-" reads stdin
(then --base names the folder the Recipe's catalog path is relative to).
Exit status: 0 OK, 1 the input has problems (diagnostics), 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import env_schema  # noqa: E402
from env_diagnostics import CODES, DiagnosticError, fail, load_yaml_text  # noqa: E402

CONTRACT_VERSION = "1"


def _read(source: str, base: Path | None) -> tuple[dict, Path]:
    if source == "-":
        text = sys.stdin.read()
        path = (base or Path.cwd()).resolve() / "stdin.yaml"
    else:
        path = Path(source).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise fail(str(source), "missing_field", f"cannot read: {exc}") from exc
    try:
        data = load_yaml_text(text)  # JSON is YAML too
    except yaml.YAMLError as exc:
        raise fail(str(source), "wrong_type", f"invalid YAML / JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise fail(str(source), "wrong_type", "must be a mapping", expected="mapping", actual=type(data).__name__)
    return data, path


def _schemas() -> dict:
    import env_types

    return {"types": env_types.TYPES_SCHEMA, "catalog": env_schema.CATALOG_SCHEMA, "recipe": env_schema.RECIPE_SCHEMA}


COMMANDS = {
    "contract": "this summary: versions, commands, frame, diagnostic codes, exit codes",
    "types": "placeable types (--all: abstract ones too)",
    "describe-type": "a type's parameters (kind, unit, range, default, level), behaviour, terrain",
    "catalog": "a Catalog's items",
    "describe-item": "an item: its parameters, the ones a placement may change, resolved shape",
    "validate": "schema, then MuJoCo checks: {ok, diagnostics}",
    "resolve": "the Recipe resolved: terrain, objects with z and solids",
    "inspect": "bounds, ground, nearest object and room to the edges of each object",
    "generate": "environment.glb / environment.xml / environment.json",
}
FRAME = {"units": "m", "axes": "ENU (x east, y north, z up)", "origin": "centre", "yaw": "deg, counter-clockwise from east"}


def cmd_contract(args) -> dict:
    import env_version

    return {
        "contract": CONTRACT_VERSION, "version": env_version.VERSION, "schemas": _schemas(),
        "commands": COMMANDS, "frame": FRAME, "diagnostic_codes": CODES,
        "diagnostic_fields": ["severity", "path", "code", "expected", "actual", "reason", "related"],
        "exit_codes": {"0": "OK", "1": "the input has problems (diagnostics)", "2": "bad usage"},
    }


def cmd_types(args) -> dict:
    library = env_schema.types()
    rows = [
        {"id": t.id, "label": t.label, "kind": "terrain" if t.is_terrain else "object", "abstract": t.abstract,
         "extends": t.parents[0] if t.parents else None, "description": t.description}
        for t in library.types.values() if args.all or not t.abstract
    ]
    return {"contract": CONTRACT_VERSION, "schemas": _schemas(), "types": rows}


def cmd_describe_type(args) -> dict:
    library = env_schema.types()
    if args.type not in library.types:
        raise fail("type", "unknown_reference", "no such type", expected=sorted(library.types), actual=args.type)
    return library.types[args.type].as_json()


def cmd_catalog(args) -> dict:
    catalog = env_schema.load_catalog(Path(args.catalog))
    return {"catalog": catalog.meta, "items": [
        {"id": item.id, "name": item.name, "type": item.type.id, "kind": "terrain" if item.type.is_terrain else "object",
         "category": item.category} for item in catalog.items.values()]}


def cmd_describe_item(args) -> dict:
    catalog = env_schema.load_catalog(Path(args.catalog))
    if args.item not in catalog.items:
        raise fail("item", "unknown_reference", "no such item", expected=sorted(catalog.items), actual=args.item)
    return catalog.items[args.item].as_json()


def _parse(args):
    data, path = _read(args.file, args.base)
    schema = data.get("schema")
    if schema == env_schema.CATALOG_SCHEMA:
        return "catalog", env_schema.parse_catalog(data, path)
    return "recipe", env_schema.parse_recipe(data, path)


def cmd_validate(args) -> dict:
    """Schema problems stop here (exit 1); a valid Recipe is then checked with
    MuJoCo, whose problems come back the same way."""
    import env_validate

    kind, parsed = _parse(args)
    summary = {"kind": kind}
    if kind != "recipe":
        return {"ok": True, "diagnostics": [], **summary, "items": len(parsed.items)}
    summary.update(objects=len(parsed.objects), terrain=parsed.terrain.kind)
    if args.no_physics:
        return {"ok": True, "diagnostics": [], **summary, "physics": "skipped"}
    if not env_validate.available():
        return {"ok": True, "diagnostics": [{"severity": "warning", "path": "", "code": "physics_skipped",
                                             "reason": "MuJoCo is not installed: the physical checks were skipped"}],
                **summary, "physics": "skipped"}
    diagnostics = env_validate.check(parsed)
    if any(item.severity == "error" for item in diagnostics):
        raise DiagnosticError(diagnostics)
    return {"ok": True, "diagnostics": [item.as_json() for item in diagnostics], **summary, "physics": "mujoco",
            "tolerance_m": env_validate.TOLERANCE_M}


def resolved_json(recipe) -> dict:
    return {
        "name": recipe.name, "description": recipe.description,
        "size_m": {"east": recipe.size_east_m, "north": recipe.size_north_m},
        "frame": FRAME,
        "terrain": {"item": recipe.terrain_item, **recipe.terrain.as_json(with_heights=False)},
        "objects": [{
            "id": obj.id, "item": obj.item, "type": obj.type,
            "pose": {"x_m": obj.pose.x_m, "y_m": obj.pose.y_m, "z_m": obj.pose.z_m, "yaw_deg": obj.pose.yaw_deg},
            "params": obj.params, **obj.shape.as_json(), **({"source": obj.source} if obj.source else {}),
        } for obj in recipe.objects],
        **({"geo": recipe.geo} if recipe.geo else {}),
    }


def cmd_resolve(args) -> dict:
    kind, parsed = _parse(args)
    if kind != "recipe":
        raise fail("schema", "wrong_schema", "resolve takes a Recipe", expected=env_schema.RECIPE_SCHEMA)
    return resolved_json(parsed)


def _gap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    """The distance between two convex outlines on the plan; 0 when they touch or overlap."""
    for polygon in (a, b):
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
            nx, ny = y2 - y1, x1 - x2  # outward normal of a counter-clockwise edge
            edge = nx * x1 + ny * y1
            if all(nx * x + ny * y >= edge for x, y in (b if polygon is a else a)):
                break
        else:
            continue
        break
    else:
        return 0.0  # no separating edge: they overlap

    def to_segment(point, start, end):
        (px, py), (x1, y1), (x2, y2) = point, start, end
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ((dx * dx + dy * dy) or 1.0)))
        return math.hypot(px - x1 - t * dx, py - y1 - t * dy)

    return min(to_segment(point, start, end)
               for points, polygon in ((a, b), (b, a)) for point in points
               for start, end in zip(polygon, polygon[1:] + polygon[:1]))


def cmd_inspect(args) -> dict:
    """Where everything is, in the numbers an agent needs to place or move an
    object: its bounds on the plan and in height, the ground under it, the
    nearest other object (0 when they touch or overlap on the plan) and the
    room between it and each edge (negative: it sticks out)."""
    kind, parsed = _parse(args)
    if kind != "recipe":
        raise fail("schema", "wrong_schema", "inspect takes a Recipe", expected=env_schema.RECIPE_SCHEMA)
    round_mm = lambda value: round(value, 3)  # noqa: E731
    half_east, half_north = parsed.size_east_m / 2, parsed.size_north_m / 2
    # The solids' own outlines (an L-shaped building, a bending road), not the envelope.
    outlines = {obj.id: [env_schema.placed(obj, solid.outline()) for solid in obj.solids if solid.collide]
                or [env_schema.footprint(obj)] for obj in parsed.objects}
    heights = [h for row in parsed.terrain.heights for h in row] or [0.0]  # flat ground has no grid
    objects = []
    for obj in parsed.objects:
        pieces = outlines[obj.id]
        xs, ys = [x for piece in pieces for x, _ in piece], [y for piece in pieces for _, y in piece]
        gaps = sorted((min(_gap(mine, theirs) for mine in pieces for theirs in outlines[other.id]), other.id)
                      for other in parsed.objects if other.id != obj.id)
        objects.append({
            "id": obj.id, "item": obj.item, "type": obj.type,
            "pose": {"x_m": obj.pose.x_m, "y_m": obj.pose.y_m, "z_m": obj.pose.z_m, "yaw_deg": obj.pose.yaw_deg},
            "bounds_m": {"x": [round_mm(min(xs)), round_mm(max(xs))], "y": [round_mm(min(ys)), round_mm(max(ys))],
                         "z": [round_mm(obj.pose.z_m + obj.shape.bottom_m), round_mm(obj.pose.z_m + obj.shape.height_m)]},
            "ground_m": round_mm(parsed.terrain.highest_under(env_schema.footprint(obj))),
            "nearest": {"id": gaps[0][1], "gap_m": round_mm(gaps[0][0])} if gaps else None,
            "room_m": {"east": round_mm(half_east - max(xs)), "west": round_mm(min(xs) + half_east),
                       "north": round_mm(half_north - max(ys)), "south": round_mm(min(ys) + half_north)},
        })
    return {
        "size_m": {"east": parsed.size_east_m, "north": parsed.size_north_m}, "frame": FRAME,
        "extent_m": {"x": [-half_east, half_east], "y": [-half_north, half_north]},
        "terrain": {"item": parsed.terrain_item, "kind": parsed.terrain.kind,
                    "height_m": [round_mm(min(heights)), round_mm(max(heights))]},
        "objects": objects,
    }


def cmd_generate(args) -> dict:
    import env_generate

    kind, parsed = _parse(args)
    if kind != "recipe":
        raise fail("schema", "wrong_schema", "generate takes a Recipe", expected=env_schema.RECIPE_SCHEMA)
    paths = env_generate.generate(parsed, args.out_dir)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    return {"ok": True, "files": {kind: str(path) for kind, path in paths.items()}, "fingerprint": manifest["fingerprint"]}


def _text(command: str, result: dict) -> str:
    if command == "inspect":
        return "\n".join(
            f"{row['id']:16} x {row['bounds_m']['x']} y {row['bounds_m']['y']} z {row['bounds_m']['z']}  "
            f"ground {row['ground_m']}  nearest {row['nearest']['id'] + ' ' + str(row['nearest']['gap_m']) + ' m' if row['nearest'] else '-'}"
            for row in result["objects"])
    if command == "types":
        return "\n".join(f"{row['id']:18} {row['kind']:8} {row['label']}" for row in result["types"])
    if command == "catalog":
        return "\n".join(f"{row['id']:22} {row['type']:16} {row['name']}" for row in result["items"])
    if command == "generate":
        return "\n".join(f"{kind:8} {path}" for kind, path in result["files"].items())
    if command == "validate":
        return f"OK  {result['kind']}" + (f" ({result['objects']} objects, {result['terrain']} terrain, "
                                            f"physics: {result['physics']})"
                                            if result["kind"] == "recipe" else f" ({result['items']} items)")
    return json.dumps(result, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envstudio", description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("contract")
    types_parser = commands.add_parser("types")
    types_parser.add_argument("--all", action="store_true")
    commands.add_parser("describe-type").add_argument("type")
    commands.add_parser("catalog").add_argument("catalog")
    describe_item = commands.add_parser("describe-item")
    describe_item.add_argument("catalog")
    describe_item.add_argument("item")
    for name in ("validate", "resolve", "inspect", "generate"):
        sub = commands.add_parser(name)
        sub.add_argument("file", help="a YAML / JSON file, or - for stdin")
        sub.add_argument("--base", type=Path, help="with -, the folder the catalog path is relative to")
        if name == "generate":
            sub.add_argument("--out-dir", type=Path, required=True)
        if name == "validate":
            sub.add_argument("--no-physics", action="store_true", help="skip the MuJoCo checks")
    args = parser.parse_args(argv)
    handlers = {"contract": cmd_contract, "inspect": cmd_inspect, "types": cmd_types, "describe-type": cmd_describe_type, "catalog": cmd_catalog,
                "describe-item": cmd_describe_item, "validate": cmd_validate, "resolve": cmd_resolve,
                "generate": cmd_generate}
    try:
        result = handlers[args.command](args)
    except DiagnosticError as error:
        problems = {"ok": False, "diagnostics": [item.as_json() for item in error.diagnostics]}
        if args.json:
            print(json.dumps(problems, ensure_ascii=False, indent=2))
        else:
            for item in error.diagnostics:
                print(f"NG  {item.path}: {item.reason}（{item.code}"
                      + (f"、期待: {item.expected}" if item.expected not in (None, []) else "")
                      + (f"、実際: {item.actual!r}" if item.actual is not None else "") + "）", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _text(args.command, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
