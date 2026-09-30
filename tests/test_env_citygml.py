"""CityGML -> parts (tools/env_citygml.py): CityGML made here from map data in
local metres by hakoniwa-envsim's osm2citygml, and Envsim's PLATEAU fixtures
(no network). Skipped when hakoniwa-envsim is not next to this repository."""

import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_citygml  # noqa: E402
import env_polygon  # noqa: E402
import env_schema  # noqa: E402
import env_validate  # noqa: E402

try:
    env_citygml.envsim_root()
    osm2citygml = env_citygml.osm2citygml()
    ENVSIM = True
except Exception:  # noqa: BLE001 - Envsim or its dependencies are missing
    ENVSIM = False

CATALOG = str(ROOT / "catalogs/starter/catalog.yaml")
L_SHAPE = [(-30, 10), (-10, 10), (-10, 15), (-25, 15), (-25, 30), (-30, 30)]


def box():
    return osm2citygml.Box.of(35.0, 135.0, 35.001, 135.0012)  # about 109 m x 111 m


def latlon(x, y):
    import geodesy

    lat, lon, _ = geodesy.local_enu_to_geodetic([(x, y, 0.0)], *box().center, 4326)[0]
    return lat, lon


def sample():
    """Overpass JSON: an L-shaped house, a canopy, a courtyard school, a
    building reaching past the east edge, two roads."""
    elements, next_node = [], [1]

    def way(way_id, points, tags, closed=False):
        ids = []
        for x, y in points:
            lat, lon = latlon(x, y)
            elements.append({"type": "node", "id": next_node[0], "lat": lat, "lon": lon})
            ids.append(next_node[0])
            next_node[0] += 1
        elements.append({"type": "way", "id": way_id, "nodes": ids + ids[:1] if closed else ids, "tags": tags})

    way(101, L_SHAPE, {"building": "yes", "height": "12", "name": "L"}, closed=True)
    way(103, [(20, 10), (30, 10), (30, 20), (20, 20)], {"building": "roof", "height": "6"}, closed=True)
    way(105, [(42, -10), (62, -10), (62, -4), (42, -4)], {"building": "warehouse"}, closed=True)  # centroid inside
    way(201, [(-30, -40), (-10, -40), (-10, -20), (-30, -20)], {}, closed=True)
    way(203, [(-25, -35), (-15, -35), (-15, -25), (-25, -25)], {}, closed=True)
    elements.append({"type": "relation", "id": 300, "tags": {"type": "multipolygon", "building": "school"},
                     "members": [{"type": "way", "ref": 201, "role": "outer"}, {"type": "way", "ref": 203, "role": "inner"}]})
    way(401, [(-45, 0), (0, 0), (20, -30)], {"highway": "primary"})
    way(402, [(40, -50), (40, 50)], {"highway": "residential", "width": "8"})
    return {"osm3s": {"timestamp_osm_base": "2026-09-01T00:00:00Z"}, "elements": elements}


@unittest.skipUnless(ENVSIM, "hakoniwa-envsim is not available")
class OsmPartsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.citygml = Path(cls.directory.name) / "citygml"
        receipt = osm2citygml.run(box(), cls.citygml, "map", osm_json=sample())
        # An Envsim build keeps copies of its sources: they are read once.
        (cls.citygml / "build/source/local").mkdir(parents=True)
        for path in cls.citygml.glob("*_op.gml"):
            shutil.copy(path, cls.citygml / "build/source/local" / path.name)
        selection = receipt["selection"]
        cls.half = (selection["half_extent_m"]["north_south"], selection["half_extent_m"]["east_west"])
        cls.recipe, cls.report = env_citygml.convert(cls.citygml, box().center, cls.half, catalog=CATALOG)
        cls.objects = {obj["id"]: obj for obj in cls.recipe["objects"]}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def world(self, obj, key="footprint"):
        pose = obj["pose"]
        return [(pose["x_m"] + x, pose["y_m"] + y) for x, y in obj["params"][key]]

    def test_one_part_per_building_and_road_surface(self):
        buildings = [obj for obj in self.recipe["objects"] if obj["item"] == "building-footprint"]
        roads = [obj for obj in self.recipe["objects"] if obj["item"] == "road-area"]
        self.assertEqual(sorted(obj["id"] for obj in buildings), ["osm_r300", "osm_w101", "osm_w103", "osm_w105"])
        self.assertEqual({obj["source"]["id"].rsplit("-", 2)[0] for obj in roads}, {"osm_w401", "osm_w402"})
        self.assertEqual((self.report["buildings"], self.report["roads"]), (4, len(roads)))

    def test_a_building_keeps_its_footprint_heights_and_source(self):
        house = self.objects["osm_w101"]
        for expected in L_SHAPE:
            self.assertLess(min(math.dist(expected, point) for point in self.world(house)), 0.005)
        self.assertEqual(house["params"]["height_m"], 12.0)
        self.assertNotIn("min_height_m", house["params"])
        source = house["source"]
        self.assertEqual((source["provider"], source["kind"], source["id"], source["note"]),
                         ("openstreetmap", "citygml", "osm_w101", "map_bldg_op.gml"))
        self.assertEqual((source["tags"]["name"], source["tags"]["osm:height"], source["tags"]["source_id"]),
                         ("L", "12", "101"))

    def test_a_canopy_stays_raised_and_courtyards_are_filled(self):
        canopy = self.objects["osm_w103"]["params"]
        self.assertEqual((canopy["height_m"], canopy["min_height_m"]), (6.0, 5.5))
        school = self.objects["osm_r300"]
        area = abs(env_polygon.signed_area([tuple(p) for p in school["params"]["footprint"]]))
        self.assertAlmostEqual(area, 400.0, delta=0.5)
        self.assertEqual(self.report["courtyards_filled"], 1)

    def test_buildings_stay_whole_and_the_environment_grows(self):
        warehouse = self.world(self.objects["osm_w105"])
        east = max(x for x, _ in warehouse)
        self.assertAlmostEqual(east, 62.0, delta=0.01)
        self.assertGreaterEqual(self.recipe["size_m"]["east"] / 2, east)
        self.assertAlmostEqual(self.recipe["size_m"]["north"], 2 * self.half[0], delta=0.01)
        self.assertTrue(any("widened" in note for note in self.report["notes"]))
        # Roads are clipped to the selection instead.
        for obj in self.recipe["objects"]:
            if obj["item"] == "road-area":
                self.assertLessEqual(max(abs(x) for x, _ in self.world(obj, "outline")), self.half[1] + 0.01)

    def test_the_recipe_records_where_the_data_came_from(self):
        geo = self.recipe["geo"]
        self.assertEqual((geo["provider"], geo["license"]), ("openstreetmap", "ODbL-1.0"))
        self.assertAlmostEqual(geo["origin"]["lat_deg"], box().center[0], places=7)
        sources = json.loads(geo["query"])["sources"]
        self.assertEqual(len(sources), 2)  # the copies under build/ were read once

    def test_the_parts_are_a_valid_world(self):
        parsed = env_schema.parse_recipe(self.recipe, ROOT / "work/citygml.yaml")
        self.assertEqual(len(parsed.objects), len(self.recipe["objects"]))
        if env_validate.available():
            self.assertEqual([item.as_json() for item in env_validate.check(parsed)], [])

    def test_the_command_writes_the_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "recipes/city.yaml"
            completed = subprocess.run([
                sys.executable, str(ROOT / "tools/env_citygml.py"), "--citygml", str(self.citygml),
                "--center", ",".join(map(str, box().center)), "--half-extent", ",".join(map(str, self.half)),
                "--out", str(out), "--json"], capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout[completed.stdout.index("{"):])["buildings"], 4)
            self.assertEqual(env_schema.load_recipe(out).objects[0].source["kind"], "citygml")


@unittest.skipUnless(ENVSIM, "hakoniwa-envsim is not available")
class PlateauPartsTest(unittest.TestCase):
    def test_plateau_buildings_stand_on_their_own_bottom(self):
        fixtures = env_citygml.envsim_root() / "tests/fixtures"
        recipe, report = env_citygml.convert(fixtures, (35.6812, 139.70672), (100.0, 100.0), catalog=CATALOG)
        self.assertEqual(report["provider"], "plateau")
        self.assertEqual(recipe["size_m"], {"east": 200.0, "north": 200.0})
        self.assertIn("PLATEAU", recipe["geo"]["attribution"])
        for obj in recipe["objects"]:
            self.assertEqual(obj["source"]["provider"], "plateau")
            self.assertNotIn("min_height_m", obj["params"])
            self.assertGreater(obj["params"]["height_m"], 0)
        env_schema.parse_recipe(recipe, ROOT / "work/plateau.yaml")


@unittest.skipUnless(ENVSIM, "hakoniwa-envsim is not available")
class WorkspaceTest(unittest.TestCase):
    """Envsim builds already in a workspace: found, and made into parts from
    the buildings Envsim extracted (its <name>-lod1.json)."""

    @classmethod
    def setUpClass(cls):
        import gml_lod1_extract

        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name) / "work"
        cls.build = cls.root / "recipes/city-world-web-ui/runtime/jobs/tokyo-test/build"
        source = cls.build / "source/13101-2024"
        receipt = osm2citygml.run(box(), source, "map", osm_json=sample())
        selection = receipt["selection"]
        center = (selection["center"]["latitude"], selection["center"]["longitude"])
        half = (selection["half_extent_m"]["north_south"], selection["half_extent_m"]["east_west"])
        records = gml_lod1_extract.extract_buildings_lod1(source / "map_bldg_op.gml", local_origin=center,
                                                          bounds={"ns_m": half[0], "ew_m": half[1]})
        for record in records:
            record["source_gml"] = str(source / "map_bldg_op.gml")
        (cls.build / "city-world-lod1.json").write_text(json.dumps({"polygons": records}), encoding="utf-8")
        (cls.build / "download-manifest.json").write_text(json.dumps({"query": {
            "center_lat": center[0], "center_lon": center[1], "ns_m": half[0], "ew_m": half[1]},
            "files": [{"feature_type": "bldg"}, {"feature_type": "tran"}]}), encoding="utf-8")
        (cls.build.parent / "job.json").write_text(json.dumps({"job_id": "tokyo-test"}), encoding="utf-8")
        # Installs and downloads in the workspace are not searched.
        (cls.root / "foundation/deep").mkdir(parents=True)
        (cls.root / "foundation/deep/download-manifest.json").write_text("{}", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_builds_are_found_with_their_selection(self):
        builds = env_citygml.discover([self.root])
        self.assertEqual([item["title"] for item in builds], ["tokyo-test"])
        build = builds[0]
        self.assertEqual((build["buildings"], build["feature_types"], build["world"]), (4, ["bldg", "tran"], False))
        self.assertAlmostEqual(build["center"]["latitude"], box().center[0], places=7)

    def test_a_build_becomes_parts_from_its_extracted_buildings(self):
        recipe, report = env_citygml.convert_build(self.build, catalog=CATALOG)
        self.assertEqual(report["buildings"], 4)
        self.assertEqual(sorted(obj["id"] for obj in recipe["objects"] if obj["item"] == "building-footprint"),
                         ["osm_r300", "osm_w101", "osm_w103", "osm_w105"])
        self.assertGreater(report["roads"], 0)
        env_schema.parse_recipe(recipe, ROOT / "work/build.yaml")


def write_dem(directory: Path, ns_m: float, ew_m: float, slope: float = 0.1, base: float = 10.0) -> Path:
    """An Envsim terrain (receipt + .hf) of a plane rising `slope` m per m northwards."""
    import hashlib
    import struct

    spacing = 2.0
    ncol = int(round(2 * ns_m / spacing)) + 1  # x = north
    nrow = int(round(2 * ew_m / spacing)) + 1  # y = -east
    samples = [base + slope * (-ns_m + col * 2 * ns_m / (ncol - 1)) for row in range(nrow) for col in range(ncol)]
    hfield = directory / "terrain.hf"
    hfield.write_bytes(struct.pack("<ii", nrow, ncol) + struct.pack(f"<{nrow * ncol}f", *samples))
    receipt = directory / "terrain-receipt.json"
    receipt.write_text(json.dumps({"nrow": nrow, "ncol": ncol, "half_extent_m": {"north_south": ns_m, "east_west": ew_m},
                                   "coordinate_system": "X=North,Y=-East,Z=Up", "altitude_offset_m": min(samples),
                                   "hfield": {"path": str(hfield), "sha256": hashlib.sha256(hfield.read_bytes()).hexdigest()}}),
                       encoding="utf-8")
    return receipt


@unittest.skipUnless(ENVSIM, "hakoniwa-envsim is not available")
class DemTerrainTest(unittest.TestCase):
    """The ground of an Envsim City World (its DEM hfield) as a Studio terrain."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.receipt = write_dem(self.dir, 30.0, 40.0)

    def recipe(self, objects=(), size=(80, 60), dem=None):
        return {"schema": env_schema.RECIPE_SCHEMA, "name": "dem", "catalog": CATALOG,
                "size_m": {"east": size[0], "north": size[1]},
                "terrain": {"item": "city-dem", "params": {"dem": str(dem or self.receipt)}}, "objects": list(objects)}

    def test_the_ground_rises_north_like_the_dem(self):
        terrain = env_schema.parse_recipe(self.recipe(), self.dir / "r.yaml").terrain
        self.assertEqual(terrain.kind, "hfield")
        self.assertAlmostEqual(terrain.height_at(0, -30), 0.0, places=3)  # the lowest point is 0
        self.assertAlmostEqual(terrain.height_at(10, 0), 3.0, places=3)
        self.assertAlmostEqual(terrain.height_at(-35, 30), 6.0, places=3)
        # Outside the DEM (the environment is wider) the edge continues.
        big = env_schema.parse_recipe(self.recipe(size=(80, 100)), self.dir / "r.yaml").terrain
        self.assertAlmostEqual(big.height_at(0, 50), 6.0, places=3)

    def test_objects_stand_on_it_and_the_world_is_valid(self):
        cone = {"id": "cone", "item": "traffic-cone", "pose": {"x_m": 0, "y_m": 20, "yaw_deg": 0}}
        parsed = env_schema.parse_recipe(self.recipe([cone]), self.dir / "r.yaml")
        self.assertAlmostEqual(parsed.objects[0].pose.z_m, 5.0 + 0.19 * 0.1 + env_schema.HFIELD_CLEARANCE_M, delta=0.002)
        if env_validate.available():
            self.assertEqual([item.as_json() for item in env_validate.check(parsed)], [])

    def test_a_changed_hfield_is_refused(self):
        hfield = self.dir / "terrain.hf"
        hfield.write_bytes(hfield.read_bytes()[:-4] + b"\0\0\0\0")
        with self.assertRaises(env_schema.DiagnosticError) as caught:
            env_schema.parse_recipe(self.recipe(), self.dir / "r.yaml")
        self.assertEqual(caught.exception.diagnostics[0].path, "terrain.params.dem")
        with self.assertRaises(env_schema.DiagnosticError):
            env_schema.parse_recipe(self.recipe(dem=self.dir / "missing.json"), self.dir / "r.yaml")

    def test_an_import_with_a_dem_tiles_the_roads(self):
        citygml = self.dir / "citygml"
        osm2citygml.run(box(), citygml, "map", osm_json=sample())
        ns, ew = box().half_extent_m()
        receipt = write_dem(self.dir, ns, ew)
        recipe, report = env_citygml.convert(citygml, box().center, (ns, ew), catalog=CATALOG, dem=receipt)
        self.assertEqual((report["terrain"], recipe["terrain"]["item"]), ("dem", "city-dem"))
        roads = [obj for obj in recipe["objects"] if obj["item"] == "road-area"]
        self.assertTrue(all("-t" in obj["id"] for obj in roads))
        for obj in roads:  # every tile fits in one 10 m cell
            xs = [obj["pose"]["x_m"] + x for x, _ in obj["params"]["outline"]]
            self.assertLessEqual(max(xs) - min(xs), env_citygml.ROAD_TILE_M + 0.01)
        parsed = env_schema.parse_recipe(recipe, self.dir / "r.yaml")
        if env_validate.available():
            self.assertEqual([item.as_json() for item in env_validate.check(parsed)], [])


class IdTest(unittest.TestCase):
    def test_gml_ids_become_part_ids(self):
        used = set()
        self.assertEqual(env_citygml._part_id("bldg_A1B2-c3", used), "bldg_a1b2-c3")
        self.assertEqual(env_citygml._part_id("bldg_A1B2-c3", used), "bldg_a1b2-c3-2")
        long_id = env_citygml._part_id("x" * 80, used)
        self.assertEqual(len(long_id), 64)
        self.assertEqual(env_citygml._part_id("_foo.bar", set()), "foo-bar")


if __name__ == "__main__":
    unittest.main()
