"use strict";

const fs = require("node:fs");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const {UNUSED_PRIVACY_KEYS} = require("./after-pack");
const {smokeBackend} = require("./smoke-backend");
const {verifyLicenseFiles} = require("./verify-licenses");

function fail(message) {
  throw new Error(`Packaged app verification failed: ${message}`);
}

function plistValue(plist, key) {
  const result = spawnSync("plutil", ["-extract", key, "raw", plist], {
    encoding: "utf8",
  });
  return result.status === 0 ? result.stdout.trim() : null;
}

async function verifyPackage(appPath) {
  const contents = path.join(appPath, "Contents");
  const resources = path.join(contents, "Resources");
  verifyLicenseFiles(resources, {strictNative: true});
  const backend = path.join(resources, "backend");
  const executables = [
    "SonarHeadUnitController",
    "SonarHeadUnitEngine",
    "SonarHeadUnitViewer",
  ];
  if (!fs.existsSync(path.join(resources, "app.asar"))) {
    fail("app.asar is missing");
  }
  for (const name of executables) {
    const executable = path.join(backend, name);
    try {
      fs.accessSync(executable, fs.constants.X_OK);
    } catch (_error) {
      fail(`${name} is missing or is not executable`);
    }
  }

  const controller = path.join(backend, "SonarHeadUnitController");
  const help = spawnSync(controller, ["--help"], {
    cwd: backend,
    encoding: "utf8",
    timeout: 30000,
  });
  if (help.error || help.status !== 0) {
    fail(`embedded controller --help exited with ${help.status}`);
  }
  for (const option of ["--auth-token", "--viewer-port", "--recordings-root", "--log-file"]) {
    if (!help.stdout.includes(option)) fail(`controller does not support ${option}`);
  }
  const engine = spawnSync(path.join(backend, "SonarHeadUnitEngine"), ["--help"], {
    cwd: backend, encoding: "utf8", timeout: 30000,
  });
  if (engine.error || engine.status !== 0) fail("embedded engine --help failed");
  for (const option of ["--network-state", "--restore-network"]) {
    if (!engine.stdout.includes(option)) fail(`engine does not support ${option}`);
  }
  const version = spawnSync(controller, ["--version"], {
    cwd: backend,
    encoding: "utf8",
    timeout: 30000,
  });
  if (version.status !== 0 || !version.stdout.includes("Python 3.12.")) {
    fail(`embedded controller is not using Python 3.12: ${version.stdout.trim()}`);
  }

  if (process.platform === "darwin") {
    const plist = path.join(contents, "Info.plist");
    const icon = plistValue(plist, "CFBundleIconFile");
    if (!icon || icon === "electron.icns") fail("custom application icon is missing");
    if (!fs.existsSync(path.join(resources, icon))) {
      fail(`application icon ${icon} is not present in Resources`);
    }
    const arbitraryLoads = plistValue(
      plist,
      "NSAppTransportSecurity.NSAllowsArbitraryLoads",
    );
    if (arbitraryLoads !== "false") fail("arbitrary network loads are enabled");
    for (const key of UNUSED_PRIVACY_KEYS) {
      if (plistValue(plist, key) !== null) fail(`unused privacy key ${key} remains`);
    }
  }
  await smokeBackend(resources);
}

const defaultApp = path.join(
  __dirname,
  "..",
  "..",
  "dist",
  "mac-arm64",
  "OpenScope.app",
);
const appPath = path.resolve(process.argv[2] || defaultApp);
if (require.main === module) verifyPackage(appPath).then(() => {
  console.log(`Verified packaged app: ${appPath}`);
}).catch(error => { console.error(error); process.exitCode = 1; });

module.exports = {verifyPackage};
