"""docs/map-import.md is the conversion spec: its tables must match tools/env_map.py."""

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_map  # noqa: E402

SPEC = (ROOT / "docs/map-import.md").read_text(encoding="utf-8")


def block(name: str) -> str:
    match = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", SPEC, re.S)
    assert match, f"docs/map-import.md has no {name} block"
    return match.group(1)


def table(name: str) -> dict[str, float]:
    return {key: float(value) for key, value in re.findall(r"^\| `([^`]+)` \| ([0-9.]+) \|$", block(name), re.M)}


class MapSpecTest(unittest.TestCase):
    def test_building_heights_by_kind(self):
        self.assertEqual(table("heights-by-kind"), env_map.HEIGHTS_BY_KIND)

    def test_lanes_by_highway_class(self):
        self.assertEqual(table("lanes-by-class"), {key: float(value) for key, value in env_map.LANES_BY_CLASS.items()})

    def test_excluded_highways(self):
        self.assertEqual(set(re.findall(r"`([^`]+)`", block("excluded-highways"))), env_map.EXCLUDED_HIGHWAYS)

    def test_constants_named_in_the_text(self):
        for text, value in (("**3 m**", env_map.LEVEL_HEIGHT_M), ("**9 m**", env_map.DEFAULT_HEIGHT_M),
                            ("**0.5 m**", env_map.ROOF_SLAB_M), ("**3.25 m**", env_map.LANE_WIDTH_M),
                            ("**4**", env_map.MAX_LANES), ("**2000 m**", env_map.MAX_SIDE_M),
                            ("**5 cm**", env_map.MIN_STEP_M * 100), ("**4 m²**", env_map.MIN_BUILDING_AREA_M2),
                            ("**1 m**", env_map.MIN_ROAD_LENGTH_M)):
            with self.subTest(text=text):
                self.assertIn(text, SPEC)
                self.assertEqual(float(re.search(r"[0-9.]+", text).group()), value)
        self.assertIn(f"{env_map.MIN_ROAD_WIDTH_M}〜{env_map.MAX_ROAD_WIDTH_M:g} m", SPEC)
        self.assertIn(env_map.overpass_query(env_map.Box.of(1, 2, 3, 4)).replace("1.0,2.0,3.0,4.0", "{s},{w},{n},{e}"), SPEC)


if __name__ == "__main__":
    unittest.main()
