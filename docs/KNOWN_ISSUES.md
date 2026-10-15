# Known issues and tested platforms

Packaged apps have been tested with sonar hardware on Apple Silicon Macs,
Windows x64, and Linux x64, including the DEB and AppImage. Intel Macs have not
been tested. Minimum OS versions and a broader device/firmware compatibility
matrix remain unqualified. Linux source operation additionally requires libpcap.

## Sonar disable is intermittent

The **Disable sonar** control sometimes does not work. A UI state change does
not prove that the transducer stopped transmitting. Confirm the device response;
if it continues, stop the session and follow the hardware manufacturer's
instructions to stop or power down the sonar. Record the steps and diagnostics
when reporting this issue.

## Range changes can require a restart

Sometimes a restart is needed before range can be controlled. Stop recording
first so files can be finalized, stop the session, then quit and relaunch
OpenScope and retry. The precise restart needed for each failure has not been
characterized; if relaunching the app does not help, follow the hardware
manufacturer's restart procedure. Report which restart restored control.

## Packaging and privileges

- Release installers are unsigned. macOS builds are not notarized; macOS and
  Windows may display security warnings during installation or first launch.
- OpenScope requests administrator rights to configure the sonar network adapter
  and capture/send packets. On macOS and Linux, only the backend is elevated.
  Linux requires `/usr/bin/pkexec` and a running desktop polkit authentication
  agent. Windows requests administrator execution for the application and
  requires Npcap to be installed separately.
- Automated network recovery supports macOS and Linux. Linux restores journaled
  address and neighbor changes and checks GLS10 routing before starting a session.
  Sonar addresses left by older builds without a journal require manual cleanup;
  the app reports these conflicts and preserves pre-existing settings. On Windows,
  inspect and restore app-made adapter, neighbor, and route changes yourself.
- An unavailable recording drive blocks recording. Reconnect it or select a
  new folder, apply paths, and explicitly restart recording.

## Reporting a problem

Open an issue at https://github.com/mahobley/OpenScope/issues with the app version,
OS version/architecture, adapter model, GLS10/LVS32 setup and firmware if known,
steps to reproduce, and expected/actual behavior. Include **Export diagnostics**
when available. Review logs before sharing them; remove personal paths, device
identifiers, and anything private. See SECURITY.md for vulnerability reporting.
