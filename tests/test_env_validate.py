import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_schema  # noqa: E402
import env_types  # noqa: E402

try:
    import mujoco  # noqa: F401
    import env_validate
except ModuleNotFoundError:
    env_validate = None

EXAMPLES = sorted((ROOT / "recipes/examples").glob("*.yaml"))


def recipe(*objects, terrain="grass-ground", size=(20, 30)):
    return env_schema.parse_recipe({
        "schema": env_schema.RECIPE_SCHEMA, "catalog": "../../catalogs/starter/catalog.yaml",
        "size_m": {"east": size[0], "north": size[1]}, "terrain": {"item": terrain}, "objects": list(objects),
    }, (ROOT / "recipes/examples/test.yaml").resolve())


def box(object_id, x, y, yaw=0, **params):
    return {"id": object_id, "item": "box-obstacle", "pose": {"x_m": x, "y_m": y, "yaw_deg": yaw}, "params": params}


@unittest.skipIf(env_validate is None, "MuJoCo is not installed")
class ValidateTest(unittest.TestCase):
    def check(self, *objects, **options):
        return [item.as_json() for item in env_validate.check(recipe(*objects, **options))]

    def test_every_example_is_a_valid_world(self):
        for path in EXAMPLES:
            with self.subTest(recipe=path.name):
                self.assertEqual(env_validate.validate(env_schema.load_recipe(path))["diagnostics"], [])

    def test_an_overlap_names_both_objects(self):
        (problem,) = self.check(box("a", 0, 0), box("b", 0.9, 0))
        self.assertEqual((problem["path"], problem["code"], problem["related"]), ("objects[0]", "overlap", ["objects[1]"]))
        self.assertAlmostEqual(problem["actual"], 0.1, places=3)

    def test_touching_is_fine_and_the_tolerance_is_one_millimetre(self):
        self.assertEqual(self.check(box("a", 0, 0), box("b", 1.0, 0)), [])
        self.assertEqual(self.check(box("a", 0, 0), box("b", 0.9995, 0)), [])  # 0.5 mm
        self.assertEqual([item["code"] for item in self.check(box("a", 0, 0), box("b", 0.998, 0))], ["overlap"])

    def test_outside_names_the_edge(self):
        (problem,) = self.check(box("a", 9.8, 0))
        self.assertEqual((problem["code"], problem["actual"]["edge"]), ("outside", "east"))
        self.assertAlmostEqual(problem["actual"]["depth_m"], 0.3, places=3)
        turned = self.check(box("a", -9.5, -14.5, yaw=45))
        self.assertEqual({item["actual"]["edge"] for item in turned}, {"west", "south"})

    def test_a_tall_building_sticking_out_is_seen_above_the_old_boundary_height(self):
        # A 300 m tower whose top part only reaches past the east edge (a leaning footprint is
        # not possible, so the whole outline sticks out 0.5 m; the boundary must reach its height).
        tower = {"id": "tower", "item": "building-footprint", "pose": {"x_m": 9.0, "y_m": 0, "yaw_deg": 0},
                 "params": {"footprint": [[-1.5, -1], [1.5, -1], [1.5, 1], [-1.5, 1]], "height_m": 300, "min_height_m": 200}}
        codes = [(item["code"], item["actual"]["edge"]) for item in self.check(tower)]
        self.assertEqual(codes, [("outside", "east")])

    def test_an_object_reaching_into_the_hills(self):
        # A long bar whose declared outline is only its middle: set on the
        # ground under that middle, its ends reach into the slope.
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy(ROOT / "types/environment-types.yaml", directory)
            (Path(directory) / "extra.yaml").write_text(
                "schema: hakoniwa.environment-types/v1\ntypes:\n"
                "  - id: long_bar\n    extends: object\n    description: test\n"
                "    shapes:\n      - {name: bar, primitive: box, w: 16, d: 0.2, h: 0.1}\n"
                "    envelope: {primitive: box, w: 0.2, d: 0.2}\n", encoding="utf-8")
            library = env_types.load_types(Path(directory))
            catalog = env_schema.parse_catalog({"schema": env_schema.CATALOG_SCHEMA, "items": [
                {"id": "hills", "type": "hills_terrain", "params": {"max_height_m": 2.0, "hills": 1, "radius_m": 6.0,
                                                                     "seed": 1}},
                {"id": "bar", "type": "long_bar"},
            ]}, ROOT / "catalogs/test.yaml", library)
            world = env_schema.parse_recipe({
                "schema": env_schema.RECIPE_SCHEMA, "size_m": {"east": 40, "north": 40}, "terrain": {"item": "hills"},
                "objects": [{"id": "bar", "item": "bar", "pose": {"x_m": 0, "y_m": 0}}],
            }, ROOT / "recipes/test.yaml", catalog)
            codes = {(item.path, item.code) for item in env_validate.check(world)}
            self.assertIn(("objects[0]", "below_terrain"), codes)

    def test_a_world_mujoco_cannot_load(self):
        with mock.patch("env_generate.environment_mjcf", return_value="<mujoco><worldbody><geom type='nope'/></worldbody></mujoco>"):
            (problem,) = [item.as_json() for item in env_validate.check(recipe(box("a", 0, 0)))]
        self.assertEqual(problem["code"], "compile_error")

    def test_the_command_line_checks_the_schema_then_the_physics(self):
        data = {"schema": env_schema.RECIPE_SCHEMA, "catalog": "catalogs/starter/catalog.yaml",
                "size_m": {"east": 20, "north": 30}, "terrain": {"item": "grass-ground"},
                "objects": [box("a", 0, 0), box("b", 0.5, 0)]}

        def run(*extra):
            completed = subprocess.run([sys.executable, str(ROOT / "tools/envstudio.py"), "--json", "validate", "-",
                                        "--base", ".", *extra], input=json.dumps(data), capture_output=True, text=True,
                                       cwd=ROOT)
            return completed.returncode, json.loads(completed.stdout)

        status, result = run()
        self.assertEqual((status, result["ok"], result["diagnostics"][0]["code"]), (1, False, "overlap"))
        status, result = run("--no-physics")
        self.assertEqual((status, result["physics"]), (0, "skipped"))
        data["objects"][1]["pose"]["x_m"] = 3
        status, result = run()
        self.assertEqual((status, result["physics"]), (0, "mujoco"))


if __name__ == "__main__":
    unittest.main()
