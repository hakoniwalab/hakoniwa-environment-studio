// Checks web/parts.js (run by tests/test_env_studio.py when Node.js is present).
import assert from "node:assert/strict";
import { Parts } from "../web/parts.js";

// Items as GET /api/catalogs/<id> sends them (only what parts.js reads).
const items = [
  { id: "wall", name: "Wall", params: { color: "#cccccc", length_m: 5 }, surface: "ground", snap: true, height_m: 2,
    envelope: { primitive: "box", width_m: 5, depth_m: 0.2 }, solids: [] },
  { id: "platform", name: "Platform", params: { color: "#888888", z_m: 1 }, surface: "elevated", snap: false, height_m: 0.1,
    envelope: { primitive: "box", width_m: 2, depth_m: 2 }, solids: [] },
];
const obj = (id, item, x, y, params) => ({ id, item, pose: { x_m: x, y_m: y, yaw_deg: -90 }, ...(params ? { params } : {}) });

const asked = [];
let answer;
const parts = new Parts({
  resolve: (item, params) => { asked.push([item, params]); return new Promise((done) => { answer = done; }); },
  onShape: () => asked.push("redraw"),
});
parts.setCatalog(items);

// Position, envelope and behaviour in metres; the yaw normalised.
const wall = parts.resolved(obj("w", "wall", 1.5, -2));
assert.deepEqual([wall.x, wall.y, wall.yaw, wall.width, wall.depth, wall.height, wall.z, wall.snap], [1.5, -2, 270, 5, 0.2, 2, 0, true]);
// An elevated object is raised by its z_m (the item's, or its own); colour is read directly.
assert.equal(parts.resolved(obj("p", "platform", 0, 0)).z, 1);
assert.equal(parts.resolved(obj("p", "platform", 0, 0, { z_m: 2.5, color: "#000000" })).z, 2.5);
assert.deepEqual(asked, []);

// A parameter that changes the shape: asked once; until the answer the object
// keeps the item's shape, then it uses the answer (and redraws).
const long = obj("w2", "wall", 0, 0, { length_m: 8 });
assert.equal(parts.resolved(long).width, 5);
parts.resolved(long);
assert.deepEqual(asked, [["wall", { length_m: 8 }]]);
answer({ ...items[0], envelope: { primitive: "box", width_m: 8, depth_m: 0.2 } });
await new Promise((done) => setTimeout(done, 0));
assert.equal(asked.at(-1), "redraw");
assert.equal(parts.resolved(long).width, 8);
// The same shape parameters on another object: cached, not asked again.
assert.equal(parts.resolved(obj("w3", "wall", 0, 0, { length_m: 8, color: "#ff0000" })).width, 8);
assert.equal(asked.filter((entry) => entry !== "redraw").length, 1);
// Changing again: the object keeps its last shape until the new one comes.
assert.equal(parts.resolved({ ...long, params: { length_m: 9 } }).width, 8);

// A new Catalog forgets the old shapes; an unknown item still draws.
parts.setCatalog(items);
assert.equal(parts.shapes.size, 0);
assert.equal(parts.resolved(obj("x", "nothing", 0, 0)).width, 0.5);
console.log("parts OK");
