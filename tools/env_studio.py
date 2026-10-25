#!/usr/bin/env python3
"""Environment Studio: the browser UI and its JSON API over Catalog and Recipe YAML.

It runs in the Hakoniwa Business Pack Workspace with the Foundation Python
that the Studio's Recipe configures (tools/env_workspace.py), from
hakoniwa-business-pack:
  python tools/workspace.py enter
  python tools/recipe.py configure --recipe ../hakoniwa-environment-studio/recipes/business-pack/environment-studio.yaml
Its data is in the Recipe workspace, $HAKONIWA_WORK_DIR/recipes/environment-studio
(below: <ws>).

Lifecycle (as the Business Pack tools; the paths are this repository's):
  python tools/env_studio.py start [--port N] [--open-browser]   run in the background
  python tools/env_studio.py status                              is it running, and where
  python tools/env_studio.py open                                open the running Studio in the browser
  python tools/env_studio.py stop                                stop the background Studio
  python tools/env_studio.py [serve] [--port N] [--open-browser] run in this terminal (Ctrl+C)
The background Studio records <ws>/studio/studio.json (pid, port, url) and
logs to <ws>/studio/studio.log.

API (all JSON):
  GET  /api/catalogs           the Catalogs (catalogs/<id>/catalog.yaml)
  GET  /api/catalogs/<id>      one Catalog's items, resolved for the browser
  GET  /api/recipes            example and saved Recipes
  GET  /api/recipes/<id>       one Recipe (as its YAML mapping)
  PUT  /api/recipes/<id>       check (tools/env_schema.py) and save a Recipe
  GET  /api/imports/sources    Workspace repositories that offer Recipes
  GET  /api/imports/sources/id the Recipes found under one environments/
  POST /api/imports            copy one candidate Recipe and its assets
  POST /api/resolve            an item's shape with a placement's parameters
  POST /api/terrain            the terrain (and its height grid) of {catalog_id, terrain, size_m}
  POST /api/glb                the GLB of an unsaved Recipe (tools/env_generate.py), for the 3D view
  POST /api/validate           schema and MuJoCo checks of an unsaved Recipe: {ok, diagnostics}
                               (tools/env_schema.py, tools/env_validate.py)
  GET  /api/health             {app, pid, port, instance, version}: which Studio answers
  POST /api/shutdown           stop this Studio (it listens on 127.0.0.1 only)

Examples live in recipes/examples/ (read only); saving writes
<ws>/recipes/<id>.yaml, so saving an example makes an editable copy. A
Recipe names its Catalog by path; the API speaks of Catalogs by id, and the
save, 3D and validate requests carry the catalog_id they use.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import webbrowser

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_generate  # noqa: E402
import env_road_ribbon  # noqa: E402
import env_catalog_items  # noqa: E402
import env_citygml  # noqa: E402
import env_envsim  # noqa: E402
import env_cityworld  # noqa: E402
import env_plateau  # noqa: E402
import env_urban  # noqa: E402
import env_rules  # noqa: E402
import env_schema  # noqa: E402
import env_validate  # noqa: E402
import env_version  # noqa: E402
import env_workspace  # noqa: E402
from env_diagnostics import DiagnosticError, load_yaml_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
WEB_ROOT = ROOT / "web"
CATALOGS = ROOT / "catalogs"
DEFAULT_CATALOG_ID = "starter"
EXAMPLE_RECIPES = ROOT / "recipes/examples"
USER_RECIPES = env_workspace.recipe_workspace() / "recipes"
# Uncommon ports below the OS ephemeral ranges (Linux 32768+, macOS/Windows
# 49152+), clear of common services (8000, 8080, 8765); Booth Studio uses 28096.
DEFAULT_PORT = 28097
STATE_DIR = env_workspace.recipe_workspace() / "studio"
# Where City World jobs are written (serve/start --export-dir); None: the
# Studio is used on its own and offers no export of City Worlds. A tool that
# takes the Worlds (hakoniwa-urban-mobility) starts the Studio with its folder.
EXPORT_DIR: Path | None = None
APP_NAME = "environment-studio"
START_TIMEOUT_SEC = 15.0
STOP_TIMEOUT_SEC = 5.0
# `start` hands the server a one-time token that /api/health echoes. The
# server's pid can differ from the process `start` spawned: on Windows a venv's
# python.exe is a launcher that runs the real interpreter as its child.
INSTANCE_ENV = "HAKONIWA_ENVIRONMENT_STUDIO_INSTANCE"
ID_PATTERN = env_rules.ID_PATTERN


class StudioError(RuntimeError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _check_id(recipe_id: str) -> str:
    if not ID_PATTERN.match(recipe_id or ""):
        raise StudioError(f"Recipe id must be lower-case letters, digits, - or _ (up to 64): {recipe_id!r}")
    return recipe_id


def _catalog_paths() -> dict[str, Path]:
    """Catalog id (its folder name) -> catalog.yaml: this repository's Catalogs,
    then the user's (<ws>/catalogs/, the buildings registered from cities)."""
    found = {}
    for folder in (CATALOGS, env_catalog_items.USER_CATALOGS):
        if folder.is_dir():
            for path in sorted(folder.glob("*/catalog.yaml")):
                found.setdefault(path.parent.name, path.resolve())
    return found


def _catalog_path(catalog_id: str) -> Path:
    path = _catalog_paths().get(catalog_id or DEFAULT_CATALOG_ID)
    if path is None:
        raise StudioError(f"Catalog {catalog_id} not found", HTTPStatus.NOT_FOUND)
    return path


def _catalog_id_of(path: Path) -> str | None:
    resolved = Path(path).resolve()
    return next((catalog_id for catalog_id, candidate in _catalog_paths().items() if candidate == resolved), None)


def list_catalogs() -> list[dict]:
    entries = []
    for catalog_id, path in _catalog_paths().items():
        entry = {"id": catalog_id, "path": str(path),
                 "user": path.is_relative_to(env_catalog_items.USER_CATALOGS.resolve())}
        try:
            catalog = env_schema.load_catalog(path)
            entry.update(name=catalog.meta.get("name", catalog_id), description=catalog.meta.get("description", ""),
                         items=len(catalog.items))
        except DiagnosticError as exc:
            entry["error"] = str(exc)
        entries.append(entry)
    return entries


def catalog_json(catalog_id: str) -> dict:
    """A Catalog as the browser needs it: each item's resolved shape and
    behaviour, and the parameters a placement may change (no types)."""
    catalog = env_schema.load_catalog(_catalog_path(catalog_id))
    return {
        "id": catalog_id,
        "path": str(catalog.path),
        "name": catalog.meta.get("name", catalog_id),
        "description": catalog.meta.get("description", ""),
        "items": [item.as_json() for item in catalog.items.values()],
        "rules": RULES,
    }


# Constants the browser's checks share with the Python side.
RULES = {"circle_segments": env_schema.CIRCLE_SEGMENTS, "tolerance_m": env_validate.TOLERANCE_M}


def _catalog(body: dict) -> env_schema.Catalog:
    return env_schema.load_catalog(_catalog_path(body.get("catalog_id") or DEFAULT_CATALOG_ID))


def _shape_for_plan(item, params: dict, shape) -> dict:
    """A shape for the browser's plan and checks. A road_ribbon's geometry is in its
    ribbon file, which the catalog item does not have: give the plan that file's outline
    (relative paths are beside the Recipes, as for the 3D view)."""
    data = shape.as_json()
    if item.type.id != "road_ribbon" or not str(params.get("ribbon_file") or "").strip():
        return data
    ribbon = Path(str(params["ribbon_file"])).expanduser()
    if not ribbon.is_absolute():
        ribbon = (USER_RECIPES.resolve() / ribbon).resolve()
    try:
        plan = env_road_ribbon.plan_outline(ribbon)
    except (OSError, ValueError, KeyError) as exc:
        raise StudioError(f"road ribbon {ribbon.name}: {exc}") from exc
    solid = {"name": "plan-outline", "primitive": "convex", "width_m": 0, "depth_m": 0,
             "height_m": round(plan["z_range_m"][1] - plan["z_range_m"][0], 6), "x_m": 0, "y_m": 0, "z_m": 0,
             "roll_deg": 0, "pitch_deg": 0, "yaw_deg": 0, "color": params.get("color", "#444444"),
             "collide": True, "visible": True, **plan}
    return {**data, "solids": [solid]}


def resolve_json(body: object) -> dict:
    """The shape of an item with a placement's parameters (the browser asks when
    a parameter that changes the shape, such as a gate's opening, is changed)."""
    if not isinstance(body, dict):
        raise StudioError("the request body must be {catalog_id, item, params}")
    item = _catalog(body).items.get(body.get("item"))
    if item is None or item.shape is None:
        raise StudioError(f"no object item {body.get('item')!r} in the catalog", HTTPStatus.NOT_FOUND)
    params, shape = env_schema.resolve_placement(item, body.get("params") or {}, "params")
    return {"item": item.id, "params": params, **_shape_for_plan(item, params, shape)}


def resolve_many_json(body: object) -> dict:
    """The shapes of many placements at once ({catalog_id, placements: [{item,
    params}]}): opening a city Recipe needs hundreds. A placement that cannot
    be resolved answers {error} in its place."""
    if not isinstance(body, dict) or not isinstance(body.get("placements"), list):
        raise StudioError("the request body must be {catalog_id, placements: [{item, params}]}")
    catalog = _catalog(body)
    shapes = []
    for placement in body["placements"]:
        try:
            if not isinstance(placement, dict):
                raise StudioError("a placement is {item, params}")
            item = catalog.items.get(placement.get("item"))
            if item is None or item.shape is None:
                raise StudioError(f"no object item {placement.get('item')!r} in the catalog")
            params, shape = env_schema.resolve_placement(item, placement.get("params") or {}, "params")
            shapes.append({"item": item.id, "params": params, **_shape_for_plan(item, params, shape)})
        except (StudioError, DiagnosticError) as exc:
            shapes.append({"error": str(exc)})
    return {"shapes": shapes}


def terrain_json(body: object) -> dict:
    """The terrain of {catalog_id, terrain: {item, params}, size_m}, with its
    height grid (for the plan's shading)."""
    if not isinstance(body, dict):
        raise StudioError("the request body must be {catalog_id, terrain, size_m}")
    recipe = env_schema.parse_recipe({
        "schema": env_schema.RECIPE_SCHEMA, "size_m": body.get("size_m"), "terrain": body.get("terrain"),
        "objects": [],
    }, USER_RECIPES.resolve() / "terrain.yaml", _catalog(body))
    return {"item": recipe.terrain_item, **recipe.terrain.as_json()}


IMPORT_MAX_DEPTH = 4
IMPORT_MAX_YAML_FILES = 500
IMPORT_FILE_KEYS = {"visual", "collision", "dem", "heightmap", "texture", "model"}


def _import_repository(name: str) -> tuple[Path, Path]:
    """Resolve a direct Workspace child and its environments/ directory."""
    if not ID_PATTERN.match(name):
        raise StudioError("取り込み元の名前が正しくありません", HTTPStatus.BAD_REQUEST)
    repository = (WORKSPACE_ROOT / name).resolve()
    workspace = WORKSPACE_ROOT.resolve()
    environments = (repository / "environments").resolve()
    if (repository == ROOT.resolve() or not repository.is_dir()
            or not repository.is_relative_to(workspace)
            or repository.parent != workspace or not environments.is_dir()
            or not environments.is_relative_to(repository)):
        raise StudioError(f"取り込み元 {name} は候補にありません", HTTPStatus.NOT_FOUND)
    return repository, environments


def _import_recipe_paths(environments: Path) -> list[Path]:
    """YAML files below environments/, bounded in both depth and count."""
    paths: list[Path] = []
    for directory, folders, files in os.walk(environments, followlinks=False):
        current = Path(directory)
        depth = len(current.relative_to(environments).parts)
        folders[:] = [] if depth >= IMPORT_MAX_DEPTH else sorted(
            name for name in folders if not (current / name).is_symlink())
        for name in sorted(files):
            if Path(name).suffix.lower() not in (".yaml", ".yml"):
                continue
            paths.append(current / name)
            if len(paths) >= IMPORT_MAX_YAML_FILES:
                return paths
    return paths


def _import_file_references(value: object, key: str = "") -> list[str]:
    """File-valued Recipe fields supported by Environment Studio Catalogs."""
    if isinstance(value, dict):
        return [item for name, child in value.items()
                for item in _import_file_references(child, str(name))]
    if isinstance(value, list):
        return [item for child in value for item in _import_file_references(child, key)]
    if not isinstance(value, str) or not value.strip():
        return []
    suffix = Path(value).suffix.lower()
    is_path_key = key in IMPORT_FILE_KEYS or key.endswith(("_path", "_file"))
    return [value] if is_path_key or suffix in (".glb", ".xml", ".png", ".jpg", ".jpeg", ".npy", ".bin") else []


def _inspect_import_recipe(path: Path, environments: Path) -> dict:
    relative = path.relative_to(environments).as_posix()
    entry = {"recipe": relative, "id": path.stem}
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(environments.resolve()):
            raise ValueError("Workspace の environments/ 外を指しています")
        data = load_yaml_text(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != env_schema.RECIPE_SCHEMA:
            # Valid non-Recipe YAML belongs to the provider and is not a candidate.
            return {**entry, "ignored": True}
        if not ID_PATTERN.match(path.stem):
            raise ValueError("ファイル名を環境 ID に使えません")
        size = data.get("size_m") or {}
        if not isinstance(size, dict) or not all(isinstance(size.get(axis), (int, float)) for axis in ("east", "north")):
            raise ValueError("size_m.east / north がありません")
        objects = data.get("objects") or []
        if not isinstance(objects, list):
            raise ValueError("objects は配列である必要があります")
        missing = []
        for text in _import_file_references(data):
            source = Path(text).expanduser()
            source = (source if source.is_absolute() else path.parent / source).resolve()
            if not source.is_relative_to(environments.resolve()) or not source.is_file():
                missing.append(text)
        return {**entry, "name": str(data.get("name") or path.stem),
                "description": str(data.get("description") or ""),
                "size_m": {"east": size["east"], "north": size["north"]},
                "objects": len(objects), "missing_files": sorted(set(missing))}
    except (OSError, yaml.YAMLError, DiagnosticError, ValueError, TypeError) as exc:
        return {**entry, "error": str(exc)}


def _import_environments(name: str) -> list[dict]:
    _repository, environments = _import_repository(name)
    return [entry for path in _import_recipe_paths(environments)
            if not (entry := _inspect_import_recipe(path, environments)).get("ignored")]


def list_import_sources() -> list[dict]:
    """Workspace siblings with at least one valid Environment Recipe."""
    result = []
    if not WORKSPACE_ROOT.is_dir():
        return result
    for repository in sorted(WORKSPACE_ROOT.iterdir()):
        if not repository.is_dir() or repository.resolve() == ROOT.resolve():
            continue
        try:
            entries = _import_environments(repository.name)
        except StudioError:
            continue
        valid = [entry for entry in entries if not entry.get("error")]
        if valid:
            result.append({"repository": repository.name, "environments": len(valid),
                           "unreadable": len(entries) - len(valid)})
    return result


def list_import_environments(name: str) -> dict:
    entries = _import_environments(name)
    if not any(not entry.get("error") for entry in entries):
        raise StudioError(f"取り込める環境が {name} にありません", HTTPStatus.NOT_FOUND)
    return {"repository": name, "environments": entries}


def _rewrite_import_assets(value: object, *, source_recipe: Path, environments: Path,
                           target_assets: Path, target_id: str, copied: dict[Path, Path], key: str = "") -> object:
    if isinstance(value, dict):
        return {name: _rewrite_import_assets(child, source_recipe=source_recipe, environments=environments,
                                             target_assets=target_assets, target_id=target_id,
                                             copied=copied, key=str(name))
                for name, child in value.items()}
    if isinstance(value, list):
        return [_rewrite_import_assets(child, source_recipe=source_recipe, environments=environments,
                                       target_assets=target_assets, target_id=target_id,
                                       copied=copied, key=key) for child in value]
    if not isinstance(value, str) or value not in _import_file_references({key: value}):
        return value
    source = Path(value).expanduser()
    source = (source if source.is_absolute() else source_recipe.parent / source).resolve()
    if not source.is_relative_to(environments.resolve()) or not source.is_file():
        raise StudioError(f"参照ファイルがありません、または environments/ の外です: {value}")
    if source not in copied:
        asset_parent = next((parent for parent in source.parents
                             if parent.name.endswith(".assets") and parent.is_relative_to(environments)), None)
        inner = source.relative_to(asset_parent) if asset_parent else Path(source.name)
        target = target_assets / inner
        if target in copied.values() and next(item for item, result in copied.items() if result == target) != source:
            target = target.with_name(f"{source.parent.name}-{target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied[source] = target
    return f"{target_id}.assets/{copied[source].relative_to(target_assets).as_posix()}"


def import_environment(body: object) -> dict:
    if not isinstance(body, dict):
        raise StudioError("the request body must be {repository, recipe, id, overwrite}")
    repository_name = str(body.get("repository") or "")
    recipe_reference = str(body.get("recipe") or "")
    target_id = _check_id(str(body.get("id") or Path(recipe_reference).stem))
    repository, environments = _import_repository(repository_name)
    entries = {entry["recipe"]: entry for entry in _import_environments(repository_name)}
    entry = entries.get(recipe_reference)
    if entry is None or entry.get("error"):
        raise StudioError("その環境は現在の取り込み候補にありません", HTTPStatus.NOT_FOUND)
    if entry.get("missing_files"):
        raise StudioError("参照ファイルがありません: " + ", ".join(entry["missing_files"]))
    target_directory = USER_RECIPES.resolve()
    target = target_directory / f"{target_id}.yaml"
    if (target.exists() or target_id in _recipe_files()) and not body.get("overwrite"):
        raise StudioError(f"Recipe {target_id} はもうあります（上書きするか別の ID にしてください）",
                          HTTPStatus.CONFLICT)
    source_recipe = (environments / recipe_reference).resolve()
    data = load_yaml_text(source_recipe.read_text(encoding="utf-8"))
    catalog_path = _recipe_catalog_path(source_recipe, data)
    target_assets = target_directory / f"{target_id}.assets"
    if target_assets.exists() and not body.get("overwrite"):
        raise StudioError(f"資産 {target_assets.name} はもうあります（上書きするか別の ID にしてください）",
                          HTTPStatus.CONFLICT)
    staged_assets = target_directory / f".{target_id}.import-{secrets.token_hex(4)}"
    copied: dict[Path, Path] = {}
    try:
        rewritten = _rewrite_import_assets(data, source_recipe=source_recipe, environments=environments,
                                           target_assets=staged_assets, target_id=target_id, copied=copied)
        rewritten["catalog"] = _catalog_reference(catalog_path, target_directory)

        # Validate against the staged copies without exposing temporary paths
        # in the saved Recipe. Both folders have the same parent and contents.
        def staged(value: object) -> object:
            if isinstance(value, dict):
                return {name: staged(child) for name, child in value.items()}
            if isinstance(value, list):
                return [staged(child) for child in value]
            if isinstance(value, str) and value.startswith(f"{target_id}.assets/"):
                return f"{staged_assets.name}/{value.split('/', 1)[1]}"
            return value

        env_schema.parse_recipe(staged(rewritten), target)
    except StudioError:
        shutil.rmtree(staged_assets, ignore_errors=True)
        raise
    except DiagnosticError as exc:
        shutil.rmtree(staged_assets, ignore_errors=True)
        raise StudioError(f"環境を取り込めません: {exc}") from exc
    except OSError as exc:
        shutil.rmtree(staged_assets, ignore_errors=True)
        raise StudioError(f"環境の資産をコピーできません: {exc}") from exc
    target_directory.mkdir(parents=True, exist_ok=True)
    if target_assets.exists():
        shutil.rmtree(target_assets)
    if staged_assets.exists():
        staged_assets.replace(target_assets)
    env_schema.save_yaml(rewritten, target)
    record = {
        "schema": "hakoniwa.environment-import/v1", "repository": repository.name,
        "source": recipe_reference, "source_path": str(source_recipe),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    records = target_directory / ".imports"
    records.mkdir(parents=True, exist_ok=True)
    (records / f"{target_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    return {"id": target_id, "path": str(target), "copied_files": len(copied), "source": record}


def _recipe_files() -> dict[str, tuple[Path, bool, dict | None]]:
    found: dict[str, tuple[Path, bool, dict | None]] = {}
    if EXAMPLE_RECIPES.is_dir():
        for path in sorted(EXAMPLE_RECIPES.glob("*.yaml")):
            found[path.stem] = (path, False, {"kind": "example", "title": "Environment Studio"})
    if USER_RECIPES.is_dir():
        for path in sorted(USER_RECIPES.glob("*.yaml")):
            found[path.stem] = (path, True, None)  # a saved Recipe hides a read-only source of the same id
    return found


def _recipe_catalog_path(path: Path, data: dict) -> Path:
    reference = data.get("catalog")
    if not isinstance(reference, str) or not reference:
        raise StudioError("Recipe catalog must be a Catalog name or path")
    named = _catalog_paths().get(reference)
    return named if named is not None else (path.parent / reference).resolve()


def list_recipes() -> list[dict]:
    """Each Recipe's name, size, object count and ground kind, read from its
    YAML without resolving it (a city Recipe with a DEM takes seconds to
    resolve; opening it reports its problems)."""
    entries = []
    for recipe_id, (path, editable, source) in sorted(_recipe_files().items()):
        entry = {"id": recipe_id, "editable": editable, "path": str(path), "source": source}
        try:
            if not ID_PATTERN.match(recipe_id):
                raise StudioError(f"ファイル名 {path.name} は Recipe の ID に使えません（小文字・数字・- _、64 文字まで）")
            data = load_yaml_text(path.read_text(encoding="utf-8"))
            size = data["size_m"]
            catalog_path = _recipe_catalog_path(path, data)
            catalog = env_schema.load_catalog(catalog_path)
            terrain_item = catalog.items.get((data.get("terrain") or {}).get("item"))
            entry.update(name=data.get("name") or recipe_id, size_m={"east": size["east"], "north": size["north"]},
                         objects=len(data.get("objects") or []),
                         terrain=(terrain_item.type.terrain or {}).get("kind", "flat") if terrain_item else "flat",
                         catalog_id=_catalog_id_of(catalog_path))
        except (StudioError, DiagnosticError, OSError, yaml.YAMLError, KeyError, TypeError, AttributeError) as exc:
            entry["error"] = str(exc)
        entries.append(entry)
    return entries


def read_recipe(recipe_id: str) -> dict:
    found = _recipe_files().get(_check_id(recipe_id))
    if found is None:
        raise StudioError(f"Recipe {recipe_id} not found", HTTPStatus.NOT_FOUND)
    path, editable, source = found
    data = load_yaml_text(path.read_text(encoding="utf-8"))
    reference = data.get("catalog") if isinstance(data, dict) else None
    catalog_id = _catalog_id_of(_recipe_catalog_path(path, data)) if isinstance(reference, str) else None
    return {"id": recipe_id, "editable": editable, "recipe": data, "catalog_id": catalog_id, "source": source}


def _catalog_reference(catalog: Path, directory: Path) -> str:
    """A Catalog as seen from a Recipe directory.

    Relative, so a saved Recipe keeps working when the repository moves; both
    ends are resolved first (a symlinked directory would give a wrong path).
    Windows cannot relate paths on different drives: then it is absolute.
    """
    catalog = catalog.resolve()
    try:
        return Path(os.path.relpath(catalog, directory)).as_posix()
    except ValueError:
        return catalog.as_posix()


def _recipe_data(body: dict, name: str) -> dict:
    data = {"schema": env_schema.RECIPE_SCHEMA, "name": body.get("name") or name}
    if body.get("description"):
        data["description"] = body["description"]
    if body.get("geo"):
        data["geo"] = body["geo"]
    data.update(size_m=body.get("size_m"), terrain=body.get("terrain"), objects=body.get("objects", []))
    return data


def _recipe_from_body(body: object, path: Path) -> env_schema.Recipe:
    if not isinstance(body, dict):
        raise StudioError("the request body must be a Recipe mapping")
    return env_schema.parse_recipe(_recipe_data(body, path.stem), path, _catalog(body))


# --- Heavy work outside the server process -------------------------------------
# Making a GLB and compiling the MuJoCo world are long for a large Recipe (CARLA
# Town04: about 80 s and 49 min) and hold the interpreter: run in this process,
# they froze every other request. Each runs in a worker process instead; a newer
# request of the same kind (another environment chosen) cancels the older one,
# and the physics check has a time limit.

VALIDATE_LIMIT_S = float(os.environ.get("ENV_STUDIO_VALIDATE_LIMIT_S", "60"))
_WORKERS: dict[str, subprocess.Popen] = {}
_WORKERS_LOCK = threading.Lock()


class Superseded(StudioError):
    """A newer request of the same kind replaced this one."""


def _in_worker(kind: str, body: object, timeout: float | None = None, client: str | None = None) -> bytes | None:
    """Run `env_studio.py --worker <kind>` on the request body; its stdout, or None
    when the time limit was reached (the worker is stopped). A newer request of the
    same kind from the same page (`client`, the X-Studio-Client header) cancels the
    older one; requests of other pages, or without the header, are never cancelled."""
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", kind]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=ROOT)
    key = (kind, client) if client else (kind, f"pid-{process.pid}")
    with _WORKERS_LOCK:
        older = _WORKERS.get(key)
        _WORKERS[key] = process
    if older is not None and older.poll() is None:
        # Marked before the kill: on Windows the killed worker's exit code (1) does not
        # tell a replaced request from a failed one.
        older.superseded = True
        older.kill()
    try:
        out, err = process.communicate(json.dumps(body).encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    finally:
        with _WORKERS_LOCK:
            if _WORKERS.get(key) is process:
                del _WORKERS[key]
    killed = {-getattr(signal, "SIGKILL", signal.SIGTERM), -signal.SIGTERM, 137}
    if getattr(process, "superseded", False) or process.returncode in killed:
        raise Superseded("a newer request replaced this one")
    if process.returncode != 0:
        raise StudioError((err.decode("utf-8", "replace").strip().splitlines() or ["worker failed"])[-1])
    return out


def worker_main(kind: str) -> int:
    """The worker side: the request body on stdin, the result on stdout."""
    body = json.loads(sys.stdin.buffer.read() or b"null")
    try:
        if kind == "glb":
            sys.stdout.buffer.write(_preview_glb(body))
        elif kind == "validate":
            sys.stdout.buffer.write(json.dumps(_validate_recipe(body)).encode("utf-8"))
        else:
            raise StudioError(f"unknown worker {kind}")
    except StudioError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def preview_glb(body: object, client: str | None = None) -> bytes:
    return _in_worker("glb", body, client=client)


def validate_recipe(body: object, client: str | None = None) -> dict:
    out = _in_worker("validate", body, timeout=VALIDATE_LIMIT_S, client=client)
    if out is None:
        return {"ok": True, "stage": "schema", "diagnostics": [
            {"severity": "warning", "path": "", "code": "physics_timeout",
             "reason": f"MuJoCo の検証が {VALIDATE_LIMIT_S:g} 秒で終わらないので止めた（大きな環境）。"
                       "スキーマの検査は合格"}]}
    return json.loads(out)


def _preview_glb(body: object) -> bytes:
    """The GLB the generator makes for an unsaved Recipe (the same file as generate)."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "preview.yaml")
    except DiagnosticError as exc:
        raise StudioError(f"3D を作れません: {exc}") from exc
    return env_generate.environment_glb(recipe)


def preview_poses(body: object) -> dict:
    """Where each object of an unsaved Recipe stands, heights included (terrain,
    roads under it): what the 3D view needs when only poses changed, without
    making and sending the whole GLB again. Axes as the GLB node: glTF."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "preview.yaml")
    except DiagnosticError as exc:
        raise StudioError(f"3D を作れません: {exc}") from exc
    poses = {}
    for obj in recipe.objects:
        # A node showing an asset stands where the GLB puts it (env_generate.asset_frame).
        x, y, z, yaw = (env_generate.asset_frame(recipe, obj) if obj.visual is not None
                        else (obj.pose.x_m, obj.pose.y_m, obj.pose.z_m, obj.pose.yaw_deg))
        poses[obj.id] = {"translation": [x, z, -y], "yaw_deg": yaw,
                         "rotation": env_generate.gltf_rotation(obj.pose.roll_deg, obj.pose.pitch_deg, yaw)}
    return {"poses": poses}


def _png(data_url: object) -> bytes | None:
    """The PNG of a data: URL (a picture the page made), or None."""
    import base64

    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except ValueError:
        return None


def item_thumbnail(catalog_id: str, item_id: str) -> Path:
    item = env_schema.load_catalog(_catalog_path(catalog_id)).items.get(item_id)
    if item is None or not item.thumbnail or not Path(item.thumbnail).is_file():
        raise StudioError(f"{item_id} has no picture", HTTPStatus.NOT_FOUND)
    return Path(item.thumbnail)


def _my_item(action):
    try:
        return action()
    except env_catalog_items.RegisterError as exc:
        raise StudioError(str(exc), HTTPStatus.CONFLICT) from exc


def item_visual(catalog_id: str, item_id: str) -> Path:
    """An item's look (its GLB), for the page to draw its picture."""
    item = env_schema.load_catalog(_catalog_path(catalog_id)).items.get(item_id)
    visual = Path(str((item.params if item else {}).get("visual") or ""))
    if item is None or not visual.is_absolute() or not visual.is_file():
        raise StudioError(f"{item_id} has no look (GLB)", HTTPStatus.NOT_FOUND)
    return visual


def register_building(body: object) -> dict:
    """Register a building of an unsaved Recipe in the user's Catalog
    (env_catalog_items.register): its look and colliders copied beside the
    Catalog in its own frame. Body: the Recipe and {object, item_name?}."""
    if not isinstance(body, dict):
        raise StudioError("the request body must be a Recipe and {object, item_name?, recipe_id?}")
    name = str(body.get("recipe_id") or "register")
    if not ID_PATTERN.match(name):
        raise StudioError(f"recipe_id {name!r} is not an id")
    try:
        recipe = _recipe_from_body({key: value for key, value in body.items()
                                    if key not in ("object", "item_name", "recipe_id", "thumbnail")},
                                   USER_RECIPES.resolve() / f"{name}.yaml")
        result = env_catalog_items.register(recipe, str(body.get("object") or ""), str(body.get("item_name") or ""),
                                            thumbnail_png=_png(body.get("thumbnail")))
    except DiagnosticError as exc:
        raise StudioError(f"登録できません: {exc}") from exc
    except env_catalog_items.RegisterError as exc:
        raise StudioError(f"登録できません: {exc}") from exc
    return result


def layer_outlines(body: object) -> dict:
    """A City World layer's outlines made again from its GLB, one per piece
    (env_citygml._glb_outlines): a layer imported with one box over all of it
    (road markings, bridges, before) gets its real pieces. Body: an unsaved
    Recipe and {object: id}; the page puts them in (and saves when asked)."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "outlines.yaml")
    except DiagnosticError as exc:
        raise StudioError(f"外形を作れません: {exc}") from exc
    obj = next((item for item in recipe.objects if item.id == body.get("object")), None)
    if obj is None or obj.type != "city_layer" or obj.visual is None:
        raise StudioError(f"{body.get('object')!r} は見た目（GLB）のある街の層ではありません", HTTPStatus.NOT_FOUND)
    return {"object": obj.id, "outlines": env_citygml._glb_outlines(obj.visual)}


def explode_layer(body: object) -> dict:
    """The road parts a City World layer (a city-layer object, Envsim's road
    network) becomes: one road-area per outline, cut into tiles on a height
    field as an import does. Body: an unsaved Recipe and {object: id}."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "explode.yaml")
    except DiagnosticError as exc:
        raise StudioError(f"分解できません: {exc}") from exc
    obj = next((item for item in recipe.objects if item.id == body.get("object")), None)
    if obj is None or obj.type != "city_layer":
        raise StudioError(f"{body.get('object')!r} は街の層（city-layer）ではありません", HTTPStatus.NOT_FOUND)
    if (obj.source or {}).get("id") != "roads":
        raise StudioError(f"{obj.id} は道路網ではありません（分解できるのは envsim の道路網の層だけです）")
    from shapely.geometry import Polygon

    turn = math.radians(obj.pose.yaw_deg)
    cos, sin = math.cos(turn), math.sin(turn)
    used = {item.id for item in recipe.objects}
    tiled = recipe.terrain.kind == "hfield"
    parts = []
    for number, outline in enumerate(obj.params["outlines"], 1):
        world = [(obj.pose.x_m + x * cos - y * sin, obj.pose.y_m + x * sin + y * cos) for x, y in outline]
        for suffix, piece in env_citygml._road_pieces(Polygon(world), tiled):
            ring = env_citygml._clean_ring(list(piece.exterior.coords)[:-1])
            if ring is None:
                continue
            pose, points = env_citygml._placed(ring)
            parts.append({"id": env_citygml._part_id(f"{obj.id}-{number}{suffix}", used),
                          "item": env_citygml.ITEMS["road"], "pose": pose, "params": {"outline": points},
                          "source": {**(obj.source or {}), "note": f"from {obj.id}"}})
    return {"objects": parts}


def _validate_recipe(body: object) -> dict:
    """Schema, then MuJoCo checks of an unsaved Recipe, as {ok, diagnostics}:
    the problems are an answer here, not an error."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "validate.yaml")
    except DiagnosticError as error:
        return {"ok": False, "stage": "schema", "diagnostics": [item.as_json() for item in error.diagnostics]}
    if not env_validate.available():
        return {"ok": True, "stage": "schema", "diagnostics": [
            {"severity": "warning", "path": "", "code": "physics_skipped", "reason": "MuJoCo is not installed"}]}
    return {"stage": "physics", **env_validate.validate(recipe)}


# Map tiles behind the area picker (web/map.html); HAKONIWA_MAP_TILES points
# elsewhere (a company tile server, another style). OpenStreetMap's own tiles
# are for light interactive use with attribution.
DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_TILES_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'


def _ready_terrains(catalog_id: str = DEFAULT_CATALOG_ID) -> list[dict]:
    """Terrain items that make a ground from their defaults alone: map data
    brings no terrain, so one that needs data (a City World's DEM) is left out."""
    catalog = env_schema.load_catalog(_catalog_path(catalog_id))
    ready = []
    for item in catalog.items.values():
        if not item.type.is_terrain:
            continue
        try:
            env_schema.parse_recipe({"schema": env_schema.RECIPE_SCHEMA, "size_m": {"east": 20, "north": 20},
                                     "terrain": {"item": item.id}, "objects": []},
                                    USER_RECIPES.resolve() / "terrain.yaml", catalog)
        except DiagnosticError:
            continue
        ready.append({"id": item.id, "name": item.name})
    return ready


def map_config() -> dict:
    return {
        "tiles": {"url": os.environ.get("HAKONIWA_MAP_TILES", DEFAULT_TILES),
                  "attribution": os.environ.get("HAKONIWA_MAP_TILES_ATTRIBUTION", DEFAULT_TILES_ATTRIBUTION)},
        "overpass": os.environ.get("HAKONIWA_OVERPASS_URL") or "https://overpass-api.de/api/interpreter",
        "max_side_m": 2000.0,
        "terrains": _ready_terrains(),
        "export_dir": str(EXPORT_DIR) if EXPORT_DIR else None,
    }


def import_map(body: object) -> dict:
    """Make a Recipe of parts from map data and save it under <ws>/recipes/ (#10).

    Map data goes through CityGML, the shared intermediate representation:
    hakoniwa-envsim's osm2citygml.py turns OpenStreetMap (Overpass) or GeoJSON
    into LOD1 CityGML, and the parts converter (env_citygml.py) turns that
    into one part per building and road surface. The map data and the CityGML
    are kept in <ws>/map-data/<id>/, so the import can be redone offline.

    Body: {id, name?, selection: {center: {latitude, longitude},
    half_extent_m: {north_south, east_west}} (the PLATEAU City World
    browser's form; or bbox: {south, west, north, east}), source: "overpass" |
    "geojson", geojson?, terrain?, catalog_id?, overwrite?}.
    """
    if not isinstance(body, dict):
        raise StudioError("the request body must be {id, bbox, source}")
    recipe_id = _check_id(str(body.get("id") or ""))
    directory = USER_RECIPES.resolve()
    target = directory / f"{recipe_id}.yaml"
    if (target.exists() or recipe_id in _recipe_files()) and not body.get("overwrite"):
        raise StudioError(f"Recipe {recipe_id} はもうあります（別の ID にしてください）", HTTPStatus.CONFLICT)
    source = body.get("source", "overpass")
    terrain = body.get("terrain") or "city-ground"
    if terrain not in {item["id"] for item in _ready_terrains(body.get("catalog_id") or DEFAULT_CATALOG_ID)}:
        raise StudioError(f"地面 {terrain} は地図からの取り込みでは使えません（City World の地形データなどが必要な地面です）")
    data_dir = directory.parent / "map-data" / recipe_id
    osm = env_citygml.osm2citygml()
    # The area first, so a malformed one is told apart from a failing conversion.
    chosen, box = body.get("selection"), body.get("bbox")
    try:
        if isinstance(chosen, dict):
            center = (float(chosen["center"]["latitude"]), float(chosen["center"]["longitude"]))
            half = (float(chosen["half_extent_m"]["north_south"]), float(chosen["half_extent_m"]["east_west"]))
            if not all(10 <= value <= 1000 for value in half):
                raise StudioError("half_extent_m must be 10 to 1000 m (as in the PLATEAU City World browser)")
            bbox = osm.Box.of(*env_citygml.bounding_box(center, half))
        elif isinstance(box, dict):
            bbox = osm.Box.of(box["south"], box["west"], box["north"], box["east"])
        else:
            bbox = None
    except (KeyError, TypeError, ValueError, osm.OsmConversionError) as exc:
        raise StudioError("selection must be {center: {latitude, longitude}, half_extent_m: {north_south, east_west}}"
                          f" (or bbox {{south, west, north, east}}): {exc}") from exc
    if bbox is None and source != "geojson":
        raise StudioError("the area to import: selection (or bbox)")
    geojson = body.get("geojson") if source == "geojson" else None
    try:
        osm_json = None if geojson is not None else env_envsim.fetch_overpass(bbox)
        receipt = osm.run(bbox, data_dir, "map", overpass=source == "overpass", osm_json=osm_json, geojson=geojson)
        if not isinstance(chosen, dict):  # a bbox: its own centre and half extents
            selection = receipt["selection"]
            center = (selection["center"]["latitude"], selection["center"]["longitude"])
            half = (selection["half_extent_m"]["north_south"], selection["half_extent_m"]["east_west"])
        catalog = _catalog_path(body.get("catalog_id") or DEFAULT_CATALOG_ID)
        recipe, report = env_citygml.convert(
            data_dir, center, half,
            catalog=_catalog_reference(catalog, directory), name=body.get("name") or None,
            terrain_item=terrain)
        if receipt.get("data_timestamp"):
            recipe["geo"]["data_timestamp"] = receipt["data_timestamp"]
        if receipt.get("query"):
            query = json.loads(recipe["geo"]["query"])
            recipe["geo"]["query"] = json.dumps({**query, "overpass": receipt["query"]}, ensure_ascii=False, sort_keys=True)
        env_schema.parse_recipe(recipe, target)
    except (DiagnosticError, osm.OsmConversionError) as exc:
        raise StudioError(f"地図から作れません: {exc}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    env_schema.save_yaml(recipe, target)
    (data_dir / "map.json").write_text(json.dumps(osm_json or geojson, ensure_ascii=False), encoding="utf-8")
    return {
        "id": recipe_id, "path": str(target), "citygml": str(data_dir), "size_m": recipe["size_m"],
        "buildings": report["buildings"], "roads": report["roads"],
        "skipped": receipt["skipped"] + report["skipped"],
        # Counted over the parts made (the CityGML may hold more than the selection takes).
        "assumed": {
            "building_height": sum(1 for obj in recipe["objects"] if obj["item"] == "building-footprint"
                                   and (obj["source"].get("tags") or {}).get("height_source") not in (None, "height")),
            "road_width": receipt["assumed"]["road_width"], "road_lanes": receipt["assumed"]["road_lanes"]},
        "notes": receipt["notes"] + report["notes"],
    }


def city_world_roots() -> list[Path]:
    """Where to look for Envsim builds: HAKONIWA_CITY_WORLD_ROOTS (separated
    like PATH), else the Business Pack work directory (HAKONIWA_WORK_DIR);
    and the City Worlds this Studio built (env_cityworld.WORK)."""
    configured = os.environ.get("HAKONIWA_CITY_WORLD_ROOTS")
    if configured:
        roots = [Path(item).expanduser().resolve() for item in configured.split(os.pathsep) if item]
    else:
        roots = [env_workspace.work_dir()]
    return roots + [env_cityworld.WORK.resolve()]


def _built(action):
    """A City World build call, its errors as Studio errors."""
    try:
        return action()
    except env_cityworld.BuildError as exc:
        raise StudioError(str(exc), HTTPStatus(exc.status)) from exc
    except OSError as exc:  # Envsim missing, or a file that cannot be written
        raise StudioError(f"City World を作れません: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def inspect_plateau(body: object) -> dict:
    """What PLATEAU has in a selection (env_plateau.py; nothing is downloaded).
    Body: {selection: {center: {latitude, longitude}, half_extent_m: {north_south, east_west}}}."""
    if not isinstance(body, dict):
        raise StudioError("the request body must be {selection}")
    try:
        center, half = env_cityworld._selection(body)
        return env_plateau.inspect(center, half)
    except env_cityworld.BuildError as exc:
        raise StudioError(str(exc)) from exc
    except env_plateau.InspectionError as exc:
        raise StudioError(f"PLATEAU のカタログに問い合わせできません: {exc}", HTTPStatus.BAD_GATEWAY) from exc
    except DiagnosticError as exc:  # Envsim missing
        raise StudioError(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def start_city_world_build(body: object) -> dict:
    """Start an Envsim build of a selection from PLATEAU (env_cityworld.py);
    body.root (the map page's folder) is searched first for a shared cache."""
    extra = [Path(str(body["root"])).expanduser().resolve()] if isinstance(body, dict) and body.get("root") else []
    if isinstance(body, dict) and body.get("overwrite") and EXPORT_DIR:
        # Replacing a City World in the export folder would change the files its export names.
        found = env_urban.exported(EXPORT_DIR).get(str((env_cityworld.WORK / str(body.get("id")) / "build").resolve()))
        if found and body.get("remove_export"):  # build again: the page asked to remove the export too
            try:
                env_urban.remove_export(EXPORT_DIR, found["id"])
            except env_urban.ExportError as exc:
                raise StudioError(str(exc), HTTPStatus.NOT_FOUND) from exc
            found = None
        if found:
            raise StudioError(f"{body.get('id')} は書き出し先に {found['id']} として書き出してあります。作り直すには、"
                              "生成結果で「書き出しを消す」を押してから生成してください。", HTTPStatus.CONFLICT)
    return _built(lambda: env_cityworld.BUILDS.start(body, extra + city_world_roots()))


def list_city_worlds(root: str | None) -> dict:
    """The Envsim builds (City Worlds and their CityGML) in a workspace."""
    roots = [Path(root).expanduser().resolve(), env_cityworld.WORK.resolve()] if root else city_world_roots()
    try:
        builds = env_citygml.discover(roots)
    except DiagnosticError as exc:
        raise StudioError(str(exc)) from exc
    # Which of them are in the export folder.
    written = env_urban.exported(EXPORT_DIR) if EXPORT_DIR else {}
    for build in builds:
        found = written.get(str(Path(build["path"]).resolve()))
        build["exported"] = {"id": found["id"], "title": found["title"]} if found else None
        build["exportable"] = env_urban.city_world_receipt(Path(build["path"])).is_file()
    return {"roots": [str(item) for item in roots], "builds": builds,
            "export_dir": str(EXPORT_DIR) if EXPORT_DIR else None}


def list_built_city_worlds() -> dict:
    """The City Worlds this Studio built from PLATEAU (the map page's result
    list), each with whether it is in the export folder, and the shared cache."""
    jobs = env_cityworld.list_jobs()
    written = env_urban.exported(EXPORT_DIR) if EXPORT_DIR else {}
    for job in jobs:
        found = written.get(str(Path(job["build"]).resolve()))
        job["exported"] = {"id": found["id"], "title": found["title"]} if found else None
    return {"jobs": jobs, "shared_cache": env_cityworld.cache_summary(env_cityworld.plateau_cache(city_world_roots())),
            "export_dir": str(EXPORT_DIR) if EXPORT_DIR else None}


def delete_built_city_world(job_id: str, remove_export: bool = False) -> dict:
    """Delete a City World this Studio built. One in the export folder stays
    until its export is removed (the export names this build's files), unless
    remove_export also deletes that export first (to delete and build again)."""
    written = env_urban.exported(EXPORT_DIR) if EXPORT_DIR else {}
    found = written.get(str((env_cityworld.WORK / job_id / "build").resolve()))
    if found and remove_export:
        try:
            env_urban.remove_export(_export_dir(), found["id"])
        except env_urban.ExportError as exc:
            raise StudioError(str(exc), HTTPStatus.NOT_FOUND) from exc
        found = None
    if found:
        raise StudioError(f"{job_id} は書き出し先に {found['id']} として書き出してあります。先に「書き出しを消す」を"
                          "押してください（書き出したものがこの City World のファイルを使っています）。", HTTPStatus.CONFLICT)
    return _built(lambda: env_cityworld.delete_job(job_id))


def _export_dir() -> Path:
    if EXPORT_DIR is None:
        raise StudioError("書き出し先が指定されていません（env_studio.py start --export-dir で起動してください）",
                          HTTPStatus.CONFLICT)
    return EXPORT_DIR


def export_city_world(body: object) -> dict:
    """Write an Envsim City World build to the export folder as it is
    (env_urban.export_city_world). Body: {path, title?}."""
    if not isinstance(body, dict) or not body.get("path"):
        raise StudioError("the request body must be {path, title?}")
    try:
        return env_urban.export_city_world(Path(str(body["path"])).expanduser(), _export_dir(),
                                           title=body.get("title") or None)
    except env_urban.ExportError as exc:
        raise StudioError(f"書き出せません: {exc}") from exc
    except OSError as exc:
        raise StudioError(f"書き出せません: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc


def remove_export(job_id: str) -> dict:
    """Delete a job this Studio wrote to the export folder (the build stays)."""
    if not ID_PATTERN.match(job_id or ""):
        raise StudioError(f"not a job id: {job_id!r}")
    try:
        env_urban.remove_export(_export_dir(), job_id)
    except env_urban.ExportError as exc:
        raise StudioError(str(exc), HTTPStatus.NOT_FOUND) from exc
    return {"id": job_id, "removed": True}


def import_city_world(body: object) -> dict:
    """Make a Recipe of parts from an Envsim build (its selection, the
    buildings it extracted, its roads) and save it under <ws>/recipes/.

    Body: {id, path, name?, terrain?, catalog_id?, overwrite?, visuals?,
    passthrough?}: visuals false leaves out the LOD2 looks, passthrough false
    makes everything from CityGML instead of using Envsim's own outputs.
    """
    if not isinstance(body, dict) or not body.get("path"):
        raise StudioError("the request body must be {id, path}")
    recipe_id = _check_id(str(body.get("id") or ""))
    directory = USER_RECIPES.resolve()
    target = directory / f"{recipe_id}.yaml"
    if (target.exists() or recipe_id in _recipe_files()) and not body.get("overwrite"):
        raise StudioError(f"Recipe {recipe_id} はもうあります（別の ID にしてください）", HTTPStatus.CONFLICT)
    build = Path(str(body["path"])).expanduser().resolve()
    if not (build / "download-manifest.json").is_file():
        raise StudioError(f"Envsim のビルドではありません（download-manifest.json がない）: {build}", HTTPStatus.NOT_FOUND)
    try:
        catalog = _catalog_path(body.get("catalog_id") or DEFAULT_CATALOG_ID)
        recipe, report = env_citygml.convert_build(
            build, catalog=_catalog_reference(catalog, directory), name=body.get("name") or None,
            terrain_item=body.get("terrain") or "city-ground", recipe_path=target,
            visuals=body.get("visuals") is not False, passthrough=body.get("passthrough") is not False)
        env_schema.parse_recipe(recipe, target)
    except DiagnosticError as exc:
        raise StudioError(f"City World から作れません: {exc}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    env_schema.save_yaml(recipe, target)
    return {"id": recipe_id, "path": str(target), "build": str(build), "size_m": recipe["size_m"],
            "buildings": report["buildings"], "roads": report["roads"], "skipped": report["skipped"],
            "courtyards": report["courtyards"], "courtyards_filled": report["courtyards_filled"],
            "notes": report["notes"], "provider": report["provider"],
            "terrain": report["terrain"], "lod2_visuals": report.get("lod2_visuals", 0),
            "clipped": report.get("clipped", 0), "overlaps_left": report.get("overlaps_left", []),
            "passthrough": report.get("passthrough")}


def _asset_users(path: Path) -> list[str]:
    """Other saved Recipes whose visuals point into this Recipe's <id>.assets
    (a Recipe saved before copies were made, or written by hand)."""
    marker = f"{path.stem}.assets/"
    users = []
    for other_id, (other, editable, _source) in sorted(_recipe_files().items()):
        if other != path and editable:
            try:
                if marker in other.read_text(encoding="utf-8"):
                    users.append(other_id)
            except OSError:
                continue
    return users


def _own_assets(data: dict, directory: Path, recipe_id: str) -> tuple[list[Path], dict]:
    """Give a Recipe saved under a new id its own copies of the assets it
    shares with another saved Recipe (that one's <id>.assets): the objects'
    visual GLBs and colliders, the terrain's files (its folder whole: the
    receipt names the hfield beside it). So deleting one never breaks the
    other. Each asset inside `directory` but outside <recipe_id>.assets is
    copied there (to the same place inside) and the param rewritten; paths
    elsewhere (absolute, outside <ws>/recipes) stay. Returns the new files
    and folders, and the rewritten params: {"objects": {id: {param: path}},
    "terrain": {param: path}}."""
    own = directory / f"{recipe_id}.assets"
    created: list[Path] = []
    rewritten: dict = {"objects": {}, "terrain": {}}

    def adopt(text: str, whole_folder: bool) -> str | None:
        source = Path(text).expanduser()
        source = (source if source.is_absolute() else directory / source).resolve()
        if not source.is_file() or not source.is_relative_to(directory) or source.is_relative_to(own):
            return None
        parts = source.relative_to(directory).parts
        inner = Path(*parts[1:]) if len(parts) > 1 and parts[0].endswith(".assets") else Path(source.name)
        if whole_folder and len(inner.parts) > 1:
            folder = own / inner.parent
            if not folder.exists():
                shutil.copytree(source.parent, folder)
                created.append(folder)
            return f"{own.name}/{inner.as_posix()}"
        target, n = own / inner, 1
        while target.exists() and target.read_bytes() != source.read_bytes():
            n += 1
            target = own / inner.with_name(f"{inner.stem}-{n}{inner.suffix}")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            created.append(target)
        return f"{own.name}/{target.relative_to(own).as_posix()}"

    for obj in data.get("objects") if isinstance(data.get("objects"), list) else []:
        params = obj.get("params") if isinstance(obj, dict) else None
        for name in ("visual", "collision"):
            text = str((params or {}).get(name) or "").strip()
            path = adopt(text, False) if text else None
            if path:
                params[name] = rewritten["objects"].setdefault(str(obj.get("id")), {})[name] = path
    terrain = data.get("terrain") if isinstance(data.get("terrain"), dict) else {}
    params = terrain.get("params") if isinstance(terrain.get("params"), dict) else {}
    for name in ("dem", "visual"):
        text = str(params.get(name) or "").strip()
        path = adopt(text, True) if text else None
        if path:
            params[name] = rewritten["terrain"][name] = path
    return created, rewritten


def export_urban(recipe_id: str, body: object) -> dict:
    """Write a saved Recipe as a City World job (env_urban.py) to the export
    folder, as <id>/. Body: {} (kept for compatibility)."""
    found = _recipe_files().get(_check_id(recipe_id))
    if found is None:
        raise StudioError(f"Recipe {recipe_id} not found（保存してから書き出してください）", HTTPStatus.NOT_FOUND)
    try:
        result = env_urban.export(found[0], _export_dir() / recipe_id)
    except env_urban.ExportError as exc:
        raise StudioError(f"書き出せません: {exc}") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StudioError(f"書き出せません: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from exc
    return {"id": recipe_id, **result}


def delete_recipe(recipe_id: str) -> dict:
    """Move a saved Recipe to <ws>/trash/<time>-<id>/, with what belongs only
    to it: its visual assets (<id>.assets) and the map data it was imported
    from (<ws>/map-data/<id>). Nothing is erased: empty the trash by hand.
    Examples (recipes/examples) cannot be deleted."""
    found = _recipe_files().get(recipe_id)  # only a listed Recipe (also one whose name is no valid id)
    if found is None:
        raise StudioError(f"Recipe {recipe_id} not found", HTTPStatus.NOT_FOUND)
    path, editable, _source = found
    if not editable:
        raise StudioError(f"{recipe_id} は例の環境なので削除できません", HTTPStatus.FORBIDDEN)
    users = _asset_users(path)
    if users:
        raise StudioError(f"{recipe_id} の見た目（{path.stem}.assets）を {', '.join(users)} も使っているので削除できません",
                          HTTPStatus.CONFLICT)
    trash = USER_RECIPES.resolve().parent / "trash" / f"{time.strftime('%Y%m%d-%H%M%S')}-{recipe_id}"
    trash.mkdir(parents=True, exist_ok=False)
    moved = []
    for source in (path, path.parent / f"{path.stem}.assets", USER_RECIPES.resolve().parent / "map-data" / path.stem):
        if source.exists():
            target = trash / source.name if source.parent == path.parent else trash / "map-data"
            shutil.move(str(source), str(target))
            moved.append(str(target))
    return {"id": recipe_id, "trash": str(trash), "moved": moved}


def save_recipe(recipe_id: str, body: object) -> dict:
    """Check a Recipe and write it under <ws>/recipes/.

    The saved catalog path points at the Catalog the request names
    (catalog_id), relative to the saved file (a copied example keeps working).
    """
    _check_id(recipe_id)
    if not isinstance(body, dict):
        raise StudioError("the request body must be a Recipe mapping")
    directory = USER_RECIPES.resolve()
    target = directory / f"{recipe_id}.yaml"
    data = _recipe_data(body, recipe_id)
    data = {"schema": data.pop("schema"), "name": data.pop("name"),
            **({"description": data.pop("description")} if "description" in data else {}),
            "catalog": _catalog_reference(_catalog_path(body.get("catalog_id") or DEFAULT_CATALOG_ID), directory),
            **data}
    copied, rewritten = _own_assets(data, directory, recipe_id)
    try:
        recipe = env_schema.parse_recipe(data, target)
    except DiagnosticError as exc:
        for extra in copied:
            shutil.rmtree(extra) if extra.is_dir() else extra.unlink(missing_ok=True)
        raise StudioError(f"Recipe を保存できません: {exc}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    env_schema.save_yaml(data, target)
    return {"id": recipe_id, "editable": True, "path": str(target), "objects": len(recipe.objects),
            "copied_assets": len(copied), "assets": rewritten}


# The code this server runs: the checkout's commit when it started. The page
# files are read afresh on every request, so after a pull the pages are newer
# than the server; health says so (code_updated) and the pages ask for a restart.
STARTED_BUILD = env_version.build_info(ROOT)


def code_updated() -> bool:
    """Whether the checkout has moved to another commit since this server started."""
    started = STARTED_BUILD.get("commit")
    if STARTED_BUILD.get("platform") != "source" or not started:
        return False
    now = env_version.git_commit(ROOT)
    return bool(now) and now != started


class StudioHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self) -> None:
        # The Studio is edited while it runs; never serve a stale script.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        """A file as it is (a City World's GLB may be hundreds of MB: streamed)."""
        types = {".glb": "model/gltf-binary", ".zip": "application/zip", ".png": "image/png"}
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(path.stat().st_size))
        if path.suffix == ".zip":
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def _body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise StudioError("invalid Content-Length") from exc
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError as exc:
            raise StudioError(f"invalid JSON body: {exc}") from exc

    # method, path pattern ("*" matches one segment), handler(self, segments) -> response.
    ROUTES = (
        ("GET", ("health",), lambda self, _: self._json({
            "app": APP_NAME, "pid": os.getpid(), "port": self.server.server_address[1],
            "instance": os.environ.get(INSTANCE_ENV), "version": STARTED_BUILD,
            "code_updated": code_updated(),
            "export_dir": str(EXPORT_DIR) if EXPORT_DIR else None})),
        ("POST", ("shutdown",), lambda self, _: self._shutdown()),
        ("GET", ("catalogs",), lambda self, _: self._json(list_catalogs())),
        ("GET", ("catalogs", "*"), lambda self, parts: self._json(catalog_json(parts[1]))),
        ("POST", ("catalogs", "my", "items"), lambda self, _: self._json(register_building(self._body()))),
        ("GET", ("catalogs", "*", "items", "*", "thumbnail"), lambda self, parts: self._file(item_thumbnail(parts[1], parts[3]))),
        ("GET", ("catalogs", "*", "items", "*", "visual"), lambda self, parts: self._file(item_visual(parts[1], parts[3]))),
        ("POST", ("catalogs", "my", "items", "*", "rename"), lambda self, parts: self._json(_my_item(
            lambda: env_catalog_items.rename_item(_check_id(parts[3]), str((self._body() or {}).get("name") or ""))))),
        ("POST", ("catalogs", "my", "items", "*", "thumbnail"), lambda self, parts: self._json(_my_item(
            lambda: env_catalog_items.set_thumbnail(_check_id(parts[3]), _png((self._body() or {}).get("thumbnail")) or b"")))),
        ("POST", ("catalogs", "my", "items", "*", "delete"), lambda self, parts: self._json(_my_item(
            lambda: env_catalog_items.delete_item(_check_id(parts[3]), USER_RECIPES.resolve(),
                                                  USER_RECIPES.resolve().parent / "trash")))),
        ("GET", ("map", "config"), lambda self, _: self._json(map_config())),
        ("POST", ("map", "import"), lambda self, _: self._json(import_map(self._body()))),
        ("GET", ("city-worlds",), lambda self, _: self._json(list_city_worlds(self._query("root")))),
        ("POST", ("plateau", "inspect"), lambda self, _: self._json(inspect_plateau(self._body()))),
        ("POST", ("city-worlds", "build"), lambda self, _: self._json(start_city_world_build(self._body()))),
        ("GET", ("city-worlds", "build", "*"), lambda self, parts: self._json(
            _built(lambda: env_cityworld.BUILDS.status(_check_id(parts[2]))))),
        ("POST", ("city-worlds", "build", "*", "cancel"), lambda self, parts: self._json(
            _built(lambda: env_cityworld.BUILDS.cancel(_check_id(parts[2]))))),
        ("GET", ("city-worlds", "jobs"), lambda self, _: self._json(list_built_city_worlds())),
        ("GET", ("city-worlds", "jobs", "*", "*"), lambda self, parts: self._file(
            _built(lambda: env_cityworld.job_file(_check_id(parts[2]), parts[3])))),
        ("POST", ("city-worlds", "jobs", "*", "delete"), lambda self, parts: self._json(
            delete_built_city_world(_check_id(parts[2]), bool((self._body() or {}).get("remove_export"))))),
        ("POST", ("city-worlds", "import"), lambda self, _: self._json(import_city_world(self._body()))),
        ("POST", ("city-worlds", "export"), lambda self, _: self._json(export_city_world(self._body()))),
        ("POST", ("exports", "*", "delete"), lambda self, parts: self._json(remove_export(parts[1]))),
        ("GET", ("imports", "sources"), lambda self, _: self._json(list_import_sources())),
        ("GET", ("imports", "sources", "*"), lambda self, parts: self._json(
            list_import_environments(parts[2]))),
        ("POST", ("imports",), lambda self, _: self._json(import_environment(self._body()))),
        ("GET", ("recipes",), lambda self, _: self._json(list_recipes())),
        ("GET", ("recipes", "*"), lambda self, parts: self._json(read_recipe(parts[1]))),
        ("PUT", ("recipes", "*"), lambda self, parts: self._json(save_recipe(parts[1], self._body()))),
        ("POST", ("recipes", "*", "urban"), lambda self, parts: self._json(export_urban(parts[1], self._body()))),
        ("DELETE", ("recipes", "*"), lambda self, parts: self._json(delete_recipe(parts[1]))),
        ("POST", ("resolve-many",), lambda self, _: self._json(resolve_many_json(self._body()))),
        ("POST", ("resolve",), lambda self, _: self._json(resolve_json(self._body()))),
        ("POST", ("validate",), lambda self, _: self._json(validate_recipe(self._body(), self.headers.get("X-Studio-Client")))),
        ("POST", ("terrain",), lambda self, _: self._json(terrain_json(self._body()))),
        ("POST", ("poses",), lambda self, _: self._json(preview_poses(self._body()))),
        ("POST", ("explode",), lambda self, _: self._json(explode_layer(self._body()))),
        ("POST", ("layer-outlines",), lambda self, _: self._json(layer_outlines(self._body()))),
        ("POST", ("glb",), lambda self, _: self._bytes(preview_glb(self._body(), self.headers.get("X-Studio-Client")),
                                                      "model/gltf-binary")),
    )

    def _query(self, name: str) -> str | None:
        return (parse_qs(urlparse(self.path).query).get(name) or [None])[0]

    def _shutdown(self) -> None:
        self._json({"stopping": True})
        # shutdown() waits for serve_forever, so it cannot run on this request's thread.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _guard(self, method: str) -> None:
        """Only this Studio's own pages may change things: a request that
        writes must carry JSON (a plain form cannot) and, when the browser
        names an origin, come from this Studio."""
        if method == "GET":
            return
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise StudioError(f"requests from {origin} are not accepted", HTTPStatus.FORBIDDEN)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise StudioError("the request body must be application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    def _api(self, method: str) -> None:
        parts = tuple(part for part in urlparse(self.path).path.split("/") if part)[1:]
        try:
            for route_method, pattern, handler in self.ROUTES:
                if route_method == method and len(pattern) == len(parts) and all(
                        expected in ("*", actual) for expected, actual in zip(pattern, parts)):
                    self._guard(method)
                    return handler(self, parts)
            raise StudioError(f"no API {method} {self.path}", HTTPStatus.NOT_FOUND)
        except StudioError as exc:
            return self._json({"error": str(exc)}, exc.status)
        except DiagnosticError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - always answer in JSON; the log keeps the traceback
            self.log_error("unexpected error on %s %s: %r", method, self.path, exc)
            import traceback
            traceback.print_exc()
            return self._json({"error": f"Studio の内部エラー: {type(exc).__name__}: {exc}"},
                              HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        return self._api("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        return self._api("DELETE")

    def do_POST(self) -> None:  # noqa: N802
        return self._api("POST")


class StudioServer(ThreadingHTTPServer):
    """On Windows SO_REUSEADDR lets a second server bind a port that is already
    listening, so the port is taken exclusively there instead."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return StudioServer(("127.0.0.1", port), StudioHandler)


# --- Lifecycle: start / status / stop --------------------------------------------

def _state_path(state_dir: Path) -> Path:
    return state_dir / "studio.json"


def _read_state(state_dir: Path) -> dict | None:
    try:
        return json.loads(_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _health(port: int, timeout: float = 1.0) -> dict | None:
    """The Studio answering on port, or None (nothing, or not an Environment Studio)."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            data = json.loads(response.read())
    except (OSError, URLError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME else None


def _running(state_dir: Path) -> dict | None:
    """The recorded background Studio if it is still the one answering on its port."""
    state = _read_state(state_dir)
    if not state:
        return None
    health = _health(int(state.get("port", 0)))
    return state if health and health.get("pid") == state.get("pid") else None


def _port_free(port: int) -> bool:
    """Whether the Studio could listen on port (bound the way StudioServer binds)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start(port: int, open_browser: bool, state_dir: Path, export_dir: Path | None = None) -> int:
    running = _running(state_dir)
    if running:
        wanted = str(export_dir.expanduser().resolve()) if export_dir else None
        current = (_health(int(running["port"])) or {}).get("export_dir")
        if wanted != current:
            print(f"ERROR: Environment Studio is already running with export folder {current or 'none'} "
                  f"(asked for {wanted or 'none'}); stop it first: {command_hint('stop')}", file=sys.stderr)
            return 1
        print(f"Environment Studio is already running: {running['url']} (pid {running['pid']})")
        if open_browser:
            webbrowser.open(running["url"])
        return 0
    if not _port_free(port):
        print(f"ERROR: {_port_in_use(port)}", file=sys.stderr)
        return 1
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / "studio.log"
    # Keep the interpreter's UTF-8 mode (the portable entrypoints use -X utf8).
    flags = ["-X", "utf8"] if sys.flags.utf8_mode else []
    command = [sys.executable, *flags, str(Path(__file__).resolve()), "serve", "--port", str(port),
               *(["--export-dir", str(export_dir)] if export_dir else [])]
    instance = secrets.token_hex(16)
    options: dict = {"cwd": ROOT, "stdin": subprocess.DEVNULL, "env": {**os.environ, INSTANCE_ENV: instance}}
    if os.name == "nt":
        # Detached from this console, so closing it does not stop the Studio.
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with log.open("ab") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, **options)
    deadline = time.monotonic() + START_TIMEOUT_SEC
    while time.monotonic() < deadline:
        health = _health(port, timeout=0.5)
        if health and health.get("instance") == instance:
            break
        if process.poll() is not None:
            print(f"ERROR: Environment Studio exited at start (exit {process.returncode}); see {log}", file=sys.stderr)
            return 1
        time.sleep(0.2)
    else:
        process.kill()
        print(f"ERROR: Environment Studio did not answer within {START_TIMEOUT_SEC:.0f} s; see {log}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/"
    # The server's own pid (see INSTANCE_ENV); status and stop check it.
    state = {"app": APP_NAME, "pid": health["pid"], "port": port, "url": url, "log": str(log),
             "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _state_path(state_dir).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"Environment Studio started: {url} (pid {health['pid']})")
    print(f"  open it with: {command_hint('open')}")
    print(f"  stop it with: {command_hint('stop')}")
    if open_browser:
        webbrowser.open(url)
    return 0


def command_hint(command: str) -> str:
    """How the person running this installation issues a lifecycle command,
    from where they are (in the Workspace, usually hakoniwa-business-pack)."""
    script = Path(__file__).resolve()
    try:
        script = Path(os.path.relpath(script, Path.cwd()))
    except ValueError:  # another drive on Windows
        pass
    return f"python {script.as_posix()} {command}"


def status(state_dir: Path) -> int:
    running = _running(state_dir)
    if running:
        print(f"Environment Studio is running: {running['url']} (pid {running['pid']}, since {running.get('started', '?')})")
        print(f"  {env_version.describe(env_version.build_info(ROOT))}")
        return 0
    print(f"Environment Studio is not running (start it with: {command_hint('start')})")
    return 1


def open_studio(state_dir: Path, port: int) -> int:
    """Open the running Studio in the browser: the background one, else the one on port."""
    running = _running(state_dir)
    url = running["url"] if running else f"http://127.0.0.1:{port}/" if _health(port) else None
    if not url:
        print(f"Environment Studio is not running (start it with: {command_hint('start --open-browser')})",
              file=sys.stderr)
        return 1
    print(f"Opening Environment Studio: {url}")
    webbrowser.open(url)
    return 0


def stop(state_dir: Path) -> int:
    running = _running(state_dir)
    if not running:
        _state_path(state_dir).unlink(missing_ok=True)
        print("Environment Studio is not running")
        return 0
    port, pid = int(running["port"]), int(running["pid"])
    try:
        urlopen(Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                        headers={"Content-Type": "application/json"}), timeout=2).read()
    except (OSError, URLError):
        pass
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while time.monotonic() < deadline and _health(port, timeout=0.3):
        time.sleep(0.2)
    if _health(port, timeout=0.3):
        # It did not stop by itself: end the process (on Windows SIGTERM terminates it).
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    _state_path(state_dir).unlink(missing_ok=True)
    print(f"Environment Studio stopped (pid {pid})")
    return 0


def _port_in_use(port: int) -> str:
    """Why port cannot be used, and what to do."""
    health = _health(port)
    if health:
        return (f"Environment Studio is already running: http://127.0.0.1:{port}/ (pid {health['pid']}). "
                f"Open it: {command_hint('open')}, or stop it first: {command_hint('stop')}"
                + ("" if health.get("instance") else " (or Ctrl+C in the terminal that runs it)"))
    return f"port {port} is in use by another program; stop it, or pass --port"


def serve(port: int, open_browser: bool, export_dir: Path | None = None) -> int:
    global EXPORT_DIR
    EXPORT_DIR = export_dir.expanduser().resolve() if export_dir else None
    if not _port_free(port):
        running = _health(port)
        if running and open_browser:
            # Asked for the Studio in the browser: that one is it.
            url = f"http://127.0.0.1:{port}/"
            print(f"Environment Studio is already running: {url} (pid {running['pid']}); opening it")
            webbrowser.open(url)
            return 0
        print(f"ERROR: {_port_in_use(port)}", file=sys.stderr)
        return 1
    server = make_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Environment Studio: {url}" + (f" (export folder {EXPORT_DIR})" if EXPORT_DIR else ""), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "start", "status", "open", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--export-dir", type=Path,
                        help="serve/start: write City World jobs here (the folder a tool that takes them watches)")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "start":
        return start(args.port, args.open_browser, args.state_dir, args.export_dir)
    if args.command == "status":
        return status(args.state_dir)
    if args.command == "open":
        return open_studio(args.state_dir, args.port)
    if args.command == "stop":
        return stop(args.state_dir)
    return serve(args.port, args.open_browser, args.export_dir)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":   # heavy work for the server (_in_worker)
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit(main())
