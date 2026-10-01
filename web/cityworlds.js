// City Worlds on the map page: the Business Pack City World Web UI
// (tools/remote_operation/city_world/web/city-world-ui.js) on the Studio's
// server instead of its Worker. PLATEAU: diagnose the selection (POST
// /api/plateau/inspect) and generate a City World with hakoniwa-envsim (POST
// /api/city-worlds/build, followed by GET /api/city-worlds/build/<id>).
// OpenStreetMap generates one the same way, from the selection's map data
// (flat ground). Both are looked at in one result list (GET
// /api/city-worlds/jobs): the area on the map, the colliders, a 3D viewer of
// the Visual and Collider GLBs, the ZIP, delete. The Studio adds writing a
// result to the export folder and taking it in as parts to edit (in Studio).

import { $, api, el } from "./dom.js";

const capabilityLabels = {
  building: "建物", terrain: "地形（DEM）", road: "道路",
  road_markings: "路面標示", bridge: "橋",
};
// The catalog's reasons (the Business Pack inspection's words) in the page's.
const capabilityReasons = {
  "dataset is not available in the selected bbox": "この範囲にはデータがありません",
  "LOD3 geometry required by the current generator is not available": "LOD3 が無いので使いません（LOD3 だけを使います）",
};
const progressPhaseLabels = {
  source_download: "データの取得",
  geometry_extract: "建物の形",
  geometry_extract_files: "建物の形",
  building_collision: "建物の当たり判定",
  terrain: "地形",
  terrain_extract: "地形（DEM）",
  terrain_gap_fill: "地形の欠けの補間",
  building_mjcf: "建物の当たり判定",
  building_physics_surfaces: "建物の当たり判定",
  building_physics_exact_reduction: "当たり判定をまとめる",
  building_physics_exact_groups: "当たり判定をまとめる",
  building_physics_tolerant_reduction: "当たり判定をまとめる（5 cm）",
  building_physics_tolerant_groups: "当たり判定をまとめる（5 cm）",
  building_physics_assemble: "建物の当たり判定",
  building_physics_write: "建物の当たり判定",
  building_visual: "建物の見た目",
  texture_download: "建物のテクスチャ",
  building_glb: "建物の見た目",
  building_glb_textures: "建物のテクスチャ",
  building_glb_batches: "建物の見た目",
  building_glb_export: "建物の見た目",
  roads: "道路の見た目",
  road_markings: "路面標示（LOD3）",
  bridges_visual: "橋の見た目",
  bridges_physics: "橋の当たり判定",
  compose: "City World の組み立て",
  dataset_validation: "データの検証",
  world_generated: "できあがりの確認",
  collider_visualization: "当たり判定の表示",
  packaging: "ZIP の作成",
};
const LARGE_VISUAL_PREVIEW_BYTES = 256 * 1024 * 1024;
const prefectureSlugs = {
  "01": "hokkaido", "02": "aomori", "03": "iwate", "04": "miyagi", "05": "akita",
  "06": "yamagata", "07": "fukushima", "08": "ibaraki", "09": "tochigi", "10": "gunma",
  "11": "saitama", "12": "chiba", "13": "tokyo", "14": "kanagawa", "15": "niigata",
  "16": "toyama", "17": "ishikawa", "18": "fukui", "19": "yamanashi", "20": "nagano",
  "21": "gifu", "22": "shizuoka", "23": "aichi", "24": "mie", "25": "shiga",
  "26": "kyoto", "27": "osaka", "28": "hyogo", "29": "nara", "30": "wakayama",
  "31": "tottori", "32": "shimane", "33": "okayama", "34": "hiroshima", "35": "yamaguchi",
  "36": "tokushima", "37": "kagawa", "38": "ehime", "39": "kochi", "40": "fukuoka",
  "41": "saga", "42": "nagasaki", "43": "kumamoto", "44": "oita", "45": "miyazaki",
  "46": "kagoshima", "47": "okinawa",
};
const BUILD_KEY = "hakoniwa-environment-city-world-build";

// The Web UI's job id, in the Studio's id characters (a "." becomes "_").
function generatedJobId(selection, inspected) {
  const codes = [...new Set(inspected.municipalities.map((item) => item.city_code))].sort();
  const cityCode = codes[0] ?? "00000";
  const prefecture = prefectureSlugs[cityCode.slice(0, 2)] ?? `pref${cityCode.slice(0, 2)}`;
  const center = selection.center;
  const multiple = codes.length > 1 ? "-multi" : "";
  return `${prefecture}-${cityCode}${multiple}-lat${center.latitude.toFixed(3)}-lon${center.longitude.toFixed(3)}`
    .replaceAll(".", "_");
}

function progressText(progress) {
  const phase = progressPhaseLabels[progress.phase] ?? progress.phase;
  const heading = phase ? `${phase} — ` : "";
  return `${progress.percent}% — ${heading}${progress.message}`;
}

// What a download will be: unknown when the catalog gives no sizes (it answers 0).
function downloadSize(bytes) {
  return bytes > 0 ? `約 ${formatBytes(bytes)}` : "サイズ不明";
}

const COMPONENT_NAMES = { buildings: "建物", terrain: "地形", roads: "道路", road_markings: "路面標示", bridges: "橋" };

// Lines of a failure, folded under 詳細 (the log is for when it is needed).
function details(lines, log) {
  return el("details", { class: "failure-details" }, el("summary", {}, "詳細"),
    ...lines.map((line) => el("div", {}, line)), log ? el("div", { class: "mono" }, `ログ：${log}`) : null);
}

function formatBytes(value) {
  if (value < 1000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)} KB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

function capabilityPresentation(capability) {
  if (capability.dataset_status !== "available") return { style: "unavailable", symbol: "—", title: "なし" };
  if (capability.generation_status !== "candidate") return { style: "limited", symbol: "△", title: "あるが使わない" };
  return { style: "candidate", symbol: "✓", title: "あり" };
}

function disposeObject(root) {
  root.traverse((object) => {
    if (object.geometry) object.geometry.dispose();
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials.filter(Boolean)) {
      for (const value of Object.values(material)) {
        if (value?.isTexture) value.dispose();
      }
      material.dispose();
    }
  });
}

// page: {map, exportDir, selection(), selectionIsValid(), selectionBounds(),
// applySelection(selection), setStatus(message, kind), importWorld(build, id), toOsm()}.
export function cityWorlds(page) {
  const { map } = page;
  const elements = Object.fromEntries([
    "physics-level", "terrain-uncovered-policy", "coplanar-union", "convex-decompose", "tolerant-planar",
    "inspect", "generate", "cancel", "mesh-summary", "overall", "municipality", "capabilities", "generation",
    "to-osm", "osm-generate", "osm-cancel", "osm-generation", "artifact-status",
    "world-entries", "world-root", "world-search", "made-count", "entry-detail", "entry-title", "artifact-path", "artifact-detail", "cache-info", "download", "view3d", "delete-artifact",
    "export-artifact", "import-artifact", "viewer-visual", "viewer-collider", "viewer-panel", "viewer-status",
    "viewer-canvas", "log",
  ].map((id) => [id, document.getElementById(id)]));
  elements["export-artifact"].hidden = !page.exportDir;

  let inspecting = false;
  let generating = false;
  let canceling = false;
  let lastAvailable = null;
  let viewerRuntime = null;
  let viewerModels = { visual: null, collider: null };
  let viewerJobId = null;
  let viewerLoadSequence = 0;
  // The generate button, cancel button and status line of each mode.
  const controls = {
    plateau: { generate: elements.generate, cancel: elements.cancel, status: elements.generation, label: "3. City Worldを生成" },
    osm: { generate: elements["osm-generate"], cancel: elements["osm-cancel"], status: elements["osm-generation"], label: "City Worldを生成" },
  };

  const generatedRectangle = L.rectangle([[0, 0], [0, 0]], {
    color: "#e06b23", weight: 5, opacity: 0, fill: false, dashArray: "10 7", interactive: false,
  }).addTo(map);
  const diagnosticMeshLayer = L.layerGroup().addTo(map);

  function writeLog(value) {
    elements.log.textContent += `${JSON.stringify(value, null, 2)}\n`;
    elements.log.scrollTop = elements.log.scrollHeight;
  }

  async function call(method, path, body) {
    writeLog({ request: `${method} /api/${path}`, ...(body === undefined ? {} : { body }) });
    try {
      const result = await api(method, path, body);
      writeLog({ response: `${method} /api/${path}`, result });
      return result;
    } catch (error) {
      writeLog({ response: `${method} /api/${path}`, error: error.message });
      throw error;
    }
  }

  function colliderReduction() {
    return elements["tolerant-planar"].checked ? "tolerant-planar"
      : elements["convex-decompose"].checked ? "convex-decompose"
      : elements["coplanar-union"].checked ? "coplanar-union" : "safe";
  }

  function options() {
    return {
      building_physics_level: Number(elements["physics-level"].value),
      building_collider_reduction: colliderReduction(),
      terrain_uncovered_policy: elements["terrain-uncovered-policy"].value,
    };
  }

  function refresh(valid = page.selectionIsValid()) {
    elements.inspect.disabled = !valid || inspecting || generating;
    elements.generate.disabled = !valid || inspecting || generating || lastAvailable === null;
    elements["osm-generate"].disabled = !valid || generating;
  }

  function invalidateInspection() {
    lastAvailable = null;
    diagnosticMeshLayer.clearLayers();
    elements["mesh-summary"].textContent = "";
    elements.generate.disabled = true;
    elements["to-osm"].hidden = true;
    if (!generating) {
      elements.generation.className = "generation";
      elements.generation.textContent = "";
    }
  }

  for (const id of ["physics-level", "terrain-uncovered-policy"]) {
    elements[id].addEventListener("input", () => { invalidateInspection(); refresh(); });
  }
  elements["coplanar-union"].addEventListener("change", () => {
    if (!elements["coplanar-union"].checked) {
      elements["convex-decompose"].checked = false;
      elements["tolerant-planar"].checked = false;
    }
    invalidateInspection();
    refresh();
  });
  elements["convex-decompose"].addEventListener("change", () => {
    if (elements["convex-decompose"].checked) elements["coplanar-union"].checked = true;
    else elements["tolerant-planar"].checked = false;
    invalidateInspection();
    refresh();
  });
  elements["tolerant-planar"].addEventListener("change", () => {
    if (elements["tolerant-planar"].checked) {
      elements["convex-decompose"].checked = true;
      elements["coplanar-union"].checked = true;
    }
    invalidateInspection();
    refresh();
  });

  // --- Diagnosis ------------------------------------------------------------

  function renderInspection(inspected, request) {
    diagnosticMeshLayer.clearLayers();
    const meshes = Array.isArray(inspected.query_meshes) ? inspected.query_meshes : [];
    for (const mesh of meshes) {
      const bounds = mesh.bbox;
      L.rectangle([[bounds.south, bounds.west], [bounds.north, bounds.east]], {
        color: "#00897b", weight: 3, opacity: 0.9, fillColor: "#36b7a7", fillOpacity: 0.035, dashArray: "5 5",
      }).bindTooltip(`PLATEAU 3次メッシュ: ${mesh.code}`).addTo(diagnosticMeshLayer);
    }
    elements["mesh-summary"].textContent = meshes.length
      ? `診断対象PLATEAUメッシュ: ${meshes.map((mesh) => mesh.code).join(", ")}` : "";
    const constant = request.options.terrain_uncovered_policy === "constant";
    // No DEM at all, but buildings and roads: the Studio builds it on flat ground at 0 m.
    const flatGround = inspected.status !== "available" && inspected.flat_ground_possible && constant;
    const available = inspected.status === "available" || flatGround;
    elements.overall.className = available ? "available" : "unavailable";
    if (inspected.status === "available") {
      elements.overall.textContent = `生成できます — ${inspected.source_file_count} ファイル・${downloadSize(inspected.estimated_download_bytes)}・地形（DEM）が無い所：${constant ? "標高 0 m で埋める" : "止める"}`;
    } else if (flatGround) {
      elements.overall.textContent = `生成できます（地形 DEM なし：地面は標高 0 m の平面） — ${inspected.source_file_count} ファイル・${downloadSize(inspected.estimated_download_bytes)}`;
    } else if (inspected.flat_ground_possible) {
      elements.overall.textContent = "生成できません — この範囲には PLATEAU の地形（DEM）がありません。「地形（DEM）が無い所」を「標高 0 m で埋める」にして診断し直すと、平らな地面で生成できます。";
    } else {
      elements.overall.textContent = `生成できません — この範囲には PLATEAU の${inspected.missing.map((name) => capabilityLabels[name] || name).join("・")}がありません。`;
    }
    elements.municipality.textContent = inspected.municipalities.length
      ? inspected.municipalities.map((item) => `${item.city} (${item.year}, spec ${item.spec})`).join(" / ") : "";
    elements.capabilities.replaceChildren();
    for (const [name, label] of Object.entries(capabilityLabels)) {
      const capability = inspected.capabilities[name];
      const view = capabilityPresentation(capability);
      elements.capabilities.append(el("div", { class: `capability ${view.style}` },
        el("div", { class: "symbol" }, view.symbol),
        el("div", {},
          el("strong", {}, `${label}：${view.title}`),
          el("small", {}, capability.dataset_status === "available"
            ? `最大 LOD${capability.max_lod}・${capability.source_file_count} ファイル${capability.reason ? `・${capabilityReasons[capability.reason] ?? capability.reason}` : ""}`
            : capabilityReasons[capability.reason] ?? capability.reason))));
    }
    // Its name for people (the list, urban's City list): the municipalities and the size.
    const cities = (inspected.building_municipalities?.length ? inspected.building_municipalities
      : [...new Set(inspected.municipalities.map((item) => item.city))]).join("・") || "PLATEAU";
    lastAvailable = available ? { request, jobId: generatedJobId(request.selection, inspected),
      title: `${cities} 付近（${sizeText(request.selection)}）` } : null;
    elements["to-osm"].hidden = available;
  }

  async function inspectSelection() {
    if (inspecting || !page.selectionIsValid()) return;
    inspecting = true;
    invalidateInspection();
    elements.inspect.disabled = true;
    elements.inspect.textContent = "診断中…";
    elements.overall.className = "";
    elements.overall.textContent = "PLATEAU catalogを確認中";
    elements.capabilities.replaceChildren();
    elements.municipality.textContent = "";
    const request = { selection: page.selection(), options: options() };
    try {
      renderInspection(await call("POST", "plateau/inspect", request), request);
    } catch (error) {
      elements.overall.className = "failed";
      elements.overall.textContent = `診断処理に失敗しました — ${error.message}`;
    } finally {
      inspecting = false;
      elements.inspect.textContent = "2. Capabilityを診断";
      refresh();
    }
  }

  // --- Generation -----------------------------------------------------------

  // Started with an export folder (from hakoniwa-urban-mobility): a new City World goes there.
  async function afterGenerated(id, build, title) {
    if (page.exportDir) await exportWorld({ path: build, title: title || id });
  }

  function saveRunning(value) {
    try {
      if (value) localStorage.setItem(BUILD_KEY, JSON.stringify(value));
      else localStorage.removeItem(BUILD_KEY);
    } catch { /* storage unavailable */ }
  }

  function loadRunning() {
    try {
      const saved = localStorage.getItem(BUILD_KEY);
      if (!saved) return null;
      try { return JSON.parse(saved); } catch { return { id: saved, mode: "plateau" }; }  // saved before modes
    } catch { return null; }
  }

  async function follow(id, mode, title = null) {
    const ui = controls[mode] ?? controls.plateau;
    generating = true;
    refresh();
    ui.generate.textContent = "生成中…";
    ui.cancel.disabled = false;
    ui.status.className = "generation running";
    saveRunning({ id, mode, title });
    const running = { title };
    try {
      for (;;) {
        const status = await api("GET", `city-worlds/build/${encodeURIComponent(id)}`);
        if (status.state === "running") {
          if (!canceling) ui.status.textContent = progressText(status.progress);
          await new Promise((resolve) => setTimeout(resolve, 2000));
          continue;
        }
        writeLog({ type: "BUILD_FINISHED", status: { ...status, log_tail: undefined } });
        if (status.state === "done") {
          ui.status.className = "generation ready";
          ui.status.textContent = `生成しました — ${id}`;
          await afterGenerated(id, status.build, running?.title);
          await refreshGeneratedJobs(`studio:${id}`);
          page.showMade?.();
        } else if (status.state === "canceled") {
          ui.status.className = "generation canceled";
          ui.status.textContent = "生成をキャンセルしました。";
        } else {
          ui.status.className = "generation failed";
          if (status.failure?.code === "DEM_UNCOVERED") {
            ui.status.replaceChildren(el("div", {}, `地形生成を停止しました — DEM が範囲の一部（${status.failure.uncovered_samples} 点：川や海の上など）を覆っていません。`
              + "生成条件の「地形（DEM）が無い所」を「標高 0 m で埋める（水面向け）」にして、もう一度診断・生成してください。"),
              details([], status.log));
          } else {
            const lines = status.errors.length ? status.errors : status.log_tail.slice(-5);
            ui.status.replaceChildren(el("div", {}, `生成できませんでした — ${id}`), details(lines, status.log));
          }
        }
        break;
      }
    } catch (error) {
      ui.status.className = "generation failed";
      ui.status.textContent = `生成の状態を取得できません — ${error.message}`;
    } finally {
      saveRunning(null);
      generating = false;
      canceling = false;
      ui.cancel.disabled = true;
      ui.cancel.textContent = "生成をキャンセル";
      ui.generate.textContent = ui.label;
      refresh();
    }
  }

  // Start a generation (body: POST /api/city-worlds/build) and follow it.
  async function generate(id, body, mode) {
    if (generating) return;
    const ui = controls[mode];
    ui.status.className = "generation running";
    ui.status.textContent = mode === "osm" ? "OpenStreetMap から地図データを取得しています…" : "生成を始めています…";
    ui.generate.disabled = true;
    try {
      await call("POST", "city-worlds/build", { id, name: id, ...body, overwrite: true });
    } catch (error) {
      ui.status.className = "generation failed";
      ui.status.textContent = `生成できませんでした — ${error.message}`;
      refresh();
      return;
    }
    await follow(id, mode, body.name || null);
  }

  async function generateWorld() {
    if (lastAvailable === null) return;
    // The Web UI's id (the municipality and the centre): the same place generated again replaces it.
    await generate(lastAvailable.jobId, { ...lastAvailable.request, name: lastAvailable.title, source: "plateau" }, "plateau");
  }

  async function cancelGeneration() {
    const running = loadRunning();
    if (!generating || canceling || !running) return;
    const ui = controls[running.mode] ?? controls.plateau;
    canceling = true;
    ui.cancel.disabled = true;
    ui.cancel.textContent = "キャンセル中…";
    ui.status.className = "generation running";
    ui.status.textContent = "生成処理を終了しています…";
    try {
      await call("POST", `city-worlds/build/${encodeURIComponent(running.id)}/cancel`, {});
    } catch (error) {
      canceling = false;
      ui.cancel.disabled = false;
      ui.cancel.textContent = "生成をキャンセル";
      ui.status.className = "generation failed";
      ui.status.textContent = `キャンセル要求の送信に失敗しました。生成処理は継続しています — ${error.message}`;
    }
  }

  // --- What was made: one list ---------------------------------------------
  // The City Worlds this Studio generated (GET /api/city-worlds/jobs: 3D, ZIP,
  // delete) and the Envsim builds found in the workspace, the Business Pack's
  // City World jobs among them (GET /api/city-worlds: take in as parts, write
  // out). An entry: {key, kind: "studio" | "workspace", title, selection, job | build}.
  let entries = [];
  let chosenKey = null;
  const ROOT_KEY = "hakoniwa-environment-world-root";
  try { elements["world-root"].value = localStorage.getItem(ROOT_KEY) || ""; } catch { /* storage unavailable */ }

  function selectedEntry() {
    return entries.find((entry) => entry.key === chosenKey) ?? null;
  }

  // The Studio's own job (3D, ZIP, delete), or null.
  function selectedGeneratedJob() {
    const entry = selectedEntry();
    return entry?.kind === "studio" ? entry.job : null;
  }

  function sizeText(selection) {
    if (!selection) return "";
    const { east_west: ew, north_south: ns } = selection.half_extent_m;
    return ew === ns ? `${Math.round(ew * 2)} m 四方` : `${Math.round(ew * 2)} × ${Math.round(ns * 2)} m`;
  }

  function badgeOf(entry) {
    if (entry.kind === "workspace") return "Business Pack など";
    return entry.job.source === "osm" ? "Studio・OpenStreetMap" : "Studio・PLATEAU";
  }

  function renderEntries() {
    elements["made-count"].textContent = entries.length ? `${entries.length}` : "";
    elements["world-entries"].replaceChildren(...(entries.length ? entries.map((entry) => el("li", {},
      el("button", {
        class: `world-entry${entry.key === chosenKey ? " chosen" : ""}`, type: "button",
        onclick: () => choose(entry.key, { restoreSelection: true }),
      },
        el("span", { class: "entry-title" }, entry.title),
        el("span", { class: "entry-meta" },
          el("span", { class: `badge ${entry.kind}` }, badgeOf(entry)),
          ` ${sizeText(entry.selection)}${entry.exported ? "・書き出し済み" : ""}`)))) : [el("li", { class: "hint" }, "まだありません")]));
  }

  function applyGeneratedSelection(entry) {
    const selection = entry?.selection;
    if (selection === null || selection === undefined) {
      generatedRectangle.setStyle({ opacity: 0 });
      return;
    }
    const job = entry.kind === "studio" ? entry.job : null;
    if (job) {
      if (Number.isInteger(job.building_physics_level)) elements["physics-level"].value = String(job.building_physics_level);
      const reduction = job.building_collider_reduction;
      elements["coplanar-union"].checked = ["coplanar-union", "convex-decompose", "tolerant-planar"].includes(reduction);
      elements["convex-decompose"].checked = ["convex-decompose", "tolerant-planar"].includes(reduction);
      elements["tolerant-planar"].checked = reduction === "tolerant-planar";
      elements["terrain-uncovered-policy"].value = job.terrain_uncovered_policy === "constant" ? "constant" : "error";
    }
    page.applySelection(selection);
    invalidateInspection();
    refresh();
    const bounds = page.selectionBounds();
    generatedRectangle.setBounds(bounds);
    generatedRectangle.setStyle({ opacity: 1 });
    generatedRectangle.unbindTooltip().bindTooltip(entry.title);
    map.fitBounds(bounds.pad(0.35), { maxZoom: 19 });
  }

  function choose(key, { restoreSelection = false } = {}) {
    chosenKey = key;
    renderEntries();
    updateArtifactSelection({ restoreSelection });
  }

  function updateArtifactSelection({ restoreSelection = false } = {}) {
    const entry = selectedEntry();
    const job = selectedGeneratedJob();
    elements["entry-detail"].hidden = entry === null;
    if (entry === null) return;
    elements["entry-title"].textContent = entry.title;
    // 3D, the ZIP and deleting: the Studio's own City Worlds; taking in and writing out: any.
    for (const id of ["download", "view3d", "delete-artifact", "viewer-visual", "viewer-collider"]) elements[id].hidden = job === null;
    elements["viewer-visual"].closest("fieldset").hidden = job === null;
    for (const id of ["download", "view3d", "delete-artifact", "viewer-visual"]) elements[id].disabled = job === null;
    elements["viewer-collider"].disabled = job === null || !job?.collider_available;
    elements["import-artifact"].disabled = false;
    elements["export-artifact"].disabled = entry.kind === "workspace" && !entry.build.exportable;
    elements["export-artifact"].textContent = entry.exported ? "書き出しを消す" : "書き出す";
    elements["export-artifact"].title = entry.exported
      ? `書き出し先の ${entry.exported.id} を消します（City World 自体は残ります）`
      : "この City World を変換せずにそのまま書き出し先に書き出します";
    if (job !== null && !job.collider_available) {
      elements["viewer-visual"].checked = true;
      elements["viewer-collider"].checked = false;
    } else if (job !== null && job.visual_size_bytes > LARGE_VISUAL_PREVIEW_BYTES) {
      // A city-scale textured GLB can exceed browser memory limits. Prefer the
      // much smaller collider preview while keeping Visual available on demand.
      elements["viewer-visual"].checked = false;
      elements["viewer-collider"].checked = true;
    }
    elements["artifact-path"].textContent = job ? `${job.path}/` : entry.build.path;
    elements["cache-info"].hidden = job === null;
    if (job === null) {
      const build = entry.build;
      elements["artifact-detail"].textContent = [
        `建物 ${build.buildings} 棟・${build.feature_types.join(" / ")}`,
        build.world ? "City World あり" : "City World なし（部品として取り込むだけできます）",
        build.dem ? "地形（DEM）あり" : null,
        entry.exported ? `書き出し済み：${entry.exported.id}` : null,
      ].filter(Boolean).join("\n");
    } else {
      const componentCounts = job.colliders?.by_component ?? {};
      const componentText = Object.entries(componentCounts).map(([name, count]) => `${COMPONENT_NAMES[name] || name} ${count}`).join("・");
      const classCounts = job.colliders?.by_physics_class ?? {};
      const classText = ["P0", "P1", "P2", "P3"].map((name) => `${name} ${classCounts[name] ?? 0}`).join("・");
      const geomTypes = job.colliders?.building_by_geom_type;
      elements["artifact-detail"].textContent = [
        `データ：${job.source === "osm" ? "OpenStreetMap（地面は標高 0 m の平面）" : "PLATEAU"}`,
        `ID：${job.job_id}`,
        `建物の当たり判定の細かさ：${job.building_physics_level ?? "不明"}（減らし方 ${job.building_collider_reduction ?? "safe"}）`,
        `当たり判定：${job.colliders?.total ?? "不明"} 個${componentText ? `（${componentText}）` : ""}`,
        job.colliders?.by_physics_class ? `建物の内訳：${classText}` : null,
        geomTypes ? `建物の形：箱 ${geomTypes.box}・メッシュ ${geomTypes.mesh}` : null,
        `3D 表示のファイル：見た目 ${formatBytes(job.visual_size_bytes ?? 0)}・当たり判定 ${formatBytes(job.collider_size_bytes ?? 0)}`,
        job.visual_size_bytes > LARGE_VISUAL_PREVIEW_BYTES ? "見た目が大きいので、3D は当たり判定だけを表示します（見た目も選べます）。" : null,
        entry.exported ? `書き出し済み：${entry.exported.id}` : null,
      ].filter(Boolean).join("\n");
    }
    if (restoreSelection) applyGeneratedSelection(entry);
  }

  async function refreshGeneratedJobs(preferredKey = null, { restoreSelection = true } = {}) {
    const root = elements["world-root"].value.trim();
    const [studio, workspace] = await Promise.all([
      api("GET", "city-worlds/jobs").catch((error) => ({ error })),
      api("GET", `city-worlds${root ? `?root=${encodeURIComponent(root)}` : ""}`).catch((error) => ({ error })),
    ]);
    const jobs = Array.isArray(studio.jobs) ? studio.jobs : [];
    if (studio.error) writeLog({ type: "GENERATED_INDEX_FAILED", error: String(studio.error.message ?? studio.error) });
    if (workspace.error) writeLog({ type: "WORKSPACE_SEARCH_FAILED", error: String(workspace.error.message ?? workspace.error) });
    const cache = studio.shared_cache;
    elements["cache-info"].textContent = cache
      ? `共有キャッシュ：${cache.object_count} ファイル・${formatBytes(cache.size_bytes)}（${cache.path}）`
      : "共有キャッシュ：取得できません";
    const own = new Set(jobs.map((job) => job.build));
    entries = [
      ...jobs.map((job) => ({ key: `studio:${job.job_id}`, kind: "studio", title: job.title || job.job_id,
        selection: job.selection, exported: job.exported, job })),
      ...(Array.isArray(workspace.builds) ? workspace.builds : []).filter((build) => !own.has(build.path)).map((build) => ({
        key: `workspace:${build.path}`, kind: "workspace", title: build.title, exported: build.exported, build,
        selection: build.center ? { center: build.center, half_extent_m: build.half_extent_m } : null })),
    ];
    const keep = preferredKey ?? chosenKey;
    chosenKey = entries.some((entry) => entry.key === keep) ? keep : null;
    renderEntries();
    updateArtifactSelection({ restoreSelection: restoreSelection && chosenKey !== null });
  }

  function closeViewerForJob(jobId) {
    if (viewerJobId !== jobId) return;
    viewerLoadSequence += 1;
    if (viewerRuntime !== null) {
      for (const model of Object.values(viewerModels).filter(Boolean)) {
        viewerRuntime.scene.remove(model);
        disposeObject(model);
      }
      viewerRuntime.renderer.render(viewerRuntime.scene, viewerRuntime.camera);
    }
    viewerModels = { visual: null, collider: null };
    viewerJobId = null;
    document.body.classList.remove("viewer-open");
    requestAnimationFrame(() => map.invalidateSize());
  }

  async function deleteSelectedArtifact() {
    const job = selectedGeneratedJob();
    if (job === null) return;
    if (!window.confirm(`生成結果 ${job.job_id} を削除しますか？\n`
      + "その City World のフォルダ（ZIP・見た目・当たり判定・途中のファイル）を削除します。\n"
      + "共有CityGMLキャッシュは削除しません。")) return;
    elements["delete-artifact"].disabled = true;
    try {
      await call("POST", `city-worlds/jobs/${encodeURIComponent(job.job_id)}/delete`, {});
      closeViewerForJob(job.job_id);
      generatedRectangle.setStyle({ opacity: 0 });
      elements["artifact-status"].className = "generation ready";
      elements["artifact-status"].textContent = `削除しました — ${job.job_id}（ダウンロード済みの CityGML は共有キャッシュに残しています）`;
      chosenKey = null;
      await refreshGeneratedJobs(null, { restoreSelection: false });
    } catch (error) {
      elements["artifact-status"].className = "generation failed";
      elements["artifact-status"].textContent = `生成結果の削除に失敗しました — ${error.message}`;
      updateArtifactSelection();
    }
  }

  // A City World written to the export folder as it is (POST /api/city-worlds/export);
  // the tool watching that folder (hakoniwa-urban-mobility) takes it as a World.
  async function exportWorld(build) {
    page.setStatus(`${build.title} を書き出しています…`);
    try {
      const result = await call("POST", "city-worlds/export", { path: build.path, title: build.title });
      page.setStatus(`${build.title} を ${result.id} として書き出しました（${result.job}）`, "ok");
    } catch (error) {
      page.setStatus(error.message, "error");
    }
  }

  async function exportSelected() {
    const entry = selectedEntry();
    if (entry === null) return;
    if (entry.exported) {
      if (!window.confirm(`書き出し先の ${entry.exported.id} を消しますか？（City World 自体は残ります）`)) return;
      try {
        await call("POST", `exports/${encodeURIComponent(entry.exported.id)}/delete`, {});
        page.setStatus(`書き出し先の ${entry.exported.id} を消しました`, "ok");
      } catch (error) {
        page.setStatus(error.message, "error");
      }
    } else {
      await exportWorld({ path: entry.kind === "studio" ? entry.job.build : entry.build.path, title: entry.title });
    }
    await refreshGeneratedJobs(entry.key, { restoreSelection: false });
  }

  // --- 3D viewer ------------------------------------------------------------

  function applyViewerMode() {
    if (viewerModels.visual === null && viewerModels.collider === null) return;
    if (viewerModels.visual !== null) viewerModels.visual.visible = elements["viewer-visual"].checked;
    if (viewerModels.collider !== null) viewerModels.collider.visible = elements["viewer-collider"].checked;
    viewerRuntime?.renderer.render(viewerRuntime.scene, viewerRuntime.camera);
  }

  function viewerLayerLabel() {
    if (elements["viewer-visual"].checked && elements["viewer-collider"].checked) return "見た目＋当たり判定";
    return elements["viewer-visual"].checked ? "見た目" : "当たり判定";
  }

  async function changeViewerLayer(changedElement) {
    if (!elements["viewer-visual"].checked && !elements["viewer-collider"].checked) changedElement.checked = true;
    const missingRequestedLayer = viewerJobId !== null && (
      (elements["viewer-visual"].checked && viewerModels.visual === null)
      || (elements["viewer-collider"].checked && viewerModels.collider === null));
    if (missingRequestedLayer) {
      await openSelectedViewer();
      return;
    }
    applyViewerMode();
    if (viewerJobId !== null) elements["viewer-status"].textContent = `${viewerJobId} — ${viewerLayerLabel()}`;
  }

  async function initializeViewer() {
    if (viewerRuntime !== null) return viewerRuntime;
    const THREE = await import("three");
    const [{ GLTFLoader }, { OrbitControls }] = await Promise.all([
      import("three/addons/loaders/GLTFLoader.js"),
      import("three/addons/controls/OrbitControls.js"),
    ]);
    const renderer = new THREE.WebGLRenderer({ canvas: elements["viewer-canvas"], antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xdde6eb);
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100000);
    const controls = new OrbitControls(camera, renderer.domElement);
    // Render only on interaction so an idle Viewer does not consume CPU.
    controls.enableDamping = false;
    controls.addEventListener("change", () => renderer.render(scene, camera));
    scene.add(new THREE.HemisphereLight(0xffffff, 0x53606b, 2.2));
    const sunlight = new THREE.DirectionalLight(0xffffff, 2.5);
    sunlight.position.set(100, 180, 80);
    scene.add(sunlight);
    const resize = () => {
      const width = Math.max(1, elements["viewer-panel"].clientWidth);
      const height = Math.max(1, elements["viewer-panel"].clientHeight);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.render(scene, camera);
    };
    new ResizeObserver(resize).observe(elements["viewer-panel"]);
    viewerRuntime = { THREE, GLTFLoader, renderer, scene, camera, controls, resize };
    return viewerRuntime;
  }

  async function openSelectedViewer() {
    const job = selectedGeneratedJob();
    if (job === null) return;
    const loadSequence = ++viewerLoadSequence;
    document.body.classList.add("viewer-open");
    elements["viewer-status"].textContent = `${job.job_id} を読み込み中…`;
    requestAnimationFrame(() => map.invalidateSize());
    const base = `/api/city-worlds/jobs/${encodeURIComponent(job.job_id)}`;
    try {
      const runtime = await initializeViewer();
      runtime.resize();
      const visualGltf = elements["viewer-visual"].checked
        ? await new runtime.GLTFLoader().loadAsync(`${base}/city-world.glb`) : null;
      const colliderGltf = job.collider_available && elements["viewer-collider"].checked
        ? await new runtime.GLTFLoader().loadAsync(`${base}/city-world-colliders.glb`) : null;
      if (loadSequence !== viewerLoadSequence) {
        if (visualGltf !== null) disposeObject(visualGltf.scene);
        if (colliderGltf !== null) disposeObject(colliderGltf.scene);
        return;
      }
      for (const model of Object.values(viewerModels).filter(Boolean)) {
        runtime.scene.remove(model);
        disposeObject(model);
      }
      viewerModels = { visual: visualGltf?.scene ?? null, collider: colliderGltf?.scene ?? null };
      viewerJobId = job.job_id;
      if (viewerModels.visual !== null) runtime.scene.add(viewerModels.visual);
      if (viewerModels.collider !== null) {
        const colliderMaterial = new runtime.THREE.MeshBasicMaterial({
          color: 0x28a86b, transparent: true, opacity: 0.38, wireframe: true, depthTest: true, depthWrite: false,
        });
        viewerModels.collider.traverse((object) => {
          if (!object.isMesh) return;
          const oldMaterials = Array.isArray(object.material) ? object.material : [object.material];
          oldMaterials.filter(Boolean).forEach((material) => material.dispose());
          object.material = colliderMaterial;
          object.renderOrder = 10;
        });
        runtime.scene.add(viewerModels.collider);
      }
      const box = new runtime.THREE.Box3();
      if (viewerModels.visual !== null) box.expandByObject(viewerModels.visual);
      if (viewerModels.collider !== null) box.expandByObject(viewerModels.collider);
      if (box.isEmpty()) throw new Error("表示できる形がありません");
      const center = box.getCenter(new runtime.THREE.Vector3());
      const size = box.getSize(new runtime.THREE.Vector3());
      if (viewerModels.visual !== null) viewerModels.visual.position.set(-center.x, -box.min.y, -center.z);
      if (viewerModels.collider !== null) viewerModels.collider.position.set(-center.x, -box.min.y, -center.z);
      const distance = Math.max(size.x, size.y, size.z, 10) * 1.35;
      runtime.camera.near = Math.max(0.1, distance / 10000);
      runtime.camera.far = Math.max(2000, distance * 20);
      runtime.camera.position.set(distance * 0.65, distance * 0.55, distance * 0.65);
      runtime.camera.updateProjectionMatrix();
      runtime.controls.target.set(0, Math.max(0, size.y * 0.2), 0);
      runtime.controls.update();
      applyViewerMode();
      elements["viewer-status"].textContent = `${job.job_id} — ${viewerLayerLabel()}`;
    } catch (error) {
      elements["viewer-status"].textContent = "3D で表示できませんでした（理由は「通信ログ」にあります）";
      writeLog({ type: "VIEWER_FAILED", job_id: job.job_id, error: String(error) });
    }
  }

  elements.inspect.addEventListener("click", inspectSelection);
  elements.generate.addEventListener("click", generateWorld);
  elements.cancel.addEventListener("click", cancelGeneration);
  elements["osm-cancel"].addEventListener("click", cancelGeneration);
  elements["to-osm"].addEventListener("click", () => page.toOsm());
  elements["viewer-visual"].addEventListener("change", (event) => changeViewerLayer(event.target));
  elements["viewer-collider"].addEventListener("change", (event) => changeViewerLayer(event.target));
  elements.download.addEventListener("click", () => {
    const job = selectedGeneratedJob();
    if (job !== null) window.location.assign(`/api/city-worlds/jobs/${encodeURIComponent(job.job_id)}/artifact.zip`);
  });
  elements.view3d.addEventListener("click", openSelectedViewer);
  elements["delete-artifact"].addEventListener("click", deleteSelectedArtifact);
  elements["export-artifact"].addEventListener("click", exportSelected);
  elements["import-artifact"].addEventListener("click", () => {
    const entry = selectedEntry();
    if (entry === null) return;
    page.importWorld(entry.kind === "studio"
      ? { id: entry.job.job_id, path: entry.job.build, title: entry.title }
      : { id: entry.build.id, path: entry.build.path, title: entry.title });
  });
  elements["world-search"].addEventListener("click", () => {
    try { localStorage.setItem(ROOT_KEY, elements["world-root"].value.trim()); } catch { /* storage unavailable */ }
    refreshGeneratedJobs();
  });

  refreshGeneratedJobs(null, { restoreSelection: false });
  const running = loadRunning();  // a generation started before this page was opened again
  if (running) follow(running.id, running.mode, running.title);

  return {
    // The selection moved: a diagnosis is for the selection it was made for.
    selectionChanged(valid) { invalidateInspection(); refresh(valid); },
    inspect: inspectSelection,
    generate,
  };
}
