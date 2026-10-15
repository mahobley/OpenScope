# Dependency security review — 2026-10-14

Current follow-up: Electron is pinned to 44.7.2 on all platforms. Mac now has a
rebuilt Python/OpenCV/FFmpeg runtime with patched PNG/TIFF/OpenSSL libraries,
and a verified DMG. Linux replacement work is recorded in
[RELEASE_STATUS.md](RELEASE_STATUS.md). On 2026-10-15 Windows received a separate
Conda runtime with Python 3.12.15, OpenCV 5.0.0, Pillow 12.3.0, PNG 1.6.59,
TIFF 4.7.2, OpenSSL 3.6.5 and shared FFmpeg 9.0.2. Its actual avutil DLL reports
LGPL v3+ with GPL disabled; media and Qt offscreen checks pass. See the checkpoint
for final installer verification and remaining hardware qualification.
The results below are historical baseline evidence; they do not audit the new
Conda Linux runtime or qualify Windows. The full npm audit was repeated after
`npm ci`: the same eight moderate build-tool entries remain, with no high or
critical findings and no available compatible fix for the underlying advisory.

Reviewed on Linux x86_64 with Node 22.22.3 (npm 10.9.8), Python 3.12.2 and pip-audit
2.10.1. Reports are snapshots of the advisory databases on this date, not a
guarantee that dependencies or bundled native libraries have no vulnerabilities.
Machine-readable evidence is in [dependency-audit/](dependency-audit/).

## Changes and results

| Dependency check | Before | After |
| --- | --- | --- |
| Full npm tree | 11 findings: 9 moderate, 2 high | 8 moderate, 0 high/critical |
| npm production dependencies (`--omit=dev`) | Not separately captured | 0 findings |
| Python runtime and resolved build requirements | 45 reported entries in 2 packages; 23 distinct advisory IDs | 0 findings |

`npm audit fix --package-lock-only --ignore-scripts` refreshed compatible
transitive dependencies, followed by a clean `npm ci`:

- `brace-expansion`: 1.1.18 → 1.1.21, 2.1.4 → 2.1.7 and 5.0.9 → 5.0.12,
  across every installed occurrence.
- `http-cache-semantics`: 4.2.0 → 4.3.0.
- `fast-uri`: 3.1.7 → 3.1.8.

Electron 44.3.0 and electron-builder 26.16.1 remain pinned. No forced downgrade,
major transitive override, audit suppression or advisory ignore was introduced.

Python pins changed from Pillow 11.3.0 to 12.3.0 and zeroconf 0.148.0 to
0.149.17. The other runtime pins and PyInstaller 6.22.2 are unchanged. Pillow's
findings cover image/font parsing, memory safety and denial of service;
zeroconf's concern mDNS parsing and resource exhaustion on the local network.
See the upstream [Pillow security release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html)
and [zeroconf security advisories](https://github.com/python-zeroconf/python-zeroconf/security/advisories).
Duplicate Python advisory IDs returned by the service are merged in the saved
reports, which retain aliases and fixed versions. The replacement source URLs,
SHA-256 hashes and license notices were updated; both archives were downloaded
and verified. Pillow remains MIT-CMU and zeroconf LGPL-2.1-or-later.

## Accepted finding: sprintf-js (moderate, build tooling)

All eight remaining npm entries derive from **one advisory**:
[GHSA-hp3w-g68c-fv3c / CVE-2026-97058](https://github.com/advisories/GHSA-hp3w-g68c-fv3c).
An attacker-controlled precision specifier can trigger an uncaught `RangeError`
and deny service. The advisory lists no patched version.

The installed path is:

```text
electron-builder 26.16.1
  app-builder-lib 26.16.1
    @electron/get 3.1.0
      global-agent 3.0.0
        roarr 2.15.4
          sprintf-js 1.1.3
```

npm also flags the ancestor packages, `dmg-builder` and
`electron-builder-squirrel-windows`; these are not eight independent defects.
The affected downloader's optional proxy support loads global-agent, whose
logger uses roarr/sprintf-js. This tree runs during development and packaging.
The application has no production npm dependencies, and its package file list
includes application source/assets rather than these builder modules. Electron
itself is also declared as a devDependency, so a clean `--omit=dev` audit alone
does not assess the shipped Electron/Chromium runtime.

Accepted for this release preparation because the vulnerable formatter is in
build tooling and requires attacker control of a format string. Build inputs
and proxy configuration must remain trusted. This acceptance does not assert
that every formatter call is unreachable. Keep the advisory visible: do not
disable audit or add an ignore. Recheck before each release and when upgrading
electron-builder; remove the acceptance when its dependency chain is fixed.
Reassess immediately if these packages become runtime dependencies or accept
untrusted build configuration. `npm audit fix --force` currently proposes a
builder downgrade to 26.5.0, rather than a patched sprintf-js; that is not an
appropriate automatic fix.

## Reproduce

From `desktop/`:

```sh
npm ci
npm audit --json
npm audit --omit=dev --json
npm ls brace-expansion http-cache-semantics fast-uri sprintf-js --all
npm test
```

From the repository root, install the audit tool into a separate environment:

```sh
python3 -m venv /tmp/openscope-audit-tools
/tmp/openscope-audit-tools/bin/python -m pip install pip-audit==2.10.1
/tmp/openscope-audit-tools/bin/python -m pip_audit \
  -r backend/packaging/requirements-build.txt --format json
```

The build requirements include all six pinned runtime dependencies. The audit
resolves transitive requirements too; Linux resolved ifaddr 0.2.0, altgraph
0.17.5 and pyinstaller-hooks-contrib 2026.8. These transitive versions are not
locked by the requirements files and must be re-audited for each actual build,
including platform-specific Windows/macOS dependencies. pip-audit is a review
tool and is not added to the application requirements.

## Validation and scope

- Clean npm install and all seven desktop JavaScript test files passed.
- With the new Python pins in an isolated environment: **220 tests passed,
  one platform-specific skip**. Controller lifecycle smoke tests passed.
- Rebuilt all three native backend executables with the updated Python pins.
  Frozen-controller lifecycle smoke tests passed.
- Linux x64 AppImage built using the refreshed npm lockfile and that backend.
  Its packaged backend passed startup/authentication/settings/restart/shutdown.
  The AppImage runtime responded to `--appimage-version`; graphical launch,
  clean-machine installation and hardware streaming were not retested here.
- Strict packaged notice verification passed: 183 native binary inputs, zero
  unresolved notice entries. The application ASAR contains 12 entries and no
  build-tool `node_modules`. Fresh source review verified all six runtime wheel
  records and 51 zeroconf source files against the updated source inputs.
- macOS and Windows native builds remain to be run on those platforms.

The local test artifact is
`/tmp/openscope-dependency-build/desktop/OpenScope-0.1.0.AppImage`, SHA-256:

```text
e3970bd87d563ca4240cbee9ebecb110f34fa52ebb6c17a6ab41e480b4df7624
```

Build outputs, the temporary electron-builder configuration, native inventories
and source-review report are retained under `/tmp/openscope-dependency-build/`.
The configuration uses the repository's build settings with only backend input
and output directories redirected, protecting the sensor session's existing
Python environment. Existing installers in `dist/` were not replaced.

This review queries package advisory databases. It does not independently
scan embedded FFmpeg/Qt/image codecs, Python, system libraries or the
Electron/Chromium runtime. Final native inventories, corresponding-source
checks, platform builds and hardware qualification remain covered by
[LICENSING_REVIEW.md](LICENSING_REVIEW.md) and [VALIDATION.md](VALIDATION.md).
