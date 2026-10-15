"use strict";

function requestIsFromStartup(event, webContents, startupURL) {
  if (!webContents || event.sender !== webContents || event.senderFrame !== webContents.mainFrame) {
    return false;
  }
  try {
    const url = new URL(event.senderFrame.url);
    // Startup errors and log paths are passed in the query string.
    url.search = "";
    url.hash = "";
    return url.protocol === "file:" && url.href === startupURL;
  } catch (_error) {
    return false;
  }
}

module.exports = {requestIsFromStartup};
