"use strict";

const path = require("node:path");
const {pathToFileURL} = require("node:url");
const {app, BrowserWindow, dialog, ipcMain, session, shell} = require("electron");
const fs = require("node:fs/promises");
const {collectDiagnostics} = require("./diagnostics");
const {ControllerBackend} = require("./backend");
const {requestIsFromStartup: isStartupRequest} = require("./ipc-access");

app.setName("OpenScope");
if (process.platform === "linux") app.setDesktopName("OpenScope.desktop");
if (process.platform === "win32") app.setAppUserModelId("com.sonarheadunit.desktop");

const STARTUP_FILE = path.join(__dirname, "startup.html");
const STARTUP_URL = pathToFileURL(STARTUP_FILE).href;
const APP_ICON = path.join(__dirname, "..", "assets", "icon-master.png");
let mainWindow = null;
let backend = null;
let backendOrigin = null;
let backendStarting = false;
let shutdownStarted = false;
let allowQuit = false;

function logPath() {
  return path.join(app.getPath("userData"), "logs", "controller.log");
}

function loadStartup(query = {}) {
  if (!mainWindow || mainWindow.isDestroyed()) return Promise.resolve();
  return mainWindow.loadFile(STARTUP_FILE, {query});
}

function createWindow() {
  mainWindow = new BrowserWindow({
    title: "OpenScope",
    icon: APP_ICON,
    width: 1280,
    height: 900,
    minWidth: 900,
    minHeight: 650,
    backgroundColor: "#061012",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
      sandbox: true,
      webSecurity: true,
    },
  });
  mainWindow.removeMenu();
  mainWindow.webContents.setWindowOpenHandler(() => ({action: "deny"}));
  const enforceNavigationPolicy = (event, target) => {
    try {
      if (backendOrigin && new URL(target).origin === backendOrigin) return;
    } catch (_error) {
      // Invalid URLs are rejected below.
    }
    event.preventDefault();
  };
  mainWindow.webContents.on("will-navigate", enforceNavigationPolicy);
  mainWindow.webContents.on("will-redirect", enforceNavigationPolicy);
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
    app.quit();
  });
  return loadStartup();
}

async function startBackend() {
  if (backendStarting) return;
  backendStarting = true;
  backendOrigin = null;
  const userDataPath = app.getPath("userData");
  const instance = new ControllerBackend({
    appRoot: app.getAppPath(),
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    userDataPath,
    homePath: app.getPath("home"),
    elevated: ["darwin", "linux"].includes(process.platform)
      && process.env.SONAR_SKIP_ELEVATION !== "1",
  });
  backend = instance;
  instance.on("phase", message => mainWindow?.webContents.send("desktop:startup-phase", message));
  instance.on("exit", ({code}) => {
    if (shutdownStarted || backend !== instance || !backendOrigin) return;
    backendOrigin = null;
    void loadStartup({
      error: `OpenScope stopped unexpectedly (status ${code}).`,
      logPath: logPath(),
    });
  });
  try {
    const readiness = await instance.start();
    if (shutdownStarted || backend !== instance) return;
    backendOrigin = readiness.origin;
    if (mainWindow && !mainWindow.isDestroyed()) {
      await mainWindow.loadURL(readiness.url);
    }
  } catch (error) {
    await instance.stop();
    if (backend === instance) backend = null;
    if (!shutdownStarted) await loadStartup({error: error.message, logPath: logPath()});
  } finally {
    backendStarting = false;
  }
}

function requestIsFromStartup(event) {
  return isStartupRequest(event, mainWindow?.webContents, STARTUP_URL);
}

ipcMain.handle("desktop:show-log", event => {
  if (!requestIsFromStartup(event)) throw new Error("Log access is not allowed here");
  shell.showItemInFolder(logPath());
});

ipcMain.handle("desktop:retry", async event => {
  if (!requestIsFromStartup(event)) throw new Error("Retry is only available on the startup screen");
  if (backendStarting || shutdownStarted) return false;
  await backend?.stop();
  await loadStartup();
  void startBackend();
  return true;
});

ipcMain.handle("desktop:cancel-startup", async event => {
  if (!requestIsFromStartup(event)) throw new Error("Cancellation is only available on the startup screen");
  await backend?.stop();
});

ipcMain.handle("desktop:export-diagnostics", async event => {
  if (event.sender !== mainWindow?.webContents || event.senderFrame !== event.sender.mainFrame) {
    throw new Error("Diagnostic access is not allowed here");
  }
  if (!requestIsFromStartup(event) && new URL(event.senderFrame.url).origin !== backendOrigin) {
    throw new Error("Diagnostic access is not allowed here");
  }
  const result = await dialog.showSaveDialog(mainWindow, {
    title: "Export OpenScope diagnostics",
    defaultPath: `OpenScope-diagnostics-${new Date().toISOString().slice(0, 10)}.json`,
    filters: [{name: "Diagnostics", extensions: ["json"]}],
  });
  if (result.canceled) return false;
  const report = collectDiagnostics(path.dirname(logPath()), app.getVersion());
  if (backend?.ready) {
    try {
      const response = await fetch(`${backend.ready.origin}/api/defaults`, {
        headers: {Authorization: `Bearer ${backend.ready.token}`}, signal: AbortSignal.timeout(3000),
      });
      if (response.ok) report.build = (await response.json()).build;
    } catch (_error) { /* Logs remain available when the backend is unresponsive. */ }
  }
  await fs.writeFile(result.filePath, JSON.stringify(report, null, 2), {mode: 0o600});
  return true;
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
  app.whenReady().then(async () => {
    if (process.platform === "darwin") app.dock.setIcon(APP_ICON);
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
      callback(false);
    });
    await createWindow();
    void startBackend();
  });
}

app.on("window-all-closed", () => app.quit());
app.on("before-quit", event => {
  if (allowQuit) return;
  event.preventDefault();
  if (shutdownStarted) return;
  shutdownStarted = true;
  Promise.resolve(backend?.stop()).finally(() => {
    backend = null;
    allowQuit = true;
    app.quit();
  });
});
