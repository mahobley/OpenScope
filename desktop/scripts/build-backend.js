"use strict";

const path = require("node:path");
const fs = require("node:fs");
const {spawnSync} = require("node:child_process");
const target = process.argv[2];
const expected = {mac: "darwin", win: "win32", linux: "linux"};
if (expected[target] !== process.platform) {
  console.error(`Build ${target} on its native OS (host: ${process.platform}).`);
  process.exit(1);
}
const root = path.resolve(__dirname, "../..");
const override = process.env.SONAR_BUILD_PYTHON;
const profileFile = path.join(root, target === "mac" ? "build/native-security/native-runtime.json" : "build/native-runtime.json");
const profile = fs.existsSync(profileFile) ? JSON.parse(fs.readFileSync(profileFile, "utf8")) : null;
const localPython = profile ? path.resolve(path.dirname(profileFile), profile.interpreter.path) : null;
const executable = override || (localPython && fs.existsSync(localPython) ? localPython :
  (process.platform === "win32" ? "py" : "python3.12"));
const prefix = !override && executable === "py" ? ["-3.12"] : [];
const result = spawnSync(executable, [...prefix, path.join(root, "scripts/build_backend.py"), "--target", target], {cwd: root, stdio: "inherit"});
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
