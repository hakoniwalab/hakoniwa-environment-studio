"""Exporting a Recipe as a hakoniwa-urban-mobility World (tools/env_urban.py):
the City World job layout, the World MJCF in Urban's frame (the same world as
the Studio's, turned), the terrain as Urban reads it, the collider view, the
receipt, Urban's own check when Urban is next to the Studio, and registering.
Skipped without MuJoCo (the checks compile the worlds)."""

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import env_generate  # noqa: E402
import env_schema  # noqa: E402
import env_urban  # noqa: E402
import env_validate  # noqa: E402

try:
    import trimesh  # noqa: F401
    READY = env_validate.available()
except ImportError:
    READY = False

TO_URBAN = ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))  # ENU -> (north, -east, up)


def compiled(xml: str | None = None, path: Path | None = None):
    import mujoco

    model = mujoco.MjModel.from_xml_path(str(path)) if path else mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return model, data


def geoms(model) -> dict:
    import mujoco

    return {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, index) or "": index for index in range(model.ngeom)}


@unittest.skipUnless(READY, "MuJoCo or trimesh is not installed")
class UrbanExportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def export(self, recipe: Path):
        result = env_urban.export(recipe, self.dir / recipe.stem)
        return result, json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))

    def assert_same_world(self, recipe_path: Path, mjcf: Path):
        """Every colliding geom of the Studio's world is in the export, turned into Urban's frame."""
        import numpy

        recipe = env_schema.load_recipe(recipe_path)
        studio_model, studio_data = compiled(env_generate.environment_mjcf(recipe))
        urban_model, urban_data = compiled(path=mjcf)
        turn = numpy.array(TO_URBAN)
        theirs = geoms(urban_model)
        checked = 0
        for name, index in geoms(studio_model).items():
            if name == env_generate.TERRAIN_GEOM:
                continue
            if not (studio_model.geom_contype[index] or studio_model.geom_conaffinity[index]):
                self.assertNotIn(name, theirs)  # visual only: the GLB's
                continue
            other = theirs[name]
            self.assertTrue(numpy.allclose(turn @ studio_data.geom_xpos[index], urban_data.geom_xpos[other], atol=1e-9), name)
            self.assertTrue(numpy.allclose(turn @ studio_data.geom_xmat[index].reshape(3, 3),
                                           urban_data.geom_xmat[other].reshape(3, 3), atol=1e-9), name)
            checked += 1
        return recipe, studio_model, urban_model, checked

    def test_a_course_on_flat_ground_becomes_a_plain_world(self):
        path = ROOT / "recipes/examples/car-test-course.yaml"
        result, receipt = self.export(path)
        job = Path(result["job"])
        for part in ("build/world/city-world.xml", "build/world/city-world.glb", "build/components/terrain/terrain.hf",
                     "build/components/terrain/terrain-receipt.json", "viewer/city-world-colliders.glb",
                     "viewer/city-world-colliders-receipt.json", "viewer/city-world.glb"):
            self.assertTrue((job / part).is_file(), part)
        self.assertEqual((receipt["kind"], receipt["coordinate_frame"]["origin"]["latitude"]), ("plain", 0.0))
        self.assertEqual(receipt["coordinate_frame"]["coordinate_systems"],
                         {"mjcf": "X=North,Y=-East,Z=Up", "glb": "X=East,Y=Up,Z=-North"})
        self.assertEqual(receipt["coordinate_frame"]["half_extent_m"], {"north_south": 20.0, "east_west": 15.0})
        recipe = env_schema.load_recipe(path)
        self.assertEqual(receipt["producer"]["fingerprint"], env_generate.fingerprint(recipe))
        # Only size / asset / worldbody, and no angle a compiler setting could read.
        root = ET.parse(job / "build/world/city-world.xml").getroot()
        self.assertEqual({element.tag for element in root}, {"asset", "worldbody"})
        self.assertFalse([element for element in root.iter() if "euler" in element.attrib or "axisangle" in element.attrib])
        self.assertFalse(list(root.iter("light")))  # the vehicles bring the lighting
        _recipe, _studio, urban_model, checked = self.assert_same_world(path, job / "build/world/city-world.xml")
        self.assertGreater(checked, 10)
        self.assertEqual(urban_model.hfield_nrow[0], 2)  # the flat ground as an hfield
        if result["check"] is not None:  # Urban's own check (its schemas/city-world-job.yaml)
            self.assertTrue(result["check"]["ok"], result["check"])  # a temporary folder lies outside the workspace: a warning
            self.assertEqual([p for p in result["check"]["problems"] if p["severity"] == "error"], [])

    def test_hills_keep_their_heights_and_their_cells(self):
        import numpy

        path = ROOT / "recipes/examples/hills-field.yaml"
        result, _receipt = self.export(path)
        mjcf = Path(result["job"]) / "build/world/city-world.xml"
        recipe, studio_model, urban_model, _checked = self.assert_same_world(path, mjcf)
        # The same grid, laid out the same way (the same diagonals), as a file Urban reads.
        self.assertTrue(numpy.allclose(studio_model.hfield_data, urban_model.hfield_data, atol=1e-6))
        self.assertTrue(numpy.allclose(studio_model.hfield_size, urban_model.hfield_size))
        terrain_receipt = json.loads((Path(result["job"]) / "build/components/terrain/terrain-receipt.json").read_text())
        raw = Path(terrain_receipt["hfield"]["path"]).read_bytes()
        nrow, ncol = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:8], "little")
        self.assertEqual((nrow, ncol), (recipe.terrain.ncol, recipe.terrain.nrow))
        self.assertAlmostEqual(terrain_receipt["maximum_altitude_m"], recipe.terrain.max_height_m, places=5)

    def test_registering_calls_urban(self):
        urban = self.dir / "hakoniwa-urban-mobility"
        (urban / "tools").mkdir(parents=True)
        (urban / "tools/urban_assets.py").write_text(
            "import sys\nprint('Registered City Asset:', sys.argv[sys.argv.index('--receipt') + 1],"
            " sys.argv[sys.argv.index('--title') + 1])\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"HAKONIWA_URBAN_MOBILITY_ROOT": str(urban)}):
            done = env_urban.register(self.dir / "job/build/world/city-world-receipt.json", precompile=False,
                                      title="車のテストコース")
        self.assertTrue(done["ok"])
        self.assertIn("job/build/world/city-world-receipt.json 車のテストコース", done["output"][-1])



class CityWorldRegistrationTest(unittest.TestCase):
    """An Envsim City World build registered in Urban as it is, unregistered,
    and found in Urban's City list (a stand-in urban_assets.py)."""

    FAKE_URBAN_ASSETS = """import json, sys
args = sys.argv[1:]
state = __file__ + ".json"
try:
    cities = json.load(open(state))
except OSError:
    cities = {}
if args[0] == "register-city":
    receipt = args[args.index("--receipt") + 1]
    job = receipt.rsplit("/build/world/", 1)[0].rsplit("/", 1)[-1]
    cities[job] = {"id": job, "kind": "city", "title": args[args.index("--title") + 1] if "--title" in args else job,
                   "receipt": receipt}
    print("Registered City Asset:", job)
elif args[0] == "unregister-city":
    cities.pop(args[args.index("--id") + 1])
    print("Unregistered City Asset")
elif args[0] == "list":
    print(json.dumps(list(cities.values())))
json.dump(cities, open(state, "w"))
"""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve()
        self.urban = self.dir / "hakoniwa-urban-mobility"
        (self.urban / "tools").mkdir(parents=True)
        (self.urban / "tools/urban_assets.py").write_text(self.FAKE_URBAN_ASSETS, encoding="utf-8")
        patch = mock.patch.dict("os.environ", {"HAKONIWA_URBAN_MOBILITY_ROOT": str(self.urban)})
        patch.start()
        self.addCleanup(patch.stop)
        self.build = self.dir / "city-worlds/sapporo/build"
        (self.build / "world").mkdir(parents=True)
        (self.build / "world/city-world-receipt.json").write_text("{}", encoding="utf-8")

    def test_a_city_world_is_registered_as_it_is_and_unregistered(self):
        with mock.patch.object(env_urban, "check", return_value=None):  # no city_world_job.py in the stand-in
            result = env_urban.register_city_world(self.build, title="札幌", precompile=False)
        self.assertTrue(result["register"]["ok"], result)
        receipt = str(self.build / "world/city-world-receipt.json")
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(env_urban.urban_cities()[receipt]["id"], "sapporo")
        self.assertEqual(env_urban.urban_cities()[receipt]["title"], "札幌")
        self.assertTrue(env_urban.unregister("sapporo")["ok"])
        self.assertEqual(env_urban.urban_cities(), {})

    def test_a_job_that_breaks_urbans_contract_is_not_registered(self):
        refused = {"ok": False, "problems": [{"severity": "error", "message": "no viewer"}]}
        with mock.patch.object(env_urban, "check", return_value=refused):
            result = env_urban.register_city_world(self.build, precompile=False)
        self.assertFalse(result["register"]["ok"])
        self.assertEqual(env_urban.urban_cities(), {})

    def test_a_build_without_a_city_world_is_refused(self):
        with self.assertRaisesRegex(env_urban.ExportError, "not a City World build"):
            env_urban.register_city_world(self.dir / "city-worlds/none/build")

@unittest.skipUnless(READY, "MuJoCo or trimesh is not installed")
class UrbanExportOfACityWorldTest(unittest.TestCase):
    """An imported City World (Envsim's outputs passed through) exports as Envsim's world."""

    def test_it_is_envsims_world_again(self):
        import numpy

        import env_citygml
        import test_env_passthrough as passthrough

        if not passthrough.READY:
            self.skipTest("hakoniwa-envsim is not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build, _info = passthrough.make_build(root)
            recipe_path = root / "recipes/city.yaml"
            recipe, _report = env_citygml.convert_build(build, recipe_path=recipe_path, catalog=passthrough.fixtures.CATALOG)
            env_citygml.write_recipe(recipe, recipe_path)
            result = env_urban.export(recipe_path, root / "urban/city")
            receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["kind"], "city")
            envsim_model, envsim_data = compiled(path=build / "world/city-world.xml")
            urban_model, urban_data = compiled(path=Path(receipt["mjcf"]["path"]))
            ours = {name.split("/", 1)[-1]: index for name, index in geoms(urban_model).items()}
            for name, index in geoms(envsim_model).items():
                other = ours[env_generate.TERRAIN_GEOM if name == "plateau_ground" else name]
                self.assertTrue(numpy.allclose(envsim_data.geom_xpos[index], urban_data.geom_xpos[other], atol=1e-9), name)
                self.assertTrue(numpy.allclose(envsim_data.geom_xmat[index], urban_data.geom_xmat[other], atol=1e-9), name)
            self.assertTrue(numpy.array_equal(envsim_model.hfield_data, urban_model.hfield_data))


@unittest.skipUnless(READY, "MuJoCo or trimesh is not installed")
class UrbanRouteTest(unittest.TestCase):
    def test_the_studio_exports_a_saved_recipe(self):
        import env_studio

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(env_studio, "USER_RECIPES", Path(directory) / "work/recipes"), \
                mock.patch.object(env_urban, "EXPORTS", Path(directory) / "work/urban"):
            result = env_studio.export_urban("car-test-course", {})
            self.assertEqual(result["job"], str((Path(directory) / "work/urban/car-test-course").resolve()))
            self.assertNotIn("register", result)
            with self.assertRaises(env_studio.StudioError):
                env_studio.export_urban("no-such-recipe", {})


if __name__ == "__main__":
    unittest.main()
