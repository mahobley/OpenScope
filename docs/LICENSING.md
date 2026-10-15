# Licensing and third-party redistribution

## Scope and attribution

OpenScope's own source is distributed under the root [MIT License](../LICENSE).
Dependencies keep their original licenses and copyright notices. A package
manager's single license label does not describe every native library inside
a wheel or installer. The release includes the full collected notices, not just
the summary in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

OpenScope is a joint release by Mourad Derbedrosian and MAHobley, published
through the `mahobley` GitHub account. Their project contributions are released
under MIT; retain [the author credits](../AUTHORS.md) when exporting a fresh
repository. Third-party code and artwork retain any separate upstream terms.

OpenScope is independent of Garmin. Product names describe interoperability;
no Garmin endorsement or ownership of Garmin software is claimed. Npcap is
separately installed by Windows users and is not redistributed by this project.

## Files shipped with an application

Paths are relative to Electron's `resources/` directory (macOS:
`OpenScope.app/Contents/Resources/`).
Relative Markdown links refer to the source checkout; the table identifies
the corresponding files in a packaged copy.

| Path | Contents |
| --- | --- |
| `licenses/OpenScope-LICENSE` | OpenScope MIT terms |
| `licenses/AUTHORS.md` | Joint author and release attribution |
| `licenses/THIRD_PARTY_NOTICES.md` | Dependency attribution summary |
| `licenses/LICENSING.md` | This guide and library replacement instructions |
| `licenses/Electron-LICENSE` | Electron copyright and MIT terms |
| `licenses/LICENSES.chromium.html` | Notices from the exact Electron runtime |
| `licenses/reference/` | Supplementary full license texts and pyserial fallback notice |
| `backend/THIRD_PARTY_LICENSES/inventory.json` | Installed Python package versions and collected notice paths |
| `backend/THIRD_PARTY_LICENSES/native-inventory.json` | Exact frozen native input hashes, owners, and notice coverage |
| `backend/THIRD_PARTY_LICENSES/native/` | Collected operating-system/Conda native-library notices |
| `backend/THIRD_PARTY_LICENSES/runtime-build.json` | Python/OpenCV build details and inspectable FFmpeg version/configuration |
| `backend/THIRD_PARTY_LICENSES/<package>-<version>/` | Upstream package metadata, copyright notices, licenses, and exceptions |

Python wheels sometimes omit notices. The collector supplies the pinned
pyserial 3.5 upstream notice from `third_party/notices/` and fails for other
packages without a collected license file. Linux builds retain distribution
libpcap notices; set `SONAR_LIBPCAP_NOTICES` to the complete matching notice file
on distributions without `/usr/share/doc/libpcap*/copyright`.

Native ownership is resolved from installed Python files, Conda package records
and cached notices, Debian/Ubuntu package records, or (on macOS) the exact
Homebrew input keg's notices, receipt and retained formula. Homebrew ownership
and collected notices do not prove corresponding-source or patch completeness.
Other packaging systems,
missing package caches, and Windows vendor runtime DLLs can need manual notice
collection. `needs-review` entries are release blockers, not cleared licenses.
Inventory collection does not itself prove that source-distribution obligations
are satisfied.

Packaging and packaged-app verification enforce strict native notice coverage
on all platforms. They also compare the backend inventory with current source
requirements, require Python 3.12.15+ within 3.12, and reject inspected FFmpeg
GPL/nonfree configurations. An enabled FFmpeg runtime without recorded license
inspection is rejected too. The standalone notice CLI retains its diagnostic
mode; use `--strict-native` for a release check.

The earlier macOS installer audit found GPL-enabled FFmpeg 7.1.1 with mismatched
wheel notices. The replacement Mac build uses source-built LGPL FFmpeg 8.1.3;
its complete LGPL text, source archives, configuration and build scripts are
retained. Patched Pillow libraries have separate full replacement notices.
Mac source-built Python/OpenSSL ownership is matched to exact binary and notice
hashes. Linux's prepared Conda runtime retains package receipts and source recipes;
its rebuilt FFmpeg is recorded as a source replacement, not as the original Conda
8.1.2 binary. Windows now has a separate Conda recipe with shared LGPL v3+
FFmpeg 9.0.2, avoiding the previous wheel's opaque embedded runtime. Interactive
installer and hardware qualification remains separate. Current results are in
[RELEASE_STATUS.md](RELEASE_STATUS.md).

## Corresponding library sources

The project's release policy is to accompany binary downloads with the complete
corresponding source for redistributed copyleft libraries, including applicable
patches, configuration, and build scripts. Host those sources beside the binary
assets on the same GitHub release. A generic upstream homepage or the automatic
OpenScope source archive alone is insufficient for this policy. We do not rely
on a promise to supply source later.

1. Build each offered platform from the reviewed OpenScope tag. Retain the exact
   dependency wheels, installer hashes, Python/Node versions, and build recipes.
2. Review `inventory.json`, `native-inventory.json`, and `runtime-build.json`.
   Include wheel-vendored libraries and automatically copied native dependencies.
3. Obtain exact matching sources for zeroconf and bundled FFmpeg, Qt, GNU runtime
   libraries, and other copyleft native libraries present in that platform build.
   Preserve their own licenses and notices. Include the dependencies needed to
   rebuild those libraries, and document any system-library exclusions used.
4. Retain upstream wheel recipes, patches, and post-processing steps such as
   auditwheel/delocate library renaming. A matching upstream version number alone
   does not establish correspondence with a patched binary.
5. Use `python scripts/prepare_license_sources.py --output build/license-sources`
   to download and hash the manifest's pinned Python sources and observed Linux
   FFmpeg/Qt source archives. Its report deliberately
   leaves correspondence review open. Extend the manifest/release source inventory
   to cover every copyleft component in the final native inventory.
6. Package reviewed source archives, recipes, modifications, and the completed
   source inventory into `OpenScope-<version>-<platform>-third-party-sources.zip`.
   Full notices already ship inside each app. A separate
   `OpenScope-<version>-<platform>-notices.zip` is optional for convenient review;
   users do not need it to install or run the app.

The Python source entries match the pinned versions. The native FFmpeg/Qt
entries describe the inspected Linux wheel, not macOS or Windows.
For that Linux wheel, FFmpeg reports 8.1.1 and LGPLv2.1-or-later, with
no `--enable-gpl` or `--enable-nonfree` in its configuration; OpenCV reports Qt
5.15.19. Recheck every final wheel. Changing dependencies can change these facts.

### Recording source-matching evidence

Run the review using the Python environment whose packages were frozen:

```bash
build/packaging-venv/bin/python scripts/review_license_sources.py \
  --sources build/license-sources \
  --notices build/licensing/linux-x64/resources/backend/THIRD_PARTY_LICENSES \
  --output build/licensing/linux-x64/source-review.json
```

This profile checks Linux x86_64 installed wheel hashes, zeroconf source files,
OpenCV packaging patches, and FFmpeg recipe arguments. It fails on changed
dependency/source files and records uncompleted binary rebuilds explicitly.
Use `--native-toc <COLLECT-00.toc>` to compare OS native inputs with their
installed Debian/Ubuntu package checksums as well as the freeze input hashes.

For Ubuntu source downloads, use signed `deb-src` indexes for the build machine's
Ubuntu release. These can be held in an isolated writable APT directory; there
is no need to alter the host configuration. Supply `--ubuntu-source-list <file>`
and `--ubuntu-source-lists <lists-directory>` plus `--ubuntu-manifest <output.json>`
to generate exact-version downloads from those indexes. Then run:

```bash
python3 scripts/prepare_license_sources.py \
  --manifest build/licensing/linux-x64/ubuntu-source-manifest.json \
  --output build/license-sources/ubuntu
```

Repeat the review with `--ubuntu-manifest <manifest.json>` and
`--ubuntu-sources build/license-sources/ubuntu` to verify every downloaded file
and record the source-package coverage. If an installed revision is no longer
indexed, obtain its exact source from an official archive instead of silently
substituting a newer version. All these records belong to the specific build.

### Prepared Mac native source asset

After building and verifying the Mac app, run:

```bash
python3.12 scripts/package_macos_sources.py
```

This verifies the retained native source hashes and creates a source/recipe ZIP
and a notices ZIP under `dist/`. It includes the original Pillow replacement
input, exact patched library sources, the OpenCV packaging patch, zeroconf
source, and the local build recipes/configuration. Publish the source/recipe ZIP
with the matching installer and application source tag; the separate notices ZIP
is optional because notices are embedded in the app. Keep the local runtime profile and source
inputs; a package version alone does not identify these rebuilt wheels.

For the Linux and Windows Conda runtimes, `scripts/prepare_conda_sources.py` reads the actual
native inventory and retains matching source archives, full recipes/patches,
package receipts and any recovered notices. Run it using Conda's Python (which
provides `ruamel.yaml`). Retain its `build/conda-source-notices.json` evidence and
`build/conda-sources/` directory. An upstream recipe with only an MD5 checksum is
recorded as such alongside an observed SHA-256; this is not signature validation.
Source downloads retain certificate validation and verify recipe checksums before
publication into the cache. The collector can retry FreeType and Qt downloads
through their official alternate release endpoints when a mirror is unavailable.
For Windows, retain `build/conda-explicit.txt` and run
`build/native-conda/python.exe scripts/package_windows_sources.py` from the
repository root. Publish its source/recipe ZIP beside the matching installer;
the separate notice ZIP is optional because notices are embedded in the app. The Windows FFmpeg DLL reports LGPL v3+; its package metadata's broader
LGPL label does not override the actual build's terms.

## LGPL library replacement and rebuilding

Users may modify the LGPL components and reverse engineer the combined software
as needed to debug those modifications, as permitted by the applicable licenses.
OpenScope adds no EULA term restricting those rights.

The backend is a PyInstaller **onedir** bundle with separate native libraries,
not an encrypted or integrity-locked runtime. Native libraries normally live in
`resources/backend/_internal/`; extension modules are in their package folders.
Qt plugins can also be under `cv2/qt/plugins/`. LGPL Python modules can be inside
the executable's Python archive, so copying a `.py` beside it is not guaranteed
to replace them. Rebuild from source when changing those modules.

To rebuild with a modified library:

1. Obtain the matching OpenScope tag and that release's third-party source assets.
2. Follow [BUILD.md](BUILD.md) on the same OS/architecture. Modify the library
   source and build a compatible wheel or shared library using the recorded recipe.
3. For a modified Python package, replace its pinned entry in
   `backend/requirements.txt` with the local wheel's absolute path. For modified
   OpenCV/FFmpeg/Qt, build an OpenCV wheel using the modified native libraries.
   Keep the wheel's version metadata, notices, and native library names consistent.
4. Run the native application build. `scripts/build_backend.py` creates a fresh
   environment, so installing only into an old build environment is insufficient.
   For Linux libpcap, select/install the modified library for the build process
   and supply its updated notice file with `SONAR_LIBPCAP_NOTICES`.
5. Verify the resulting bundle loads the replacement and passes startup,
   recording, and shutdown checks. Preserve license notices and mark library
   modifications in the source asset.

Compatible shared libraries may also be replaced in an unpacked **test copy**.
Preserve the filenames/ABI required by dependent binaries. macOS changes can
invalidate signatures; use a locally rebuilt/ad-hoc-signed copy and the normal
local authorization flow. On Windows, separately installed Npcap is governed by
its own terms. Do not claim library replacement was tested until it has been
exercised for the released platform.

## Release review record

See [the current preparation record](LICENSING_REVIEW.md) for completed work
and the exact remaining artifact checks.

For each platform, retain a record with: OpenScope tag/commit, installer SHA-256,
notice/source asset names and hashes, all native inventory entries, exact source
revisions and patches, library build recipes, review of GPL/LGPL compatibility,
and a successful library replacement/rebuild check. Resolve all missing notices
and corresponding sources before publishing the binary assets.

Our current work completes the notice/documentation machinery. It does not sign
off on future installers or a source archive
whose correspondence has not been reviewed. Existing installers built before the
Scapy migration must be replaced rather than relabeled.

## Upstream references

- [OpenCV wheel licensing and bundled FFmpeg/Qt](https://github.com/opencv/opencv-python#licensing)
- [FFmpeg licensing and redistribution guidance](https://ffmpeg.org/legal.html)
- [LGPL 2.1, especially sections 4 and 6](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html)
- [Qt 5 LGPLv3 terms](https://doc.qt.io/archives/qt-5.15/lgpl.html)
- [PyInstaller's distribution exception](https://pyinstaller.org/en/stable/license.html)
- [Npcap's own license](https://github.com/nmap/npcap/blob/master/LICENSE)
