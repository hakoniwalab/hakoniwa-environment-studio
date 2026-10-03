#!/usr/bin/env python3
"""Build a PLATEAU City World with hakoniwa-envsim from the Studio (#14).

The map page's selection (centre, half extents: the City World browser's
rules) becomes an Envsim build under city-worlds/<id>/ in the Studio's Recipe
workspace ($HAKONIWA_WORK_DIR/recipes/environment-studio): its
hakoniwa-envsim-build.yaml (the business pack's visual-physics profile:
LOD2 looks, building physics P0-P3, DEM terrain, roads, markings, bridges)
and job.json, then `tools/hako.py --config ... build` runs in the background.
Envsim downloads the CityGML from PLATEAU (a shared cache is used again: the
business pack's when it is there) and converts it; its [HAKO_PROGRESS] lines
say how far it is. The finished build is a workspace City World like any
other: the map page imports it as parts (env_citygml.convert_build), Envsim's
own outputs passed through.

After Envsim, the job gets its viewer files as the City World Web UI makes
them (viewer/city-world.glb, and viewer/city-world-colliders.glb from Envsim's
mjcf_colliders2glb.py), so it follows hakoniwa-urban-mobility's City World job
contract (schemas/city-world-job.yaml) and can be registered there.

One build runs at a time (Envsim keeps its resolved manifest in one place).
`offline` rebuilds a City World built before from what that build fetched
(its catalog answers and CityGML): Envsim reads them from the build folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_envsim  # noqa: E402
import env_rules  # noqa: E402
import env_workspace  # noqa: E402

WORK = env_workspace.recipe_workspace() / "city-worlds"
BUSINESS_PACK_CACHE = "recipes/city-world-web-ui/runtime/cache/plateau-citygml"
MIN_HALF_M, MAX_HALF_M = 10.0, 1000.0
LOG_TAIL_LINES = 30


class BuildError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def plateau_cache(roots: list[Path]) -> Path:
    """Where downloaded PLATEAU CityGML is kept: HAKONIWA_PLATEAU_CACHE, else
    the City World Web UI's shared cache in the Business Pack work directory,
    else city-worlds/cache in the Studio's Recipe workspace."""
    configured = os.environ.get("HAKONIWA_PLATEAU_CACHE")
    if configured:
        return Path(configured).expanduser().resolve()
    for root in roots:
        if (root / BUSINESS_PACK_CACHE).is_dir():
            return (root / BUSINESS_PACK_CACHE).resolve()
    return (WORK / "cache").resolve()


# The build conditions the map page offers (hakoniwa-envsim
# docs/hakoniwa-build-reference.md), with the City World Web UI's defaults.
BUILD_OPTIONS = {
    "building_physics_level": (3, (0, 1, 2, 3)),
    "building_collider_reduction": ("safe", ("safe", "coplanar-union", "convex-decompose", "tolerant-planar")),
    "terrain_uncovered_policy": ("error", ("error", "constant")),
    # Lower the DEM under bridges to the low ground around them (Envsim, inferred).
    "terrain_bridge_carve": (False, (False, True)),
    # Ease the DEM outside the bridges' floor edges to the edges' heights (Envsim).
    "terrain_bridge_blend": (False, (False, True)),
}


def build_options(given: object) -> dict:
    """The build conditions of a request, the defaults filled in."""
    given = given if isinstance(given, dict) else {}
    options = {}
    for name, (default, allowed) in BUILD_OPTIONS.items():
        value = given.get(name, default)
        if value not in allowed:
            raise BuildError(f"{name} must be one of {', '.join(map(str, allowed))} (got {value!r})")
        options[name] = value
    return options


def build_config(job: Path, center: tuple[float, float], half: tuple[float, float], cache: Path,
                 options: dict | None = None, files: Path | None = None) -> str:
    """The Envsim build of a selection, as the business pack's City World
    Web UI makes it (profile visual-physics-v1), with the chosen conditions.
    terrain_uncovered_policy constant makes the ground flat at 0 m where there
    is no DEM (a whole area without DEM gets a flat ground).

    `files`: local CityGML (OpenStreetMap through osm2citygml.py) instead of
    PLATEAU: buildings and roads, no DEM (the ground is flat at 0 m), no road
    markings or bridges (Envsim's source.kind files)."""
    lat, lon = center
    ns, ew = half
    options = build_options(options)
    if files is not None:
        options["terrain_uncovered_policy"] = "constant"
        source = f"""source:
  kind: files
  path: {files.as_posix()}
  feature_type: bldg
  feature_types:
    bldg: true
    tran: true
    dem: false
    frn: false
    brid: false
"""
    else:
        source = f"""source:
  api_base_url: https://api.plateauview.mlit.go.jp
  cache_dir: {cache.as_posix()}
  feature_type: bldg
  feature_types:
    bldg: true
    tran: true
    dem: true
    frn: true
    brid: true
  year: latest
"""
    return f"""version: 1
component: hakoniwa-envsim

pipeline:
  type: plateau-citygml-to-assets

{source}
selection:
  center:
    latitude: {lat:.9f}
    longitude: {lon:.9f}
  half_extent_m:
    north_south: {ns}
    east_west: {ew}

geometry:
  base_epsilon_m: 0.2
  waste_threshold: 0.1
  wall_thickness_m: 0.1
  roof_collision_thickness_m: 0.02

mjcf:
  model_name: plateau_city_world
  collision: all
  floor: false
  building_physics_level: {options["building_physics_level"]}
  building_collider_reduction: {options["building_collider_reduction"]}

glb:
  enabled: true
  lod_policy: highest_available
  texture_mode: embedded-if-available

city_world:
  enabled: true
  parallel_workers: 8
  dem_parallel_workers: 4
  building_physics_workers: 4
  terrain_spacing_m: 2
  terrain_uncovered_policy: {options["terrain_uncovered_policy"]}
  terrain_uncovered_elevation_m: 0
  terrain_bridge_carve: {"true" if options["terrain_bridge_carve"] else "false"}
  terrain_bridge_blend: {"true" if options["terrain_bridge_blend"] else "false"}
  marking_vertical_offset_m: 0.055
  bridge_collision_thickness_m: 0.02
  bridge_max_surface_slope_deg: 60

output:
  build_dir: {(job / 'build').as_posix()}
  install_dir: {(job / 'install').as_posix()}
  name: city-world
"""


def _selection(body: dict) -> tuple[tuple[float, float], tuple[float, float]]:
    try:
        selection = body["selection"]
        lat = float(selection["center"]["latitude"])
        lon = float(selection["center"]["longitude"])
        ns = float(selection["half_extent_m"]["north_south"])
        ew = float(selection["half_extent_m"]["east_west"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BuildError("selection must be {center: {latitude, longitude}, half_extent_m: {north_south, east_west}}") from exc
    if not (-90 < lat < 90 and -180 <= lon <= 180):
        raise BuildError(f"the centre ({lat}, {lon}) is not on the Earth")
    if not (MIN_HALF_M <= ns <= MAX_HALF_M and MIN_HALF_M <= ew <= MAX_HALF_M):
        raise BuildError(f"half extents must be {MIN_HALF_M:g}-{MAX_HALF_M:g} m (got {ns} x {ew})")
    return (lat, lon), (ns, ew)


def _check_offline(job: Path, job_id: str, center, half) -> None:
    """An offline build rebuilds what a build of this id fetched before (its
    catalog answers and CityGML, kept in its build folder) for the same area."""
    answers = list((job / "build").glob("plateau-catalog-response-*.json"))
    if not answers:
        raise BuildError(f"オフラインで作り直せるのは、同じ ID で前に作った City World だけです（{job_id} には取得済みの"
                         "データがありません）。チェックを外して作ってください。ダウンロード済みの CityGML は、"
                         "チェックしなくても共有のキャッシュから使い回します。")
    try:
        before = json.loads((job / "build" / "world" / "city-world-receipt.json").read_text(encoding="utf-8"))
        frame = before["coordinate_frame"]
        same = (abs(frame["origin"]["latitude"] - center[0]) < 1e-9 and abs(frame["origin"]["longitude"] - center[1]) < 1e-9
                and abs(frame["half_extent_m"]["north_south"] - half[0]) < 1e-6
                and abs(frame["half_extent_m"]["east_west"] - half[1]) < 1e-6)
    except (OSError, ValueError, KeyError, TypeError):
        same = False
    if not same:
        raise BuildError(f"オフラインで作り直せるのは、{job_id} を前に作ったときと同じ範囲だけです。範囲を変えたときは、"
                         "チェックを外して別の ID で作ってください（ダウンロード済みの CityGML は使い回します）。")


# How far a build is, as the City World Web UI shows it (Business Pack
# tools/remote_operation/city_world/generation.py _forward_build_progress):
# Envsim's [HAKO_PROGRESS] events as a percentage, a phase, and a message.
_BUILD_PHASES = {
    "geometry_extract": (35, "建物形状を抽出しています"),
    "building_collision": (42, "建物の当たり判定を作っています"),
    "terrain": (43, "地形生成を開始しています"),
    "building_mjcf": (52, "建物の当たり判定（物理）を作っています"),
    "building_visual": (56, "建物の見た目を作っています"),
    "building_glb": (72, "建物の見た目（GLB）を書き出しています"),
    "roads": (76, "道路と地形の見た目を作っています"),
    "road_markings": (79, "LOD3路面標示を生成しています"),
    "bridges_visual": (81, "橋の見た目を作っています"),
    "bridges_physics": (83, "橋の当たり判定を作っています"),
    "compose": (86, "City Worldを統合しています"),
    "dataset_validation": (88, "データの内容を検証しています"),
    # After Envsim (build_job below).
    "collider_visualization": (92, "当たり判定の表示用 GLB を作っています"),
    "packaging": (96, "検証・ZIP作成をしています"),
}
FEATURE_NAMES = {"bldg": "建物", "tran": "道路", "dem": "地形（DEM）", "frn": "路面標示", "brid": "橋"}
_SOURCE_ACTIONS = {
    "cache-reused": "共有キャッシュを再利用しました",
    "offline-reused": "ローカルデータを再利用しました",
    "downloaded": "ダウンロードしました",
    "cache-populated": "ダウンロードして共有キャッシュへ保存しました",
}


def progress_step(event: dict) -> tuple[int, str] | None:
    """(percent, message) of one [HAKO_PROGRESS] event, or None."""
    phase = event.get("phase")
    current, total = int(event.get("current", 0) or 0), int(event.get("total", 0) or 0)
    class_id = str(event.get("class_id", "P?"))
    if phase == "source_download":
        action = _SOURCE_ACTIONS.get(event.get("mode"), "取得またはキャッシュ再利用を確認しています")
        feature = FEATURE_NAMES.get(event.get("feature"), event.get("feature", ""))
        return 15, f"PLATEAU の{feature}データ: {action}（{current}/{total}）"
    if phase == "terrain_extract":
        return (43 if total == 0 else 43 + int(3 * current / total)), f"DEMソースを並列抽出しています（{current}/{total}）"
    if phase == "terrain_gap_fill":
        return (47 if total == 0 else 47 + int(current >= total)), f"DEMの小さな欠損を補間しています（{current}/{total}）"
    if phase == "texture_download":
        if total == 0:
            return 70, "選択範囲に建物テクスチャはありません"
        return 56 + int(14 * current / total), f"建物テクスチャを取得・再利用しています（{current}/{total}）"
    if phase == "geometry_extract_files":
        return 35, f"建物GMLを並列抽出しています（{current}/{total}）"
    if phase == "building_glb_batches":
        return 72, f"建物の見た目（GLB）を組み立てています（{current}/{total}）"
    if phase == "building_glb_textures":
        return 72, f"建物のテクスチャを読み込んでいます（{current}/{total}）"
    if phase == "building_glb_export":
        return 72, "建物の見た目（GLB）を書き出しています"
    if phase == "building_physics_surfaces":
        return 52, f"LOD2 の建物の面を当たり判定にしています（GML {current}/{total}）"
    if phase in ("building_physics_exact_reduction", "building_physics_tolerant_reduction"):
        label = "厳密にまとめ" if phase == "building_physics_exact_reduction" else "5 cm 許容でまとめ"
        return 52, f"建物の当たり判定を{label}しています（{class_id} {current}/{total}、{int(event.get('colliders', 0))} 個）"
    if phase == "building_physics_tolerant_groups":
        return 52, f"建物の当たり判定を 5 cm 許容でまとめています（{class_id} 面のまとまり {current}/{total}）"
    if phase == "building_physics_exact_groups":
        return 52, f"建物の当たり判定を厳密にまとめています（{class_id} 平面のまとまり {current}/{total}）"
    if phase == "building_physics_assemble":
        return 52, "建物の当たり判定（MJCF）を組み立てています"
    if phase == "building_physics_write":
        return 52, "建物の当たり判定（MJCF）を書き出しています"
    if phase in _BUILD_PHASES:
        return _BUILD_PHASES[phase]
    return None


def build_progress(lines: list[str]) -> dict:
    """{percent, phase, message} of a build log: the latest step, the
    percentage never going back (as the Web UI keeps it)."""
    progress = {"percent": 10, "phase": None, "message": "PLATEAU catalogとソースを確認しています"}
    for line in lines:
        if not line.startswith("[HAKO_PROGRESS] "):
            continue
        try:
            event = json.loads(line.split(" ", 1)[1])
        except ValueError:
            continue
        step = progress_step(event) if isinstance(event, dict) else None
        if step is not None:
            progress = {"percent": max(step[0], progress["percent"]), "phase": event["phase"], "message": step[1]}
    return progress


SOURCES = ("plateau", "osm")


def osm_citygml(folder: Path, center: tuple[float, float], half: tuple[float, float], body: dict) -> tuple[Path, dict]:
    """The selection's OpenStreetMap buildings and roads (Overpass, or the
    GeoJSON given as body.geojson) as CityGML in `folder`, by hakoniwa-envsim's
    osm2citygml.py, with the map data kept beside it. Returns the folder and
    what the data was."""
    osm = env_envsim.osm2citygml()
    geojson = body.get("geojson") if body.get("map_data") == "geojson" else None
    if body.get("map_data") == "geojson" and not isinstance(geojson, dict):
        raise BuildError("map_data geojson needs the GeoJSON (geojson)")
    box = osm.Box.of(*env_envsim.bounding_box(center, half))
    if geojson is None:
        try:
            data = env_envsim.fetch_overpass(box)
        except osm.OsmConversionError as exc:
            raise BuildError("OpenStreetMap の地図データを取得できません。Overpass API の公開サーバが混んでいます。"
                             f"しばらくしてから、もう一度押してください（{exc}）", 502) from exc
    else:
        data = geojson
    try:
        receipt = osm.run(box, folder, "map", overpass=geojson is None, osm_json=None if geojson else data,
                          geojson=geojson)
    except osm.OsmConversionError as exc:
        raise BuildError(f"OpenStreetMap から作れません: {exc}") from exc
    if not receipt.get("roads"):  # Envsim's City World needs roads (source.kind files: bldg and tran)
        raise BuildError("この範囲には、City World に使える道路がありません（OpenStreetMap の歩道・階段・トンネルは"
                         "道路にしません）。車道が入るように範囲を広げるか、動かしてください。")
    (folder / "map.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder, {"provider": "geojson" if geojson is not None else "openstreetmap",
                    "data_timestamp": receipt.get("data_timestamp"),
                    "buildings": receipt.get("buildings"), "roads": receipt.get("roads"),
                    **{key: receipt[key] for key in ("attribution", "license") if receipt.get(key)}}


EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception): ")
DEM_UNCOVERED = re.compile(r"DemError: height field has (\d+) uncovered samples")


def build_failure(lines: list[str]) -> dict | None:
    """A failure the page can explain, as the City World Web UI does:
    DEM_UNCOVERED (the DEM leaves part of the area uncovered, over water
    for instance, and terrain_uncovered_policy is error)."""
    for line in lines:
        found = DEM_UNCOVERED.search(line)
        if found:
            return {"code": "DEM_UNCOVERED", "uncovered_samples": int(found.group(1))}
    return None


class Builds:
    """The City World builds this Studio started (one runs at a time)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def running(self) -> str | None:
        for job_id, job in self.jobs.items():
            if job["process"].poll() is None:
                return job_id
        return None

    def start(self, body: object, roots: list[Path]) -> dict:
        if not isinstance(body, dict):
            raise BuildError("the request body must be {id, selection, options?, offline?, overwrite?}")
        job_id = str(body.get("id") or "")
        if not env_rules.ID_PATTERN.match(job_id):
            raise BuildError(f"{job_id!r} is not an id (lower case letters, digits, - and _)")
        center, half = _selection(body)
        options = build_options(body.get("options"))
        source = body.get("source") or "plateau"
        if source not in SOURCES:
            raise BuildError(f"source must be one of {', '.join(SOURCES)} (got {source!r})")
        if source == "osm" and body.get("offline"):
            raise BuildError("an OpenStreetMap City World is not rebuilt offline (generate it again)")
        with self.lock:
            other = self.running()
            if other is not None:
                raise BuildError(f"City World {other} を作っているところです（終わってから始めてください）", 409)
            job = WORK / job_id
            built = (job / "build" / "world" / "city-world-receipt.json").is_file()
            if body.get("offline"):
                _check_offline(job, job_id, center, half)
            elif built and not body.get("overwrite"):  # a build that failed half way may simply run again
                raise BuildError(f"City World {job_id} はもうあります（別の ID にしてください）", 409)
            envsim = env_envsim.root()
            if built and body.get("overwrite") and not body.get("offline"):
                # Generated again (the map page's id is the place): the old one goes, as in the Web UI.
                shutil.rmtree(job)
            job.mkdir(parents=True, exist_ok=True)
            cache = plateau_cache(roots)
            files = map_data = None
            if source == "osm":
                try:
                    files, map_data = osm_citygml(job / "osm", center, half, body)
                except BuildError:
                    shutil.rmtree(job, ignore_errors=True)  # nothing of it is left half made
                    raise
                options["terrain_uncovered_policy"] = "constant"
            config = job / "hakoniwa-envsim-build.yaml"
            config.write_text(build_config(job, center, half, cache, options, files), encoding="utf-8")
            (job / "job.json").write_text(json.dumps({
                "schema_version": 1, "job_id": job_id, "name": body.get("name") or job_id,
                "request": {"selection": body["selection"], "source": source,
                            "profile": "visual-physics-v1" if source == "plateau" else "osm-flat-ground",
                            "options": options, "offline": bool(body.get("offline")),
                            **({"map_data": map_data} if map_data else {})},
                "created_by": "hakoniwa-environment-studio",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log = job / "generation.log"
            # This module runs Envsim's build, then the viewer files (build_job below).
            command = [sys.executable, str(Path(__file__).resolve()), "build-job", str(job), "--envsim", str(envsim),
                       *(["--offline"] if body.get("offline") else [])]
            with log.open("w", encoding="utf-8") as stream:
                stream.write(f"$ {' '.join(command)}\n")
                stream.flush()
                process = subprocess.Popen(command, cwd=envsim, stdout=stream, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True,
                                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
            self.jobs[job_id] = {"process": process, "log": log, "job": job, "started": time.time(),
                                 "cache": str(cache)}
        return self.status(job_id)

    def status(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        folder = WORK / job_id
        log = job["log"] if job else folder / "generation.log"
        if job is None and not log.is_file():
            raise BuildError(f"City World {job_id} is not being built", 404)
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
        progress = build_progress(lines)
        code = job["process"].poll() if job else None
        built = (folder / "build" / "download-manifest.json").is_file() and (
            folder / "build" / "world" / "city-world-receipt.json").is_file()
        if job is not None and code is None:
            state = "running"
        elif code == 0 or (job is None and built):
            state = "done" if built else "failed"
        else:
            state = "canceled" if job is not None and job.get("canceled") else "failed"
        # What went wrong: Envsim's ERROR lines and the exceptions' own lines (not the traceback's header).
        errors = [line for line in lines if line.startswith("ERROR") or EXCEPTION_LINE.match(line)][-5:]
        if state == "done":
            progress = {"percent": 100, "phase": "ready", "message": "City Worldができました"}
        return {"id": job_id, "state": state, "returncode": code, "progress": progress,
                "elapsed_s": round(time.time() - job["started"], 1) if job else None,
                "build": str(folder / "build") if built else None, "errors": errors,
                "failure": build_failure(lines) if state == "failed" else None,
                "log": str(log), "log_tail": lines[-LOG_TAIL_LINES:], "cache": job["cache"] if job else None}

    def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise BuildError(f"City World {job_id} is not being built", 404)
        process = job["process"]
        if process.poll() is None:
            job["canceled"] = True
            # The build runs in its own session (Envsim's hako.py and its workers under it): stop them all.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
        return self.status(job_id)


BUILDS = Builds()


def collider_view(job: Path, mjcf: Path, envsim: Path | None = None) -> None:
    """viewer/city-world-colliders.glb (and its receipt) of a job's World MJCF,
    by Envsim's mjcf_colliders2glb.py, as the City World Web UI makes it."""
    envsim = envsim or env_envsim.root()
    viewer = job / "viewer"
    viewer.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(envsim / "src" / "city_pipeline" / "mjcf_colliders2glb.py"),
                    "--in", str(mjcf), "--out", str(viewer / "city-world-colliders.glb"),
                    "--receipt", str(viewer / "city-world-colliders-receipt.json")],
                   cwd=envsim, check=True, stdin=subprocess.DEVNULL)


def build_job(job: Path, offline: bool = False, envsim: Path | None = None) -> int:
    """Run Envsim's build of a job (its hakoniwa-envsim-build.yaml), then its
    viewer files and its ZIP."""
    envsim = envsim or env_envsim.root()
    command = [sys.executable, str(envsim / "tools" / "hako.py"), "--config",
               str(job / "hakoniwa-envsim-build.yaml"), *(["--offline"] if offline else []), "build"]
    print(f"$ {' '.join(command)}", flush=True)
    code = subprocess.call(command, cwd=envsim, stdin=subprocess.DEVNULL)
    if code != 0:
        return code
    world = job / "build" / "world"
    print("[HAKO_PROGRESS] " + json.dumps({"phase": "collider_visualization", "current": 0, "total": 1}), flush=True)
    (job / "viewer").mkdir(parents=True, exist_ok=True)
    shutil.copy2(world / "city-world.glb", job / "viewer" / "city-world.glb")
    try:
        collider_view(job, world / "city-world.xml", envsim)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: the collider view could not be made: {exc}", flush=True)
        return 1
    print("[HAKO_PROGRESS] " + json.dumps({"phase": "packaging"}), flush=True)
    try:
        package(job)
    except (OSError, BuildError) as exc:
        print(f"ERROR: the ZIP could not be made: {exc}", flush=True)
        return 1
    print(f"OK: viewer files: {job / 'viewer'}, ZIP: {artifact_path(job)}", flush=True)
    return 0


# The ZIP of a City World, with the City World Web UI's fixed entries.
ZIP_ENTRIES = {
    "visual/city-world.glb": "city-world.glb",
    "physics/city-world.xml": "city-world.xml",
    "validation/dataset-validation.json": "dataset-validation.json",
    "receipt/city-world-receipt.json": "city-world-receipt.json",
}


def artifact_path(job: Path) -> Path:
    return job / "artifacts" / f"city-world-{job.name}.zip"


def package(job: Path) -> Path:
    """artifacts/city-world-<id>.zip of a built job (written aside, then moved in)."""
    import zipfile

    world = job / "build" / "world"
    missing = [name for name in ZIP_ENTRIES.values() if not (world / name).is_file()]
    if missing:
        raise BuildError(f"City World {job.name} has no {', '.join(missing)}", 404)
    target = artifact_path(job)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry, name in ZIP_ENTRIES.items():
            archive.write(world / name, entry,
                          compress_type=zipfile.ZIP_STORED if name.endswith(".glb") else zipfile.ZIP_DEFLATED)
    partial.replace(target)
    return target


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def collider_counts(job: Path) -> dict | None:
    """The colliders of a build, as the Web UI's result shows them: the total
    and per component (the World receipt), and the buildings' per physics class
    and geom type (Envsim's building-physics-application.json)."""
    counts = _read_json(job / "build" / "world" / "city-world-receipt.json").get("components", {}).get("mjcf_geom_counts")
    if not isinstance(counts, dict):
        return None
    physics = _read_json(job / "build" / "components" / "buildings" / "building-physics-application.json")
    by_class = physics.get("collider_geom_counts", {}).get("by_class", {})
    by_type = physics.get("collider_geom_types", {}).get("by_class", {})
    classes = ("P0", "P1", "P2", "P3")
    return {
        "total": int(counts.get("total", 0)),
        "by_component": {str(key): int(value) for key, value in counts.items() if key != "total"},
        "by_physics_class": {name: int(by_class.get(name, 0)) for name in classes} if physics else None,
        "building_by_geom_type": {kind: sum(int(by_type.get(name, {}).get(kind, 0)) for name in classes)
                                  for kind in ("box", "mesh")} if physics else None,
    }


def _size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def size_text(selection: dict | None) -> str:
    """"300 m 四方" or "400 × 300 m" of a selection (as the map page writes it)."""
    try:
        ew, ns = (float(selection["half_extent_m"][key]) * 2 for key in ("east_west", "north_south"))
    except (KeyError, TypeError, ValueError):
        return ""
    return f"{round(ew)} m 四方" if abs(ew - ns) < 1e-6 else f"{round(ew)} × {round(ns)} m"


def display_title(job: Path, record: dict) -> str:
    """A City World's name for people: the one it was generated with, else
    (one generated before names) its municipalities from the download record,
    or OpenStreetMap and its centre, with its size."""
    name = record.get("name")
    if name and name != job.name:
        return str(name)
    request = record.get("request", {})
    selection = request.get("selection")
    size = size_text(selection)
    if request.get("source") == "osm":
        try:
            centre = selection["center"]
            place = f"OpenStreetMap（{float(centre['latitude']):.3f}, {float(centre['longitude']):.3f}）"
        except (KeyError, TypeError, ValueError):
            place = "OpenStreetMap"
    else:
        files = [item for item in _read_json(job / "build" / "download-manifest.json").get("files", [])
                 if isinstance(item, dict) and item.get("city_name")]
        # Where its buildings are (terrain and road files reach beyond the area).
        buildings = [item for item in files if item.get("feature_type") == "bldg"] or files
        cities = list(dict.fromkeys(str(item["city_name"]) for item in buildings))
        place = "・".join(cities) or job.name
    return f"{place} 付近（{size}）" if size else place


def list_jobs() -> list[dict]:
    """The City Worlds this Studio built (city-worlds/<id>/ with a World and its
    viewer files), newest first, with what the Web UI's result list shows."""
    jobs = []
    for receipt in WORK.glob("*/build/world/city-world-receipt.json"):
        job = receipt.parents[2]
        visual, colliders = job / "viewer" / "city-world.glb", job / "viewer" / "city-world-colliders.glb"
        if not visual.is_file() or BUILDS.running() == job.name:
            continue
        record = _read_json(job / "job.json")
        request = record.get("request", {})
        options = build_options(request.get("options")) if isinstance(request.get("options"), dict) else {}
        artifact = artifact_path(job)
        jobs.append({
            "job_id": job.name, "title": display_title(job, record), "path": str(job), "build": str(job / "build"),
            "source": request.get("source") or "plateau",
            "selection": request.get("selection"),
            "building_physics_level": options.get("building_physics_level"),
            "building_collider_reduction": options.get("building_collider_reduction"),
            "terrain_uncovered_policy": options.get("terrain_uncovered_policy"),
            "colliders": collider_counts(job),
            "collider_available": colliders.is_file(),
            "visual_size_bytes": _size(visual), "collider_size_bytes": _size(colliders) or None,
            "size_bytes": _size(artifact) or sum(_size(item) for item in (job / "build" / "world").iterdir()),
            "artifact_available": artifact.is_file(),
            "updated_at_msec": int(receipt.stat().st_mtime * 1000),
        })
    return sorted(jobs, key=lambda item: (-item["updated_at_msec"], item["job_id"]))


def cache_summary(cache: Path) -> dict:
    """The shared PLATEAU cache: where, and how many files of what size."""
    objects = [item for item in (cache / "objects").glob("*/*")
               if item.is_file() and not item.name.endswith(".cache.json")]
    return {"path": str(cache), "object_count": len(objects), "size_bytes": sum(item.stat().st_size for item in objects)}


def job_file(job_id: str, name: str) -> Path:
    """A file of a built City World for the page: its viewer GLBs or its ZIP
    (made now when a build before the ZIP existed has none)."""
    job = WORK / job_id
    if not (job / "build" / "world" / "city-world-receipt.json").is_file():
        raise BuildError(f"City World {job_id} はありません", 404)
    if name == "artifact.zip":
        path = artifact_path(job)
        return path if path.is_file() else package(job)
    if name not in ("city-world.glb", "city-world-colliders.glb"):
        raise BuildError(f"no file {name}", 404)
    path = job / "viewer" / name
    if not path.is_file():
        raise BuildError(f"City World {job_id} has no {name}", 404)
    return path


def delete_job(job_id: str) -> dict:
    """Delete a built City World (its folder: ZIP, GLB, MJCF, intermediate
    files). The shared CityGML cache stays."""
    job = WORK / job_id
    if not (job / "job.json").is_file():
        raise BuildError(f"City World {job_id} はありません", 404)
    if BUILDS.running() == job_id:
        raise BuildError(f"City World {job_id} を作っているところです（中止してから削除してください）", 409)
    shutil.rmtree(job)
    BUILDS.jobs.pop(job_id, None)
    return {"deleted": job_id, "path": str(job)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build-job", help="run Envsim's build of a job folder, then its viewer files")
    command.add_argument("job", type=Path)
    command.add_argument("--offline", action="store_true")
    command.add_argument("--envsim", type=Path, help="the hakoniwa-envsim checkout (default: the Studio's)")
    args = parser.parse_args(argv)
    return build_job(args.job.resolve(), offline=args.offline, envsim=args.envsim.resolve() if args.envsim else None)


if __name__ == "__main__":
    raise SystemExit(main())
