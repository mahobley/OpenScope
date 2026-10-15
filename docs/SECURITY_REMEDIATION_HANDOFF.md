# Superseded checkpoint

Continue from [RELEASE_STATUS.md](RELEASE_STATUS.md). The notes below describe the earlier pause, before native builds completed.

# Restart checkpoint — 2026-10-14

The user asked to stop here so they can restart the Mac to reclaim disk space.
Resume the authorized security remediation when they return. No native compilation
or app build was running at this checkpoint. No changes have been committed.

## User's objective and constraints

Address the dependency/native-library vulnerabilities **while keeping existing
application functionality**, including sonar controls and recording formats.
The project also has Linux and Windows versions: distinguish shared changes
from Mac-only observations and fixes. Do not assume Mac findings apply to other
platforms. Do not remove recording formats as a shortcut.

The user initially requested source fixes without rebuilding; those were made.
After discussing that native fixes require replacement/rebuilding, they asked
to address the vulnerabilities while preserving functionality. Native dependency
work was just starting when the user requested this restart checkpoint.

Workspace: `/Users/mahobley/Code/openscope-release`.
Current checkout: `f0ba7f8170514fd6903123eabda2baeb9cbd337a`.
Do not discard the existing edits or the unrelated, untracked `release/` folder.

## Findings and retained evidence

Read `docs/MACOS_INSTALLER_AUDIT.md` for the full original artifact audit.
Machine-readable evidence is in `build/audit-macos-installer/` (ignored by Git).
The previous Linux audit is `docs/DEPENDENCY_AUDIT.md` and its linked evidence.

The Mac installer inspected before the user's attempted rebuild contained:

- Pillow 11.3.0 and zeroconf 0.148.0, despite newer source pins. OSV returned
  18 distinct Pillow and 5 zeroconf package advisories. These are version
  matches, not demonstrated OpenScope exploits; one Pillow issue is Windows-only.
- Python 3.12.9; Electron 44.3.0.
- OpenCV Python 5.0.0.93 with FFmpeg 7.1.1, reporting **GPLv3-or-later** and
  enabling GPL features, contrary to the LGPL notice summary. Its macOS wheel
  includes x264/x265 and a large Homebrew-derived native dependency tree.
- Native versions independently read through library APIs: OpenSSL 3.6.0,
  GnuTLS 3.8.9, libpng 1.6.49 (both PIL and cv2 copies), libtiff 4.7.0 (both),
  Mbed TLS crypto 3.6.3, libarchive 3.8.1, PCRE2 10.45. Published advisories
  match several of these. Inventory covers 224 Mach-O files; it is not a
  complete exploitability assessment of every embedded/static component.
- 52 unresolved native notice entries: Python framework, 50 Python extension
  modules, and libmpdec. All have matching installed Homebrew keg notices
  according to the new collector's read-only preview.
- No Scapy in any of the three frozen backend archives. No pip/setuptools
  runtime modules, although their notices appear in build-environment inventory.

The original DMG and unpacked app were removed by the user during their rebuild
attempt. The audit used a retained read-only mount; our mount (`/dev/disk6`) was
detached after evidence collection. Other user-mounted old installer volumes
were left alone. A full original DMG hash was not captured before removal;
per-file hashes and an app-tree fingerprint are saved in `artifact.json`.

## Source fixes already made and tested

- `desktop/package.json` and `desktop/package-lock.json`: Electron **44.7.2**
  for all platforms. Lockfile-only install; existing node_modules runtime was
  not updated. electron-builder remains 26.16.1.
- Existing source pins already had Pillow **12.3.0**, zeroconf **0.149.17**.
  Other runtime pins: numpy 2.0.2, opencv-python 5.0.0.93, dpkt 1.9.8,
  pyserial 3.5. PyInstaller 6.22.2.
- `scripts/build_backend.py` requires Python **3.12.15+ within 3.12**.
  Mac shell wrapper preflights the same requirement before npm installation.
  Installed Homebrew Python is still **3.12.9**; it was not upgraded.
- `desktop/scripts/verify-licenses.js` checks packaged dependencies against
  current source pins, Python patch level, FFmpeg configuration/license evidence,
  and rejects GPL/nonfree configurations or enabled-but-uninspected FFmpeg.
- `after-pack.js` and `verify-package.js` now enforce strict native notices.
  Missing/unknown per-binary coverage cannot be bypassed by an empty summary.
- `scripts/collect_licenses.py`: Mac native ownership/notices come from the
  exact source binary's Homebrew keg, including retained recipe and receipt.
  Wheel ownership is explicitly not full native license/source clearance.
  FFmpeg inspection recognizes separate avutil DLLs as well as Unix libraries.
- Corrected `THIRD_PARTY_NOTICES.md`, `docs/BUILD.md`, `docs/LICENSING.md`,
  and added a follow-up to `docs/DEPENDENCY_AUDIT.md`.
- Tests added in `backend/tests/test_license_collection.py` and
  `desktop/test/license-files.test.js`.

Validation completed: **49 desktop tests passed**, **14 Python licensing/source
tests passed**. Targeted desktop license tests rerun after the last check change:
14 passed. Shell syntax, Python AST parsing and `git diff --check` passed.
Fresh npm lockfile audit: 0 high/critical; 8 moderate entries arising from the
known single sprintf-js build-tool advisory, documented in the earlier audit.
No backend, installer, source-controller lifecycle, or hardware validation has
been run with a replacement native stack.

## Current blocker and next work

Disk had only **2.8 GiB free** at checkpoint. User will restart to free space.
We requested approximately 10–15 GB additional working room (or a writable
external build folder). Recheck actual `shutil.disk_usage` and `df` on return.
The existing 3 GiB build check uses binary GiB; npm installation can consume
enough space between preflight and backend startup to cause the later failure.

No replacement native-build implementation has been written yet. The only
additional preparation in the latest turn was downloading the official OpenCV
source archive (78 MiB):

`build/native-security/sources/opencv_python-5.0.0.93.tar.gz`

Expected SHA-256 from official PyPI metadata:
`66aac3e5b5faa48d4025816592f3af19e4bfc2c68dec067bae2dbb4ca10aa9e2`.
Verify it before extracting/using. Metadata saved in
`build/audit-macos-installer/opencv-pypi.json`. PyPI's latest OpenCV remains
5.0.0.93 on review date, so there is no confirmed newer wheel pin to substitute.

Preferred next investigation: reproducible Mac OpenCV build against a patched,
LGPL-only FFmpeg while retaining the existing recording formats. Avoid falling
back to AVFoundation-only unless the user explicitly accepts format changes.
Disabling FFmpeg alone does not preserve the current AVI/MKV lossless options.
Do not blindly swap dylibs or claim a newer package pin clears vendored codecs.
Check both static and dynamic OpenCV dependencies and Pillow's separate copies.
A patched Python 3.12 environment is needed as well. No system-wide Homebrew
upgrade or package installation has been performed.

Recording factory is `backend/live_viewer/recording.py:make_video_writer`:

- AVI tries FFV1, HFYU, MJPG.
- MKV tries FFV1, MJPG.
- MP4/M4V try avc1, H264, mp4v.

Capture also needs JPEG/PNG, metadata, echogram PNGs, and existing image-processing
functions. Create real encode/decode acceptance checks for the formats before
and after replacing dependencies; retain lossless behavior where available.
Keep Linux/Windows code paths working; their native stacks need independent
artifact checks rather than inheriting conclusions from this Mac audit.

## Upstream information checked

- OpenCV Mac GPL issue: https://github.com/opencv/opencv-python/issues/1260
- FFmpeg downloads: https://ffmpeg.org/download.html
  Current releases observed: 9.0.2, 8.1.3, and 7.1.5. Choose a compatible patched
  branch and verify source authenticity/hash and configuration. Do not assume
  FFmpeg 8.1.1 from the earlier Linux review is current enough now.
- libpng: https://www.libpng.org/pub/png/libpng.html
  Current 1.6.59; official tar.xz SHA-256:
  `d80dd2a38a37f803cb9b6ac7b14bd6e74ddc3b654780a8380bdf93523fdb4389`.
- libtiff current 4.7.2: https://libtiff.gitlab.io/libtiff/releases/v4.7.2.html
- Python 3.12.15: https://www.python.org/downloads/release/python-31215/
  Source-only release. Official tar.xz SHA-256 shown:
  `c2c4321961fab0fb999d66e0cecf521c2ab3994c7992873ea99e306c1094fd5a`.
- OpenSSL 3.6 advisories: https://www.openssl-library.org/news/vulnerabilities-3.6/index.html
  Fixes through 3.6.5 were listed during audit.
- Electron 44.7.2: https://releases.electronjs.org/release/v44.7.2

## Temporary tools and permissions

`/private/tmp/openscope-license-tests/` contains isolated pytest (no app runtime
dependencies); it may disappear across restart and can be recreated.
`/private/tmp/openscope-npm-audit-cache/` was used for lockfile updates.
Build evidence in repository `build/` should persist unless explicitly cleaned.
Available commands included make, clang, pkg-config; cmake and ninja were not
on PATH at the last check. Native builds have not started.

Do not spawn sub-agents: current developer instructions prohibit delegation
unless user or applicable repository instructions explicitly request it.
