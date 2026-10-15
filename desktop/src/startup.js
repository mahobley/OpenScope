"use strict";

const params = new URLSearchParams(window.location.search);
const error = params.get("error");
if (error) {
  document.querySelector("#cancel-startup").hidden = true;
  document.querySelector(".mark").remove();
  document.querySelector("#title").textContent = "OpenScope could not start";
  document.querySelector("#detail").textContent = error;
  const logPath = document.querySelector("#log-path");
  logPath.textContent = `Diagnostic log: ${params.get("logPath")}`;
  logPath.hidden = false;
  document.querySelector("#actions").hidden = false;
}
window.sonarDesktop.onStartupPhase(message => {
  document.querySelector("#title").textContent = message;
});
document.querySelector("#retry").addEventListener("click", async event => {
  event.target.disabled = true;
  try { await window.sonarDesktop.retry(); }
  catch (error) { document.querySelector("#detail").textContent = error.message; }
  finally { event.target.disabled = false; }
});
document.querySelector("#cancel-startup").addEventListener("click", async event => {
  event.target.disabled = true;
  await window.sonarDesktop.cancelStartup();
});

document.querySelector("#show-log").addEventListener("click", () => {
  void window.sonarDesktop.showLog();
});
document.querySelector("#export-diagnostics").addEventListener("click", async () => {
  try { await window.sonarDesktop.exportDiagnostics(); }
  catch (error) { document.querySelector("#detail").textContent = error.message; }
});
