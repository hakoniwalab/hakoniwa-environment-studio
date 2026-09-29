from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_terrain  # noqa: E402
from env_diagnostics import DiagnosticError  # noqa: E402

HILLS = {"max_height_m": 2.0, "hills": 5, "radius_m": 3.0, "seed": 3, "resolution_m": 0.5}
SETTINGS = {"kind": "hfield", "generator": "hills", "color": "#7a8b5a", "friction": 1.0}


def hills(**params):
    return env_terrain.make_terrain(SETTINGS, {**HILLS, **params}, 20.0, 30.0, "terrain.params")


class TerrainTest(unittest.TestCase):
    def test_flat_ground_is_at_zero(self):
        flat = env_terrain.make_terrain({**SETTINGS, "kind": "flat", "generator": "flat"}, {}, 20, 30, "t")
        self.assertEqual((flat.kind, flat.height_at(3, 4), flat.highest_under([(0, 0), (1, 0), (1, 1)])), ("flat", 0.0, 0.0))

    def test_hills_are_deterministic_and_bounded(self):
        a, b = hills(), hills()
        self.assertEqual(a.heights, b.heights)
        self.assertNotEqual(a.heights, hills(seed=4).heights)
        self.assertEqual((a.nrow, a.ncol), (61, 41))  # 30 m and 20 m at 0.5 m
        self.assertLessEqual(a.max_height_m, 2.0)
        self.assertGreater(a.max_height_m, 0.5)

    def test_grid_points_match_the_heights(self):
        terrain = hills()
        # Row 0 is the north edge, column 0 the west edge.
        self.assertAlmostEqual(terrain.height_at(-10, 15), terrain.heights[0][0])
        self.assertAlmostEqual(terrain.height_at(10, -15), terrain.heights[-1][-1])
        self.assertAlmostEqual(terrain.height_at(-10 + 0.5, 15 - 1.0), terrain.heights[2][1])
        # Halfway between two grid points is their mean.
        mean = (terrain.heights[2][1] + terrain.heights[2][2]) / 2
        self.assertAlmostEqual(terrain.height_at(-10 + 0.75, 15 - 1.0), mean)

    def test_the_highest_ground_under_an_outline(self):
        terrain = hills()
        square = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        top = terrain.highest_under(square)
        self.assertGreaterEqual(top, max(terrain.height_at(x, y) for x, y in square))
        # No grid point inside the square is higher.
        for row in range(terrain.nrow):
            for col in range(terrain.ncol):
                x, y = -10 + col * 0.5, 15 - row * 0.5
                if -1 <= x <= 1 and -1 <= y <= 1:
                    self.assertLessEqual(terrain.heights[row][col], top + 1e-9)

    def test_too_fine_a_grid_is_refused(self):
        with self.assertRaises(DiagnosticError) as caught:
            hills(resolution_m=0.001)
        self.assertEqual((caught.exception.diagnostics[0].path, caught.exception.diagnostics[0].code),
                         ("terrain.params.resolution_m", "out_of_range"))


if __name__ == "__main__":
    unittest.main()
