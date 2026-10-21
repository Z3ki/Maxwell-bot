"use strict";
// Maxwell dashboard. Two sign-in modes, both served from this one file:
//   link:  /dashboard in Discord mints a one-time #t=<token>; we trade it for
//          a bearer token that lives in this variable and nowhere else. The
//          public site runs user JavaScript on the same origin, so cookies and
//          web storage are off the table. Reload = sign in again. Deal with it.
//   oauth: classic Discord login on a dedicated host, HttpOnly cookie + CSRF.
const $ = (id) => document.getElementById(id);
const BASE = (() => {
  const at = location.pathname.indexOf("/dashboard");
  return at > 0 ? location.pathname.slice(0, at) : "";
})();

let mode = "oauth",
  bearer = "",
  csrf = "",
  user = null,
  plan = null,
  servers = [],
  clientId = "",
  selected = null,
  view = "overview",
  connection = null,
  connectionScope = "chat",
  providers = {},
  serverData = null,
  files = null,
  cwd = "",
  editing = null,
  schedules = null;
let dirty = false,
  busy = false,
  connectionDirty = false;
const dirtyForms = new Set();

const personalForm = $("personal-form"),
  connectionForm = $("connection-form"),
  serverForm = $("server-form"),
  handleForm = $("handle-form"),
  scheduleForm = $("schedule-form");
const field = (form, name) => form.elements.namedItem(name);
if (window.matchMedia("(max-width:620px)").matches)
  $("server-picker").open = false;

const VIEWS = {
  overview: ["Just for you", "Overview", "Your plan, your usage and what Maxwell can do for you."],
  settings: ["Just for you", "Reply settings", "Make Maxwell feel familiar. Your choices stay in sync with Discord."],
  ai: ["Just for you", "AI connection", "Optional: point chat or Premium subagents at your own AI provider."],
  files: ["Premium storage", "Files", "Your /storage folder. It survives sandbox wipes, and public/ is your website."],
  schedules: ["Premium wake-ups", "Schedules", "Maxwell wakes up on time, runs the task in the background and sends you the result."],
};
const TEXT_FILE = /\.(html?|css|m?js|json|txt|md|csv|tsv|xml|svg|py|ya?ml|toml|ini|cfg|conf|sh|ts|tsx|jsx|lua|gd|mcfunction|mcmeta|lang|log)$/i;
const WEB_FILE = /\.(html?|css|m?js|svg)$/i;

// ---------------------------------------------------------------- helpers

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
function option(value, label) {
  const node = el("option", "", label);
  node.value = value;
  return node;
}
function button(label, className, onClick) {
  const node = el("button", className, label);
  node.type = "button";
  node.addEventListener("click", (event) => action(() => onClick(event), node));
  return node;
}
function bytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB"];
  let value = n,
    unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return (value >= 10 ? value.toFixed(0) : value.toFixed(1)) + " " + units[unit];
}
function moment(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function span(seconds) {
  seconds = Math.max(0, Math.round(Number(seconds) || 0));
  if (seconds < 90) return seconds + " seconds";
  if (seconds < 5400) return Math.round(seconds / 60) + " minutes";
  if (seconds < 172800) return Math.round(seconds / 3600) + " hours";
  return Math.round(seconds / 86400) + " days";
}
function meter(bar, used, limit) {
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  bar.style.width = (ratio * 100).toFixed(1) + "%";
  bar.parentElement.classList.toggle("warn", ratio >= 0.75 && ratio < 1);
  bar.parentElement.classList.toggle("full", ratio >= 1);
}
const join = (folder, name) => (folder ? folder + "/" + name : name);
const parent = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
const base = (path) => path.slice(path.lastIndexOf("/") + 1);

function notice(message, error = false, link = "") {
  const box = $("notice");
  box.hidden = false;
  box.classList.toggle("error", error);
  box.replaceChildren(document.createTextNode(message));
  if (link) {
    const anchor = el("a", "", link);
    anchor.href = link;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    box.append(" ", anchor);
  }
}
function resetNotice() {
  $("notice").hidden = true;
}

// ---------------------------------------------------------------- API

async function api(path, method = "GET", body, raw = false) {
  const headers = {};
  if (bearer) headers.Authorization = "Bearer " + bearer;
  if (method !== "GET") {
    if (csrf) headers["X-CSRF-Token"] = csrf;
    headers["Content-Type"] = raw ? "application/octet-stream" : "application/json";
  }
  let response;
  try {
    response = await fetch(BASE + "/api/dashboard/" + path, {
      method,
      credentials: mode === "link" ? "omit" : "same-origin",
      cache: "no-store",
      headers,
      ...(body !== undefined ? { body: raw ? body : JSON.stringify(body) } : {}),
    });
  } catch {
    throw new Error("Could not reach Maxwell. Check your connection and try again.");
  }
  let result = {};
  try {
    result = await response.json();
  } catch {
    if (response.status === 413) throw new Error("That file is too large to upload here.");
    if (!response.ok) throw new Error("Maxwell is not answering right now. Try again shortly.");
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "redeem") {
      bearer = "";
      checkpoint();
      showLogin(result.error || "Your session ended. Sign in again to continue.");
    }
    throw new Error(result.error || "Could not complete this change.");
  }
  return result;
}
async function action(callback, control) {
  if (busy) return;
  busy = true;
  if (control) control.disabled = true;
  try {
    await callback();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    if (control) control.disabled = false;
  }
}

// ---------------------------------------------------------------- dirty tracking

function markDirty(id) {
  dirtyForms.add(id);
  dirty = true;
}
function checkpoint(id) {
  if (id) dirtyForms.delete(id);
  else dirtyForms.clear();
  dirty = dirtyForms.size > 0;
}
function canNavigate() {
  return !dirty || window.confirm("You have unsaved changes. Discard them?");
}
document.querySelectorAll("form").forEach((form) =>
  form.addEventListener("input", () => markDirty(form.id)),
);
$("editor-text").addEventListener("input", () => markDirty("editor"));
$("editor-path").addEventListener("input", () => markDirty("editor"));
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ---------------------------------------------------------------- shell

function showLogin(message = "") {
  $("loading").hidden = true;
  $("workspace").hidden = true;
  $("account").hidden = true;
  $("login").hidden = false;
  $("link-signin").hidden = mode !== "link";
  $("signin").hidden = mode === "link";
  $("login-note").textContent = message;
}
function heading(label, title, description) {
  $("scope-label").textContent = label;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
}
function showPanel(id) {
  document.querySelectorAll(".view").forEach((node) => {
    node.hidden = node.id !== id;
  });
  resetNotice();
}
function highlight() {
  document.querySelectorAll(".menu .nav").forEach((node) => {
    node.classList.toggle("active", !selected && node.dataset.view === view);
  });
  renderServers();
}
const LOADERS = {
  overview: loadOverview,
  settings: async () => fillPersonal(await api("personal")),
  ai: async () => fillConnection(await api(connectionApi())),
  files: loadFiles,
  schedules: loadSchedules,
};
async function openView(name, initial = false) {
  if (!initial && !canNavigate()) return;
  await LOADERS[name]();
  view = name;
  selected = null;
  heading(...VIEWS[name]);
  showPanel("view-" + name);
  highlight();
  checkpoint();
}
document.querySelectorAll(".menu .nav").forEach((node) =>
  node.addEventListener("click", () => action(() => openView(node.dataset.view), node)),
);

function applyPlan(data) {
  plan = data;
  const premium = data.plan === "premium";
  $("plan-pill").textContent = premium ? "Premium" : "Free";
  $("plan-pill").classList.toggle("premium", premium);
  $("files-badge").hidden = premium;
  $("schedules-badge").hidden = premium;
}

// ---------------------------------------------------------------- overview

async function loadOverview() {
  const [data, listing] = await Promise.all([api("plan"), api("files")]);
  applyPlan(data);
  files = listing;
  const premium = data.plan === "premium";
  $("plan-name").textContent = premium ? "Premium" : "Free";
  $("plan-note").textContent = premium
    ? "Everything below is unlocked for you."
    : "Chat, tools, the shell and your reply settings are all yours. Premium extras are granted by the bot operator.";
  document.querySelector(".plan-card").classList.toggle("premium", premium);
  $("feature-list").replaceChildren(
    ...data.features.map((row) => {
      const item = el("li", row.on ? "on" : "", row.label);
      return item;
    }),
  );

  const messages = data.messages;
  // An API process that predates the percent-only payload still sends used/limit.
  // Convert it here so the raw numbers never hit the page. Delete after the restart.
  if (messages.percent_used === undefined) {
    messages.unlimited = !messages.enabled || messages.exempt || !(messages.limit > 0);
    messages.percent_used = messages.unlimited ? 0 : Math.round((messages.used * 100) / messages.limit);
  }
  if (messages.unlimited) {
    $("msg-value").textContent = "Unlimited";
    meter($("msg-bar"), 0, 0);
    $("msg-note").textContent = messages.exempt
      ? "No message limit applies to you."
      : "This bot has no message limit right now.";
  } else {
    // The API only hands out a percentage; the real allowance never reaches the browser.
    const percent = Math.max(0, messages.percent_used);
    $("msg-value").textContent = percent + "% used";
    meter($("msg-bar"), percent, 100);
    $("msg-note").textContent = messages.resets_in > 0
      ? Math.max(0, 100 - percent) + "% left. Frees up in " + span(messages.resets_in) + "."
      : "Nothing used yet.";
  }

  const storage = data.storage;
  if (!storage.enabled) {
    $("store-value").textContent = "Off";
    meter($("store-bar"), 0, 0);
    $("store-note").textContent = "The operator turned persistent storage off.";
  } else if (storage.quota_bytes <= 0) {
    $("store-value").textContent = storage.files ? bytes(storage.used_bytes) : "Premium";
    meter($("store-bar"), 0, 0);
    $("store-note").textContent = storage.files
      ? "Kept read-only from an earlier plan. You can download or delete it."
      : "Premium keeps builds in /storage so the 10-minute sandbox wipe can’t eat them.";
  } else {
    $("store-value").textContent = bytes(storage.used_bytes) + " / " + bytes(storage.quota_bytes);
    meter($("store-bar"), storage.used_bytes, storage.quota_bytes);
    $("store-note").textContent = storage.files + (storage.files === 1 ? " file" : " files") + " kept across sandbox wipes.";
  }

  const root = listing.public_root;
  $("site-value").replaceChildren();
  if (root && premium) {
    const anchor = el("a", "", root.replace(/^https?:\/\//, ""));
    anchor.href = root;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    $("site-value").append(anchor);
  } else {
    $("site-value").textContent = premium ? "Pick an address in Files" : "Premium";
  }
}

// ---------------------------------------------------------------- personal settings

function fillPersonal(data) {
  $("context-count").replaceChildren(
    ...data.context_counts.map((count) =>
      option(String(count), count === 0 ? "None — just my request" : "Last " + count + " messages"),
    ),
  );
  for (const [key, value] of Object.entries(data.defaults)) {
    const input = field(personalForm, key);
    if (input) input.value = String(value);
  }
  field(personalForm, "personality").value = data.personality;
}
personalForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const defaults = {};
    for (const key of ["language", "detail", "visibility", "mode", "web", "context", "tracking"])
      defaults[key] =
        key === "context" ? Number(field(personalForm, key).value) : field(personalForm, key).value.trim();
    fillPersonal(
      await api("personal", "PUT", { defaults, personality: field(personalForm, "personality").value }),
    );
    checkpoint(personalForm.id);
    notice("Saved. Your personal settings are now shared with Discord.");
  }, event.submitter);
});
$("privacy-review").addEventListener("click", (event) =>
  action(async () => {
    const data = await api("privacy");
    notice(
      data.processing + " Choices: " +
        (data.consents.map((c) => c.kind + ": " + c.status).join(", ") || "none"),
    );
  }, event.currentTarget),
);
$("privacy-delete").addEventListener("click", (event) =>
  action(async () => {
    if (
      !window.confirm(
        "Delete supported personal data, including your /storage files and schedules, and turn history off? Provider copies and backups need operator review. You will be signed out.",
      )
    )
      return;
    const result = await api("privacy", "DELETE");
    field(personalForm, "tracking").value = "off";
    notice(
      "History is off. " +
        (result.failed.length ? "Failed stores: " + result.failed.join(", ") + ". " : "Supported active stores processed. ") +
        result.manual_required.join(" ") + " " + result.retained,
    );
  }, event.currentTarget),
);

// ---------------------------------------------------------------- AI connection

function setSupport(settings = {}) {
  for (const key of ["vision", "audio", "tools", "reasoning"])
    field(connectionForm, key).checked = key === "tools" ? settings[key] !== false : settings[key] === true;
  field(connectionForm, "max_tokens").value = settings.max_tokens || 4096;
  field(connectionForm, "request_timeout").value = settings.timeout || 300;
  field(connectionForm, "turn_timeout").value = settings.turn_timeout || 900;
  field(connectionForm, "effort").value = settings.effort || "";
  field(connectionForm, "model_context").value = settings.context || "";
}
function parseConnection(status) {
  const values = {};
  (status?.modalities || "text, tools")
    .split(",")
    .map((s) => s.trim())
    .forEach((key) => {
      values[key] = true;
    });
  (status?.generation || "")
    .split(/\s+/)
    .filter(Boolean)
    .forEach((item) => {
      const [key, value] = item.split("=");
      values[key] =
        key === "reasoning"
          ? value === "on"
          : ["max_tokens", "context", "timeout", "turn_timeout"].includes(key)
            ? Number(value)
            : value;
    });
  return values;
}
function providerChanged() {
  const provider = field(connectionForm, "provider").value,
    same = connection?.provider === provider;
  field(connectionForm, "api_key").value = "";
  field(connectionForm, "api_key").required = !same;
  field(connectionForm, "api_key").placeholder = same
    ? "Leave blank to keep your saved key"
    : "Paste your provider’s API key";
  field(connectionForm, "model").value = same ? connection.model : providers[provider]?.suggested_model || "";
  $("custom-endpoint").hidden = provider !== "custom";
  field(connectionForm, "base_url").required = provider === "custom";
  field(connectionForm, "base_url").value = same && provider === "custom" ? connection.base_url : "";
  setSupport(same ? parseConnection(connection) : {});
  connectionDirty = false;
  $("provider-help").replaceChildren();
  $("test-connection").hidden = !same;
  $("test-connection").disabled = !same;
}
function connectionApi() {
  return connectionScope === "worker" ? "worker-connection" : "connection";
}
function fillConnection(data) {
  connection = data.connection;
  providers = data.providers;
  $("provider").value = "custom";
  providerChanged();
  $("connection-status").textContent = data.unreadable
    ? "Your saved connection could not be read. Remove it and reconnect, or ask the operator."
    : connection
      ? "Connected to " + connection.provider_label + " · " + connection.model + " · key " + connection.masked_key
      : connectionScope === "worker"
        ? "Inheriting your chat connection or the operator’s worker default."
        : "Using Maxwell’s included AI. No setup needed.";
  if (!data.enabled)
    $("connection-status").textContent =
      "Personal AI connections are unavailable on this bot. Ask the operator to enable them.";
  for (const input of connectionForm.elements) input.disabled = !data.enabled || data.unreadable;
  $("remove-connection").hidden = !data.present;
  $("remove-connection").disabled = false;
  $("remove-confirm").hidden = true;
}
connectionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const provider = field(connectionForm, "provider").value;
    const body = {
      provider,
      model: field(connectionForm, "model").value.trim(),
      api_key: field(connectionForm, "api_key").value.trim(),
    };
    if (provider === "custom") body.base_url = field(connectionForm, "base_url").value.trim();
    if (connectionDirty) {
      body.modalities = [
        "text",
        ...["vision", "audio", "tools"].filter((key) => field(connectionForm, key).checked),
      ].join(", ");
      body.generation =
        "reasoning=" + (field(connectionForm, "reasoning").checked ? "on" : "off") +
        " max_tokens=" + field(connectionForm, "max_tokens").value +
        " timeout=" + field(connectionForm, "request_timeout").value +
        " turn_timeout=" + field(connectionForm, "turn_timeout").value;
      if (field(connectionForm, "effort").value) body.generation += " effort=" + field(connectionForm, "effort").value;
      if (field(connectionForm, "model_context").value)
        body.generation += " context=" + field(connectionForm, "model_context").value;
    }
    fillConnection(await api(connectionApi(), "PUT", body));
    checkpoint(connectionForm.id);
    notice("AI connection saved. Test it to check the key and model.");
  }, event.submitter);
});
$("connection-scope").addEventListener("change", (event) => {
  const next = event.target.value;
  event.target.value = connectionScope;
  if (busy || (dirtyForms.has(connectionForm.id) && !window.confirm("Discard unsaved AI connection changes?")))
    return;
  action(async () => {
    const data = await api(next === "worker" ? "worker-connection" : "connection");
    connectionScope = next;
    event.target.value = next;
    fillConnection(data); // Also wipes the key box before you edit the other slot.
    checkpoint(connectionForm.id);
  });
});
$("connection-advanced").addEventListener("input", () => {
  connectionDirty = true;
});
$("remove-connection").addEventListener("click", () => {
  $("remove-confirm").hidden = false;
});
$("cancel-remove").addEventListener("click", () => {
  $("remove-confirm").hidden = true;
});
$("confirm-remove").addEventListener("click", (event) =>
  action(async () => {
    fillConnection(await api(connectionApi(), "DELETE"));
    checkpoint(connectionForm.id);
    notice(
      connectionScope === "worker"
        ? "Subagent connection removed. Workers inherit your chat connection or the operator default."
        : "Connection removed. You are using Maxwell’s included AI.",
    );
  }, event.currentTarget),
);
$("test-connection").addEventListener("click", (event) =>
  action(async () => {
    const result = await api(connectionApi() + "/test", "POST", {});
    notice(result.message);
  }, event.currentTarget),
);

// ---------------------------------------------------------------- files

async function loadFiles() {
  renderFiles(await api("files"));
}
function hasFolder(path) {
  return !path || files.entries.some((row) => row.type === "dir" && row.path === path);
}
function renderFiles(listing) {
  files = listing;
  const status = listing.status;
  const writable = listing.enabled && status.quota_bytes > 0;
  const locked = !writable && !status.files;
  $("files-locked").hidden = writable;
  $("files-locked").querySelector("h2").textContent = listing.enabled
    ? status.files
      ? "Your files are kept read-only"
      : "Persistent storage is part of Premium"
    : "Persistent storage is turned off on this bot";
  $("files-console").hidden = locked;
  if (locked) return;
  for (const id of ["new-folder", "new-file"]) $(id).disabled = !writable;
  $("upload-input").disabled = !writable;
  document.querySelector(".upload-button").hidden = !writable;
  handleForm.hidden = !writable;

  const link = $("public-link");
  if (listing.public_root) {
    link.href = listing.public_root;
    link.textContent = listing.public_root;
  } else {
    link.removeAttribute("href");
    link.textContent = writable ? "Choose an address to publish public/" : "No public address";
  }
  if (!dirtyForms.has(handleForm.id)) field(handleForm, "handle").value = status.handle || "";

  if (!hasFolder(cwd)) cwd = "";
  $("files-usage").textContent =
    bytes(status.used_bytes) + (status.quota_bytes > 0 ? " of " + bytes(status.quota_bytes) : "") +
    " · " + status.files + (status.files === 1 ? " file" : " files");
  meter($("files-bar"), status.used_bytes, status.quota_bytes);
  renderCrumbs();
  renderRows();
}
function renderCrumbs() {
  const crumbs = [button("storage", "", () => goTo(""))];
  let path = "";
  for (const part of cwd ? cwd.split("/") : []) {
    path = join(path, part);
    const target = path;
    crumbs.push(el("span", "sep", "/"), button(part, "", () => goTo(target)));
  }
  $("crumbs").replaceChildren(...crumbs);
}
async function goTo(path) {
  if (dirtyForms.has("editor") && !window.confirm("Discard unsaved file changes?")) return;
  closeEditor();
  cwd = path;
  renderCrumbs();
  renderRows();
}
function renderRows() {
  const rows = files.entries
    .filter((row) => parent(row.path) === cwd)
    .sort((a, b) => (a.type === b.type ? base(a.path).localeCompare(base(b.path)) : a.type === "dir" ? -1 : 1));
  $("file-rows").replaceChildren(...rows.map(fileRow));
  $("files-empty").hidden = rows.length > 0;
  $("files-empty").textContent =
    !cwd && !rows.length
      ? "Nothing stored yet. Upload here, or ask Maxwell to save a build to /storage. Put pages in public/ to host them."
      : "This folder is empty. Drop files here or ask Maxwell to save something.";
}
function fileRow(row) {
  const name = base(row.path),
    dir = row.type === "dir",
    writable = files.enabled && files.status.quota_bytes > 0;
  const tr = el("tr");
  const nameCell = el("td"),
    wrap = el("div", "fname");
  wrap.append(el("span", "ficon" + (dir ? " dir" : WEB_FILE.test(name) ? " web" : ""), dir ? "DIR" : (name.split(".").pop() || "").slice(0, 4).toUpperCase()));
  const open = el("button", "", name + (dir ? "/" : ""));
  open.type = "button";
  open.addEventListener("click", () =>
    action(() => (dir ? goTo(row.path) : TEXT_FILE.test(name) && row.size <= 512 * 1024 ? editFile(row.path) : download(row.path)), open),
  );
  wrap.append(open);
  if (row.public_url) wrap.append(el("span", "public-tag", "PUBLIC"));
  nameCell.append(wrap);
  const size = el("td", "num", dir ? "—" : bytes(row.size));
  const when = el("td", "when", moment(row.mtime));
  const actions = el("td", "act");
  if (row.public_url) {
    const visit = el("a", "mini secondary", "Open");
    visit.href = dir && !row.public_url.endsWith("/") ? row.public_url + "/" : row.public_url;
    visit.target = "_blank";
    visit.rel = "noopener noreferrer";
    actions.append(visit);
  }
  if (!dir) {
    actions.append(button("Link", "mini secondary", () => shareLink(row.path)));
    actions.append(button("Download", "mini secondary", () => download(row.path)));
  }
  if (writable) actions.append(button("Rename", "mini quiet", () => rename(row.path)));
  actions.append(button("Delete", "mini danger", () => remove(row.path, dir)));
  tr.append(nameCell, size, when, actions);
  return tr;
}
async function shareLink(path) {
  const { url } = await api("files/link", "POST", { path });
  try {
    await navigator.clipboard.writeText(url);
    notice("Link copied. Private links expire after an hour; public/ links don’t.", false, url);
  } catch {
    notice("Here’s your link. Private links expire after an hour; public/ links don’t.", false, url);
  }
}
async function download(path) {
  const { url } = await api("files/link", "POST", { path, download: true });
  // Same-origin + download attribute = a real download, no navigation,
  // no popup blocker, and the in-memory session survives.
  const anchor = el("a");
  anchor.href = url;
  anchor.download = base(path);
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}
async function rename(path) {
  const next = window.prompt("New path inside /storage (move it by changing folders):", path);
  if (!next || next.trim() === path) return;
  renderFiles(await api("files/move", "POST", { src: path, dst: next.trim().replace(/^\/+/, "") }));
  notice("Moved to " + next.trim() + ".");
}
async function remove(path, dir) {
  if (!window.confirm((dir ? "Delete the folder " + path + " and everything in it?" : "Delete " + path + "?") + " This cannot be undone."))
    return;
  renderFiles(await api("files?path=" + encodeURIComponent(path), "DELETE"));
  if (editing === path) closeEditor();
  notice("Deleted " + path + ".");
}
async function uploadAll(list) {
  const chosen = Array.from(list || []);
  if (!chosen.length) return;
  const limit = files.upload_limit;
  for (const [index, file] of chosen.entries()) {
    if (file.size > limit) throw new Error(file.name + " is larger than " + bytes(limit) + ". Ask Maxwell to save big builds from the shell instead.");
    notice("Uploading " + (index + 1) + " of " + chosen.length + ": " + file.name + "…");
    renderFiles(await api("files/upload?path=" + encodeURIComponent(join(cwd, file.name)), "POST", file, true));
  }
  notice(chosen.length === 1 ? "Uploaded " + chosen[0].name + "." : "Uploaded " + chosen.length + " files.");
}
$("upload-input").addEventListener("change", (event) => {
  const input = event.currentTarget;
  action(async () => {
    try {
      await uploadAll(input.files);
    } finally {
      input.value = "";
    }
  });
});
const dropzone = $("dropzone");
dropzone.addEventListener("dragover", (event) => {
  if (!files || !(files.status.quota_bytes > 0)) return;
  event.preventDefault();
  dropzone.classList.add("over");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("over");
  action(() => uploadAll(event.dataTransfer?.files));
});
$("new-folder").addEventListener("click", (event) =>
  action(async () => {
    const name = window.prompt("Folder name:", cwd ? "" : "public");
    if (!name || !name.trim()) return;
    const path = join(cwd, name.trim().replace(/^\/+|\/+$/g, ""));
    renderFiles(await api("files/mkdir", "POST", { path }));
    cwd = path;
    renderCrumbs();
    renderRows();
  }, event.currentTarget),
);
function openEditor(path, text, existing) {
  editing = existing ? path : null;
  $("editor").hidden = false;
  $("editor-title").textContent = existing ? "Edit " + base(path) : "New file";
  $("editor-path").value = path;
  $("editor-text").value = text;
  checkpoint("editor");
  $("editor").scrollIntoView({ behavior: "smooth", block: "start" });
  $(existing ? "editor-text" : "editor-path").focus();
}
function closeEditor() {
  editing = null;
  $("editor").hidden = true;
  checkpoint("editor");
}
async function editFile(path) {
  if (dirtyForms.has("editor") && !window.confirm("Discard unsaved file changes?")) return;
  const data = await api("files/text?path=" + encodeURIComponent(path));
  openEditor(path, data.text, true);
}
$("new-file").addEventListener("click", () => {
  if (dirtyForms.has("editor") && !window.confirm("Discard unsaved file changes?")) return;
  openEditor(join(cwd || "public", "index.html"), "", false);
});
$("editor-cancel").addEventListener("click", () => {
  if (dirtyForms.has("editor") && !window.confirm("Discard unsaved file changes?")) return;
  closeEditor();
});
$("editor-save").addEventListener("click", (event) =>
  action(async () => {
    const path = $("editor-path").value.trim().replace(/^\/+/, "");
    if (!path) throw new Error("Give the file a path, like public/index.html.");
    const blob = new Blob([$("editor-text").value], { type: "text/plain" });
    renderFiles(await api("files/upload?path=" + encodeURIComponent(path), "POST", blob, true));
    editing = path;
    $("editor-title").textContent = "Edit " + base(path);
    checkpoint("editor");
    const row = files.entries.find((entry) => entry.path === path);
    notice("Saved " + path + ".", false, row?.public_url || "");
  }, event.currentTarget),
);
handleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    checkpoint(handleForm.id);
    renderFiles(await api("files/handle", "PUT", { handle: field(handleForm, "handle").value.trim().toLowerCase() }));
    notice("Your public folder now lives at", false, files.public_root);
  }, event.submitter);
});

// ---------------------------------------------------------------- schedules

const localZone = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
})();
try {
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  $("tz-list").replaceChildren(...["UTC", ...zones].map((zone) => option(zone, zone)));
} catch {
  // Old browser; typing the zone still works.
}
async function loadSchedules() {
  renderSchedules(await api("schedules"));
}
function renderSchedules(data) {
  schedules = data;
  const allowed = data.premium && data.enabled;
  $("schedules-locked").hidden = data.premium && data.enabled;
  $("schedules-locked").querySelector("h2").textContent = data.enabled
    ? "Scheduled wake-ups are part of Premium"
    : "Schedules are turned off on this bot";
  $("schedule-new").disabled = !allowed || data.schedules.length >= data.limits.max;
  $("schedule-limits").textContent =
    data.schedules.length + " of " + data.limits.max + " schedules, at least " + data.limits.min_minutes +
    " minutes apart, up to " + data.limits.daily_runs + " runs a day.";
  $("cron-floor").textContent = "Runs at most every " + data.limits.min_minutes + " minutes.";
  $("schedule-list").replaceChildren(...data.schedules.map(scheduleCard));
  $("schedule-empty").hidden = data.schedules.length > 0;
  const names = Object.fromEntries(data.schedules.map((row) => [row.id, row.name]));
  $("history").replaceChildren(
    ...data.history.map((run) => {
      const item = el("li");
      item.append(el("span", "", moment(run.at)), el("span", "", names[run.schedule_id] || "Deleted schedule"), el("span", "", run.status || ""));
      return item;
    }),
  );
  $("history-card").hidden = !data.history.length;
}
function scheduleCard(row) {
  const card = el("article", "schedule" + (row.enabled ? "" : " off"));
  const body = el("div");
  const title = el("h3", "", row.name);
  title.append(el("span", "state" + (row.enabled ? " on" : ""), row.enabled ? "Active" : "Paused"));
  const once = row.kind === "once" ? zonedLocal(row.spec, row.tz).replace("T", " ") : "";
  body.append(title, el("p", "when", once ? "Once, " + once + " (" + row.tz + ")" : row.summary));
  body.append(el("p", "prompt muted", row.prompt.length > 220 ? row.prompt.slice(0, 220) + "…" : row.prompt));
  const meta = [
    row.enabled && row.next_run ? "Next (your time): " + moment(row.next_run) : "Not scheduled",
    row.last_run ? "Last: " + moment(row.last_run) + (row.last_status ? " (" + row.last_status + ")" : "") : row.last_status || "",
    row.runs ? row.runs + (row.runs === 1 ? " run" : " runs") : "",
    row.deliver === "dm" ? "Reports by DM" : "Reports in its channel",
  ].filter(Boolean);
  body.append(el("p", "meta muted", meta.join(" · ")));
  const controls = el("div", "controls");
  const allowed = schedules.premium && schedules.enabled;
  if (row.enabled || allowed)
    controls.append(
      button(row.enabled ? "Pause" : "Resume", "mini secondary", async () => {
        renderSchedules(await api("schedules/" + row.id, "PUT", { enabled: !row.enabled }));
        notice(row.enabled ? "Paused “" + row.name + "”." : "Resumed “" + row.name + "”.");
      }),
    );
  if (allowed) controls.append(button("Edit", "mini secondary", () => editSchedule(row)));
  controls.append(
    button("Delete", "mini danger", async () => {
      if (!window.confirm("Delete the schedule “" + row.name + "”?")) return;
      renderSchedules(await api("schedules/" + row.id, "DELETE"));
      notice("Deleted “" + row.name + "”.");
    }),
  );
  card.append(body, controls);
  return card;
}
function zonedLocal(iso, zone) {
  // datetime-local wants wall-clock time in the schedule's zone, not yours.
  try {
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-CA", {
        timeZone: zone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(new Date(iso))
        .map((part) => [part.type, part.value]),
    );
    return parts.year + "-" + parts.month + "-" + parts.day + "T" + parts.hour + ":" + parts.minute;
  } catch {
    return "";
  }
}
function setKind(kind) {
  for (const input of scheduleForm.querySelectorAll('input[name="kind"]')) input.checked = input.value === kind;
  $("cron-fields").hidden = kind !== "cron";
  $("once-fields").hidden = kind !== "once";
  field(scheduleForm, "at").required = kind === "once";
  field(scheduleForm, "cron").required = kind === "cron";
}
function syncPreset() {
  const value = field(scheduleForm, "cron").value.trim();
  const select = field(scheduleForm, "preset");
  select.value = Array.from(select.options).some((opt) => opt.value === value) ? value : "custom";
}
function openScheduleForm(row = null) {
  scheduleForm.reset();
  scheduleForm.hidden = false;
  $("schedule-form-title").textContent = row ? "Edit “" + row.name + "”" : "New schedule";
  field(scheduleForm, "id").value = row ? row.id : "";
  field(scheduleForm, "name").value = row ? row.name : "";
  field(scheduleForm, "timezone").value = row ? row.tz : localZone;
  field(scheduleForm, "prompt").value = row ? row.prompt : "";
  setKind(row ? row.kind : "cron");
  field(scheduleForm, "cron").value = row && row.kind === "cron" ? row.spec : "0 9 * * *";
  field(scheduleForm, "at").value = row && row.kind === "once" ? zonedLocal(row.spec, row.tz) : "";
  syncPreset();
  checkpoint(scheduleForm.id);
  scheduleForm.scrollIntoView({ behavior: "smooth", block: "start" });
  field(scheduleForm, row ? "prompt" : "name").focus();
}
function closeScheduleForm() {
  scheduleForm.hidden = true;
  checkpoint(scheduleForm.id);
}
function editSchedule(row) {
  if (dirtyForms.has(scheduleForm.id) && !window.confirm("Discard unsaved schedule changes?")) return;
  openScheduleForm(row);
}
$("schedule-new").addEventListener("click", () => {
  if (dirtyForms.has(scheduleForm.id) && !window.confirm("Discard unsaved schedule changes?")) return;
  openScheduleForm();
});
$("schedule-cancel").addEventListener("click", () => {
  if (dirtyForms.has(scheduleForm.id) && !window.confirm("Discard unsaved schedule changes?")) return;
  closeScheduleForm();
});
scheduleForm.querySelectorAll('input[name="kind"]').forEach((input) =>
  input.addEventListener("change", () => setKind(input.value)),
);
field(scheduleForm, "preset").addEventListener("change", (event) => {
  if (event.target.value !== "custom") field(scheduleForm, "cron").value = event.target.value;
  else field(scheduleForm, "cron").focus();
});
field(scheduleForm, "cron").addEventListener("input", syncPreset);
scheduleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const id = field(scheduleForm, "id").value;
    const kind = scheduleForm.querySelector('input[name="kind"]:checked').value;
    const body = {
      name: field(scheduleForm, "name").value.trim(),
      prompt: field(scheduleForm, "prompt").value.trim(),
      timezone: field(scheduleForm, "timezone").value.trim() || "UTC",
    };
    if (kind === "cron") body.cron = field(scheduleForm, "cron").value.trim();
    else body.at = field(scheduleForm, "at").value;
    renderSchedules(await api(id ? "schedules/" + id : "schedules", id ? "PUT" : "POST", body));
    closeScheduleForm();
    notice(id ? "Schedule updated." : "Schedule saved. Maxwell will DM you the results.");
  }, event.submitter);
});

// ---------------------------------------------------------------- servers

function renderServers() {
  const query = $("server-search").value.toLowerCase();
  $("server-list").replaceChildren(
    ...servers
      .filter((server) => server.name.toLowerCase().includes(query))
      .map((server) => {
        const node = el("button", "nav");
        node.type = "button";
        node.classList.toggle("active", selected === server.id);
        const text = el("div", "", server.name);
        if (!server.installed) text.append(el("small", "", "Add Maxwell"));
        node.append(text);
        node.addEventListener("click", () => action(() => openServer(server), node));
        return node;
      }),
  );
  $("server-note").textContent = servers.length
    ? ""
    : mode === "link"
      ? "No servers you manage were found where Maxwell is installed. Re-run /dashboard after joining one."
      : "No servers you manage were found. You can still configure your account.";
}
async function loadServers() {
  const data = await api("servers");
  servers = data.servers;
  clientId = data.client_id;
  renderServers();
}
function checkItem(container, key, label, checked, group) {
  const node = el("label"),
    input = el("input");
  input.type = "checkbox";
  input.value = key;
  input.checked = checked;
  input.dataset.group = group;
  node.append(input, document.createTextNode(label));
  container.append(node);
}
function fillServer(data) {
  serverData = data;
  $("channels").replaceChildren(option("", "All channels"), ...data.channels.map((row) => option(row.id, "#" + row.name)));
  if (data.settings.channel && !data.channels.some((row) => row.id === data.settings.channel))
    $("channels").append(option(data.settings.channel, "Previously selected channel (unavailable)"));
  field(serverForm, "channel").value = data.settings.channel;
  field(serverForm, "progress").value = data.settings.progress;
  field(serverForm, "ticket").value = data.settings.ticket ? "on" : "off";
  $("capability-list").replaceChildren();
  for (const [key, label] of Object.entries(data.capabilities))
    checkItem($("capability-list"), key, label, data.settings.capabilities.includes(key), "capability");
  $("plugin-list").replaceChildren();
  data.plugins.forEach((row) =>
    checkItem($("plugin-list"), row.id, row.name, data.settings.plugins[row.id] ?? row.default, "plugin"),
  );
  if (!data.plugins.length) $("plugin-list").textContent = "No optional plugins are installed.";
}
async function openServer(server) {
  if (!canNavigate()) return;
  let data;
  if (server.installed) data = await api("servers/" + server.id);
  selected = server.id;
  heading("This server", server.name, "Manage where Maxwell replies and which tools are available.");
  highlight();
  if (!server.installed) {
    showPanel("view-install");
    $("invite").href =
      "https://discord.com/oauth2/authorize?" +
      new URLSearchParams({
        client_id: clientId,
        scope: "bot applications.commands",
        guild_id: server.id,
        disable_guild_select: "true",
      });
  } else {
    showPanel("view-server");
    fillServer(data);
  }
  checkpoint();
}
serverForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const plugins = {};
    $("plugin-list")
      .querySelectorAll("input")
      .forEach((input) => {
        const row = serverData.plugins.find((p) => p.id === input.value),
          previous = serverData.settings.plugins[input.value] ?? row.default;
        if (previous !== input.checked) plugins[input.value] = input.checked;
      });
    const changes = {
      channel: field(serverForm, "channel").value,
      progress: field(serverForm, "progress").value,
      ticket: field(serverForm, "ticket").value === "on",
      capabilities: Array.from($("capability-list").querySelectorAll("input:checked"), (input) => input.value),
    };
    if (Object.keys(plugins).length)
      changes.plugins = {
        ...Object.fromEntries(
          Object.entries(serverData.settings.plugins).filter(([key]) => serverData.plugins.some((row) => row.id === key)),
        ),
        ...plugins,
      };
    fillServer(await api("servers/" + selected, "PUT", changes));
    checkpoint(serverForm.id);
    notice("Server settings saved. Maxwell picks them up within a few seconds.");
  }, event.submitter);
});
$("server-search").addEventListener("input", renderServers);
$("refresh-servers").addEventListener("click", (event) =>
  action(async () => {
    await loadServers();
    notice(mode === "link" ? "Server list refreshed. New servers show up after a fresh /dashboard link." : "Server list refreshed.");
  }, event.currentTarget),
);

// ---------------------------------------------------------------- session

$("logout").addEventListener("click", (event) =>
  action(async () => {
    if (!canNavigate()) return;
    await api("logout", "POST", {});
    bearer = "";
    csrf = "";
    checkpoint();
    showLogin(mode === "link" ? "Signed out. Run /dashboard in Discord to come back." : "");
  }, event.currentTarget),
);

async function enter() {
  $("username").textContent = user.name;
  $("account").hidden = false;
  $("login").hidden = true;
  await openView("overview", true);
  $("loading").hidden = true;
  $("workspace").hidden = false;
  try {
    await loadServers();
  } catch (error) {
    $("server-note").textContent = error.message;
  }
}

async function start() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const link = fragment.get("t") || "";
  // Get the one-time token out of the address bar and history right away.
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  try {
    const data = await api("session");
    if (!data.configured) {
      mode = "off";
      showLogin("The web dashboard is not enabled on this bot. Use /config in Discord.");
      $("link-signin").hidden = true;
      $("signin").hidden = true;
      return;
    }
    mode = data.mode || "oauth";
    if (mode === "link") {
      if (!link) return showLogin("");
      const session = await api("redeem", "POST", { token: link });
      bearer = session.token;
      user = session.user;
    } else {
      if (!data.user)
        return showLogin(
          new URLSearchParams(location.search).has("login")
            ? "Sign-in did not finish. Try again, or ask the operator to check the dashboard setup."
            : "",
        );
      csrf = data.csrf;
      user = data.user;
    }
    await enter();
  } catch (error) {
    showLogin(error.message);
  }
}
// A fresh link pasted into this tab only changes the fragment, which never
// reloads the page. Reload on purpose so start() sees the new token.
window.addEventListener("hashchange", () => {
  if (!new URLSearchParams(location.hash.slice(1)).get("t")) return;
  if (dirty && !window.confirm("Open the new sign-in link and discard unsaved changes?")) return;
  dirty = false;
  location.reload();
});
start();
