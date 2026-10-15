"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {collectChromiumNotice, verifyLicenseFiles} = require("../scripts/verify-licenses");

function fixture(t, platform = "linux") {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "openscope-license-test-"));
  t.after(() => fs.rmSync(directory, {recursive:true, force:true}));
  const resources = platform === "darwin"
    ? path.join(directory, "OpenScope.app", "Contents", "Resources")
    : path.join(directory, "resources");
  const root = path.resolve(__dirname, "../..");
  fs.mkdirSync(path.join(resources,"licenses"), {recursive:true});
  for (const [source, target] of [
    ["LICENSE", "OpenScope-LICENSE"], ["AUTHORS.md", "AUTHORS.md"], ["THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"],
    ["docs/LICENSING.md", "LICENSING.md"], ["desktop/node_modules/electron/LICENSE", "Electron-LICENSE"],
  ]) fs.copyFileSync(path.join(root,source),path.join(resources,"licenses",target));
  fs.cpSync(path.join(root,"third_party"),path.join(resources,"licenses","reference"),{recursive:true});
  const notices = path.join(resources,"backend","THIRD_PARTY_LICENSES");
  fs.mkdirSync(notices,{recursive:true});
  fs.copyFileSync(path.join(root,"third_party/notices/pyserial-3.5-LICENSE.txt"),path.join(notices,"pyserial-LICENSE.txt"));
  const packages = fs.readFileSync(path.join(root,"backend/requirements.txt"),"utf8").trim().split(/\r?\n/).map(line => {
    const [name,version] = line.split("==");
    return {name,version,license_files:["pyserial-LICENSE.txt"]};
  });
  fs.writeFileSync(path.join(notices,"inventory.json"),JSON.stringify({packages}));
  fs.writeFileSync(path.join(notices,"native-inventory.json"),JSON.stringify({binaries:[],unresolved_notices:[]}));
  fs.writeFileSync(path.join(notices,"runtime-build.json"),JSON.stringify({python:"3.12.15 (test fixture)",ffmpeg:[]}));
  fs.writeFileSync(path.join(platform === "darwin" ? resources : directory,"LICENSES.chromium.html"),"Exact runtime Chromium notices");
  const context = {appOutDir:directory,electronPlatformName:platform,packager:{appInfo:{productFilename:"OpenScope"}}};
  return {directory,resources,notices,context};
}

for (const platform of ["linux","win32","darwin"]) {
  test(`${platform} packaging retains exact runtime Chromium notices with dependency licenses`, t => {
    const {resources,context} = fixture(t,platform);
    assert.equal(collectChromiumNotice(context),resources);
    assert.equal(fs.readFileSync(path.join(resources,"licenses/LICENSES.chromium.html"),"utf8"),"Exact runtime Chromium notices");
    assert.deepEqual(verifyLicenseFiles(resources,{strictNative:true}),[]);
  });
}

test("missing dependency notice prevents package verification", t => {
  const {resources,notices,context} = fixture(t);
  collectChromiumNotice(context);
  fs.unlinkSync(path.join(notices,"pyserial-LICENSE.txt"));
  assert.throws(() => verifyLicenseFiles(resources),/missing or empty/);
});

test("missing Chromium notices prevent packaging", t => {
  const {context,directory} = fixture(t);
  fs.unlinkSync(path.join(directory,"LICENSES.chromium.html"));
  assert.throws(() => collectChromiumNotice(context),/Chromium notices are missing/);
});

test("strict release check rejects unresolved native notices", t => {
  const {resources,notices,context} = fixture(t);
  collectChromiumNotice(context);
  fs.writeFileSync(path.join(notices,"native-inventory.json"),JSON.stringify({binaries:[],unresolved_notices:["vendor.dll"]}));
  assert.deepEqual(verifyLicenseFiles(resources),["vendor.dll"]);
  assert.throws(() => verifyLicenseFiles(resources,{strictNative:true}),/vendor.dll/);
});

test("packaging rejects a backend with old dependency pins", t => {
  const {resources,notices,context} = fixture(t);
  collectChromiumNotice(context);
  const file = path.join(notices,"inventory.json");
  const inventory = JSON.parse(fs.readFileSync(file,"utf8"));
  inventory.packages.find(item => item.name.toLowerCase() === "pillow").version = "11.3.0";
  fs.writeFileSync(file,JSON.stringify(inventory));
  assert.throws(() => verifyLicenseFiles(resources),/expected Pillow==12\.3\.0, found 11\.3\.0/);
});

for (const [description, runtime, error] of [
  ["old Python", {python:"3.12.9"}, /Python 3\.12\.15/],
  ["uninspected FFmpeg", {python:"3.12.15",opencv_build:"FFMPEG: YES",ffmpeg:[]}, /no native license inspection/],
  ["GPL FFmpeg", {python:"3.12.15",ffmpeg:[{file:"libavutil",configuration:"--enable-gpl --enable-version3",license:"GPL version 3 or later"}]}, /LGPL packaging policy/],
  ["GPL flags despite an LGPL label", {python:"3.12.15",ffmpeg:[{file:"libavutil",configuration:"--enable-gpl",license:"LGPL version 2.1 or later"}]}, /LGPL packaging policy/],
  ["nonfree FFmpeg", {python:"3.12.15",ffmpeg:[{file:"libavutil",configuration:"--enable-nonfree",license:"LGPL version 2.1 or later"}]}, /LGPL packaging policy/],
]) {
  test(`packaging rejects ${description}`, t => {
    const {resources,notices,context} = fixture(t);
    collectChromiumNotice(context);
    fs.writeFileSync(path.join(notices,"runtime-build.json"),JSON.stringify(runtime));
    assert.throws(() => verifyLicenseFiles(resources),error);
  });
}

test("inspected LGPL FFmpeg passes the configuration check", t => {
  const {resources,notices,context} = fixture(t);
  collectChromiumNotice(context);
  fs.writeFileSync(path.join(notices,"runtime-build.json"),JSON.stringify({
    python:"3.12.15",opencv_build:"FFMPEG: YES",
    ffmpeg:[{file:"libavutil",configuration:"--enable-shared",license:"LGPL version 2.1 or later"}],
  }));
  assert.deepEqual(verifyLicenseFiles(resources,{strictNative:true}),[]);
});

for (const platform of ["linux", "win32", "darwin"]) {
  test(`${platform} Conda OpenCV version is checked against its packaging profile`, t => {
    const {resources,notices,context} = fixture(t);
    collectChromiumNotice(context);
    const file = path.join(notices, "inventory.json");
    const inventory = JSON.parse(fs.readFileSync(file, "utf8"));
    inventory.packages.find(item => item.name === "opencv-python").version = "5.0.0";
    fs.writeFileSync(file, JSON.stringify(inventory));
    fs.writeFileSync(path.join(notices, "runtime-build.json"), JSON.stringify({
      python: "3.12.15", platform, packaging_profile: "conda",
    }));
    if (platform === "darwin") assert.throws(() => verifyLicenseFiles(resources), /opencv-python/);
    else assert.deepEqual(verifyLicenseFiles(resources, {strictNative: true}), []);
  });
}

test("strict check reads pending coverage even if the summary omits it", t => {
  const {resources,notices,context} = fixture(t);
  collectChromiumNotice(context);
  fs.writeFileSync(path.join(notices,"native-inventory.json"),JSON.stringify({
    binaries:[{path:"unknown.dylib",notice_coverage:"needs-review",license_files:[]}],unresolved_notices:[],
  }));
  assert.throws(() => verifyLicenseFiles(resources,{strictNative:true}),/unknown\.dylib/);
});
