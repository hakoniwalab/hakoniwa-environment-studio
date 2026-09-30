from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_types  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402


def library_from(text: str) -> env_types.TypeLibrary:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "types.yaml"
        path.write_text("schema: hakoniwa.environment-types/v1\ntypes:\n" + textwrap.indent(textwrap.dedent(text), "  "),
                        encoding="utf-8")
        return env_types.load_types(Path(directory))


def resolved(library, type_id, **params):
    env_type = library.types[type_id]
    return env_types.resolve_shape(env_type, {**env_types.defaults(env_type), **params}, type_id)


BOX = """
- id: box
  params:
    width_m: {kind: length, default: 1}
    depth_m: {kind: length, default: 1}
    height_m: {kind: length, default: 1}
  shapes:
    - {name: body, primitive: box, w: $width_m, d: $depth_m, h: $height_m}
"""


class ShippedTypesTest(unittest.TestCase):
    def setUp(self):
        self.library = env_types.load_types()

    def test_the_starter_types_cover_the_mvp_catalog(self):
        placeable = {key for key, value in self.library.types.items() if not value.abstract}
        self.assertEqual(placeable, {
            "flat_ground", "hills_terrain", "wall", "building_block", "box_obstacle", "pylon", "gate", "ramp",
            "platform", "landing_pad", "road_surface", "road_marking", "guard_rail", "road_sign", "traffic_signal",
            "road_path", "building_footprint"})
        self.assertTrue(self.library.types["hills_terrain"].is_terrain)
        self.assertTrue(self.library.types["landing_pad"].is_a("platform"))

    def test_every_type_describes_itself(self):
        for env_type in self.library.types.values():
            with self.subTest(type=env_type.id):
                described = env_type.as_json()
                self.assertTrue(described["description"])
                for param in described["params"]:
                    self.assertIn(param["level"], ("type", "item", "placement"))
                    if param["kind"] == "length":
                        self.assertEqual(param["unit"], "m")

    def test_a_gate_opening_and_its_bottom_bar(self):
        low = resolved(self.library, "gate")
        self.assertEqual([solid.name for solid in low.solids], ["post-left", "post-right", "top"])
        raised = resolved(self.library, "gate", clearance_m=0.5)
        bottom = next(solid for solid in raised.solids if solid.name == "bottom")
        self.assertAlmostEqual(bottom.z_m + bottom.height_m / 2, 0.5)  # its top is the opening's bottom
        self.assertAlmostEqual(raised.height_m, 0.5 + 1.5 + 0.1)

    def test_a_ramp_is_a_wedge_rising_along_y(self):
        ramp = resolved(self.library, "ramp", width_m=2.0, length_m=4.0, height_m=0.5)
        (body,) = ramp.solids
        self.assertEqual(body.primitive, "wedge")
        corners = body.corners()
        top = [corner for corner in corners if abs(corner[2] - 0.5) < 1e-9]
        self.assertTrue(all(abs(y - 2.0) < 1e-9 for _, y, _ in top))  # the high edge is at +y
        self.assertEqual(len(corners), 6)

    def test_a_round_sign_plate_is_a_disc_tipped_to_face_y(self):
        sign = resolved(self.library, "road_sign")
        plate = next(solid for solid in sign.solids if solid.name == "plate")
        self.assertEqual((plate.primitive, plate.roll_deg), ("cylinder", 90.0))
        ys = [y for _, y, _ in plate.corners()]
        self.assertAlmostEqual(max(ys) - min(ys), 0.02)  # its thickness now lies along y

    def test_road_lane_lines_follow_the_number_of_lanes(self):
        for lanes, lines in ((1, 0), (2, 1), (3, 2), (4, 3)):
            with self.subTest(lanes=lanes):
                road = resolved(self.library, "road_surface", lanes=lanes)
                self.assertEqual(sum(1 for solid in road.solids if solid.name.startswith("lane-")), lines)
        markings = [solid for solid in resolved(self.library, "road_surface").solids if solid.name != "slab"]
        self.assertTrue(all(not solid.collide for solid in markings))


class ExpressionTest(unittest.TestCase):
    def test_functions_are_in_degrees(self):
        for text, expected in (("sin($a)", 1.0), ("cos($a)", 0.0), ("sqrt($b)", 3.0), ("abs(-$b)", 9)):
            with self.subTest(text=text):
                value = env_types.evaluate(env_types.compile_expression(text, "t"), {"a": 90, "b": 9}, "t")
                self.assertAlmostEqual(value, expected, places=9)

    def test_anything_else_is_refused_with_a_diagnostic(self):
        for text, code in (("$w + __import__('os')", "invalid_expression"), ("$w.real", "invalid_expression"),
                           ("$w +", "invalid_expression")):
            with self.subTest(text=text), self.assertRaises(DiagnosticError) as caught:
                env_types.compile_expression(text, "shapes[0].w")
            self.assertEqual((caught.exception.diagnostics[0].code, caught.exception.diagnostics[0].path),
                             (code, "shapes[0].w"))


class DefinitionTest(unittest.TestCase):
    def test_a_tilted_solid_reports_its_extent(self):
        library = library_from(BOX + """
- id: tilted
  params: {length_m: {kind: length, default: 4}}
  shapes:
    - {name: slab, primitive: box, w: 1, d: $length_m, h: 0.1, z: 2, roll: 30}
""")
        shape = resolved(library, "tilted")
        # A 4 m slab rolled 30 degrees about x: its y extent shrinks, its z grows.
        self.assertAlmostEqual(shape.envelope["depth_m"], 4 * 0.8660254 + 0.1 * 0.5, places=5)
        self.assertAlmostEqual(shape.height_m - shape.bottom_m, 4 * 0.5 + 0.1 * 0.8660254, places=5)

    def test_invalid_types_are_reported_with_paths(self):
        cases = {
            "extends itself": ("- {id: a, extends: b}\n- {id: b, extends: a}", "invalid_shape"),
            "undeclared": ("- id: a\n  shapes:\n    - {primitive: box, w: $width_m, d: 1, h: 1}", "unknown_reference"),
            "no shapes": ("- id: a\n  params: {x: {kind: length, default: 1}}", "missing_field"),
            "elevated without z": ("- id: a\n  behavior: {surface: elevated}\n  shapes:\n    - {primitive: box, w: 1, d: 1, h: 1}",
                                   "missing_field"),
            "unknown kind": ("- id: a\n  params: {s: {kind: area}}\n  shapes:\n    - {primitive: box, w: 1, d: 1, h: 1}",
                             "not_one_of"),
            "typo in a shape": ("- id: a\n  shapes:\n    - {primitive: box, w: 1, d: 1, h: 1, hight: 2}", "unknown_field"),
            "generator params": ("- id: t\n  terrain: {kind: hfield, generator: hills}", "missing_field"),
        }
        for name, (text, code) in cases.items():
            with self.subTest(case=name), self.assertRaises(DiagnosticError) as caught:
                library_from(text)
            self.assertEqual(caught.exception.diagnostics[0].code, code)

    def test_resolving_checks_the_solids(self):
        library = library_from("""
- id: round
  params: {w: {kind: length, default: 1}, d: {kind: length, default: 1}}
  shapes:
    - {primitive: cylinder, w: $w, d: $d, h: 1}
""")
        with self.assertRaises(DiagnosticError) as caught:
            resolved(library, "round", w=1.0, d=0.8)
        self.assertEqual(caught.exception.diagnostics[0].code, "invalid_shape")

    def test_parameters_are_checked_by_kind_and_range(self):
        param = env_types.Param("lanes", "integer", "placement", "車線", default=2, min=1, max=4, values=(1, 2, 3, 4))
        self.assertEqual(param.check(3, "p"), 3)
        for value, code in ((5, "not_one_of"), (2.5, "wrong_type"), (True, "wrong_type")):
            with self.subTest(value=value), self.assertRaises(DiagnosticError) as caught:
                param.check(value, "p")
            self.assertEqual(caught.exception.diagnostics[0].code, code)
        length = env_types.Param("w", "length", "placement", "幅", min=0.1, max=10)
        self.assertEqual(length.check(2, "p"), 2.0)  # metres as floats


if __name__ == "__main__":
    unittest.main()
