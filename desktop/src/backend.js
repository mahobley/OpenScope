"use strict";

const {EventEmitter} = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {spawn} = require("node:child_process");

const READY_PREFIX = "SONAR_CONTROLLER_READY ";
const AUTHORIZED_MARKER = "SONAR_AUTHORIZED";
const MAX_LOG_BYTES = 2 * 1024 * 1024;

function trimLegacyLog(logPath) {
  let descriptor;
  try {
    descriptor = fs.openSync(logPath, "r+");
    const size = fs.fstatSync(descriptor).size;
    if (size <= MAX_LOG_BYTES) return;
    const tail = Buffer.alloc(MAX_LOG_BYTES);
    const read = fs.readSync(descriptor, tail, 0, tail.length, size - tail.length);
    // Retaining the middle of a line can detach a secret from its token label.
    const newline = tail.subarray(0, read).indexOf(10);
    const safeTail = newline < 0 ? Buffer.alloc(0) : tail.subarray(newline + 1, read);
    fs.ftruncateSync(descriptor, 0);
    fs.writeSync(descriptor, safeTail, 0, safeTail.length, 0);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}
const APPLESCRIPT_ADMIN_RUNNER = [
  "-e", "on run argv",
  "-e", "do shell script (item 1 of argv) with administrator privileges",
  "-e", "end run",
];

function parseReadyLine(line) {
  if (!line.startsWith(READY_PREFIX)) return null;
  const value = JSON.parse(line.slice(READY_PREFIX.length));
  const url = new URL(value.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1") {
    throw new Error("Controller announced a non-loopback URL");
  }
  if (!Number.isInteger(value.port) || Number(url.port) !== value.port) {
    throw new Error("Controller readiness port does not match its URL");
  }
  const token = url.searchParams.get("token");
  if (!token) throw new Error("Controller readiness URL has no token");
  return {url: value.url, origin: url.origin, port: value.port, token};
}

function controllerCommand({appRoot, isPackaged, resourcesPath, platform = process.platform}) {
  const override = process.env.SONAR_CONTROLLER_EXECUTABLE;
  if (override) return {executable: override, args: []};
  if (isPackaged) {
    return {
      executable: path.join(resourcesPath, "backend", platform === "win32" ? "SonarHeadUnitController.exe" : "SonarHeadUnitController"),
      args: [],
    };
  }
  const repositoryRoot = path.resolve(appRoot, "..");
  return {
    executable: process.env.SONAR_PYTHON || "python3",
    args: [path.join(repositoryRoot, "backend", "browser_controller.py")],
  };
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'\\''`)}'`;
}

function elevatedLaunchSpec({executable, args, cwd, homePath, logPath, uid, gid, lifetimePath, parentPid,
  platform = process.platform}) {
  const environment = [
    `HOME=${shellQuote(homePath)}`,
    `SUDO_UID=${shellQuote(uid)}`,
    `SUDO_GID=${shellQuote(gid)}`,
    "PYTHONUNBUFFERED=1",
  ].join(" ");
  let command = [
    "cd", shellQuote(cwd), "&&",
    ...(lifetimePath ? ["exec", "/usr/bin/env"] : []), environment,
    shellQuote(executable),
    ...args.map(shellQuote),
    ">>", shellQuote(logPath), "2>&1",
  ].join(" ");
  if (lifetimePath) {
    // This supervisor runs with the original authorization. It also covers
    // cancellation during Python imports, before the HTTP endpoint exists.
    const alive = `test -f ${shellQuote(lifetimePath)} && kill -0 ${Number(parentPid)} 2>/dev/null`;
    command = [
      `${alive} || exit 0`,
      `echo ${AUTHORIZED_MARKER} >> ${shellQuote(logPath)}`,
      `( ${command} ) & backend_pid=$!`,
      'cleanup() {',
      'kill -TERM "$backend_pid" 2>/dev/null || true',
      'remaining=20',
      'while kill -0 "$backend_pid" 2>/dev/null && test "$remaining" -gt 0; do /bin/sleep 1; remaining=$((remaining - 1)); done',
      'kill -KILL "$backend_pid" 2>/dev/null || true',
      '}',
      "trap cleanup EXIT",
      "trap 'exit 0' HUP INT TERM",
      `while kill -0 "$backend_pid" 2>/dev/null && ${alive}; do /bin/sleep 1; done`,
      `${alive} || exit 0`,
      'wait "$backend_pid"; result=$?; trap - EXIT; exit "$result"',
    ].join("\n");
  }
  if (platform === "linux") {
    return {
      executable: "/usr/bin/pkexec",
      // A GUI authentication agent is required; never wait for a password on
      // the launcher's unavailable terminal. pkexec supplies a clean environment.
      args: ["--disable-internal-agent", "/bin/sh", "-c", command],
    };
  }
  if (platform !== "darwin") throw new Error(`Backend elevation is unsupported on ${platform}`);
  return {
    executable: "/usr/bin/osascript",
    args: [...APPLESCRIPT_ADMIN_RUNNER, command],
  };
}

function stageAppImageBackend(executable, userDataPath) {
  // Root cannot necessarily access a user's FUSE mount. Copy the complete
  // PyInstaller runtime before authorization, preserving relative symlinks.
  const directory = fs.mkdtempSync(path.join(userDataPath, "backend-runtime-"));
  try {
    const runtime = path.join(directory, "backend");
    fs.cpSync(path.dirname(executable), runtime, {recursive: true, verbatimSymlinks: true});
    return {directory, executable: path.join(runtime, path.basename(executable))};
  } catch (error) {
    fs.rmSync(directory, {recursive: true, force: true});
    throw error;
  }
}

class ControllerBackend extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.child = null;
    this.ready = null;
    this.logStream = null;
    this.stopLogFollower = null;
    this.lastErrorLine = null;
    this.lifetimePath = null;
    this.stopping = null;
    this.authorized = false;
    this.cancelStart = null;
    this.runtimeDirectory = null;
  }

  start() {
    if (this.child) throw new Error("Controller backend is already running");
    this.ready = null;
    this.lastErrorLine = null;
    this.stopping = null;
    let {executable, args} = controllerCommand(this.options);
    const platform = this.options.platform ?? process.platform;
    const recordingsPath = path.join(this.options.userDataPath, "recordings");
    const logsPath = path.join(this.options.userDataPath, "logs");
    fs.mkdirSync(recordingsPath, {recursive: true});
    fs.mkdirSync(logsPath, {recursive: true});
    const logPath = path.join(logsPath, "controller.log");
    for (const name of ["controller.log", "controller.log.1", "controller.log.2",
      "controller.log.3", "controller.log.4", "launcher.log", "launcher.log.1"]) {
      trimLegacyLog(path.join(logsPath, name));
    }
    // Python owns size rotation; preserve the previous launch for diagnostics.
    fs.closeSync(fs.openSync(logPath, "a", 0o600));
    fs.chmodSync(logPath, 0o600);
    const launchLog = path.join(logsPath, "launcher.log");
    if (fs.existsSync(launchLog)) fs.renameSync(launchLog, `${launchLog}.1`);
    fs.writeFileSync(launchLog, "", {mode: 0o600});
    this.lifetimePath = path.join(logsPath, `backend-${crypto.randomUUID()}.alive`);
    fs.writeFileSync(this.lifetimePath, "", {mode: 0o600, flag: "wx"});
    const authToken = this.options.authToken
      || crypto.randomBytes(32).toString("base64url");
    const commandArgs = [
      ...args,
      "--host", "127.0.0.1",
      "--port", "0",
      "--viewer-port", "0",
      "--recordings-root", recordingsPath,
      `--auth-token=${authToken}`,
      "--log-file", logPath,
      "--desktop-lifetime-file", this.lifetimePath,
      "--desktop-parent-pid", String(process.pid),
    ];
    if (platform === "linux" && this.options.elevated && this.options.isPackaged
      && (this.options.appImagePath ?? process.env.APPIMAGE)
      && !process.env.SONAR_CONTROLLER_EXECUTABLE) {
      try {
        const staged = stageAppImageBackend(executable, this.options.userDataPath);
        this.runtimeDirectory = staged.directory;
        executable = staged.executable;
      } catch (error) {
        this.removeLifetimeFile();
        throw error;
      }
    }
    const cwd = this.options.isPackaged
      ? path.dirname(executable)
      : path.resolve(this.options.appRoot, "..", "backend");

    return new Promise((resolve, reject) => {
      let settled = false;
      let stdoutBuffer = "";
      let timer = null;
      const finish = (error, readiness) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        this.cancelStart = null;
        if (error) reject(error);
        else resolve(readiness);
      };
      this.cancelStart = () => finish(new Error("OpenScope startup was cancelled"));
      const armStartupTimeout = () => {
        if (settled || timer) return;
        this.authorized = true;
        this.emit("phase", "Starting the local connection…");
        timer = setTimeout(() => finish(new Error(
          "Timed out starting OpenScope. Use Retry to try again.",
        )), this.options.startupTimeoutMs ?? 30000);
      };
      const consume = chunk => {
        stdoutBuffer += chunk.toString();
        const lines = stdoutBuffer.split(/\r?\n/);
        stdoutBuffer = lines.pop();
        for (const line of lines) {
          armStartupTimeout();
          this.emit("log", line);
          try {
            const readiness = parseReadyLine(line);
            if (readiness) {
              if (readiness.token !== authToken) {
                throw new Error("Controller readiness token does not match");
              }
              this.ready = readiness;
              if (this.stopping) return;
              finish(null, readiness);
            }
          } catch (error) {
            finish(error);
          }
        }
      };
      const stopControllerLog = this.followLog(logPath, consume, {fromEnd: true});
      const spawnImpl = this.options.spawnImpl || spawn;
      if (this.options.elevated) {
        const launch = elevatedLaunchSpec({
          executable,
          args: commandArgs,
          cwd,
          homePath: this.options.homePath,
          logPath: launchLog,
          uid: this.options.uid ?? process.getuid(),
          gid: this.options.gid ?? process.getgid(),
          lifetimePath: this.lifetimePath,
          parentPid: process.pid,
          platform,
        });
        this.child = spawnImpl(launch.executable, launch.args, {
          env: process.env,
          stdio: ["ignore", "pipe", "pipe"],
        });
      } else {
        this.logStream = fs.createWriteStream(launchLog, {flags: "a"});
        this.child = spawnImpl(executable, commandArgs, {
          cwd,
          env: {...process.env, PYTHONUNBUFFERED: "1"},
          stdio: ["ignore", "pipe", "pipe"],
        });
        this.child.stdout.on("data", chunk => {
          this.logStream?.write(chunk);
          consume(chunk);
        });
      }
      const stopLauncherLog = this.options.elevated
        ? this.followLog(launchLog, chunk => {
          const contents = chunk.toString();
          if (contents.includes(AUTHORIZED_MARKER)) armStartupTimeout();
          const detail = contents.replaceAll(AUTHORIZED_MARKER, "").trim();
          if (detail) {
            this.lastErrorLine = detail;
            this.emit("log", detail);
          }
        }) : () => {};
      this.stopLogFollower = () => { stopControllerLog(); stopLauncherLog(); };
      this.child.stdout.on("data", chunk => {
        if (this.options.elevated) this.emit("log", chunk.toString().trimEnd());
      });
      this.child.stderr.on("data", chunk => {
        const line = chunk.toString().trimEnd();
        this.lastErrorLine = line;
        this.logStream?.write(chunk);
        this.emit("log", line);
      });
      this.child.once("error", error => finish(
        platform === "linux" && this.options.elevated && error.code === "ENOENT"
          ? new Error("Linux backend authorization requires /usr/bin/pkexec. Install your distribution's polkit package and a desktop authentication agent.")
          : error,
      ));
      this.child.once("close", (code, signal) => {
        this.removeLifetimeFile();
        this.stopLogFollower?.();
        this.stopLogFollower = null;
        this.child = null;
        this.ready = null;
        this.logStream?.end();
        this.logStream = null;
        if (this.runtimeDirectory) {
          fs.rmSync(this.runtimeDirectory, {recursive: true, force: true});
          this.runtimeDirectory = null;
        }
        this.emit("exit", {code, signal});
        const detail = this.lastErrorLine ? `: ${this.lastErrorLine}` : "";
        if (platform === "linux" && this.options.elevated && !this.authorized) {
          if (code === 126) return finish(new Error("Administrator authorization was cancelled. Use Retry to try again."));
          if (code === 127) return finish(new Error(
            `Could not authorize the Linux backend. Check that polkit and a desktop authentication agent are running${detail}`,
          ));
        }
        finish(new Error(
          `Controller exited before readiness (status ${code})${detail}`,
        ));
      });
      if (this.options.elevated) {
        this.emit("phase", "Waiting for administrator authorization…");
      } else {
        armStartupTimeout();
      }
    });
  }

  followLog(logPath, consume, {fromEnd = false} = {}) {
    let descriptor = fs.openSync(logPath, "r");
    let offset = fromEnd ? fs.fstatSync(descriptor).size : 0;
    const poll = () => {
      try {
        // Keep the descriptor across rotation so unread bytes in the old log
        // are consumed before switching to the newly created file.
        for (let attempt = 0; attempt < 2; attempt += 1) {
          const stat = fs.fstatSync(descriptor);
          if (stat.size < offset) offset = 0;
          const length = Math.min(256 * 1024, stat.size - offset);
          if (length > 0) {
            const contents = Buffer.alloc(length);
            const read = fs.readSync(descriptor, contents, 0, length, offset);
            offset += read;
            consume(contents.subarray(0, read));
          }
          if (offset < stat.size || fs.statSync(logPath).ino === stat.ino) break;
          const next = fs.openSync(logPath, "r");
          fs.closeSync(descriptor);
          descriptor = next;
          offset = 0;
        }
      } catch (error) {
        if (error.code !== "ENOENT") {
          this.emit("log", `Could not read controller log: ${error.message}`);
        }
      }
    };
    const timer = setInterval(poll, 100);
    return () => {
      clearInterval(timer);
      poll();
      fs.closeSync(descriptor);
    };
  }

  async stop() {
    if (this.stopping) return this.stopping;
    this.stopping = this.stopBackend();
    return this.stopping;
  }

  removeLifetimeFile() {
    if (!this.lifetimePath) return;
    fs.rmSync(this.lifetimePath, {force: true});
    this.lifetimePath = null;
  }

  async stopBackend() {
    this.removeLifetimeFile();
    this.cancelStart?.();
    const child = this.child;
    if (!child) return;
    if (this.options.elevated && !this.authorized) {
      child.kill("SIGTERM");
      if (await this.waitForExit(1000)) return;
    }
    if (this.ready) {
      try {
        await this.requestShutdown();
      } catch (error) {
        this.emit("log", `Graceful shutdown failed: ${error.message}`);
      }
    }
    // Allow capture to finish its current write and finalize the recordings.
    if (await this.waitForExit(this.options.shutdownTimeoutMs ?? 22000)) return;
    child.kill("SIGTERM");
    if (await this.waitForExit(3000)) return;
    child.kill("SIGKILL");
    await this.waitForExit(1000);
  }

  requestShutdown() {
    const {port, token} = this.ready;
    return new Promise((resolve, reject) => {
      const request = http.request({
        hostname: "127.0.0.1",
        port,
        path: "/api/shutdown",
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "Content-Length": 2,
        },
        timeout: 2000,
      }, response => {
        response.resume();
        if (response.statusCode === 202) resolve();
        else reject(new Error(`Shutdown returned HTTP ${response.statusCode}`));
      });
      request.on("timeout", () => request.destroy(new Error("Shutdown timed out")));
      request.on("error", reject);
      request.end("{}");
    });
  }

  waitForExit(timeoutMs) {
    if (!this.child) return Promise.resolve(true);
    return new Promise(resolve => {
      const timer = setTimeout(() => {
        this.removeListener("exit", onExit);
        resolve(false);
      }, timeoutMs);
      const onExit = () => {
        clearTimeout(timer);
        resolve(true);
      };
      this.once("exit", onExit);
    });
  }
}

module.exports = {
  ControllerBackend,
  READY_PREFIX,
  controllerCommand,
  elevatedLaunchSpec,
  parseReadyLine,
  shellQuote,
  stageAppImageBackend,
};
