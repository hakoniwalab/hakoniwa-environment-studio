// 2D plan view: the environment from above, drawn in SVG in metres, the
// origin at its centre.
//
// Environment frame (docs/data-contract.md): x east (right), y north (up on
// screen), yaw counter-clockwise from east. SVG y grows downwards, so every
// point is drawn at (x, -y).
//
// Mouse: drag a part to move it (snaps to the grid and to area/part edges,
// except parts that do not snap (snap false), which only snap to the grid; hold Alt for free movement), drag the round handle to turn it (15° steps;
// hold Shift for free turning), drag the background to pan, wheel to zoom.
// Where parts are stacked (a cone on a platform), a click picks the smallest
// one under the pointer, and clicking again without dragging picks the next.
// Several parts: Shift (or Cmd / Ctrl) + click adds or removes one, Shift +
// drag on the background selects those whose centre is in the rectangle;
// dragging any selected part moves them all, and the handle above them turns
// them together about their centre.

import { bodyOutlines, bounds, contact, contains, footprint, heightOverlap, outlines } from "./geometry.js";

function area(polygon) {
  let sum = 0;
  for (let i = 0; i < polygon.length; i += 1) {
    const [x1, y1] = polygon[i];
    const [x2, y2] = polygon[(i + 1) % polygon.length];
    sum += x1 * y2 - x2 * y1;
  }
  return Math.abs(sum) / 2;
}

const SVG_NS = "http://www.w3.org/2000/svg";
// Distance, in screen pixels, within which a part edge snaps to another edge.
const EDGE_SNAP_PX = 12;
const ROTATE_STEP_DEG = 15;
// A press that moves less than this (screen pixels) is a click, not a drag.
const CLICK_SLOP_PX = 4;

function svg(tag, attributes = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  for (const child of children) node.append(child);
  return node;
}

const points = (corners) => corners.map(([x, y]) => `${x},${-y}`).join(" ");

// Positions are kept to the millimetre.
export const roundMm = (metres) => Math.round(metres * 1000) / 1000;

// Where a set of parts turns about: the middle of their positions.
export function pivotOf(parts) {
  return [parts.reduce((sum, part) => sum + part.x, 0) / parts.length, parts.reduce((sum, part) => sum + part.y, 0) / parts.length];
}

// Poses {id, x, y, yaw} of parts turned by degrees about a pivot.
export function turned(parts, degrees, [px, py]) {
  const angle = (degrees * Math.PI) / 180;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return parts.map((part) => ({
    id: part.id,
    x: roundMm(px + cos * (part.x - px) - sin * (part.y - py)),
    y: roundMm(py + sin * (part.x - px) + cos * (part.y - py)),
    yaw: ((Math.round(part.yaw + degrees) % 360) + 360) % 360,
  }));
}

// A number per solids list (the same list while a part's shape is unchanged).
const SOLIDS = new WeakMap();
let solidsCount = 0;
function solidsVersion(solids) {
  if (!solids) return 0;
  if (!SOLIDS.has(solids)) SOLIDS.set(solids, (solidsCount += 1));
  return SOLIDS.get(solids);
}

export class PlanView {
  constructor(container, { onSelect, onChange, onChangeMany, onDragEnd = () => {} }) {
    this.container = container;
    this.onSelect = onSelect; // (ids) => the new selection
    this.onChange = onChange; // (id, {x, y, yaw}) => one part moved or turned
    this.onChangeMany = onChangeMany; // ([{id, x, y, yaw}]) => several at once
    this.onDragEnd = onDragEnd; // () => a move or turn finished (one undo step)
    this.area = { minX: -10, maxX: 10, minY: -10, maxY: 10 }; // the environment's extent
    this.terrain = null; // {href, area}: an image of the ground's heights, drawn under everything
    this.parts = [];
    this.selection = new Set();
    this.moving = null; // ids dragged together (they do not snap to one another)
    this.marquee = null; // {from, to} while selecting by rectangle
    this.problems = { outside: new Set(), overlapping: new Set() };
    this.grid = 0.5;
    this.view = null; // {x, y, width, height} of the viewBox, in metres
    // Zoom, in metres per screen pixel: kept when the plan's box changes size (the
    // problem list below it grows and shrinks), so only the visible area changes.
    this.scale = null;
    this.drag = null;
    // Each part's drawing, kept while nothing it shows changes (a drag in a
    // city of hundreds of parts redraws only the part that moves).
    this.drawn = new Map(); // id -> {key, group, area}
    this.svg = svg("svg", { class: "plan", tabindex: "0" });
    this.world = svg("g");
    this.svg.append(this.world);
    container.append(this.svg);
    this.svg.addEventListener("pointerdown", (event) => this.pointerDown(event));
    this.svg.addEventListener("pointermove", (event) => this.pointerMove(event));
    this.svg.addEventListener("pointerup", (event) => this.pointerUp(event));
    this.svg.addEventListener("pointercancel", (event) => this.pointerUp(event));
    this.svg.addEventListener("wheel", (event) => this.wheel(event), { passive: false });
    new ResizeObserver(() => this.applyView()).observe(container);
  }

  setGrid(step) {
    this.grid = step;
    this.render();
  }

  // parts: [{id, name, x, y, yaw, width, depth, color}]; selected: [ids]
  setScene({ area, parts, selected, problems, terrain = null }) {
    const resized = !this.view || ["minX", "maxX", "minY", "maxY"].some((key) => area[key] !== this.area[key]);
    this.area = area;
    this.terrain = terrain;
    this.parts = parts;
    this.selection = new Set(selected);
    this.problems = problems;
    if (resized) this.fit();
    this.render();
  }

  fit() {
    const { minX, maxX, minY, maxY } = this.area;
    const margin = Math.max(maxX - minX, maxY - minY) * 0.08 + 1;
    this.view = {
      x: minX - margin, y: -maxY - margin,
      width: maxX - minX + 2 * margin, height: maxY - minY + 2 * margin,
    };
    this.scale = null; // the zoom that shows all of it, set by applyView
    this.applyView();
  }

  applyView() {
    if (!this.view) return;
    // Square pixels at the current zoom: the view is the box's size in
    // pixels times the zoom, about the same centre.
    const rect = this.container.getBoundingClientRect();
    if (rect.width && rect.height) {
      if (!this.scale) this.scale = Math.max(this.view.width / rect.width, this.view.height / rect.height);
      const cx = this.view.x + this.view.width / 2;
      const cy = this.view.y + this.view.height / 2;
      this.view.width = rect.width * this.scale;
      this.view.height = rect.height * this.scale;
      this.view.x = cx - this.view.width / 2;
      this.view.y = cy - this.view.height / 2;
    }
    const { x, y, width, height } = this.view;
    this.svg.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
    this.render();
  }

  metresPerPixel() {
    const rect = this.svg.getBoundingClientRect();
    return rect.width && this.view ? this.view.width / rect.width : 1;
  }

  // Pointer position in environment metres.
  toArea(event) {
    return this.areaAt(event.clientX, event.clientY);
  }

  // A screen point (client pixels) in environment metres.
  areaAt(clientX, clientY) {
    const point = this.svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const local = point.matrixTransform(this.svg.getScreenCTM().inverse());
    return [local.x, -local.y];
  }

  render() {
    const px = this.metresPerPixel();
    const { minX, maxX, minY, maxY } = this.area;
    // The background (terrain, grid, labels), kept while the zoom, area, grid and terrain stay.
    const backgroundKey = [px, minX, maxX, minY, maxY, this.grid, this.terrain?.href, JSON.stringify(this.terrain?.grid)].join("|");
    if (this.background?.key !== backgroundKey) {
    const nodes = [];
    if (this.terrain) {
      // Over the grid's own area when it has one (an Envsim terrain), else the environment.
      const grid = this.terrain.grid;
      const [x0, x1, y0, y1] = grid ? [-grid.east / 2, grid.east / 2, -grid.north / 2, grid.north / 2] : [minX, maxX, minY, maxY];
      nodes.push(svg("image", {
        href: this.terrain.href, x: x0, y: -y1, width: x1 - x0, height: y1 - y0,
        preserveAspectRatio: "none", class: "terrain",
      }));
    }
    // Grid lines from the origin (skipped when they would be denser than 6 px).
    const lines = (step) => {
      const path = [];
      for (let x = Math.ceil(minX / step) * step; x <= maxX + 1e-9; x += step) path.push(`M${x},${-minY}V${-maxY}`);
      for (let y = Math.ceil(minY / step) * step; y <= maxY + 1e-9; y += step) path.push(`M${minX},${-y}H${maxX}`);
      return path.join("");
    };
    if (this.grid && this.grid / px >= 6) nodes.push(svg("path", { d: lines(this.grid), class: "grid", "stroke-width": px }));
    // Every 10 m a little stronger, and the axes through the origin.
    if (10 / px >= 6) nodes.push(svg("path", { d: lines(10), class: "major", "stroke-width": px * 1.2 }));
    nodes.push(svg("path", { d: `M${minX},0H${maxX}M0,${-minY}V${-maxY}`, class: "axis", "stroke-width": px * 1.2 }));
    nodes.push(svg("rect", { x: minX, y: -maxY, width: maxX - minX, height: maxY - minY, class: "area", "stroke-width": px * 2 }));
    // North, above the area.
    const north = svg("text", { x: 0, y: -maxY - 10 * px, class: "label-area", "font-size": 12 * px }, []);
    north.textContent = "北 ↑";
    nodes.push(north);
    const size = svg("text", { x: 0, y: -minY + 18 * px, class: "label-area", "font-size": 11 * px }, []);
    size.textContent = `${roundMm(maxX - minX)} m × ${roundMm(maxY - minY)} m（原点は中心）`;
    nodes.push(size);
    this.background = { key: backgroundKey, nodes };
    }
    const nodes = [...this.background.nodes];

    // Larger parts first, so smaller ones (a cone on a platform) stay in
    // sight on top of them, and the selected part last of all. Elevated parts
    // (raised above the ground) are drawn faint and dashed, so what is
    // under them shows through.
    // Roads and markings (the surface layer) lie under everything, selected
    // too (a City World's markings would cover the city).
    // The area a part covers, once: its solids' (a City World layer's pieces:
    // its bridges cover far less than its road network, though their envelopes
    // may be as large), else its envelope's.
    const shoelace = (points) => Math.abs(points.reduce((sum, [x, y], i) => {
      const [nx, ny] = points[(i + 1) % points.length];
      return sum + x * ny - nx * y;
    }, 0)) / 2;
    const covered = (part) => (part.solids?.length
      ? part.solids.reduce((sum, solid) => sum + (solid.points?.length ? shoelace(solid.points)
        : (solid.width_m || 0) * (solid.depth_m || 0)), 0)
      : part.width * part.depth);
    const areas = new Map(this.parts.map((part) => [part, covered(part)]));
    const ordered = [...this.parts].sort((a, b) =>
      Number(b.layer === "surface") - Number(a.layer === "surface")
      || Number(this.selection.has(a.id)) - Number(this.selection.has(b.id))
      || areas.get(b) - areas.get(a));
    const single = this.selection.size === 1;
    const kept = new Map();
    for (const part of ordered) {
      const selected = this.selection.has(part.id);
      const bad = this.problems.outside.has(part.id) || this.problems.overlapping.has(part.id);
      const key = [part.x, part.y, part.yaw, part.width, part.depth, part.primitive, part.color, part.detailed,
        part.layer, part.surface, solidsVersion(part.solids), selected, bad, selected && single, px].join("|");
      const before = this.drawn.get(part.id);
      const group = before?.key === key ? before.group : this.partGroup(part, px, selected, bad, single);
      kept.set(part.id, { key, group });
      nodes.push(group);
    }
    this.drawn = kept;
    // Several selected: a dashed box around them, their pivot, and one handle
    // above them that turns them together.
    const chosen = this.parts.filter((part) => this.selection.has(part.id));
    if (chosen.length > 1) {
      const box = bounds(chosen.flatMap(footprint));
      const [cx, cy] = pivotOf(chosen);
      const top = box.maxY + 36 * px;
      nodes.push(svg("rect", {
        x: box.minX, y: -box.maxY, width: box.maxX - box.minX, height: box.maxY - box.minY,
        class: "group-box", "stroke-width": px * 1.5,
      }));
      nodes.push(svg("circle", { cx, cy: -cy, r: 4 * px, class: "group-pivot" }));
      nodes.push(svg("line", { x1: cx, y1: -cy, x2: cx, y2: -top, class: "handle-bar", "stroke-width": px }));
      nodes.push(svg("circle", {
        cx, cy: -top, r: 8 * px, class: "rotate-handle", "data-handle": "rotate-group", "stroke-width": px * 1.5,
      }));
    }
    if (this.marquee) {
      const { from, to } = this.marquee;
      nodes.push(svg("rect", {
        x: Math.min(from[0], to[0]), y: -Math.max(from[1], to[1]),
        width: Math.abs(to[0] - from[0]), height: Math.abs(to[1] - from[1]), class: "marquee", "stroke-width": px,
      }));
    }
    // Snap guides while dragging: the parts it attached to and the aligned edges.
    if (this.guides) {
      for (const part of this.parts) {
        if (!this.guides.targets.has(part.id)) continue;
        nodes.push(svg("polygon", { points: points(footprint(part)), class: "snap-target", "stroke-width": px * 2 }));
      }
      for (const line of this.guides.lines) {
        const d = "x" in line ? `M${line.x},${-minY + 1}V${-maxY - 1}` : `M${minX - 1},${-line.y}H${maxX + 1}`;
        nodes.push(svg("path", { d, class: "snap-line", "stroke-width": px * 1.5 }));
      }
    }
    // Only what changed is put in (a drag changes one part's drawing).
    const current = [...this.world.childNodes];
    const changed = nodes.map((node, index) => index).filter((index) => current[index] !== nodes[index]);
    if (current.length !== nodes.length || changed.some((index) => nodes[index].parentNode === this.world)) {
      this.world.replaceChildren(...nodes); // reordered (a new selection draws last): all at once
    } else {
      for (const index of changed) this.world.replaceChild(nodes[index], current[index]);
    }
  }

  // One part as drawn on the plan: its outlines, facing tick, label and (when
  // it alone is selected) its rotate handle.
  partGroup(part, px, selected, bad, single) {
    const shapes = outlines(part);
    const corners = shapes.flatMap((shape) => shape.polygon);
    const mounted = part.surface === "elevated";
    const group = svg("g", {
      class: `part${mounted ? " mounted" : ""}${part.layer === "surface" ? " surface" : ""}${selected ? " selected" : ""}${bad ? " problem" : ""}`,
      "data-id": part.id,
    });
    for (const shape of shapes) {
      group.append(svg("polygon", {
        points: points(shape.polygon), fill: shape.color,
        class: shape.collide ? "" : "paint", "stroke-width": px * (selected ? 3 : 1.5),
      }));
    }
    // A tick on the local +y side marks the part's facing, for rotations
    // (a part drawn by its solids shows its own shape instead).
    const yaw = (part.yaw * Math.PI) / 180;
    const front = [part.x - Math.sin(yaw) * part.depth / 2, part.y + Math.cos(yaw) * part.depth / 2];
    if (!part.detailed) {
      group.append(svg("line", {
        x1: part.x, y1: -part.y, x2: front[0], y2: -front[1], class: "facing", "stroke-width": px * 1.5,
      }));
    }
    // The id only when it fits inside the part (about 6.5 px per character).
    const box = bounds(corners);
    // (A part drawn by its solids only when selected: its box says little about where its shape has room.)
    if ((!part.detailed || selected) && (box.maxX - box.minX) / px > part.id.length * 6.5 + 8
      && (box.maxY - box.minY) / px > 14) {
      const label = svg("text", { x: part.x, y: -part.y + 4 * px, class: "label", "font-size": 11 * px, "stroke-width": 3 * px });
      label.textContent = part.id;
      group.append(label);
    }
    if (selected && single) {
      const reach = part.depth / 2 + 28 * px;
      const handle = [part.x - Math.sin(yaw) * reach, part.y + Math.cos(yaw) * reach];
      group.append(svg("line", {
        x1: front[0], y1: -front[1], x2: handle[0], y2: -handle[1], class: "handle-bar", "stroke-width": px,
      }));
      group.append(svg("circle", {
        cx: handle[0], cy: -handle[1], r: 7 * px, class: "rotate-handle", "data-handle": "rotate",
        "stroke-width": px * 1.5,
      }));
    }
    return group;
  }

  pointerDown(event) {
    this.svg.focus();
    const target = event.target;
    const group = target.closest?.("g.part");
    const [x, y] = this.toArea(event);
    // Keep receiving moves outside the SVG; a pointer the browser does not
    // track (a scripted event) cannot be captured, which is harmless.
    try { this.svg.setPointerCapture(event.pointerId); } catch { /* not a live pointer */ }
    if (target.dataset?.handle === "rotate" && this.selection.size === 1) {
      this.drag = { mode: "rotate", id: [...this.selection][0] };
      return;
    }
    const chosen = this.parts.filter((part) => this.selection.has(part.id));
    if (target.dataset?.handle === "rotate-group" && chosen.length > 1) {
      const pivot = pivotOf(chosen);
      this.drag = { mode: "rotate-group", pivot, base: chosen.map((part) => ({ ...part })),
                    from: (Math.atan2(y - pivot[1], x - pivot[0]) * 180) / Math.PI };
      return;
    }
    const additive = event.shiftKey || event.metaKey || event.ctrlKey;
    const hits = this.partsAt([x, y]);
    if (group || hits.length) {
      const hit = hits[0] || this.parts.find((item) => item.id === group.dataset.id);
      if (additive) {
        // Add the part to the selection, or take it out.
        const ids = new Set(this.selection);
        if (ids.has(hit.id)) ids.delete(hit.id); else ids.add(hit.id);
        this.onSelect([...ids]);
        return;
      }
      const inSelection = hits.find((item) => this.selection.has(item.id));
      if (chosen.length > 1 && inSelection) {
        // Drag the whole selection by the part under the pointer.
        this.moving = new Set(this.selection);
        this.drag = {
          mode: "move", id: inSelection.id, offset: [inSelection.x - x, inSelection.y - y], moved: false,
          start: [event.clientX, event.clientY], base: chosen.map((part) => ({ id: part.id, x: part.x, y: part.y })),
        };
        return;
      }
      // Keep dragging the selected part when it is under the pointer; otherwise
      // take the smallest part there. A locked layer (a City World's roads,
      // under everything) is not kept: it would hold every click on the city.
      const part = (inSelection && !inSelection.locked ? inSelection : null) || hit;
      this.onSelect([part.id]);
      if (part.locked) {  // a layer under everything: dragging pans instead of moving it
        this.drag = { mode: "pan", start: [event.clientX, event.clientY], view: { ...this.view } };
        return;
      }
      this.drag = {
        mode: "move", id: part.id, offset: [part.x - x, part.y - y], moved: false, start: [event.clientX, event.clientY],
        // A click (no drag) on an already selected part goes to the next one under it.
        next: part === inSelection && hits.length > 1 ? hits[(hits.indexOf(part) + 1) % hits.length].id : null,
      };
      return;
    }
    if (additive) {
      this.drag = { mode: "marquee", base: [...this.selection] };
      this.marquee = { from: [x, y], to: [x, y] };
      return;
    }
    this.onSelect([]);
    this.drag = { mode: "pan", start: [event.clientX, event.clientY], view: { ...this.view } };
  }

  pointerMove(event) {
    const drag = this.drag;
    if (!drag) return;
    if (drag.mode === "pan") {
      const px = this.metresPerPixel();
      this.view.x = drag.view.x - (event.clientX - drag.start[0]) * px;
      this.view.y = drag.view.y - (event.clientY - drag.start[1]) * px;
      this.applyView();
      return;
    }
    const [x, y] = this.toArea(event);
    if (drag.mode === "marquee") {
      this.marquee.to = [x, y];
      const { from } = this.marquee;
      const inside = this.parts.filter((item) => item.x >= Math.min(from[0], x) && item.x <= Math.max(from[0], x)
        && item.y >= Math.min(from[1], y) && item.y <= Math.max(from[1], y)).map((item) => item.id);
      this.onSelect([...new Set([...drag.base, ...inside])]);
      return;
    }
    if (drag.mode === "rotate-group") {
      let degrees = (Math.atan2(y - drag.pivot[1], x - drag.pivot[0]) * 180) / Math.PI - drag.from;
      if (!event.shiftKey) degrees = Math.round(degrees / ROTATE_STEP_DEG) * ROTATE_STEP_DEG;
      this.onChangeMany(turned(drag.base, degrees, drag.pivot));
      return;
    }
    const part = this.parts.find((item) => item.id === drag.id);
    if (!part) return;
    if (drag.mode === "rotate") {
      // The handle sits on the part's local +y side: local +y points at the pointer.
      let yaw = (Math.atan2(y - part.y, x - part.x) * 180) / Math.PI - 90;
      if (!event.shiftKey) yaw = Math.round(yaw / ROTATE_STEP_DEG) * ROTATE_STEP_DEG;
      this.onChange(part.id, { yaw: ((Math.round(yaw) % 360) + 360) % 360 });
      return;
    }
    if (!drag.moved && Math.hypot(event.clientX - drag.start[0], event.clientY - drag.start[1]) < CLICK_SLOP_PX) return;
    let nx = x + drag.offset[0];
    let ny = y + drag.offset[1];
    if (!event.altKey) [nx, ny] = this.snap(part, nx, ny);
    else this.guides = null;
    drag.moved = true;
    if (drag.base) {
      // The others follow the dragged part by the same amount.
      const primary = drag.base.find((item) => item.id === part.id);
      const dx = roundMm(nx) - primary.x;
      const dy = roundMm(ny) - primary.y;
      this.onChangeMany(drag.base.map((item) => ({ id: item.id, x: item.x + dx, y: item.y + dy })));
      return;
    }
    this.onChange(part.id, { x: roundMm(nx), y: roundMm(ny) });
  }

  // Parts whose outline contains a point, smallest first.
  partsAt(point) {
    return this.parts.map((part) => ({ part, polygons: bodyOutlines(part) }))
      .filter(({ polygons }) => polygons.some((polygon) => contains(polygon, point)))
      .sort((a, b) => Number(a.part.layer === "surface") - Number(b.part.layer === "surface")
        || area(footprint(a.part)) - area(footprint(b.part)))
      .map(({ part }) => part);
  }

  pointerUp(event) {
    const drag = this.drag;
    if (drag) this.svg.releasePointerCapture?.(event.pointerId);
    if (drag?.mode === "move" && !drag.moved) {
      // A click on one of several selected parts picks that part alone.
      if (drag.base) this.onSelect([drag.id]);
      else if (drag.next) this.onSelect([drag.next]);
    }
    this.drag = null;
    const edited = drag && (drag.mode.startsWith("rotate") || (drag.mode === "move" && drag.moved));
    this.moving = null;
    if (this.marquee) {
      this.marquee = null;
      this.render();
    }
    if (this.guides) {
      this.guides = null;
      this.render();
    }
    if (edited) this.onDragEnd();
  }

  // Where a dragged part lands (hold Alt to skip all of this):
  //  1. its centre snaps to the grid;
  //  2. contact: a gap to a neighbour or an area edge within EDGE_SNAP_PX closes,
  //     and a small overlap is pushed out, so the part ends flush against it,
  //     along up to two different directions (e.g. into a corner);
  //  3. alignment: on an axis contact left free, its box edges line up with
  //     another part's (walls or rails in a row).
  // A part that does not snap (part.snap false: a cone, a sign) stops after 1,
  // and is never a target for other parts either.
  // this.guides records what it snapped to, for render().
  snap(part, x, y) {
    if (this.grid) {
      x = roundMm(Math.round(x / this.grid) * this.grid);
      y = roundMm(Math.round(y / this.grid) * this.grid);
    }
    if (part.snap === false) {
      this.guides = null;
      return [x, y];
    }
    const reach = EDGE_SNAP_PX * this.metresPerPixel();
    // Push out of an overlap only when it is shallow, so a part never jumps far.
    const pushLimit = Math.max(reach, 0.4 * Math.min(part.width, part.depth));
    // Parts drawn by their solids (footprints, roads) are no snap targets: their envelope is not their shape.
    const others = this.parts.filter((other) => other.id !== part.id && other.snap !== false && !other.detailed
      && !this.moving?.has(other.id));
    const used = [];
    const guides = { targets: new Set(), lines: [] };
    const parallel = (axis) => used.some((other) => Math.abs(other[0] * axis[0] + other[1] * axis[1]) > 0.99);

    for (let pass = 0; pass < 2; pass += 1) {
      const moving = footprint({ ...part, x, y });
      const candidates = [];
      for (const other of others) {
        // Only parts at the same height can be touched (a cone under a gate's bar cannot).
        if (heightOverlap(part, other) <= 0) continue;
        const touch = contact(moving, footprint(other));
        if (touch) candidates.push({ ...touch, target: other.id });
      }
      const box = bounds(moving);
      for (const [gap, axis, move] of [
        [box.minX - this.area.minX, [1, 0], [this.area.minX - box.minX, 0]],
        [this.area.maxX - box.maxX, [1, 0], [this.area.maxX - box.maxX, 0]],
        [box.minY - this.area.minY, [0, 1], [0, this.area.minY - box.minY]],
        [this.area.maxY - box.maxY, [0, 1], [0, this.area.maxY - box.maxY]],
      ]) candidates.push({ gap, axis, move, target: null });
      let best = null;
      for (const candidate of candidates) {
        const limit = candidate.gap > 0 ? reach : pushLimit;
        if (Math.abs(candidate.gap) > limit || parallel(candidate.axis)) continue;
        const size = Math.hypot(candidate.move[0], candidate.move[1]);
        if (!best || size < best.size) best = { ...candidate, size };
      }
      if (!best) break;
      x += best.move[0];
      y += best.move[1];
      used.push(best.axis);
      if (best.target) guides.targets.add(best.target);
    }

    // Alignment of box edges on the axes contact left free.
    const box = bounds(footprint({ ...part, x, y }));
    const edges = { x: [], y: [] };
    for (const other of others) {
      const b = bounds(footprint(other));
      edges.x.push(b.minX, b.maxX);
      edges.y.push(b.minY, b.maxY);
    }
    const align = (own, targets) => {
      let best = null;
      for (const edge of own) {
        for (const target of targets) {
          const delta = target - edge;
          if (Math.abs(delta) <= reach && (best === null || Math.abs(delta) < Math.abs(best.delta))) best = { delta, at: target };
        }
      }
      return best;
    };
    if (!parallel([1, 0])) {
      const hit = align([box.minX, box.maxX], edges.x);
      if (hit) { x += hit.delta; guides.lines.push({ x: hit.at }); }
    }
    if (!parallel([0, 1])) {
      const hit = align([box.minY, box.maxY], edges.y);
      if (hit) { y += hit.delta; guides.lines.push({ y: hit.at }); }
    }
    this.guides = guides;
    return [x, y];
  }

  wheel(event) {
    event.preventDefault();
    const [x, y] = this.toArea(event);
    const factor = Math.exp(event.deltaY * 0.0015);
    const view = this.view;
    const svgY = -y;
    view.x = x - (x - view.x) * factor;
    view.y = svgY - (svgY - view.y) * factor;
    view.width *= factor;
    view.height *= factor;
    if (this.scale) this.scale *= factor;
    this.applyView();
  }
}
