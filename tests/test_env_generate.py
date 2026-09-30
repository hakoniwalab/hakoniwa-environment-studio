import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_generate  # noqa: E402
import env_schema  # noqa: E402

try:
    import mujoco
    import numpy as np
except ModuleNotFoundError:  # the MuJoCo checks need MuJoCo
    mujoco = None

EXAMPLES = ROOT / "recipes/examples"


def load(name):
    return env_schema.load_recipe(EXAMPLES / f"{name}.yaml")


def read_glb(data: bytes):
    magic, version, length = struct.unpack_from("<III", data, 0)
    assert (magic, version, length) == (0x46546C67, 2, len(data))
    json_length, _ = struct.unpack_from("<II", data, 12)
    document = json.loads(data[20:20 + json_length])
    bin_length, _ = struct.unpack_from("<II", data, 20 + json_length)
    return document, data[28 + json_length:28 + json_length + bin_length]


def world_corners(obj, solid):
    """A solid's outline vertices in the environment frame."""
    yaw = math.radians(obj.pose.yaw_deg)
    c, s = math.cos(yaw), math.sin(yaw)
    return [(obj.pose.x_m + c * x - s * y, obj.pose.y_m + s * x + c * y, obj.pose.z_m + z) for x, y, z in solid.corners()]


def same_points(a, b, tolerance=1e-5):
    remaining = list(b)
    for point in a:
        match = next((other for other in remaining if math.dist(point, other) < tolerance), None)
        if match is None:
            return False
        remaining.remove(match)
    return not remaining


class GlbTest(unittest.TestCase):
    def test_every_object_is_a_named_node_placed_in_gltf_axes(self):
        recipe = load("car-test-course")
        document, _ = read_glb(env_generate.environment_glb(recipe))
        nodes = {node["name"]: node for node in document["nodes"]}
        self.assertEqual(nodes["terrain"]["extras"]["terrain"], "asphalt-ground")
        for obj in recipe.objects:
            with self.subTest(object=obj.id):
                node = nodes[obj.id]
                self.assertEqual(node["extras"], {"object": obj.id, "item": obj.item, "type": obj.type})
                x, up, minus_north = node["translation"]
                self.assertAlmostEqual((x, up, -minus_north), (obj.pose.x_m, obj.pose.z_m, obj.pose.y_m))
                _, sy, _, cw = node["rotation"]
                self.assertAlmostEqual(math.degrees(2 * math.atan2(sy, cw)) % 360, obj.pose.yaw_deg % 360, places=6)
                # One primitive per visible solid.
                primitives = document["meshes"][node["mesh"]]["primitives"]
                self.assertEqual(len(primitives), sum(1 for solid in obj.solids if solid.visible))

    def test_the_hfield_terrain_is_a_grid_mesh(self):
        recipe = load("hills-field")
        document, _ = read_glb(env_generate.environment_glb(recipe))
        terrain = next(node for node in document["nodes"] if node["name"] == "terrain")
        accessor = document["accessors"][document["meshes"][terrain["mesh"]]["primitives"][0]["attributes"]["POSITION"]]
        self.assertEqual(accessor["count"], recipe.terrain.nrow * recipe.terrain.ncol)
        self.assertAlmostEqual(accessor["max"][1], recipe.terrain.max_height_m, places=4)  # glTF y is up
        self.assertAlmostEqual(accessor["min"][0], -20.0)
        self.assertAlmostEqual(accessor["max"][2], 20.0)  # -north: the south edge

    def test_output_is_deterministic(self):
        recipe = load("hills-field")
        self.assertEqual(env_generate.environment_glb(recipe), env_generate.environment_glb(load("hills-field")))
        self.assertEqual(env_generate.environment_mjcf(recipe), env_generate.environment_mjcf(load("hills-field")))
        self.assertEqual(env_generate.fingerprint(recipe), env_generate.fingerprint(load("hills-field")))
        self.assertNotEqual(env_generate.fingerprint(recipe), env_generate.fingerprint(load("drone-practice-field")))


@unittest.skipIf(mujoco is None, "MuJoCo is not installed")
class MjcfTest(unittest.TestCase):
    def compile(self, recipe, **options):
        model = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(recipe, **options))
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        return model, data

    def ray_height(self, model, data, x, y):
        geom = np.array([-1], dtype=np.int32)
        distance = mujoco.mj_ray(model, data, np.array([x, y, 100.0]), np.array([0, 0, -1.0]), None, 1, -1, geom)
        return 100.0 - distance, mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, geom[0])

    def test_the_hfield_matches_the_terrain_grid(self):
        recipe = load("hills-field")
        empty = env_schema.parse_recipe({**_raw("hills-field"), "objects": []}, recipe.path)
        model, data = self.compile(empty)
        terrain = recipe.terrain
        for row, col in ((0, 0), (0, terrain.ncol - 1), (terrain.nrow - 1, 0), (40, 77), (120, 30), (80, 80)):
            with self.subTest(row=row, col=col):
                x = -terrain.half_east + col / (terrain.ncol - 1) * terrain.size_east_m
                y = terrain.half_north - row / (terrain.nrow - 1) * terrain.size_north_m
                inset = (min(max(x, -19.999), 19.999), min(max(y, -19.999), 19.999))
                height, geom = self.ray_height(model, data, *inset)
                self.assertEqual(geom, "terrain")
                self.assertAlmostEqual(height, terrain.heights[row][col], places=3)

    def test_flat_ground_top_is_at_zero(self):
        model, data = self.compile(load("drone-practice-field"))
        height, geom = self.ray_height(model, data, 5.0, -10.0)
        self.assertEqual(geom, "terrain")
        self.assertAlmostEqual(height, 0.0, places=6)

    def test_geoms_are_where_the_solids_are(self):
        # Boxes, cylinders, tilted discs and a wedge (a mesh MuJoCo recentres).
        recipe = load("car-test-course")
        model, data = self.compile(recipe)
        checked = set()
        for obj in recipe.objects:
            for solid in obj.solids:
                index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, env_generate.geom_name(obj.id, solid.name))
                self.assertGreaterEqual(index, 0)
                centre, axes = data.geom_xpos[index], data.geom_xmat[index].reshape(3, 3)
                if solid.primitive == "box":
                    half = model.geom_size[index]
                    got = [tuple(centre + axes @ [sx * half[0], sy * half[1], sz * half[2]])
                           for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
                elif solid.primitive == "wedge":
                    mesh = model.geom_dataid[index]
                    start, count = model.mesh_vertadr[mesh], model.mesh_vertnum[mesh]
                    got = [tuple(centre + axes @ vertex) for vertex in model.mesh_vert[start:start + count]]
                else:
                    radius, half_h = model.geom_size[index][:2]
                    got = [tuple(centre + axes @ [sx * radius, sy * radius, sz * half_h])
                           for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
                with self.subTest(object=obj.id, solid=solid.name):
                    self.assertTrue(same_points(got, world_corners(obj, solid), tolerance=1e-4))
                checked.add((solid.primitive, bool(solid.roll_deg)))
        self.assertLessEqual({("box", False), ("cylinder", False), ("cylinder", True), ("wedge", False)}, checked)

    def test_visual_solids_take_no_part_in_contacts(self):
        recipe = load("car-test-course")
        model, _ = self.compile(recipe)
        lane = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, env_generate.geom_name("road", "lane-1"))
        slab = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, env_generate.geom_name("road", "slab"))
        self.assertEqual((model.geom_contype[lane], model.geom_conaffinity[lane]), (0, 0))
        self.assertEqual(model.geom_group[lane], env_generate.VISUAL_GROUP)
        # The road is on the surface layer: it collides with everything but other roads.
        self.assertEqual((model.geom_contype[slab], model.geom_conaffinity[slab]), (env_generate.SURFACE_CONTYPE, 1))

    def test_objects_stand_on_the_hills(self):
        recipe = load("hills-field")
        model, data = self.compile(recipe)
        for obj in recipe.objects:
            with self.subTest(object=obj.id):
                # Just beside the object's base the ground is not above it.
                ground, geom = self.ray_height(model, data, obj.pose.x_m, obj.pose.y_m)
                self.assertIn(geom, ("terrain", *[env_generate.geom_name(obj.id, s.name) for s in obj.solids]))
                self.assertLessEqual(recipe.terrain.height_at(obj.pose.x_m, obj.pose.y_m), obj.pose.z_m + 1e-6)

    def test_the_validation_world_has_free_bodies_and_boundaries(self):
        recipe = load("drone-practice-field")
        model, _ = self.compile(recipe, validation=True)
        body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "object:gate-1")
        self.assertEqual(model.body_dofnum[body], 6)
        for edge in ("north", "south", "east", "west"):
            self.assertGreaterEqual(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "boundary:" + edge), 0)
        static, _ = self.compile(recipe)
        self.assertEqual(static.body_dofnum[mujoco.mj_name2id(static, mujoco.mjtObj.mjOBJ_BODY, "object:gate-1")], 0)


class CommandTest(unittest.TestCase):
    def test_the_command_writes_glb_mjcf_and_a_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            self.assertEqual(env_generate.main([str(EXAMPLES / "drone-practice-field.yaml"), "--out-dir", str(out)]), 0)
            manifest = json.loads((out / "environment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["half_extent_m"], {"east_west": 10.0, "north_south": 15.0})
            self.assertEqual(manifest["objects"][0]["mjcf_body"], "object:gate-1")
            self.assertEqual(len(manifest["fingerprint"]), 64)
            self.assertEqual((out / "environment.glb").read_bytes()[:4], b"glTF")
            self.assertEqual(manifest["files"]["mjcf"]["path"], "environment.xml")


def _raw(name):
    import yaml

    return yaml.safe_load((EXAMPLES / f"{name}.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
