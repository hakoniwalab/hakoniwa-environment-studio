import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_generate  # noqa: E402
import env_road_ribbon  # noqa: E402
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
    def test_ribbon_draws_three_six_broken_yellow_and_omits_none(self):
        with tempfile.TemporaryDirectory() as directory:
            ribbon = Path(directory) / "road.json"
            ribbon.write_text(json.dumps({
                "schema": "hakoniwa.road-ribbon/v1",
                "sections": [
                    {"s_m": 0, "points_m": [[-2, 0, 0], [0, 0, 0], [2, 0, 0]]},
                    {"s_m": 3, "points_m": [[-2, 3, 0], [0, 3, 0], [2, 3, 0]]},
                    {"s_m": 9, "points_m": [[-2, 9, 0], [0, 9, 0], [2, 9, 0]]},
                    {"s_m": 12, "points_m": [[-2, 12, 0], [0, 12, 0], [2, 12, 0]]},
                ],
                "lanes": [],
                "markings": [
                    {"boundary": 1, "start_s_m": 0, "end_s_m": 12, "color": "yellow",
                     "lines": [{"pattern": "dashed", "t_offset_m": 0, "width_m": 0.12,
                                "length_m": 3, "space_m": 6, "s_offset_m": 0}]},
                    {"boundary": 2, "start_s_m": 0, "end_s_m": 12, "color": "white", "lines": []},
                ],
            }), encoding="utf-8")
            shape = env_road_ribbon.load(ribbon, thickness_m=0.02, color="#444444",
                                         line_color="#ffffff")

        marks = [solid for solid in shape.solids if solid.name.startswith("mark-")]
        self.assertEqual([solid.name for solid in marks], ["mark-1-0-1", "mark-1-0-3"])
        self.assertTrue(all(solid.color == "#f1c232" for solid in marks))

    def test_rich_ribbon_draws_double_colored_marks_and_lane_kinds_without_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            ribbon = Path(directory) / "road.json"
            ribbon.write_text(json.dumps({
                "schema": "hakoniwa.road-ribbon/v1",
                "sections": [
                    {"s_m": 0, "points_m": [[-3, 0, 0], [0, 0, 0], [3, 0, 0]]},
                    {"s_m": 5, "points_m": [[-3, 5, 0], [0, 5, 0], [3, 5, 0]]},
                ],
                "lanes": [
                    {"id": 1, "kind": "sidewalk", "left_boundary": 0, "right_boundary": 1,
                     "speed_limit_kmh": 0},
                    {"id": -1, "kind": "biking", "left_boundary": 1, "right_boundary": 2,
                     "speed_limit_kmh": 20},
                ],
                "markings": [{"boundary": 1, "start_s_m": 0, "end_s_m": 5, "color": "yellow",
                              "lines": [
                                  {"pattern": "solid", "t_offset_m": -0.2, "width_m": 0.2,
                                   "length_m": 4, "space_m": 0, "s_offset_m": 0},
                                  {"pattern": "dashed", "t_offset_m": 0.2, "width_m": 0.1,
                                   "length_m": 4, "space_m": 4, "s_offset_m": 0},
                              ]}],
            }), encoding="utf-8")
            shape = env_road_ribbon.load(ribbon, thickness_m=0.02, color="#444444",
                                         line_color="#ffffff")
        by_prefix = lambda prefix: [solid for solid in shape.solids if solid.name.startswith(prefix)]
        self.assertEqual(len(by_prefix("lane-")), 2)
        self.assertEqual(len(by_prefix("mark-")), 2)
        self.assertTrue(all(not solid.collide for solid in by_prefix("lane-") + by_prefix("mark-")))
        self.assertTrue(all(solid.color == "#f1c232" for solid in by_prefix("mark-")))
        for mark in by_prefix("mark-"):   # a flat stripe of the line's width, not a vertical sliver
            top = mark.vertices[:4]
            self.assertAlmostEqual(max(p[2] for p in top) - min(p[2] for p in top), 0.0, places=9)
            width = math.dist(top[0], top[1])
            self.assertTrue(any(abs(width - w) < 1e-9 for w in (0.2, 0.1)), width)

        # Its look: one mesh per colour, only the faces seen from above, shared vertices
        # (a large network drawn per column was a 1 GB GLB of 850,000 primitives).
        pieces = env_generate.ribbon_top_pieces(shape.solids)
        colours = {solid.color for solid in shape.solids if solid.visible}
        drawn = {color.split("@")[0] for _, color in pieces} - {env_generate.RIBBON_SIDE_COLOR}
        self.assertEqual(sorted(drawn), sorted(colours))                  # decals carry "@<level>"
        for (positions, normals, indices), color in pieces:
            if color == env_generate.RIBBON_SIDE_COLOR:
                continue                                                    # the outer edges' sides
            self.assertTrue(all(normals[i][2] > 0.1 for i in indices))      # no inner sides or bottoms
            self.assertLess(len(positions), len(indices))                     # vertices are shared
        drawn = sum(len(indices) // 3 for (_, _, indices), color in pieces if color != env_generate.RIBBON_SIDE_COLOR)
        whole = sum(len(env_generate.solid_mesh(solid)[2]) // 3 for solid in shape.solids if solid.visible)
        self.assertLessEqual(drawn * 4, whole)                               # tops are 2 of 12 triangles

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

    def test_ribbon_look_has_edge_sides_and_no_hidden_top(self):
        # An elevated ribbon (z = 5 m) whose lanes cover the whole width: the slab's top is not
        # drawn under the lane faces (it flickered), the lane faces are decals (drawn over the
        # road), and the two outer edges get sides as deep as the surface (no wall to the ground).
        with tempfile.TemporaryDirectory() as directory:
            ribbon = Path(directory) / "road.json"
            ribbon.write_text(json.dumps({"schema": "hakoniwa.road-ribbon/v1", "sections": [
                {"s_m": s, "points_m": [[-2, s, 5.0], [2, s, 5.0]]} for s in (0, 5, 10)],
                "lanes": [{"id": -1, "kind": "driving", "left_boundary": 0, "right_boundary": 1,
                           "speed_limit_kmh": 50}]}), encoding="utf-8")
            shape = env_road_ribbon.load(ribbon, thickness_m=0.02, color="#444444", line_color="#ffffff")
        self.assertFalse(any(solid.visible for solid in shape.solids if solid.name.startswith("slab-")))
        self.assertTrue(all(solid.collide for solid in shape.solids if solid.name.startswith("slab-")))
        pieces = dict((color, mesh) for mesh, color in env_generate.ribbon_top_pieces(shape.solids))
        positions, normals, indices = pieces[env_generate.RIBBON_SIDE_COLOR]
        self.assertEqual(len(indices) // 3, 2 * 2 * 2)                     # 2 columns x 2 edges x 2 triangles
        self.assertAlmostEqual(min(p[2] for p in positions), 5.0 - 0.02)
        self.assertTrue(any(key.endswith("@1") for key in pieces))          # the lane face is a decal
        self.assertEqual(sorted({round(n[0]) for n in normals}), [-1, 1])  # facing out on both sides

    def test_drawing_only_sections_cut_the_lanes_not_the_collision_pieces(self):
        # A dashed line's end ("collider": false) cuts the lane faces and lines, while the road's
        # collision piece spans it: one slab over 0..10 m, two lane faces.
        with tempfile.TemporaryDirectory() as directory:
            ribbon = Path(directory) / "road.json"
            sections = [{"s_m": s, "points_m": [[-2, s, 0.0], [2, s, 0.0]]} for s in (0, 5, 10)]
            sections[1]["collider"] = False
            ribbon.write_text(json.dumps({"schema": "hakoniwa.road-ribbon/v1", "sections": sections,
                "lanes": [{"id": -1, "kind": "driving", "left_boundary": 0, "right_boundary": 1,
                           "speed_limit_kmh": 50}]}), encoding="utf-8")
            shape = env_road_ribbon.load(ribbon, thickness_m=0.02, color="#444444", line_color="#ffffff")
            sections[1]["collider"] = True
            ribbon.write_text(json.dumps({"schema": "hakoniwa.road-ribbon/v1", "sections": sections}),
                              encoding="utf-8")
            with self.assertRaises(Exception):
                env_road_ribbon.load(ribbon, thickness_m=0.02, color="#444444", line_color="#ffffff")
        slabs = [solid for solid in shape.solids if solid.name.startswith("slab-")]
        self.assertEqual(len(slabs), 1)
        self.assertEqual(sorted(round(v[1]) for v in slabs[0].vertices[:4]), [0, 0, 10, 10])
        self.assertEqual(len([solid for solid in shape.solids if solid.name.startswith("lane-")]), 2)

    def test_ribbon_plan_outline_is_its_edges(self):
        # The 2-D plan draws a road_ribbon from its file's outline (it showed nothing before).
        with tempfile.TemporaryDirectory() as directory:
            ribbon = Path(directory) / "road.json"
            ribbon.write_text(json.dumps({"schema": "hakoniwa.road-ribbon/v1", "sections": [
                {"s_m": s, "points_m": [[-2, s, 0.1 * s], [0, s, 0.1 * s], [2, s, 0.1 * s]]} for s in range(0, 11)]}),
                encoding="utf-8")
            plan = env_road_ribbon.plan_outline(ribbon)
        self.assertEqual(len(plan["outline"]), 22)                         # left edge out, right edge back
        self.assertEqual(plan["outline"][0], [-2, 0]); self.assertEqual(plan["outline"][10], [-2, 10])
        self.assertEqual(plan["outline"][11], [2, 10]); self.assertEqual(plan["outline"][-1], [2, 0])
        self.assertEqual(plan["z_range_m"], [0, 1.0])

    def test_road_ribbon_enables_multiccd_only_for_that_representation(self):
        ordinary = ET.fromstring(env_generate.environment_mjcf(load("car-test-course")))
        self.assertIsNone(ordinary.find("./option/flag[@multiccd='enable']"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ribbon = root / "road.json"
            ribbon.write_text(json.dumps({
                "schema": "hakoniwa.road-ribbon/v1",
                "sections": [
                    {"s_m": 0, "points_m": [[-2, 0, 0], [0, 0, 0], [2, 0, 0]]},
                    {"s_m": 5, "points_m": [[-2, 5, 0], [0, 5, 0], [2, 5, 0]]},
                ],
                "lanes": [
                    {"id": 1, "kind": "driving", "left_boundary": 0, "right_boundary": 1, "speed_limit_kmh": 50},
                    {"id": -1, "kind": "sidewalk", "left_boundary": 1, "right_boundary": 2, "speed_limit_kmh": 0},
                ],
                "markings": [{"boundary": 1, "start_s_m": 0, "end_s_m": 5, "color": "white",
                              "lines": [{"pattern": "solid", "t_offset_m": 0, "width_m": 0.12,
                                         "length_m": 0, "space_m": 0, "s_offset_m": 0}]}],
            }), encoding="utf-8")
            recipe_path = root / "ribbon.yaml"
            recipe_path.write_text(
                "\n".join((
                    "schema: hakoniwa.environment-recipe/v1",
                    "name: ribbon",
                    f"catalog: {(ROOT / 'catalogs/starter/catalog.yaml').as_posix()}",
                    "size_m: {east: 10, north: 10}",
                    "terrain: {item: asphalt-ground}",
                    "objects:",
                    "  - id: road",
                    "    item: road-ribbon",
                    "    pose: {x_m: 0, y_m: 0}",
                    "    params: {ribbon_file: road.json}",
                )) + "\n",
                encoding="utf-8",
            )
            model = ET.fromstring(
                env_generate.environment_mjcf(env_schema.load_recipe(recipe_path))
            )
            self.assertIsNotNone(model.find("./option/flag[@multiccd='enable']"))
            # Its road marks are drawn by the GLB only: no collision-free geoms in the MJCF.
            road_geoms = [geom for geom in model.iter("geom") if geom.get("name", "").startswith("geom:road/")]
            self.assertTrue(road_geoms)
            self.assertFalse([geom.get("name") for geom in road_geoms if geom.get("contype") == "0"])

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
