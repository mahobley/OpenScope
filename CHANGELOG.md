# Changelog

## 0.1.0 — beta

- Desktop sonar viewer for GLS10 with an LVS32 transducer, with live sonar,
  echogram, display controls, and video/PNG recording.
- Tested packaged apps on macOS Apple Silicon (DMG), Windows x64 (EXE), and
  Linux x64 (DEB and AppImage), including sonar hardware operation.
- Local authenticated controller startup, diagnostics export, persistent
  settings, and recording destinations.
- libpcap/Npcap packet adapter with dpkt, plus network recovery on macOS and Linux.
- Updated bundled runtimes, dependency notices, and native build recipes.

### Installation and permissions

The installers are unsigned. macOS and Windows may show security warnings;
macOS builds are not notarized. OpenScope requests administrator rights for
sonar networking, including adapter configuration and packet capture/send.
On macOS and Linux, only the backend is elevated; Windows requests administrator
execution for the application. Linux requires polkit and a desktop authentication
agent. Windows requires a separate Npcap installation.

### Known limitations

- Disabling sonar sometimes does not work; confirm the device response.
- Range control sometimes requires restarting the app or sonar setup.
- Intel Macs, exact minimum OS versions, and broader hardware/firmware
  compatibility have not been established.

See [installation instructions](docs/QUICK_START.md) and
[known issues and troubleshooting](docs/KNOWN_ISSUES.md).
