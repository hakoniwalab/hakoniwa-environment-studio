"""A City World from hakoniwa-envsim comes back as Envsim made it
(tools/env_citygml.py passthrough, env_generate.asset_frame, env_roundtrip.py):
an Envsim build is made here (a DEM terrain, each building's colliders, a road
network GLB and the composed world), imported, generated again and compared
with MuJoCo. Skipped when hakoniwa-envsim or MuJoCo is not available."""

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import env_citygml  # noqa: E402
import env_generate  # noqa: E402
import env_roundtrip  # noqa: E402
import env_schema  # noqa: E402
import env_validate  # noqa: E402
import test_env_citygml as fixtures  # noqa: E402

READY = fixtures.ENVSIM and env_validate.available()


def glb(nodes) -> bytes:
    """A GLB of named nodes at the origin: [(name, positions in ENU)] as fans of triangles."""
    builder = env_generate._GlbBuilder()
    for name, points in nodes:
        mesh = ([tuple(point) for point in points], [(0.0, 0.0, 1.0)] * len(points),
                [index for i in range(1, len(points) - 1) for index in (0, i, i + 1)])
        builder.node(name, [(mesh, "#808080")], (0.0, 0.0, 0.0), 0.0, {})
    return builder.glb()


def make_build(root: Path) -> tuple[Path, dict]:
    """An Envsim build of the OSM sample: its selection, extracted buildings,
    a DEM terrain and the City World outputs Envsim writes."""
    import gml_lod1_extract

    build = root / "jobs/test/build"
    source = build / "source/13101-2024"
    receipt = fixtures.osm2citygml.run(fixtures.box(), source, "map", osm_json=fixtures.sample())
    selection = receipt["selection"]
    center = (selection["center"]["latitude"], selection["center"]["longitude"])
    ns, ew = selection["half_extent_m"]["north_south"], selection["half_extent_m"]["east_west"]
    records = gml_lod1_extract.extract_buildings_lod1(source / "map_bldg_op.gml", local_origin=center,
                                                      bounds={"ns_m": ns, "ew_m": ew})
    for record in records:
        record["source_gml"] = str(source / "map_bldg_op.gml")
    (build / "city-world-lod1.json").write_text(json.dumps({"polygons": records}), encoding="utf-8")
    (build / "download-manifest.json").write_text(json.dumps({"query": {
        "center_lat": center[0], "center_lon": center[1], "ns_m": ns, "ew_m": ew},
        "files": [{"feature_type": "bldg"}, {"feature_type": "tran"}, {"feature_type": "dem"}]}), encoding="utf-8")
    components = build / "components"
    # Terrain: a plane rising north (Envsim's MuJoCo frame: x north, y west), its MJCF and GLB.
    terrain_dir = components / "terrain"
    terrain_dir.mkdir(parents=True)
    terrain_receipt = fixtures.write_dem(terrain_dir, ns, ew, slope=0.05, base=0.0)
    data = json.loads(terrain_receipt.read_text(encoding="utf-8"))
    span = 0.05 * 2 * ns
    data["mjcf"] = str(terrain_dir / "terrain.xml")
    terrain_receipt.write_text(json.dumps(data), encoding="utf-8")
    (terrain_dir / "terrain.xml").write_text(
        f'<mujoco><asset><hfield name="plateau_terrain" file="terrain.hf" size="{ns} {ew} {span:.6f} 1.0"/></asset>'
        '<worldbody><geom name="plateau_ground" type="hfield" hfield="plateau_terrain"/></worldbody></mujoco>',
        encoding="utf-8")
    (terrain_dir / "terrain.glb").write_bytes(glb([("terrain", [(-ew, -ns, 0), (ew, -ns, 0), (ew, ns, span), (-ew, ns, span)])]))
    # Buildings: per building a box body (P0) turned by euler degrees and a mesh surface (P1).
    buildings = components / "buildings"
    buildings.mkdir()
    meshes, bodies = [], []
    ids = sorted({record["id"].split("__part_")[0] for record in records})
    for number, gml_id in enumerate(ids):
        xs = [x for x, _ in records[[r["id"].split("__part_")[0] for r in records].index(gml_id)]["vertices"]]
        ys = [y for _, y in records[[r["id"].split("__part_")[0] for r in records].index(gml_id)]["vertices"]]
        east, north = sum(xs) / len(xs), sum(ys) / len(ys)
        x, y = north, -east  # Envsim's frame
        meshes.append(f'<mesh name="roof_{gml_id}_piece_0000" vertex="{x} {y} 3  {x + 2} {y} 3  {x} {y + 2} 3  '
                      f'{x} {y} 3.02  {x + 2} {y} 3.02  {x} {y + 2} 3.02" face="0 1 2 5 4 3 0 3 4 0 4 1 1 4 5 1 5 2 2 5 3 2 3 0"/>')
        bodies.append(f'<body name="body_{gml_id}"><geom name="geom_{gml_id}" type="box" size="1.5 2.5 1.25" '
                      f'pos="{x} {y} {1.25 + number}" euler="0 0 {10 + 7 * number}" rgba="0.82 0.82 0.86 1" '
                      f'contype="1" conaffinity="0"/></body>')
        bodies.append(f'<geom name="roof_{gml_id}_piece_0000" type="mesh" mesh="roof_{gml_id}_piece_0000" '
                      f'contype="1" conaffinity="0"/>')
    (buildings / "buildings.xml").write_text(
        f'<mujoco model="b"><asset>{"".join(meshes)}</asset><worldbody>{"".join(bodies)}</worldbody></mujoco>',
        encoding="utf-8")
    roads = components / "roads"
    roads.mkdir()
    road_points = [(-45, -2, 0.5), (20, -2, 0.5), (20, 2, 0.6), (-45, 2, 0.6)]
    (roads / "roads.glb").write_bytes(glb([("roadway", road_points)]))
    world = build / "world"
    world.mkdir()
    (world / "city-world.xml").write_text(
        f'<mujoco model="plateau_city_world"><asset><hfield name="plateau_terrain" file="{terrain_dir / "terrain.hf"}" '
        f'size="{ns} {ew} {span:.6f} 1.0"/>{"".join(meshes)}</asset><worldbody>'
        '<geom name="plateau_ground" type="hfield" hfield="plateau_terrain" pos="0 0 0" rgba="0.55 0.55 0.55 1"/>'
        f'{"".join(bodies)}</worldbody></mujoco>', encoding="utf-8")
    (world / "city-world.glb").write_bytes(glb([
        ("component-0-terrain-terrain", [(-ew, -ns, 0), (ew, -ns, 0), (ew, ns, span), (-ew, ns, span)]),
        ("component-1-roads-roadway", road_points)]))
    (world / "city-world-receipt.json").write_text(json.dumps({
        "coordinate_frame": {"origin": {"latitude": center[0], "longitude": center[1], "altitude_offset_m": 0.0}},
        "components": {"buildings_xml": str(buildings / "buildings.xml"), "extra_mjcf": [],
                       "glb_geometry_counts": {str(terrain_dir / "terrain.glb"): 1, str(roads / "roads.glb"): 1,
                                               str(buildings / "buildings.glb"): 0}}}), encoding="utf-8")
    return build, {"ids": ids}


@unittest.skipUnless(READY, "hakoniwa-envsim or MuJoCo is not available")
class PassthroughTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.build, cls.info = make_build(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def imported(self, **options):
        path = Path(tempfile.mkdtemp(dir=self.root)) / "city.yaml"
        recipe, report = env_citygml.convert_build(self.build, recipe_path=path, catalog=fixtures.CATALOG, **options)
        env_citygml.write_recipe(recipe, path)
        return path, recipe, report

    def test_an_import_carries_envsims_colliders_terrain_and_road_network(self):
        path, recipe, report = self.imported()
        self.assertEqual(report["passthrough"], {"colliders": len(self.info["ids"]), "layers": ["roads"], "terrain": True})
        buildings = [obj for obj in recipe["objects"] if obj["item"] == "building-footprint"]
        self.assertTrue(all("anchor" in obj and obj["params"]["collision"].startswith("city.assets/") for obj in buildings))
        # The road network is one layer drawn by the road outlines, not road parts that collide.
        self.assertFalse([obj for obj in recipe["objects"] if obj["item"] == "road-area"])
        layer = next(obj for obj in recipe["objects"] if obj["item"] == "city-layer")
        self.assertEqual((layer["id"], layer["params"]["visual"]), ("roads", "city.assets/layer-roads.glb"))
        self.assertGreater(len(layer["params"]["outlines"]), 0)
        self.assertEqual(recipe["terrain"]["params"], {"dem": "city.assets/terrain/terrain-receipt.json",
                                                       "visual": "city.assets/terrain/terrain.glb"})
        # The footprints still place the parts and pass the Studio's checks.
        self.assertEqual([item.as_json() for item in env_validate.check(env_schema.load_recipe(path))], [])

    def test_unedited_it_generates_envsims_own_world(self):
        path, _recipe, _report = self.imported()
        result = env_roundtrip.check(self.build, path)
        mjcf = result["mjcf"]
        self.assertEqual((mjcf["missing"], mjcf["different"], mjcf["added_colliders"]), ([], [], []), mjcf)
        self.assertEqual(mjcf["matched"], mjcf["envsim_geoms"])
        self.assertLess(mjcf["max_position_error_m"], 1e-9)
        for layer in ("terrain", "roads"):
            self.assertEqual((result["glb"][layer]["envsim_covered"], result["glb"][layer]["studio_covered"]), (1.0, 1.0))

    def test_a_moved_building_takes_its_colliders_along(self):
        import mujoco
        import numpy as np

        path, recipe, _report = self.imported()
        part = next(obj for obj in recipe["objects"] if obj["item"] == "building-footprint")
        gml_id = part["source"]["id"].split("__part_")[0]
        part["pose"]["x_m"] += 5.0  # east: the ground rises only northwards
        part["pose"]["yaw_deg"] = 90.0
        env_citygml.write_recipe(recipe, path)
        parsed = env_schema.load_recipe(path)
        mjcf = env_roundtrip.compare_mjcf(self.build / "world/city-world.xml", env_generate.environment_mjcf(parsed))
        moved = {item["geom"] for item in mjcf["different"]}
        self.assertEqual(moved, {f"geom_{gml_id}", f"roof_{gml_id}_piece_0000"})
        self.assertEqual(mjcf["matched"], mjcf["envsim_geoms"] - 2)
        # Where it went: turned 90 degrees about the part's position and moved 5 m east.
        em = mujoco.MjModel.from_xml_path(str(self.build / "world/city-world.xml"))
        ed = mujoco.MjData(em)
        mujoco.mj_forward(em, ed)
        sm = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(parsed))
        sd = mujoco.MjData(sm)
        mujoco.mj_forward(sm, sd)
        before = env_roundtrip._to_studio(ed.geom_xpos[mujoco.mj_name2id(em, mujoco.mjtObj.mjOBJ_GEOM, f"geom_{gml_id}")])
        after = sd.geom_xpos[mujoco.mj_name2id(sm, mujoco.mjtObj.mjOBJ_GEOM, f"{part['id']}/geom_{gml_id}")]
        ax, ay = part["anchor"]["x_m"], part["anchor"]["y_m"]
        dx, dy = before[0] - ax, before[1] - ay
        expected = (ax + 5.0 - dy, ay + dx, before[2])
        self.assertTrue(np.allclose(after, expected, atol=1e-6), (after, expected))

    def test_a_copied_building_keeps_its_own_collider_names(self):
        import mujoco

        path, recipe, _report = self.imported()
        part = next(obj for obj in recipe["objects"] if obj["item"] == "building-footprint")
        copy = json.loads(json.dumps(part))
        copy["id"] = part["id"] + "-copy"
        copy["pose"]["y_m"] -= 80.0
        recipe["objects"].append(copy)
        env_citygml.write_recipe(recipe, path)
        model = mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(env_schema.load_recipe(path)))
        names = {mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, index) for index in range(model.ngeom)}
        gml_id = part["source"]["id"].split("__part_")[0]
        self.assertTrue({f"{part['id']}/geom_{gml_id}", f"{copy['id']}/geom_{gml_id}"} <= names)

    def test_on_flat_ground_the_colliders_stand_on_it(self):
        import mujoco

        path, recipe, report = self.imported(use_dem=False)
        self.assertEqual((report["terrain"], report["passthrough"]["terrain"]), ("flat", False))
        part = next(obj for obj in recipe["objects"] if obj["item"] == "building-footprint")
        self.assertNotIn("terrain", part["anchor"])
        parsed = env_schema.load_recipe(path)
        obj = next(item for item in parsed.objects if item.id == part["id"])
        self.assertEqual(env_generate.asset_frame(parsed, obj)[2], obj.pose.z_m)
        mujoco.MjModel.from_xml_string(env_generate.environment_mjcf(parsed))  # compiles

    def test_colliders_need_an_anchor(self):
        path, recipe, _report = self.imported()
        part = next(obj for obj in recipe["objects"] if obj["item"] == "building-footprint")
        del part["anchor"]
        with self.assertRaises(env_schema.DiagnosticError) as caught:
            env_schema.parse_recipe(recipe, path)
        self.assertTrue(caught.exception.diagnostics[0].path.endswith(".params.collision"))

    def test_generate_writes_the_hfield_beside_the_world(self):
        import mujoco

        path, _recipe, _report = self.imported()
        out = Path(tempfile.mkdtemp(dir=self.root))
        paths = env_generate.generate(env_schema.load_recipe(path), out)
        self.assertEqual(paths["hfield"].read_bytes(), (self.build / "components/terrain/terrain.hf").read_bytes())
        self.assertIn('file="environment-terrain.hf"', paths["mjcf"].read_text(encoding="utf-8"))
        mujoco.MjModel.from_xml_path(str(paths["mjcf"]))


    def test_saved_under_a_new_id_it_keeps_its_own_assets(self):
        from unittest import mock

        import env_studio

        user = Path(tempfile.mkdtemp(dir=self.root)) / "work/recipes"
        with mock.patch.object(env_studio, "USER_RECIPES", user):
            imported = env_studio.import_city_world({"id": "city", "path": str(self.build)})
            self.assertEqual(imported["passthrough"]["colliders"], len(self.info["ids"]))
            loaded = env_studio.read_recipe("city")
            body = {**{key: loaded["recipe"][key] for key in ("name", "size_m", "terrain", "objects")},
                    "catalog_id": "starter"}
            saved = env_studio.save_recipe("city-2", body)
            self.assertEqual(saved["assets"]["terrain"], {"dem": "city-2.assets/terrain/terrain-receipt.json",
                                                          "visual": "city-2.assets/terrain/terrain.glb"})
            self.assertTrue(all(params["collision"].startswith("city-2.assets/")
                                for params in saved["assets"]["objects"].values() if "collision" in params))
            env_studio.delete_recipe("city")
            result = env_roundtrip.check(self.build, user / "city-2.yaml")
            self.assertEqual((result["mjcf"]["missing"], result["mjcf"]["different"]), ([], []))


    def test_the_road_network_can_be_split_into_road_parts(self):
        import env_studio

        path, recipe, _report = self.imported()
        body = {**{key: recipe[key] for key in ("name", "size_m", "terrain", "objects")}, "catalog_id": "starter"}
        with unittest.mock.patch.object(env_studio, "USER_RECIPES", path.parent):
            parts = env_studio.explode_layer({**body, "object": "roads"})["objects"]
            self.assertTrue(parts and all(part["item"] == "road-area" for part in parts))
            self.assertTrue(all("-t" in part["id"] for part in parts))  # tiles: the terrain is a height field
            recipe["objects"] = [obj for obj in recipe["objects"] if obj["id"] != "roads"] + parts
            env_schema.parse_recipe(recipe, path)
            with self.assertRaises(env_studio.StudioError):
                env_studio.explode_layer({**body, "object": recipe["objects"][0]["id"]})

    def test_the_3d_view_moves_an_asset_where_the_glb_puts_it(self):
        import env_studio

        path, recipe, _report = self.imported()
        body = {**{key: recipe[key] for key in ("name", "size_m", "terrain", "objects")}, "catalog_id": "starter"}
        with unittest.mock.patch.object(env_studio, "USER_RECIPES", path.parent):
            poses = env_studio.preview_poses(body)["poses"]
        self.assertEqual(poses["roads"]["translation"], [0.0, 0.0, -0.0])  # unmoved: Envsim's own place


@unittest.skipUnless(env_validate.available(), "MuJoCo is not installed")
class EulerTest(unittest.TestCase):
    def test_envsims_euler_degrees_turn_as_mujoco_turns_them(self):
        import mujoco
        import numpy as np

        for angles in ((0, 0, 10.24473), (10, 20, 30), (-45, 5, 170)):
            degrees = mujoco.MjModel.from_xml_string(
                f'<mujoco><worldbody><geom type="box" size="1 1 1" euler="{angles[0]} {angles[1]} {angles[2]}"/>'
                '</worldbody></mujoco>')
            quat = env_generate.euler_quaternion(angles)
            ours = mujoco.MjModel.from_xml_string(
                '<mujoco><compiler angle="radian"/><worldbody><geom type="box" size="1 1 1" '
                f'quat="{quat[0]!r} {quat[1]!r} {quat[2]!r} {quat[3]!r}"/></worldbody></mujoco>')
            self.assertTrue(np.allclose(degrees.geom_quat[0], ours.geom_quat[0], atol=1e-12), angles)


if __name__ == "__main__":
    unittest.main()
