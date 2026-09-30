// Small helpers shared by the Studio's modules: DOM lookup and building, and
// the Studio server's JSON API.

export const $ = (selector) => document.querySelector(selector);

// el("button", {class: "secondary", onclick: fn}, "label", child, ...)
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child);
  return node;
}

export async function api(method, path, body) {
  const response = await fetch(`/api/${path}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (response.status === 404 && String(data.error).startsWith("no API ")) {
    // The page files are newer than the running server (the repository was updated while it ran).
    throw new Error(`この画面より古い Environment Studio が動いています。停止して起動し直してください（${data.error}）`);
  }
  if (!response.ok) throw new Error(data.error || `${response.status}`);
  return data;
}

// The server started before the checkout was updated (GET /api/health
// code_updated): a banner under the top bar that stays until the Studio is
// restarted (the page is newer than the server it talks to).
export async function warnIfServerIsOld() {
  let health;
  try { health = await api("GET", "health"); } catch { return; }
  if (!health.code_updated) return;
  const banner = el("div", { class: "stale-banner", role: "alert" },
    "Environment Studio のコードが、起動したあとに更新されています。この画面の機能を正しく使うには、"
    + "Environment Studio を停止して起動し直してください。");
  (document.querySelector(".topbar") ?? document.body.firstElementChild).after(banner);
}
