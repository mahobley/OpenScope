# Historical review record

For the replacement builds, read [RELEASE_STATUS.md](RELEASE_STATUS.md). The following records describe the earlier Linux wheel and do not qualify the new runtimes.

# Licensing preparation record

## Joint release

Mourad Derbedrosian and MAHobley are collaborating on this joint MIT-licensed
release, to be published through the `mahobley` GitHub account. Attribution is
retained in [AUTHORS.md](../AUTHORS.md). Root MIT terms cover their own project
code; third-party licenses remain separate.

## Earlier review on 2026-10-14 — Linux x86_64 (before dependency fixes)

- The updated collector retains a complete pyserial 3.5 BSD notice with Chris
  Liechti's copyright; all 17 installed package entries have notice files.
- Pillow 11.3.0 is identified as MIT-CMU, matching its distribution metadata.
- A fresh native backend build has 182 native binary/extension input entries
  mapped to 35 Python distribution, Debian/Ubuntu, or Conda owners. All those
  entries have collected notice coverage; none is marked `needs-review`.
- The build contains neither Scapy nor the unused pytest/Readline modules.
  The optional GNU Readline shared library is not included in the new bundle.
- OpenCV reports 5.0.0 with Qt 5.15.19. Its FFmpeg libraries report 8.1.1,
  LGPLv2.1-or-later, and a configuration without `--enable-gpl`/`--enable-nonfree`.
- Nine source archives have been downloaded and SHA-256 checked: the six pinned
  Python runtime dependencies, PyInstaller, FFmpeg, and Qt base. Official PyPI
  metadata supplies the Python sdist hashes; the manifest retains the native
  archive hashes observed from the upstream HTTPS downloads.
- The OpenCV source distribution includes the Linux Docker build recipe and
  Qt packaging patch. Copies are retained with the local source inputs to help
  review correspondence and reproduce library builds.
- Eight Python licensing tests and six desktop licensing cases cover the
  pyserial fallback, license hashes, unknown versions/native owners, corrupted
  source downloads, platform Chromium-notice locations, and missing notices.
  The complete Python suite passed 216 tests with one platform skip; all seven
  desktop test files passed.

These findings apply to the local review build, not pre-existing installers.
Generated review files are in `build/licensing/linux-x64/`; source inputs are in
`build/license-sources/`. They are build artifacts, not checked-in dependency source.

## Dependency security updates — 2026-10-14

Pillow is now pinned to 12.3.0 (MIT-CMU) and zeroconf to 0.149.17
(LGPL-2.1-or-later). Their source-manifest URLs and SHA-256 hashes have been
refreshed from official PyPI metadata and the replacement archives downloaded
and verified. The replacement Linux backend and AppImage have 183 native input entries with
complete notice coverage. A refreshed source review verifies the six runtime
wheel records and 51 zeroconf source files; it is retained at
`/tmp/openscope-dependency-build/source-review.json`. Earlier wheel counts,
binary inventories and correspondence reports below describe the previous
pins. Final release correspondence checks, including the GNU runtime work,
remain open. See [DEPENDENCY_AUDIT.md](DEPENDENCY_AUDIT.md) for security results
and build validation.

## Required for the binary release

### Source matching progress — 2026-10-14

The source review is now reproducible with `scripts/review_license_sources.py`.
The pre-update report is `build/licensing/linux-x64/source-review.json` and includes
hashes of its source manifest, native inventory, and runtime build record.

- All 1,227 hashed installed files across the six runtime packages match their
  wheel `RECORD` entries. This detects local modifications; the installed
  `RECORD` is not independent authentication of an upstream wheel.
- All 51 installed zeroconf `.py`/`.pxd` source files match the pinned source
  archive. Its compiled extensions have not been independently rebuilt.
- For this 64-bit Linux non-headless wheel, `patchQtPlugins` is the applicable
  OpenCV **packaging** patch. It modifies OpenCV's CMake install rules to copy Qt
  plugins and fonts; it does not patch Qt's library source. `patchOpenEXR` is
  conditional on a 32-bit Linux wheel and does not apply to this build. Other
  vendored patch files are present in the archive and remain preserved there;
  their presence alone does not prove they were applied.
- FFmpeg's recorded configure arguments match the exact upstream Linux wheel
  recipe, including its 8.1.1 version. The Qt recipe selects 5.15.19 and records
  its configure options. This is recipe evidence, not a bit-for-bit rebuild.
- All 27 Debian/Ubuntu native binary inputs match their installed package
  checksums and the SHA-256 hashes recorded at freezing time.
- Sources for all 24 distinct Ubuntu source-package revisions are downloaded:
  81 files including `.dsc`, upstream archives, distro patches, and available
  signature files. Hashes come from freshly signed Ubuntu APT indexes. Files
  are under `build/license-sources/ubuntu/`; the versioned download manifest is
  `build/licensing/linux-x64/ubuntu-source-manifest.json`. This includes the
  distro patch sets rather than assuming the libraries are pristine upstream.
- Twelve licensing regression tests pass, including detection of modified wheel
  files, wrong archive hashes, and mismatched Ubuntu package revisions.

The Ubuntu source indexes were prepared in `/tmp/openscope-apt-sources/` without
changing the machine's APT configuration or installing software. To reconstruct
a downloaded patched source tree, use `dpkg-source -x <package-version>.dsc` in
a writable directory alongside the other files for that source package.

NumPy reports GCC 10.2.1 for its C/C++ build and scipy-openblas 0.3.27. Its
vendored `libgfortran`/`libquadmath` provenance still needs review: the compiler
used for NumPy's C extensions does not establish the source revision of those
prebuilt GNU libraries. The Ubuntu GCC 14 source archive is for the OS-supplied
GNU runtimes and must not be substituted as their source without evidence.

Notice coverage is complete for this inspected Linux backend. Corresponding
source review remains open for the final binaries: check all copyleft native
libraries listed in the native inventory, including OS libraries and NumPy's
GNU runtime components, not only the downloaded FFmpeg/Qt/zeroconf archives.
Retain exact patches, build recipes, and a successful LGPL replacement/rebuild
check. The current source-input folder is not a complete cleared source asset.

Repeat the artifact inventory and source review for rebuilt macOS and Windows
packages; their bundled native libraries can differ. Run the strict packaged
notice check from [LICENSING.md](LICENSING.md), retain final asset hashes, and
publish the reviewed source assets next to the matching installers. Npcap
continues to be separately installed and must not be added to the installers.
