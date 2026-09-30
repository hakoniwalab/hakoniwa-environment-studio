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
(build / "world" / "city-world-receipt.json").write_text("{}")
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
        self.assertEqual(status["progress"], {"phase": "collider_visualization", "current": 1, "total": 1})
        self.assertEqual(status["build"], str(self.dir / "work/city-worlds/sapporo/build"))
        # The viewer files the City World job contract asks for.
        viewer = self.dir / "work/city-worlds/sapporo/viewer"
        self.assertEqual((viewer / "city-world-colliders.glb").read_bytes(), b"glTF-colliders")
        self.assertEqual((viewer / "city-world.glb").read_bytes(), b"glTF")
        # Done, it is there to import; building it again needs overwrite.
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "sapporo", "selection": SELECTION}, roots)
        self.assertEqual(caught.exception.status, 409)

    def test_one_build_at_a_time_and_it_can_be_cancelled(self):
        slow = {**SELECTION, "half_extent_m": {"north_south": 900, "east_west": 20}}  # 9 s a step
        self.builds.start({"id": "slow", "selection": slow}, [])
        with self.assertRaises(env_cityworld.BuildError) as caught:
            self.builds.start({"id": "other", "selection": SELECTION}, [])
        self.assertEqual(caught.exception.status, 409)
        status = self.builds.cancel("slow")
        self.assertEqual((status["state"], status["build"]), ("failed", None))

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
