import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_schema  # noqa: E402
import envstudio  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

CATALOG = ROOT / "catalogs/starter/catalog.yaml"
EXAMPLES = sorted((ROOT / "recipes/examples").glob("*.yaml"))


def recipe_data(*objects, terrain=None):
    return {
        "schema": env_schema.RECIPE_SCHEMA, "name": "test", "catalog": "../../catalogs/starter/catalog.yaml",
        "size_m": {"east": 20, "north": 30}, "terrain": terrain or {"item": "grass-ground"}, "objects": list(objects),
    }


def parse(data):
    return env_schema.parse_recipe(data, (ROOT / "recipes/examples/test.yaml").resolve())


def problems(data):
    with self_raises() as caught:
        parse(data)
    return caught.diagnostics


class self_raises:
    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if not isinstance(error, DiagnosticError):
            raise AssertionError(f"expected diagnostics, got {error!r}")
        self.diagnostics = [item.as_json() for item in error.diagnostics]
        return True


class CatalogTest(unittest.TestCase):
    def test_the_starter_catalog_has_every_category(self):
        catalog = env_schema.load_catalog(CATALOG)
        types = {item.type.id for item in catalog.items.values()}
        self.assertLessEqual({"flat_ground", "hills_terrain", "wall", "building_block", "box_obstacle", "pylon", "gate",
                              "ramp", "platform", "landing_pad", "road_surface", "road_marking", "guard_rail",
                              "road_sign", "traffic_signal", "food_stall", "open_space"}, types)

    def test_catalog_problems_are_all_reported(self):
        data = {"schema": env_schema.CATALOG_SCHEMA, "items": [
            {"id": "a", "type": "gate", "params": {"opening_width_m": 99, "colour": "#fff000"}},
            {"id": "b", "type": "no_such_type"},
            {"id": "c", "type": "object"},
        ]}
        with self_raises() as caught:
            env_schema.parse_catalog(data, CATALOG)
        codes = {(item["path"], item["code"]) for item in caught.diagnostics}
        self.assertEqual(codes, {
            ("items[0].params.opening_width_m", "out_of_range"),
            ("items[0].params.colour", "unknown_reference"),
            ("items[1].type", "unknown_reference"),
            ("items[2].type", "not_allowed"),
        })


class RecipeTest(unittest.TestCase):
    def test_every_example_is_valid(self):
        for path in EXAMPLES:
            with self.subTest(recipe=path.name):
                recipe = env_schema.load_recipe(path)
                self.assertTrue(recipe.objects)

    def test_the_readme_example_field(self):
        recipe = env_schema.load_recipe(ROOT / "recipes/examples/drone-practice-field.yaml")
        self.assertEqual((recipe.size_east_m, recipe.size_north_m), (20.0, 30.0))
        gates = [obj for obj in recipe.objects if obj.item == "race-gate"]
        self.assertEqual(len(gates), 3)
        pad = next(obj for obj in recipe.objects if obj.item == "landing-pad")
        self.assertGreater(pad.pose.x_m, 0)  # right (east)
        self.assertGreater(pad.pose.y_m, 0)  # back (north)

    def test_objects_stand_on_the_hills(self):
        recipe = env_schema.load_recipe(ROOT / "recipes/examples/hills-field.yaml")
        self.assertEqual(recipe.terrain.kind, "hfield")
        for obj in recipe.objects:
            with self.subTest(object=obj.id):
                ground = recipe.terrain.highest_under(env_schema.footprint(obj))
                self.assertAlmostEqual(obj.pose.z_m, ground + env_schema.HFIELD_CLEARANCE_M, places=5)
                self.assertGreaterEqual(obj.pose.z_m, recipe.terrain.height_at(obj.pose.x_m, obj.pose.y_m) - 1e-9)

    def test_a_placement_changes_only_what_its_item_allows(self):
        gate = parse(recipe_data({"id": "g", "item": "race-gate", "pose": {"x_m": 0, "y_m": 0},
                                  "params": {"opening_width_m": 3}})).objects[0]
        self.assertAlmostEqual(gate.shape.envelope["width_m"], 3.2)
        found = problems(recipe_data({"id": "g", "item": "race-gate", "pose": {"x_m": 0, "y_m": 0},
                                      "params": {"bar_m": 0.2}}))
        self.assertEqual([(item["path"], item["code"]) for item in found], [("objects[0].params.bar_m", "not_allowed")])

    def test_every_problem_in_a_recipe_comes_back_at_once(self):
        found = problems(recipe_data(
            {"id": "g1", "item": "race-gate", "pose": {"x_m": 0, "y_m": 0}, "params": {"opening_width_m": 50}},
            {"id": "g2", "item": "race-gat", "pose": {"x_m": 0, "y_m": 0}},
            {"id": "c1", "item": "traffic-cone", "pose": {"x_m": "left", "y_m": 0, "z_m": 3}},
            {"id": "g1", "item": "traffic-cone", "pose": {"x_m": 1, "y_m": 1}},
        ))
        self.assertEqual([(item["path"], item["code"]) for item in found], [
            ("objects[0].params.opening_width_m", "out_of_range"),
            ("objects[1].item", "unknown_reference"),
            ("objects[2].pose.z_m", "unknown_field"),
            ("objects[2].pose.x_m", "wrong_type"),
        ])
        self.assertIn("race-gate", found[1]["expected"])  # the right name is offered

    def test_the_terrain_must_be_a_terrain_item(self):
        found = problems(recipe_data(terrain={"item": "race-gate"}))
        self.assertEqual(found[0]["path"], "terrain.item")
        self.assertIn("grass-ground", found[0]["expected"])

    def test_invalid_recipes(self):
        cases = {
            "schema": (lambda d: d.update(schema="other"), "wrong_schema"),
            "size": (lambda d: d["size_m"].update(east=0), "out_of_range"),
            "typo": (lambda d: d.update(objets=[]), "unknown_field"),
            "duplicate": (lambda d: d["objects"].extend([copy.deepcopy(d["objects"][0])]), "duplicate_id"),
        }
        for name, (mutate, code) in cases.items():
            data = recipe_data({"id": "c", "item": "traffic-cone", "pose": {"x_m": 0, "y_m": 0}})
            mutate(data)
            with self.subTest(case=name):
                self.assertIn(code, [item["code"] for item in problems(data)])


class CommandLineTest(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        completed = subprocess.run([sys.executable, str(ROOT / "tools/envstudio.py"), "--json", *args],
                                   input=stdin, capture_output=True, text=True, cwd=ROOT)
        return completed.returncode, json.loads(completed.stdout)

    def test_discovery(self):
        status, types = self.run_cli("types")
        self.assertEqual(status, 0)
        self.assertEqual(types["schemas"]["recipe"], env_schema.RECIPE_SCHEMA)
        self.assertIn("gate", [row["id"] for row in types["types"]])
        status, gate = self.run_cli("describe-type", "gate")
        self.assertIn("opening_width_m", [param["name"] for param in gate["params"]])
        status, item = self.run_cli("describe-item", "catalogs/starter/catalog.yaml", "race-gate")
        self.assertEqual({param["name"] for param in item["placement_params"]},
                         {"color", "opening_width_m", "opening_height_m", "clearance_m"})

    def test_validate_and_resolve_from_stdin(self):
        data = recipe_data({"id": "g", "item": "race-gate", "pose": {"x_m": 0, "y_m": 0}})
        data["catalog"] = "catalogs/starter/catalog.yaml"
        status, result = self.run_cli("validate", "-", "--base", ".", stdin=json.dumps(data))
        self.assertEqual((status, result["ok"]), (0, True))
        status, resolved = self.run_cli("resolve", "-", "--base", ".", stdin=json.dumps(data))
        self.assertEqual(resolved["objects"][0]["pose"]["z_m"], 0.0)
        data["objects"][0]["params"] = {"clearance": 1}
        status, result = self.run_cli("validate", "-", "--base", ".", stdin=json.dumps(data))
        self.assertEqual((status, result["ok"]), (1, False))
        self.assertEqual(result["diagnostics"][0]["path"], "objects[0].params.clearance")
        self.assertIn("clearance_m", result["diagnostics"][0]["expected"])

    def test_text_output(self):
        self.assertEqual(envstudio.main(["validate", str(EXAMPLES[0])]), 0)


if __name__ == "__main__":
    unittest.main()
