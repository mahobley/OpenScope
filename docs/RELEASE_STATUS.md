# Release preparation checkpoint — updated 2026-10-15

The maintainer confirmed on 2026-10-15 that all offered packaged apps have been
tested with sonar hardware: macOS Apple Silicon DMG, Windows x64 EXE, and Linux
x64 DEB/AppImage. Linux has the same beta status as the other platforms. Installers
are unsigned and request administrator rights for networking. Sonar disable and
range-control issues remain documented. Separate notice ZIPs are optional because
notices ship inside the apps; corresponding third-party sources remain release assets.

## Verified Mac build

- Installer: `dist/OpenScope-0.1.0-arm64.dmg` (unsigned).
- Its packaged backend passed startup, authenticated lifecycle, shutdown, and
  strict native notice verification. No unresolved native notices remain.
- Python 3.12.15 / OpenSSL 3.6.5; Electron 44.7.2.
- OpenCV 5.0.0 rebuilt with LGPL FFmpeg 8.1.3, libpng 1.6.59,
  libtiff 4.7.2, zlib 1.3.2 and libjpeg-turbo 3.2.0.
- Pillow 12.3.0 retains its upstream codecs/fonts, with its PNG/TIFF libraries
  replaced and ABI symbol checks retained. The replacement dependencies use
  private linkage so unrelated `/usr/local` libraries cannot be picked up.
- AVI/MKV encode/decode is pixel-exact FFV1. MP4/M4V still encode H.264 using
  VideoToolbox outside the restricted execution sandbox. JPEG/PNG checks pass.
- Final patched-Python regression: 224 tests passed, one skipped. All 52 desktop
  tests passed. Evidence is retained in `build/native-security/logs/`.
- Mac third-party source/recipe and notice ZIPs have been generated in `dist/`.

On 2026-10-15 the DMG was rebuilt again after the Windows/shared-source fixes.
Packaged startup/shutdown, strict notices and actual frozen OpenCV media checks
passed. Rebuild log: `build/mac-final-rebuild-20261015.log`; media evidence:
`build/mac-final-frozen-media-20261015.json`. Compiler/freezer output folders and
the generated source ZIP were cleared for disk space, then regenerated; the
patched native runtime, source inputs and original working DMG were retained
until the new installer completed.

## Linux verification

User-authorized host: `ssh dori`.
Current checkout, explicitly selected by the user: `/mnt/data/garmin_release`.
Earlier build checkout: `/nvme1/openscope-release-build-20261014`.
The final shared source update was applied to the current checkout after verifying
its baseline commit `d82f7de` and clean tracked worktree. Its existing
`backend/controller-settings.json` and copied Mac DMG were preserved.

`build/native-conda` contains Python 3.12.15, NumPy 2.0.2, Pillow 12.3.0,
OpenCV 5.0.0 / Qt 6.12.0, patched PNG/TIFF/OpenSSL, and an LGPL FFmpeg
8.1.3 rebuild with OpenH264. The initial Conda FFmpeg 8.1.2 was replaced before
packaging. Exact Conda records/cache and replacement source hashes are retained.

The Linux backend and AppImage have built. The packaged controller lifecycle
passes, native notice coverage is complete, and the actual frozen OpenCV library
passes FFV1 AVI/MKV, H.264 MP4/M4V, JPEG/PNG and offscreen Qt window checks.
The AppImage was rebuilt from the final source on 2026-10-15. All 224 Python
tests pass (one skipped), as do all 52 desktop tests.
Packaging excludes unused GNU Readline after checking retained native dependencies. Log:
`build/linux-backend-package.log`. Native build log: `build/linux-runtime-build.log`.
Linux source/notice ZIPs have been generated from retained Conda sources and
exact matching Ubuntu source revisions. The maintainer has since confirmed
physical desktop and sonar testing for both Linux packages; the automated
offscreen GUI check itself does not exercise a physical desktop or sonar device.
Current evidence: `build/linux-final-rebuild-20261015.log`,
`build/linux-python-final-tests.log`, `build/linux-desktop-final-tests.log`,
`build/linux-final-packaged-smoke.log` and `build/linux-final-frozen-media.json`.

## Windows verification — 2026-10-15

SSH access to the user's Windows host now works over the phone hotspot. Work is
inside `C:\Users\fishe\garmin_emulator`; no system Python or Conda base packages
were changed. `build/native-conda` contains Python 3.12.15, OpenCV 5.0.0 / Qt
6.12.0, Pillow 12.3.0, OpenSSL 3.6.5, PNG 1.6.59, TIFF 4.7.2 and shared LGPL
FFmpeg 9.0.2. The actual avutil DLL reports LGPL v3+ with GPL disabled.

Runtime media acceptance passes FFV1 AVI/MKV, H.264 MP4/M4V, JPEG/PNG and Pillow.
Windows Python regression passes 223 tests with two Unix-only cases skipped.
Desktop regression passes 49 tests with three Unix-only cases skipped (52 total).
The same source passes 224 Python tests (one skipped) and 52 desktop tests on Mac.

Fixed Windows PNG publication: flushing a read-only descriptor fails on Windows;
atomic image writes now flush a writable descriptor. Recovery journal writes
close the temporary file before replacement. Other fixes make tests explicit
about POSIX APIs, permissions, UTF-8, line endings and platform temporary paths.
`.gitattributes` preserves upstream notice bytes so their recorded hashes also
match Windows checkouts. Conda receipt scans are cached per collector process.

The final backend and unsigned NSIS installer built successfully after these fixes:
`dist/OpenScope Setup 0.1.0.exe` (347,030,618 bytes). All 310 packaged native
libraries match their audited input SHA-256 hashes, and no unresolved native
notices remain. Packaged startup, authenticated API, settings persistence, restart
and shutdown passed. The actual frozen OpenCV extension passed FFV1 AVI/MKV,
H.264 MP4/M4V, JPEG/PNG and offscreen Qt window checks. The media check confirms
the loaded FFmpeg DLLs are the packaged copies, rather than build-prefix copies.
Offscreen Qt reports missing font-directory/size-hint messages; the check exits
successfully. The maintainer subsequently confirmed Windows desktop and hardware
operation.

Required native sources are retained, including exact FreeType and Qt archives.
Official alternate endpoints resolved mirror certificate failures; certificate
validation and recipe checksums remained enabled. The source/notice packager is
`scripts/package_windows_sources.py`. Preserve `build/conda-sources/`,
`build/conda-source-notices.json`, `build/conda-explicit.txt` and the package cache.
Source/notice ZIPs were generated alongside the installer in Windows `dist/`.

Logs on Windows: `build/windows-build-final.log`, `build/windows-sources.log`,
`build/windows-python-tests-final.log`, `build/windows-desktop-tests-final.log`,
`build/windows-packaged-smoke.log`, `build/windows-packaged-native-hashes.log`
and `build/windows-frozen-media.json`. The smoke-test CLI now resolves relative
resource paths before changing the backend working directory. The initial hung
diagnostic runner and its children were stopped; the corrected tests exit normally.
The source changes are recorded on `release/desktop-preparation`; native build
profiles, caches and installers remain local build outputs. The GitHub workflow
was updated but not executed. On 2026-10-15 the user confirmed Mac and Windows
work with the hardware, and subsequently confirmed both Linux packages tested too.

## Safe resume

- Preserve `build/native-security/` on the Mac, especially sources, wheels,
  notices, profile and logs. The compiler object trees were removed to save space;
  original source archives remain. Preserve the remote Conda package cache.
- Windows remediation and final cross-platform build changes are on
  `release/desktop-preparation`. Earlier work is in `d82f7de`.
  `release/` was pre-existing and left alone.
- Read this file, `docs/BUILD.md`, and current logs before resuming.
- Mac profile verification: `python3.12 scripts/prepare_macos_runtime.py`.
- Rebuild Mac backend: `node desktop/scripts/build-backend.js mac`.
- Linux: in the isolated checkout, put the user's Node 22 directory on PATH,
  then `node desktop/scripts/build-backend.js linux`.
- Preserve the prepared Windows runtime and package cache; rebuild using
  `cd desktop` then `npm run build:win`.
- No signing credentials, release publication, tags or remote pushes were used.

## Commands for the prepared machines

Mac: `cd desktop && npm run build:mac` uses the saved patched runtime.
The complete DMG/source/notice assets are in the local `dist/` directory.

Linux: `ssh dori`, then:

```bash
cd /mnt/data/garmin_release/desktop
export PATH=/home/mahobley/.nvm/versions/node/v22.22.3/bin:$PATH
npm run build:linux
```

Use `/mnt/data/garmin_release`, as requested. Its `dist/` directory contains the
rebuilt AppImage and the matching third-party source and notice ZIPs.

Windows PowerShell, using the prepared checkout:

```powershell
cd C:\Users\fishe\garmin_emulator\desktop
npm run build:win
```

To install the generated unsigned Windows build interactively:

```powershell
Start-Process "C:\Users\fishe\garmin_emulator\dist\OpenScope Setup 0.1.0.exe"
```

## Preserved source and remaining limits

`dist/OpenScope-0.1.0-release-source.tar.gz` preserves the earlier Mac/Linux source
snapshot. `dist/OpenScope-0.1.0-windows-remediation-source.tar.gz` preserves the
current source, including new untracked scripts/docs; it is copied to Windows
`dist/` too. `build/windows-remediation.patch` and `build/windows-remediation-status.txt`
record the working changes before they were committed. The release-preparation
branch contains the build recipes and shared fixes; installers and prepared
native runtimes are excluded from Git. Existing prepared machines can reuse
their validated runtime after pulling; fresh machines must run runtime preparation.
`dist/OpenScope-0.1.0-final-source.tar.gz` is the final Mac/Linux source snapshot,
including the updated platform checkpoints. It is also retained in Linux `dist/`.

The Mac is unsigned. Linux was built/tested on Ubuntu 24.04 on dori; other distro
compatibility is not established by these tests. Physical desktop and hardware
operation have since been confirmed by the maintainer.
One unfixed moderate sprintf-js advisory remains in the npm build-tool tree
(eight dependent audit entries); those modules are not packaged in the app.
Windows is also unsigned. The user confirmed Mac, Windows, and both Linux packages
work with hardware. The original opaque Windows FFmpeg blocker has been resolved
for the prepared runtime and this verified packaged backend.
