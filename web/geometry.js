// Environment geometry for the 2D editor (kept in step with
// tools/env_schema.py footprint): metres in the environment's ENU frame, the
// origin at its centre. An "area" is the environment's extent,
// {minX, maxX, minY, maxY}.

// Interference tolerance (1 mm): touching is fine, overlapping more than this is not.
export const TOLERANCE_M = 0.001;
// Floating-point slack, so an overlap exactly at the tolerance counts as within it.
const EPSILON_M = 1e-9;

export function normalizeYaw(degrees) {
  return ((Number(degrees) % 360) + 360) % 360;
}

// Sides of the polygon that stands for a cylinder's circle (env_schema.py too).
export const CIRCLE_SEGMENTS = 32;

function outline(primitive, width, depth, dx = 0, dy = 0) {
  const halfW = width / 2;
  const halfD = depth / 2;
  const local = primitive === "cylinder"
    ? Array.from({ length: CIRCLE_SEGMENTS }, (_, i) => {
      const angle = (2 * Math.PI * i) / CIRCLE_SEGMENTS;
      return [halfW * Math.cos(angle), halfD * Math.sin(angle)];
    })
    : [[-halfW, -halfD], [halfW, -halfD], [halfW, halfD], [-halfW, halfD]];
  return local.map(([x, y]) => [x + dx, y + dy]);
}

function placed(part, local) {
  const yaw = (part.yaw * Math.PI) / 180;
  const cos = Math.cos(yaw);
  const sin = Math.sin(yaw);
  return local.map(([x, y]) => [part.x + cos * x - sin * y, part.y + sin * x + cos * y]);
}

// The floor outline of a part (its envelope, counter-clockwise), from the
// centre of its bottom face; width runs along the part's local x, depth along
// its local y. A box has four corners; a cylinder is a polygon on its circle.
export function footprint(part) {
  return placed(part, outline(part.primitive, part.width, part.depth));
}

// The outline of one of a part's solids seen from above, in the environment
// frame: the server sends it (solid.outline, in the object's frame, tilts
// included), so the browser does no tilt maths.
export function solidFootprint(part, solid) {
  return placed(part, solid.outline);
}

// What the plan draws for a part: its envelope, or, for a part whose envelope
// is not its shape (a footprint, a road along a centre line: part.detailed),
// each visible solid's outline. [{polygon, color, collide}]
export function outlines(part) {
  if (!part.detailed || !part.solids?.length) return [{ polygon: footprint(part), color: part.color, collide: true }];
  return part.solids.filter((solid) => solid.visible !== false)
    .map((solid) => ({ polygon: solidFootprint(part, solid), color: solid.color, collide: solid.collide !== false }));
}

// The outlines that make up a part's body (for picking and the edges).
export function bodyOutlines(part) {
  const body = outlines(part).filter((outline) => outline.collide).map((outline) => outline.polygon);
  return body.length ? body : [footprint(part)];
}

// The solids of a part that take part in interference, as {polygon, z0, z1}
// in the environment frame; a part without solids is its envelope.
function collisionVolumes(part) {
  const z = part.z ?? 0;
  if (!part.solids) return [{ polygon: footprint(part), z0: z, z1: z + (part.height ?? Infinity) }];
  return part.solids.filter((solid) => solid.collide !== false).map((solid) => ({
    polygon: solidFootprint(part, solid), z0: z + solid.z_range_m[0], z1: z + solid.z_range_m[1],
  }));
}

// How deep two parts overlap: the deepest pair of their solids, each pair the
// smaller of its overlap on the floor and in height (0 when apart).
export function overlapDepth(a, b) {
  let depth = 0;
  for (const va of collisionVolumes(a)) {
    for (const vb of collisionVolumes(b)) {
      const height = Math.min(va.z1, vb.z1) - Math.max(va.z0, vb.z0);
      if (height <= 0) continue;
      depth = Math.max(depth, Math.min(penetration(va.polygon, vb.polygon), height));
    }
  }
  return depth;
}

// Whether a polygon contains a point (even-odd rule).
export function contains(polygon, [x, y]) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function bounds(points) {
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
}

// How far a part sticks out of the area (0 when inside).
export function outsideBy(part, area) {
  const box = bounds(bodyOutlines(part).flat());
  return Math.max(0, area.minX - box.minX, area.minY - box.minY, box.maxX - area.maxX, box.maxY - area.maxY);
}

// How much two parts overlap in height (0 when one is above the other).
// Parts carry z (bottom above the floor) and height; missing means on the floor.
export function heightOverlap(a, b) {
  const az = a.z ?? 0;
  const bz = b.z ?? 0;
  return Math.max(0, Math.min(az + (a.height ?? Infinity), bz + (b.height ?? Infinity)) - Math.max(az, bz));
}

// Penetration depth of two convex polygons by the separating axis test
// (0 when they are apart or only touch).
export function penetration(a, b) {
  let depth = Infinity;
  for (const polygon of [a, b]) {
    for (let i = 0; i < polygon.length; i += 1) {
      const [x1, y1] = polygon[i];
      const [x2, y2] = polygon[(i + 1) % polygon.length];
      const length = Math.hypot(x2 - x1, y2 - y1) || 1;
      const axis = [-(y2 - y1) / length, (x2 - x1) / length];
      const project = (points) => points.map(([x, y]) => x * axis[0] + y * axis[1]);
      const pa = project(a);
      const pb = project(b);
      const overlap = Math.min(Math.max(...pa), Math.max(...pb)) - Math.max(Math.min(...pa), Math.min(...pb));
      if (overlap <= 0) return 0;
      depth = Math.min(depth, overlap);
    }
  }
  return depth;
}

function axesOf(a, b) {
  const axes = [];
  for (const polygon of [a, b]) {
    for (let i = 0; i < polygon.length; i += 1) {
      const [x1, y1] = polygon[i];
      const [x2, y2] = polygon[(i + 1) % polygon.length];
      const length = Math.hypot(x2 - x1, y2 - y1) || 1;
      axes.push([-(y2 - y1) / length, (x2 - x1) / length]);
    }
  }
  return axes;
}

// How polygon a must move to just touch polygon b, along one separating-axis
// direction: {gap, move, axis}. gap > 0: a is that far from b and move closes
// the gap (the axis where they are furthest apart); gap <= 0: they overlap by
// -gap and move pushes a out the shortest way.
export function contact(a, b) {
  let best = null;
  let overlapBest = null;
  for (const axis of axesOf(a, b)) {
    const project = (points) => points.map(([x, y]) => x * axis[0] + y * axis[1]);
    const pa = project(a);
    const pb = project(b);
    const aMin = Math.min(...pa); const aMax = Math.max(...pa);
    const bMin = Math.min(...pb); const bMax = Math.max(...pb);
    const ahead = bMin - aMax; // b lies ahead of a along the axis
    const behind = aMin - bMax; // b lies behind a
    const separation = Math.max(ahead, behind);
    if (separation > 0) {
      const step = ahead > behind ? ahead : -behind;
      if (!best || separation > best.gap) best = { gap: separation, move: [axis[0] * step, axis[1] * step], axis };
    } else {
      // Overlapping on this axis: the cheaper way out along it.
      const forward = bMax - aMin; // move a forward past b
      const backward = aMax - bMin; // move a backward past b
      const step = forward < backward ? forward : -backward;
      const depth = Math.min(forward, backward);
      if (!overlapBest || depth < overlapBest.depth) {
        overlapBest = { depth, gap: -depth, move: [axis[0] * step, axis[1] * step], axis };
      }
    }
  }
  return best ?? overlapBest;
}

// The furthest a part can travel along a unit direction before it runs into
// another part or out of the area (within the tolerance), in metres.
// Obstacles it already overlaps are ignored, so a stuck part can still slide.
export function slideDistance(part, direction, others, area, limit = 10000) {
  const start = footprint(part);
  // Only parts at the same height can be run into.
  const obstacles = others.filter((other) => heightOverlap(part, other) > TOLERANCE_M)
    .map(footprint).filter((polygon) => penetration(start, polygon) <= TOLERANCE_M);
  const startOutside = outsideBy(part, area);
  const blocked = (t) => {
    const moved = { ...part, x: part.x + direction[0] * t, y: part.y + direction[1] * t };
    const polygon = footprint(moved);
    if (outsideBy(moved, area) > Math.max(startOutside, 0) + EPSILON_M) return true;
    return obstacles.some((other) => penetration(polygon, other) > EPSILON_M);
  };
  // March in 1 cm steps to the first blocked point, then bisect to 0.01 mm.
  const step = 0.01;
  let free = 0;
  let hit = null;
  for (let t = step; t <= limit; t += step) {
    if (blocked(t)) { hit = t; break; }
    free = t;
  }
  if (hit === null) return free;
  for (let i = 0; i < 10; i += 1) {
    const middle = (free + hit) / 2;
    if (blocked(middle)) hit = middle; else free = middle;
  }
  return free;
}

// Quick in-browser check: parts outside the area and overlapping pairs. Two
// parts overlap when a solid of one meets a solid of the other on the ground
// and in height (a car under a raised gate's top does not); MuJoCo remains
// the formal check (it also sees the terrain).
//
// Surface parts (roads, markings) are left out of the overlap check: they may
// cross one another, and what stands on them stands on top of them (the
// server sets those heights; MuJoCo checks them).
export function checkLayout(parts, area) {
  const outside = parts.filter((part) => outsideBy(part, area) > TOLERANCE_M + EPSILON_M).map((part) => part.id);
  const overlaps = [];
  for (let i = 0; i < parts.length; i += 1) {
    for (let j = i + 1; j < parts.length; j += 1) {
      if (parts[i].layer === "surface" || parts[j].layer === "surface") continue;
      const depth = overlapDepth(parts[i], parts[j]);
      if (depth > TOLERANCE_M + EPSILON_M) overlaps.push({ a: parts[i].id, b: parts[j].id, depth });
    }
  }
  return { outside, overlaps };
}
