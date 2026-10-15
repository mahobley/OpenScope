const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../../backend/live_viewer/web/app.js"), "utf8"
);

function imageElement() {
  const listeners = new Map();
  const requests = [];
  return {
    requests,
    addEventListener(event, callback) { listeners.set(event, callback); },
    set src(url) { requests.push(url); },
    emit(event) { listeners.get(event)(); },
  };
}

test("viewer lets slow JPEGs finish, then catches up to the latest frame", () => {
  const image = imageElement();
  const echogram = imageElement();
  const context = vm.createContext({
    image, echogram, updateNoSignalState() {},
    document: {querySelector() { return {}; }},
  });
  // Execute the actual image lifecycle and refresh branches, without the
  // unrelated control-panel DOM and audio setup.
  const lifecycle = source.slice(source.indexOf("let sonarImageLoading"),
                                source.indexOf("let autosaveControlsInitialized"));
  const updates = source.slice(source.indexOf("    if (status.display_enabled !== false && !sonarImageLoading"),
                              source.indexOf("    const echogramIsCropped"));
  vm.runInContext(lifecycle, context);
  function refresh(sequence, displayEnabled = true) {
    context.status = {frame_sequence: sequence, echogram_sequence: sequence,
                      display_enabled: displayEnabled};
    vm.runInContext(updates, context);
  }

  refresh(1);
  refresh(2);
  refresh(3);
  assert.deepEqual(image.requests, ["/api/frame.jpg?v=1"]);
  assert.deepEqual(echogram.requests, ["/api/echogram.jpg?v=1"]);

  image.emit("load");
  refresh(4);
  assert.deepEqual(image.requests, ["/api/frame.jpg?v=1", "/api/frame.jpg?v=4"]);
  assert.equal(echogram.requests.length, 1);

  echogram.emit("load");
  refresh(5);
  assert.equal(image.requests.length, 2);
  assert.equal(echogram.requests.at(-1), "/api/echogram.jpg?v=5");

  // Failed downloads can retry the same published frame.
  image.emit("error");
  echogram.emit("error");
  refresh(5);
  assert.equal(image.requests.at(-1), "/api/frame.jpg?v=5");
  assert.equal(echogram.requests.length, 3);
  image.emit("load");
  echogram.emit("load");
  refresh(5);
  assert.equal(image.requests.length, 3);
  assert.equal(echogram.requests.length, 3);
  refresh(6, false);
  refresh(7, false);
  assert.equal(image.requests.length, 3);
  assert.equal(echogram.requests.length, 3);
  refresh(7, true);
  assert.equal(image.requests.at(-1), "/api/frame.jpg?v=7");
  assert.equal(echogram.requests.at(-1), "/api/echogram.jpg?v=7");
});

function geometryHarness() {
  let now = 0;
  let width = 800;
  let resize;
  const ratios = [];
  const heights = [];
  const image = {naturalWidth: 800, naturalHeight: 400, addEventListener() {}};
  const style = {
    set aspectRatio(value) { this.ratio = Number(value); ratios.push(this.ratio); },
  };
  const viewer = {
    style,
    classList: {add() {}},
    getBoundingClientRect() { return {width, height: width / (style.ratio || 2)}; },
  };
  const context = vm.createContext({
    sonarImage: image,
    sonarViewer: viewer,
    echogramPanelForSizing: {style: {setProperty(_name, value) { heights.push(value); }}},
    performance: {now: () => now},
    window: {addEventListener() {}},
    ResizeObserver: class {
      constructor(callback) { resize = callback; }
      observe() {}
    },
  });
  const sizing = source.slice(source.indexOf("const SONAR_LAYOUT_SETTLE_MS"),
                              source.indexOf("liveRangeCrop.addEventListener"));
  vm.runInContext(sizing, context);
  return {
    ratios, heights,
    frame(height, time) {
      image.naturalHeight = height;
      now = time;
      vm.runInContext("stabilizeSonarGeometry()", context);
    },
    resize(newWidth) { width = newWidth; resize(); },
  };
}

test("transient dimensions and pixel jitter leave the sonar and echogram layout steady", () => {
  const gui = geometryHarness();
  gui.frame(400, 0);
  gui.frame(390, 33);
  gui.frame(390, 66);
  gui.frame(400, 100);
  // Returning to the original size cancels the candidate, even if the same
  // glitch appears much later. Small jitter never establishes a new size.
  gui.frame(390, 800);
  gui.frame(400, 850);
  gui.frame(399, 1000);
  gui.frame(399, 1400);
  gui.frame(399, 1800);
  assert.deepEqual(gui.ratios, [2]);
  assert.deepEqual(gui.heights, ["400px"]);
});

test("persistent dimensions can resize the layout after both settling conditions", () => {
  const gui = geometryHarness();
  gui.frame(400, 0);
  gui.frame(500, 100);
  gui.frame(500, 133);
  gui.frame(500, 166);
  assert.deepEqual(gui.ratios, [2]);
  gui.frame(500, 501);
  assert.deepEqual(gui.ratios, [2, 1.6]);
  assert.deepEqual(gui.heights, ["400px", "500px"]);
});

test("two slow frames cannot resize the layout, and window resizing remains responsive", () => {
  const gui = geometryHarness();
  gui.frame(400, 0);
  gui.frame(500, 100);
  gui.frame(500, 600);
  assert.deepEqual(gui.ratios, [2]);
  gui.resize(1200);
  assert.equal(gui.heights.at(-1), "600px");
  assert.deepEqual(gui.ratios, [2]);
});
