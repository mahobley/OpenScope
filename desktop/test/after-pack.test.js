"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {UNUSED_PRIVACY_KEYS} = require("../scripts/after-pack");

test("removes privacy prompts for capabilities the renderer cannot request", () => {
  assert.deepEqual(UNUSED_PRIVACY_KEYS, [
    "NSAudioCaptureUsageDescription",
    "NSBluetoothAlwaysUsageDescription",
    "NSBluetoothPeripheralUsageDescription",
    "NSCameraUsageDescription",
    "NSMicrophoneUsageDescription",
  ]);
});
