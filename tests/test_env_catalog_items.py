"""A building of a city registered as a Catalog item (tools/env_catalog_items.py):
its look and colliders carried into its own frame beside the user's Catalog,
the Catalog including the starter one, and the item placed elsewhere (turned,
on other ground) standing where it should in the generated world."""

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import env_catalog_items  # noqa: E402
import env_generate  # noqa: E402
import env_schema  # noqa: E402
from test_env_passthrough import glb  # noqa: E402

STARTER = ROOT / "catalogs/starter/catalog.yaml"
ANCHOR = {"x_m": 5.0, "y_m": 20.0, "z_m": 3.0, "yaw_deg": 0.0}
# The building in its anchor's frame (east, north, up): 4 m square, from 0.5 m below the anchor to 10 m.
CORNERS = [(-2, -2), (2, -2), (2, 2), (-2, 2)]
LOW, HIGH = -0.5, 10.0


def envsim(east, north, up):
    """Envsim's MJCF axes (x north, y west) of an environment point."""
    return north, -east, up


def make_city(folder: Path) -> Path:
    """A city Recipe with one building as an import leaves it: a GLB in its
    anchor's frame, colliders (one mesh) in Envsim's frame of the city."""
    assets = folder / "city.assets"
    assets.mkdir(parents=True)
    points = [(x, y, z) for z in (LOW, HIGH) for x, y in CORNERS]
    (assets / "b1.glb").write_bytes(glb([("b1", points)]))
    vertices = " ".join(" ".join(repr(value) for value in envsim(ANCHOR["x_m"] + x, ANCHOR["y_m"] + y, ANCHOR["z_m"] + z))
                        for x, y, z in points)
    (assets / "b1.xml").write_text(
        f'<mujoco model="b1"><asset><mesh name="b1_mesh" vertex="{vertices}"/></asset>'
        '<worldbody><geom name="b1_geom" type="mesh" mesh="b1_mesh"/></worldbody></mujoco>', encoding="utf-8")
    recipe = {
        "schema": env_schema.RECIPE_SCHEMA, "name": "city", "catalog": str(STARTER),
        "size_m": {"east": 80, "north": 80}, "terrain": {"item": "grass-ground"},
        "geo": {"provider": "plateau", "attribution": "PLATEAU (国土交通省)", "license": "CC BY 4.0"},
        "objects": [{
            "id": "b1", "item": "building-footprint", "pose": {"x_m": ANCHOR["x_m"], "y_m": ANCHOR["y_m"], "yaw_deg": 0},
            "params": {"footprint": [list(point) for point in CORNERS], "height_m": HIGH,
                       "visual": "city.assets/b1.glb", "collision": "city.assets/b1.xml"},
            "source": {"provider": "plateau", "kind": "citygml", "id": "bldg_98b7a618-2dc3-42c5", "note": "x_bldg_op.gml"},
            "anchor": ANCHOR,
        }],
    }
    path = folder / "city.yaml"
    path.write_text(yaml.safe_dump(recipe, allow_unicode=True), encoding="utf-8")
    return path


def mesh_points_in_world(mjcf: str, object_id: str) -> list[tuple[float, float, float]]:
    """The world positions of an object's collider mesh vertices in a generated world."""
    root = ET.fromstring(mjcf)
    vertices = {}
    for mesh in root.iter("mesh"):
        numbers = [float(value) for value in mesh.get("vertex").split()]
        vertices[mesh.get("name")] = [tuple(numbers[i:i + 3]) for i in range(0, len(numbers), 3)]
    body = next(element for element in root.iter("body") if element.get("name") == env_generate.BODY_PREFIX + object_id)

    def frame(element):
        pos = tuple(float(value) for value in (element.get("pos") or "0 0 0").split())
        quat = tuple(float(value) for value in (element.get("quat") or "1 0 0 0").split())
        return pos, quat

    def rotate(quat, point):
        w, x, y, z = quat
        px, py, pz = point
        # q * p * q^-1
        tx, ty, tz = 2 * (y * pz - z * py), 2 * (z * px - x * pz), 2 * (x * py - y * px)
        return (px + w * tx + y * tz - z * ty, py + w * ty + z * tx - x * tz, pz + w * tz + x * ty - y * tx)

    found = []

    def walk(element, chain):
        chain = chain + [frame(element)]
        for child in element:
            if child.tag == "body":
                walk(child, chain)
            elif child.tag == "geom" and child.get("mesh"):
                for point in vertices[child.get("mesh")]:
                    for pos, quat in reversed(chain):
                        point = tuple(a + b for a, b in zip(rotate(quat, point), pos))
                    found.append(point)

    walk(body, [])
    return found


class RegisterBuildingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.city = env_schema.load_recipe(make_city(self.dir / "recipes"))
        self.catalog = self.dir / "catalogs/my/catalog.yaml"

    def test_a_building_becomes_an_item_of_the_users_catalog(self):
        result = env_catalog_items.register(self.city, "b1", "茅場町のビル", catalog=self.catalog)
        item = result["item"]
        self.assertEqual((item["id"], item["name"], item["category"]), ("bldg-98b7a618-2dc3-42c5", "茅場町のビル", "登録した建物"))
        # Its provenance: the source building and the data's attribution and licence.
        self.assertEqual((item["source"]["id"], item["source"]["attribution"], item["source"]["license"]),
                         ("bldg_98b7a618-2dc3-42c5", "PLATEAU (国土交通省)", "CC BY 4.0"))
        catalog = env_schema.load_catalog(self.catalog)
        # The starter parts are there too, and the item's files are beside the Catalog.
        self.assertIn("traffic-cone", catalog.items)
        params = catalog.items[item["id"]].params
        self.assertEqual(Path(params["visual"]), (self.catalog.parent / f"assets/{item['id']}.glb").resolve())
        # The look stands on height 0.
        look = env_generate.read_glb(Path(params["visual"]).read_bytes())[0]
        self.assertAlmostEqual(env_catalog_items.glb_lowest_y(look), 0.0, places=6)
        # Registered twice: refused.
        with self.assertRaisesRegex(env_catalog_items.RegisterError, "もう"):
            env_catalog_items.register(self.city, "b1", "again", catalog=self.catalog)

    def test_the_item_stands_where_it_is_placed_turned_on_other_ground(self):
        item = env_catalog_items.register(self.city, "b1", "ビル", catalog=self.catalog)["item"]
        recipe_path = self.dir / "field/field.yaml"
        recipe_path.parent.mkdir(parents=True)
        recipe_path.write_text(yaml.safe_dump({
            "schema": env_schema.RECIPE_SCHEMA, "name": "field", "catalog": str(self.catalog),
            "size_m": {"east": 40, "north": 40}, "terrain": {"item": "asphalt-ground"},
            "objects": [{"id": "copy", "item": item["id"], "pose": {"x_m": -6.0, "y_m": 4.0, "yaw_deg": 90}}],
        }), encoding="utf-8")
        recipe = env_schema.load_recipe(recipe_path)
        points = mesh_points_in_world(env_generate.environment_mjcf(recipe), "copy")
        # Turned a quarter: the 4 m square stays a 4 m square about (-6, 4), its lowest point on the ground.
        self.assertAlmostEqual(min(p[0] for p in points), -8.0, places=6)
        self.assertAlmostEqual(max(p[0] for p in points), -4.0, places=6)
        self.assertAlmostEqual(min(p[1] for p in points), 2.0, places=6)
        self.assertAlmostEqual(max(p[1] for p in points), 6.0, places=6)
        self.assertAlmostEqual(min(p[2] for p in points), 0.0, places=6)
        self.assertAlmostEqual(max(p[2] for p in points), HIGH - LOW, places=6)

    def test_a_recipe_of_the_starter_catalog_reads_with_the_users(self):
        env_catalog_items.register(self.city, "b1", "ビル", catalog=self.catalog)
        data = yaml.safe_load((self.dir / "recipes/city.yaml").read_text(encoding="utf-8"))
        data["catalog"] = str(self.catalog)
        path = self.dir / "recipes/city-my.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.assertEqual([obj.item for obj in env_schema.load_recipe(path).objects], ["building-footprint"])

    def test_only_a_building_is_registered(self):
        with self.assertRaisesRegex(env_catalog_items.RegisterError, "not an object"):
            env_catalog_items.register(self.city, "nothing", "x", catalog=self.catalog)


if __name__ == "__main__":
    unittest.main()
