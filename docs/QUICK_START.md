# Install and start a session

## Hardware

Use a powered GLS10 with an LVS32 transducer and a dedicated Ethernet adapter
connected directly to the computer. Keep the sonar connection separate from
your normal internet/network interface.

```text
LVS32 transducer ── GLS10 ── Ethernet cable/adapter ── computer
                      └── appropriate power supply
```

Follow the hardware manufacturer's connection and power instructions.
Packaged apps have been tested with sonar hardware on Apple Silicon Macs,
Windows x64, and Linux x64 (DEB and AppImage).

On any operating system, explicitly select the adapter connected to the GLS10
when multiple wired Ethernet interfaces are active. Open **Advanced → Connection
and logging** and choose the **Sonar Ethernet interface** before starting a session.
The app remembers your selection. Auto-detect prefers an adapter already assigned
the head-unit address, otherwise it requires a single eligible wired adapter;
it does not choose the first or second entry arbitrarily. If sonar enable fails
and other wired adapters are available, the top session bar suggests alternatives.
Use **Choose another adapter** to open the controls, then apply and restart the
session with the adapter connected to the GLS10.

## macOS app

1. Download the Apple Silicon DMG for the release.
2. Open the DMG and drag OpenScope to Applications.
3. Open OpenScope. The current build is unsigned; macOS may require an explicit
   approval in Privacy & Security. Approve only an artifact you trust.
4. Connect the dedicated Ethernet adapter and approve the administrator prompt.
5. Select the sonar adapter in the connection controls and start a session.

The app needs administrator authorization for raw packet capture and adapter
setup. macOS journals app-owned network changes and restores them at shutdown.
After an interrupted session, reconnect the same adapter and use **Restore
network settings** in **Connection diagnostics** if recovery remains pending.

## Windows and Linux

Download the Windows EXE or the Linux DEB/AppImage for the release. All release
installers are unsigned. Windows may show a security warning when opening the
installer. Install Npcap separately with WinPcap API compatibility enabled, then
install and launch OpenScope. Approve the Windows administrator prompt so the
app can configure the sonar adapter and capture/send network packets.

On Debian/Ubuntu, install the DEB with `sudo apt install ./OpenScope_0.1.0_amd64.deb`,
then launch OpenScope from the application menu. Alternatively, make the AppImage
executable and open it. See [Linux build/install notes](BUILD.md#linux--x64) for
AppImage runtime requirements. Run the app as your normal user: it requests
backend administrator authorization through `pkexec` for sonar networking.
Linux requires polkit and a running desktop authentication agent.

Linux records its address and neighbor changes and restores them when the session
ends, including failed startup. After an interruption, recovery runs before the
next session. Reconnect a missing adapter and use **Restore network settings** if
recovery remains pending. Addresses left by older builds without a recovery
journal must be removed manually; the app reports conflicts before negotiation.

## View and record

Check that the live sonar display updates. Adjust range and display gain; verify
that range changes actually take effect. Disabling sonar can fail intermittently,
and range control can require a restart; see [known issues](KNOWN_ISSUES.md).

Scroll to **Recording & outputs**, choose a destination and recording format,
then **Apply paths** before recording. You can record video, PNGs with metadata,
or both. Stop recording before changing output formats or disconnecting a drive.
Stop recording and the session before quitting the app.

If startup fails, use **Export diagnostics** and follow the issue-reporting
instructions. The current build has no automatic update workflow; install a
newer release manually.
