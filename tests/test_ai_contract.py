"""The AI authoring contract (docs/ai-contract.md), replayed without an AI:
each round of tests/fixtures/ai-loop gives the diagnostics the next round repairs."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "tests/fixtures/ai-loop"
sys.path.insert(0, str(ROOT / "tools"))

import env_validate  # noqa: E402


def envstudio(*args, stdin=None):
    completed = subprocess.run([sys.executable, str(ROOT / "tools/envstudio.py"), "--json", *args],
                               input=stdin, capture_output=True, text=True, check=False, timeout=120)
    return completed.returncode, json.loads(completed.stdout)


class ContractTest(unittest.TestCase):
    def test_the_contract_names_versions_commands_and_codes(self):
        status, contract = envstudio("contract")
        self.assertEqual(status, 0)
        self.assertEqual(contract["schemas"]["recipe"], "hakoniwa.environment-recipe/v1")
        self.assertLessEqual({"validate", "resolve", "inspect", "generate", "describe-item"}, set(contract["commands"]))
        self.assertLessEqual({"unknown_field", "overlap", "outside", "below_terrain"}, set(contract["diagnostic_codes"]))
        self.assertEqual(contract["frame"]["origin"], "centre")

    def test_round_1_schema_diagnostics_say_how_to_repair(self):
        status, result = envstudio("validate", str(LOOP / "1-draft.yaml"))
        self.assertEqual((status, result["ok"]), (1, False))
        found = {item["path"]: item for item in result["diagnostics"]}
        self.assertEqual({path: item["code"] for path, item in found.items()}, {
            "objects[0].pose.yaw": "unknown_field",
            "objects[1].params.opening_width_m": "out_of_range",
            "objects[3].item": "unknown_reference",
        })
        self.assertIn("yaw_deg", found["objects[0].pose.yaw"]["expected"])
        self.assertIn("landing-pad", found["objects[3].item"]["expected"])

    @unittest.skipUnless(env_validate.available(), "MuJoCo is not installed")
    def test_round_2_physics_diagnostics_name_the_objects_and_depths(self):
        status, result = envstudio("validate", str(LOOP / "2-draft.yaml"))
        self.assertEqual((status, result["ok"]), (1, False))
        codes = sorted((item["code"], item["path"], json.dumps(item["actual"], sort_keys=True), tuple(item.get("related", ())))
                       for item in result["diagnostics"])
        self.assertEqual([code[:2] for code in codes],
                         [("outside", "objects[3]"), ("outside", "objects[3]"), ("overlap", "objects[0]")])
        edges = {json.loads(code[2])["edge"] for code in codes if code[0] == "outside"}
        self.assertEqual(edges, {"north", "east"})
        self.assertEqual(codes[2][3], ("objects[4]",))

    def test_inspect_gives_room_and_gaps_for_placing(self):
        status, before = envstudio("inspect", str(LOOP / "2-draft.yaml"))
        self.assertEqual(status, 0)
        rows = {row["id"]: row for row in before["objects"]}
        self.assertEqual((rows["pad"]["room_m"]["north"], rows["pad"]["room_m"]["east"]), (-0.5, -0.5))
        self.assertEqual(rows["marker"]["nearest"], {"id": "gate-1", "gap_m": 0.0})
        _, after = envstudio("inspect", str(LOOP / "3-repaired.yaml"))
        rows = {row["id"]: row for row in after["objects"]}
        self.assertTrue(all(value > 0 for value in rows["pad"]["room_m"].values()))
        self.assertGreater(rows["marker"]["nearest"]["gap_m"], 0)

    def test_round_3_validates_and_generates_deterministically(self):
        status, result = envstudio("validate", str(LOOP / "3-repaired.yaml"))
        self.assertEqual((status, result["ok"]), (0, True))
        fingerprints = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as directory:
                status, generated = envstudio("generate", str(LOOP / "3-repaired.yaml"), "--out-dir", directory)
                self.assertEqual(status, 0)
                fingerprints.append(generated["fingerprint"])
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_json_on_stdin_is_the_same_contract(self):
        recipe = {"schema": "hakoniwa.environment-recipe/v1", "name": "stdin", "catalog": "catalogs/starter/catalog.yaml",
                  "size_m": {"east": 10, "north": 10}, "terrain": {"item": "grass-ground"},
                  "objects": [{"id": "c", "item": "traffic-cone", "pose": {"x_m": 0, "y_m": 0, "yaw_deg": 0}, "colour": "red"}]}
        status, result = envstudio("validate", "-", "--base", str(ROOT), stdin=json.dumps(recipe))
        self.assertEqual(status, 1)
        self.assertEqual([(item["code"], item["path"]) for item in result["diagnostics"]], [("unknown_field", "objects[0].colour")])


if __name__ == "__main__":
    unittest.main()
