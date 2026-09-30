"""Map import (#10): OpenStreetMap (Overpass JSON) and GeoJSON to a Recipe,
from data made up here in local metres (no network)."""

import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_map  # noqa: E402
import env_polygon  # noqa: E402
import env_schema  # noqa: E402
import env_validate  # noqa: E402

BOX = env_map.Box.of(35.0, 135.0, 35.001, 135.0012)  # about 109 m x 111 m
FRAME = env_map.LocalFrame(BOX)
CATALOG = str(ROOT / "catalogs/starter/catalog.yaml")


def latlon(x, y):
    return (FRAME.lat0 + y / FRAME.north_per_deg, FRAME.lon0 + x / FRAME.east_per_deg)


class Overpass:
    """Builds Overpass JSON from shapes in local metres."""

    def __init__(self):
        self.elements, self.next_node = [], 1

    def nodes(self, points):
        ids = []
        for x, y in points:
            lat, lon = latlon(x, y)
            self.elements.append({"type": "node", "id": self.next_node, "lat": lat, "lon": lon})
            ids.append(self.next_node)
            self.next_node += 1
        return ids

    def way(self, way_id, points, tags, closed=False):
        ids = self.nodes(points)
        self.elements.append({"type": "way", "id": way_id, "nodes": ids + ids[:1] if closed else ids, "tags": tags})
        return way_id

    def data(self):
        return {"osm3s": {"timestamp_osm_base": "2026-09-01T00:00:00Z"}, "elements": self.elements}


L_SHAPE = [(-30, 10), (-10, 10), (-10, 15), (-25, 15), (-25, 30), (-30, 30)]


def sample():
    osm = Overpass()
    osm.way(101, L_SHAPE, {"building": "yes", "height": "12 m", "name": "L"}, closed=True)
    osm.way(102, [(0, 10), (10, 10), (10, 20), (0, 20)], {"building": "apartments", "building:levels": "4"}, closed=True)
    osm.way(103, [(20, 10), (30, 10), (30, 20), (20, 20)], {"building": "roof", "height": "6"}, closed=True)
    osm.way(104, [(45, -10), (70, -10), (70, 0), (45, 0)], {"building": "warehouse"}, closed=True)  # clipped at x 54.6
    osm.way(105, [(80, 0), (90, 0), (90, 10), (80, 10)], {"building": "house"}, closed=True)  # outside
    osm.way(106, [(0, -20), (1, -20), (1, -19), (0, -19)], {"building": "shed"}, closed=True)  # 1 m²: too small
    # A multipolygon courtyard building from two half rings (and an inner ring, not cut out).
    osm.way(201, [(-30, -40), (-10, -40), (-10, -20)], {})
    osm.way(202, [(-10, -20), (-30, -20), (-30, -40)], {})
    osm.way(203, [(-25, -35), (-15, -35), (-15, -25), (-25, -25), (-25, -35)], {})
    osm.elements.append({"type": "relation", "id": 300, "tags": {"type": "multipolygon", "building": "school"},
                         "members": [{"type": "way", "ref": 201, "role": "outer"},
                                     {"type": "way", "ref": 202, "role": "outer"},
                                     {"type": "way", "ref": 203, "role": "inner"}]})
    osm.way(401, [(-80, 0), (0, 0), (20, -30)], {"highway": "primary", "name": "Main"})  # leaves the area west
    osm.way(402, [(40, -60), (40, 60)], {"highway": "residential", "lanes": "6", "width": "8"})
    osm.way(403, [(-40, -50), (-40, 50)], {"highway": "footway"})  # not a road for cars
    osm.elements.append({"type": "way", "id": 107, "nodes": [9990, 9991, 9992, 9990], "tags": {"building": "yes"}})
    return osm.data()


class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.recipe, self.report = env_map.to_recipe(env_map.features_from_overpass(sample()), BOX, catalog=CATALOG,
                                                     data_timestamp="2026-09-01T00:00:00Z")
        self.objects = {obj["id"]: obj for obj in self.recipe["objects"]}

    def test_the_area_is_the_bbox_in_metres_about_its_centre(self):
        self.assertAlmostEqual(self.recipe["size_m"]["east"], 109.5, delta=0.3)
        self.assertAlmostEqual(self.recipe["size_m"]["north"], 110.9, delta=0.3)
        geo = self.recipe["geo"]
        self.assertEqual(geo["origin"], {"lat_deg": 35.0005, "lon_deg": 135.0006})
        self.assertEqual((geo["attribution"], geo["license"]), (env_map.ATTRIBUTION, "ODbL-1.0"))
        self.assertEqual(geo["data_timestamp"], "2026-09-01T00:00:00Z")

    def test_a_footprint_comes_back_in_metres(self):
        house = self.objects["building-101"]
        pose, points = house["pose"], house["params"]["footprint"]
        world = [(pose["x_m"] + x, pose["y_m"] + y) for x, y in points]
        self.assertEqual(len(world), 6)
        for (x, y), (ex, ey) in zip(sorted(world), sorted(L_SHAPE)):
            self.assertAlmostEqual(x, ex, delta=0.01)
            self.assertAlmostEqual(y, ey, delta=0.01)
        self.assertEqual(house["params"]["height_m"], 12.0)
        self.assertEqual(house["source"], {"provider": "openstreetmap", "kind": "way", "id": "101",
                                           "tags": {"building": "yes", "name": "L", "height": "12 m"}})

    def test_missing_heights_and_widths_are_filled_by_fixed_defaults(self):
        self.assertEqual(self.objects["building-102"]["params"]["height_m"], 12.0)  # 4 levels x 3 m
        self.assertEqual(self.objects["building-104"]["params"]["height_m"], env_map.HEIGHTS_BY_KIND["warehouse"])
        canopy = self.objects["building-103"]["params"]
        self.assertEqual((canopy["height_m"], canopy["min_height_m"]), (6.0, 6.0 - env_map.ROOF_SLAB_M))
        main = self.objects["road-401"]["params"]
        self.assertEqual((main["lanes"], main["width_m"]), (2, 2 * env_map.LANE_WIDTH_M))
        side = self.objects["road-402"]["params"]
        self.assertEqual((side["lanes"], side["width_m"]), (env_map.MAX_LANES, 8.0))  # lanes 6 drawn as 4
        self.assertEqual(self.report.assumed, {"building_height": 3, "road_width": 1, "road_lanes": 1})

    def test_features_are_clipped_to_the_area_or_left_out(self):
        warehouse = self.objects["building-104"]
        xs = [warehouse["pose"]["x_m"] + x for x, _ in warehouse["params"]["footprint"]]
        self.assertAlmostEqual(max(xs), self.recipe["size_m"]["east"] / 2, delta=0.002)
        # Roads end half their width inside the edge.
        main = self.objects["road-401"]
        west = min(main["pose"]["x_m"] + x for x, _ in main["params"]["centerline"])
        self.assertAlmostEqual(west, -self.recipe["size_m"]["east"] / 2 + main["params"]["width_m"] / 2, delta=0.01)
        skipped = {item["source"]: item["reason"] for item in self.report.skipped}
        self.assertEqual(set(skipped), {"way/105", "way/106", "way/107"})
        self.assertNotIn("road-403", self.objects)  # a footway is no road
        self.assertEqual((self.report.buildings, self.report.roads), (5, 2))

    def test_a_multipolygon_is_joined_and_its_courtyard_noted(self):
        school = self.objects["building-r300"]
        area = abs(env_polygon.signed_area([tuple(p) for p in school["params"]["footprint"]]))
        self.assertAlmostEqual(area, 400.0, delta=0.5)
        self.assertTrue(any("relation/300" in note for note in self.report.notes))

    def test_the_recipe_is_valid_and_deterministic(self):
        parsed = env_schema.parse_recipe(self.recipe, ROOT / "work/map.yaml")
        self.assertEqual(len(parsed.objects), 7)
        again, _ = env_map.to_recipe(env_map.features_from_overpass(sample()), BOX, catalog=CATALOG,
                                     data_timestamp="2026-09-01T00:00:00Z")
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.recipe, sort_keys=True))

    @unittest.skipUnless(env_validate.available(), "MuJoCo is not installed")
    def test_the_imported_world_passes_the_physical_checks(self):
        parsed = env_schema.parse_recipe(self.recipe, ROOT / "work/map.yaml")
        self.assertEqual([item.as_json() for item in env_validate.check(parsed)], [])


class GeoJsonTest(unittest.TestCase):
    def test_buildings_and_roads_from_a_feature_collection(self):
        ring = [list(reversed(latlon(x, y))) for x, y in [(0, 0), (10, 0), (10, 8), (0, 8), (0, 0)]]
        road = [list(reversed(latlon(x, y))) for x, y in [(-30, -20), (30, -20)]]
        data = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "way/7", "properties": {"building": "office", "height": 30},
             "geometry": {"type": "Polygon", "coordinates": [ring]}},
            {"type": "Feature", "properties": {"highway": "tertiary"},
             "geometry": {"type": "LineString", "coordinates": road}},
            {"type": "Feature", "properties": {"natural": "tree"}, "geometry": {"type": "Point", "coordinates": ring[0]}},
        ]}
        recipe, report, _ = env_map.import_map(BOX, geojson=data, catalog=CATALOG)
        ids = [obj["id"] for obj in recipe["objects"]]
        self.assertEqual(ids, ["building-7", "road-1"])
        self.assertEqual(recipe["objects"][0]["params"]["height_m"], 30.0)
        self.assertEqual(recipe["geo"]["provider"], "geojson")
        self.assertNotIn("attribution", recipe["geo"])
        env_schema.parse_recipe(recipe, ROOT / "work/map.yaml")
        # Without a bbox the area is the data's own extent.
        self.assertAlmostEqual(env_map.geojson_box(data).west, latlon(-30, 0)[1])


class CommandTest(unittest.TestCase):
    def test_import_from_a_saved_overpass_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "osm.json"
            source.write_text(json.dumps(sample()), encoding="utf-8")
            out = Path(directory) / "recipes/map.yaml"
            bbox = f"{BOX.south},{BOX.west},{BOX.north},{BOX.east}"
            completed = subprocess.run([sys.executable, str(ROOT / "tools/env_map.py"), "--bbox", bbox,
                                        "--osm-json", str(source), "--out", str(out), "--json"],
                                       capture_output=True, text=True, check=False, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["buildings"], 5)
            recipe = env_schema.load_recipe(out)  # the catalog path is relative to the Recipe
            self.assertEqual(recipe.objects[0].source["provider"], "openstreetmap")
            self.assertEqual(recipe.geo["bbox_deg"], BOX.as_json())

    def test_a_too_large_area_is_refused(self):
        with self.assertRaises(env_map.DiagnosticError) as caught:
            env_map.to_recipe([], env_map.Box.of(35.0, 135.0, 35.1, 135.1))
        self.assertEqual(caught.exception.diagnostics[0].code, "out_of_range")

    def test_lengths_in_tags(self):
        self.assertEqual([env_map.parse_length(value) for value in ("12", "12 m", "12,5m", "10 ft", "tall", None)],
                         [12.0, 12.0, 12.5, 3.048, None, None])


if __name__ == "__main__":
    unittest.main()
