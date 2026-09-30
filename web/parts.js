// Recipe objects resolved for the plan and the checks, from the Catalog items
// the server sends (GET /api/catalogs/<id>): the browser knows no types.
//
// An item comes with its resolved shape (solids with their outline seen from
// above and their height range, envelope, height), behaviour (surface, snap)
// and its parameters. A placement whose own parameters change the shape (a
// gate's opening, a wall's length) is resolved by the server (POST
// /api/resolve) and cached; while that answer is on its way the object keeps
// the shape it was last drawn with. tests/parts_check.mjs covers this module.
//
// Heights here are above the ground under the object: the plan's quick check
// compares objects as if the ground were flat; the server sets each object on
// the terrain and MuJoCo checks the real heights.

import { normalizeYaw } from "./geometry.js";

// Placement parameters the plan reads directly (how high it is raised, its
// colour); only the others change the shape the server resolves.
export const PLAN_PARAMS = new Set(["z_m", "color"]);

const FALLBACK_SHAPE = { envelope: { primitive: "box", width_m: 0.5, depth_m: 0.5 }, height_m: 0.5, solids: [] };

export class Parts {
  // resolve(item, params) -> Promise<shape>: asks the server for a shape.
  // onShape(): a shape has arrived (redraw). onError(error): it failed.
  constructor({ resolve, onShape = () => {}, onError = () => {} }) {
    this.resolve = resolve;
    this.onShape = onShape;
    this.onError = onError;
    this.setCatalog([]);
  }

  // A new Catalog: its items, and nothing cached from the last one.
  setCatalog(items) {
    this.items = new Map(items.map((item) => [item.id, item]));
    this.shapes = new Map(); // "item + shape parameters" -> shape
    this.lastShapes = new Map(); // object id -> the shape it was last drawn with
    this.pending = new Set();
  }

  item(id) {
    return this.items.get(id);
  }

  // A parameter's value for a placement: its own, else its item's.
  paramValue(part, name) {
    return part.params?.[name] ?? this.items.get(part.item)?.params?.[name];
  }

  // The shape of a placement: its item's, or the server's for parameters that
  // change it.
  shapeOf(part) {
    const item = this.items.get(part.item);
    const shaping = Object.fromEntries(Object.entries(part.params || {}).filter(([name]) => !PLAN_PARAMS.has(name)));
    if (!item || !Object.keys(shaping).length) return item;
    const key = JSON.stringify([part.item, Object.entries(shaping).sort()]);
    const cached = this.shapes.get(key);
    if (cached) {
      this.lastShapes.set(part.id, cached);
      return cached;
    }
    if (!this.pending.has(key)) {
      this.pending.add(key);
      this.resolve(part.item, shaping)
        .then((shape) => { this.shapes.set(key, shape); this.onShape(); })
        .catch((error) => this.onError(error))
        .finally(() => this.pending.delete(key));
    }
    return this.lastShapes.get(part.id) || item;
  }

  // An object in plan units (metres): position, envelope, solids, height of
  // its base above the ground under it (its z_m when elevated), behaviour.
  resolved(part) {
    const item = this.items.get(part.item);
    const shape = this.shapeOf(part) || FALLBACK_SHAPE;
    const surface = shape.surface || "ground";
    return {
      id: part.id,
      name: item?.name || part.item,
      x: part.pose.x_m,
      y: part.pose.y_m,
      yaw: normalizeYaw(part.pose.yaw_deg ?? 0),
      primitive: shape.envelope.primitive,
      width: shape.envelope.width_m,
      depth: shape.envelope.depth_m,
      height: shape.height_m,
      solids: shape.solids,
      z: surface === "elevated" ? this.paramValue(part, "z_m") ?? 0 : 0,
      surface,
      layer: shape.layer || "object",
      // Its envelope is not its shape (a footprint, a road along a line): the plan draws its solids.
      detailed: shape.layer === "surface" || shape.solids.some((solid) => solid.primitive === "prism"),
      snap: shape.snap !== false,
      color: this.paramValue(part, "color") || "#b0b4ba",
    };
  }
}
