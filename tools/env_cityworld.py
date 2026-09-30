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


def build_config(job: Path, center: tuple[float, float], half: tuple[float, float], cache: Path) -> str:
    """The Envsim build of a selection, as the business pack's City World
    Web UI makes it (profile visual-physics-v1)."""
    lat, lon = center
    ns, ew = half
    return f"""version: 1
component: hakoniwa-envsim

pipeline:
  type: plateau-citygml-to-assets

source:
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
  building_physics_level: 3
  building_collider_reduction: convex-decompose

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
  terrain_uncovered_policy: error
  terrain_uncovered_elevation_m: 0
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
            raise BuildError("the request body must be {id, selection, offline?, overwrite?}")
        job_id = str(body.get("id") or "")
        if not env_rules.ID_PATTERN.match(job_id):
            raise BuildError(f"{job_id!r} is not an id (lower case letters, digits, - and _)")
        center, half = _selection(body)
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
            job.mkdir(parents=True, exist_ok=True)
            cache = plateau_cache(roots)
            config = job / "hakoniwa-envsim-build.yaml"
            config.write_text(build_config(job, center, half, cache), encoding="utf-8")
            (job / "job.json").write_text(json.dumps({
                "schema_version": 1, "job_id": job_id, "name": body.get("name") or job_id,
                "request": {"selection": body["selection"], "profile": "visual-physics-v1",
                            "offline": bool(body.get("offline"))},
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
        progress = None
        for line in reversed(lines):
            if line.startswith("[HAKO_PROGRESS]"):
                try:
                    progress = json.loads(line.split(" ", 1)[1])
                except (IndexError, ValueError):
                    progress = None
                break
        code = job["process"].poll() if job else None
        built = (folder / "build" / "download-manifest.json").is_file() and (
            folder / "build" / "world" / "city-world-receipt.json").is_file()
        if job is not None and code is None:
            state = "running"
        elif code == 0 or (job is None and built):
            state = "done" if built else "failed"
        else:
            state = "failed"
        errors = [line for line in lines if line.startswith("ERROR") or "Traceback" in line][-5:]
        return {"id": job_id, "state": state, "returncode": code, "progress": progress,
                "elapsed_s": round(time.time() - job["started"], 1) if job else None,
                "build": str(folder / "build") if built else None, "errors": errors,
                "log": str(log), "log_tail": lines[-LOG_TAIL_LINES:], "cache": job["cache"] if job else None}

    def cancel(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise BuildError(f"City World {job_id} is not being built", 404)
        if job["process"].poll() is None:
            job["process"].terminate()
            try:
                job["process"].wait(timeout=10)
            except subprocess.TimeoutExpired:
                job["process"].kill()
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
    """Run Envsim's build of a job (its hakoniwa-envsim-build.yaml), then its viewer files."""
    import shutil

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
    print("[HAKO_PROGRESS] " + json.dumps({"phase": "collider_visualization", "current": 1, "total": 1}), flush=True)
    print(f"OK: viewer files: {job / 'viewer'}", flush=True)
    return 0


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
