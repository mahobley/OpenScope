# OpenScope desktop app

This directory contains the Electron shell for the existing Python controller.
It keeps the controller and viewer interfaces intact while owning application
startup, local authentication, navigation policy, diagnostics, and shutdown.

See [build instructions](../docs/BUILD.md), [terminal setup](../docs/RUN_FROM_SOURCE.md),
and [known limitations](../docs/KNOWN_ISSUES.md). Packaged apps have been tested with sonar hardware on Apple Silicon Macs,
Windows x64, and Linux x64 (DEB and AppImage).

The sonar logo in `assets/icon-master.png` is shared by the application window,
Linux launchers, macOS Dock/app bundle, and Windows executable/installer.
Linux packages use `OpenScope.desktop` to associate the running window with its
launcher on X11 and Wayland, with standard icon sizes in `assets/icons/` derived
from the master logo. AppImage desktop-menu integration depends on the
desktop environment or an AppImage integration tool.

## Development

Install a current Node.js LTS release, then run from this directory:

```bash
npm install
npm test
npm start
```

macOS and Linux ask for administrator authorization while starting the Python backend.
Linux uses `/usr/bin/pkexec` and requires a running desktop polkit authentication
agent. Only the backend is elevated; Electron stays under the desktop user.
Use `SONAR_SKIP_ELEVATION=1 npm start` only for UI development that does not
need interface configuration or packet capture.

The development app starts `../backend/browser_controller.py` with `python3`.
Set `SONAR_PYTHON` to use a particular Python environment, or set
`SONAR_CONTROLLER_EXECUTABLE` to exercise the shell against a frozen controller.
The selected environment needs the dependencies from `backend/environment.yml`.

The controller binds OS-assigned loopback ports and emits a structured readiness
line. Electron exchanges the readiness URL's random token for an HTTP-only cookie,
then displays the existing controller interface in a sandboxed renderer with Node
integration disabled. Controller output is written to:

```text
~/Library/Application Support/OpenScope/logs/controller.log
```

Recordings are restricted to the adjacent `recordings/` directory.

The controller log is retained across launches and rotates at 2 MiB with four
backups. Startup failures before Python logging opens are saved separately in
`launcher.log`, retaining the previous launch too. The desktop follows only new
log bytes, including across rotation. **Export diagnostics** is available in
the controller header and startup error screen; it saves a JSON report with
the app version, OS/runtime details, and bounded log tails. Authentication
tokens are removed from exported logs.

On macOS, use a dedicated Ethernet adapter connected directly to the GLS10.
Administrator authorization permits raw packet access and adapter setup. The
app adds its head-unit address as an alias, preserves other IPv4 addresses,
and journals its address, static-neighbor, and fallback-route changes in
`network-recovery.json` under the application-data directory. Normal shutdown
restores these changes. An interrupted session is recovered before the next
session starts. If its adapter is disconnected, reconnect it, stop any running
session, and use **Restore network settings** in **Connection diagnostics**.
The journal stays available until recovery succeeds. Linux also journals app-owned
address and neighbor changes, restores them when the session ends or before the
next session after an interruption, and checks that GLS10 traffic routes through
the selected adapter. Older unjournaled sonar addresses require manual cleanup.
Windows retains its existing setup behavior.

Backend startup uses a per-launch lifetime file and monitors the desktop process.
Closing the app, cancelling startup, or losing the desktop process triggers
backend cleanup even before its HTTP endpoint is ready. The elevated launch
supervisor uses the original administrator authorization for termination. The
controller stops the engine's process group, and the viewer handles termination
by finishing in-progress capture writes and finalizing recordings. Shutdown
allows a bounded grace period before forcing unresponsive processes to exit.

## Unsigned Apple Silicon build

From this directory:

```bash
npm run build:mac
```

For a clean dependency install plus the complete backend, DMG, and verification
workflow, run `./desktop/scripts/build_electron_dmg.sh` from the repository root.
The script moves old dependencies and unpacked app output aside before building,
so Finder's `.DS_Store` files cannot block their cleanup with `ENOTEMPTY`.

That command first builds the three PyInstaller executables, then places their
complete onedir runtime in the application's `Contents/Resources/backend/`
directory and produces an arm64 DMG under the repository root's `dist/`.

The macOS build hook removes unused camera, microphone, audio, and Bluetooth
privacy declarations and restricts App Transport Security to local networking
before code signing runs.

To recheck an existing unpacked application without rebuilding it, run
`npm run verify:package`. This verifies the embedded executable contract,
application archive, and macOS transport/privacy metadata, then starts the
embedded controller twice to verify authentication, persistent settings and
clean shutdown. No sonar hardware or administrator access is needed for this check.

The package configuration builds unsigned beta installers and includes a
project-specific sonar icon. The Electron main
process remains unprivileged; on macOS and Linux it uses the system administrator
prompt to start only the Python backend. Signing and notarization remain pending.
Packaged hardware operation has been tested on all three platforms; broader
OS/hardware compatibility remains open.

If authorization is cancelled or the backend exits during startup, the startup
window displays the error and offers Retry and diagnostic-log controls.
The startup timeout begins after administrator authorization completes.

Use **Choose folder for all recordings…** in the viewer to select an external
drive, then **Apply paths**. Output folders, recording format, viewer adjustments,
theme and connection settings are saved across launches. If a remembered drive
is unavailable, recording is blocked until the drive is reconnected and its
paths applied, or another folder is chosen. **Open folder** controls reveal the
applied output locations. Capture opens with elevated privileges; recording
and the folder picker then run as the signed-in user.

Run `npm run smoke:backend` to check the source controller. Set `SONAR_PYTHON`
to the Python environment containing the backend dependencies. Automated source
and lifecycle checks also run through `.github/workflows/checks.yml`.
