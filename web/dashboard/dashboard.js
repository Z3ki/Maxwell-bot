"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "",
  servers = [],
  clientId = "",
  selected = null,
  connection = null,
  providers = {},
  serverData = null;
let dirty = false,
  busy = false,
  connectionDirty = false;
const dirtyForms = new Set();
if (window.matchMedia("(max-width:620px)").matches)
  $("server-picker").open = false;
const personalForm = $("personal-form"),
  connectionForm = $("connection-form"),
  serverForm = $("server-form");
const field = (form, name) => form.elements.namedItem(name);
function notice(message, error = false) {
  $("notice").hidden = false;
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}
async function api(path, method = "GET", body) {
  const response = await fetch("/api/dashboard/" + path, {
    method,
    credentials: "same-origin",
    headers:
      method === "GET"
        ? {}
        : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("Could not reach Maxwell. Try again shortly.");
  }
  if (!response.ok) {
    if (response.status === 401) {
      dirty = false;
      showLogin("Your session expired. Sign in again to continue.");
    }
    throw new Error(result.error || "Could not complete this change.");
  }
  return result;
}
async function action(callback, button) {
  if (busy) return;
  busy = true;
  if (button) button.disabled = true;
  try {
    await callback();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    if (button) button.disabled = false;
  }
}
function showLogin(message = "") {
  $("loading").hidden = true;
  $("workspace").hidden = true;
  $("account").hidden = true;
  $("login").hidden = false;
  $("login-note").textContent = message;
}
function option(value, label) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  return el;
}
function checkpoint(form) {
  if (form) dirtyForms.delete(form.id);
  else dirtyForms.clear();
  dirty = dirtyForms.size > 0;
}
function canNavigate() {
  return !dirty || window.confirm("You have unsaved changes. Discard them?");
}
function resetNotice() {
  $("notice").hidden = true;
}
function scope(personal, name = "") {
  $("personal-content").hidden = !personal;
  $("server-content").hidden = personal;
  $("install-content").hidden = true;
  $("personal-nav").classList.toggle("active", personal);
  $("scope-label").textContent = personal
    ? "JUST FOR YOU"
    : "FOR EVERYONE IN THIS SERVER";
  $("page-title").textContent = personal ? "My account" : name;
  $("page-description").textContent = personal
    ? "Make Maxwell feel familiar. Your choices stay in sync with Discord."
    : "Manage where Maxwell replies and which tools are available.";
  resetNotice();
}
function renderServers() {
  const query = $("server-search").value.toLowerCase();
  $("server-list").replaceChildren();
  servers
    .filter((server) => server.name.toLowerCase().includes(query))
    .forEach((server) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "nav";
      button.classList.toggle("active", selected === server.id);
      const text = document.createElement("div");
      text.textContent = server.name;
      if (!server.installed) {
        const small = document.createElement("small");
        small.textContent = "Add Maxwell";
        text.append(small);
      }
      button.append(text);
      button.addEventListener("click", () =>
        action(() => openServer(server), button),
      );
      $("server-list").append(button);
    });
  $("server-note").textContent = servers.length
    ? ""
    : "No servers you manage were found. You can still configure your account.";
}
async function loadServers() {
  const data = await api("servers");
  servers = data.servers;
  clientId = data.client_id;
  renderServers();
}
function fillPersonal(data) {
  $("context-count").replaceChildren(
    ...data.context_counts.map((count) =>
      option(
        String(count),
        count === 0 ? "None — just my request" : "Last " + count + " messages",
      ),
    ),
  );
  for (const [key, value] of Object.entries(data.defaults))
    field(personalForm, key).value = String(value);
  field(personalForm, "personality").value = data.personality;
}
function setSupport(settings = {}) {
  for (const key of ["vision", "audio", "tools", "reasoning"])
    field(connectionForm, key).checked =
      key === "tools" ? settings[key] !== false : settings[key] === true;
  field(connectionForm, "max_tokens").value = settings.max_tokens || 4096;
  field(connectionForm, "temperature").value = settings.temperature ?? 0.4;
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
          : ["max_tokens", "temperature", "context"].includes(key)
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
  field(connectionForm, "model").value = same
    ? connection.model
    : providers[provider]?.suggested_model || "";
  $("custom-endpoint").hidden = provider !== "custom";
  $("endpoint-override").hidden = provider === "custom";
  field(connectionForm, "base_url").required = provider === "custom";
  field(connectionForm, "base_url").value =
    same && provider === "custom" ? connection.base_url : "";
  field(connectionForm, "advanced_url").value =
    same && connection.base_url !== providers[provider]?.base_url
      ? connection.base_url
      : "";
  setSupport(same ? parseConnection(connection) : {});
  connectionDirty = false;
  $("provider-help").replaceChildren();
  const keyUrls = {
    openai: "https://platform.openai.com/api-keys",
    openrouter: "https://openrouter.ai/settings/keys",
    groq: "https://console.groq.com/keys",
  };
  if (keyUrls[provider]) {
    const link = document.createElement("a");
    link.href = keyUrls[provider];
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent =
      "Get an API key from " + providers[provider].label + " ↗";
    $("provider-help").append(link);
  }
  $("test-connection").hidden = !same;
  $("test-connection").disabled = !same;
}
function fillConnection(data) {
  connection = data.connection;
  providers = data.providers;
  const selectedProvider =
    connection?.provider || field(connectionForm, "provider").value || "openai";
  $("provider").replaceChildren(
    ...Object.entries(providers).map(([key, item]) =>
      option(key, key === "custom" ? "Other provider" : item.label),
    ),
  );
  $("provider").value = selectedProvider;
  providerChanged();
  $("connection-status").textContent = data.unreadable
    ? "Your saved connection could not be read. Remove it and reconnect, or ask the operator."
    : connection
      ? "Connected to " +
        connection.provider_label +
        " · " +
        connection.model +
        " · key " +
        connection.masked_key
      : "Using Maxwell’s included AI. No setup needed.";
  if (!data.enabled)
    $("connection-status").textContent =
      "Personal AI connections are unavailable on this bot. Ask the operator to enable them.";
  for (const el of connectionForm.elements)
    el.disabled = !data.enabled || data.unreadable;
  $("remove-connection").hidden = !data.present;
  $("remove-connection").disabled = false;
  $("remove-confirm").hidden = true;
}
async function openPersonal(initial = false) {
  if (!initial && !canNavigate()) return;
  const [personal, ai] = await Promise.all([
    api("personal"),
    api("connection"),
  ]);
  selected = null;
  scope(true);
  fillPersonal(personal);
  fillConnection(ai);
  renderServers();
  checkpoint();
}
function checkItem(container, key, label, checked, group) {
  const el = document.createElement("label"),
    input = document.createElement("input");
  input.type = "checkbox";
  input.value = key;
  input.checked = checked;
  input.dataset.group = group;
  el.append(input, document.createTextNode(label));
  container.append(el);
}
function fillServer(data) {
  serverData = data;
  $("channels").replaceChildren(
    option("", "All channels"),
    ...data.channels.map((row) => option(row.id, "#" + row.name)),
  );
  if (
    data.settings.channel &&
    !data.channels.some((row) => row.id === data.settings.channel)
  )
    $("channels").append(
      option(
        data.settings.channel,
        "Previously selected channel (unavailable)",
      ),
    );
  field(serverForm, "channel").value = data.settings.channel;
  field(serverForm, "progress").value = data.settings.progress;
  field(serverForm, "ticket").value = data.settings.ticket ? "on" : "off";
  $("capability-list").replaceChildren();
  for (const [key, label] of Object.entries(data.capabilities))
    checkItem(
      $("capability-list"),
      key,
      label,
      data.settings.capabilities.includes(key),
      "capability",
    );
  $("plugin-list").replaceChildren();
  data.plugins.forEach((row) =>
    checkItem(
      $("plugin-list"),
      row.id,
      row.name,
      data.settings.plugins[row.id] ?? row.default,
      "plugin",
    ),
  );
  if (!data.plugins.length)
    $("plugin-list").textContent = "No optional plugins are installed.";
}
async function openServer(server) {
  if (!canNavigate()) return;
  let data;
  if (server.installed) data = await api("servers/" + server.id);
  selected = server.id;
  scope(false, server.name);
  renderServers();
  if (!server.installed) {
    $("server-content").hidden = true;
    $("install-content").hidden = false;
    $("invite").href =
      "https://discord.com/oauth2/authorize?" +
      new URLSearchParams({
        client_id: clientId,
        scope: "bot applications.commands",
        guild_id: server.id,
        disable_guild_select: "true",
      });
  } else fillServer(data);
  checkpoint();
}
personalForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const defaults = {};
    for (const key of [
      "language",
      "detail",
      "visibility",
      "mode",
      "web",
      "context",
    ])
      defaults[key] =
        key === "context"
          ? Number(field(personalForm, key).value)
          : field(personalForm, key).value.trim();
    fillPersonal(
      await api("personal", "PUT", {
        defaults,
        personality: field(personalForm, "personality").value,
      }),
    );
    checkpoint(personalForm);
    notice("Saved. Your personal settings are now shared with Discord.");
  }, event.submitter);
});
connectionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const provider = field(connectionForm, "provider").value;
    const body = {
      provider,
      model: field(connectionForm, "model").value.trim(),
      api_key: field(connectionForm, "api_key").value.trim(),
    };
    if (provider === "custom")
      body.base_url = field(connectionForm, "base_url").value.trim();
    else if (connectionDirty)
      body.base_url = field(connectionForm, "advanced_url").value.trim();
    if (connectionDirty) {
      body.modalities = [
        "text",
        ...["vision", "audio", "tools"].filter(
          (key) => field(connectionForm, key).checked,
        ),
      ].join(", ");
      body.generation =
        "reasoning=" +
        (field(connectionForm, "reasoning").checked ? "on" : "off") +
        " max_tokens=" +
        field(connectionForm, "max_tokens").value +
        " temperature=" +
        field(connectionForm, "temperature").value;
      if (field(connectionForm, "effort").value)
        body.generation += " effort=" + field(connectionForm, "effort").value;
      if (field(connectionForm, "model_context").value)
        body.generation +=
          " context=" + field(connectionForm, "model_context").value;
    }
    fillConnection(await api("connection", "PUT", body));
    checkpoint(connectionForm);
    notice("AI connection saved. Test it to check the key and model.");
  }, event.submitter);
});
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
      capabilities: Array.from(
        $("capability-list").querySelectorAll("input:checked"),
        (input) => input.value,
      ),
    };
    if (Object.keys(plugins).length)
      changes.plugins = {
        ...Object.fromEntries(
          Object.entries(serverData.settings.plugins).filter(([key]) =>
            serverData.plugins.some((row) => row.id === key),
          ),
        ),
        ...plugins,
      };
    fillServer(await api("servers/" + selected, "PUT", changes));
    checkpoint(serverForm);
    notice(
      "Server settings saved. Maxwell will pick them up within a few seconds.",
    );
  }, event.submitter);
});
$("provider").addEventListener("change", providerChanged);
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
    fillConnection(await api("connection", "DELETE"));
    checkpoint(connectionForm);
    notice("Connection removed. You are using Maxwell’s included AI.");
  }, event.target),
);
$("test-connection").addEventListener("click", (event) =>
  action(async () => {
    const result = await api("connection/test", "POST", {});
    notice(result.message);
  }, event.target),
);
$("personal-nav").addEventListener("click", (event) =>
  action(() => openPersonal(), event.target),
);
$("server-search").addEventListener("input", renderServers);
$("refresh-servers").addEventListener("click", (event) =>
  action(async () => {
    await loadServers();
    notice("Server list refreshed.");
  }, event.target),
);
$("logout").addEventListener("click", (event) =>
  action(async () => {
    if (!canNavigate()) return;
    await api("logout", "POST", {});
    checkpoint();
    showLogin();
  }, event.target),
);
document.querySelectorAll("form").forEach((form) =>
  form.addEventListener("input", () => {
    dirtyForms.add(form.id);
    dirty = true;
  }),
);
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
async function start() {
  try {
    const data = await api("session");
    if (!data.configured) {
      showLogin(
        "The dashboard is not enabled yet. Use /config in Discord for now.",
      );
      $("signin").hidden = true;
      return;
    }
    if (!data.user) {
      showLogin(
        new URLSearchParams(location.search).has("login")
          ? "Sign-in did not finish. Try again, or ask the operator to check the dashboard setup."
          : "",
      );
      return;
    }
    csrf = data.csrf;
    $("username").textContent = data.user.name;
    $("account").hidden = false;
    await openPersonal(true);
    $("loading").hidden = true;
    $("workspace").hidden = false;
    try {
      await loadServers();
    } catch (error) {
      $("server-note").textContent = error.message;
    }
  } catch (error) {
    showLogin(error.message);
  }
}
start();
