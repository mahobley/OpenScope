# Run from a terminal without packaging

These commands start the Python controller and browser interface directly.
They do not build an installer and do not require Node.js or Electron.
Use Python 3.12 and run commands from the repository root.

OpenScope has been tested with sonar hardware on Apple Silicon Macs, Windows
x64, and Linux x64. Packaged downloads are described in [QUICK_START.md](QUICK_START.md).

## macOS and Linux

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
sudo .venv/bin/python backend/browser_controller.py --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765 in a browser, select the dedicated sonar Ethernet
adapter, and start a session. Stop recording and the session, then press Ctrl+C
to shut down. Do not expose this local service through a public proxy or tunnel.

On Linux, install your distribution's `libpcap` runtime and `iproute2` network tools before a hardware
session. Graphical/OpenCV native-library requirements depend on the distribution;
Ubuntu commonly needs `libgl1` and `libglib2.0-0` (or its current replacement).
Configure the dedicated adapter through the app. Linux journals app-owned address
and neighbor changes and restores them when the session ends or before the next
session after an interruption. Reconnect the adapter and use **Restore network
settings** if recovery is pending. Pre-existing addresses are preserved; remove
conflicting sonar addresses left by older builds manually.

If pip dependencies cause native-library problems on macOS, the existing Conda
alternative remains available:

```bash
conda config --set channel_priority flexible
conda env create --file backend/environment.yml
conda activate emulator
sudo "$CONDA_PREFIX/bin/python" backend/browser_controller.py
```

## Windows

Install Python 3.12 and install [Npcap](https://npcap.com/) separately, enabling
WinPcap API compatibility. Npcap is not bundled with OpenScope. See
[Npcap installation guidance](https://npcap.com/guide/npcap-users-guide.html).

In PowerShell from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
```

Then open PowerShell as Administrator, change to this checkout, and run:

```powershell
.\.venv\Scripts\python.exe backend/browser_controller.py --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. Use a dedicated Ethernet adapter, allow the necessary
local-network traffic if prompted, and restore any adapter changes manually
after the session. Report Windows failures rather than assuming compatibility.

## Run the desktop shell from source (optional)

This additionally requires Node.js 22.12 or newer and npm:

```bash
cd desktop
npm ci
SONAR_PYTHON="$(pwd)/../.venv/bin/python" npm start
```

This command allows the normal backend administrator prompt on macOS and Linux.
Linux requires `/usr/bin/pkexec` and a running desktop polkit authentication
agent; use the terminal/browser workflow above if authorization is unavailable.
For UI-only development on macOS or Linux, use
`SONAR_SKIP_ELEVATION=1` with the same command.

On Windows, after installing Python dependencies, use PowerShell:

```powershell
cd desktop
npm ci
$env:SONAR_PYTHON = (Resolve-Path ..\.venv\Scripts\python.exe).Path
npm start
```

Hardware operation requires the appropriate administrator permissions. Do not
run the Linux Electron application as root or disable its sandbox to bypass
permission errors.
