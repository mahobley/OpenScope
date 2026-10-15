# macOS installer dependency and native-library audit — 2026-10-14

Follow-up source remediation (no installer rebuild): Electron is now pinned to
44.7.2 on all platforms; packaging enforces current Python runtime pins,
Python 3.12.15+ within 3.12, strict native notices, and inspected LGPL FFmpeg
configuration. The Mac collector now locates notices/receipts/formulas from
the exact Homebrew input keg. A read-only check against the retained original
TOC found matching keg notices for all 52 previously unresolved entries.
That resolves their collection path for a future build, not the security of
those old binaries or the correctness of every wheel-vendored license.
All 49 desktop tests and 14 license/source-review tests passed. The findings
below remain a record of the original artifact. Mac OpenCV libraries and source
correspondence remain unresolved; Linux and Windows need independent artifact
verification. See [BUILD.md](BUILD.md) for current platform-specific blockers.

**Result: this inspected installer is not cleared for public release.** It
contains dependencies with published security advisories, incomplete native
notice review, and a GPL-enabled FFmpeg build that contradicts its LGPL summary.
These are artifact findings; this review did not demonstrate an OpenScope exploit.

## Artifact and evidence

Inspected the application mounted read-only from the local
`dist/OpenScope-0.1.0-arm64.dmg`, at
`/Volumes/OpenScope 0.1.0-arm64 2/OpenScope.app`. This is the installer that
existed when the audit started, before the user attempted a new build.
The original DMG and unpacked output were removed during that attempt, before
a DMG SHA-256 could be recorded. All subsequent checks used the retained mount.
The findings do not describe a future replacement installer or establish which
copy is currently installed in Applications.

- Embedded application version: 0.1.0; backend build ID: `3ff4c5ca77f7`.
- Checkout inspected: `f0ba7f8170514fd6903123eabda2baeb9cbd337a`.
  The embedded manifest does not identify its Git commit, so this is not proof
  that every bundled source file came from that commit.
- SHA-256 inventory of 1,017 regular application files retained locally;
  canonical tree fingerprint:
  `0738a70aad7c9df3fa768a4e75938acd45401619330620758db61c4266459d37`.
  See `artifact.json` for the fingerprint definition; it excludes symlinks and
  is not the installer hash.
- Inspected 224 Mach-O files, including 208 backend native inventory entries.
- Opened the three PyInstaller archives and the Electron application ASAR.
- Queried OSV for all 14 Python inventory entries and Electron 44.3.0, retrieved
  advisory details, and deduplicated alias records. Checked selected native
  versions against upstream advisories, independently of Python package names.

Machine-readable evidence and the inspection scripts are in
[`../build/audit-macos-installer/`](../build/audit-macos-installer/):
`artifact.json`, `app-sha256.json`, `inventory.json`, `native-inventory.json`,
`runtime-build.json`, `macho-inventory.json`, `native-versions.json`,
`python-archives.json`, `asar-files.json`, `checks.json`, `osv-query.json`,
`osv-batch.json`, `osv-advisories.json`, and `osv-summary.json`.
This generated directory is ignored by Git; retain it with release evidence.

## Python dependencies actually recorded in the installer

| Package | Bundled inventory version | Assessment |
| --- | --- | --- |
| Pillow | 11.3.0 | 18 distinct OSV/GHSA advisory matches, including one Windows-only issue; current source pins 12.3.0 |
| zeroconf | 0.148.0 | 5 distinct advisory matches; current source pins 0.149.17 |
| numpy | 2.0.2 | No OSV package match returned |
| opencv-python | 5.0.0.93 | No OSV package match; significant native findings below |
| dpkt | 1.9.8 | No OSV package match returned |
| pyserial | 3.5 | No OSV package match returned |
| ifaddr | 0.2.0 | No OSV package match returned |
| altgraph, macholib, packaging | 0.17.5, 1.16.4, 26.3 | No OSV package match returned; inventory includes build-environment packages |
| PyInstaller, hooks-contrib | 6.22.2, 2026.8 | No OSV package match returned; freezing tools/bootloader notices |
| setuptools | 84.0.0 | No OSV package match returned; build-environment inventory |
| pip | 25.0 | 6 distinct advisory matches; build tooling, not a frozen application module |

The notice inventory describes the installed build environment, not exclusively
importable runtime modules. None of the three frozen Python module archives
contains `pip`, `setuptools`, `PyInstaller`, `pytest`, `readline`, or `scapy`.
Pip's findings must therefore not be added to the application's runtime count.
PyInstaller's bootloader remains part of each executable despite the absence
of its Python package. Scapy is absent from all three archives and the inventory.

Pillow's matches include image/font memory corruption, excessive allocation,
and parsing failures. The 18 package matches are not 18 proven Mac attack paths:
one concerns WindowsViewer and others require particular APIs or formats.
The viewer archive does include PIL image/font/CMS code and PSD/FITS/PDF modules.
The current 12.3.0 source pin addresses the returned Pillow package advisories.
See the [upstream Pillow advisories](https://github.com/python-pillow/Pillow/security/advisories).

Zeroconf is directly relevant to the LAN-facing sonar discovery path. Its five
matches cover DNS compression recursion, unbounded packet/cache state, queue
exhaustion, and malformed record lengths. All are classified moderate in the
queried GitHub database; highest returned fix version is 0.149.16. The current
0.149.17 pin covers these. IDs, affected ranges, aliases and references are saved
in `osv-summary.json`. See the
[upstream zeroconf advisories](https://github.com/python-zeroconf/python-zeroconf/security/advisories).

Rebuilding from the current pins should replace these two old runtime versions.
It will not by itself fix the unchanged OpenCV wheel's native libraries or the
Electron pin. The audit cannot determine why the inspected backend used old pins.

## Native libraries and embedded runtimes

The following native versions were read from the actual mounted libraries'
exported version APIs, except Python (embedded build metadata) and Electron
(framework Info.plist). Version-based matches assume no undocumented backport;
matching source/patch records were not available to establish otherwise.

| Component | Observed version | Finding / action |
| --- | --- | --- |
| FFmpeg | 7.1.1 | Upstream lists security fixes in 7.1.2, including CVE-2025-1594, CVE-2025-9951 and CVE-2025-59728; select a currently maintained patched build and assess the enabled codecs |
| OpenSSL in cv2 | 3.6.0 | Falls in affected ranges including CVE-2025-15467 (CMS stack overflow, fixed in 3.6.1) and later advisories; upstream lists fixes through 3.6.5 |
| GnuTLS in cv2 | 3.8.9 | Predates fixes including CVE-2025-32988/32989 and CVE-2026-33845/33846; current upstream recommendations include 3.8.13 |
| libpng in cv2 and PIL | 1.6.49 in both copies | Affected upstream ranges include CVE-2026-33416, CVE-2026-33636 (ARM Neon), and CVE-2026-46675; upstream 1.6.59 fixes the latest listed issue |
| Mbed TLS crypto in cv2 | 3.6.3 | CVE-2025-52497 affects encrypted PEM parsing through this version; `mbedtls_pem_read_buffer` is exported by this shipped library; fixed in 3.6.4, but review later advisories too |
| libarchive in cv2 | 3.8.1 | CVE-2026-45308 / GHSA-xgqq-r5j8-3mch affects <=3.8.7, fixed >=3.8.8; ZIP writing reachability in OpenScope not established |
| libtiff in cv2 and PIL | 4.7.0 | Upstream 4.7.1 includes buffer-safety fixes; complete CVE/backport mapping remains open |
| PCRE2 in cv2 | 10.45 | Version established; exhaustive advisory assessment remains open |
| CPython | 3.12.9 | Predates multiple security releases; use current supported 3.12 security patch level (3.12.15 on review date), with matching native notices |
| Electron | 44.3.0 | No OSV npm-package match returned; official release embeds Chromium 152.0.7977.78 and Node 24.20.0; current 44.7.2 embeds newer runtimes and upstream security backports |

Primary references: [FFmpeg security](https://ffmpeg.org/security.html),
[OpenSSL 3.6 advisories](https://www.openssl-library.org/news/vulnerabilities-3.6/index.html),
[GnuTLS security](https://www.gnutls.org/security-new.html),
[libpng security warnings](https://www.libpng.org/pub/png/libpng.html),
[Mbed TLS encrypted PEM advisory](https://mbed-tls.readthedocs.io/en/latest/security-advisories/mbedtls-security-advisory-2025-06-2/),
[libarchive ZIP advisory](https://github.com/libarchive/libarchive/security/advisories/GHSA-xgqq-r5j8-3mch),
[libtiff 4.7.1 changes](https://libtiff.gitlab.io/libtiff/releases/v4.7.1.html),
[Python 3.12.15](https://www.python.org/downloads/release/python-31215/),
[Electron 44.3.0](https://releases.electronjs.org/release/v44.3.0), and
[Electron 44.7.2](https://releases.electronjs.org/release/v44.7.2).

These are representative native findings, not an exhaustive CVE total for all
224 binaries. Remaining codecs, statically linked components, and Electron's
complete Chromium/Node vulnerability mapping need further provenance review.
An empty Python/npm advisory result does not clear these native components.
The earlier libarchive RAR advisory GHSA-6vpf-5947-22g5 was checked and is fixed
in 3.8.1; it is not counted as a finding.

`otool -L` found no absolute non-system dependencies such as `/opt/homebrew/`
or a developer home directory. Dependencies use bundle-relative paths or Apple
system libraries/frameworks. This is not a complete runtime loader validation.
No standalone libpcap is bundled: macOS uses the system libpcap dynamically.
System libraries require host OS patching rather than copying an arbitrary
replacement into the app.

## License and corresponding-source findings

1. **FFmpeg license mismatch.** Both `runtime-build.json` and a direct call to
   bundled `avutil_license()` report GPL version 3 or later. The configure line
   enables `--enable-gpl` and `--enable-version3`; x264, x265, rubberband and
   vidstab libraries are present. The wheel notice describes FFmpeg as LGPL 2.1,
   and OpenScope's summary says its FFmpeg is LGPL. Those descriptions are not
   accurate for this artifact. Generic GPL text is included, but does not resolve
   missing attribution, source correspondence or combined-work obligations.
   Choose a patched LGPL configuration without GPL/nonfree features, or review
   and fulfill the GPL distribution requirements for the combined distribution.
   OpenScope's own MIT source terms can remain; removing Scapy does not make all
   other bundled components permissively licensed. See
   [FFmpeg's license guidance](https://www.ffmpeg.org/legal.html).
2. **Strict native notice check fails.** Standard verification passes only with
   52 pending entries. Strict mode fails for CPython's framework, 50 standard
   extension modules, and `libmpdec.4.dylib`. These require exact distribution
   ownership and notices; 52 entries do not mean 52 distinct licenses.
3. **Wheel ownership is not complete native clearance.** The collector marks
   wheel-owned binaries as covered based on package ownership. This missed the
   FFmpeg contradiction. Its `collected` status must not be interpreted as
   proof of correct terms for every vendored library. OpenSSL 3.x is another
   reason to review exact native attribution rather than the old OpenSSL 1.x
   notice included by the wheel.
4. **Corresponding sources remain unverified.** The included source manifest's
   native FFmpeg/Qt profile is for Linux. It cannot establish correspondence for
   this Mac FFmpeg 7.1.1 Homebrew-configured dependency tree. No reviewed matching
   Mac source asset/build recipes or successful replacement/rebuild evidence
   were established. Zeroconf also retains its LGPL obligations. Qt was not
   observed in this Mac backend (OpenCV reports Cocoa).
5. Electron, Chromium, project MIT, author attribution, pyserial, and package
   notice files required by the existing verifier are present and nonempty.
   This verifies presence, not all underlying redistribution obligations.

The app has an ad-hoc executable signature, no TeamIdentifier, and no sealed
resource envelope according to `codesign -dv`. It provides no Developer ID
publisher identity. Signing/notarization remains separate release work and
would not fix vulnerable dependencies or license gaps.

## Recommended order

1. Rebuild with the existing Pillow 12.3.0 / zeroconf 0.149.17 pins and a patched
   Python 3.12, then inspect the replacement installer inventory explicitly.
2. Replace/rebuild the macOS OpenCV native dependency stack with current security
   fixes and a deliberate FFmpeg license configuration. Review both copies of
   shared codecs; updating Pillow alone does not update cv2's libpng/libtiff.
   Do not delete dylibs from the bundle without rebuilding and checking linkage.
3. Update Electron within its supported release line, regenerate the lockfile,
   and repeat desktop/package verification and hardware/recording checks.
4. Resolve strict native notices, correct the artifact-specific license summary,
   and collect exact matching sources/patches/recipes before publishing binaries.
5. Retain the final DMG checksum, dependency/native inventory and audit snapshot.

No dependency pins, build behavior, installed applications, or existing user
data were changed for this audit. Source tests were not rerun because no runtime
code changed. Package lifecycle/hardware tests and exploit reproduction were
outside this inspection. The mounted original was used to avoid racing the
user's concurrent rebuild.
