// Checks web/problems.js (run by tests/test_env_studio.py when Node.js is present).
import assert from "node:assert/strict";
import { fromServer, objectAt, quickProblems } from "../web/problems.js";

const objects = [{ id: "a" }, { id: "b" }, { id: "c" }];
assert.equal(objectAt(objects, "objects[1].pose.x_m").id, "b");
assert.equal(objectAt(objects, "size_m.east"), null);

// Server diagnostics name the objects they are about (by their objects[i] paths).
const problems = fromServer({ stage: "physics", diagnostics: [
  { severity: "error", code: "overlap", path: "objects[0]", related: ["objects[2]"], actual: 0.0213 },
  { severity: "error", code: "outside", path: "objects[1]", actual: { edge: "east", depth_m: 0.5 } },
  { severity: "error", code: "below_terrain", path: "objects[2]", actual: 0.003 },
  { severity: "warning", code: "physics_skipped", path: "" },
  { severity: "error", code: "unknown_reference", path: "objects[1].item", reason: "not an object item" },
] }, objects);
assert.deepEqual([...problems.overlapping].sort(), ["a", "b", "c"]);
assert.deepEqual([...problems.outside], ["b"]);
assert.deepEqual(problems.lines, [
  "a と c が 21 mm 重なっています",
  "b が東の端から 500 mm はみ出しています",
  "c が地面に 3 mm めり込んでいます",
  "objects[1].item: not an object item",
]);

// The quick check leaves roads (the surface layer) out.
const box = (id, x, layer = "object") => ({ id, x, y: 0, yaw: 0, width: 1, depth: 1, layer });
const quick = quickProblems([box("a", 0), box("b", 0.5), box("road", 0.2, "surface")], { minX: -5, maxX: 5, minY: -5, maxY: 5 });
assert.deepEqual([...quick.overlapping].sort(), ["a", "b"]);
console.log("problems OK");
