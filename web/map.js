// Map import (#10): pick an area on a Leaflet map and have the Studio server
// make a Recipe of its buildings and roads (POST /api/map/import). Leaflet
// only shows the map and the area; the data comes from Overpass (through the
// server) or a GeoJSON file; the server turns it into CityGML (hakoniwa-envsim
// osm2citygml.py) and that into parts (tools/env_citygml.py).

import { $, api, el } from "./dom.js";

const DEFAULT_CENTER = [35.6809, 139.7667]; // Tokyo Station
const DEFAULT_ZOOM = 17;
const STORE_KEY = "hakoniwa-environment-map-selection";
// Metres per degree of latitude, as Envsim (plateau_citygml.bounding_box) and
// the PLATEAU City World browser convert a selection to degrees.
const METRES_PER_DEGREE = 111320;
const HALF_EXTENT = { min: 10, max: 1000 }; // metres, as in the PLATEAU browser

function setStatus(message, kind = "") {
  $("#status").textContent = message;
  $("#status").className = `status ${kind}`;
}

const numeric = (id) => Number($(`#${id}`).value);

function loadSelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY));
    if (saved && Number.isFinite(saved.latitude)) return saved;
  } catch { /* private window or nothing saved */ }
  return { latitude: DEFAULT_CENTER[0], longitude: DEFAULT_CENTER[1], northSouth: 150, eastWest: 150 };
}

function saveSelection() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      latitude: numeric("latitude"), longitude: numeric("longitude"),
      northSouth: numeric("northSouth"), eastWest: numeric("eastWest"),
    }));
  } catch { /* storage unavailable */ }
}

// The selection as the PLATEAU City World browser sends it.
function selection() {
  return {
    center: { latitude: numeric("latitude"), longitude: numeric("longitude") },
    half_extent_m: { north_south: numeric("northSouth"), east_west: numeric("eastWest") },
  };
}

function selectionIsValid() {
  return Number.isFinite(numeric("latitude")) && Number.isFinite(numeric("longitude"))
    && numeric("latitude") >= -90 && numeric("latitude") <= 90
    && numeric("longitude") >= -180 && numeric("longitude") <= 180
    && ["northSouth", "eastWest"].every((id) => numeric(id) >= HALF_EXTENT.min && numeric(id) <= HALF_EXTENT.max);
}

function boundsOf(latitude, longitude, northSouth, eastWest) {
  const latDelta = northSouth / METRES_PER_DEGREE;
  const lonDelta = eastWest / (METRES_PER_DEGREE * Math.cos((latitude * Math.PI) / 180));
  return L.latLngBounds([latitude - latDelta, longitude - lonDelta], [latitude + latDelta, longitude + lonDelta]);
}

function selectionBounds() {
  return boundsOf(numeric("latitude"), numeric("longitude"), numeric("northSouth"), numeric("eastWest"));
}

function setInputsFromBounds(bounds) {
  const center = bounds.getCenter();
  const northSouth = (bounds.getNorth() - bounds.getSouth()) * METRES_PER_DEGREE / 2;
  const eastWest = (bounds.getEast() - bounds.getWest()) * METRES_PER_DEGREE * Math.cos((center.lat * Math.PI) / 180) / 2;
  $("#latitude").value = center.lat.toFixed(6);
  $("#longitude").value = center.lng.toFixed(6);
  $("#northSouth").value = northSouth.toFixed(1);
  $("#eastWest").value = eastWest.toFixed(1);
}

// The selection on the map: a draggable centre marker, a rectangle that moves
// when dragged and corner handles that resize it (the opposite corner stays),
// a click on the map to recentre -- the PLATEAU City World browser's controls.
function selectionControls(map, onChange) {
  const marker = L.marker(map.getCenter(), { draggable: true }).addTo(map)
    .bindTooltip("中心", { direction: "top" });
  const rectangle = L.rectangle([[0, 0], [0, 0]], {
    color: "#1367a8", weight: 2, fillColor: "#2d8dca", fillOpacity: 0.18, className: "selection-rectangle",
  }).addTo(map).bindTooltip("ドラッグで移動／四隅のハンドルで大きさを変更");
  const handles = ["nw", "ne", "se", "sw"].map((direction) => L.marker([0, 0], {
    draggable: true, keyboard: false, zIndexOffset: 1000,
    icon: L.divIcon({
      className: `selection-resize-handle selection-resize-handle-${direction}`,
      html: '<span aria-hidden="true"></span>', iconSize: [28, 28], iconAnchor: [14, 14],
    }),
  }).addTo(map));
  const opposite = [2, 3, 0, 1];
  const corners = (bounds) => [bounds.getNorthWest(), bounds.getNorthEast(), bounds.getSouthEast(), bounds.getSouthWest()];
  let suppressClickUntil = 0;
  let interaction = null;
  // Leaflet may send a click right after a drag; ignore it for a moment.
  const suppressClick = () => { suppressClickUntil = Date.now() + 250; };

  function refresh({ skipHandle = -1 } = {}) {
    if (!selectionIsValid()) {
      $("#selection-summary").textContent = "範囲の値を確認してください（half extent は 10〜1000 m）。";
      onChange(false);
      return;
    }
    const center = [numeric("latitude"), numeric("longitude")];
    marker.setLatLng(center);
    const bounds = selectionBounds();
    rectangle.setBounds(bounds);
    corners(bounds).forEach((corner, index) => { if (index !== skipHandle) handles[index].setLatLng(corner); });
    $("#selection-summary").textContent = `${(numeric("eastWest") * 2).toFixed(0)}m × ${(numeric("northSouth") * 2).toFixed(0)}m`
      + ` / center=${center[0].toFixed(6)}, ${center[1].toFixed(6)}`;
    saveSelection();
    onChange(true);
  }

  function setCenter(latlng) {
    $("#latitude").value = latlng.lat.toFixed(6);
    $("#longitude").value = latlng.lng.toFixed(6);
    refresh();
  }

  map.on("click", (event) => { if (Date.now() >= suppressClickUntil) setCenter(event.latlng); });
  marker.on("drag", (event) => setCenter(event.target.getLatLng()));
  for (const id of ["latitude", "longitude", "northSouth", "eastWest"]) {
    $(`#${id}`).addEventListener("input", () => refresh());
    $(`#${id}`).addEventListener("change", () => {
      if (selectionIsValid()) map.fitBounds(selectionBounds().pad(0.35), { maxZoom: 19 });
    });
  }
  handles.forEach((handle, index) => {
    let fixed = null;
    handle.on("mousedown", (event) => {
      // The handle sits over the movable rectangle: claim the gesture first.
      interaction = "resize";
      L.DomEvent.stopPropagation(event.originalEvent);
      suppressClick();
    });
    handle.on("dragstart", () => {
      interaction = "resize";
      fixed = corners(rectangle.getBounds())[opposite[index]];
      suppressClick();
    });
    handle.on("drag", (event) => {
      setInputsFromBounds(L.latLngBounds(fixed, event.target.getLatLng()));
      refresh({ skipHandle: index });
    });
    handle.on("dragend", () => {
      suppressClick();
      fixed = null;
      interaction = null;
      refresh();
    });
  });
  rectangle.on("mousedown", (event) => {
    const target = event.originalEvent?.target;
    if (interaction === "resize" || (target instanceof Element && target.closest(".selection-resize-handle"))) return;
    interaction = "move";
    L.DomEvent.stopPropagation(event.originalEvent);
    suppressClick();
    const start = event.latlng;
    const original = rectangle.getBounds();
    map.dragging.disable();
    const move = (moveEvent) => {
      const dLat = moveEvent.latlng.lat - start.lat;
      const dLon = moveEvent.latlng.lng - start.lng;
      setInputsFromBounds(L.latLngBounds([original.getSouth() + dLat, original.getWest() + dLon],
        [original.getNorth() + dLat, original.getEast() + dLon]));
      refresh();
    };
    const finish = () => {
      suppressClick();
      map.off("mousemove", move);
      map.off("mouseup", finish);
      document.removeEventListener("mouseup", finish);
      map.dragging.enable();
      interaction = null;
    };
    map.on("mousemove", move);
    map.on("mouseup", finish);
    document.addEventListener("mouseup", finish, { once: true });
  });
  refresh();
  return { refresh };
}

const ROOT_KEY = "hakoniwa-environment-world-root";

// The City Worlds (Envsim builds) in a workspace: listed, outlined on the map,
// and imported as parts (POST /api/city-worlds/import).
function worldBounds(build) {
  return boundsOf(build.center.latitude, build.center.longitude, build.half_extent_m.north_south,
    build.half_extent_m.east_west);
}

async function searchWorlds(map, layer) {
  const root = $("#world-root").value.trim();
  try { localStorage.setItem(ROOT_KEY, root); } catch { /* storage unavailable */ }
  setStatus("探しています…");
  let found;
  try {
    found = await api("GET", `city-worlds${root ? `?root=${encodeURIComponent(root)}` : ""}`);
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  if (!root) $("#world-root").value = found.roots.join(", ");
  layer.clearLayers();
  $("#world-list").replaceChildren(...found.builds.map((build) => {
    const bounds = worldBounds(build);
    L.rectangle(bounds, { color: "#e07b00", weight: 2, fillOpacity: 0.05 }).bindTooltip(build.title).addTo(layer);
    const importButton = el("button", { class: "secondary", onclick: () => importWorld(build) }, "取り込む");
    const exportButton = !found.export_dir ? null : build.exported
      ? el("button", { class: "secondary", title: `書き出し先の ${build.exported.id}（City World 自体は残ります）`,
        onclick: () => removeExport(build, () => searchWorlds(map, layer)) }, "書き出しを消す")
      : el("button", { class: "secondary", disabled: !build.exportable,
        title: build.exportable ? "作った City World をそのまま書き出し先に書き出します" : "City World がないビルドです",
        onclick: () => exportWorld(build, () => searchWorlds(map, layer)) }, "書き出す");
    return el("li", {},
      el("button", { onclick: () => map.fitBounds(bounds, { padding: [30, 30] }) },
        el("span", {}, build.title),
        el("span", { class: "meta" }, `建物 ${build.buildings}・${build.feature_types.join(" / ")}${build.world ? "・City World あり" : ""}${build.dem ? "・地形（DEM）あり" : ""}${build.exported ? `・書き出し済み（${build.exported.id}）` : ""}`)),
      el("span", { class: "row" }, importButton, ...(exportButton ? [exportButton] : [])));
  }));
  if (!found.builds.length) $("#world-list").replaceChildren(el("li", { class: "hint" }, "Envsim のビルドは見つかりませんでした"));
  setStatus(`${found.builds.length} 件見つかりました`, found.builds.length ? "ok" : "");
}

// A City World written to the export folder as it is (no Recipe in between):
// POST /api/city-worlds/export. The tool watching that folder
// (hakoniwa-urban-mobility) takes it as a World named after the job folder.
async function exportWorld(build, then = null) {
  setStatus(`${build.title} を書き出しています…`);
  try {
    const result = await api("POST", "city-worlds/export", { path: build.path, title: build.title });
    setStatus(`${build.title} を ${result.id} として書き出しました（${result.job}）`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
  if (then) await then();
}

async function removeExport(build, then = null) {
  if (!window.confirm(`書き出し先の ${build.exported.id} を消しますか？（City World 自体は残ります）`)) return;
  try {
    await api("POST", `exports/${encodeURIComponent(build.exported.id)}/delete`, {});
    setStatus(`書き出し先の ${build.exported.id} を消しました`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
  if (then) await then();
}

async function importWorld(build, given = null) {
  const id = given || window.prompt("作る環境の ID（小文字・数字・- _）", build.id);
  if (!id) return;
  setStatus(`${build.title} を部品にしています…`);
  try {
    const result = await api("POST", "city-worlds/import", { id, path: build.path, name: build.title });
    const items = [
      `大きさ ${result.size_m.east} m × ${result.size_m.north} m（${result.provider}、地面：${result.terrain === "dem" ? "City World の地形（DEM）" : "平ら"}）`,
      `建物 ${result.buildings}（LOD2 の見た目付き ${result.lod2_visuals ?? 0}）、道路 ${result.roads}`,
      ...(result.passthrough ? [`envsim の原本をそのまま使用：当たり判定 ${result.passthrough.colliders} 棟、`
        + `${result.passthrough.terrain ? "地形、" : ""}層 ${result.passthrough.layers.join("・") || "なし"}`] : []),
      ...(result.courtyards ? [`中庭（穴） ${result.courtyards}`] : []),
      ...(result.courtyards_filled ? [`小さい・形の崩れた中庭を埋めた数 ${result.courtyards_filled}`] : []),
      ...(result.clipped ? [`元データで重なっていた外形を切り取った建物 ${result.clipped}`] : []),
      ...(result.overlaps_left?.length ? [`切り取れずに残った重なり ${result.overlaps_left.length} 組（検証で確認してください）`] : []),
      ...result.notes.filter((note) => !note.includes("were clipped")),
    ];
    $("#report").replaceChildren(el("ul", {}, ...items.map((item) => el("li", {}, item))));
    $("#open").href = `./?recipe=${encodeURIComponent(result.id)}`;
    $("#result").hidden = false;
    setStatus(`${result.id} を作りました（${result.path}）`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

// A City World built from PLATEAU by hakoniwa-envsim (POST /api/city-worlds/build),
// followed until it is done, then imported under the same id.
const BUILD_KEY = "hakoniwa-environment-city-world-build";
const PHASES = { source_download: "ダウンロード", catalog: "カタログの問い合わせ", collider_visualization: "当たり判定の表示用 GLB" };

function describeBuild(status) {
  const progress = status.progress || {};
  const phase = PHASES[progress.phase] || progress.phase || "準備";
  const count = progress.total ? ` ${progress.current}/${progress.total}` : "";
  const feature = progress.feature ? `（${progress.feature}）` : "";
  const elapsed = status.elapsed_s ? `・${Math.round(status.elapsed_s)} 秒` : "";
  return `${status.id}：${phase}${feature}${count}${elapsed}`;
}

async function followBuild(id, onDone) {
  $("#build-cancel").hidden = false;
  $("#build-start").disabled = true;
  try { localStorage.setItem(BUILD_KEY, id); } catch { /* storage unavailable */ }
  for (;;) {
    let status;
    try {
      status = await api("GET", `city-worlds/build/${encodeURIComponent(id)}`);
    } catch (error) {
      $("#build-status").textContent = error.message;
      break;
    }
    $("#build-status").textContent = describeBuild(status);
    if (status.state !== "running") {
      if (status.state === "done") {
        $("#build-status").textContent = `${id} を作りました。続けて登録・取り込みをします…`;
        await onDone(status);
        $("#build-status").textContent = `${id} を作りました（City World：${status.build}）`;
      } else {
        $("#build-status").replaceChildren(el("div", {}, `${id} を作れませんでした（ログ：${status.log}）`),
          ...(status.errors.length ? status.errors : status.log_tail.slice(-5)).map((line) => el("div", {}, line)));
      }
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  try { localStorage.removeItem(BUILD_KEY); } catch { /* storage unavailable */ }
  $("#build-cancel").hidden = true;
  $("#build-start").disabled = false;
}

function report(result) {
  const assumed = result.assumed;
  const items = [
    `大きさ ${result.size_m.east} m × ${result.size_m.north} m`,
    `建物 ${result.buildings}（高さを既定値で補った ${assumed.building_height}）`,
    `道路 ${result.roads}（幅を既定値で補った ${assumed.road_width}、車線数 ${assumed.road_lanes}）`,
  ];
  if (result.skipped.length) items.push(`取り込まなかった ${result.skipped.length}（範囲外・小さすぎる・形が不完全）`);
  for (const note of result.notes) items.push(note);
  return el("ul", {}, ...items.map((item) => el("li", {}, item)));
}

async function init() {
  const config = await api("GET", "map/config");
  // Started with an export folder: offer writing City Worlds there.
  $("#build-register-field").hidden = !config.export_dir;
  const saved = loadSelection();
  $("#latitude").value = saved.latitude.toFixed(6);
  $("#longitude").value = saved.longitude.toFixed(6);
  $("#northSouth").value = saved.northSouth;
  $("#eastWest").value = saved.eastWest;
  const map = L.map("map", { maxZoom: 20 }).setView([saved.latitude, saved.longitude], DEFAULT_ZOOM);
  L.tileLayer(config.tiles.url, { maxZoom: 20, maxNativeZoom: 19, attribution: config.tiles.attribution }).addTo(map);
  L.control.scale().addTo(map);
  let importing = false;
  const controls = selectionControls(map, (valid) => { $("#import").disabled = importing || !valid; });
  map.fitBounds(selectionBounds().pad(0.35), { maxZoom: 19 });
  const update = () => controls.refresh();

  $("#source").addEventListener("change", () => { $("#geojson-field").hidden = $("#source").value !== "geojson"; });

  const worlds = L.layerGroup().addTo(map);
  try { $("#world-root").value = localStorage.getItem(ROOT_KEY) || ""; } catch { /* storage unavailable */ }
  $("#world-search").addEventListener("click", () => searchWorlds(map, worlds));
  searchWorlds(map, worlds);

  const imported = async (status) => {
    const build = { path: status.build, title: $("#recipe-name").value.trim() || status.id };
    if (config.export_dir && $("#build-register").checked) await exportWorld(build);
    if ($("#build-import").checked) await importWorld(build, status.id);
    searchWorlds(map, worlds);
  };
  $("#build-start").addEventListener("click", async () => {
    const id = $("#build-id").value.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
      setStatus("ID は小文字・数字・- _ で付けてください", "error");
      return;
    }
    if (!selectionIsValid()) {
      setStatus("範囲の値を確認してください", "error");
      return;
    }
    try {
      await api("POST", "city-worlds/build", {
        id, name: $("#recipe-name").value.trim() || id, selection: selection(),
        offline: $("#build-offline").checked, overwrite: $("#build-offline").checked, root: $("#world-root").value.trim().split(",")[0].trim() || undefined,
      });
    } catch (error) {
      setStatus(error.message, "error");
      return;
    }
    setStatus(`${id} を作り始めました（hakoniwa-envsim）`, "ok");
    followBuild(id, imported);
  });
  $("#build-cancel").addEventListener("click", async () => {
    let id = null;
    try { id = localStorage.getItem(BUILD_KEY); } catch { /* storage unavailable */ }
    if (!id || !window.confirm(`${id} を作るのを中止しますか？`)) return;
    try { await api("POST", `city-worlds/build/${encodeURIComponent(id)}/cancel`, {}); } catch (error) { setStatus(error.message, "error"); }
  });
  try {  // a build started before this page was opened again
    const running = localStorage.getItem(BUILD_KEY);
    if (running) followBuild(running, imported);
  } catch { /* storage unavailable */ }

  // Only grounds that need no data of their own (map data brings no terrain).
  $("#terrain").replaceChildren(...config.terrains
    .map((item) => el("option", { value: item.id, selected: item.id === "city-ground" }, item.name)));

  $("#import").addEventListener("click", async () => {
    const id = $("#recipe-id").value.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
      setStatus("ID は小文字・数字・- _ で付けてください", "error");
      return;
    }
    if (!selectionIsValid()) {
      setStatus("範囲の値を確認してください", "error");
      return;
    }
    const body = { id, name: $("#recipe-name").value.trim(), selection: selection(), terrain: $("#terrain").value,
      source: $("#source").value };
    if (body.source === "geojson") {
      const file = $("#geojson").files[0];
      if (!file) { setStatus("GeoJSON ファイルを選んでください", "error"); return; }
      try { body.geojson = JSON.parse(await file.text()); } catch (error) {
        setStatus(`GeoJSON を読めません: ${error.message}`, "error");
        return;
      }
    }
    importing = true;
    $("#import").disabled = true;
    setStatus(body.source === "overpass" ? "OpenStreetMap から取得して作っています…" : "作っています…");
    try {
      const result = await api("POST", "map/import", body);
      $("#report").replaceChildren(report(result));
      $("#open").href = `./?recipe=${encodeURIComponent(result.id)}`;
      $("#result").hidden = false;
      setStatus(`${result.id} を作りました（${result.path}）`, "ok");
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      importing = false;
      update();
    }
  });
}

init().catch((error) => setStatus(`読み込みに失敗しました: ${error.message}`, "error"));
