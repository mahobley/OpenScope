"use strict";

const fs = require("node:fs");
const path = require("node:path");

function requireFile(root, relative) {
  const file = path.resolve(root, relative);
  if (!file.startsWith(path.resolve(root) + path.sep)) {
    throw new Error(`Invalid license path: ${relative}`);
  }
  if (!fs.existsSync(file) || !fs.statSync(file).isFile() || fs.statSync(file).size === 0) {
    throw new Error(`Required license/notice file is missing or empty: ${relative}`);
  }
  return file;
}

function verifyLicenseFiles(resources, {strictNative = false} = {}) {
  for (const file of [
    "licenses/OpenScope-LICENSE", "licenses/AUTHORS.md", "licenses/THIRD_PARTY_NOTICES.md",
    "licenses/LICENSING.md", "licenses/Electron-LICENSE", "licenses/LICENSES.chromium.html",
    "licenses/reference/notices/pyserial-3.5-LICENSE.txt",
    ...["GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "Apache-2.0"]
      .map(name => `licenses/reference/licenses/${name}.txt`),
  ]) requireFile(resources, file);

  const notices = path.join(resources, "backend", "THIRD_PARTY_LICENSES");
  const inventory = JSON.parse(fs.readFileSync(requireFile(notices, "inventory.json"), "utf8"));
  const runtime = JSON.parse(fs.readFileSync(requireFile(notices, "runtime-build.json"), "utf8"));
  // Compare against source inputs, not metadata copied from an older backend.
  const requirements = fs.readFileSync(path.join(__dirname, "../../backend/requirements.txt"), "utf8");
  for (const line of requirements.split(/\r?\n/)) {
    const pin = line.trim().match(/^([\w.-]+)==([^\s;]+)$/);
    if (!pin) continue;
    const normalize = name => name.toLowerCase().replace(/[-_.]+/g, "-");
    const dependency = inventory.packages.find(item => normalize(item.name) === normalize(pin[1]));
    // Conda's OpenCV 5.0.0 packages use the upstream library version; .93 is
    // the separate PyPI wrapper revision. All other pins remain identical.
    const expected = pin[1] === "opencv-python" && runtime.packaging_profile === "conda" &&
      ["linux", "win32"].includes(runtime.platform) ? "5.0.0" : pin[2];
    if (!dependency || dependency.version !== expected) {
      throw new Error(`Stale or missing backend dependency: expected ${pin[1]}==${pin[2]}, found ${dependency?.version || "missing"}. Rebuild the backend from the current requirements.`);
    }
  }
  for (const dependency of inventory.packages) {
    if (dependency.name.toLowerCase() === "scapy") throw new Error("Scapy must not be included in new builds");
    if (!dependency.license_files.length) throw new Error(`Missing license notice for ${dependency.name}`);
    for (const file of dependency.license_files) requireFile(notices, file);
  }
  const python = (runtime.python || "").match(/^3\.12\.(\d+)(?:\s|$)/);
  if (!python || Number(python[1]) < 15) {
    throw new Error("Packaged backend requires Python 3.12.15 or a later 3.12 security release");
  }
  if (/FFMPEG:\s+YES/.test(runtime.opencv_build || "") && !runtime.ffmpeg?.length) {
    throw new Error("OpenCV enables FFmpeg but no native license inspection was recorded; review this platform's native runtime before packaging");
  }
  for (const ffmpeg of runtime.ffmpeg || []) {
    const flags = (ffmpeg.configuration || "").split(/\s+/);
    if (ffmpeg.inspection_error || !ffmpeg.license || !ffmpeg.configuration) {
      throw new Error(`FFmpeg license inspection incomplete: ${ffmpeg.file}`);
    }
    if (flags.includes("--enable-nonfree") || flags.includes("--enable-gpl") ||
        !/^LGPL\b/.test(ffmpeg.license)) {
      throw new Error(`FFmpeg configuration is outside the reviewed LGPL packaging policy: ${ffmpeg.file} (${ffmpeg.license}). Replace/rebuild the native dependency; copying license text does not clear this build.`);
    }
  }
  const native = JSON.parse(fs.readFileSync(requireFile(notices, "native-inventory.json"), "utf8"));
  for (const binary of native.binaries) {
    for (const file of binary.license_files) requireFile(notices, file);
  }
  const pending = [...new Set([...native.unresolved_notices,
    ...native.binaries.filter(binary => binary.notice_coverage !== "collected").map(binary => binary.path)])];
  if (strictNative && pending.length) {
    throw new Error(`Native notice review required: ${pending.join(", ")}`);
  }
  return pending;
}

function collectChromiumNotice(context) {
  const mac = context.electronPlatformName === "darwin";
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const resources = mac ? path.join(app, "Contents", "Resources") : path.join(context.appOutDir, "resources");
  const candidates = [
    path.join(resources, "LICENSES.chromium.html"),
    path.join(context.appOutDir, "LICENSES.chromium.html"),
    path.join(app, "Contents", "Frameworks", "Electron Framework.framework", "Versions", "A", "Resources", "LICENSES.chromium.html"),
  ];
  const source = candidates.find(file => fs.existsSync(file));
  if (!source) throw new Error("Exact Electron runtime Chromium notices are missing");
  fs.mkdirSync(path.join(resources, "licenses"), {recursive:true});
  fs.copyFileSync(source, path.join(resources, "licenses", "LICENSES.chromium.html"));
  return resources;
}

if (require.main === module) {
  try {
    if (!process.argv[2]) throw new Error("Usage: node scripts/verify-licenses.js <resources-directory> [--strict-native]");
    const pending = verifyLicenseFiles(path.resolve(process.argv[2]), {strictNative:process.argv.includes("--strict-native")});
    console.log(`License files verified; ${pending.length} native notice entries need review. Corresponding-source review is separate.`);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

module.exports = {verifyLicenseFiles, collectChromiumNotice};
