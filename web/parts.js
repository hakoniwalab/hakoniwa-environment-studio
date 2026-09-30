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

// Placement parameters that do not change the shape the server resolves (how
// high it is raised, its colour, its visual asset); the plan reads the first two.
export const PLAN_PARAMS = new Set(["z_m", "color", "visual"]);

const FALLBACK_SHAPE = { envelope: { primitive: "box", width_m: 0.5, depth_m: 0.5 }, height_m: 0.5, solids: [] };

export class Parts {
  // resolve(item, params) -> Promise<shape>: asks the server for a shape.
  // resolveMany([{item, params}]) -> Promise<[shape | {error}]>: many at once
  // (optional; opening a city Recipe asks for hundreds).
  // onShape(): shapes have arrived (redraw; called once per batch of answers).
  // onError(error): a shape could not be resolved.
  constructor({ resolve, resolveMany = null, onShape = () => {}, onError = () => {} }) {
    this.resolve = resolve;
    this.resolveMany = resolveMany;
    this.onShapeNow = onShape;
    this.onError = onError;
    this.redrawQueued = false;
    this.keys = new WeakMap(); // a placement's params object -> its cache key (params are replaced when edited)
    this.setCatalog([]);
  }

  // A new Catalog: its items, and nothing cached from the last one. Answers
  // still on their way for the old Catalog are dropped (generation).
  setCatalog(items) {
    this.items = new Map(items.map((item) => [item.id, item]));
    this.shapes = new Map(); // "item + shape parameters" -> shape
    this.failed = new Set(); // keys the server refused: not asked again until they change
    this.lastShapes = new Map(); // object id -> the shape it was last drawn with
    this.pending = new Set();
    this.generation = (this.generation || 0) + 1;
  }

  item(id) {
    return this.items.get(id);
  }

  // A parameter's value for a placement: its own, else its item's.
  paramValue(part, name) {
    return part.params?.[name] ?? this.items.get(part.item)?.params?.[name];
  }

  // The parameters of a placement that change its shape, and their cache key
  // (null when it has none: the item's own shape).
  shaping(part) {
    if (!part.params) return null;
    let entry = this.keys.get(part.params);
    if (!entry || entry.item !== part.item) {
      const shaping = Object.fromEntries(Object.entries(part.params).filter(([name]) => !PLAN_PARAMS.has(name)));
      entry = Object.keys(shaping).length
        ? { item: part.item, shaping, key: JSON.stringify([part.item, Object.entries(shaping).sort()]) }
        : { item: part.item, shaping: null, key: null };
      this.keys.set(part.params, entry);
    }
    return entry.key ? entry : null;
  }

  redraw() {
    if (this.redrawQueued) return;
    this.redrawQueued = true;
    queueMicrotask(() => { this.redrawQueued = false; this.onShapeNow(); });
  }

  // Ask for the shapes of many placements at once (before drawing a Recipe).
  async prime(parts) {
    if (!this.resolveMany) return;
    const wanted = new Map();
    for (const part of parts) {
      const entry = this.items.has(part.item) && this.shaping(part);
      if (entry && !this.shapes.has(entry.key) && !this.pending.has(entry.key) && !this.failed.has(entry.key)) {
        wanted.set(entry.key, entry);
      }
    }
    if (!wanted.size) return;
    const generation = this.generation;
    const entries = [...wanted.values()];
    for (const entry of entries) this.pending.add(entry.key);
    try {
      const answers = await this.resolveMany(entries.map((entry) => ({ item: entry.item, params: entry.shaping })));
      if (generation !== this.generation) return;
      answers.forEach((answer, index) => {
        if (answer && !answer.error) this.shapes.set(entries[index].key, answer);
        else this.failed.add(entries[index].key);
      });
      if (answers.some((answer) => answer?.error)) this.onError(new Error(answers.find((answer) => answer?.error).error));
      this.redraw();
    } catch (error) {
      this.onError(error);
    } finally {
      if (generation === this.generation) for (const entry of entries) this.pending.delete(entry.key);
    }
  }

  // The shape of a placement: its item's, or the server's for parameters that
  // change it.
  shapeOf(part) {
    const item = this.items.get(part.item);
    const entry = item && this.shaping(part);
    if (!entry) return item;
    const cached = this.shapes.get(entry.key);
    if (cached) {
      this.lastShapes.set(part.id, cached);
      return cached;
    }
    if (!this.pending.has(entry.key) && !this.failed.has(entry.key)) {
      const generation = this.generation;
      this.pending.add(entry.key);
      this.resolve(part.item, entry.shaping)
        .then((shape) => {
          if (generation !== this.generation) return;
          this.shapes.set(entry.key, shape);
          this.redraw();
        })
        .catch((error) => {
          if (generation !== this.generation) return;
          this.failed.add(entry.key);
          this.onError(error);
        })
        .finally(() => { if (generation === this.generation) this.pending.delete(entry.key); });
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
      locked: shape.locked === true, // selected on the plan, never dragged (a City World layer)
      color: this.paramValue(part, "color") || "#b0b4ba",
    };
  }
}
