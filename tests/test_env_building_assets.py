"""A building with its own look and colliders (a CityGML import): lift_m and
scale_x / scale_y / scale_z move and stretch them; the stairs part."""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_generate  # noqa: E402
import env_schema  # noqa: E402

try:
    import mujoco
    import numpy as np
except ModuleNotFoundError:
    mujoco = None

CATALOG = (ROOT / "catalogs/starter/catalog.yaml").resolve()


def recipe_text(objects: str) -> str:
    return f"""schema: hakoniwa.environment-recipe/v1
name: buildings
catalog: {CATALOG}
size_m: {{east: 100, north: 100}}
terrain: {{item: grass-ground}}
objects:
{objects}
"""


class BuildingAssetTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        look = env_schema.load_recipe(self.write("look.yaml", recipe_text(
            "- {id: wall, item: box-obstacle, pose: {x_m: 0, y_m: 0, yaw_deg: 0}, "
            "params: {width_m: 30, depth_m: 0.2, height_m: 10}}")))
        (self.directory / "wall.glb").write_bytes(env_generate.environment_glb(look))
        # Envsim's frame: x north, y west. A wall 30 m long east-west (Envsim y), 0.2 m thick, 10 m high.
        self.write("wall.xml", '<mujoco model="wall"><asset/><worldbody>'
                   '<geom name="wall" type="box" pos="0 0 5" size="0.1 15 5" contype="1" conaffinity="1"/>'
                   '</worldbody></mujoco>')

    def write(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def building(self, extra: str = "") -> env_schema.Recipe:
        return env_schema.load_recipe(self.write("r.yaml", recipe_text(
            "- id: bldg\n  item: building-footprint\n  pose: {x_m: 0, y_m: 0, yaw_deg: 0}\n"
            "  anchor: {x_m: 0, y_m: 0, z_m: 0, yaw_deg: 0}\n"
            "  params: {footprint: [[-15, -0.1], [15, -0.1], [15, 0.1], [-15, 0.1]], height_m: 10, "
            f"collision: wall.xml, visual: wall.glb{extra}}}\n")))

    def node(self, recipe):
        document, _ = env_generate.read_glb(env_generate.environment_glb(recipe))
        return next(node for node in document["nodes"] if node["name"] == "bldg")

    def body_z(self, recipe):
        body = next(b for b in ET.fromstring(env_generate.environment_mjcf(recipe)).iter("body")
                    if b.get("name") == "object:bldg")
        return float(body.get("pos").split()[2])

    def test_lift_raises_the_look_and_the_colliders(self):
        plain, lifted = self.building(), self.building(", lift_m: 5")
        self.assertAlmostEqual(self.node(lifted)["translation"][1] - self.node(plain)["translation"][1], 5.0, places=6)
        self.assertAlmostEqual(self.body_z(lifted) - self.body_z(plain), 5.0, places=6)

    def test_scales_stretch_the_look_and_the_footprint(self):
        stretched = self.building(", scale_x: 0.5, scale_y: 2, scale_z: 1.5")
        self.assertEqual(self.node(stretched)["scale"], [0.5, 1.5, 2.0])  # glTF: x, up, -y
        self.assertNotIn("scale", self.node(self.building()))
        xs = [x for x, _ in env_schema.footprint(stretched.objects[0])]
        self.assertAlmostEqual(max(xs) - min(xs), 15.0, places=6)

    @unittest.skipIf(mujoco is None, "needs MuJoCo")
    def test_scales_stretch_the_colliders(self):
        def extent(recipe):
            model = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(recipe))
            data = mujoco.MjData(model)
            mujoco.mj_forward(model, data)
            geom = np.zeros(1, dtype=np.int32)

            def hit(start, direction):
                distance = mujoco.mj_ray(model, data, np.array(start, float), np.array(direction, float), None, 1, -1, geom)
                return None if distance < 0 else distance

            # East end of the wall (its x), its top.
            east = 40.0 - hit([40.0, 0.0, 2.0], [-1.0, 0.0, 0.0])
            top = 40.0 - hit([0.0, 0.0, 40.0], [0.0, 0.0, -1.0])
            return east, top

        east, top = extent(self.building())
        self.assertAlmostEqual(east, 15.0, places=3)
        self.assertAlmostEqual(top, 10.0, places=3)
        east, top = extent(self.building(", scale_x: 0.5, scale_z: 1.5"))
        self.assertAlmostEqual(east, 7.5, places=3)
        self.assertAlmostEqual(top, 15.0, places=3)

    def test_stairs_rise_to_their_height(self):
        recipe = env_schema.load_recipe(self.write("s.yaml", recipe_text(
            "- {id: up, item: stairs, pose: {x_m: 0, y_m: 0, yaw_deg: 0}, params: {height_m: 6, length_m: 12, width_m: 3}}")))
        solids = recipe.objects[0].solids
        self.assertEqual(len(solids), 24)
        tops = [solid.z_m + solid.height_m / 2 for solid in solids]
        self.assertAlmostEqual(max(tops), 6.0)
        self.assertTrue(all(abs(solid.z_m - solid.height_m / 2) < 1e-9 for solid in solids))  # each down to the ground
        self.assertEqual(sorted(tops), tops)  # rising along y


if __name__ == "__main__":
    unittest.main()
