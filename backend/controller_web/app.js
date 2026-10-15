const statePill = document.querySelector("#controller-state");
const viewer = document.querySelector("#viewer");
const viewerPlaceholder = document.querySelector("#viewer-placeholder");
const viewerPlaceholderMessage = document.querySelector("#viewer-placeholder-message");
const restartAutoDiscoveryButton = document.querySelector("#restart-auto-discovery");
const stopButton = document.querySelector("#stop-session");
const applyButton = document.querySelector("#apply-settings");
const fullDiscoveryButton = document.querySelector("#launch-full-discovery");
const statusDot = document.querySelector(".status-dot");
const themeToggle = document.querySelector("#theme-toggle");
let viewerUrl = "";
let statusRefreshPromise = null;
let autoStartAttempted = false;
let latestStatus = null;
let availableInterfaces = [];
let sonarEnableFailed = false;
let defaultSaveRoot = "recordings";
let pendingLogText = null;
const exportDiagnostics = document.querySelector("#export-diagnostics");
if (window.sonarDesktop?.exportDiagnostics) {
  exportDiagnostics.hidden = false;
  exportDiagnostics.addEventListener("click", async () => {
    try { await window.sonarDesktop.exportDiagnostics(); }
    catch (error) { document.querySelector("#form-error").textContent = error.message; }
  });
}

function sendViewerTheme() {
  if (!viewerUrl || !viewer.contentWindow) return;
  viewer.contentWindow.postMessage(
    {type: "emulator-ui-theme", theme: document.documentElement.dataset.theme},
    new URL(viewerUrl).origin,
  );
}

function applyTheme(theme, persist = true) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  themeToggle.textContent = light ? "Dark mode" : "Light mode";
  themeToggle.setAttribute("aria-label", `Switch to ${light ? "dark" : "light"} mode`);
  if (persist) savePreferences({theme: light ? "light" : "dark"});
  sendViewerTheme();
}

applyTheme("dark", false);
themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});
viewer.addEventListener("load", sendViewerTheme);

window.addEventListener("message", event => {
  if (!viewerUrl || event.source !== viewer.contentWindow) return;
  if (event.origin !== new URL(viewerUrl).origin) return;
  if (event.data?.type === "viewer-range-status") {
    document.querySelector("#range-failure").hidden = event.data.failed !== true;
    return;
  }
  if (event.data?.type === "viewer-sonar-status") {
    const failed = event.data.failed === true;
    const newlyFailed = failed && !sonarEnableFailed;
    sonarEnableFailed = failed;
    updateAdapterSuggestion();
    if (newlyFailed) void loadInterfaces();
    return;
  }
  if (event.data?.type !== "viewer-height") return;
  const height = Number(event.data.height);
  if (Number.isFinite(height) && height >= 400 && height <= 10000) {
    viewer.style.height = `${Math.ceil(height)}px`;
  }
});

async function getJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

async function loadInterfaces() {
  const select = document.querySelector("#interface");
  const selected = select.value;
  try {
    const result = await getJson("/api/interfaces", {cache: "no-store"});
    availableInterfaces = result.interfaces;
    select.replaceChildren(new Option("Auto-detect", ""));
    result.interfaces.forEach(name => select.add(new Option(name, name)));
    select.value = selected;
    updateAdapterSuggestion();
    if (result.error) document.querySelector("#form-error").textContent = result.error;
  } catch (error) {
    document.querySelector("#form-error").textContent = error.message;
  }
}

function updateAdapterSuggestion() {
  const banner = document.querySelector("#adapter-suggestion");
  const selected = latestStatus?.diagnostics?.interface || document.querySelector("#interface").value;
  const alternatives = availableInterfaces.filter(name => name !== selected);
  const show = sonarEnableFailed && latestStatus?.running === true
    && latestStatus?.viewer_ready === true && Boolean(selected) && alternatives.length > 0;
  banner.hidden = !show;
  if (!show) return;
  const other = alternatives.length === 1 ? "another wired adapter" : "other wired adapters";
  document.querySelector("#adapter-suggestion-message").textContent =
    `Sonar failed to enable on ${selected}. Try ${other}: ${alternatives.join(", ")}. `
    + "Choose the adapter connected to your GLS10 under Advanced → Connection and logging, then apply and restart the session.";
}

document.querySelector("#choose-another-adapter").addEventListener("click", () => {
  const advanced = document.querySelector("#advanced-settings");
  advanced.open = true;
  advanced.scrollIntoView({behavior: "smooth", block: "start"});
  document.querySelector("#interface").focus({preventScroll: true});
});

async function loadDefaults() {
  try {
    const defaults = await getJson("/api/defaults", {cache: "no-store"});
    defaultSaveRoot = defaults.save_root;
    applyTheme(defaults.preferences?.theme || "dark", false);
    for (const key of ["interface", "startup_range", "auto_configure_interface", "debug"]) {
      const input = document.querySelector(`#${key.replaceAll("_", "-")}`);
      const value = defaults.preferences?.[key];
      if (!input || value === undefined) continue;
      if (input.type === "checkbox") input.checked = value === true;
      else if ([...input.options].some(option => option.value === value)) input.value = value;
    }
    if (defaults.build) document.querySelector("#build-info").textContent = `v${defaults.build.version} · ${defaults.build.id}`;
  } catch (error) {
    document.querySelector("#form-error").textContent = error.message;
  }
}

async function savePreferences(values) {
  try {
    await getJson("/api/preferences", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(values)});
  } catch (error) { document.querySelector("#form-error").textContent = `Could not save settings: ${error.message}`; }
}
for (const key of ["interface", "startup_range", "auto_configure_interface", "debug"]) {
  const input = document.querySelector(`#${key.replaceAll("_", "-")}`);
  input?.addEventListener("change", () => savePreferences({[key]: input.type === "checkbox" ? input.checked : input.value}));
}

document.querySelector("#refresh-interfaces").addEventListener("click", loadInterfaces);
document.querySelector("#restore-network").addEventListener("click", async () => {
  const button = document.querySelector("#restore-network");
  button.disabled = true;
  try {
    await getJson("/api/network/restore", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: "{}"});
    await refreshStatus();
  } catch (error) {
    document.querySelector("#form-error").textContent = error.message;
    document.querySelector("#advanced-settings").open = true;
  } finally { button.disabled = latestStatus?.running === true; }
});
function sessionConfig({fullDiscovery = false} = {}) {
  return {
    interface: fullDiscovery ? "" : document.querySelector("#interface").value,
    save_root: defaultSaveRoot,
    session_name: "",
    startup_range: document.querySelector("#startup-range").value,
    autosave_raw: false,
    autosave_echogram: false,
    autosave_minutes: 10,
    save_frames: true,
    auto_configure_interface: fullDiscovery || document.querySelector("#auto-configure-interface").checked,
    debug: document.querySelector("#debug").checked
  };
}

async function startEmulator(options) {
  return getJson("/api/session/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(sessionConfig(options))
  });
}

function setViewerPlaceholder(message, {canRestart = false} = {}) {
  viewerPlaceholderMessage.textContent = message;
  restartAutoDiscoveryButton.hidden = !canRestart;
  restartAutoDiscoveryButton.disabled = false;
}

async function waitUntilStopped() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const status = await getJson("/api/status", {cache: "no-store"});
    if (!status.running) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error("OpenScope did not stop in time");
}

document.querySelector("#start-form").addEventListener("submit", async event => {
  event.preventDefault();
  const errorBox = document.querySelector("#form-error");
  errorBox.textContent = "";
  applyButton.disabled = true;
  try {
    if (latestStatus?.running) {
      await getJson("/api/session/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: "{}"
      });
      await waitUntilStopped();
    }
    await startEmulator();
    await refreshStatus();
  } catch (error) {
    errorBox.textContent = error.message;
    document.querySelector("#advanced-settings").open = true;
  } finally {
    applyButton.disabled = false;
  }
});

fullDiscoveryButton.addEventListener("click", async () => {
  const errorBox = document.querySelector("#form-error");
  errorBox.textContent = "";
  fullDiscoveryButton.disabled = true;
  try {
    await startEmulator({fullDiscovery: true});
    await refreshStatus();
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    fullDiscoveryButton.disabled = latestStatus?.running === true;
  }
});

restartAutoDiscoveryButton.addEventListener("click", async () => {
  setViewerPlaceholder("Restarting OpenScope…");
  restartAutoDiscoveryButton.disabled = true;
  try {
    await startEmulator({fullDiscovery: true});
    await refreshStatus();
  } catch (error) {
    document.querySelector("#form-error").textContent = error.message;
    setViewerPlaceholder(
      "OpenScope stopped. Restart with auto-detect and IP repair, or adjust the advanced settings.",
      {canRestart: true}
    );
  }
});

stopButton.addEventListener("click", async () => {
  if (!confirm("Stop this session? This ends the stream and all active recordings.")) return;
  try {
    await getJson("/api/session/stop", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}"
    });
  } catch (error) {
    document.querySelector("#session-message").textContent = error.message;
  }
});

async function refreshStatusOnce() {
  try {
    const status = await getJson("/api/status", {cache: "no-store"});
    latestStatus = status;
    const diagnostics = status.diagnostics ?? {};
    statePill.textContent = status.running ? "OpenScope running" : status.message;
    statePill.classList.toggle("live", status.running);
    statusDot.classList.toggle("live", status.running);
    document.querySelector("#session-message").textContent = status.message;
    const sessionError = document.querySelector("#session-error");
    const recoveryMessage = !status.running && status.network_recovery_pending
      ? "Network changes still need restoring. Reconnect the dedicated adapter and use Restore network settings in Connection diagnostics."
      : "";
    sessionError.textContent = status.error
      ? `${status.error} ${recoveryMessage} Open “Session log” below for full details.`
      : recoveryMessage;
    sessionError.hidden = !sessionError.textContent;
    stopButton.disabled = !status.running;
    document.querySelector("#restore-network").disabled = status.running;
    fullDiscoveryButton.disabled = status.running;
    applyButton.textContent = status.running
      ? "Apply and restart OpenScope"
      : "Start OpenScope with these settings";
    updateSessionLog(status.logs.length ? status.logs.join("\n") : "No session output yet.");
    document.querySelector("#diagnostic-interface").textContent =
      diagnostics.interface ?? "Not selected";
    const link = document.querySelector("#diagnostic-link");
    link.textContent = diagnostics.link_state ?? "Unknown";
    link.classList.toggle("healthy", diagnostics.link_state === "active");
    link.classList.toggle("warning", diagnostics.link_state === "inactive");
    const imagePackets = diagnostics.image_packets_per_second;
    const totalPackets = diagnostics.packets_per_second;
    document.querySelector("#diagnostic-packets").textContent =
      imagePackets == null
        ? "Waiting"
        : `${imagePackets} image chunks/s · ${totalPackets ?? 0} total packets/s`;
    const stream = document.querySelector("#diagnostic-stream");
    stream.textContent =
      diagnostics.stream_state ?? "Waiting for packet diagnostics";
    stream.classList.toggle(
      "healthy",
      imagePackets != null && imagePackets > 0
    );
    stream.classList.toggle(
      "warning",
      diagnostics.link_state === "inactive" || imagePackets === 0
    );
    document.querySelector("#diagnostic-guidance").textContent =
      diagnostics.guidance ?? "";
    document.querySelector("#diagnostic-action").textContent =
      diagnostics.action_feedback ?? "No actions yet";
    if (status.running && status.viewer_ready && viewerUrl !== status.viewer_url) {
      document.querySelector("#range-failure").hidden = true;
      sonarEnableFailed = false;
      viewerUrl = status.viewer_url;
      viewer.src = viewerUrl;
    } else if (!status.running && viewerUrl) {
      document.querySelector("#range-failure").hidden = true;
      sonarEnableFailed = false;
      viewer.removeAttribute("src");
      viewerUrl = "";
    }
    updateAdapterSuggestion();
    const viewerReady = status.running && status.viewer_ready;
    viewer.hidden = !viewerReady;
    viewerPlaceholder.hidden = viewerReady;
    if (!status.viewer_ready) {
      setViewerPlaceholder(
        status.running
          ? "Starting OpenScope…"
          : "OpenScope stopped. Restart with auto-detect and IP repair, or adjust the advanced settings.",
        {canRestart: !status.running}
      );
    }
    if (!status.running && status.exit_code == null && !autoStartAttempted) {
      autoStartAttempted = true;
      setViewerPlaceholder("Starting OpenScope…");
      try {
        await startEmulator();
      } catch (error) {
        document.querySelector("#form-error").textContent = error.message;
        document.querySelector("#advanced-settings").open = true;
      }
    }
  } catch (error) {
    statePill.textContent = "OpenScope disconnected";
    sonarEnableFailed = false;
    updateAdapterSuggestion();
    viewer.hidden = true;
    viewerPlaceholder.hidden = false;
    setViewerPlaceholder("OpenScope disconnected");
  }
}

function selectionTouches(element) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return false;
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const range = selection.getRangeAt(index);
    if (
      element.contains(range.commonAncestorContainer) ||
      element.contains(range.startContainer) ||
      element.contains(range.endContainer)
    ) {
      return true;
    }
  }
  return false;
}

function updateSessionLog(nextText) {
  const logs = document.querySelector("#logs");
  if (logs.textContent === nextText) return;
  if (selectionTouches(logs)) {
    pendingLogText = nextText;
    return;
  }
  logs.textContent = nextText;
  pendingLogText = null;
}

document.addEventListener("selectionchange", () => {
  if (pendingLogText === null) return;
  const logs = document.querySelector("#logs");
  if (selectionTouches(logs)) return;
  updateSessionLog(pendingLogText);
});

function refreshStatus() {
  if (statusRefreshPromise === null) {
    statusRefreshPromise = refreshStatusOnce().finally(() => {
      statusRefreshPromise = null;
    });
  }
  return statusRefreshPromise;
}

async function pollStatus() {
  await refreshStatus();
  setTimeout(pollStatus, 500);
}

async function initialize() {
  await loadInterfaces();
  await loadDefaults();
  pollStatus();
}

initialize();
