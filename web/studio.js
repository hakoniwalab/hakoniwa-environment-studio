// Environment Studio: edits one Environment Recipe (docs/data-contract.md) in
// the browser. The Recipe is kept as its YAML mapping (size_m, terrain,
// objects); the plan view gets resolved objects. The browser knows no types:
// the server sends each Catalog item's resolved solids, envelope and
// behaviour, and the parameters a placement may change; a placement whose
// parameters change the shape is resolved by the server too (POST
// /api/resolve, cached). Metres, ENU, origin at the environment's centre.

import { $, api, el, warnIfServerIsOld } from "./dom.js";
import { History } from "./history.js";
import { createInspector } from "./inspector.js";
import { Parts } from "./parts.js";
import { PlanView, pivotOf, roundMm, turned } from "./plan2d.js";
import { normalizeYaw, slideDistance } from "./geometry.js";
import { fromServer, quickProblems, renderProblems } from "./problems.js";
import { terrainImage } from "./terrain.js";

const PREVIEW_DELAY_MS = 300;
const VALIDATE_DELAY_MS = 300;
const NUDGE_FAR = 10; // Shift + arrow moves ten grid steps
const ITEM_DRAG = "application/x-hakoniwa-item"; // a Catalog part dragged from the list
const NUDGE_FINE_M = 0.01; // Alt + arrow
const HISTORY_LIMIT = 200; // undo steps kept
// Copied objects go to the system clipboard as JSON text with this format tag
// (the browser's own copy / cut / paste events, so the Edit menu works too).
const CLIPBOARD_FORMAT = "hakoniwa-environment-objects/v1";
const PASTE_STEP_M = 1; // each paste of the same objects lands this much further
const DEFAULT_SIZE = { east: 20, north: 30 };

// The Catalog's items and the objects resolved from them (web/parts.js).
const catalog = new Parts({
  resolve: (item, params) => api("POST", "resolve", { catalog_id: state.catalogId, item, params }),
  resolveMany: (placements) => api("POST", "resolve-many", { catalog_id: state.catalogId, placements })
    .then((answer) => answer.shapes),
  onShape: () => render({ live: true }),
  onError: (error) => setStatus(error.message, "error"),
});

const state = {
  catalogs: [], // [{id, name, description, items}] from GET /api/catalogs
  catalogId: null, // the Catalog the current Recipe uses
  catalogInfo: null, // its name and description
  recipes: [],
  current: null, // {id, editable, recipe}
  selection: [], // ids of the selected objects (one: the inspector edits it; several: move and turn together)
  // Undo / redo (web/history.js): the Recipe as JSON is recorded whenever
  // editing settles, so a drag or a slider counts as one step.
  history: new History(HISTORY_LIMIT, sameStep),
  lastPaste: { text: null, count: 0 },
  saved: null, // snapshot of the last saved or opened Recipe
  plan: null,
  view3d: null, // web/view3d.js, created when the 3D view is first shown
  view3dFailed: false,
  openedId: null, // the saved Recipe being edited, if any
  previewTimer: null,
  previewVersion: 0,
  previewShape: null, // shapeKey() of the GLB in the 3D view
  validation: null, // {layout, result}: the server's check and the layout it covered
  validateTimer: null,
  terrain: { key: null, image: null }, // the ground's image on the plan, and what it shows
};

function setStatus(message, kind = "") {
  const node = $("#status");
  node.textContent = message;
  node.className = `status ${kind}`;
}

function snapshot() {
  return state.current ? JSON.stringify({ id: state.current.id, recipe: state.current.recipe }) : null;
}

const isDirty = () => state.current && snapshot() !== state.saved;

// --- Recipe helpers ---------------------------------------------------------------

function recipe() {
  return state.current.recipe;
}

function objectById(id) {
  return recipe().objects.find((obj) => obj.id === id);
}

function selectedObjects() {
  return state.selection.map(objectById).filter(Boolean);
}

function select(ids) {
  state.selection = ids;
  render();
}

// A Recipe object resolved for the plan and the checks (web/parts.js).
const resolved = (obj) => catalog.resolved(obj);

// The environment's extent, centred on the origin.
function area() {
  const size = recipe().size_m;
  return { minX: -size.east / 2, maxX: size.east / 2, minY: -size.north / 2, maxY: size.north / 2 };
}

function uniqueId(prefix) {
  const used = new Set(recipe().objects.map((obj) => obj.id));
  let index = 1;
  while (used.has(`${prefix}-${index}`)) index += 1;
  return `${prefix}-${index}`;
}

// What the server needs to check, preview or save the Recipe as it is now.
function recipeBody() {
  const { name, description, size_m: size, terrain, objects, geo } = recipe();
  return { name, description, size_m: size, terrain, objects, geo, catalog_id: state.catalogId };
}

// --- Rendering --------------------------------------------------------------------

// live: while a slider is being dragged, update the plan and the 3D view but
// leave the panels (and the slider in them) alone.
// Unsaved changes: a mark beside the name, and the save button stands out.
function renderDirty() {
  const dirty = Boolean(isDirty());
  $("#dirty").hidden = !dirty;
  $("#save").classList.toggle("attention", dirty);
}

function render({ live = false } = {}) {
  const current = state.current;
  if (!current) return; // shapes can arrive before the first Recipe is open
  $("#recipe-id").value = current.id;
  $("#recipe-name").value = recipe().name || "";
  const size = recipe().size_m;
  $("#size-east").value = size.east;
  $("#size-north").value = size.north;
  const preset = `${size.east}x${size.north}`;
  $("#size-preset").value = [...$("#size-preset").options].some((option) => option.value === preset) ? preset : "";

  const views = recipe().objects.map(resolved);
  // The server's check once it covers this exact layout; the quick check until
  // then. While a part is dragged the quick check alone: the key stringifies
  // every object, and the drag's end renders again with it.
  const dragging = live && Boolean(state.plan.drag);
  const layout = dragging ? null : layoutKey();
  const checked = layout && state.validation?.layout === layout ? state.validation.result : null;
  const problems = checked ? fromServer(checked, recipe().objects) : quickProblems(views, area());
  state.plan.setScene({
    area: area(), parts: views, selected: state.selection, terrain: state.terrain.image,
    problems: { outside: problems.outside, overlapping: problems.overlapping },
  });
  renderProblems($("#problems"), problems, Boolean(checked), recipe().objects.length > 0);
  if (!dragging) scheduleValidation(layout);
  refreshTerrain();
  // While a slider is held, rebuilding the panels would drop it.
  if (!live && !inspector.sliding) {
    renderCatalogSelect();
    renderTerrainPanel();
    inspector.render();
    renderRecipeList();
  }
  state.view3d?.setSelected(state.selection);
  renderDirty();
  followPoses();
  schedulePreview();
  if (!live && !inspector.sliding && !state.plan.drag) checkpoint();
}

// --- Undo / redo (Cmd / Ctrl + Z, Cmd / Ctrl + Shift + Z or Ctrl + Y) ------------

// An undo step: the Recipe without its objects, and each object as JSON. An
// object unchanged since the last step keeps that step's string, so a city of
// hundreds of parts costs one string per part moved, not the whole Recipe.
function step() {
  const kept = new Map((state.history.head?.objects || []).map((text) => [text, text]));
  return {
    rest: JSON.stringify({ ...recipe(), objects: null }), // objects keep their place among the keys
    objects: recipe().objects.map((obj) => {
      const text = JSON.stringify(obj);
      return kept.get(text) ?? text;
    }),
  };
}

function sameStep(a, b) {
  return a === b || (a !== null && b !== null && a.rest === b.rest && a.objects.length === b.objects.length
    && a.objects.every((text, index) => text === b.objects[index]));
}

function resetHistory() {
  state.history.reset(null);
  state.history.reset(step());
}

function checkpoint() {
  state.history.record(step());
}

function showSnapshot(snapshot, message) {
  if (snapshot === null) return;
  state.current.recipe = { ...JSON.parse(snapshot.rest), objects: snapshot.objects.map((text) => JSON.parse(text)) };
  const ids = new Set(recipe().objects.map((obj) => obj.id));
  state.selection = state.selection.filter((id) => ids.has(id));
  render();
  setStatus(message);
}

// Record first: an edit that has not settled yet (just after a drag) is a step too.
const undo = () => { checkpoint(); showSnapshot(state.history.undo(), "元に戻しました"); };
const redo = () => { checkpoint(); showSnapshot(state.history.redo(), "やり直しました"); };

// --- Copy, cut and paste (the browser's clipboard events) -------------------------

const editingText = (event) => event.target.closest?.("input, select, textarea");

function onCopy(event, cut) {
  const chosen = selectedObjects();
  if (!state.current || editingText(event) || !chosen.length) return;
  event.clipboardData.setData("text/plain", JSON.stringify({
    format: CLIPBOARD_FORMAT, catalog_id: state.catalogId, objects: chosen,
  }, null, 2));
  event.preventDefault();
  if (cut) deleteSelected();
  setStatus(`${chosen.length} 個の部品を${cut ? "切り取り" : "コピーし"}ました`);
}

// Paste copied objects with new ids, a little further from the originals each time.
function onPaste(event) {
  if (!state.current || editingText(event)) return;
  const text = event.clipboardData.getData("text/plain");
  let data;
  try { data = JSON.parse(text); } catch { return; }
  if (data?.format !== CLIPBOARD_FORMAT) return;
  event.preventDefault();
  const valid = Array.isArray(data.objects) && data.objects.every((obj) => obj && typeof obj.item === "string"
    && obj.pose && Number.isFinite(obj.pose.x_m) && Number.isFinite(obj.pose.y_m));
  if (!valid) {
    setStatus("貼り付けたデータの形が正しくありません", "error");
    return;
  }
  if (data.catalog_id !== state.catalogId) {
    setStatus("コピーした部品は別のカタログの部品です。同じカタログの環境に貼り付けてください。", "error");
    return;
  }
  state.lastPaste = { text, count: state.lastPaste.text === text ? state.lastPaste.count + 1 : 1 };
  const offset = PASTE_STEP_M * state.lastPaste.count;
  const pasted = data.objects.filter((obj) => catalog.item(obj.item));
  state.selection = pasted.map((obj) => {
    const copy = JSON.parse(JSON.stringify(obj));
    copy.id = uniqueId(catalog.item(obj.item).id_prefix || "object");
    copy.pose.x_m = roundMm(obj.pose.x_m + offset);
    copy.pose.y_m = roundMm(obj.pose.y_m - offset);
    recipe().objects.push(copy);
    return copy.id;
  });
  render();
  setStatus(`${pasted.length} 個の部品を貼り付けました`);
}

// --- 3D preview -------------------------------------------------------------------

function viewMode() {
  return $("#view-mode [aria-pressed='true']").dataset.mode;
}

function setViewMode(mode) {
  for (const button of document.querySelectorAll("#view-mode button")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
  $("#views").className = `views mode-${mode}`;
  state.view3d?.setActive(mode !== "plan");
  if (mode !== "plan") ensure3d();
  schedulePreview();
}

async function ensure3d() {
  if (state.view3d || state.view3dFailed) return;
  const host = $("#view3d");
  try {
    const { View3D } = await import("./view3d.js");
    state.view3d = new View3D(host);
    // A click in 3D picks a part as on the plan (Shift adds or takes it out); moving stays on the plan.
    state.view3d.onSelect = (id, additive) => {
      if (!additive) return select(id ? [id] : []);
      if (!id) return;
      select(state.selection.includes(id) ? state.selection.filter((item) => item !== id) : [...state.selection, id]);
    };
    host.querySelector(".view3d-message")?.remove();
    schedulePreview(0);
  } catch (error) {
    state.view3dFailed = true;
    host.querySelector(".view3d-message").textContent =
      `3D を表示できません（WebGL と、three.js の取得にネットワークが必要です）: ${error.message}`;
  }
}

// Everything the GLB depends on except where objects stand: while this stays
// the same, the 3D view moves its nodes instead of loading a new GLB.
function shapeKey() {
  const { size_m: size, terrain, objects } = recipe();
  return JSON.stringify({ catalog: state.catalogId, size, terrain, objects: objects.map(({ pose, ...rest }) => rest) });
}

// Show moves at once (a drag follows the pointer); the heights (terrain, roads
// under an object) stay as shown until the server's answer.
function followPoses() {
  if (!state.view3d || !state.current || viewMode() === "plan" || state.previewShape !== shapeKey()) return;
  state.view3d.setPoses(Object.fromEntries(recipe().objects.map((obj) => [obj.id, {
    translation: [obj.pose.x_m, undefined, -obj.pose.y_m], yaw_deg: obj.pose.yaw_deg || 0,
  }])));
}

// Ask the server for the Recipe's GLB once editing pauses; a newer edit wins.
// When only poses changed, ask for the poses (heights included) instead.
function schedulePreview(delay = PREVIEW_DELAY_MS) {
  if (!state.view3d || !state.current || viewMode() === "plan") return;
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(refreshPreview, delay);
}

async function refreshPreview() {
  const version = (state.previewVersion += 1);
  const shape = shapeKey();
  try {
    if (shape === state.previewShape) {
      const { poses } = await api("POST", "poses", recipeBody());
      if (version !== state.previewVersion) return;
      if (state.view3d.setPoses(poses)) return;
    }
    const response = await fetch("/api/glb", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(recipeBody()),
    });
    if (!response.ok) throw new Error((await response.json()).error);
    const buffer = await response.arrayBuffer();
    if (version !== state.previewVersion) return;
    // Parsing is asynchronous too: a newer preview may finish first.
    await state.view3d.setGlb(buffer, recipe().size_m, () => version === state.previewVersion);
    if (version === state.previewVersion) state.previewShape = shape;
    state.view3d.setSelected(state.selection);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

// --- Problems: the quick check, then the server's (schema and MuJoCo) -------------

// The layout a check covers: the size, the terrain and every object.
function layoutKey() {
  const { size_m: size, terrain, objects } = recipe();
  return JSON.stringify({ catalog: state.catalogId, size, terrain, objects });
}

// Check the layout on the server once editing pauses; a stale answer is ignored.
function scheduleValidation(current = layoutKey()) {
  if (!state.current || state.validation?.layout === current) return;
  clearTimeout(state.validateTimer);
  state.validateTimer = setTimeout(async () => {
    const layout = layoutKey();
    try {
      const result = await api("POST", "validate", recipeBody());
      state.validation = { layout, result };
      if (layout === layoutKey()) render();
    } catch (error) {
      setStatus(error.message, "error");
    }
  }, VALIDATE_DELAY_MS);
}

function renameObject(obj, id) {
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
    setStatus("ID は小文字・数字・- _ で付けてください", "error");
  } else if (id !== obj.id && objectById(id)) {
    setStatus(`ID ${id} はもう使われています`, "error");
  } else {
    obj.id = id;
    state.selection = [id];
    setStatus("");
  }
  render();
}

// --- Terrain ----------------------------------------------------------------------

const terrainItems = () => [...catalog.items.values()].filter((item) => item.kind === "terrain");

// The ground on the plan: an image of the terrain's heights (lighter is
// higher), fetched when the terrain, its parameters or the size change.
async function refreshTerrain() {
  const { terrain, size_m: size } = recipe();
  const key = JSON.stringify([state.catalogId, terrain, size]);
  if (state.terrain.key === key) return;
  state.terrain.key = key;
  try {
    const ground = await api("POST", "terrain", { catalog_id: state.catalogId, terrain, size_m: size });
    if (state.terrain.key !== key) return;
    state.terrain.image = { href: terrainImage(ground), grid: ground.grid_m }; // an Envsim terrain covers its own area
    render({ live: true });
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function renderTerrainPanel() {
  const select = $("#terrain-select");
  const current = recipe().terrain?.item;
  select.replaceChildren(...terrainItems().map((item) => el("option", { value: item.id, selected: item.id === current }, item.name)));
  select.value = current;
  const item = catalog.item(current);
  if (!item) {
    $("#terrain-params").replaceChildren();
    return;
  }
  const store = (name, value) => {
    const terrain = recipe().terrain;
    terrain.params = { ...(terrain.params || {}) };
    if (value === undefined) delete terrain.params[name];
    else terrain.params[name] = value;
    if (!Object.keys(terrain.params).length) delete terrain.params;
  };
  $("#terrain-params").replaceChildren(
    ...inspector.paramFields(item.placement_params, recipe().terrain.params || {}, item.params, store));
}

function setTerrain(itemId) {
  recipe().terrain = { item: itemId };
  render();
}

// --- Catalogs (catalogs/<id>/catalog.yaml) -----------------------------------------

async function loadCatalog(id) {
  if (!id || (id === state.catalogId && catalog.items.size)) return;
  const loaded = await api("GET", `catalogs/${id}`);
  state.catalogId = id;
  state.catalogInfo = { name: loaded.name, description: loaded.description };
  catalog.setCatalog(loaded.items);
  state.terrain = { key: null, image: null };
  renderCatalog();
}

// A Recipe keeps its Catalog; it can change to one that has every part it
// uses (マイカタログ includes the starter Catalog), or freely while it is empty.
async function switchCatalog(id) {
  if (state.current && recipe().objects.length) {
    const other = await api("GET", `catalogs/${id}`);
    const ids = new Set(other.items.map((item) => item.id));
    const missing = [...new Set([recipe().terrain?.item, ...recipe().objects.map((obj) => obj.item)])]
      .filter((item) => item && !ids.has(item));
    if (missing.length) {
      $("#catalog-select").value = state.catalogId;
      setStatus(`この環境の部品（${missing.slice(0, 3).join("、")}${missing.length > 3 ? " ほか" : ""}）が「${other.name}」にないので、カタログを変えられません`, "error");
      return;
    }
  }
  await loadCatalog(id);
  if (state.current && !catalog.item(recipe().terrain?.item)) recipe().terrain = { item: terrainItems()[0]?.id };
  render();
}

function renderCatalogSelect() {
  const select = $("#catalog-select");
  select.replaceChildren(...state.catalogs.filter((item) => !item.error).map((item) =>
    el("option", { value: item.id, selected: item.id === state.catalogId }, item.name || item.id)));
  select.value = state.catalogId;
  $("#catalog-source").textContent = state.catalogInfo?.description || "";
}

const metres = (value) => `${Math.round(value * 100) / 100}`;

function renderCatalog() {
  const query = $("#catalog-search").value;
  const objects = [...catalog.items.values()].filter((item) => item.kind !== "terrain"
    && matches(query, item.name, item.category, item.id));
  if (!objects.length) {
    $("#catalog-list").replaceChildren(el("li", { class: "hint" }, "見つかりません"));
    return;
  }
  // By category, in the Catalog's order, each under its heading.
  const groups = new Map();
  for (const entry of objects) groups.set(entry.category || "その他", [...(groups.get(entry.category || "その他") || []), entry]);
  $("#catalog-list").replaceChildren(...[...groups].flatMap(([category, members]) => [
    el("li", { class: "catalog-heading" }, category), ...members.map((entry) => {
    const { envelope, height_m: height } = entry;
    const size = envelope.primitive === "cylinder"
      ? `φ${metres(envelope.width_m)}×H${metres(height)} m`
      : `${metres(envelope.width_m)}×${metres(envelope.depth_m)}×${metres(height)} m`;
    return el("li", {},
      el("button", {
        onclick: () => addObject(entry.id), draggable: "true",
        ondragstart: (event) => {
          event.dataTransfer.setData(ITEM_DRAG, entry.id);
          event.dataTransfer.setData("text/plain", entry.name);
          event.dataTransfer.effectAllowed = "copy";
        },
        title: `${entry.category}：${entry.description || ""}\n上面図にドラッグして置きます（クリックで中央に置きます）`,
      },
        el("span", {}, entry.name),
        el("span", { class: "meta" }, size),
        entry.thumbnail
          ? el("img", { class: "thumbnail", alt: "", loading: "lazy",
            src: `/api/catalogs/${encodeURIComponent(state.catalogId)}/items/${encodeURIComponent(entry.id)}/thumbnail` })
          : el("span", { class: "swatch", style: `background:${entry.params.color || "#cccccc"}` })));
  })]));
}

// Move a saved Recipe (and its visual assets, its imported map data) to the
// trash (work/trash/); examples cannot be deleted.
async function deleteRecipe(item) {
  if (!window.confirm(`環境「${item.name || item.id}」（${item.id}）を削除しますか？\n`
    + "見た目の GLB と取り込んだ地図データも一緒に work/trash/ へ移します（そこから戻せます）。")) return;
  try {
    const result = await api("DELETE", `recipes/${encodeURIComponent(item.id)}`, {});
    const wasOpen = state.current?.id === item.id || state.openedId === item.id;
    await loadRecipes();
    if (wasOpen) {
      state.saved = snapshot(); // nothing left to keep: do not ask about unsaved changes
      const next = state.recipes.find((entry) => !entry.error);
      if (next) await openRecipe(next.id); else newRecipe();
    }
    setStatus(`${item.id} を削除しました（${result.trash}）`, "ok");
  } catch (error) {
    setStatus(`${item.id} を削除できません: ${error.message}`, "error");
  }
}

// Whether a text matches what is typed in a search box (any of its words, in any case).
function matches(query, ...texts) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const haystack = texts.filter(Boolean).join(" ").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

function renderRecipeList() {
  const query = $("#recipe-search").value;
  const shown = state.recipes.filter((item) => matches(query, item.id, item.name));
  if (!shown.length) {
    $("#recipe-list").replaceChildren(el("li", { class: "hint" }, query.trim() ? "見つかりません" : "まだありません"));
    return;
  }
  $("#recipe-list").replaceChildren(...shown.map((item) => el("li", { class: "recipe-row" },
    el("button", {
      "aria-current": String(state.current?.id === item.id),
      onclick: () => {
        if (!confirmDiscard()) return;
        if (item.error) { setStatus(`${item.id} を開けません: ${item.error}`, "error"); return; }
        setStatus(`${item.id} を開いています…`);
        openRecipe(item.id).catch((error) => setStatus(`${item.id} を開けません: ${error.message}`, "error"));
      },
    }, el("span", {}, item.name && item.name !== item.id ? item.name : item.id),
    item.name && item.name !== item.id ? el("span", { class: "meta" }, item.id) : null,
    el("span", { class: "meta" }, item.error ? "エラー"
      : `${item.size_m.east}×${item.size_m.north} m・${item.objects} 部品${item.terrain === "hfield" ? "・丘" : ""}${
        item.editable ? "" : "・例"}`)),
    item.editable ? el("button", {
      class: "secondary icon delete", title: `${item.id} を削除（work/trash/ へ移す）`, "aria-label": `${item.id} を削除`,
      onclick: () => deleteRecipe(item),
    }, "×") : null)));
}

// --- Editing ----------------------------------------------------------------------

// A part of the Catalog added to the environment: where it was dropped on
// the plan (on the grid), or at the centre (a click).
function addObject(itemId, at = null) {
  const entry = catalog.item(itemId);
  if (!entry || !state.current) return;
  const id = uniqueId(entry.id_prefix || "object");
  const step = Number($("#grid").value) || 0;
  const onGrid = (value) => roundMm(step > 0 ? Math.round(value / step) * step : value);
  const [x, y] = at ? at.map(onGrid) : [0, 0];
  recipe().objects.push({ id, item: itemId, pose: { x_m: x, y_m: y, yaw_deg: 0 } });
  state.selection = [id];
  render();
}

function changeObject(id, change) {
  const obj = objectById(id);
  if (!obj) return;
  if ("x" in change) obj.pose.x_m = change.x;
  if ("y" in change) obj.pose.y_m = change.y;
  if ("yaw" in change) obj.pose.yaw_deg = normalizeYaw(change.yaw);
  render({ live: true }); // during a drag: the panels and the undo step follow at its end
}

// Poses [{id, x, y, yaw}] for several objects at once (a group drag or turn).
function applyPoses(poses) {
  for (const pose of poses) {
    const obj = objectById(pose.id);
    if (!obj) continue;
    if ("x" in pose) obj.pose.x_m = pose.x;
    if ("y" in pose) obj.pose.y_m = pose.y;
    if ("yaw" in pose) obj.pose.yaw_deg = normalizeYaw(pose.yaw);
  }
  render({ live: true });
}

function deleteSelected() {
  if (!state.selection.length) return;
  const gone = new Set(state.selection);
  recipe().objects = recipe().objects.filter((obj) => !gone.has(obj.id));
  state.selection = [];
  render();
}

// A City World layer (Envsim's road network) into road parts one can edit
// one by one: flat surfaces on the ground instead of Envsim's look (one undo step).
async function explodeLayer(obj) {
  if (!window.confirm(`「${obj.id}」を個別の道路部品に分解しますか？\n`
    + "envsim の見た目（地形に沿った道路）の代わりに、1 本ずつ編集できる平らな道路（面）を地面に置きます。")) return;
  try {
    const { objects } = await api("POST", "explode", { ...recipeBody(), object: obj.id });
    await catalog.prime(objects);
    const index = recipe().objects.findIndex((entry) => entry.id === obj.id);
    if (index < 0) return;
    recipe().objects.splice(index, 1, ...objects);
    state.selection = [];
    render();
    setStatus(`${obj.id} を ${objects.length} 個の道路部品にしました`, "ok");
  } catch (error) {
    setStatus(`${obj.id} を分解できません: ${error.message}`, "error");
  }
}

// A building of this environment registered in the user's Catalog (POST
// /api/catalogs/my/items): its look and colliders copied there in its own
// frame, so it can be placed in any environment that uses マイカタログ.
async function registerBuilding(obj) {
  const name = window.prompt("マイカタログに登録する名前", obj.source?.tags?.name || obj.id);
  if (!name) return;
  setStatus(`${obj.id} をマイカタログに登録しています…`);
  try {
    // Its picture from the 3D view, when the view has it (else the list shows its colour).
    let thumbnail;
    try { thumbnail = state.view3d?.snapshot(obj.id) || undefined; } catch { thumbnail = undefined; }
    const { item } = await api("POST", "catalogs/my/items",
      { ...recipeBody(), object: obj.id, item_name: name, recipe_id: state.current?.id || undefined, thumbnail });
    state.catalogs = await api("GET", "catalogs");
    if (state.catalogId === "my") {
      state.catalogId = null;
      await loadCatalog("my");
    }
    renderCatalogSelect();
    setStatus(state.catalogId === "my"
      ? `「${item.name}」をマイカタログに登録しました（部品一覧の「${item.category}」）`
      : `「${item.name}」をマイカタログに登録しました。置くには、環境のカタログを「マイカタログ」にしてください`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function duplicateSelected() {
  const chosen = selectedObjects();
  if (!chosen.length) return;
  let offset;
  if (chosen.length === 1) {
    // Next to the original along its width, so a row of walls or rails grows naturally.
    const width = resolved(chosen[0]).width;
    const yaw = (normalizeYaw(chosen[0].pose.yaw_deg ?? 0) * Math.PI) / 180;
    offset = [Math.cos(yaw) * width, Math.sin(yaw) * width];
  } else {
    // Several: the copies stand beside them, one group width along x.
    const xs = chosen.map(resolved).flatMap((view) => [view.x - view.width / 2, view.x + view.width / 2]);
    offset = [Math.max(...xs) - Math.min(...xs), 0];
  }
  const copies = chosen.map((obj) => {
    const copy = JSON.parse(JSON.stringify(obj));
    copy.id = uniqueId(catalog.item(obj.item)?.id_prefix || "object");
    copy.pose.x_m = roundMm(obj.pose.x_m + offset[0]);
    copy.pose.y_m = roundMm(obj.pose.y_m + offset[1]);
    recipe().objects.push(copy);
    return copy.id;
  });
  state.selection = copies;
  render();
}

// Turn the selection: one object in place, several about their centre.
function rotateSelected(degrees) {
  const chosen = selectedObjects();
  if (!chosen.length) return;
  const views = chosen.map(resolved);
  applyPoses(chosen.length === 1
    ? [{ id: views[0].id, yaw: views[0].yaw + degrees }]
    : turned(views, degrees, pivotOf(views)));
  render();
}

function moveSelected(dx, dy) {
  for (const obj of selectedObjects()) {
    obj.pose.x_m = roundMm(obj.pose.x_m + dx);
    obj.pose.y_m = roundMm(obj.pose.y_m + dy);
  }
  render();
}

// Slide the selection until one of its objects touches another or an edge.
function slideSelected([dx, dy]) {
  const chosen = selectedObjects();
  if (!chosen.length) return;
  const ids = new Set(chosen.map((obj) => obj.id));
  const others = recipe().objects.filter((item) => !ids.has(item.id)).map(resolved);
  // To the millimetre: a rounding up of under 0.5 mm stays within the checks' tolerance.
  const distance = Math.round(1000 * Math.min(...chosen.map((obj) => slideDistance(resolved(obj), [dx, dy], others, area())))) / 1000;
  moveSelected(dx * distance, dy * distance);
  const name = chosen.length === 1 ? chosen[0].id : `${chosen.length} 個の部品`;
  setStatus(distance ? `${name}を ${distance} m 寄せました` : `${name}はその方向にもう接しています`);
}

function nudgeSelected(dx, dy, far, fine) {
  const step = fine ? NUDGE_FINE_M : (Number($("#grid").value) || NUDGE_FINE_M) * (far ? NUDGE_FAR : 1);
  moveSelected(dx * step, dy * step);
}

// The 3D camera brought to the selected parts (the button, or F): only when
// asked, keeping the way it looks, so the view does not turn by itself.
function focusSelected() {
  if (!state.view3d || viewMode() === "plan") return;
  if (!state.selection.length) {
    setStatus("3D で見たい部品を選んでください", "error");
    return;
  }
  if (!state.view3d.focus(state.selection)) setStatus("選んだ部品が 3D にまだありません（読み込み中です）", "error");
}

function onKey(event) {
  if (!state.current || event.target.closest?.("input, select, textarea")) return;
  const meta = event.metaKey || event.ctrlKey;
  const key = event.key.toLowerCase();
  if (event.key === "Delete" || event.key === "Backspace") deleteSelected();
  else if (meta && key === "z") (event.shiftKey ? redo : undo)();
  else if (meta && key === "y") redo();
  else if (meta && key === "a") select(recipe().objects.map((obj) => obj.id));
  else if (meta && key === "d") duplicateSelected();
  else if (!meta && key === "r") rotateSelected(event.shiftKey ? -90 : 90);
  else if (!meta && key === "f") focusSelected();
  else if (event.key.startsWith("Arrow")) {
    const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[event.key];
    if (event.altKey && event.shiftKey) slideSelected(direction);
    else nudgeSelected(direction[0], direction[1], event.shiftKey, event.altKey);
  } else if (event.key === "Escape") {
    state.selection = [];
    render();
  } else return;
  event.preventDefault();
}

// --- Recipes ----------------------------------------------------------------------

function confirmDiscard() {
  return !isDirty() || window.confirm("保存していない変更があります。破棄して開きますか？");
}

async function loadRecipes() {
  state.recipes = await api("GET", "recipes");
  if (state.current) renderRecipeList();
}

async function openRecipe(id) {
  const loaded = await api("GET", `recipes/${id}`);
  loaded.recipe.objects = loaded.recipe.objects || [];
  await loadCatalog(loaded.catalog_id || state.catalogId);
  await catalog.prime(loaded.recipe.objects); // every shape in one request, before the first drawing
  state.current = { id, editable: loaded.editable, recipe: loaded.recipe };
  state.openedId = loaded.editable ? id : null; // the saved Recipe this edits (saving it again is no overwrite)
  state.selection = [];
  state.saved = snapshot();
  resetHistory();
  state.plan.fit();
  render();
  state.plan.fit();
  setStatus(loaded.editable ? "" : "例の環境です。保存するとコピーが作られます。");
}

function newRecipe() {
  if (!confirmDiscard()) return;
  state.current = {
    id: "", editable: true,
    recipe: { name: "", size_m: { ...DEFAULT_SIZE }, terrain: { item: terrainItems()[0]?.id }, objects: [] },
  };
  state.selection = [];
  state.saved = null;
  resetHistory();
  render();
  state.plan.fit();
  setStatus("");
  $("#recipe-id").focus();
}

async function saveRecipe() {
  const id = $("#recipe-id").value.trim();
  if (!id) { setStatus("ID を入力してください", "error"); return; }
  // Saving under another saved Recipe's ID replaces it: ask first.
  const other = state.recipes.find((item) => item.id === id && item.editable);
  if (other && id !== state.openedId && !window.confirm(`環境 ${id} はもうあります。上書きしますか？`)) return;
  recipe().name = $("#recipe-name").value.trim() || id;
  try {
    const saved = await api("PUT", `recipes/${id}`, recipeBody());
    // Saved under a new ID, the Recipe got its own copies of its assets (GLBs, colliders, terrain files).
    for (const [objectId, params] of Object.entries(saved.assets?.objects || {})) {
      const obj = objectById(objectId);
      if (obj) obj.params = { ...obj.params, ...params };
    }
    if (Object.keys(saved.assets?.terrain || {}).length) {
      recipe().terrain = { ...recipe().terrain, params: { ...recipe().terrain.params, ...saved.assets.terrain } };
    }
    state.current.id = id;
    state.current.editable = true;
    state.openedId = id;
    state.saved = snapshot();
    await loadRecipes();
    render();
    setStatus(`保存しました（${saved.path}${saved.copied_assets ? `・資産 ${saved.copied_assets} 件をコピー` : ""}）`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

// The saved Recipe as a City World job in the export folder the Studio was
// started with (POST /api/recipes/<id>/urban; tools/env_urban.py). The tool
// watching that folder (hakoniwa-urban-mobility) takes it as a World.
async function exportUrban() {
  // A saved Recipe as it is on disk (an example too): unsaved edits would not be in it.
  const id = state.current?.id;
  if (!id || isDirty() || !state.recipes.some((item) => item.id === id && !item.error)) {
    setStatus("書き出す前に、環境を保存してください", "error");
    return;
  }
  setStatus(`${id} を City World ジョブにしています…`);
  try {
    const result = await api("POST", `recipes/${encodeURIComponent(id)}/urban`, {});
    setStatus(`${id} を書き出しました（${result.kind === "city" ? "地図の原点あり" : "平らな World"}：${result.job}）`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function setSize(east, north) {
  if (!(east > 0 && north > 0)) return;
  recipe().size_m = { east: roundMm(east), north: roundMm(north) };
  render();
  state.plan.fit();
}

// The right panel (web/inspector.js) and what it may do.
const inspector = createInspector($("#inspector"), {
  render: (options) => render(options),
  recipe: () => recipe(),
  parts: catalog,
  resolved: (obj) => resolved(obj),
  selectedParts: () => selectedObjects(),
  select: (ids) => select(ids),
  rename: (obj, id) => renameObject(obj, id),
  duplicate: () => duplicateSelected(),
  remove: () => deleteSelected(),
  explode: (obj) => explodeLayer(obj),
  register: (obj) => registerBuilding(obj),
  slide: (direction) => slideSelected(direction),
  move: (dx, dy) => moveSelected(dx, dy),
  applyPoses: (poses) => applyPoses(poses),
});

// Where a Catalog part can be dropped: locate(event) gives [east, north] (or null).
function dropTarget(host, locate) {
  const carries = (event) => event.dataTransfer?.types.includes(ITEM_DRAG);
  host.addEventListener("dragover", (event) => {
    if (!carries(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    host.classList.add("drop-target");
  });
  host.addEventListener("dragleave", () => host.classList.remove("drop-target"));
  host.addEventListener("drop", (event) => {
    host.classList.remove("drop-target");
    if (!carries(event)) return;
    event.preventDefault();
    const at = locate(event);
    if (!at) {
      setStatus("環境の上に落としてください", "error");
      return;
    }
    addObject(event.dataTransfer.getData(ITEM_DRAG), at);
  });
}

async function init() {
  state.plan = new PlanView($("#plan"), {
    onSelect: select,
    onChange: changeObject,
    onChangeMany: applyPoses,
    onDragEnd: () => render(), // panels, and the finished move as one undo step
  });
  state.plan.setGrid(Number($("#grid").value));
  // A part dragged from the list is placed where it is dropped on the plan.
  dropTarget($("#plan"), (event) => state.plan.areaAt(event.clientX, event.clientY));
  state.catalogs = await api("GET", "catalogs");
  // マイカタログ when there is one (it includes the starter Catalog), else the first.
  await loadCatalog((state.catalogs.find((item) => item.id === "my" && !item.error) ?? state.catalogs.find((item) => !item.error))?.id);
  await loadRecipes();
  $("#catalog-select").addEventListener("change", (event) => switchCatalog(event.target.value));
  $("#recipe-search").addEventListener("input", renderRecipeList);
  $("#catalog-search").addEventListener("input", renderCatalog);
  $("#terrain-select").addEventListener("change", (event) => setTerrain(event.target.value));

  $("#new-recipe").addEventListener("click", newRecipe);
  $("#save").addEventListener("click", saveRecipe);
  $("#urban").addEventListener("click", exportUrban);
  // Export only when started with an export folder (a tool that takes the Worlds).
  try { $("#urban").hidden = !(await api("GET", "health")).export_dir; } catch { /* keep it hidden */ }
  warnIfServerIsOld();
  $("#fit").addEventListener("click", () => state.plan.fit());
  for (const button of document.querySelectorAll("#view-mode button")) {
    button.addEventListener("click", () => setViewMode(button.dataset.mode));
  }
  $("#view-overview").addEventListener("click", () => state.view3d?.overview());
  $("#view-car").addEventListener("click", () => state.view3d?.carView());
  $("#view-drone").addEventListener("click", () => state.view3d?.droneView());
  $("#view-focus").addEventListener("click", focusSelected);
  setViewMode(viewMode());
  $("#grid").addEventListener("change", (event) => state.plan.setGrid(Number(event.target.value)));
  $("#size-preset").addEventListener("change", (event) => {
    if (!event.target.value) return;
    const [east, north] = event.target.value.split("x").map(Number);
    setSize(east, north);
  });
  $("#size-east").addEventListener("change", (event) => setSize(Number(event.target.value), recipe().size_m.north));
  $("#size-north").addEventListener("change", (event) => setSize(recipe().size_m.east, Number(event.target.value)));
  $("#recipe-name").addEventListener("change", (event) => { recipe().name = event.target.value.trim(); });
  // The ID being typed is the Recipe's ID (render writes it back into the field).
  $("#recipe-id").addEventListener("input", (event) => { state.current.id = event.target.value.trim(); });
  document.addEventListener("keydown", onKey);
  document.addEventListener("copy", (event) => onCopy(event, false));
  document.addEventListener("cut", (event) => onCopy(event, true));
  document.addEventListener("paste", onPaste);
  window.addEventListener("beforeunload", (event) => { if (isDirty()) event.preventDefault(); });

  // ?recipe=<id> opens that Recipe (the map import page links here).
  const wanted = new URLSearchParams(window.location.search).get("recipe");
  const first = state.recipes.find((item) => !item.error && item.id === wanted)
    || state.recipes.find((item) => !item.error);
  if (first) await openRecipe(first.id);
  else newRecipe();
}

init().catch((error) => setStatus(`読み込みに失敗しました: ${error.message}`, "error"));
