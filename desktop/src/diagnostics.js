"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

function redact(text) {
  return text.replace(/([?&]token=)[^\s"&]+/gi, "$1[redacted]")
    .replace(/(Bearer\s+)[^\s"]+/gi, "$1[redacted]")
    .replace(/(sonar_controller_token=)[^;\s]+/gi, "$1[redacted]");
}

function collectDiagnostics(logsPath, version) {
  const logs = {};
  for (const name of ["controller.log", "controller.log.1", "controller.log.2",
    "controller.log.3", "controller.log.4", "launcher.log", "launcher.log.1"]) {
    let fd;
    try {
      fd = fs.openSync(path.join(logsPath, name), "r");
      const size = fs.fstatSync(fd).size;
      const buffer = Buffer.alloc(Math.min(size, 2 * 1024 * 1024));
      const read = fs.readSync(fd, buffer, 0, buffer.length, Math.max(0, size - buffer.length));
      let contents = buffer.subarray(0, read).toString("utf8");
      // A bounded tail may begin inside a credential. Discard that partial line.
      if (size > buffer.length) {
        const newline = contents.indexOf("\n");
        contents = newline < 0 ? "" : contents.slice(newline + 1);
      }
      logs[name] = redact(contents).slice(-2 * 1024 * 1024);
    } catch (error) {
      if (error.code !== "ENOENT") logs[name] = `Could not read log: ${error.message}`;
    } finally {
      if (fd !== undefined) fs.closeSync(fd);
    }
  }
  return {version, exportedAt: new Date().toISOString(), platform: process.platform,
    architecture: process.arch, osRelease: os.release(), versions: process.versions, logs};
}

module.exports = {collectDiagnostics, redact};
