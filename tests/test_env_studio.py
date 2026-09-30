import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import env_studio  # noqa: E402
import env_validate  # noqa: E402

SIZE = {"east": 20, "north": 30}
GROUND = {"item": "grass-ground"}


def cone(id_, x, y):
    return {"id": id_, "item": "traffic-cone", "pose": {"x_m": x, "y_m": y, "yaw_deg": 0}}


class StudioServerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.user = Path(directory.name) / "work/recipes"
        patch = mock.patch.object(env_studio, "USER_RECIPES", self.user)
        patch.start()
        self.addCleanup(patch.stop)
        self.server = env_studio.make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_the_page_is_served_without_caching(self):
        with urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            self.assertIn(b"Environment Studio", response.read())
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_the_catalog_sends_items_resolved_for_the_browser(self):
        status, catalog = self.call("GET", "/api/catalogs/starter")
        self.assertEqual(status, 200)
        items = {item["id"]: item for item in catalog["items"]}
        gate = items["race-gate"]
        # The browser gets the resolved shape (outline and height range per solid), not the type.
        self.assertEqual((gate["kind"], gate["envelope"]["width_m"], gate["height_m"]), ("object", 1.7, 2.1))
        self.assertTrue(all("outline" in solid and "z_range_m" in solid for solid in gate["solids"]))
        self.assertIn("opening_width_m", [param["name"] for param in gate["placement_params"]])
        self.assertEqual(items["rolling-hills"]["kind"], "terrain")
        self.assertEqual(catalog["rules"], {"circle_segments": 32, "tolerance_m": env_validate.TOLERANCE_M})
        self.assertEqual(self.call("GET", "/api/catalogs/no-such")[0], 404)

    def test_the_studio_resolves_a_placement_that_changes_the_shape(self):
        status, shape = self.call("POST", "/api/resolve", {
            "catalog_id": "starter", "item": "concrete-wall", "params": {"width_m": 8}})
        self.assertEqual((status, shape["envelope"]["width_m"]), (200, 8))
        self.assertEqual(self.call("POST", "/api/resolve", {
            "catalog_id": "starter", "item": "concrete-wall", "params": {"width_m": -1}})[0], 400)
        self.assertEqual(self.call("POST", "/api/resolve", {"catalog_id": "starter", "item": "grass-ground"})[0], 404)

    def test_the_terrain_comes_with_its_height_grid(self):
        status, terrain = self.call("POST", "/api/terrain", {
            "catalog_id": "starter", "terrain": {"item": "rolling-hills", "params": {"seed": 3}}, "size_m": SIZE})
        self.assertEqual((status, terrain["kind"], terrain["item"]), (200, "hfield", "rolling-hills"))
        heights = terrain["heights"]
        self.assertEqual((len(heights), len(heights[0])), (terrain["nrow"], terrain["ncol"]))
        self.assertGreater(max(map(max, heights)), 0)
        status, flat = self.call("POST", "/api/terrain", {"catalog_id": "starter", "terrain": GROUND, "size_m": SIZE})
        self.assertEqual((status, flat["kind"]), (200, "flat"))

    def test_an_example_opens_read_only_and_saving_makes_an_editable_copy(self):
        _, listed = self.call("GET", "/api/recipes")
        example = next(item for item in listed if item["id"] == "drone-practice-field")
        self.assertFalse(example["editable"])
        self.assertEqual((example["size_m"], example["objects"], example["catalog_id"]), (SIZE, 6, "starter"))
        _, loaded = self.call("GET", "/api/recipes/drone-practice-field")
        recipe = loaded["recipe"]
        recipe["objects"][0]["pose"]["x_m"] = 1.25
        body = {key: recipe[key] for key in ("name", "size_m", "terrain", "objects")}
        status, saved = self.call("PUT", "/api/recipes/drone-practice-field", {**body, "catalog_id": "starter"})
        self.assertEqual(status, 200, saved)
        written = yaml.safe_load((self.user / "drone-practice-field.yaml").read_text(encoding="utf-8"))
        # The catalog path points at the Studio Catalog from the saved location.
        self.assertEqual((self.user / written["catalog"]).resolve(),
                         (env_studio.CATALOGS / "starter/catalog.yaml").resolve())
        self.assertEqual(written["objects"][0]["pose"]["x_m"], 1.25)
        _, listed = self.call("GET", "/api/recipes")
        self.assertTrue(next(item for item in listed if item["id"] == "drone-practice-field")["editable"])

    def test_the_3d_preview_is_the_generated_glb_of_the_unsaved_recipe(self):
        objects = [cone("a", 0, 0), cone("b", 2, 0)]
        body = json.dumps({"size_m": SIZE, "terrain": GROUND, "objects": objects}).encode()
        request = Request(f"http://127.0.0.1:{self.port}/api/glb", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.headers["Content-Type"], "model/gltf-binary")
            glb = response.read()
        self.assertEqual(glb[:4], b"glTF")
        document = json.loads(glb[20:20 + int.from_bytes(glb[12:16], "little")])
        self.assertEqual([node["name"] for node in document["nodes"]], ["terrain", "a", "b"])
        status, error = self.call("POST", "/api/glb", {"size_m": {"east": 0, "north": 1}, "terrain": GROUND})
        self.assertEqual(status, 400)
        self.assertIn("3D を作れません", error["error"])

    @unittest.skipUnless(env_validate.available(), "MuJoCo is not installed")
    def test_the_studio_validates_an_unsaved_layout_with_mujoco(self):
        status, result = self.call("POST", "/api/validate", {
            "size_m": SIZE, "terrain": GROUND, "objects": [cone("a", 0, 0), cone("b", 0.2, 0), cone("c", 9.9, 0)]})
        self.assertEqual((status, result["ok"], result["stage"]), (200, False, "physics"))
        codes = {(item["code"], item["path"]) for item in result["diagnostics"]}
        self.assertIn(("outside", "objects[2]"), codes)
        self.assertTrue(any(code == "overlap" for code, _ in codes))

    def test_schema_problems_are_diagnostics_not_errors(self):
        status, result = self.call("POST", "/api/validate", {
            "size_m": SIZE, "terrain": GROUND, "objects": [{"id": "x", "item": "no-such", "pose": {"x_m": 0, "y_m": 0}}]})
        self.assertEqual((status, result["ok"], result["stage"]), (200, False, "schema"))
        self.assertEqual([(item["code"], item["path"]) for item in result["diagnostics"]],
                         [("unknown_reference", "objects[0].item")])

    def test_a_map_area_becomes_a_saved_recipe(self):
        import test_env_map

        box = test_env_map.BOX.as_json()
        with mock.patch.object(env_studio.env_map, "fetch_overpass", return_value=test_env_map.sample()) as fetch:
            status, result = self.call("POST", "/api/map/import", {"id": "my-block", "name": "街区", "bbox": box})
        self.assertEqual(status, 200, result)
        fetch.assert_called_once()
        self.assertEqual((result["buildings"], result["roads"]), (5, 2))
        # Saved like any Recipe (and opens in the Studio), with the data as fetched beside it.
        _, loaded = self.call("GET", "/api/recipes/my-block")
        self.assertEqual((loaded["editable"], loaded["recipe"]["terrain"]["item"]), (True, "city-ground"))
        self.assertEqual(loaded["recipe"]["geo"]["bbox_deg"], box)
        self.assertTrue((self.user.parent / "map-data/my-block.json").exists())
        # The same id again is refused; the map settings are there for the page.
        self.assertEqual(self.call("POST", "/api/map/import", {"id": "my-block", "bbox": box})[0], 409)
        _, config = self.call("GET", "/api/map/config")
        self.assertIn("{z}", config["tiles"]["url"])
        status, error = self.call("POST", "/api/map/import", {"id": "big", "bbox": {
            "south": 35, "west": 135, "north": 35.2, "east": 135.2}, "source": "geojson",
            "geojson": {"type": "FeatureCollection", "features": []}})
        self.assertEqual(status, 400)
        self.assertIn("地図から作れません", error["error"])

    def test_an_invalid_recipe_is_rejected_and_not_saved(self):
        status, body = self.call("PUT", "/api/recipes/bad", {
            "size_m": SIZE, "terrain": GROUND, "objects": [{"id": "x", "item": "no-such", "pose": {"x_m": 0, "y_m": 0}}]})
        self.assertEqual(status, 400)
        self.assertIn("保存できません", body["error"])
        self.assertFalse((self.user / "bad.yaml").exists())
        self.assertEqual(self.call("PUT", "/api/recipes/Bad_Id", {"size_m": SIZE})[0], 400)
        self.assertEqual(self.call("GET", "/api/recipes/missing")[0], 404)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class BrowserModulesTest(unittest.TestCase):
    def check(self, script):
        completed = subprocess.run(["node", str(ROOT / "tests" / script)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_browser_layout_check(self):
        self.check("geometry_check.mjs")

    def test_the_browser_object_resolution(self):
        self.check("parts_check.mjs")

    def test_the_browser_undo_history(self):
        self.check("history_check.mjs")


class LifecycleTest(unittest.TestCase):
    """start / status / stop run a real background Studio on a free port."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "studio"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.addCleanup(lambda: env_studio.stop(self.state))

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools/env_studio.py"), *args, "--state-dir", str(self.state)],
            capture_output=True, text=True, check=False, timeout=60,
        )

    def test_start_status_stop(self):
        self.assertEqual(self.run_tool("status").returncode, 1)
        started = self.run_tool("start", "--port", str(self.port))
        self.assertEqual(started.returncode, 0, started.stderr)
        state = json.loads((self.state / "studio.json").read_text(encoding="utf-8"))
        self.assertEqual(state["port"], self.port)
        with urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["pid"], state["pid"])
        again = self.run_tool("start", "--port", str(self.port))
        self.assertIn("already running", again.stdout)
        self.assertEqual(self.run_tool("status").returncode, 0)
        self.assertEqual(self.run_tool("stop").returncode, 0)
        self.assertFalse((self.state / "studio.json").exists())
        self.assertEqual(self.run_tool("status").returncode, 1)


if __name__ == "__main__":
    unittest.main()
