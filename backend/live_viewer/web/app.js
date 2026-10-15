const image = document.querySelector("#sonar");
if (window.parent !== window) document.documentElement.classList.add("embedded");
const themeToggle = document.querySelector("#theme-toggle");

function applyTheme(theme, persist = true) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  themeToggle.textContent = light ? "Dark mode" : "Light mode";
  themeToggle.setAttribute("aria-label", `Switch to ${light ? "dark" : "light"} mode`);
  if (persist) action("set_theme", light ? "light" : "dark");
}

applyTheme("dark", false);
themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});
window.addEventListener("message", event => {
  if (event.source === window.parent && event.data?.type === "emulator-ui-theme") applyTheme(event.data.theme, false);
});
function reportEmbeddedHeight() {
  if (window.parent === window) return;
  const content = document.querySelector("main");
  window.parent.postMessage({
    type: "viewer-height",
    // Do not report documentElement.scrollHeight: in an iframe it includes
    // the parent-assigned viewport height, creating an ever-growing loop.
    height: content.scrollHeight
  }, "*");
}
new ResizeObserver(reportEmbeddedHeight).observe(document.querySelector("main"));
window.addEventListener("load", reportEmbeddedHeight);
const echogram = document.querySelector("#echogram");
const empty = document.querySelector("#empty");
const sonarViewer = document.querySelector("#sonar-panel .viewer");
const liveRangeCrop = document.querySelector("#live-range-crop");
const sonarImage = document.querySelector("#sonar");
const echogramPanelForSizing = document.querySelector("#echogram-panel");

const SONAR_LAYOUT_SETTLE_MS = 400;
const SONAR_LAYOUT_MIN_FRAMES = 3;
const SONAR_LAYOUT_TOLERANCE_PX = 2;
let stableSonarRatio = null;
let pendingSonarGeometry = null;

function matchEchogramToSonar() {
  // Follow the stable container, rather than a transient JPEG's dimensions.
  const height = Math.round(sonarViewer.getBoundingClientRect().height);
  if (height > 0) {
    echogramPanelForSizing.style.setProperty("--live-sonar-height", `${height}px`);
  }
}

function stabilizeSonarGeometry() {
  if (!sonarImage.naturalWidth || !sonarImage.naturalHeight) return;
  const ratio = sonarImage.naturalWidth / sonarImage.naturalHeight;
  const width = sonarViewer.getBoundingClientRect().width;
  if (width <= 0) return;
  const differs = other => Math.abs(width / ratio - width / other) > SONAR_LAYOUT_TOLERANCE_PX;
  const now = performance.now();
  if (stableSonarRatio !== null && !differs(stableSonarRatio)) {
    pendingSonarGeometry = null;
    return;
  }
  if (stableSonarRatio !== null) {
    if (!pendingSonarGeometry || differs(pendingSonarGeometry.ratio)) {
      pendingSonarGeometry = {ratio, since: now, frames: 1};
      return;
    }
    pendingSonarGeometry.frames += 1;
    if (pendingSonarGeometry.frames < SONAR_LAYOUT_MIN_FRAMES
        || now - pendingSonarGeometry.since < SONAR_LAYOUT_SETTLE_MS) return;
  }
  stableSonarRatio = ratio;
  pendingSonarGeometry = null;
  sonarViewer.style.aspectRatio = String(ratio);
  sonarViewer.classList.add("has-stable-geometry");
  matchEchogramToSonar();
}

sonarImage.addEventListener("load", stabilizeSonarGeometry);
window.addEventListener("resize", matchEchogramToSonar);
new ResizeObserver(matchEchogramToSonar).observe(sonarViewer);
liveRangeCrop.addEventListener("click", () => action("toggle_live_range_crop"));
// Leave each image request in flight until load/error. Faster polling must
// not repeatedly cancel JPEG downloads on a slow connection.
let sonarImageLoading = false;
let echogramImageLoading = false;
image.addEventListener("load", () => { sonarImageLoading = false; });
image.addEventListener("error", () => { sonarImageLoading = false; sequence = -1; });
echogram.addEventListener("load", () => { echogramImageLoading = false; });
echogram.addEventListener("error", () => { echogramImageLoading = false; echogramSequence = -1; });
let sequence = -1;
let echogramSequence = -1;
let autosaveControlsInitialized = false;
let appliedOutputPaths = null;
let pendingOutputPaths = null;
let outputPathsSubmitting = false;
let outputSubmitStartedAt = 0;
let currentRecordingStatus = null;
let folderChooserOpen = false;
let pendingGain = null;
let gainRequestActive = false;
let pendingEchogramGain = null;
let echogramGainRequestActive = false;
let pendingIntensityWindow = null;
let intensityWindowRequestActive = false;
let rangeRequestGraceUntil = 0;
let previousStatus = null;
let audioContext = null;

function ensureAudio() {
  const Context = window.AudioContext || window.webkitAudioContext;
  audioContext ??= new Context();
  if (audioContext.state === "suspended") audioContext.resume();
}

function tone(frequency, duration, delay = 0, volume = 0.08) {
  ensureAudio();
  const oscillator = audioContext.createOscillator();
  const gain = audioContext.createGain();
  const start = audioContext.currentTime + delay;
  oscillator.frequency.value = frequency;
  oscillator.type = "square";
  gain.gain.setValueAtTime(volume, start);
  gain.gain.exponentialRampToValueAtTime(0.001, start + duration);
  oscillator.connect(gain).connect(audioContext.destination);
  oscillator.start(start);
  oscillator.stop(start + duration);
}

function diagnosticSound(kind) {
  if (kind === "start") {
    tone(440, 0.08);
  } else if (kind === "failure") {
    tone(180, 0.22);
    tone(120, 0.28, 0.18);
  }
}

// Unlock one-time action alerts after a user gesture.
window.addEventListener("keydown", ensureAudio);
window.addEventListener("pointerdown", ensureAudio);

function formatRecordingDuration(startedAt) {
  const elapsedMilliseconds = Math.max(0, Math.floor(Date.now() - startedAt * 1000));
  const totalSeconds = Math.floor(elapsedMilliseconds / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const centiseconds = Math.floor(elapsedMilliseconds % 1000 / 10);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(centiseconds).padStart(2, "0")}`;
}

function updateRecordingTimer(id, active, startedAt) {
  const timer = document.querySelector(id);
  timer.hidden = !active || startedAt == null;
  if (!timer.hidden) timer.textContent = formatRecordingDuration(startedAt);
}

function updateNoSignalState(status) {
  const state = status.no_signal_state;
  const active = state === "waiting" || state === "stalled";
  const enableCallout = document.querySelector("#empty-enable-callout");
  const enableButton = document.querySelector("#empty-enable");
  sonarViewer.classList.toggle("is-no-signal", active && !image.getAttribute("src"));
  empty.hidden = !active;
  if (!active) return;

  const waiting = state === "waiting";
  const enabling = status.sonar_pending === true;
  const canPromptForEnable = waiting && status.sonar_enabled !== true;
  enableCallout.hidden = !canPromptForEnable;
  enableButton.disabled = enabling;
  enableButton.textContent = enabling ? "Enabling sonar…" : "Enable sonar";
  empty.querySelector("strong").textContent = waiting
    ? "No incoming sonar signal"
    : "Sonar stream has stopped";
  document.querySelector("#empty-detail").textContent = waiting
    ? "No decoded sonar frames received yet."
    : "Trying to re-establish the network connection. The last received frame is shown while it recovers.";
  document.querySelector("#empty-guidance").textContent = canPromptForEnable
    ? "Press Enable sonar to start the stream, then check the sonar power, Ethernet connection, network adapter, and stream setting if no image appears."
    : "Check the sonar power, Ethernet connection, network adapter, and stream setting.";
}

function drawHistogram(histogram) {
  if (!histogram || !Array.isArray(histogram.counts)) return;
  const canvas = document.querySelector("#intensity-histogram");
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const padding = 8;
  const peak = Math.max(...histogram.counts, 1);
  context.clearRect(0, 0, width, height);
  const light = document.documentElement.dataset.theme === "light";
  context.fillStyle = light ? "#ffffff" : "#061214";
  context.fillRect(0, 0, width, height);
  context.strokeStyle = light ? "#a9c7c5" : "#244345";
  context.strokeRect(0.5, 0.5, width - 1, height - 1);
  context.beginPath();
  histogram.counts.forEach((count, intensity) => {
    const x = padding + intensity / 255 * (width - padding * 2);
    const y = height - padding - count / peak * (height - padding * 2);
    if (intensity === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = light ? "#087c70" : "#70dfca";
  context.lineWidth = 2;
  context.stroke();
  const mean = histogram.mean == null ? "—" : histogram.mean.toFixed(1);
  const median = histogram.median == null ? "—" : histogram.median.toFixed(1);
  document.querySelector("#histogram-summary").textContent =
    `${histogram.nonzero_pixels.toLocaleString()} signal px · zeros ${histogram.zero_percent.toFixed(1)}% · mean ${mean} · median ${median}`;
}

async function action(type, value) {
  try {
    const response = await fetch("/api/action", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({type, value})
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.error || `Request failed (${response.status})`);
    }
    return true;
  } catch (error) {
    document.querySelector("#notice").textContent =
      `Action request failed: ${error.message}`;
    return false;
  }
}

document.querySelectorAll("[data-action]").forEach(button => {
  button.addEventListener("click", () => {
    if (["save_frame", "save_echogram_frame"].includes(button.dataset.action)) {
      diagnosticSound("start");
    }
    action(button.dataset.action);
  });
});
const echogramRangeCrop = document.querySelector("#echogram-range-crop");
const screens = document.querySelector(".screens");
const echogramToggle = document.querySelector("#echogram-toggle");

echogramToggle.addEventListener("click", () => {
  const hidden = screens.classList.toggle("echogram-hidden");
  echogramToggle.textContent = hidden ? "Show echogram" : "Hide echogram";
  echogramToggle.setAttribute(
    "aria-label", hidden ? "Show echogram" : "Hide echogram"
  );
  reportEmbeddedHeight();
});

echogramRangeCrop.addEventListener("click", () => {
  action("toggle_echogram_range_crop");
});
document.querySelector("#range-select").addEventListener("change", event => {
  const raw = event.target.value;
  // The action endpoint acknowledges queueing before the viewer loop applies
  // the command. Keep the user's selection during that brief status gap.
  rangeRequestGraceUntil = Date.now() + 1000;
  action("set_range", raw === "min" ? raw : Number(raw));
});
document.querySelector("#color-scheme").addEventListener("change", event => {
  action("set_color_scheme", event.target.value);
});
async function sendLatestGain() {
  if (gainRequestActive) return;
  gainRequestActive = true;
  try {
    while (pendingGain !== null) {
      const value = pendingGain;
      pendingGain = null;
      await action("set_gain", value);
    }
  } finally {
    gainRequestActive = false;
  }
}

const gainSelect = document.querySelector("#gain-select");
function requestGain(value) {
  value = Math.max(0, Math.min(100, Number(value)));
  gainSelect.value = String(value);
  document.querySelector("#gain-value").textContent = `${value}%`;
  pendingGain = value;
  sendLatestGain();
}
gainSelect.addEventListener("input", event => requestGain(event.target.value));
document.querySelector("#gain-down").addEventListener("click", () =>
  requestGain(Number(gainSelect.value) - 1));
document.querySelector("#gain-up").addEventListener("click", () =>
  requestGain(Number(gainSelect.value) + 1));
document.querySelector("#gain-reset").addEventListener("click", () => requestGain(21));

async function sendLatestEchogramGain() {
  if (echogramGainRequestActive) return;
  echogramGainRequestActive = true;
  try {
    while (pendingEchogramGain !== null) {
      const value = pendingEchogramGain;
      pendingEchogramGain = null;
      await action("set_echogram_gain", value);
    }
  } finally {
    echogramGainRequestActive = false;
  }
}

const echogramGain = document.querySelector("#echogram-gain");
function requestEchogramGain(value) {
  value = Math.max(0, Math.min(300, Number(value)));
  echogramGain.value = String(value);
  document.querySelector("#echogram-gain-value").textContent = `${value}%`;
  pendingEchogramGain = value;
  sendLatestEchogramGain();
}
echogramGain.addEventListener("input", event => requestEchogramGain(event.target.value));
document.querySelector("#echogram-gain-down").addEventListener("click", () =>
  requestEchogramGain(Number(echogramGain.value) - 5));
document.querySelector("#echogram-gain-up").addEventListener("click", () =>
  requestEchogramGain(Number(echogramGain.value) + 5));
document.querySelector("#echogram-gain-reset").addEventListener("click", () =>
  requestEchogramGain(100));
async function sendLatestIntensityWindow() {
  if (intensityWindowRequestActive) return;
  intensityWindowRequestActive = true;
  try {
    while (pendingIntensityWindow !== null) {
      const value = pendingIntensityWindow;
      pendingIntensityWindow = null;
      await action("set_intensity_window", value);
    }
  } finally {
    intensityWindowRequestActive = false;
  }
}

function updateIntensityWindow(changed) {
  const minimumInput = document.querySelector("#intensity-min");
  const maximumInput = document.querySelector("#intensity-max");
  let minimum = Number(minimumInput.value);
  let maximum = Number(maximumInput.value);
  if (minimum > maximum) {
    if (changed === minimumInput) minimum = maximum;
    else maximum = minimum;
  }
  minimumInput.value = minimum;
  maximumInput.value = maximum;
  document.querySelector("#intensity-min-value").textContent = minimum;
  document.querySelector("#intensity-max-value").textContent = maximum;
  const range = document.querySelector(".dual-range");
  range.style.setProperty("--minimum", `${minimum / 255 * 100}%`);
  range.style.setProperty("--maximum", `${maximum / 255 * 100}%`);
  pendingIntensityWindow = {minimum, maximum};
  sendLatestIntensityWindow();
}

document.querySelector("#intensity-min").addEventListener("input", event => {
  updateIntensityWindow(event.target);
});
document.querySelector("#intensity-max").addEventListener("input", event => {
  updateIntensityWindow(event.target);
});

const outputPathInputs = [
  document.querySelector("#recording-path"),
  document.querySelector("#echogram-recording-path"),
  document.querySelector("#snapshot-path")
];

function readOutputPaths() {
  return {
    recording: outputPathInputs[0].value.trim(),
    echogram: outputPathInputs[1].value.trim(),
    snapshots: outputPathInputs[2].value.trim()
  };
}

function statusOutputPaths(status) {
  return {
    recording: status.recording_path ?? "",
    echogram: status.echogram_recording_path ?? "",
    snapshots: status.snapshot_path ?? ""
  };
}

function outputPathsEqual(left, right) {
  return left !== null && right !== null
    && left.recording === right.recording
    && left.echogram === right.echogram
    && left.snapshots === right.snapshots;
}

function populateOutputPaths(paths) {
  outputPathInputs[0].value = paths.recording;
  outputPathInputs[1].value = paths.echogram;
  outputPathInputs[2].value = paths.snapshots;
}

function updateOutputControls() {
  const status = currentRecordingStatus;
  const applyPaths = document.querySelector("#apply-paths");
  const pathsState = document.querySelector("#paths-state");
  const guidance = document.querySelector("#paths-guidance");
  const pathsLoaded = appliedOutputPaths !== null;
  const currentPaths = readOutputPaths();
  const pathsComplete = Object.values(currentPaths).every(Boolean);
  const pathsDirty = pathsLoaded && !outputPathsEqual(currentPaths, appliedOutputPaths);
  const pathsNeedApply = pathsLoaded && (
    pathsDirty || currentRecordingStatus?.paths_confirmed !== true
  );
  const recordingBusy = status !== null && (
    status.recording === true
    || status.recording_requested === true
    || status.echogram_recording === true
    || status.autosave_raw === true
    || status.autosave_echogram === true
  );

  outputPathInputs.forEach(input => {
    input.disabled = recordingBusy || outputPathsSubmitting;
  });
  document.querySelectorAll("[data-folder-target]").forEach(button => {
    button.disabled = recordingBusy || outputPathsSubmitting || folderChooserOpen;
  });
  document.querySelector("#choose-recording-folder").disabled = recordingBusy || outputPathsSubmitting || folderChooserOpen;

  applyPaths.classList.toggle(
    "needs-apply",
    pathsNeedApply && pathsComplete && !recordingBusy && !outputPathsSubmitting
  );
  applyPaths.disabled = (
    !pathsNeedApply || !pathsComplete || recordingBusy || outputPathsSubmitting
  );
  if (!pathsLoaded) {
    pathsState.textContent = "Loading paths…";
    pathsState.className = "settings-state";
    applyPaths.textContent = "Loading paths…";
    guidance.textContent = "Waiting for the current output paths.";
  } else if (outputPathsSubmitting) {
    pathsState.textContent = "Applying changes…";
    pathsState.className = "settings-state dirty";
    applyPaths.textContent = "Applying…";
    guidance.textContent = "Validating the three output destinations.";
  } else if (recordingBusy) {
    pathsState.textContent = "Paths locked while recording";
    pathsState.className = "settings-state";
    applyPaths.textContent = pathsNeedApply ? "Apply path changes" : "Paths applied";
    guidance.textContent = "Stop manual recording and autosave before changing paths.";
  } else if (!pathsComplete) {
    pathsState.textContent = "Path required";
    pathsState.className = "settings-state dirty";
    applyPaths.textContent = "Apply path changes";
    guidance.textContent = "All three output paths are required.";
  } else if (pathsNeedApply) {
    pathsState.textContent = "Unapplied changes";
    pathsState.className = "settings-state dirty";
    applyPaths.textContent = "Apply path changes";
    guidance.textContent = "Apply these destinations before starting a recording.";
  } else {
    pathsState.textContent = "Paths ready";
    pathsState.className = "settings-state ready";
    applyPaths.textContent = "Paths applied";
    guidance.textContent = "Recordings and saved frames will use these destinations.";
  }

  const recording = document.querySelector("#recording");
  const echogramRecording = document.querySelector("#echogram-recording");
  const pathBlocked = !pathsLoaded || pathsNeedApply || outputPathsSubmitting;
  recording.disabled = status === null || (
    status.recording !== true && (status.autosave_raw === true || pathBlocked)
  );
  echogramRecording.disabled = status === null || (
    status.echogram_recording !== true
    && (status.autosave_echogram === true || pathBlocked)
  );
  document.querySelector("#save-sonar-frame").disabled = pathBlocked;
  document.querySelector("#save-echogram-frame").disabled = pathBlocked;

  const rawAutosave = document.querySelector("#autosave-raw");
  const echogramAutosave = document.querySelector("#autosave-echogram");
  const autosaveMinutes = document.querySelector("#autosave-minutes");
  const startAutosave = document.querySelector("#apply-autosave");
  const stopAutosave = document.querySelector("#stop-autosave");
  const autosaveGuidance = document.querySelector("#autosave-guidance");
  const autosaveActive = status?.autosave_raw === true
    || status?.autosave_echogram === true;
  const autosaveSelected = rawAutosave.checked || echogramAutosave.checked;
  const minutes = Number(autosaveMinutes.value);
  const minutesValid = minutes > 0 && minutes <= 1440;
  const manualConflict = (
    rawAutosave.checked && status?.recording === true
  ) || (
    echogramAutosave.checked && status?.echogram_recording === true
  );
  rawAutosave.disabled = status?.recording === true;
  echogramAutosave.disabled = status?.echogram_recording === true;
  startAutosave.disabled = (
    status === null || pathBlocked || !autosaveSelected
    || !minutesValid || manualConflict
  );
  stopAutosave.disabled = !autosaveActive;
  if (pathBlocked) {
    autosaveGuidance.textContent = "Apply the output paths before starting autosave.";
  } else if (manualConflict) {
    autosaveGuidance.textContent = "Stop the selected manual recording before enabling autosave.";
  } else if (!autosaveSelected) {
    autosaveGuidance.textContent = "Select sonar, echogram, or both.";
  } else if (!minutesValid) {
    autosaveGuidance.textContent = "Segment length must be between 0.1 and 1440 minutes.";
  } else if (autosaveActive) {
    autosaveGuidance.textContent = "Autosave is active. Change the selection or length, then update it.";
  } else {
    autosaveGuidance.textContent = "Ready to start autosave.";
  }
}

function syncOutputPaths(status) {
  const serverPaths = statusOutputPaths(status);
  currentRecordingStatus = status;
  if (appliedOutputPaths === null) {
    appliedOutputPaths = serverPaths;
    populateOutputPaths(serverPaths);
  } else if (
    pendingOutputPaths !== null
    && status.paths_confirmed === true
    && outputPathsEqual(serverPaths, pendingOutputPaths)
  ) {
    appliedOutputPaths = serverPaths;
    pendingOutputPaths = null;
    outputPathsSubmitting = false;
    populateOutputPaths(serverPaths);
  } else if (
    outputPathsSubmitting
    && Date.now() - outputSubmitStartedAt > 250
    && /path validation failed|stop both recordings/i.test(status.notice ?? "")
  ) {
    pendingOutputPaths = null;
    outputPathsSubmitting = false;
  } else if (
    pendingOutputPaths === null
    && outputPathsEqual(readOutputPaths(), appliedOutputPaths)
    && !outputPathsEqual(serverPaths, appliedOutputPaths)
  ) {
    appliedOutputPaths = serverPaths;
    populateOutputPaths(serverPaths);
  }
  updateOutputControls();
}

outputPathInputs.forEach(input => {
  input.addEventListener("input", updateOutputControls);
});
document.querySelectorAll("#autosave-raw, #autosave-echogram, #autosave-minutes").forEach(input => {
  input.addEventListener("input", updateOutputControls);
});

document.querySelectorAll("[data-folder-target]").forEach(button => {
  button.addEventListener("click", async () => {
    const input = document.querySelector(`#${button.dataset.folderTarget}`);
    const originalLabel = button.textContent;
    folderChooserOpen = true;
    button.textContent = "Opening…";
    updateOutputControls();
    try {
      const response = await fetch("/api/select-folder", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({initial_path: input.value})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Folder chooser failed");
      if (!result.selected) return;
      let selectedPath = result.path;
      if (button.hasAttribute("data-preserve-filename")) {
        const filename = input.value.replace(/[\\/]$/, "").split(/[\\/]/).pop();
        const separator = selectedPath.includes("\\") ? "\\" : "/";
        if (filename) selectedPath = `${selectedPath.replace(/[\\/]$/, "")}${separator}${filename}`;
      }
      input.value = selectedPath;
      document.querySelector("#notice").textContent =
        "Folder selected. Apply the output paths to save this change.";
      updateOutputControls();
    } catch (error) {
      document.querySelector("#notice").textContent =
        `Could not open folder chooser: ${error.message}`;
    } finally {
      folderChooserOpen = false;
      button.textContent = originalLabel;
      updateOutputControls();
    }
  });
});
document.querySelector("#apply-paths").addEventListener("click", async () => {
  pendingOutputPaths = readOutputPaths();
  outputPathsSubmitting = true;
  outputSubmitStartedAt = Date.now();
  updateOutputControls();
  if (!await action("set_output_paths", pendingOutputPaths)) {
    pendingOutputPaths = null;
    outputPathsSubmitting = false;
    updateOutputControls();
  }
});
document.querySelectorAll("[data-open-folder]").forEach(button => {
  button.addEventListener("click", () => action("open_output_folder", button.dataset.openFolder));
});
document.querySelector("#choose-recording-folder").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (folderChooserOpen || currentRecordingStatus?.recording || currentRecordingStatus?.echogram_recording) return;
  folderChooserOpen = true;
  button.disabled = true;
  updateOutputControls();
  try {
    const response = await fetch("/api/select-folder", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({initial_path: document.querySelector("#snapshot-path").value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Folder chooser failed");
    if (!result.selected) return;
    const folder = result.path.replace(/[\\/]$/, "");
    const separator = folder.includes("\\") ? "\\" : "/";
    for (const id of ["recording-path", "echogram-recording-path"]) {
      const input = document.querySelector(`#${id}`);
      const filename = input.value.split(/[\\/]/).pop();
      input.value = `${folder}${separator}${filename}`;
    }
    document.querySelector("#snapshot-path").value = `${folder}${separator}snapshots`;
    document.querySelector("#notice").textContent = "Folder selected for all recordings. Apply the output paths to save this change.";
  } catch (error) { document.querySelector("#notice").textContent = `Could not select recording folder: ${error.message}`; }
  finally { folderChooserOpen = false; button.disabled = false; updateOutputControls(); }
});
document.querySelector("#recording-format").addEventListener("change", event => {
  const format = event.target.value;
  event.target.disabled = true;
  action("set_recording_formats", {video: format !== "png", png: format !== "video"});
});
document.querySelector("#apply-autosave").addEventListener("click", () => {
  action("set_autosave", {
    raw: document.querySelector("#autosave-raw").checked,
    echogram: document.querySelector("#autosave-echogram").checked,
    minutes: Number(document.querySelector("#autosave-minutes").value)
  });
});
document.querySelector("#stop-autosave").addEventListener("click", () => {
  document.querySelector("#autosave-raw").checked = false;
  document.querySelector("#autosave-echogram").checked = false;
  action("stop_autosave");
});
document.querySelector("#stop-emulator").addEventListener("click", () => {
  if (confirm("Stop this session? This ends the stream and all active recordings.")) {
    action("stop_emulator");
  }
});
async function refresh() {
  try {
    const status = await fetch("/api/status", {cache: "no-store"}).then(r => r.json());
    if (window.parent === window && status.theme) applyTheme(status.theme, false);
    if (window.parent !== window && (
      !previousStatus || status.range_change_failed !== previousStatus.range_change_failed
    )) {
      window.parent.postMessage({
        type: "viewer-range-status", failed: status.range_change_failed === true
      }, "*");
    }
    if (window.parent !== window && (
      !previousStatus || status.sonar_transition !== previousStatus.sonar_transition
      || status.sonar_requested !== previousStatus.sonar_requested
    )) {
      window.parent.postMessage({
        type: "viewer-sonar-status",
        failed: status.sonar_transition === "failed" && status.sonar_requested === true
      }, "*");
    }
    if (previousStatus) {
      if (
        (!previousStatus.recording && status.recording && !status.autosave_raw)
        || (!previousStatus.echogram_recording && status.echogram_recording && !status.autosave_echogram)
      ) diagnosticSound("start");
      if (status.notice !== previousStatus.notice && /failed|could not|no sonar frame|unexpected/i.test(status.notice ?? "")) {
        diagnosticSound("failure");
      }
    }
    previousStatus = status;
    updateNoSignalState(status);
    const sonarPending = status.sonar_pending;
    let connectionLabel = "Waiting for stream";
    if (sonarPending === false) connectionLabel = "Disabling sonar…";
    else if (sonarPending === true) connectionLabel = "Enabling sonar…";
    else if (status.sonar_enabled === false) connectionLabel = "Sonar disabled";
    else if (status.streaming) connectionLabel = "Live stream";
    else if (status.stream_stalled) connectionLabel = "Stream stalled";
    document.querySelector("#connection").textContent = connectionLabel;
    document.querySelector("#frame").textContent = status.frame_id ?? "—";
    const captureFps = status.capture_fps ?? status.fps ?? 0;
    const displayFps = status.display_fps ?? 0;
    document.querySelector("#fps").textContent = captureFps
      ? `${captureFps.toFixed(1)} / ${displayFps.toFixed(1)}` : "—";
    const actualRangeLabel = status.range == null ? "—" : `${status.range} ft`;
    document.querySelector("#range").textContent = status.range_pending
      ? `${actualRangeLabel} → ${status.range_requested} ft…`
      : actualRangeLabel;
    const rangeSelect = document.querySelector("#range-select");
    // A requested range is only authoritative while it is pending. Once the
    // hardware rejects/exhausts a change, range is the active value and must
    // also restore the select even if it still has focus.
    const selectedRange = status.range_pending
      ? (status.range_requested ?? status.range)
      : status.range;
    if (status.range_pending) rangeRequestGraceUntil = 0;
    const rangeRequestIsHandingOff = Date.now() < rangeRequestGraceUntil;
    if (selectedRange != null && !rangeRequestIsHandingOff && (
      document.activeElement !== rangeSelect || !status.range_pending
    )) {
      rangeSelect.value = String(selectedRange);
    }
    document.querySelector("#sonar-state").textContent =
      status.sonar_transition === "failed"
        ? (status.sonar_requested === true ? "Failed to enable" : "Failed to disable")
        : sonarPending === false ? "Disabling sonar…"
        : (sonarPending === true ? "Enabling sonar…"
          : (status.sonar_enabled === false ? "Disabled"
            : (status.sonar_enabled === true ? "Enabled" : "Unknown")));
    document.querySelectorAll(".sonar-disabled-banner").forEach(banner => {
      banner.hidden = status.sonar_enabled !== false;
    });
    document.querySelector("#tilt").textContent = status.tilt_available === true
      ? `${status.tilt_degrees >= 0 ? "+" : ""}${status.tilt_degrees.toFixed(1)}°`
      : "Unavailable";
    document.querySelector("#roll").textContent = status.roll_available === true
      ? `${status.roll_degrees >= 0 ? "+" : ""}${status.roll_degrees.toFixed(1)}°`
      : "Unavailable";
    document.querySelector("#reverse-tilt").classList.toggle("active", status.tilt_reversed === true);
    document.querySelector("#reverse-roll").classList.toggle("active", status.roll_reversed === true);
    const sonarToggle = document.querySelector("#sonar-toggle");
    sonarToggle.textContent = sonarPending === false ? "Disabling sonar…"
      : (sonarPending === true ? "Enabling sonar…"
        : (status.sonar_enabled === true ? "Disable sonar" : "Enable sonar"));
    sonarToggle.disabled = sonarPending === true || sonarPending === false;
    const displayEnabled = status.display_enabled !== false;
    const displayToggle = document.querySelector("#display-toggle");
    displayToggle.textContent = displayEnabled ? "Hide display" : "Show display";
    displayToggle.setAttribute("aria-pressed", String(!displayEnabled));
    screens.hidden = !displayEnabled;
    const colorScheme = document.querySelector("#color-scheme");
    if (status.color_scheme && document.activeElement !== colorScheme) {
      colorScheme.value = status.color_scheme;
    }
    document.querySelector("#view-toggle").textContent = status.warp_enabled === true
      ? "View polar" : "View Cartesian";
    document.querySelector(".screens").classList.toggle(
      "cartesian-view", status.warp_enabled === true
    );
    const liveIsCropped = status.live_range_cropped === true;
    liveRangeCrop.textContent = liveIsCropped ? "Show full range" : "Crop at range";
    const distanceGrid = document.querySelector("#distance-grid");
    distanceGrid.textContent = status.distance_grid_enabled === false
      ? "Show distance grid" : "Hide distance grid";
    document.querySelector("#motion-toggle").classList.toggle(
      "active", status.motion_enabled === true
    );
    if (status.gain != null && document.activeElement !== gainSelect) {
      gainSelect.value = String(status.gain);
      document.querySelector("#gain-value").textContent = `${status.gain}%`;
    }
    if (status.echogram_gain != null && document.activeElement !== echogramGain) {
      echogramGain.value = String(status.echogram_gain);
      document.querySelector("#echogram-gain-value").textContent =
        `${status.echogram_gain}%`;
    }
    if (displayEnabled) drawHistogram(status.histogram);
    const intensityMin = document.querySelector("#intensity-min");
    const intensityMax = document.querySelector("#intensity-max");
    if (
      status.intensity_min != null
      && status.intensity_max != null
      && document.activeElement !== intensityMin
      && document.activeElement !== intensityMax
    ) {
      intensityMin.value = status.intensity_min;
      intensityMax.value = status.intensity_max;
      document.querySelector("#intensity-min-value").textContent = status.intensity_min;
      document.querySelector("#intensity-max-value").textContent = status.intensity_max;
      const range = document.querySelector(".dual-range");
      range.style.setProperty("--minimum", `${status.intensity_min / 255 * 100}%`);
      range.style.setProperty("--maximum", `${status.intensity_max / 255 * 100}%`);
    }
    const format = document.querySelector("#recording-format");
    format.value = status.record_video_enabled === false ? "png"
      : (status.record_png_enabled === false ? "video" : "both");
    format.disabled = status.recording === true || status.recording_requested === true || status.autosave_raw === true;
    const recording = document.querySelector("#recording");
    recording.classList.toggle("active", status.recording === true);
    recording.disabled = status.autosave_raw === true;
    recording.textContent = status.autosave_raw
      ? "Autosaving sonar"
      : (status.recording ? "Stop sonar recording"
        : status.recording_requested ? "Cancel pending recording" : "Record sonar");
    updateRecordingTimer("#recording-timer", status.recording === true, status.recording_started_at);
    const echogramRecording = document.querySelector("#echogram-recording");
    echogramRecording.classList.toggle("active", status.echogram_recording === true);
    echogramRecording.disabled = status.autosave_echogram === true;
    echogramRecording.textContent = status.autosave_echogram
      ? "Autosaving echogram PNGs"
      : (status.echogram_recording
        ? "Finish and save echogram PNG"
        : "Start echogram capture");
    updateRecordingTimer("#echogram-recording-timer", status.echogram_recording === true, status.echogram_recording_started_at);
    if (!autosaveControlsInitialized) {
      document.querySelector("#autosave-raw").checked = status.autosave_raw === true;
      document.querySelector("#autosave-echogram").checked = status.autosave_echogram === true;
      if (status.autosave_minutes != null) {
        document.querySelector("#autosave-minutes").value = status.autosave_minutes;
      }
      autosaveControlsInitialized = true;
    }
    const autosaveActive = status.autosave_raw === true || status.autosave_echogram === true;
    document.querySelector("#apply-autosave").textContent = autosaveActive
      ? "Update autosave"
      : "Start autosave";
    syncOutputPaths(status);
    document.querySelector("#recording-failure").hidden = !status.recording_error;
    document.querySelector("#recording-failure-message").textContent = status.recording_error ?? "";
    document.querySelector("#disk-space").textContent = status.disk_free_bytes == null
      ? "" : `${(status.disk_free_bytes / (1024 ** 3)).toFixed(1)} GiB available`;
    document.querySelector("#notice").textContent = status.notice ?? "";
    if (status.display_enabled !== false && !sonarImageLoading && status.frame_sequence !== undefined && status.frame_sequence !== sequence) {
      sonarImageLoading = true;
      sequence = status.frame_sequence;
      image.src = `/api/frame.jpg?v=${sequence}`;
      updateNoSignalState(status);
    }
    if (status.display_enabled !== false && !echogramImageLoading && status.echogram_sequence !== undefined && status.echogram_sequence !== echogramSequence) {
      echogramImageLoading = true;
      echogramSequence = status.echogram_sequence;
      echogram.src = `/api/echogram.jpg?v=${echogramSequence}`;
      document.querySelector("#echogram-empty").hidden = true;
    }
    const echogramIsCropped = status.echogram_range_cropped === true;
    echogramRangeCrop.textContent = echogramIsCropped ? "Show full range" : "Crop at range";
    echogramRangeCrop.setAttribute(
      "aria-label",
      echogramIsCropped ? "Show full echogram range" : "Crop echogram at range boundary"
    );
  } catch (_) {
    document.querySelector("#connection").textContent = "OpenScope disconnected";
  }
  setTimeout(refresh, previousStatus?.display_enabled === false ? 250 : 33);
}
refresh();

document.querySelector("#acknowledge-recording-failure").addEventListener("click", () => {
  void action("acknowledge_recording_error");
});
