"use strict";

const {contextBridge, ipcRenderer} = require("electron");

contextBridge.exposeInMainWorld("sonarDesktop", {
  showLog: () => ipcRenderer.invoke("desktop:show-log"),
  exportDiagnostics: () => ipcRenderer.invoke("desktop:export-diagnostics"),
  retry: () => ipcRenderer.invoke("desktop:retry"),
  cancelStartup: () => ipcRenderer.invoke("desktop:cancel-startup"),
  onStartupPhase: callback => ipcRenderer.on("desktop:startup-phase", (_event, message) => callback(message)),
});
