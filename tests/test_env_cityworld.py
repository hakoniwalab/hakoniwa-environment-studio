"""Building a PLATEAU City World from the Studio (tools/env_cityworld.py):
the build configuration, the background run and its progress, one build at
a time, cancelling. A stand-in for Envsim's tools/hako.py is used (no network)."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_cityworld  # noqa: E402

FAKE_HAKO = '''
import json, re, sys, time
from pathlib import Path
config = Path(sys.argv[sys.argv.index("--config") + 1]).read_text()
build = Path(re.search(r"build_dir: (.+)", config).group(1).strip())
delay = float(re.search(r"north_south: ([0-9.]+)", config).group(1)) / 100  # 10 m: 0.1 s
for step in range(1, 4):
    print("[HAKO_PROGRESS] " + json.dumps({"phase": "source_download", "feature": "bldg", "current": step, "total": 3}))
    sys.stdout.flush()
    time.sleep(delay)
if "fail" in str(build):
    print("ERROR: no cached catalog response")
    sys.exit(2)
(build / "world").mkdir(parents=True, exist_ok=True)
(build / "download-manifest.json").write_text("{}")
(build / "world" / "city-world-receipt.json").write_text(json.dumps({"components": {"mjcf_geom_counts": {"total": 7, "buildings": 5, "terrain": 2}}}))
(build / "world" / "dataset-validation.json").write_text("{}")
(build / "components" / "buildings").mkdir(parents=True, exist_ok=True)
(build / "components" / "buildings" / "building-physics-application.json").write_text(json.dumps({
    "collider_geom_counts": {"by_class": {"P3": 4, "P0": 1}},
    "collider_geom_types": {"by_class": {"P3": {"box": 3, "mesh": 1}, "P0": {"box": 1}}}}))
(build / "world" / "city-world.xml").write_text("<mujoco/>")
(build / "world" / "city-world.glb").write_bytes(b"glTF")
print("OK: built")
'''

FAKE_COLLIDERS = '''
import sys
from pathlib import Path
args = sys.argv
Path(args[args.index("--out") + 1]).write_bytes(b"glTF-colliders")
Path(args[args.index("--receipt") + 1]).write_text('{"source_mjcf": "%s"}' % args[args.index("--in") + 1])
'''

SELECTION = {"center": {"latitude": 43.06738, "longitude": 141.350709}, "half_extent_m": {"north_south": 10, "east_west": 20}}


class CityWorldBuildTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        envsim = self.dir / "hakoniwa-envsim"
        (envsim / "tools").mkdir(parents=True)
        (envsim / "tools/hako.py").write_text(FAKE_HAKO, encoding="utf-8")
        (envsim / "src/city_pipeline").mkdir(parents=True)
        (envsim / "src/city_pipeline/mjcf_colliders2glb.py").write_text(FAKE_COLLIDERS, encoding="utf-8")
        for patch in (mock.patch.object(env_cityworld, "WORK", self.dir / "work/city-worlds"),
                      mock.patch.object(env_cityworld.env_envsim, "root", lambda: envsim),
                      mock.patch.dict("os.environ", {"HAKONIWA_PLATEAU_CACHE": ""})):
            patch.start()
            self.addCleanup(patch.stop)
        self.builds = env_cityworld.Builds()
        self.addCleanup(lambda: [job["process"].kill() for job in self.builds.jobs.values()
                                 if job["process"].poll() is None])

    def wait(self, job_id, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            status = self.builds.status(job_id)
            if status["state"] != "running":
                return status
            time.sleep(0.05)
        self.fail("the build did not finish")

    def test_a_selection_is_built_in_the_background_and_reports_progress(self):
        roots = [self.dir / "business-pack/work"]
        cache = roots[0] / env_cityworld.BUSINESS_PACK_CACHE
        cache.mkdir(parents=True)
        started = self.builds.start({"id": "sapporo", "selection": SELECTION}, roots)
        self.assertEqual(started["state"], "running")
        config = (self.dir / "work/city-worlds/sapporo/hakoniwa-envsim-build.yaml").read_text(encoding="utf-8")
        self.assertIn("latitude: 43.067380000", config)
        self.assertIn("north_south: 10.0", config)
        self.assertIn(f"cache_dir: {cache.resolve().as_posix()}", config)  # the business pack's shared cache
        self.assertIn("building_physics_level: 3", config)
        job = json.loads((self.dir / "work/city-worlds/sapporo/job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["job_id"], "sapporo")
        status = self.wait("sapporo")
        self.assertEqual((status["state"], status["returncode"]), ("done", 0))
        self.assertEqual(status["progress"]["percent"], 100)
        self.assertEqual(status["build"], str(self.dir / "work/city-worlds/sapporo/build"))
        # The viewer files the City World job contract asks for.
        viewer = self.dir / "work/city-worlds/sapporo/viewer"
        self.assertEqual((viewer / "city-world-colliders.glb").read_bytes(), b"glTF-colliders")
        self.assertEqual((viewer / "city-world.glb").read_bytes(), b"glTF")
        # And its ZIP, with the City World Web UI's entries.
        import zipfile
        with zipfile.ZipFile(self.dir / "work/city-worlds/sapporo/artifacts/city-world-sapporo.zip") as archive:
            self.assertEqual(sorted(archive.namelist()), sorted(env_cityworld.ZIP_ENTRIES))
        # Done, it is there to import; building it again needs overwrite.
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "sapporo", "selection": SELECTION}, roots)
        self.assertEqual(caught.exception.status, 409)
        # Generated again with overwrite: the old one goes first.
        (self.dir / "work/city-worlds/sapporo/build/stale.txt").write_text("old", encoding="utf-8")
        self.assertEqual(self.wait(self.builds.start({"id": "sapporo", "selection": SELECTION, "overwrite": True},
                                                     roots)["id"])["state"], "done")
        self.assertFalse((self.dir / "work/city-worlds/sapporo/build/stale.txt").exists())

    def test_one_build_at_a_time_and_it_can_be_cancelled(self):
        slow = {**SELECTION, "half_extent_m": {"north_south": 900, "east_west": 20}}  # 9 s a step
        self.builds.start({"id": "slow", "selection": slow}, [])
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "other", "selection": SELECTION}, [])
        self.assertEqual(caught.exception.status, 409)
        status = self.builds.cancel("slow")
        self.assertEqual((status["state"], status["build"]), ("canceled", None))

    def test_a_failed_build_says_why(self):
        self.builds.start({"id": "fail", "selection": SELECTION}, [])
        status = self.wait("fail")
        self.assertEqual((status["state"], status["returncode"]), ("failed", 2))
        self.assertEqual(status["errors"], ["ERROR: no cached catalog response"])
        self.assertIn("build-job", status["log_tail"][0])
        self.assertIn("hako.py", status["log_tail"][1])

    def test_offline_only_rebuilds_the_same_area_and_a_failed_build_runs_again(self):
        # Nothing fetched yet: refused before anything is written.
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "shibuya", "selection": SELECTION, "offline": True}, [])
        self.assertIn("チェックを外して", str(caught.exception))
        self.assertFalse((self.dir / "work/city-worlds/shibuya/hakoniwa-envsim-build.yaml").exists())
        # A build (its catalog answers and receipt), then offline with a wider area: refused, config kept.
        self.wait(self.builds.start({"id": "shibuya", "selection": SELECTION}, [])["id"])
        build = self.dir / "work/city-worlds/shibuya/build"
        (build / "plateau-catalog-response-bldg.json").write_text("{}", encoding="utf-8")
        (build / "world/city-world-receipt.json").write_text(json.dumps({"coordinate_frame": {
            "origin": SELECTION["center"], "half_extent_m": {"north_south": 10.0, "east_west": 20.0}}}), encoding="utf-8")
        config = (self.dir / "work/city-worlds/shibuya/hakoniwa-envsim-build.yaml").read_text(encoding="utf-8")
        wider = {**SELECTION, "half_extent_m": {"north_south": 200, "east_west": 200}}
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "shibuya", "selection": wider, "offline": True}, [])
        self.assertIn("同じ範囲", str(caught.exception))
        self.assertEqual((self.dir / "work/city-worlds/shibuya/hakoniwa-envsim-build.yaml").read_text(encoding="utf-8"), config)
        # The same area offline is fine.
        self.assertEqual(self.wait(self.builds.start({"id": "shibuya", "selection": SELECTION, "offline": True}, [])["id"])["state"], "done")
        # A build that stopped half way (no receipt) runs again under its id.
        (self.dir / "work/city-worlds/half/build/source").mkdir(parents=True)
        self.assertEqual(self.builds.start({"id": "half", "selection": SELECTION}, [])["state"], "running")
        self.wait("half")

    def test_the_results_are_listed_served_and_deleted(self):
        with mock.patch.object(env_cityworld, "BUILDS", self.builds):
            self.wait(self.builds.start({"id": "sapporo", "selection": SELECTION, "options": {
                "building_physics_level": 2, "building_collider_reduction": "coplanar-union"}}, [])["id"])
            (job,) = env_cityworld.list_jobs()
            self.assertEqual((job["job_id"], job["selection"], job["building_physics_level"],
                              job["building_collider_reduction"], job["collider_available"]),
                             ("sapporo", SELECTION, 2, "coplanar-union", True))
            # The colliders as the Web UI shows them.
            self.assertEqual(job["colliders"], {
                "total": 7, "by_component": {"buildings": 5, "terrain": 2},
                "by_physics_class": {"P0": 1, "P1": 0, "P2": 0, "P3": 4},
                "building_by_geom_type": {"box": 4, "mesh": 1}})
            self.assertEqual(env_cityworld.job_file("sapporo", "city-world-colliders.glb").read_bytes(), b"glTF-colliders")
            # A build from before the ZIP gets one when it is asked for.
            env_cityworld.artifact_path(self.dir / "work/city-worlds/sapporo").unlink()
            self.assertTrue(env_cityworld.job_file("sapporo", "artifact.zip").is_file())
            with self.assertRaises(env_cityworld.BuildError):
                env_cityworld.job_file("sapporo", "../job.json")
            env_cityworld.delete_job("sapporo")
            self.assertFalse((self.dir / "work/city-worlds/sapporo").exists())
            self.assertEqual(env_cityworld.list_jobs(), [])

    def test_a_dem_that_leaves_part_uncovered_is_told_apart(self):
        lines = ["Traceback (most recent call last):", '  File "dem2hfield.py", line 357',
                 "DemError: height field has 132 uncovered samples; first MuJoCo (X,Y) coordinates=[(88.0, -150.0)]",
                 "ERROR: Command 'dem2hfield.py' returned non-zero exit status 1."]
        self.assertEqual(env_cityworld.build_failure(lines), {"code": "DEM_UNCOVERED", "uncovered_samples": 132})
        self.assertIsNone(env_cityworld.build_failure(["ERROR: no cached catalog response"]))
        self.assertEqual([line for line in lines if env_cityworld.EXCEPTION_LINE.match(line)], [lines[2]])

    def test_a_city_world_generated_before_names_gets_one(self):
        job = self.dir / "work/city-worlds/tokyo-x"
        (job / "build").mkdir(parents=True)
        (job / "build/download-manifest.json").write_text(json.dumps({"files": [
            {"feature_type": "dem", "city_name": "港区"}, {"feature_type": "bldg", "city_name": "中央区"},
            {"feature_type": "bldg", "city_name": "中央区"}, {"feature_type": "tran", "city_name": "江東区"}]}), encoding="utf-8")
        record = {"name": "tokyo-x", "request": {"selection": SELECTION}}
        # The municipalities of its buildings (terrain and roads reach further), and its size.
        self.assertEqual(env_cityworld.display_title(job, record), "中央区 付近（40 × 20 m）")
        osm = {"name": "osm-x", "request": {"source": "osm", "selection": SELECTION}}
        self.assertEqual(env_cityworld.display_title(job.parent / "osm-x", osm), "OpenStreetMap（43.067, 141.351） 付近（40 × 20 m）")
        self.assertEqual(env_cityworld.display_title(job, {"name": "札幌駅前"}), "札幌駅前")

    def test_progress_is_a_percentage_that_does_not_go_back(self):
        lines = ["[HAKO_PROGRESS] " + json.dumps(event) for event in (
            {"phase": "building_glb"}, {"phase": "source_download", "feature": "bldg", "current": 1, "total": 2,
                                        "mode": "cache-reused"})]
        progress = env_cityworld.build_progress(lines)
        self.assertEqual((progress["percent"], progress["phase"]), (72, "source_download"))
        self.assertIn("共有キャッシュを再利用しました（1/2）", progress["message"])
        self.assertEqual(env_cityworld.build_progress([])["percent"], 10)

    def test_an_openstreetmap_city_world_is_built_from_its_map_data_on_flat_ground(self):
        class FakeOsm:
            class OsmConversionError(Exception):
                pass

            class Box:
                @staticmethod
                def of(south, west, north, east):
                    return (south, west, north, east)

            fetched = []

            @classmethod
            def fetch_overpass(cls, box, endpoint=None):
                cls.fetched.append(endpoint)
                if endpoint == env_cityworld.env_envsim.OVERPASS_ENDPOINTS[0]:  # busy: the next one is asked
                    raise cls.OsmConversionError("HTTP Error 504: Gateway Timeout")
                return {"elements": [], "osm3s": {"timestamp_osm_base": "2026-10-01T00:00:00Z"}}

            @staticmethod
            def run(box, out_dir, name, overpass=False, osm_json=None, geojson=None):
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{name}_bldg_op.gml").write_text("<gml/>", encoding="utf-8")
                return {"buildings": 3, "roads": 2, "data_timestamp": "2026-10-01T00:00:00Z",
                        "attribution": "© OpenStreetMap contributors", "license": "ODbL-1.0"}

        with mock.patch.object(env_cityworld.env_envsim, "osm2citygml", lambda: FakeOsm), \
                mock.patch.object(env_cityworld.env_envsim, "bounding_box", lambda center, half: (0, 0, 1, 1)), \
                mock.patch.dict("os.environ", {"HAKONIWA_OVERPASS_URL": ""}):
            status = self.wait(self.builds.start({"id": "osm-block", "selection": SELECTION, "source": "osm",
                                                  "map_data": "overpass"}, [])["id"])
        self.assertEqual(status["state"], "done")
        job = self.dir / "work/city-worlds/osm-block"
        config = (job / "hakoniwa-envsim-build.yaml").read_text(encoding="utf-8")
        # Envsim reads the converted files (no PLATEAU), without DEM: the ground is flat at 0 m.
        self.assertIn(f"kind: files\n  path: {(job / 'osm').as_posix()}", config)
        self.assertIn("dem: false", config)
        self.assertIn("terrain_uncovered_policy: constant", config)
        self.assertNotIn("api_base_url", config)
        self.assertEqual(FakeOsm.fetched, list(env_cityworld.env_envsim.OVERPASS_ENDPOINTS[:2]))
        self.assertTrue((job / "osm/map.json").is_file())
        request = json.loads((job / "job.json").read_text(encoding="utf-8"))["request"]
        self.assertEqual((request["source"], request["map_data"]["license"], request["map_data"]["buildings"]),
                         ("osm", "ODbL-1.0", 3))
        with mock.patch.object(env_cityworld, "BUILDS", self.builds):
            self.assertEqual([item["source"] for item in env_cityworld.list_jobs()], ["osm"])
        # Every instance busy: the Studio says so, and nothing is left.
        busy = type("Busy", (FakeOsm,), {"fetch_overpass": classmethod(
            lambda cls, box, endpoint=None: (_ for _ in ()).throw(cls.OsmConversionError("HTTP Error 504")))})
        with mock.patch.object(env_cityworld.env_envsim, "osm2citygml", lambda: busy), \
                mock.patch.object(env_cityworld.env_envsim, "bounding_box", lambda center, half: (0, 0, 1, 1)), \
                self.assertRaisesRegex(env_cityworld.BuildError, "混んでいます") as caught:
            self.builds.start({"id": "busy", "selection": SELECTION, "source": "osm", "map_data": "overpass"}, [])
        self.assertEqual(caught.exception.status, 502)
        self.assertFalse((self.dir / "work/city-worlds/busy").exists())
        # An area without roads is refused before anything is built (Envsim's City World needs them).
        FakeOsm.run = staticmethod(lambda box, out_dir, name, **kw: (out_dir.mkdir(parents=True), {"buildings": 3, "roads": 0})[1])
        with mock.patch.object(env_cityworld.env_envsim, "osm2citygml", lambda: FakeOsm), \
                mock.patch.object(env_cityworld.env_envsim, "bounding_box", lambda center, half: (0, 0, 1, 1)), \
                self.assertRaisesRegex(env_cityworld.BuildError, "道路がありません"):
            self.builds.start({"id": "no-roads", "selection": SELECTION, "source": "osm", "map_data": "overpass"}, [])
        self.assertFalse((self.dir / "work/city-worlds/no-roads").exists())
        # Not rebuilt offline; an unknown source is refused.
        for body in ({"id": "x", "selection": SELECTION, "source": "osm", "offline": True},
                     {"id": "x", "selection": SELECTION, "source": "mars"}):
            with self.assertRaises(env_cityworld.BuildError):
                self.builds.start(body, [])

    def test_bad_requests_are_refused(self):
        for body, words in (({"id": "Bad Id", "selection": SELECTION}, "not an id"),
                            ({"id": "x", "selection": {"center": {}}}, "selection must be"),
                            ({"id": "x", "selection": {**SELECTION, "half_extent_m": {"north_south": 5, "east_west": 20}}},
                             "half extents")):
            with self.subTest(body=body), self.assertRaises(env_cityworld.BuildError) as caught:
                self.builds.start(body, [])
            self.assertIn(words, str(caught.exception))
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.status("nothing")
        self.assertEqual(caught.exception.status, 404)

    def test_without_a_shared_cache_it_keeps_its_own(self):
        self.assertEqual(env_cityworld.plateau_cache([self.dir / "nowhere"]), (self.dir / "work/city-worlds/cache").resolve())
        with mock.patch.dict("os.environ", {"HAKONIWA_PLATEAU_CACHE": str(self.dir / "mine")}):
            self.assertEqual(env_cityworld.plateau_cache([]), (self.dir / "mine").resolve())


if __name__ == "__main__":
    unittest.main()
