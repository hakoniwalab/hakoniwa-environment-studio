// Problems of a layout, for the list under the plan and the red outlines: the
// quick in-browser check (web/geometry.js), then the server's diagnostics
// (schema, then MuJoCo) once they cover the layout on screen.

import { el } from "./dom.js";
import { checkLayout } from "./geometry.js";

const EDGE_NAMES = { north: "北", south: "南", east: "東", west: "西" };

// {outside: Set, overlapping: Set, lines: [text]} from the quick check (flat
// ground, roads left out: approximate until the server answers).
export function quickProblems(views, area) {
  const check = checkLayout(views, area);
  return {
    outside: new Set(check.outside), overlapping: new Set(check.overlaps.flatMap((pair) => [pair.a, pair.b])),
    lines: [
      ...check.outside.map((id) => `${id} が環境の外にはみ出しています`),
      ...check.overlaps.map((pair) => `${pair.a} と ${pair.b} が ${Math.round(pair.depth * 1000)} mm 重なっています`),
    ],
  };
}

// The object an objects[i] path points at (diagnostics use the Recipe's paths).
export function objectAt(objects, path) {
  const match = /^objects\[(\d+)\]/.exec(path || "");
  return match ? objects[Number(match[1])] : null;
}

// The same shape from the server's answer ({stage, diagnostics}).
export function fromServer(result, objects) {
  const outside = new Set();
  const overlapping = new Set();
  const lines = result.diagnostics.filter((item) => item.severity !== "warning").map((item) => {
    const obj = objectAt(objects, item.path);
    const other = objectAt(objects, item.related?.[0]);
    const depth = (value) => Math.round(value * 1000);
    if (item.code === "overlap" && obj && other) {
      overlapping.add(obj.id).add(other.id);
      return `${obj.id} と ${other.id} が ${depth(item.actual)} mm 重なっています`;
    }
    if (item.code === "outside" && obj) {
      outside.add(obj.id);
      return `${obj.id} が${EDGE_NAMES[item.actual.edge] || item.actual.edge}の端から ${depth(item.actual.depth_m)} mm はみ出しています`;
    }
    if (item.code === "below_terrain" && obj) {
      overlapping.add(obj.id);
      return `${obj.id} が地面に ${depth(item.actual)} mm めり込んでいます`;
    }
    if (obj) overlapping.add(obj.id);
    return `${item.path}: ${item.reason}`;
  });
  return { outside, overlapping, lines, stage: result.stage };
}

// The list: a summary line (by whom it was checked) and one line per problem.
export function renderProblems(host, problems, byServer, hasObjects) {
  const source = byServer ? (problems.stage === "schema" ? "スキーマ" : "MuJoCo") : "簡易チェック・MuJoCo 検証中";
  const items = problems.lines.map((line) => el("li", {}, line));
  const summary = items.length
    ? el("li", { class: "summary" }, `NG（${source}）`)
    : el("li", { class: "ok" }, hasObjects ? `OK：重なり・はみ出し・地面へのめり込みはありません（${source}）` : "");
  host.replaceChildren(summary, ...items);
}
