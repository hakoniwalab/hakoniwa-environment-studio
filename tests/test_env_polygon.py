"""Footprints and centre lines (#10): polygons, the prism and ribbon shapes,
the surface layer, and the MuJoCo world made from them."""

import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_generate  # noqa: E402
import env_polygon as polygon  # noqa: E402
import env_schema  # noqa: E402
import env_types  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

try:
    import mujoco
    import numpy as np
except ModuleNotFoundError:
    mujoco = None

L_SHAPE = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
U_SHAPE = [(0, 0), (9, 0), (9, 9), (6, 9), (6, 3), (3, 3), (3, 9), (0, 9)]


class PolygonTest(unittest.TestCase):
    def test_concave_rings_split_into_convex_pieces_of_the_same_area(self):
        for ring in (L_SHAPE, U_SHAPE):
            pieces = polygon.convex_pieces(ring)
            self.assertGreater(len(pieces), 1)
            self.assertTrue(all(polygon.is_convex(piece) for piece in pieces))
            self.assertAlmostEqual(sum(polygon.signed_area(piece) for piece in pieces), polygon.signed_area(ring))
        self.assertEqual(polygon.convex_pieces([(0, 0), (1, 0), (1, 1), (0, 1)]), [[(0, 0), (1, 0), (1, 1), (0, 1)]])

    def test_simple_rings_and_cleaning(self):
        self.assertTrue(polygon.is_simple(L_SHAPE))
        self.assertFalse(polygon.is_simple([(0, 0), (2, 2), (2, 0), (0, 2)]))  # a bow tie
        self.assertEqual(polygon.cleaned([(0, 0), (5, 0), (10, 0), (10, 10), (0, 10), (0, 0)], True),
                         [(0, 0), (10, 0), (10, 10), (0, 10)])

    def test_clipping_to_a_box(self):
        self.assertEqual(polygon.clip_polyline([(-5, 0), (5, 0), (5, 20)], (-2, -2, 8, 8)),
                         [[(-2.0, 0.0), (5.0, 0.0), (5.0, 8.0)]])
        # A line leaving and coming back is two parts.
        parts = polygon.clip_polyline([(0, 0), (20, 0), (20, 5), (0, 5)], (-1, -1, 10, 10))
        self.assertEqual(len(parts), 2)
        clipped = polygon.clip_polygon(L_SHAPE, (-1, -1, 5, 5))
        self.assertAlmostEqual(abs(polygon.signed_area(clipped)), 24.0)

    def test_convex_overlap_allows_touching(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        self.assertFalse(polygon.convex_overlap(square, [(1, 0), (2, 0), (2, 1), (1, 1)]))
        self.assertTrue(polygon.convex_overlap(square, [(0.5, 0.5), (2, 0.5), (2, 2), (0.5, 2)]))


class ShapeTest(unittest.TestCase):
    def setUp(self):
        self.library = env_types.load_types()

    def test_a_footprint_parameter_is_checked_and_turned_counter_clockwise(self):
        param = self.library.types["building_footprint"].params["footprint"]
        clockwise = [[0, 10], [4, 10], [4, 4], [10, 4], [10, 0], [0, 0]]
        self.assertGreater(polygon.signed_area([tuple(p) for p in param.check(clockwise, "p")]), 0)
        for bad, code in (([[0, 0], [2, 2], [2, 0], [0, 2]], "invalid_shape"), ([[0, 0], [1, 1]], "invalid_shape"),
                          ("square", "wrong_type"), ([[0, 0], [1, "a"], [1, 1]], "wrong_type")):
            with self.subTest(bad=bad), self.assertRaises(DiagnosticError) as caught:
                param.check(bad, "p")
            self.assertEqual(caught.exception.diagnostics[0].code, code)

    def test_a_concave_building_is_convex_prisms(self):
        building = self.library.types["building_footprint"]
        shape = env_types.resolve_shape(building, {**env_types.defaults(building),
                                                   "footprint": [list(p) for p in L_SHAPE], "height_m": 12.0}, "b")
        self.assertEqual([solid.name for solid in shape.solids], ["body-1", "body-2"])
        self.assertTrue(all(solid.primitive == "prism" and solid.height_m == 12.0 for solid in shape.solids))
        area = sum(polygon.signed_area(list(solid.points)) for solid in shape.solids)
        self.assertAlmostEqual(area, 64.0)
        self.assertEqual(shape.height_m, 12.0)

    def test_a_road_is_a_box_per_segment_with_the_bends_filled(self):
        road = self.library.types["road_path"]
        line = [[0, 0], [10, 0], [10, 10]]
        shape = env_types.resolve_shape(road, {**env_types.defaults(road), "centerline": line, "lanes": 1}, "r")
        slabs = {solid.name: solid for solid in shape.solids if solid.name.startswith("slab")}
        self.assertEqual(sorted(slabs), ["slab-1", "slab-2", "slab-joint-1"])
        first = slabs["slab-1"]
        self.assertEqual((first.x_m, first.y_m, first.depth_m, first.width_m, first.yaw_deg), (5, 0, 10, 7, -90))
        self.assertEqual((slabs["slab-joint-1"].primitive, slabs["slab-joint-1"].width_m), ("cylinder", 7))
        # An edge line runs 3.3 m to the right of the centre line (south of an eastward segment).
        edge = next(solid for solid in shape.solids if solid.name == "edge-right-1")
        self.assertAlmostEqual(edge.y_m, -3.3)
        self.assertEqual(shape.layer, "surface")


class SurfaceLayerTest(unittest.TestCase):
    def setUp(self):
        self.recipe = env_schema.load_recipe(ROOT / "tests/fixtures/map-shapes.yaml")
        self.objects = {obj.id: obj for obj in self.recipe.objects}

    def test_objects_stand_on_the_roads_under_them(self):
        self.assertEqual(self.objects["cone"].pose.z_m, 0.02 + env_schema.SURFACE_GAP_M)  # on the road slab
        self.assertEqual(self.objects["main"].pose.z_m, 0.0)
        # The L-shaped building's corner reaches over the crossing road.
        self.assertEqual(self.objects["l-house"].pose.z_m, 0.02 + env_schema.SURFACE_GAP_M)

    @unittest.skipUnless(mujoco, "MuJoCo is not installed")
    def test_roads_cross_without_problems_and_the_world_matches_the_footprint(self):
        import env_validate

        self.assertEqual(env_validate.check(self.recipe), [])
        model = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(self.recipe))
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)

        def top(x, y):
            geom = np.zeros(1, dtype=np.int32)
            distance = mujoco.mj_ray(model, data, np.array([x, y, 100.0]), np.array([0, 0, -1.0]), None, 1, -1, geom)
            name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, int(geom[0])) or ""
            return round(100.0 - distance, 3), name

        house = self.objects["l-house"]
        yaw = math.radians(house.pose.yaw_deg)

        def world(x, y):
            return (house.pose.x_m + math.cos(yaw) * x - math.sin(yaw) * y,
                    house.pose.y_m + math.sin(yaw) * x + math.cos(yaw) * y)

        # Inside the L: the roof, 12 m above its base; in the notch: the ground.
        height, name = top(*world(-4, 4))
        self.assertAlmostEqual(height, 12.02, delta=0.002)
        self.assertEqual(name.split("/")[0], "geom:l-house")
        height, name = top(*world(3, 3))
        self.assertEqual(name, "terrain")
        # On the bend of the main road: the joint's cylinder.
        self.assertEqual(top(0, -5)[1].split("/")[0], "geom:main")


if __name__ == "__main__":
    unittest.main()
