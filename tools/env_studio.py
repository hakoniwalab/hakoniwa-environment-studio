#!/usr/bin/env python3
"""Environment Studio: the browser UI and its JSON API over Catalog and Recipe YAML.

Lifecycle (as the Business Pack tools):
  python tools/env_studio.py start [--port N] [--open-browser]   run in the background
  python tools/env_studio.py status                              is it running, and where
  python tools/env_studio.py stop                                stop the background Studio
  python tools/env_studio.py [serve] [--port N] [--open-browser] run in this terminal (Ctrl+C)
The background Studio records work/studio/studio.json (pid, port, url) and
logs to work/studio/studio.log.

API (all JSON):
  GET  /api/catalogs           the Catalogs (catalogs/<id>/catalog.yaml)
  GET  /api/catalogs/<id>      one Catalog's items, resolved for the browser
  GET  /api/recipes            example and saved Recipes
  GET  /api/recipes/<id>       one Recipe (as its YAML mapping)
  PUT  /api/recipes/<id>       check (tools/env_schema.py) and save a Recipe
  POST /api/resolve            an item's shape with a placement's parameters
  POST /api/terrain            the terrain (and its height grid) of {catalog_id, terrain, size_m}
  POST /api/glb                the GLB of an unsaved Recipe (tools/env_generate.py), for the 3D view
  POST /api/validate           schema and MuJoCo checks of an unsaved Recipe: {ok, diagnostics}
                               (tools/env_schema.py, tools/env_validate.py)
  GET  /api/health             {app, pid, port, instance, version}: which Studio answers
  POST /api/shutdown           stop this Studio (it listens on 127.0.0.1 only)

Examples live in recipes/examples/ (read only); saving writes
work/recipes/<id>.yaml, so saving an example makes an editable copy. A
Recipe names its Catalog by path; the API speaks of Catalogs by id, and the
save, 3D and validate requests carry the catalog_id they use.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import webbrowser

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_generate  # noqa: E402
import env_citygml  # noqa: E402
import env_schema  # noqa: E402
import env_validate  # noqa: E402
import env_version  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
CATALOGS = ROOT / "catalogs"
DEFAULT_CATALOG_ID = "starter"
EXAMPLE_RECIPES = ROOT / "recipes/examples"
USER_RECIPES = ROOT / "work/recipes"
DEFAULT_PORT = 8097  # Booth Studio uses 8096
STATE_DIR = ROOT / "work/studio"
APP_NAME = "environment-studio"
START_TIMEOUT_SEC = 15.0
STOP_TIMEOUT_SEC = 5.0
# `start` hands the server a one-time token that /api/health echoes. The
# server's pid can differ from the process `start` spawned: on Windows a venv's
# python.exe is a launcher that runs the real interpreter as its child.
INSTANCE_ENV = "HAKONIWA_ENVIRONMENT_STUDIO_INSTANCE"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class StudioError(RuntimeError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _check_id(recipe_id: str) -> str:
    if not ID_PATTERN.match(recipe_id or ""):
        raise StudioError(f"Recipe id must be lower-case letters, digits, - or _ (up to 64): {recipe_id!r}")
    return recipe_id


def _catalog_paths() -> dict[str, Path]:
    """Catalog id (its folder name) -> catalog.yaml."""
    if not CATALOGS.is_dir():
        return {}
    return {path.parent.name: path.resolve() for path in sorted(CATALOGS.glob("*/catalog.yaml"))}


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
        entry = {"id": catalog_id, "path": str(path)}
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


def resolve_json(body: object) -> dict:
    """The shape of an item with a placement's parameters (the browser asks when
    a parameter that changes the shape, such as a gate's opening, is changed)."""
    if not isinstance(body, dict):
        raise StudioError("the request body must be {catalog_id, item, params}")
    item = _catalog(body).items.get(body.get("item"))
    if item is None or item.shape is None:
        raise StudioError(f"no object item {body.get('item')!r} in the catalog", HTTPStatus.NOT_FOUND)
    params, shape = env_schema.resolve_placement(item, body.get("params") or {}, "params")
    return {"item": item.id, "params": params, **shape.as_json()}


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


def _recipe_files() -> dict[str, tuple[Path, bool]]:
    found: dict[str, tuple[Path, bool]] = {}
    for directory, editable in ((EXAMPLE_RECIPES, False), (USER_RECIPES, True)):
        if directory.is_dir():
            for path in sorted(directory.glob("*.yaml")):
                found[path.stem] = (path, editable)  # a saved Recipe hides the example of the same id
    return found


def list_recipes() -> list[dict]:
    entries = []
    for recipe_id, (path, editable) in sorted(_recipe_files().items()):
        entry = {"id": recipe_id, "editable": editable, "path": str(path)}
        try:
            recipe = env_schema.load_recipe(path)
            entry.update(name=recipe.name, size_m={"east": recipe.size_east_m, "north": recipe.size_north_m},
                         objects=len(recipe.objects), terrain=recipe.terrain.kind,
                         catalog_id=_catalog_id_of(recipe.catalog.path))
        except DiagnosticError as exc:
            entry["error"] = str(exc)
        entries.append(entry)
    return entries


def read_recipe(recipe_id: str) -> dict:
    found = _recipe_files().get(_check_id(recipe_id))
    if found is None:
        raise StudioError(f"Recipe {recipe_id} not found", HTTPStatus.NOT_FOUND)
    path, editable = found
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    reference = data.get("catalog") if isinstance(data, dict) else None
    catalog_id = _catalog_id_of(path.parent / reference) if isinstance(reference, str) else None
    return {"id": recipe_id, "editable": editable, "recipe": data, "catalog_id": catalog_id}


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


def preview_glb(body: object) -> bytes:
    """The GLB the generator makes for an unsaved Recipe (the same file as generate)."""
    try:
        recipe = _recipe_from_body(body, USER_RECIPES.resolve() / "preview.yaml")
    except DiagnosticError as exc:
        raise StudioError(f"3D を作れません: {exc}") from exc
    return env_generate.environment_glb(recipe)


def validate_recipe(body: object) -> dict:
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


def map_config() -> dict:
    return {
        "tiles": {"url": os.environ.get("HAKONIWA_MAP_TILES", DEFAULT_TILES),
                  "attribution": os.environ.get("HAKONIWA_MAP_TILES_ATTRIBUTION", DEFAULT_TILES_ATTRIBUTION)},
        "overpass": os.environ.get("HAKONIWA_OVERPASS_URL") or "https://overpass-api.de/api/interpreter",
        "max_side_m": 2000.0,
    }


def import_map(body: object) -> dict:
    """Make a Recipe of parts from map data and save it under work/recipes/ (#10).

    Map data goes through CityGML, the shared intermediate representation:
    hakoniwa-envsim's osm2citygml.py turns OpenStreetMap (Overpass) or GeoJSON
    into LOD1 CityGML, and the parts converter (env_citygml.py) turns that
    into one part per building and road surface. The map data and the CityGML
    are kept in work/map-data/<id>/, so the import can be redone offline.

    Body: {id, name?, bbox: {south, west, north, east}, source: "overpass" |
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
    data_dir = directory.parent / "map-data" / recipe_id
    try:
        osm = env_citygml.osm2citygml()
        box = body.get("bbox")
        bbox = osm.Box.of(box["south"], box["west"], box["north"], box["east"]) if isinstance(box, dict) else None
        geojson = body.get("geojson") if source == "geojson" else None
        osm_json = None if geojson is not None else osm.fetch_overpass(bbox) if bbox else None
        receipt = osm.run(bbox, data_dir, "map", overpass=source == "overpass", osm_json=osm_json, geojson=geojson)
        selection = receipt["selection"]
        catalog = _catalog_path(body.get("catalog_id") or DEFAULT_CATALOG_ID)
        recipe, report = env_citygml.convert(
            data_dir, (selection["center"]["latitude"], selection["center"]["longitude"]),
            (selection["half_extent_m"]["north_south"], selection["half_extent_m"]["east_west"]),
            catalog=_catalog_reference(catalog, directory), name=body.get("name") or None,
            terrain_item=body.get("terrain") or "city-ground")
        if receipt.get("data_timestamp"):
            recipe["geo"]["data_timestamp"] = receipt["data_timestamp"]
        if receipt.get("query"):
            query = json.loads(recipe["geo"]["query"])
            recipe["geo"]["query"] = json.dumps({**query, "overpass": receipt["query"]}, ensure_ascii=False, sort_keys=True)
        env_schema.parse_recipe(recipe, target)
    except (KeyError, TypeError) as exc:
        raise StudioError("bbox must be {south, west, north, east} in degrees") from exc
    except DiagnosticError as exc:
        raise StudioError(f"地図から作れません: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - Envsim's converter reports its own errors
        if type(exc).__name__ != "OsmConversionError":
            raise
        raise StudioError(f"地図から作れません: {exc}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    (data_dir / "map.json").write_text(json.dumps(osm_json or geojson, ensure_ascii=False), encoding="utf-8")
    return {
        "id": recipe_id, "path": str(target), "citygml": str(data_dir), "size_m": recipe["size_m"],
        "buildings": report["buildings"], "roads": report["roads"],
        "skipped": receipt["skipped"] + report["skipped"], "assumed": receipt["assumed"],
        "notes": receipt["notes"] + report["notes"],
    }


def save_recipe(recipe_id: str, body: object) -> dict:
    """Check a Recipe and write it under work/recipes/.

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
    try:
        recipe = env_schema.parse_recipe(data, target)
    except DiagnosticError as exc:
        raise StudioError(f"Recipe を保存できません: {exc}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"id": recipe_id, "editable": True, "path": str(target), "objects": len(recipe.objects)}


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

    def _body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError as exc:
            raise StudioError(f"invalid JSON body: {exc}") from exc

    def _api(self, method: str) -> None:
        parts = [part for part in urlparse(self.path).path.split("/") if part][1:]
        try:
            if method == "GET" and parts == ["health"]:
                return self._json({
                    "app": APP_NAME, "pid": os.getpid(), "port": self.server.server_address[1],
                    "instance": os.environ.get(INSTANCE_ENV),
                    "version": env_version.build_info(ROOT),
                })
            if method == "POST" and parts == ["shutdown"]:
                self._json({"stopping": True})
                # shutdown() waits for serve_forever, so it cannot run on this request's thread.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None
            if method == "GET" and parts == ["catalogs"]:
                return self._json(list_catalogs())
            if method == "GET" and len(parts) == 2 and parts[0] == "catalogs":
                return self._json(catalog_json(parts[1]))
            if method == "GET" and parts == ["map", "config"]:
                return self._json(map_config())
            if method == "POST" and parts == ["map", "import"]:
                return self._json(import_map(self._body()))
            if method == "GET" and parts == ["recipes"]:
                return self._json(list_recipes())
            if method == "POST" and parts == ["resolve"]:
                return self._json(resolve_json(self._body()))
            if method == "POST" and parts == ["validate"]:
                return self._json(validate_recipe(self._body()))
            if method == "POST" and parts == ["terrain"]:
                return self._json(terrain_json(self._body()))
            if method == "POST" and parts == ["glb"]:
                return self._bytes(preview_glb(self._body()), "model/gltf-binary")
            if len(parts) == 2 and parts[0] == "recipes":
                if method == "GET":
                    return self._json(read_recipe(parts[1]))
                if method == "PUT":
                    return self._json(save_recipe(parts[1], self._body()))
            raise StudioError(f"no API {method} {self.path}", HTTPStatus.NOT_FOUND)
        except StudioError as exc:
            return self._json({"error": str(exc)}, exc.status)
        except DiagnosticError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        return self._api("PUT")

    def do_POST(self) -> None:  # noqa: N802
        return self._api("POST")


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), StudioHandler)


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
    """Whether the Studio could listen on port (address reuse on, as the server has)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start(port: int, open_browser: bool, state_dir: Path) -> int:
    running = _running(state_dir)
    if running:
        print(f"Environment Studio is already running: {running['url']} (pid {running['pid']})")
        if open_browser:
            webbrowser.open(running["url"])
        return 0
    if not _port_free(port):
        print(f"ERROR: port {port} is in use; stop what uses it or pass --port", file=sys.stderr)
        return 1
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / "studio.log"
    # Keep the interpreter's UTF-8 mode (the portable entrypoints use -X utf8).
    flags = ["-X", "utf8"] if sys.flags.utf8_mode else []
    command = [sys.executable, *flags, str(Path(__file__).resolve()), "serve", "--port", str(port)]
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
    print(f"  stop it with: {command_hint('stop')}")
    if open_browser:
        webbrowser.open(url)
    return 0


def command_hint(command: str) -> str:
    """How the person running this installation issues a lifecycle command."""
    return f"python tools/env_studio.py {command}"


def status(state_dir: Path) -> int:
    running = _running(state_dir)
    if running:
        print(f"Environment Studio is running: {running['url']} (pid {running['pid']}, since {running.get('started', '?')})")
        print(f"  {env_version.describe(env_version.build_info(ROOT))}")
        return 0
    print(f"Environment Studio is not running (start it with: {command_hint('start')})")
    return 1


def stop(state_dir: Path) -> int:
    running = _running(state_dir)
    if not running:
        _state_path(state_dir).unlink(missing_ok=True)
        print("Environment Studio is not running")
        return 0
    port, pid = int(running["port"]), int(running["pid"])
    try:
        urlopen(Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST"), timeout=2).read()
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


def serve(port: int, open_browser: bool) -> int:
    server = make_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Environment Studio: {url}", flush=True)
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
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "start", "status", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "start":
        return start(args.port, args.open_browser, args.state_dir)
    if args.command == "status":
        return status(args.state_dir)
    if args.command == "stop":
        return stop(args.state_dir)
    return serve(args.port, args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
