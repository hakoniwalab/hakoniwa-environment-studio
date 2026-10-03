"""What PLATEAU has in a selection (tools/env_plateau.py), with a stand-in for
hakoniwa-envsim's catalog client (no network), and the build conditions the
map page offers (env_cityworld.build_options / build_config)."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_cityworld  # noqa: E402
import env_plateau  # noqa: E402


class FakeClient:
    """Answers the catalog with the files given per feature type."""

    def __init__(self, files):
        self.files = files

    def bounding_box(self, lat, lon, ns, ew):
        return (lon - 0.001, lat - 0.001, lon + 0.001, lat + 0.001)

    def third_mesh_codes(self, bbox):
        return ["64414278"]

    def third_mesh_bounds(self, code):
        return (141.35, 43.0667, 141.3625, 43.075)

    def search_url(self, api, feature_type, bbox, mesh_level=3):
        return feature_type

    def request_catalog(self, url, allow_not_found=False):
        return url

    def select_files(self, feature_type, _type, year, allow_empty=False, min_lod=1):
        return self.files.get(feature_type, [])


def file(feature, lod, code="64414278001", size=1000):
    return {"url": f"https://example/{feature}/{code}", "city_code": "01100", "city_name": "札幌市",
            "year": 2020, "spec": "3.5", "max_lod": lod, "file_size": size, "code": code}


class PlateauInspectionTest(unittest.TestCase):
    def test_an_area_with_buildings_terrain_and_roads_can_be_built(self):
        client = FakeClient({"bldg": [file("bldg", 2)], "dem": [file("dem", 1)], "tran": [file("tran", 1)],
                             "frn": [file("frn", 1)]})
        result = env_plateau.inspect((43.0668, 141.351), (100, 100), client)
        self.assertEqual((result["status"], result["reason"]), ("available", None))
        self.assertEqual(result["capabilities"]["building"]["max_lod"], 2)
        # Road markings below LOD3 are there, but the builder does not use them.
        self.assertEqual((result["capabilities"]["road_markings"]["dataset_status"],
                          result["capabilities"]["road_markings"]["generation_status"]), ("available", "scoped_out"))
        self.assertEqual(result["municipalities"],
                         [{"city_code": "01100", "city": "札幌市", "year": 2020, "spec": "3.5"}])
        self.assertEqual((result["source_file_count"], result["estimated_download_bytes"]), (4, 4000))
        self.assertEqual(result["building_municipalities"], ["札幌市"])
        # The third meshes asked about, to be drawn on the map.
        self.assertEqual(result["query_meshes"], [{"code": "64414278", "bbox": {
            "west": 141.35, "south": 43.0667, "east": 141.3625, "north": 43.075}}])

    def test_an_area_without_roads_is_not_available(self):
        result = env_plateau.inspect((35.36, 138.73), (100, 100),
                                     FakeClient({"bldg": [file("bldg", 1)], "dem": [file("dem", 1)]}))
        self.assertEqual((result["status"], result["missing"], result["flat_ground_possible"]),
                         ("unavailable", ["road"], False))
        self.assertIn("road", result["reason"])

    def test_an_area_without_terrain_can_be_built_on_flat_ground(self):
        result = env_plateau.inspect((35.0, 139.0), (100, 100),
                                     FakeClient({"bldg": [file("bldg", 1)], "tran": [file("tran", 1)]}))
        self.assertEqual((result["status"], result["flat_ground_possible"]), ("unavailable", True))

    def test_bridges_at_lod2_are_built_road_markings_at_lod2_are_not(self):
        # Many cities publish bridges at LOD2 only; the generator draws them
        # (and their floor surfaces collide). Road markings exist only at LOD3.
        client = FakeClient({"bldg": [file("bldg", 2)], "dem": [file("dem", 1)], "tran": [file("tran", 2)],
                             "frn": [file("frn", 2)], "brid": [file("brid", 2, code="64414278002")]})
        capabilities = env_plateau.inspect((43.0, 141.3), (100, 100), client)["capabilities"]
        self.assertEqual((capabilities["bridge"]["generation_status"], capabilities["bridge"]["reason"]),
                         ("candidate", None))
        self.assertEqual(capabilities["road_markings"]["generation_status"], "scoped_out")
        self.assertIn("LOD3", capabilities["road_markings"]["reason"])
        lod1 = FakeClient({"bldg": [file("bldg", 2)], "dem": [file("dem", 1)], "tran": [file("tran", 2)],
                           "brid": [file("brid", 1, code="64414278002")]})
        bridge = env_plateau.inspect((43.0, 141.3), (100, 100), lod1)["capabilities"]["bridge"]
        self.assertEqual(bridge["generation_status"], "scoped_out")
        self.assertIn("LOD2", bridge["reason"])

    def test_bridges_outside_the_area_are_left_out(self):
        client = FakeClient({"bldg": [file("bldg", 1)], "dem": [file("dem", 1)], "tran": [file("tran", 1)],
                             "brid": [file("brid", 3, code="64414278002"), file("brid", 3, code="64414279001")]})
        self.assertEqual(env_plateau.inspect((43.0, 141.3), (100, 100), client)["capabilities"]["bridge"]["source_file_count"], 1)

    def test_a_catalog_that_does_not_answer_is_an_inspection_error(self):
        class Down(FakeClient):
            def request_catalog(self, url, allow_not_found=False):
                raise OSError("network is down")

        with self.assertRaisesRegex(env_plateau.InspectionError, "did not answer"):
            env_plateau.inspect((43.0, 141.3), (100, 100), Down({}))


class BuildConditionsTest(unittest.TestCase):
    def test_the_defaults_are_the_city_world_web_uis(self):
        # Physics Level 3, every collider reduction off, stop where the DEM does not cover.
        self.assertEqual(env_cityworld.build_options(None), {
            "building_physics_level": 3, "building_collider_reduction": "safe",
            "terrain_uncovered_policy": "error", "terrain_bridge_carve": False, "building_road_passage": False,
            "terrain_bridge_blend": False})

    def test_chosen_conditions_go_into_the_envsim_build(self):
        config = env_cityworld.build_config(Path("/job"), (43.0, 141.3), (100.0, 100.0), Path("/cache"), {
            "building_physics_level": 0, "building_collider_reduction": "safe", "terrain_uncovered_policy": "constant"})
        self.assertIn("building_physics_level: 0", config)
        self.assertIn("building_collider_reduction: safe", config)
        self.assertIn("terrain_uncovered_policy: constant", config)
        self.assertIn("terrain_bridge_carve: false", config)  # off unless chosen (an inference)
        carved = env_cityworld.build_config(Path("/job"), (43.0, 141.3), (100.0, 100.0), Path("/cache"),
                                            {"terrain_bridge_carve": True})
        self.assertIn("terrain_bridge_carve: true", carved)
        self.assertIn("building_road_passage: false", config)
        passage = env_cityworld.build_config(Path("/job"), (43.0, 141.3), (100.0, 100.0), Path("/cache"),
                                             {"building_road_passage": True})
        self.assertIn("building_road_passage: true", passage)
        self.assertIn("terrain_bridge_blend: false", config)
        blend = env_cityworld.build_config(Path("/job"), (43.0, 141.3), (100.0, 100.0), Path("/cache"),
                                           {"terrain_bridge_blend": True})
        self.assertIn("terrain_bridge_blend: true", blend)

    def test_a_condition_outside_envsims_values_is_refused(self):
        with self.assertRaisesRegex(env_cityworld.BuildError, "building_physics_level"):
            env_cityworld.build_options({"building_physics_level": 4})


if __name__ == "__main__":
    unittest.main()
