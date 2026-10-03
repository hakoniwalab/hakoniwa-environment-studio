"""くり抜き (passage) parts: a box cut out of the colliders and the look of the
parts it overlaps (env_generate.passage_cuts, _cut_collision, _cut_look)."""

import json
import struct
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
name: passages
catalog: {CATALOG}
size_m: {{east: 100, north: 100}}
terrain: {{item: grass-ground}}
objects:
{objects}
"""


def positions_of(glb: bytes, node_name: str):
    document, binary = env_generate.read_glb(glb)
    node = next(node for node in document["nodes"] if node["name"] == node_name)
    points = []
    for primitive in document["meshes"][node["mesh"]]["primitives"]:
        accessor = document["accessors"][primitive["attributes"]["POSITION"]]
        view = document["bufferViews"][accessor["bufferView"]]
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        values = struct.unpack_from(f"<{accessor['count'] * 3}f", binary, start)
        points.extend(zip(values[0::3], values[1::3], values[2::3]))
    return points


class PassageTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        # A wall 30 m long (east-west), 0.2 m thick, 10 m high: its look from a
        # box part, its colliders as Envsim writes them (x north, y west).
        look = env_schema.load_recipe(self.write("look.yaml", recipe_text(
            "- {id: wall, item: box-obstacle, pose: {x_m: 0, y_m: 0, yaw_deg: 0}, "
            "params: {width_m: 30, depth_m: 0.2, height_m: 10}}")))
        (self.directory / "wall.glb").write_bytes(env_generate.environment_glb(look))
        self.write("wall.xml", '<mujoco model="wall"><asset/><worldbody>'
                   '<geom name="wall" type="box" pos="0 0 5" size="0.1 15 5" rgba="0.8 0.8 0.8 1" contype="1" conaffinity="1"/>'
                   '</worldbody></mujoco>')
        self.building = ("- id: bldg\n  item: building-footprint\n  pose: {x_m: 0, y_m: 0, yaw_deg: 0}\n"
                         "  anchor: {x_m: 0, y_m: 0, z_m: 0, yaw_deg: 0}\n"
                         "  params: {footprint: [[-15, -0.1], [15, -0.1], [15, 0.1], [-15, 0.1]], height_m: 10, "
                         "collision: wall.xml, visual: wall.glb}\n")
        self.passage = ("- {id: way, item: passage, pose: {x_m: 0, y_m: 0, yaw_deg: 0}, "
                        "params: {width_m: 10, depth_m: 30, height_m: 4.5, below_m: 0.5}}\n")

    def write(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_passage_box_stands_on_the_ground_where_it_is_placed(self):
        recipe = env_schema.load_recipe(self.write("p.yaml", recipe_text(self.passage.replace("x_m: 0", "x_m: 20"))))
        cuts = env_generate.passage_cuts(recipe)
        self.assertEqual([cut["id"] for cut in cuts], ["way"])
        xs = sorted({round(c[0], 3) for c in cuts[0]["corners"]})
        zs = sorted({round(c[2], 3) for c in cuts[0]["corners"]})
        self.assertEqual((xs, zs), ([15.0, 25.0], [-0.5, 4.5]))

    def test_the_colliders_are_cut_and_the_look_too(self):
        recipe = env_schema.load_recipe(self.write("r.yaml", recipe_text(self.building + self.passage)))
        mjcf = env_generate.environment_mjcf(recipe)
        root = ET.fromstring(mjcf)
        names = [geom.get("name") for geom in root.iter("geom")]
        self.assertNotIn("bldg/wall", names)
        self.assertTrue(any(name.startswith("bldg/wall/cut") for name in names), names)
        self.assertFalse(any("way" in name for name in names))  # the passage itself is nothing
        glb = env_generate.environment_glb(recipe)
        points = positions_of(glb, "bldg")
        self.assertTrue(points)
        # Nothing of the look is left inside the passage (glTF: x east, y up).
        inside = [p for p in points if abs(p[0]) < 5 - 1e-3 and p[1] < 4.5 - 1e-3]
        self.assertEqual(inside, [])
        self.assertTrue(any(abs(p[0]) > 5 + 1e-3 and p[1] < 1 for p in points))  # the wall's ends stay
        # The editor draws the passage, a generated environment does not.
        document, _ = env_generate.read_glb(glb)
        self.assertNotIn("way", [node["name"] for node in document["nodes"]])
        editor, _ = env_generate.read_glb(env_generate.environment_glb(recipe, editor=True))
        self.assertIn("way", [node["name"] for node in editor["nodes"]])

    def test_a_building_away_from_the_passage_is_copied_as_it_is(self):
        far = self.passage.replace("x_m: 0", "x_m: 40")
        recipe = env_schema.load_recipe(self.write("f.yaml", recipe_text(self.building + far)))
        alone = env_schema.load_recipe(self.write("a.yaml", recipe_text(self.building)))
        self.assertEqual(positions_of(env_generate.environment_glb(recipe), "bldg"),
                         positions_of(env_generate.environment_glb(alone), "bldg"))
        self.assertIn("bldg/wall", [g.get("name") for g in ET.fromstring(env_generate.environment_mjcf(recipe)).iter("geom")])

    def test_lift_raises_a_buildings_look_and_colliders(self):
        lifted = self.building.replace("height_m: 10, ", "height_m: 10, lift_m: 5, ")
        recipe = env_schema.load_recipe(self.write("l.yaml", recipe_text(lifted)))
        alone = env_schema.load_recipe(self.write("a.yaml", recipe_text(self.building)))
        document, _ = env_generate.read_glb(env_generate.environment_glb(recipe))
        node = next(node for node in document["nodes"] if node["name"] == "bldg")
        base, _ = env_generate.read_glb(env_generate.environment_glb(alone))
        node0 = next(node for node in base["nodes"] if node["name"] == "bldg")
        self.assertAlmostEqual(node["translation"][1] - node0["translation"][1], 5.0, places=6)
        body = next(b for b in ET.fromstring(env_generate.environment_mjcf(recipe)).iter("body") if b.get("name") == "object:bldg")
        body0 = next(b for b in ET.fromstring(env_generate.environment_mjcf(alone)).iter("body") if b.get("name") == "object:bldg")
        self.assertAlmostEqual(float(body.get("pos").split()[2]) - float(body0.get("pos").split()[2]), 5.0, places=6)

    @unittest.skipIf(mujoco is None, "needs MuJoCo")
    def test_a_car_passes_where_the_wall_was_cut(self):
        def first_hit(text: str):
            model = mujoco.MjModel.from_xml_string(text)
            data = mujoco.MjData(model)
            mujoco.mj_forward(model, data)
            geom = np.zeros(1, dtype=np.int32)
            distance = mujoco.mj_ray(model, data, np.array([0.0, -20.0, 1.5]), np.array([0.0, 1.0, 0.0]), None, 1, -1, geom)
            return None if distance < 0 else mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, int(geom[0]))

        blocked = env_schema.load_recipe(self.write("b.yaml", recipe_text(self.building)))
        self.assertEqual(first_hit(env_generate.environment_mjcf(blocked)), "bldg/wall")
        opened = env_schema.load_recipe(self.write("o.yaml", recipe_text(self.building + self.passage)))
        self.assertIsNone(first_hit(env_generate.environment_mjcf(opened)))


if __name__ == "__main__":
    unittest.main()
