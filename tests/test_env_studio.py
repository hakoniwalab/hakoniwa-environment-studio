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
import test_env_citygml  # noqa: E402
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
        for patch in (mock.patch.object(env_studio, "USER_RECIPES", self.user),
                      mock.patch.object(env_studio.env_cityworld, "WORK", self.user.parent / "city-worlds")):
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

    def test_only_this_studios_pages_may_change_things(self):
        def post(headers):
            request = Request(f"http://127.0.0.1:{self.port}/api/validate", data=b"{}", method="POST", headers=headers)
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status
            except HTTPError as error:
                return error.code

        self.assertEqual(post({"Content-Type": "text/plain"}), 415)  # a plain form or no-cors fetch
        self.assertEqual(post({"Content-Type": "application/json", "Origin": "https://example.com"}), 403)
        self.assertEqual(post({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}), 200)
        self.assertEqual(self.call("GET", "/api/no-such")[0], 404)

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

    def test_many_placements_resolve_in_one_request(self):
        status, answer = self.call("POST", "/api/resolve-many", {"catalog_id": "starter", "placements": [
            {"item": "concrete-wall", "params": {"width_m": 8}}, {"item": "concrete-wall", "params": {"width_m": -1}},
            {"item": "no-such"}]})
        self.assertEqual(status, 200)
        shapes = answer["shapes"]
        self.assertEqual(shapes[0]["envelope"]["width_m"], 8)
        self.assertIn("error", shapes[1])
        self.assertIn("error", shapes[2])

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

    def test_a_move_needs_only_the_poses_which_match_the_glb_nodes(self):
        # A cone on a board: moving the board lifts the cone, so heights come from the server.
        _, loaded = self.call("GET", "/api/recipes/drone-practice-field")
        body = {**{key: loaded["recipe"][key] for key in ("name", "size_m", "terrain", "objects")}, "catalog_id": "starter"}
        body["objects"][0]["pose"]["x_m"] += 0.5
        status, answer = self.call("POST", "/api/poses", body)
        self.assertEqual(status, 200, answer)
        request = Request(f"http://127.0.0.1:{self.port}/api/glb", data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            glb = response.read()
        document = json.loads(glb[20:20 + int.from_bytes(glb[12:16], "little")])
        nodes = {node["name"]: node for node in document["nodes"][1:]}
        self.assertEqual(set(answer["poses"]), set(nodes))
        for name, pose in answer["poses"].items():
            for got, want in zip(pose["translation"], nodes[name]["translation"]):
                self.assertAlmostEqual(got, want, places=9)

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

    @unittest.skipUnless(test_env_citygml.ENVSIM, "hakoniwa-envsim is not available")
    def test_a_map_area_becomes_a_saved_recipe_of_parts(self):
        box = test_env_citygml.box().as_json()
        osm = env_studio.env_citygml.osm2citygml()
        with mock.patch.object(osm, "fetch_overpass", return_value=test_env_citygml.sample()) as fetch:
            status, result = self.call("POST", "/api/map/import", {"id": "my-block", "name": "街区", "bbox": box})
        self.assertEqual(status, 200, result)
        fetch.assert_called_once()
        self.assertEqual(result["buildings"], 4)
        # Saved like any Recipe (and opens in the Studio); the map data and its CityGML are kept.
        _, loaded = self.call("GET", "/api/recipes/my-block")
        self.assertEqual((loaded["editable"], loaded["recipe"]["terrain"]["item"]), (True, "city-ground"))
        geo = loaded["recipe"]["geo"]
        self.assertEqual((geo["provider"], geo["data_timestamp"]), ("openstreetmap", "2026-09-01T00:00:00Z"))
        self.assertIn("overpass", json.loads(geo["query"]))
        data = self.user.parent / "map-data/my-block"
        self.assertTrue((data / "map.json").exists() and (data / "map_bldg_op.gml").exists())
        # The PLATEAU City World browser's selection form: the typed half extents are kept.
        center = test_env_citygml.box().center
        with mock.patch.object(osm, "fetch_overpass", return_value=test_env_citygml.sample()):
            status, result = self.call("POST", "/api/map/import", {"id": "by-selection", "selection": {
                "center": {"latitude": center[0], "longitude": center[1]},
                "half_extent_m": {"north_south": 50, "east_west": 50}}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["size_m"]["north"], 100.0)
        self.assertLessEqual(result["assumed"]["building_height"], result["buildings"])
        status, error = self.call("POST", "/api/map/import", {"id": "tiny", "selection": {
            "center": {"latitude": center[0], "longitude": center[1]}, "half_extent_m": {"north_south": 5, "east_west": 50}}})
        self.assertEqual(status, 400)
        # A ground that needs data of its own is not offered for map data.
        status, error = self.call("POST", "/api/map/import", {"id": "dem", "terrain": "city-dem", "bbox": box})
        self.assertEqual(status, 400)
        self.assertIn("city-dem", error["error"])
        # The same id again is refused; the map settings are there for the page.
        self.assertEqual(self.call("POST", "/api/map/import", {"id": "my-block", "bbox": box})[0], 409)
        _, config = self.call("GET", "/api/map/config")
        self.assertIn("{z}", config["tiles"]["url"])
        self.assertNotIn("city-dem", [item["id"] for item in config["terrains"]])
        status, error = self.call("POST", "/api/map/import", {"id": "big", "bbox": {
            "south": 35, "west": 135, "north": 35.2, "east": 135.2}, "source": "geojson",
            "geojson": {"type": "FeatureCollection", "features": []}})
        self.assertEqual(status, 400)
        self.assertIn("地図から作れません", error["error"])

    @unittest.skipUnless(test_env_citygml.ENVSIM, "hakoniwa-envsim is not available")
    def test_city_worlds_in_a_workspace_are_listed_and_imported_as_parts(self):
        workspace = test_env_citygml.WorkspaceTest
        workspace.setUpClass()
        self.addCleanup(workspace.tearDownClass)
        from urllib.parse import quote
        status, found = self.call("GET", f"/api/city-worlds?root={quote(str(workspace.root))}")
        self.assertEqual((status, [item["title"] for item in found["builds"]]), (200, ["tokyo-test"]))
        status, result = self.call("POST", "/api/city-worlds/import",
                                   {"id": "tokyo-parts", "path": found["builds"][0]["path"], "name": "東京"})
        self.assertEqual((status, result["buildings"]), (200, 4), result)
        _, loaded = self.call("GET", "/api/recipes/tokyo-parts")
        self.assertEqual(loaded["recipe"]["objects"][0]["source"]["kind"], "citygml")
        self.assertEqual(self.call("POST", "/api/city-worlds/import", {"id": "x", "path": "/nowhere"})[0], 404)

    def test_a_saved_recipe_is_deleted_into_the_trash_with_its_files(self):
        _, loaded = self.call("GET", "/api/recipes/drone-practice-field")
        body = {key: loaded["recipe"][key] for key in ("name", "size_m", "terrain", "objects")}
        self.assertEqual(self.call("PUT", "/api/recipes/field-copy", {**body, "catalog_id": "starter"})[0], 200)
        (self.user / "field-copy.assets").mkdir()
        (self.user / "field-copy.assets/a.glb").write_bytes(b"glb")
        map_data = self.user.parent / "map-data/field-copy"
        map_data.mkdir(parents=True)
        (map_data / "map.json").write_text("{}", encoding="utf-8")

        status, result = self.call("DELETE", "/api/recipes/field-copy", {})
        self.assertEqual(status, 200, result)
        trash = Path(result["trash"])
        self.assertEqual(trash.parent, (self.user.parent / "trash").resolve())
        self.assertEqual(sorted(path.name for path in trash.iterdir()),
                         ["field-copy.assets", "field-copy.yaml", "map-data"])
        self.assertTrue((trash / "map-data/map.json").is_file())
        self.assertFalse((self.user / "field-copy.yaml").exists() or map_data.exists())
        _, listed = self.call("GET", "/api/recipes")
        self.assertNotIn("field-copy", [item["id"] for item in listed])

        self.assertEqual(self.call("DELETE", "/api/recipes/field-copy", {})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/recipes/drone-practice-field", {})[0], 403)  # an example
        self.assertTrue((env_studio.EXAMPLE_RECIPES / "drone-practice-field.yaml").is_file())

    def test_saving_under_a_new_id_copies_the_visuals_so_deleting_one_keeps_the_other(self):
        _, loaded = self.call("GET", "/api/recipes/drone-practice-field")
        body = {**{key: loaded["recipe"][key] for key in ("name", "size_m", "terrain")}, "catalog_id": "starter"}
        footprint = {"id": "house", "item": "building-footprint", "pose": {"x_m": 0, "y_m": 0, "yaw_deg": 0},
                     "params": {"footprint": [[-2, -2], [2, -2], [2, 2], [-2, 2]], "height_m": 3}}
        self.assertEqual(self.call("PUT", "/api/recipes/town", {**body, "objects": [footprint]})[0], 200)
        (self.user / "town.assets").mkdir()
        shutil.copyfile(self._tiny_glb(), self.user / "town.assets/house.glb")
        footprint["params"]["visual"] = "town.assets/house.glb"
        status, saved = self.call("PUT", "/api/recipes/town", {**body, "objects": [footprint]})
        self.assertEqual((status, saved["copied_assets"]), (200, 0), saved)  # its own assets: nothing to copy

        status, copy = self.call("PUT", "/api/recipes/town-2", {**body, "objects": [footprint]})
        self.assertEqual((status, copy["copied_assets"], copy["assets"]),
                         (200, 1, {"objects": {"house": {"visual": "town-2.assets/house.glb"}}, "terrain": {}}))
        written = yaml.safe_load((self.user / "town-2.yaml").read_text(encoding="utf-8"))
        self.assertEqual(written["objects"][0]["params"]["visual"], "town-2.assets/house.glb")
        self.assertEqual(self.call("DELETE", "/api/recipes/town", {})[0], 200)
        self.assertEqual(self.call("GET", "/api/recipes/town-2")[0], 200)
        status, answer = self.call("POST", "/api/poses", {**body, "objects": written["objects"]})
        self.assertEqual(status, 200, answer)

        # A Recipe written by hand that points into another's assets blocks deleting that one.
        (self.user / "town-3.yaml").write_text(
            (self.user / "town-2.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        status, error = self.call("DELETE", "/api/recipes/town-2", {})
        self.assertEqual(status, 409)
        self.assertIn("town-3", error["error"])

    def _tiny_glb(self):
        """A small GLB to stand in for a building's LOD2 look: the Studio's own preview of one cone."""
        body = json.dumps({"size_m": SIZE, "terrain": GROUND, "objects": [cone("a", 0, 0)]}).encode()
        request = Request(f"http://127.0.0.1:{self.port}/api/glb", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        path = self.user.parent / "tiny.glb"
        with urlopen(request, timeout=10) as response:
            path.write_bytes(response.read())
        return path

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

    def test_the_browser_problem_list(self):
        self.check("problems_check.mjs")


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
