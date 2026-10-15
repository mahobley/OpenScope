"use strict";

const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {PassThrough} = require("node:stream");
const test = require("node:test");
const {
  ControllerBackend,
  controllerCommand,
  elevatedLaunchSpec,
  parseReadyLine,
  shellQuote,
  stageAppImageBackend,
} = require("../src/backend");

test("administrator prompt time does not consume the backend startup timeout", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-authorization-"));
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => {};
  const backend = new ControllerBackend({platform: "darwin", appRoot: "/checkout/desktop", isPackaged: true,
    resourcesPath: "/App/Contents/Resources", userDataPath: directory, homePath: "/Users/test",
    elevated: true, uid: 501, gid: 20, authToken: "secret", spawnImpl: () => child, startupTimeoutMs: 30});
  let settled = false;
  const start = backend.start();
  start.then(() => { settled = true; }, () => { settled = true; });
  try {
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(settled, false);
    fs.appendFileSync(path.join(directory, "logs", "launcher.log"), "SONAR_AUTHORIZED\n");
    await assert.rejects(start, /Timed out starting/);
  } finally { child.emit("close", 0, null); fs.rmSync(directory, {recursive: true, force: true}); }
});

test("parses authenticated loopback readiness", () => {
  const ready = parseReadyLine(
    "SONAR_CONTROLLER_READY "
      + '{"url":"http://127.0.0.1:49152/?token=secret","port":49152}',
  );
  assert.deepEqual(ready, {
    url: "http://127.0.0.1:49152/?token=secret",
    origin: "http://127.0.0.1:49152",
    port: 49152,
    token: "secret",
  });
});

test("rejects readiness from a non-loopback host", () => {
  assert.throws(
    () => parseReadyLine(
      "SONAR_CONTROLLER_READY "
        + '{"url":"http://example.com:49152/?token=secret","port":49152}',
    ),
    /non-loopback/,
  );
});

test("resolves source and packaged controller commands", () => {
  const source = controllerCommand({
    appRoot: "/checkout/desktop",
    isPackaged: false,
    resourcesPath: "/unused",
  });
  assert.equal(source.executable, process.env.SONAR_PYTHON || "python3");
  assert.equal(source.args[0], path.resolve("/checkout", "backend", "browser_controller.py"));

  const packaged = controllerCommand({
    appRoot: "/unused",
    isPackaged: true,
    resourcesPath: "/App/Contents/Resources",
  });
  assert.equal(
    packaged.executable,
    path.join("/App/Contents/Resources", "backend", process.platform === "win32" ? "SonarHeadUnitController.exe" : "SonarHeadUnitController"),
  );
  assert.deepEqual(packaged.args, []);
});

test("quotes shell arguments without allowing command substitution", () => {
  assert.equal(shellQuote("plain value"), "'plain value'");
  assert.equal(shellQuote("it's $(unsafe)"), "'it'\\''s $(unsafe)'");
});

test("builds an administrator launch without interpolating AppleScript", () => {
  const launch = elevatedLaunchSpec({
    platform: "darwin",
    executable: "/App Path/Controller",
    args: ["--auth-token", "secret value"],
    cwd: "/App Path",
    homePath: "/Users/test user",
    logPath: "/Users/test user/controller.log",
    uid: 501,
    gid: 20,
  });
  assert.equal(launch.executable, "/usr/bin/osascript");
  assert.equal(launch.args.at(-1), [
    "cd '/App Path' &&",
    "HOME='/Users/test user' SUDO_UID='501' SUDO_GID='20' PYTHONUNBUFFERED=1",
    "'/App Path/Controller' '--auth-token' 'secret value'",
    ">> '/Users/test user/controller.log' 2>&1",
  ].join(" "));
  assert.equal(
    launch.args[3],
    "do shell script (item 1 of argv) with administrator privileges",
  );
});

test("Linux authorization uses a GUI agent and preserves the invoking user", () => {
  const launch = elevatedLaunchSpec({
    platform: "linux", executable: "/App Path/Controller", args: ["--auth-token=secret"],
    cwd: "/App Path", homePath: "/home/test user", logPath: "/home/test user/log",
    uid: 1000, gid: 1001, lifetimePath: "/home/test user/alive", parentPid: process.pid,
  });
  assert.equal(launch.executable, "/usr/bin/pkexec");
  assert.deepEqual(launch.args.slice(0, 3), ["--disable-internal-agent", "/bin/sh", "-c"]);
  assert.match(launch.args[3], /exec \/usr\/bin\/env HOME='\/home\/test user' SUDO_UID='1000' SUDO_GID='1001'/);
  assert.match(launch.args[3], /SONAR_AUTHORIZED/);
  assert.match(launch.args[3], /trap cleanup EXIT/);
});

test("stages the complete AppImage runtime with working relative symlinks", {skip: process.platform === "win32" && "AppImage requires Unix permissions and symlinks"}, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-appimage-"));
  try {
    const source = path.join(directory, "mount", "backend");
    const userData = path.join(directory, "user");
    fs.mkdirSync(path.join(source, "_internal"), {recursive: true});
    fs.mkdirSync(userData);
    fs.writeFileSync(path.join(source, "SonarHeadUnitController"), "controller", {mode: 0o755});
    fs.writeFileSync(path.join(source, "_internal", "library.so"), "library");
    fs.symlinkSync("_internal/library.so", path.join(source, "library.so"));
    const staged = stageAppImageBackend(path.join(source, "SonarHeadUnitController"), userData);
    fs.rmSync(path.join(directory, "mount"), {recursive: true});
    assert.equal(fs.statSync(staged.directory).mode & 0o777, 0o700);
    assert.equal(fs.statSync(staged.executable).mode & 0o777, 0o755);
    assert.equal(fs.readlinkSync(path.join(path.dirname(staged.executable), "library.so")), "_internal/library.so");
    assert.equal(fs.readFileSync(path.join(path.dirname(staged.executable), "library.so"), "utf8"), "library");
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

for (const [code, message] of [[126, /authorization was cancelled/], [127, /desktop authentication agent/]]) {
  test(`Linux authorization failure ${code} cleans up the staged AppImage runtime`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-pkexec-"));
    const resources = path.join(directory, "resources");
    fs.mkdirSync(path.join(resources, "backend"), {recursive: true});
    fs.writeFileSync(path.join(resources, "backend", "SonarHeadUnitController"), "controller");
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    const backend = new ControllerBackend({
      platform: "linux", appRoot: directory, isPackaged: true, resourcesPath: resources,
      userDataPath: directory, homePath: directory, elevated: true, uid: 1000, gid: 1000, appImagePath: "/test.AppImage",
      spawnImpl(executable, args) {
        assert.equal(executable, "/usr/bin/pkexec");
        assert.ok(args.at(-1).includes(backend.runtimeDirectory));
        return child;
      },
    });
    try {
      const started = backend.start();
      const runtime = backend.runtimeDirectory;
      const lifetime = backend.lifetimePath;
      child.emit("close", code, null);
      await assert.rejects(started, message);
      assert.equal(fs.existsSync(runtime), false);
      assert.equal(fs.existsSync(lifetime), false);
    } finally { fs.rmSync(directory, {recursive: true, force: true}); }
  });
}

test("missing pkexec produces an actionable startup error", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-no-pkexec-"));
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const backend = new ControllerBackend({
    platform: "linux", appRoot: directory, isPackaged: false, userDataPath: directory,
    homePath: directory, elevated: true, uid: 1000, gid: 1000, spawnImpl: () => child,
  });
  try {
    const started = backend.start();
    child.emit("error", Object.assign(new Error("spawn ENOENT"), {code: "ENOENT"}));
    child.emit("close", -2, null);
    await assert.rejects(started, /Install your distribution's polkit package/);
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

test("discovers elevated controller readiness through its protected log", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-desktop-test-"));
  let child;
  const spawnImpl = () => {
    child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.kill = () => {};
    return child;
  };
  const backend = new ControllerBackend({
    platform: "darwin",
    appRoot: "/checkout/desktop",
    isPackaged: true,
    resourcesPath: "/App/Contents/Resources",
    userDataPath: directory,
    homePath: "/Users/test",
    elevated: true,
    uid: 501,
    gid: 20,
    authToken: "secret",
    spawnImpl,
    startupTimeoutMs: 1000,
  });
  try {
    const start = backend.start();
    const logPath = path.join(directory, "logs", "controller.log");
    fs.appendFileSync(
      logPath,
      "SONAR_CONTROLLER_READY "
        + '{"url":"http://127.0.0.1:49152/?token=secret","port":49152}\n',
    );
    assert.equal((await start).port, 49152);
    if (process.platform !== "win32") assert.equal(fs.statSync(logPath).mode & 0o777, 0o600);
  } finally {
    child.emit("close", 0, null);
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test("cancelling startup removes the lifetime file and stops only once", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-cancel-test-"));
  let child;
  const killed = [];
  const backend = new ControllerBackend({
    platform: "darwin", appRoot: "/checkout/desktop", isPackaged: true,
    resourcesPath: "/App/Contents/Resources", userDataPath: directory,
    homePath: "/Users/test", elevated: true, uid: 501, gid: 20,
    shutdownTimeoutMs: 0,
    spawnImpl() {
      child = new EventEmitter();
      child.stdout = new PassThrough();
      child.stderr = new PassThrough();
      child.kill = signal => { killed.push(signal); child.emit("close", 0, signal); };
      return child;
    },
  });
  try {
    const starting = assert.rejects(backend.start(), /startup was cancelled/);
    const lifetime = backend.lifetimePath;
    assert.ok(fs.existsSync(lifetime));
    await Promise.all([backend.stop(), backend.stop(), starting]);
    assert.equal(fs.existsSync(lifetime), false);
    assert.deepEqual(killed, ["SIGTERM"]);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

for (const cause of ["lifetime file removed", "desktop process exited"]) {
test(`launch supervisor finalizes the backend after ${cause}`, {timeout: 10000,
  skip: process.platform === "win32" && "Unix elevation supervisor requires /bin/sh"}, async () => {
  const {spawn} = require("node:child_process");
  const {once} = require("node:events");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sonar-supervisor-test-"));
  const lifetime = path.join(directory, "backend.alive");
  const ready = path.join(directory, "ready");
  const stopped = path.join(directory, "stopped");
  fs.writeFileSync(lifetime, "");
  const owner = cause === "desktop process exited"
    ? spawn(process.execPath, ["-e", "setInterval(() => {}, 100);"]) : null;
  const ownerClosed = owner ? once(owner, "close") : null;
  const launch = elevatedLaunchSpec({
    executable: process.execPath,
    args: ["-e", `const fs = require('node:fs');
      process.on('SIGTERM', () => { fs.writeFileSync(${JSON.stringify(stopped)}, 'finalized'); process.exit(0); });
      fs.writeFileSync(${JSON.stringify(ready)}, 'ready');
      setInterval(() => {}, 100);`],
    cwd: directory, homePath: directory, logPath: path.join(directory, "log"),
    uid: process.getuid(), gid: process.getgid(), lifetimePath: lifetime,
    parentPid: owner?.pid || process.pid,
  });
  const child = spawn("/bin/sh", ["-c", launch.args.at(-1)]);
  const closed = once(child, "close");
  try {
    const deadline = Date.now() + 4000;
    while (!fs.existsSync(ready) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.ok(fs.existsSync(ready), "backend reached its startup code");
    if (owner) {
      owner.kill("SIGTERM");
      await ownerClosed;
    } else {
      fs.unlinkSync(lifetime);
    }
    await closed;
    assert.equal(fs.readFileSync(stopped, "utf8"), "finalized");
    // A password prompt accepted after cancellation must not launch Python.
    fs.unlinkSync(ready);
    const late = spawn("/bin/sh", ["-c", launch.args.at(-1)]);
    assert.equal((await once(late, "close"))[0], 0);
    assert.equal(fs.existsSync(ready), false);
  } finally {
    child.kill("SIGTERM");
    owner?.kill("SIGTERM");
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
}

test("resolves the Windows packaged controller with its executable suffix", () => {
  const command = controllerCommand({appRoot: "/unused", isPackaged: true,
    resourcesPath: "/resources", platform: "win32"});
  assert.equal(command.executable, path.join("/resources", "backend", "SonarHeadUnitController.exe"));
  assert.deepEqual(command.args, []);
});
