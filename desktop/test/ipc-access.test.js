"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {requestIsFromStartup} = require("../src/ipc-access");

function request(url) {
  const mainFrame = {url};
  const webContents = {mainFrame};
  return {event: {sender: webContents, senderFrame: mainFrame}, webContents};
}

for (const startupURL of [
  "file:///C:/Program%20Files/OpenScope/resources/app.asar/src/startup.html",
  "file:///Applications/OpenScope.app/Contents/Resources/app.asar/src/startup.html",
  "file:///opt/OpenScope/resources/app.asar/src/startup.html",
]) {
  test(`accepts startup IPC with error query: ${startupURL}`, () => {
    const {event, webContents} = request(`${startupURL}?error=failed&logPath=C%3A%5Clogs#status`);
    assert.equal(requestIsFromStartup(event, webContents, startupURL), true);
  });
}

test("rejects other files, hosts, controller pages, and invalid URLs", () => {
  const startupURL = "file:///C:/Program%20Files/OpenScope/resources/app.asar/src/startup.html";
  for (const url of [
    startupURL.replace("startup.html", "other.html"),
    startupURL.replace("file:///", "file://other-host/"),
    "http://127.0.0.1:8765/",
    "not a URL",
  ]) {
    const {event, webContents} = request(url);
    assert.equal(requestIsFromStartup(event, webContents, startupURL), false);
  }
});

test("rejects other windows, subframes, and a closed main window", () => {
  const startupURL = "file:///C:/OpenScope/startup.html";
  const {event, webContents} = request(startupURL);
  assert.equal(requestIsFromStartup(event, {mainFrame: event.senderFrame}, startupURL), false);
  assert.equal(requestIsFromStartup({...event, senderFrame: {url: startupURL}}, webContents, startupURL), false);
  assert.equal(requestIsFromStartup(event, null, startupURL), false);
});
