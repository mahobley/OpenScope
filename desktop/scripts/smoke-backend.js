"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {ControllerBackend} = require("../src/backend");

async function smokeBackend(resourcesPath) {
  if (resourcesPath) resourcesPath = path.resolve(resourcesPath);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "openscope-smoke-"));
  let backend;
  try {
    for (let launch = 0; launch < 2; launch += 1) {
      backend = new ControllerBackend({appRoot: path.resolve(__dirname, ".."),
        isPackaged: Boolean(resourcesPath), resourcesPath, userDataPath: directory,
        elevated: false, authToken: "-startup-regression-token", startupTimeoutMs: 30000});
      const ready = await backend.start();
      const headers = {Authorization: `Bearer ${ready.token}`, "Content-Type": "application/json"};
      const defaults = await fetch(`${ready.origin}/api/defaults`, {headers}).then(response => response.json());
      assert.ok(defaults.build?.id && defaults.build?.version, "build identification missing");
      if (launch === 0) {
        const response = await fetch(`${ready.origin}/api/preferences`, {method: "POST", headers, body: JSON.stringify({theme: "light"})});
        assert.equal(response.status, 200);
      } else assert.equal(defaults.preferences.theme, "light", "settings did not survive restart");
      await backend.stop();
      assert.equal(backend.child, null, "backend did not stop cleanly");
    }
  } finally { await backend?.stop(); fs.rmSync(directory, {recursive: true, force: true}); }
}

if (require.main === module) smokeBackend(process.argv[2]).then(() => {
  console.log("Backend startup, authenticated API, persistent settings, restart and shutdown passed.");
}).catch(error => { console.error(error); process.exitCode = 1; });
module.exports = {smokeBackend};
