// Checks web/geometry.js (run by tests/test_env_studio.py when Node.js is present).
import assert from "node:assert/strict";
import { mm, pivotOf, turned as turnedGroup } from "../web/plan2d.js";
import { checkLayout, contact, footprint, heightOverlap, normalizeYaw, outsideBy, overlapDepth, penetration, slideDistance, solidFootprint } from "../web/geometry.js";

const area = { minX: -10, maxX: 10, minY: -15, maxY: 15 }; // 20 m x 30 m about the centre
const near = (a, b, slack = 1e-6) => Math.abs(a - b) < slack;
const part = (id, x, y, yaw = 0, width = 1, depth = 1) => ({ id, x, y, yaw, width, depth });

// Same corners as tools/env_schema.py footprint: a 4 x 0.2 wall turned 90°.
const turned = footprint(part("w", 1, 1, 90, 4, 0.2));
const xs = turned.map(([x]) => x);
const ys = turned.map(([, y]) => y);
assert.ok(near(Math.max(...xs) - Math.min(...xs), 0.2));
assert.ok(near(Math.max(...ys) - Math.min(...ys), 4));

// Touching is fine; overlapping by more than 1 mm is not.
assert.equal(penetration(footprint(part("a", 0, 0)), footprint(part("b", 1, 0))), 0);
assert.ok(near(penetration(footprint(part("a", 0, 0)), footprint(part("b", 0.9, 0))), 0.1));

// Outside: measured against the area's edges on every side of the origin.
assert.equal(outsideBy(part("s", 9.5, -14.5), area), 0);
assert.ok(near(outsideBy(part("s", -9.7, 0), area), 0.2));
assert.ok(near(outsideBy(part("s", 9.5, 14.5, 45), area), Math.SQRT1_2 - 0.5));

const result = checkLayout([part("a", 0, 0), part("b", 0.8, 0), part("c", 0, 14.9)], area);
assert.deepEqual(result.outside, ["c"]);
assert.deepEqual(result.overlaps.map(({ a, b }) => [a, b]), [["a", "b"]]);
// Exactly 1 mm into each other is within the tolerance; 2 mm is not.
assert.equal(checkLayout([part("a", 0, 0), part("b", 0.999, 0)], area).overlaps.length, 0);
assert.equal(checkLayout([part("a", 0, 0), part("b", 0.998, 0)], area).overlaps.length, 1);

// contact(): a gap is closed and an overlap pushed out, both to a flush touch.
let c = contact(footprint(part("a", 0, 0)), footprint(part("b", 1.5, 0)));
assert.ok(near(c.gap, 0.5) && near(c.move[0], 0.5) && near(c.move[1], 0));
c = contact(footprint(part("a", 0, 0)), footprint(part("b", 0.8, 0)));
assert.ok(near(c.gap, -0.2) && near(c.move[0], -0.2));

// slideDistance(): up to the next object, or to the area's edge (to about 1 cm, then bisected).
const obstacles = [part("b", 5, 0)];
assert.ok(near(slideDistance(part("a", 0, 0), [1, 0], obstacles, area), 4, 1e-4));
assert.ok(near(slideDistance(part("a", 0, 5), [1, 0], obstacles, area), 9.5, 1e-4));
assert.ok(near(slideDistance(part("a", 0, 5), [0, -1], [], area), 19.5, 1e-4));
// An object already overlapping an obstacle can slide out of it.
assert.ok(slideDistance(part("a", 4.8, 0), [-1, 0], obstacles, area) > 10);

// Heights: a cone under a gate's crossbar is no overlap; a box as high as the bar is.
const cone = { ...part("c", 0, 0), z: 0, height: 0.7 };
const bar = { ...part("g", 0, 0), z: 2, height: 0.1 };
const block = { ...part("s", 0, 0), z: 1.95, height: 1 };
assert.equal(heightOverlap(cone, bar), 0);
assert.ok(near(heightOverlap(bar, block), 0.1));
assert.deepEqual(checkLayout([cone, bar], area).overlaps, []);

// A cylinder is a polygon on its circle.
const round = { ...part("r", 0, 0, 0, 0.6, 0.6), primitive: "cylinder" };
const ring = footprint(round);
assert.equal(ring.length, 32);
assert.ok(ring.every(([x, y]) => near(Math.hypot(x, y), 0.3)));

// Solids as the server sends them (outline in the object's frame, height range):
// a gate is two posts and a bar; a cone passes between the posts.
const box = (name, width, depth, z0, z1, x = 0) => ({
  name, collide: true, z_range_m: [z0, z1],
  outline: [[x - width / 2, -depth / 2], [x + width / 2, -depth / 2], [x + width / 2, depth / 2], [x - width / 2, depth / 2]],
});
const gate = {
  ...part("gate", 0, 0, 0, 1.7, 0.1), z: 0, height: 2.1,
  solids: [box("left", 0.1, 0.1, 0, 2.1, -0.8), box("right", 0.1, 0.1, 0, 2.1, 0.8), box("top", 1.7, 0.1, 2, 2.1)],
};
assert.equal(overlapDepth(gate, { ...cone, width: 0.38, depth: 0.38 }), 0);
assert.ok(overlapDepth(gate, { ...cone, x: 0.8, width: 0.38, depth: 0.38 }) > 0);
// A solid's outline follows the object's turn: the right post of a gate turned 90° is on +y.
const post = solidFootprint({ ...gate, yaw: 90 }, gate.solids[1]);
assert.ok(Math.min(...post.map(([, y]) => y)) > 0.7);

// Turning several objects together about the middle of their positions.
const group = [part("t", 2, 1, 0), part("c1", 1, 1, 0), part("c2", 3, 1, 180)];
assert.deepEqual(pivotOf(group), [2, 1]);
assert.deepEqual(turnedGroup(group, 90, pivotOf(group)).map(({ id, x, y, yaw }) => [id, x, y, yaw]),
  [["t", 2, 1, 90], ["c1", 2, 0, 90], ["c2", 2, 2, 270]]);

assert.equal(mm(1.23456), 1.235);
assert.equal(normalizeYaw(-90), 270);
assert.equal(normalizeYaw(450), 90);
console.log("geometry OK");
