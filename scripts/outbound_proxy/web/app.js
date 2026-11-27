"use strict";

const el = (id) => document.getElementById(id);
const localSession = document.body.dataset.localSession === "true";
let csrf = "";
let editing = null;
let busy = false;

function notice(text, failed = false) {
  el("notice").textContent = text;
  el("notice").classList.toggle("error", failed);
}

async function request(path, data) {
  const headers = {};
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const options = {method: "GET", headers, credentials: "same-origin", cache: "no-store"};
  if (data !== undefined) {
    headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (response.status === 403) {
    const wasSignedIn = Boolean(csrf);
    csrf = "";
    el("panel").hidden = true;
    el("login").hidden = localSession;
    if (localSession) throw new Error("Local session expired. Reload this page to reopen the panel.");
    el("secret").focus();
    throw new Error(wasSignedIn
      ? "Session expired. Reload this page to sign in with your paired browser."
      : "Pairing or sign-in failed. Obtain a fresh code from the operator terminal.");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function rowFor(entry, selected) {
  const row = document.createElement("tr");
  const name = document.createElement("th");
  name.scope = "row";
  name.textContent = entry.name;
  const host = entry.host.includes(":") ? "[" + entry.host + "]" : entry.host;
  const endpoint = document.createElement("td");
  endpoint.textContent = host + ":" + entry.port;
  const actions = document.createElement("td");
  function addButton(label, click) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("aria-label", label + " " + entry.name);
    button.addEventListener("click", click);
    actions.append(button);
  }
  addButton("Check", () => mutate({action: "check", name: entry.name}));
  if (!selected) addButton("Select", () => mutate({action: "use", name: entry.name}));
  addButton("Edit", () => openEditor(entry.name));
  if (!selected) addButton("Remove", () => {
    if (window.confirm("Remove upstream " + entry.name + "?")) {
      mutate({action: "remove", name: entry.name});
    }
  });
  row.append(name, endpoint, actions);
  return row;
}

function drawBridge(bridge) {
  el("bridge").hidden = !bridge;
  el("members").replaceChildren();
  if (!bridge) return;
  el("bridge-name").textContent = bridge.name;
  const messages = {
    missing: "Network has not been created.",
    unavailable: "Docker network information is unavailable.",
    conflict: "The existing network configuration conflicts with the dedicated bridge."
  };
  el("bridge-status").textContent = messages[bridge.status] || "";
  el("bridge-details").hidden = bridge.status !== "present";
  if (bridge.status !== "present") return;
  for (const field of ["interface", "link", "subnet", "gateway"]) {
    el("bridge-" + field).textContent = bridge[field];
  }
  const count = bridge.members.length;
  el("member-count").textContent = "Attached containers (" + count + ")";
  el("member-table").hidden = count === 0;
  for (const member of bridge.members) {
    const row = el("members").insertRow();
    row.insertCell().textContent = member.name;
    row.insertCell().textContent = member.ip || "Unassigned";
  }
}

function draw(state) {
  el("login").hidden = true;
  el("panel").hidden = false;
  el("running").textContent = state.running === null ? "Unknown" : state.running ? "Running" : "Stopped";
  el("service-dot").classList.toggle("online", state.running);
  el("enabled").textContent = state.enabled ? "Enabled" : "Disabled";
  el("boot").textContent = state.boot_enabled === null ? "Unknown" : state.boot_enabled ? "Enabled" : "Disabled";
  el("selected").textContent = state.selected || "None";
  const check = state.last_check;
  el("check").textContent = check ? [check.name, check.outcome, check.time].join(" · ") : "No check recorded";
  el("pending").hidden = !state.recovery_pending;
  el("enable").disabled = !state.selected || (state.running && state.enabled && state.boot_enabled);
  el("disable").disabled = state.running === false && !state.enabled && state.boot_enabled === false;
  el("port-form").hidden = Boolean(state.deployment);
  el("host-listeners").hidden = !state.deployment;
  if (state.deployment) el("endpoints").textContent = state.deployment.endpoints.join(" · ");
  el("port").value = state.port;
  el("failover").checked = state.failover;
  el("docker").checked = state.docker;
  el("docker").disabled = !state.docker_supported;
  el("docker-unavailable").hidden = state.docker_supported;
  if (state.deployment) {
    el("docker-unavailable").textContent = "Enable Docker clients in the deployment to use this listener.";
  }
  drawBridge(state.bridge);
  el("empty").hidden = state.upstreams.length !== 0;
  el("catalog").hidden = state.upstreams.length === 0;
  el("upstreams").replaceChildren(...state.upstreams.map(e => rowFor(e, e.name === state.selected)));
}

async function refresh() {
  draw(await request("/api/state"));
}

async function perform(work) {
  if (busy) return;
  busy = true;
  el("panel").disabled = true;
  el("panel").setAttribute("aria-busy", "true");
  notice("Working…");
  try {
    await work();
    notice("");
  } catch (error) {
    notice(error.message, true);
  } finally {
    el("panel").disabled = false;
    el("panel").setAttribute("aria-busy", "false");
    busy = false;
  }
}

async function mutate(action) {
  await perform(async () => {
    try {
      draw(await request("/api/action", action));
    } catch (failure) {
      if (csrf) {
        try {
          await refresh();
        } catch (refreshFailure) {
          throw new Error(failure.message + " Could not refresh status. " + refreshFailure.message);
        }
      }
      throw failure;
    }
  });
}

function openEditor(name) {
  editing = name;
  const update = name !== null;
  el("editor-title").textContent = update ? "Edit upstream" : "Add upstream";
  el("name").value = name || "";
  el("name").readOnly = update;
  el("uri").value = "";
  el("uri").required = !update;
  el("uri-help").textContent = update ? "Leave blank to keep the saved URI." : "Include the username, password and host. The HTTPS port defaults to 443.";
  el("editor").showModal();
  el(update ? "uri" : "name").focus();
}

function storedKey(candidate = null) {
  return new Promise((resolve, reject) => {
    const opening = indexedDB.open("outbound-proxy", 1);
    opening.onerror = () => reject(new Error("Browser key storage is unavailable."));
    opening.onupgradeneeded = () => opening.result.createObjectStore("keys");
    opening.onsuccess = () => {
      const db = opening.result;
      const tx = db.transaction("keys", candidate ? "readwrite" : "readonly");
      const store = tx.objectStore("keys");
      const get = store.get("signer");
      let keys;
      get.onsuccess = () => {
        keys = get.result || candidate;
        if (!get.result && candidate) store.put(candidate, "signer");
      };
      tx.onabort = () => {
        db.close();
        reject(new Error("The browser could not retain its signing key."));
      };
      tx.oncomplete = () => {
        db.close();
        resolve(keys);
      };
    };
  });
}

function base64(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}

// WebCrypto emits two fixed-width integers. OpenSSL reads the ASN.1 signature.
function derSignature(raw) {
  function integer(part) {
    let first = 0;
    while (first < part.length - 1 && part[first] === 0) first++;
    const bytes = Array.from(part.slice(first));
    if (bytes[0] & 128) bytes.unshift(0);
    return [2, bytes.length, ...bytes];
  }
  const bytes = new Uint8Array(raw);
  const values = [...integer(bytes.slice(0, 32)), ...integer(bytes.slice(32))];
  return new Uint8Array([48, values.length, ...values]);
}

async function signIn(keys, code) {
  const publicBytes = await crypto.subtle.exportKey("spki", keys.publicKey);
  const fingerprint = new Uint8Array(await crypto.subtle.digest("SHA-256", publicBytes));
  const id = Array.from(fingerprint, octet => octet.toString(16).padStart(2, "0")).join("");
  if (code) await request("/api/pair", {code, public: base64(publicBytes)});
  const {challenge} = await request("/api/challenge", {id});
  const raw = await crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, keys.privateKey,
                                       new TextEncoder().encode(challenge));
  const response = await request("/api/login", {id, challenge, signature: base64(derSignature(raw))});
  csrf = response.csrf;
  await refresh();
}

el("login-form").addEventListener("submit", event => {
  event.preventDefault();
  const code = el("secret").value.trim();
  el("secret").value = "";
  perform(async () => {
    let keys = await storedKey();
    if (!keys) {
      const candidate = await crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"},
                                                        false, ["sign", "verify"]);
      keys = await storedKey(candidate);
    }
    await signIn(keys, code);
  });
});

el("refresh").addEventListener("click", () => perform(refresh));
for (const action of ["enable", "disable"]) {
  el(action).addEventListener("click", () => mutate({action}));
}
for (const action of ["failover", "docker"]) {
  el(action).addEventListener("change", event => mutate({action, value: event.target.checked}));
}
el("port-form").addEventListener("submit", event => {
  event.preventDefault();
  mutate({action: "port", value: el("port").valueAsNumber});
});
el("add").addEventListener("click", () => openEditor(null));
el("cancel").addEventListener("click", () => el("editor").close());
el("editor").addEventListener("close", () => {
  el("uri").value = "";
  el("name").value = "";
});
el("editor-form").addEventListener("submit", event => {
  event.preventDefault();
  const action = {action: editing === null ? "add" : "update", name: el("name").value};
  if (el("uri").value) action.uri = el("uri").value;
  el("uri").value = "";
  el("editor").close();
  mutate(action);
});

async function reopen() {
  el("login-form").inert = true;
  try {
    if (localSession) {
      csrf = (await request("/api/session", {})).csrf;
      await refresh();
      return;
    }
    const keys = await storedKey();
    if (keys) await signIn(keys);
  } catch (error) {
    notice(error.message, true);
  } finally {
    el("login-form").inert = false;
  }
}
reopen();
