"""CC0 scenery catalog: provenance, normalized visuals and fitted physics."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from PIL import Image
import trimesh
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import env_generate  # noqa: E402
import env_schema  # noqa: E402

CATALOG_PATH = ROOT / "catalogs/scenery-cc0/catalog.yaml"


class Cc0SceneryCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = env_schema.load_catalog(CATALOG_PATH)
        cls.items = {key: value for key, value in cls.catalog.items.items() if key.startswith("cc0-")}

    def test_catalog_entries_load_with_complete_cc0_provenance(self):
        self.assertEqual(len(self.items), 14)
        self.assertNotIn("cc0-shrub", self.items)
        for item in self.items.values():
            with self.subTest(item=item.id):
                self.assertEqual(item.source["license"], "CC0")
                for field in ("site", "original_asset", "url", "author", "polygons"):
                    self.assertTrue(item.source.get(field), field)
                visual = Path(item.params["visual"])
                self.assertTrue(visual.is_file())
                self.assertEqual(visual.suffix, ".glb")

    def test_visual_bounds_polygons_and_simple_colliders_match_catalog(self):
        for item in self.items.values():
            with self.subTest(item=item.id):
                visual = Path(item.params["visual"])
                scene = trimesh.load_scene(visual)
                width, height, depth = scene.extents
                expected = (item.params["native_width_m"], item.params["native_height_m"],
                            item.params["native_depth_m"])
                for actual, wanted in zip((width, height, depth), expected):
                    self.assertAlmostEqual(actual, wanted, places=4)
                self.assertAlmostEqual(scene.bounds[0][1], 0.0, places=5)
                polygons = sum(len(mesh.faces) for mesh in scene.geometry.values())
                self.assertEqual(polygons, item.source["polygons"])

                self.assertEqual(len(item.shape.solids), 1)
                solid = item.shape.solids[0]
                self.assertIn(solid.primitive, ("box", "cylinder"))
                self.assertTrue(solid.collide)
                self.assertEqual((solid.width_m, solid.depth_m, solid.height_m),
                                 (item.params["width_m"], item.params["depth_m"], item.params["height_m"]))

                document, _ = env_generate.read_glb(visual.read_bytes())
                self.assertTrue(document.get("meshes"))
                self.assertTrue(all("uri" not in image for image in document.get("images", [])))

    def test_poly_haven_textures_are_at_most_512_pixels(self):
        for item_id in ("cc0-heritage-lamp", "cc0-concrete-barrier", "cc0-chainlink-fence"):
            visual = Path(self.items[item_id].params["visual"])
            document, binary = env_generate.read_glb(visual.read_bytes())
            for image in document["images"]:
                view = document["bufferViews"][image["bufferView"]]
                offset = view.get("byteOffset", 0)
                payload = binary[offset:offset + view["byteLength"]]
                with Image.open(io.BytesIO(payload)) as texture:
                    self.assertLessEqual(max(texture.size), 512)

    def test_recipe_target_size_scales_visual_and_physics_together(self):
        target = {"width_m": 9.0, "depth_m": 4.5, "height_m": 3.0}
        recipe_data = {
            "schema": env_schema.RECIPE_SCHEMA,
            "name": "scaled scenery",
            "catalog": str(CATALOG_PATH),
            "size_m": {"east": 40, "north": 40},
            "terrain": {"item": "grass-ground"},
            "objects": [{"id": "fence", "item": "cc0-chainlink-fence",
                         "pose": {"x_m": 0, "y_m": 0, "yaw_deg": 0}, "params": target}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recipe.yaml"
            path.write_text(yaml.safe_dump(recipe_data), encoding="utf-8")
            recipe = env_schema.load_recipe(path)

        obj = recipe.objects[0]
        solid = obj.solids[0]
        self.assertEqual((solid.width_m, solid.depth_m, solid.height_m), (9.0, 4.5, 3.0))
        document, _ = env_generate.read_glb(env_generate.environment_glb(recipe))
        node = next(entry for entry in document["nodes"] if entry["name"] == "fence")
        self.assertEqual(node["scale"], [3.0, 1.5, 56.25])  # glTF x, up, -Studio-y
        self.assertIn("BLEND", {material.get("alphaMode") for material in document["materials"]})

        root = ET.fromstring(env_generate.environment_mjcf(recipe))
        geom = next(entry for entry in root.iter("geom") if entry.get("name") == "geom:fence/collider")
        self.assertEqual(geom.get("type"), "box")
        self.assertEqual(tuple(map(float, geom.get("size").split())), (4.5, 2.25, 1.5))
        # Omitted MuJoCo contact fields use the colliding defaults (1, 1).
        self.assertEqual((geom.get("contype", "1"), geom.get("conaffinity", "1")), ("1", "1"))


if __name__ == "__main__":
    unittest.main()
