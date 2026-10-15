"use strict";

const {execFileSync} = require("node:child_process");
const path = require("node:path");
const {collectChromiumNotice, verifyLicenseFiles} = require("./verify-licenses");

const UNUSED_PRIVACY_KEYS = [
  "NSAudioCaptureUsageDescription",
  "NSBluetoothAlwaysUsageDescription",
  "NSBluetoothPeripheralUsageDescription",
  "NSCameraUsageDescription",
  "NSMicrophoneUsageDescription",
];

module.exports = async context => {
  const resources = collectChromiumNotice(context);
  verifyLicenseFiles(resources, {strictNative: true});
  if (context.electronPlatformName !== "darwin") return;
  const appName = `${context.packager.appInfo.productFilename}.app`;
  const plist = path.join(context.appOutDir, appName, "Contents", "Info.plist");
  const edit = command => execFileSync(
    "/usr/libexec/PlistBuddy",
    ["-c", command, plist],
    {stdio: "ignore"},
  );

  edit("Set :NSAppTransportSecurity:NSAllowsArbitraryLoads false");
  for (const key of UNUSED_PRIVACY_KEYS) edit(`Delete :${key}`);
};

module.exports.UNUSED_PRIVACY_KEYS = UNUSED_PRIVACY_KEYS;
