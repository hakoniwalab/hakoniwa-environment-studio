// The right panel: one selected part (its position, the parameters its item
// lets a placement change, where it comes from) or several (turn and move
// them together). Its fields are built from the parameter definitions the
// server sends; the panel knows no part types.
//
// createInspector(host, app) takes what it needs from the Studio as `app`:
//   render(options), recipe(), parts (web/parts.js), resolved(part),
//   selectedParts(), select(ids), rename(part, id), duplicate(), remove(),
//   slide(direction), move(dx, dy), applyPoses(poses).

import { el } from "./dom.js";
import { normalizeYaw } from "./geometry.js";
import { pivotOf, roundMm, turned } from "./plan2d.js";

const NUDGE_BUTTON_M = 0.1;
// Screen directions in the environment frame: up on the plan is +y (north).
const DIRECTIONS = { left: [-1, 0], up: [0, 1], down: [0, -1], right: [1, 0] };
const ARROWS = { left: "←", up: "↑", down: "↓", right: "→" };

export function createInspector(host, app) {
  // While a slider is held the Studio does not rebuild the panel (that would
  // drop the slider); groupBase holds the poses "turn together" started from.
  let sliding = false;
  let groupBase = null;

  // A slider and a number box for the same value: the slider applies
  // as it moves, the number box when it is entered.
  function sliderField(label, value, onchange, { min, max, step = 1, numberStep = step }) {
    const clamp = (number) => Math.max(min, Math.min(max, number));
    const range = el("input", { type: "range", min: String(min), max: String(max), step: String(step), value: String(value) });
    const number = el("input", { type: "number", min: String(min), max: String(max), step: String(numberStep), value: String(value) });
    range.addEventListener("pointerdown", () => { sliding = true; });
    range.addEventListener("input", () => {
      number.value = range.value;
      onchange(Number(range.value));
      app.render({ live: true });
    });
    range.addEventListener("change", () => { sliding = false; app.render(); });
    range.addEventListener("pointerup", () => { if (sliding) { sliding = false; app.render(); } });
    number.addEventListener("change", () => {
      const value = Number(number.value);
      if (number.value.trim() === "" || !Number.isFinite(value)) { number.value = range.value; return; }
      range.value = String(clamp(value));
      onchange(clamp(value));
      app.render();
    });
    return el("label", { class: "field slider-field" }, label, el("span", { class: "slider-row" }, range, number));
  }

  function numberField(label, value, onchange, attributes = {}) {
    return el("label", { class: "field" }, label, el("input", {
      type: "number", value: String(value), step: "any", ...attributes,
      onchange: (event) => {
        const text = event.target.value.trim();
        const number = Number(text);
        if (text !== "" && Number.isFinite(number)) onchange(number); // an emptied field keeps the value
        app.render();
      },
    }));
  }

  function renderInspector() {
    const chosen = app.selectedParts();
    if (chosen.length > 1) {
      renderGroupInspector(host, chosen);
      return;
    }
    const part = chosen[0];
    if (!part) {
      host.replaceChildren(el("p", { class: "hint" },
        "部品をクリックすると、ここで位置やパラメータを編集できます（位置は m、1 mm 単位）。Shift＋クリックで複数選べます。"));
      return;
    }
    const entry = app.parts.item(part.item);
    const view = app.resolved(part);
    host.replaceChildren(...[
      el("label", { class: "field" }, "ID", el("input", {
        value: part.id,
        onchange: (event) => app.rename(part, event.target.value.trim()),
      })),
      el("p", { class: "meta" }, `${entry?.name || part.item}（${entry?.category || ""}・${part.item}）`),
      sourceLine(entry),
      objectSource(part),
      el("div", { class: "grid2" },
        numberField("x (m)・東", part.pose.x_m, (value) => { part.pose.x_m = roundMm(value); }),
        numberField("y (m)・北", part.pose.y_m, (value) => { part.pose.y_m = roundMm(value); }),
        sliderField("角度 (°)", Math.round(part.pose.yaw_deg ?? 0), (value) => { part.pose.yaw_deg = normalizeYaw(value); }, { min: 0, max: 359 }),
      ),
      el("p", { class: "meta" }, view.surface === "elevated"
        ? `地面から ${view.z} m 上に置きます` : "地面の上に立ちます（丘の上では足元の高さに合わせます）"),
      assumedLine(entry, part),
      ...paramSection(entry, part),
      el("h2", {}, "動かす"),
      el("div", { class: "move-row" }, el("span", { class: "meta" }, "寄せる"),
        ...Object.entries(DIRECTIONS).map(([name, direction]) => el("button", {
          class: "secondary icon", title: `${ARROWS[name]} に何かに当たるまで寄せる`, onclick: () => app.slide(direction),
        }, ARROWS[name]))),
      el("div", { class: "move-row" }, el("span", { class: "meta" }, `${NUDGE_BUTTON_M} m`),
        ...Object.entries(DIRECTIONS).map(([name, [dx, dy]]) => el("button", {
          class: "secondary icon", title: `${ARROWS[name]} に ${NUDGE_BUTTON_M} m`, onclick: () => app.move(dx * NUDGE_BUTTON_M, dy * NUDGE_BUTTON_M),
        }, ARROWS[name]))),
      el("p", { class: "hint" }, "キー：矢印＝グリッド分、Alt＋矢印＝1 cm、Shift＋矢印＝10 倍、Alt＋Shift＋矢印＝寄せる。吸い付く部品はドラッグ中に近くの部品・端にぴったり寄ります（Alt で解除）。"),
      el("div", { class: "row", style: "margin-top: 12px" },
        el("button", { class: "secondary", onclick: () => app.duplicate() }, "複製"),
        el("button", { class: "danger", onclick: () => app.remove() }, "削除"),
      ),
    ].filter(Boolean));
  }

  // Several parts: turn them together about their centre, move them, copy or delete them.
  function renderGroupInspector(host, chosen) {
    const turn = sliderField("まとめて回す (°)", 0, (degrees) => {
      // Turn from the poses the slider started at, so steps do not add up rounding.
      if (!groupBase) {
        const views = chosen.map(app.resolved);
        groupBase = { views, pivot: pivotOf(views) };
      }
      app.applyPoses(turned(groupBase.views, degrees, groupBase.pivot));
    }, { min: -180, max: 180 });
    host.replaceChildren(
      el("h2", {}, `${chosen.length} 個の部品を選択中`),
      el("p", { class: "meta" }, chosen.map((part) => part.id).join("、")),
      turn,
      el("p", { class: "hint" }, "上面図の上の丸いハンドルでも回せます（15° 刻み、Shift で解除）。R で 90°。"),
      el("h2", {}, "まとめて動かす"),
      el("div", { class: "move-row" }, el("span", { class: "meta" }, "寄せる"),
        ...Object.entries(DIRECTIONS).map(([name, direction]) => el("button", {
          class: "secondary icon", title: `${ARROWS[name]} にどれかが当たるまで寄せる`, onclick: () => app.slide(direction),
        }, ARROWS[name]))),
      el("div", { class: "move-row" }, el("span", { class: "meta" }, `${NUDGE_BUTTON_M} m`),
        ...Object.entries(DIRECTIONS).map(([name, [dx, dy]]) => el("button", {
          class: "secondary icon", title: `${ARROWS[name]} に ${NUDGE_BUTTON_M} m`, onclick: () => app.move(dx * NUDGE_BUTTON_M, dy * NUDGE_BUTTON_M),
        }, ARROWS[name]))),
      el("p", { class: "hint" }, "Shift（または ⌘ / Ctrl）＋クリックで追加・外す、Shift＋背景ドラッグで範囲選択、⌘ / Ctrl＋A ですべて。選んだ部品のどれかをドラッグすると全部が動きます。矢印キーも全部に効きます。"),
      el("div", { class: "row", style: "margin-top: 12px" },
        el("button", { class: "secondary", onclick: () => app.duplicate() }, "まとめて複製"),
        el("button", { class: "danger", onclick: () => app.remove() }, "まとめて削除"),
        el("button", { class: "secondary", onclick: () => app.select([]) }, "選択を解除"),
      ),
    );
  }

  // One field for a parameter, from its definition: a list for choices, a
  // colour picker, a check box, a slider and a number for ranges (lengths in
  // metres to the millimetre, angles and integers in whole steps).
  function paramField(definition, value, set) {
    const { kind, label } = definition;
    const title = definition.description || "";
    if (definition.values) {
      return el("label", { class: "field", title }, label, el("select", {
        onchange: (event) => { set(definition.values[Number(event.target.value)]); app.render(); },
      }, ...definition.values.map((choice, index) => el("option", { value: String(index), selected: choice === value }, String(choice)))));
    }
    if (kind === "color") {
      return el("label", { class: "field", title }, label, el("input", {
        type: "color", value, onchange: (event) => { set(event.target.value.toLowerCase()); app.render(); },
      }));
    }
    if (kind === "polygon" || kind === "polyline") return pointsField(definition, value, set);
    if (kind === "bool") {
      return el("label", { class: "field inline", title }, el("input", {
        type: "checkbox", checked: value, onchange: (event) => { set(event.target.checked); app.render(); },
      }), label);
    }
    if (["length", "angle", "number", "integer"].includes(kind)) {
      const whole = kind === "angle" || kind === "integer";
      const tidy = (number) => (kind === "length" ? roundMm(number) : whole ? Math.round(number) : number);
      const unit = definition.unit ? ` (${definition.unit})` : "";
      const { min, max } = definition;
      if (min !== undefined && max !== undefined && kind !== "number") {
        const span = max - min;
        const step = whole ? 1 : span <= 50 ? 0.01 : span <= 500 ? 0.1 : 1;
        const field = sliderField(`${label}${unit}`, value, (number) => set(tidy(number)), { min, max, step, numberStep: whole ? 1 : "any" });
        field.title = title;
        return field;
      }
      return numberField(`${label}${unit}`, value, (number) => set(tidy(number)), { min, max, title });
    }
    return el("label", { class: "field", title }, label, el("input", {
      value, onchange: (event) => { set(event.target.value); app.render(); },
    }));
  }

  // A footprint or centre line: its size in words, and its points as JSON to
  // edit by hand (the server checks them: a crossing outline is refused).
  function pointsField(definition, value, set) {
    const points = Array.isArray(value) ? value : [];
    const closed = definition.kind === "polygon";
    const pairs = closed ? points.map((point, i) => [point, points[(i + 1) % points.length]]) : points.slice(1).map((point, i) => [points[i], point]);
    const length = pairs.reduce((sum, [a, b]) => sum + Math.hypot(b[0] - a[0], b[1] - a[1]), 0);
    const area = closed ? Math.abs(points.reduce((sum, [x1, y1], i) => {
      const [x2, y2] = points[(i + 1) % points.length];
      return sum + x1 * y2 - x2 * y1;
    }, 0)) / 2 : 0;
    const summary = closed ? `${points.length} 点・面積 ${Math.round(area * 10) / 10} m²・周長 ${Math.round(length * 10) / 10} m`
      : `${points.length} 点・長さ ${Math.round(length * 10) / 10} m`;
    const text = el("textarea", { rows: "4", spellcheck: "false", class: "points" }, JSON.stringify(points));
    text.addEventListener("change", () => {
      try {
        set(JSON.parse(text.value));
        app.render();
      } catch (error) {
        text.setCustomValidity(`JSON として読めません: ${error.message}`);
        text.reportValidity();
      }
    });
    text.addEventListener("input", () => text.setCustomValidity(""));
    return el("label", { class: "field", title: definition.description || "" },
      `${definition.label}（${summary}）`, text);
  }

  // Fields for parameters a caller keeps: values[name] is the current value,
  // defaults[name] the item's (a value equal to it is not written out).
  function paramFields(definitions, values, defaults, store) {
    return definitions.map((definition) => paramField(definition, values[definition.name] ?? defaults[definition.name],
      (value) => store(definition.name, value === defaults[definition.name] ? undefined : value)));
  }

  // The fields for what the item lets a placement change.
  function paramSection(entry, part) {
    const definitions = entry?.placement_params || [];
    if (!definitions.length) return [];
    const store = (name, value) => {
      part.params = { ...(part.params || {}) };
      if (value === undefined) delete part.params[name];
      else part.params[name] = value;
      if (!Object.keys(part.params).length) delete part.params;
    };
    return [
      el("h2", {}, "この部品の設定"),
      el("div", { class: "params" }, ...paramFields(definitions, part.params || {}, entry.params, store)),
      part.params ? el("button", {
        class: "secondary", onclick: () => { delete part.params; app.render(); },
      }, "品目の値に戻す") : null,
    ];
  }

  // What the item is and where it comes from.
  function sourceLine(entry) {
    const source = entry?.source || {};
    const bits = [entry?.description, source.note, source.url].filter(Boolean);
    return bits.length ? el("div", { class: "source" }, ...bits.map((bit) => el("div", {}, bit))) : null;
  }

  // Where this object came from (a map feature, #10): kept in the Recipe as its source.
  function objectSource(part) {
    const source = part.source;
    if (!source) return null;
    // The OSM element: the source itself, or (through CityGML) its source_kind / source_id tags.
    const tags = source.tags || {};
    const [kind, id] = ["node", "way", "relation"].includes(source.kind) ? [source.kind, source.id]
      : [tags.source_kind, tags.source_id];
    const osm = source.provider === "openstreetmap" && ["node", "way", "relation"].includes(kind);
    const label = source.kind === "citygml" ? `${source.provider}（CityGML ${source.id}）` : `${source.provider} ${source.kind}/${source.id}`;
    const text = Object.entries(tags).map(([key, value]) => `${key}=${value}`).join("、");
    return el("div", { class: "source" }, "出典：", label,
      osm ? el("span", {}, "・", el("a", { href: `https://www.openstreetmap.org/${kind}/${id}`, target: "_blank", rel: "noopener" },
        `OpenStreetMap ${kind}/${id}`)) : null,
      text ? el("div", {}, text) : null);
  }

  // Values the item assumes (not in its source), unless this part sets them itself.
  function assumedLine(entry, part) {
    const labels = entry?.param_labels || {};
    const items = Object.entries(entry?.assumed || {}).filter(([key]) => !(key in (part.params || {})));
    if (!items.length) return null;
    return el("div", { class: "assumed" }, el("strong", {}, "仮定値（出典に記載なし）"),
      ...items.map(([key, reason]) => el("div", {}, `${labels[key] || key}：${reason}`)));
  }

  return {
    get sliding() { return sliding; },
    paramFields,
    // Rebuild the panel for the current selection.
    render() {
      groupBase = null;
      renderInspector();
    },
  };
}
