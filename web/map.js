// Map import (#10): pick an area on a Leaflet map and have the Studio server
// make a Recipe of its buildings and roads (POST /api/map/import). Leaflet
// only shows the map and the area; the data comes from Overpass (through the
// server) or a GeoJSON file; the server turns it into CityGML (hakoniwa-envsim
// osm2citygml.py) and that into parts (tools/env_citygml.py).

import { $, api, el } from "./dom.js";

const DEFAULT_CENTER = [35.6809, 139.7667]; // Tokyo Station
const DEFAULT_ZOOM = 17;
const STORE_KEY = "hakoniwa-environment-map-view";
// WGS84: metres per degree at a latitude (the area's size; Envsim's geodesy.py
// does the exact conversion).
const A = 6378137.0;
const E2 = 6.69437999014e-3;

function metresPerDegree(lat) {
  const phi = (lat * Math.PI) / 180;
  const w = Math.sqrt(1 - E2 * Math.sin(phi) ** 2);
  return { north: (Math.PI / 180) * A * (1 - E2) / w ** 3, east: (Math.PI / 180) * A / w * Math.cos(phi) };
}

function setStatus(message, kind = "") {
  $("#status").textContent = message;
  $("#status").className = `status ${kind}`;
}

function loadView() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY));
    if (saved && Array.isArray(saved.center)) return saved;
  } catch { /* private window or nothing saved */ }
  return { center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM };
}

function saveView(map) {
  try {
    const center = map.getCenter();
    localStorage.setItem(STORE_KEY, JSON.stringify({ center: [center.lat, center.lng], zoom: map.getZoom() }));
  } catch { /* storage unavailable */ }
}

// The area: size_m about the map's centre, as degrees.
function bbox(map) {
  const center = map.getCenter();
  const east = Number($("#size-east").value);
  const north = Number($("#size-north").value);
  const scale = metresPerDegree(center.lat);
  const halfLat = north / 2 / scale.north;
  const halfLon = east / 2 / scale.east;
  const round = (value) => Math.round(value * 1e7) / 1e7;
  return {
    south: round(center.lat - halfLat), west: round(center.lng - halfLon),
    north: round(center.lat + halfLat), east: round(center.lng + halfLon),
  };
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
  const view = loadView();
  const map = L.map("map").setView(view.center, view.zoom);
  L.tileLayer(config.tiles.url, { maxZoom: 19, attribution: config.tiles.attribution }).addTo(map);
  const area = L.rectangle([[0, 0], [0, 0]], { color: "#1f6feb", weight: 2, fillOpacity: 0.08, interactive: false }).addTo(map);
  const centre = L.circleMarker(view.center, { radius: 4, color: "#1f6feb", interactive: false }).addTo(map);

  const update = () => {
    const box = bbox(map);
    area.setBounds([[box.south, box.west], [box.north, box.east]]);
    centre.setLatLng(map.getCenter());
    const c = map.getCenter();
    $("#bbox").textContent = `中心 ${c.lat.toFixed(6)}, ${c.lng.toFixed(6)}／範囲 ${box.south}, ${box.west} 〜 ${box.north}, ${box.east}`;
    const east = Number($("#size-east").value);
    const north = Number($("#size-north").value);
    $("#import").disabled = !(east > 0 && north > 0 && Math.max(east, north) <= config.max_side_m);
    if (Math.max(east, north) > config.max_side_m) setStatus(`一辺は ${config.max_side_m} m までです`, "error");
  };
  map.on("move", update);
  map.on("moveend", () => saveView(map));
  for (const id of ["#size-east", "#size-north"]) $(id).addEventListener("input", update);
  update();

  $("#go").addEventListener("click", () => {
    const [lat, lon] = $("#center").value.split(/[,\s]+/).filter(Boolean).map(Number);
    if (Number.isFinite(lat) && Number.isFinite(lon)) map.setView([lat, lon], Math.max(map.getZoom(), 16));
    else setStatus("中心は「緯度, 経度」で入力してください", "error");
  });
  $("#source").addEventListener("change", () => { $("#geojson-field").hidden = $("#source").value !== "geojson"; });

  const catalog = await api("GET", "catalogs/starter");
  $("#terrain").replaceChildren(...catalog.items.filter((item) => item.kind === "terrain")
    .map((item) => el("option", { value: item.id, selected: item.id === "city-ground" }, item.name)));

  $("#import").addEventListener("click", async () => {
    const id = $("#recipe-id").value.trim();
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
      setStatus("ID は小文字・数字・- _ で付けてください", "error");
      return;
    }
    const body = { id, name: $("#recipe-name").value.trim(), bbox: bbox(map), terrain: $("#terrain").value,
      source: $("#source").value };
    if (body.source === "geojson") {
      const file = $("#geojson").files[0];
      if (!file) { setStatus("GeoJSON ファイルを選んでください", "error"); return; }
      try { body.geojson = JSON.parse(await file.text()); } catch (error) {
        setStatus(`GeoJSON を読めません: ${error.message}`, "error");
        return;
      }
    }
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
      update();
    }
  });
}

init().catch((error) => setStatus(`読み込みに失敗しました: ${error.message}`, "error"));
