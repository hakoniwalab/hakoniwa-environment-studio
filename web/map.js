// The map page (#10): pick an area on a Leaflet map and make a City World of
// it with hakoniwa-envsim, from PLATEAU or from OpenStreetMap (cityworlds.js:
// generate, look at it in 3D, write it to the export folder). Taking a City
// World in as parts goes on in Studio.

import { $, api, el, warnIfServerIsOld } from "./dom.js";
import { cityWorlds } from "./cityworlds.js";

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

// A City World taken in as parts (POST /api/city-worlds/import), then opened in Studio to edit.
async function importWorld(build, given = null) {
  const id = given || window.prompt("作る環境の ID（小文字・数字・- _）", build.id ?? build.title);
  if (!id) return;
  setStatus(`${build.title} を部品にしています…`);
  try {
    const result = await api("POST", "city-worlds/import", { id, path: build.path, name: build.title });
    window.location.assign(`./?recipe=${encodeURIComponent(result.id)}`);
  } catch (error) {
    setStatus(error.message, "error");
  }
}


// PLATEAU or OpenStreetMap: the same selection, two ways to make the area.
// PLATEAU covers the cities it has modelled (LOD2 buildings, DEM terrain, road
// surfaces); OpenStreetMap covers anywhere, with buildings as extruded outlines.
const MODE_KEY = "hakoniwa-environment-map-mode";
const MODE_HINTS = {
  plateau: "PLATEAU：整備された都市だけですが、LOD2 の建物（テクスチャ付き）・建物ごとの当たり判定・地形（DEM）・道路面まであります。まず「2. データを診断」でデータがあるか確かめてください。",
  osm: "OpenStreetMap：世界中どこでも使えます。建物は外形を押し出した箱（高さは推定を含む）、地面は平らです。できた City World は PLATEAU と同じく「生成結果」で 3D で確かめられます。",
};
const TAB_KEY = "hakoniwa-environment-map-tab";

function setTab(tab) {
  for (const button of document.querySelectorAll("#side-tabs button")) {
    button.setAttribute("aria-pressed", String(button.dataset.tab === tab));
  }
  $("#tab-make").hidden = tab !== "make";
  $("#tab-made").hidden = tab !== "made";
  try { localStorage.setItem(TAB_KEY, tab); } catch { /* storage unavailable */ }
}

function setMode(mode) {
  for (const button of document.querySelectorAll("#source-mode button")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
  $("#mode-plateau").hidden = mode !== "plateau";
  $("#mode-osm").hidden = mode !== "osm";
  $("#mode-hint").textContent = MODE_HINTS[mode];
  try { localStorage.setItem(MODE_KEY, mode); } catch { /* storage unavailable */ }
}

async function init() {
  const config = await api("GET", "map/config");
  warnIfServerIsOld();
  const saved = loadSelection();
  $("#latitude").value = saved.latitude.toFixed(6);
  $("#longitude").value = saved.longitude.toFixed(6);
  $("#northSouth").value = saved.northSouth;
  $("#eastWest").value = saved.eastWest;
  const map = L.map("map", { maxZoom: 20 }).setView([saved.latitude, saved.longitude], DEFAULT_ZOOM);
  L.tileLayer(config.tiles.url, { maxZoom: 20, maxNativeZoom: 19, attribution: config.tiles.attribution }).addTo(map);
  L.control.scale().addTo(map);
  let worlds = null;
  // OpenStreetMap: a default id from the centre (as PLATEAU's, without a
  // municipality), following the selection until it is typed over.
  let osmIdTyped = false;
  const defaultOsmId = () => `osm-lat${numeric("latitude").toFixed(3)}-lon${numeric("longitude").toFixed(3)}`
    .replaceAll(".", "_");
  $("#recipe-id").addEventListener("input", () => {
    osmIdTyped = $("#recipe-id").value.trim() !== "";
  });
  const controls = selectionControls(map, (valid) => {
    if (valid && !osmIdTyped) $("#recipe-id").value = defaultOsmId();
    worlds?.selectionChanged(valid);
  });
  map.fitBounds(selectionBounds().pad(0.35), { maxZoom: 19 });

  // City Worlds: the City World Web UI (cityworlds.js) over the same selection.
  worlds = cityWorlds({
    map, exportDir: config.export_dir, selection, selectionIsValid, selectionBounds, setStatus, importWorld,
    applySelection(given) {
      $("#latitude").value = Number(given.center.latitude).toFixed(6);
      $("#longitude").value = Number(given.center.longitude).toFixed(6);
      $("#northSouth").value = Number(given.half_extent_m.north_south).toFixed(1);
      $("#eastWest").value = Number(given.half_extent_m.east_west).toFixed(1);
      controls.refresh();
    },
    toOsm: () => setMode("osm"),
    showMade: () => setTab("made"),
  });
  worlds.selectionChanged(selectionIsValid());

  $("#source").addEventListener("change", () => { $("#geojson-field").hidden = $("#source").value !== "geojson"; });

  let mode = "plateau";
  try { mode = localStorage.getItem(MODE_KEY) === "osm" ? "osm" : "plateau"; } catch { /* storage unavailable */ }
  setMode(mode);
  for (const button of document.querySelectorAll("#source-mode button")) {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  }
  // The same selection in the other source: from OSM to a PLATEAU diagnosis.
  $("#to-plateau").addEventListener("click", () => { setMode("plateau"); worlds.inspect(); });

  // 作る / できたもの: making a City World, and what was made.
  for (const button of document.querySelectorAll("#side-tabs button")) {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  }
  let tab = "make";
  try { tab = localStorage.getItem(TAB_KEY) === "made" ? "made" : "make"; } catch { /* storage unavailable */ }
  setTab(tab);

  // OpenStreetMap: a City World of the selection's map data, generated like PLATEAU's.
  $("#osm-generate").addEventListener("click", async () => {
    const id = $("#recipe-id").value.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
      setStatus("ID は小文字・数字・- _ で付けてください", "error");
      return;
    }
    const span = (half) => Math.round(half * 2);
    const size = numeric("eastWest") === numeric("northSouth") ? `${span(numeric("eastWest"))} m 四方`
      : `${span(numeric("eastWest"))} × ${span(numeric("northSouth"))} m`;
    const body = { selection: selection(), source: "osm", map_data: $("#source").value,
      name: `OpenStreetMap（${numeric("latitude").toFixed(3)}, ${numeric("longitude").toFixed(3)}）付近（${size}）` };
    if (body.map_data === "geojson") {
      const file = $("#geojson").files[0];
      if (!file) { setStatus("GeoJSON ファイルを選んでください", "error"); return; }
      try { body.geojson = JSON.parse(await file.text()); } catch (error) {
        setStatus(`GeoJSON を読めません: ${error.message}`, "error");
        return;
      }
    }
    await worlds.generate(id, body, "osm");
  });
}

init().catch((error) => setStatus(`読み込みに失敗しました: ${error.message}`, "error"));
