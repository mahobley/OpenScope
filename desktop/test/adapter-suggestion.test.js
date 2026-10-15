"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const controllerSource = fs.readFileSync(path.join(__dirname, "../../backend/controller_web/app.js"), "utf8");
const viewerSource = fs.readFileSync(path.join(__dirname, "../../backend/live_viewer/web/app.js"), "utf8").replace(/\r\n/g, "\n");

function controllerHarness(interfaces = ["eno1", "eno2"], preferences = {}) {
  const elements = new Map();
  const handlers = new Map();
  const requests = [];
  const querySelector = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: "", options: [], hidden: true, style: {}, classList: {toggle() {}},
      addEventListener(event, callback) { this[event] = callback; },
      replaceChildren(...options) { this.options = options; },
      add(option) { this.options.push(option); },
      setAttribute() {},
      focus() { this.focused = true; },
      scrollIntoView() { this.scrolled = true; },
    });
    return elements.get(selector);
  };
  const viewerWindow = {postMessage() {}};
  querySelector("#viewer").contentWindow = viewerWindow;
  const context = vm.createContext({
    URL,
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    document: {querySelector, documentElement: {dataset: {}}, addEventListener() {}},
    window: {addEventListener(type, handler) { handlers.set(type, handler); }},
    fetch: async url => {
      requests.push(url);
      return {ok: true, json: async () => url === "/api/interfaces"
        ? {interfaces} : {preferences, save_root: "recordings"}};
    },
  });
  vm.runInContext(controllerSource.replace(/\ninitialize\(\);\s*$/, ""), context);
  vm.runInContext(`latestStatus = {running: true, viewer_ready: true, diagnostics: {interface: "eno1"}};
    viewerUrl = "http://127.0.0.1:8766/";`, context);
  return {
    element: querySelector, requests,
    async initialize() { await vm.runInContext("loadInterfaces().then(loadDefaults)", context); },
    message(failed, overrides = {}) {
      handlers.get("message")({origin: "http://127.0.0.1:8766", source: viewerWindow,
        data: {type: "viewer-sonar-status", failed}, ...overrides});
    },
    setStatus(status) {
      context.nextStatus = status;
      vm.runInContext("latestStatus = nextStatus; updateAdapterSuggestion();", context);
    },
  };
}

test("enable failure suggests other adapters and opens the picker without switching", async () => {
  const ui = controllerHarness(["eno1", "eno2", "USB Ethernet"]);
  await ui.initialize();
  ui.message(true);
  assert.equal(ui.element("#adapter-suggestion").hidden, false);
  assert.match(ui.element("#adapter-suggestion-message").textContent, /failed to enable on eno1/);
  assert.match(ui.element("#adapter-suggestion-message").textContent, /eno2, USB Ethernet/);
  ui.element("#choose-another-adapter").click();
  assert.equal(ui.element("#advanced-settings").open, true);
  assert.equal(ui.element("#advanced-settings").scrolled, true);
  assert.equal(ui.element("#interface").focused, true);
  assert.equal(ui.element("#interface").value, "");
  assert.ok(ui.requests.every(url => !url.includes("/api/session/")));
});

test("the suggestion clears on a retry, recovery, or stopped session", async () => {
  const ui = controllerHarness();
  await ui.initialize();
  ui.message(true);
  ui.message(false);
  assert.equal(ui.element("#adapter-suggestion").hidden, true);
  ui.message(true);
  ui.setStatus({running: false, viewer_ready: false, diagnostics: {interface: "eno1"}});
  assert.equal(ui.element("#adapter-suggestion").hidden, true);
});

test("a single adapter or an untrusted viewer message cannot show the suggestion", async () => {
  const single = controllerHarness(["eno1"]);
  await single.initialize();
  single.message(true);
  assert.equal(single.element("#adapter-suggestion").hidden, true);
  const multiple = controllerHarness();
  await multiple.initialize();
  multiple.message(true, {origin: "http://example.com"});
  multiple.message(true, {source: {}});
  assert.equal(multiple.element("#adapter-suggestion").hidden, true);
});

test("the picker starts with auto-detect and restores only a saved selection", async () => {
  const fresh = controllerHarness(["eno1", "eno2"]);
  await fresh.initialize();
  assert.equal(fresh.element("#interface").value, "");
  const saved = controllerHarness(["eno1", "eno2"], {interface: "eno2"});
  await saved.initialize();
  assert.equal(saved.element("#interface").value, "eno2");
});

test("the viewer reports enable failures separately from disable failures and recovery", () => {
  const messages = [];
  const context = vm.createContext({
    window: {parent: {postMessage(message) { messages.push(message); }}},
    previousStatus: null, status: null,
  });
  const start = viewerSource.indexOf('    if (window.parent !== window && (\n      !previousStatus || status.sonar_transition');
  assert.ok(start >= 0);
  const reporting = viewerSource.slice(start, viewerSource.indexOf("    if (previousStatus) {", start));
  for (const [transition, requested] of [["pending", true], ["failed", true], ["failed", true],
                                       ["confirmed", true], ["failed", false]]) {
    context.status = {sonar_transition: transition, sonar_requested: requested};
    vm.runInContext(reporting, context);
    context.previousStatus = context.status;
  }
  assert.deepEqual(messages.map(message => message.failed), [false, true, false, false]);
});
