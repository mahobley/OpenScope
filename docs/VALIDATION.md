# Release validation

## Packaged hardware testing — 2026-10-15

The maintainer confirms all offered packages have now been tested with sonar
hardware: macOS Apple Silicon DMG, Windows x64 EXE, and Linux x64 DEB/AppImage.
The known sonar-disable and range-control issues remain. Installers are unsigned
and request administrator rights for networking. See [RELEASE_STATUS.md](RELEASE_STATUS.md)
for automated build checks. The records below describe earlier preparation stages.


## Packet adapter migration — 2026-10-14, Linux x86_64

- Replaced application Scapy imports with the libpcap/Npcap adapter and dpkt 1.9.8.
- Python source suite: 206 passed, 1 platform-specific skip, 22 existing Pillow
  deprecation warnings, using the pinned runtime dependencies on Python 3.12.
- All six desktop JavaScript test files passed.
- Source controller smoke check passed: authenticated API, persistent settings,
  restart, and clean shutdown.
- New packet tests cover Ethernet/ARP wire bytes, IPv4/IPv6 UDP extraction,
  capture timestamps, mDNS questions/SRV records and checksums, malformed and
  fragmented packets, adapter metadata, cancellation, and capture/send failures.
  A saved Ethernet capture was read through real libpcap without capture privileges.
- Linux native interface enumeration succeeded. The frozen three-executable
  backend built, and the frozen controller passed the lifecycle smoke check.
  Its Python archives include dpkt and the adapter and contain no Scapy modules.
  All three frozen executables passed `--help`; the frozen controller's
  authenticated interface API enumerated the host's two eligible Ethernet adapters.
- A fresh license inventory includes dpkt's license and the installed Linux
  libpcap distribution's copyright notices, with no Scapy package entry.

No live packet capture/transmission or sonar hardware session was performed.
macOS/Windows adapter behavior and installer builds have not been tested for
this migration. Earlier hardware results and pre-existing installers do not
qualify the replacement packet adapter. Rebuild installers and qualify each
offered platform before release. The remaining native-library
review remains a separate release task. npm/Python dependency
audit results are now recorded in [DEPENDENCY_AUDIT.md](DEPENDENCY_AUDIT.md).

## Earlier preparation record — Apple Silicon

Validated on 2026-10-14 in the separate release checkout on an Apple Silicon Mac.

- Python source suite: 158 tests passed (22 Pillow deprecation warnings).
- Desktop JavaScript suite: 20 tests passed.
- Source backend smoke check: startup, authenticated API, persistent settings,
  restart, and shutdown passed without sonar hardware.
- Python dependency license collection: completed successfully.
- Documentation links and shell syntax: checked.

An initial Python run failed 14 recording tests while less than 256 MiB was
free; that exercised the existing low-disk guard. The suite passed after removal
of disposable files created by this preparation.

The attempted Mac build exposed and corrected a license-metadata collection
error. Full installer rebuilding and package verification remain pending because
there is insufficient free disk space. Build entry points now require at least
3 GiB before creating a backend environment. This branch contains no newly
qualified installer artifacts.

Windows and Linux installer builds have not been executed locally. The manual
CI packaging workflow is provided but has not been run. Hardware controls and
clean-machine installation were not retested during this preparation. Existing
reported testing is limited to Apple Silicon MacBooks and Linux; known sonar
disable and range-control issues remain.

The earlier npm install reported 11 audit findings (9 moderate, 2 high); this is
superseded by [the dependency audit and fixes](DEPENDENCY_AUDIT.md). Dependency
license/source obligations and platform signing likewise remain release checks.
