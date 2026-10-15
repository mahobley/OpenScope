# Build native desktop apps

Build each platform on that platform, using Python 3.12.15 or a newer 3.12
security release and Node.js 22.12+ with npm.
Python and Electron architectures must match the target. Install Git for a
checkout; source archives contain the same build scripts and lockfile.
Builds download pinned Python packages and Electron/npm dependencies, so internet
access and several GB of free disk space are needed. These are unsigned builds.

All paths below are relative to the repository root unless stated otherwise.
Every desktop format (`.deb`, AppImage, DMG, and Windows installer) is written to
the root `dist/` directory. Generated backend files and packaging intermediates
are kept in root `build/`; application source lives in `backend/` and `desktop/`.

The packaged apps have been tested with sonar hardware on Apple Silicon Macs,
Windows x64, and Linux x64 (DEB and AppImage). New builds still need verification
on their target platform; broader OS/hardware compatibility remains open.

Packaging on all three platforms now rejects stale Python runtime pins, older
Python security patch levels, unresolved native notices, and inspected FFmpeg
configurations outside the LGPL policy (GPL/nonfree features). An enabled but
uninspected FFmpeg runtime also requires review. These checks do not establish
that all native CVEs or corresponding-source obligations have been resolved.

**macOS:** release packaging requires the locally rebuilt runtime described below.
It replaces the previously audited vulnerable OpenCV/FFmpeg native stack and
Pillow's PNG/TIFF copies. Existing upstream Mac wheels are not release inputs.

**Linux:** use the isolated native runtime below. It preserves Qt GUI support
and rebuilds LGPL FFmpeg 8.1.3 with OpenH264. Its Conda OpenCV distribution uses
upstream version 5.0.0; PyPI's 5.0.0.93 is a separate wrapper revision.

**Windows:** prepare the isolated Conda runtime below instead of using the PyPI
OpenCV wheel's older embedded FFmpeg. The runtime has shared, inspectable LGPL
FFmpeg 9.0.2 and retains Qt GUI support. See [RELEASE_STATUS.md](RELEASE_STATUS.md)
for current installer and hardware qualification before planning a public release.

On every operating system, select the adapter connected to the GLS10 explicitly
when multiple wired Ethernet interfaces are active: open **Advanced → Connection
and logging** and choose the **Sonar Ethernet interface**.

## macOS / Apple Silicon

On an Apple Silicon Mac, use native arm64 Python 3.12 to bootstrap the isolated
Python 3.12.15 runtime, Node.js 22.12+, and Xcode Command Line Tools. The first
native source build needs substantially more space than packaging (allow 10 GB).
An already prepared runtime can be reused; its wheel and recipe hashes are checked.
From the repository root:

```bash
python3.12 scripts/prepare_macos_runtime.py
```

Then package (at least 2 GiB actually available on the checkout's filesystem):

```bash
./desktop/scripts/build_electron_dmg.sh
```

This performs a clean npm install, freezes the Python backend, produces the DMG,
and runs the existing macOS package/lifecycle verification. The DMG is in
`dist/`. Intel Macs are not supported by this build recipe.

For an incremental build with dependencies already installed (the Mac backend
requires 1 GiB free; a clean npm/DMG build requires 2 GiB):

```bash
cd desktop
npm run build:mac
```

Signing and notarization are not set up by this repository. See
[Electron's signing guide](https://www.electronjs.org/docs/latest/tutorial/code-signing)
before publishing a signed distribution.

## Windows / x64

Use an x64 Windows machine, Conda/Miniforge, Python 3.12 for bootstrap, and x64
Node.js 22.12+. The preparation script installs Python 3.12.15, OpenCV 5.0.0,
Pillow 12.3.0 and LGPL FFmpeg 9.0.2 under `build/native-conda`; your system Python
and Conda base environment remain unchanged. A 3.12.10 bootstrap interpreter is
acceptable; it is not the interpreter shipped in the installer. Retain the
isolated `build/conda-pkgs` cache and allow at least 10 GB of working space.
In PowerShell from the repository root, substitute your Conda executable path:

```powershell
py -3.12 scripts/prepare_windows_runtime.py --conda "$env:USERPROFILE\miniconda3\Scripts\conda.exe"
cd desktop
npm ci
npm test
npm run build:win
```

The script builds the native `.exe` backend and an NSIS installer in
`dist/`. Install Npcap separately on the target machine; it is not bundled.
The prepared runtime is selected automatically by subsequent builds. Native
notices and matching required Conda sources/recipes are collected after freezing.
To retain the exact selected package versions and prepare release source assets:

```powershell
cd ..
& "$env:USERPROFILE\miniconda3\Scripts\conda.exe" list --prefix build/native-conda --explicit | Set-Content -Encoding ascii build/conda-explicit.txt
.\build\native-conda\python.exe scripts/package_windows_sources.py
```

The installer requests administrator execution for the application because the
current shell does not implement separate Windows backend elevation.
This behavior needs interactive install and hardware testing before public release.

## Linux / x64

Use an x86_64 Linux machine with Python 3.12 for bootstrap, Conda/Miniforge,
GCC/Make/pkg-config, and Node.js 22.12+. The runtime and package cache are isolated
under `build/`; retain the cache for native notices and exact source recipes.
Allow at least 10 GB on the checkout's filesystem. Install the distribution's
libpcap runtime, iproute2, Electron desktop dependencies, and polkit with a desktop
authentication agent. Build on the oldest supported distribution.

```bash
python3.12 scripts/prepare_linux_runtime.py --conda /path/to/conda
cd desktop
npm ci
npm run build:linux
```

The prepared Python and native libraries are selected automatically. Native
notices and required Conda source recipes/archives are collected automatically
after freezing; the first run also downloads those source archives. Linux
packaging uses that isolated Conda prefix directly because PyInstaller's Conda
support does not work inside a nested virtual environment. Ordinary pip installs
must not overwrite its OpenCV/Pillow/NumPy native dependencies.

An AppImage is written under `dist/`. Make it executable to launch it.
Some distributions require a FUSE runtime for AppImage mounting. The desktop
shell requests administrator authorization through `pkexec` for the backend;
Electron stays under the desktop user. AppImage launches
copy the bundled backend to a private temporary directory under the app's user
data directory so the elevated backend does not need access to the FUSE mount.
This copy is removed when the backend exits normally; an abrupt desktop failure
can leave an unused `backend-runtime-*` directory behind.
Linux journals app-owned address and neighbor changes, restores them when the
session ends or before the next session after an interruption, and checks that
GLS10 traffic routes through the selected adapter before negotiation. Pre-existing
settings are preserved; older unjournaled sonar addresses require manual cleanup.
The privileged terminal/browser workflow remains a fallback when desktop
authorization is unavailable. Linux elevation and hardware operation still need
manual qualification. Do not publish this AppImage as a qualified hardware release yet.

### Debian / Ubuntu `.deb`

On an x86_64 Debian or Ubuntu desktop, use the same Linux prerequisites above.
The existing electron-builder setup also supports a `.deb` installer. From the
repository root, replace the example maintainer with your name and email:

```bash
cd desktop
npm ci
node scripts/build-backend.js linux
npx electron-builder --linux deb --x64 \
  --config.deb.maintainer="Your Name <you@example.com>" \
  --config.extraMetadata.homepage="https://github.com/mahobley/openscope"
```

The command freezes the native Python backend and bundles it into a `.deb` under
`dist/`, named `OpenScope_<version>_amd64.deb` (for example,
`OpenScope_0.1.0_amd64.deb`). Its Debian package name is `openscope`.
The metadata overrides supply the maintainer and project URL
required by the Debian packaging target, including when building a source archive
without Git metadata. If the backend was already built on this machine, you can
skip `node scripts/build-backend.js linux`.

From `desktop/`, inspect and install the generated package (substitute its actual
filename if multiple versions are present):

```bash
dpkg-deb --info ../dist/*.deb
sudo apt install ../dist/*.deb
```

If you installed an older package named `sonar-head-unit-desktop`, quit OpenScope
and remove it with `sudo apt remove sonar-head-unit-desktop` before installing
the renamed `openscope` package. Both install into `/opt/OpenScope`, so they
cannot be installed together. Replace any old pinned launcher with the new
**OpenScope** entry in the application menu.

Launch **OpenScope** from the desktop application menu as your normal user.
The backend still requests administrator authorization through `pkexec`, so polkit
and a desktop authentication agent are required. The `.deb` does not need FUSE.
Both the DEB and AppImage have been tested with sonar hardware.

## Build details

`backend/requirements.txt` pins runtime dependencies. The packaging requirements
add pinned PyInstaller. `desktop/package-lock.json` pins npm dependencies.
`scripts/build_backend.py` creates `build/packaging-venv/`, generates build
identity and Python dependency license notices, and freezes three sibling
executables into `build/backend/SonarHeadUnit/`. Electron embeds that complete
runtime under `resources/backend/` (inside Contents/Resources on macOS).
PyInstaller working files are kept in `build/pyinstaller/`, and the generated
build manifest and dependency license notices also live under `build/`.

Use `SONAR_BUILD_PYTHON` to select a Python 3.12 executable. The generic backend
build can also be run directly with `python3.12 scripts/build_backend.py --target
linux` or `py -3.12 scripts/build_backend.py --target win` from the root. Targets
are native-only; cross-compiling the Python backend is not supported.

Packet capture and raw Ethernet transmission use libpcap/Npcap with dpkt for
packet parsing. Linux builds bundle the installed libpcap runtime and collect
its distribution copyright notices from `/usr/share/doc/libpcap*/copyright`.
If that location is unavailable, set `SONAR_LIBPCAP_NOTICES` to a file containing
the complete notices for the exact libpcap build. macOS uses its system libpcap;
Windows requires separately installed Npcap. New builds do not include Scapy.
The replacement packet adapter still requires real sonar qualification on each
offered platform, including Ethernet transmission, sustained capture, recording,
and shutdown. Existing hardware results do not qualify this new adapter.

References: [PyInstaller platform builds](https://www.pyinstaller.org/en/stable/usage.html),
[electron-builder Linux](https://www.electron.build/docs/linux/) and
[Windows NSIS](https://www.electron.build/docs/nsis/).

## License output

The backend build collects Python notices before freezing, then collects notices
for the exact native inputs from PyInstaller's `COLLECT-00.toc`. Debian/Ubuntu
package copyright files and Conda's cached `info/licenses/` files are retained
when those libraries are bundled. Keep the Conda package cache when building
with Conda. Missing notices appear in `native-inventory.json` for review.
The generated `runtime-build.json` records OpenCV and inspectable FFmpeg build
information. Electron's packaging hook requires the project/dependency notices
and copies the exact runtime's Chromium notices into `resources/licenses/`.

See [LICENSING.md](LICENSING.md) for source preparation, complete corresponding
source assets, and instructions for rebuilding with modified LGPL components.
Prepare initial source inputs with:

```bash
python3 scripts/prepare_license_sources.py --output build/license-sources
```

This downloads the pinned source inputs; it does not clear unreviewed patches,
other platform variants, or additional copyleft native libraries in a build.

## Check before distribution

Run the automated tests and the manual checklist in [RELEASING.md](RELEASING.md).
The macOS package verifier is macOS-specific. Windows/Linux builds currently
need manual install/startup/backend checks in addition to the source tests.
A manually triggered CI workflow builds platform artifacts for review; it
never publishes a release automatically.
