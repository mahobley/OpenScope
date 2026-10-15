"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {collectDiagnostics} = require("../src/diagnostics");
const {ControllerBackend} = require("../src/backend");

test("a truncated credential line cannot leak its unlabelled suffix", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-diagnostics-"));
  try {
    fs.writeFileSync(path.join(directory, "controller.log"), "Bearer " + "secret".repeat(400000) + "\nsafe event\n");
    assert.equal(collectDiagnostics(directory, "test").logs["controller.log"], "safe event\n");
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

test("startup trimming also discards partial credential lines", async () => {
  const {EventEmitter} = require("node:events");
  const {PassThrough} = require("node:stream");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-trim-"));
  fs.mkdirSync(path.join(directory, "logs"));
  const log = path.join(directory, "logs", "controller.log");
  fs.writeFileSync(log, "Bearer " + "secret".repeat(400000) + "\nsafe event\n");
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const backend = new ControllerBackend({appRoot: directory, isPackaged: false, userDataPath: directory, spawnImpl: () => child});
  const starting = assert.rejects(backend.start(), /exited before readiness/);
  try {
    assert.equal(fs.readFileSync(log, "utf8"), "safe event\n");
  } finally {
    child.emit("close", 1, null);
    await starting;
    await new Promise(resolve => setImmediate(resolve));
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test("diagnostics include version and bounded log tails with credentials removed", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-diagnostics-"));
  try {
    fs.writeFileSync(path.join(directory, "controller.log"), "x".repeat(3 * 1024 * 1024)
      + '\nhttp://127.0.0.1:1234/?token=secret\nBearer private\nsonar_controller_token=cookie;\n');
    const report = collectDiagnostics(directory, "0.1.0");
    assert.equal(report.version, "0.1.0");
    assert.equal(report.architecture, process.arch);
    assert.ok(report.logs["controller.log"].length <= 2 * 1024 * 1024);
    assert.doesNotMatch(JSON.stringify(report), /secret|private|cookie/);
    assert.match(report.logs["controller.log"], /redacted/);
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

test("log follower consumes only new bytes and survives rotation", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-log-follow-"));
  const log = path.join(directory, "controller.log");
  fs.writeFileSync(log, "previous launch\n");
  const backend = new ControllerBackend({});
  const chunks = [];
  const stop = backend.followLog(log, buffer => chunks.push(buffer.toString()), {fromEnd: true});
  try {
    fs.appendFileSync(log, "new launch\n");
    await new Promise(resolve => setTimeout(resolve, 150));
    fs.renameSync(log, `${log}.1`);
    fs.writeFileSync(log, "rotated log longer than the previous file contents\n");
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(chunks.join(""), "new launch\nrotated log longer than the previous file contents\n");
  } finally { stop(); fs.rmSync(directory, {recursive: true, force: true}); }
});
