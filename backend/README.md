# GLS10-compatible viewer

This directory contains the Python backend: the emulator, browser UI, viewer,
runtime configuration, and automated tests. Generated backend files are written
to the repository's `build/` directory; finished desktop installers go in `dist/`.

## Install

From this directory:

```bash
conda config --set channel_priority flexible
conda env create --file environment.yml
conda activate emulator
python -m pytest -q
```

The environment file uses packages from both `defaults` and `conda-forge`.
Flexible channel priority is required because strict priority can exclude the
`conda-forge` builds of `dpkt`, `libpcap`, and `pyserial` and cause environment solving to
fail.

If environment creation was interrupted after creating the environment, remove
that incomplete environment and retry:

```bash
conda env remove --name emulator
conda config --set channel_priority flexible
conda env create --file environment.yml
```

## Run in the browser

Run with the network privileges required by packet capture and adapter setup:

```bash
sudo "$CONDA_PREFIX/bin/python" browser_controller.py
```

Open `http://127.0.0.1:8765`.

Packet capture and polar recording run independently of screen rendering. In
**Recording & outputs**, choose **Video only**, **PNGs with metadata only**, or
**Video + PNGs with metadata** before recording. This choice applies to manual
sonar recording and autosave. Stop sonar recording and autosave before changing
formats. The default saves both. The standalone viewer also accepts
`--no-save-frames` for video only or `--no-save-video` for PNGs with metadata only.
When PNG recording is selected, decoded polar PNGs and their metadata are saved
for every complete frame while sonar recording is active. The frames folder is
created when the first PNG or snapshot is saved, rather than at session startup
or when output paths are applied. These PNGs preserve
the original intensities and packet capture timestamps; display gain, intensity
filtering, motion effects and Cartesian conversion do not change the archive.
The display uses the newest saved/captured frame and skips older display updates
when rendering falls behind, without building an image backlog.

The GUI shows capture FPS and render FPS separately. Render FPS measures images
produced by Python; the browser may display fewer. MP4 recordings retain their
configured constant playback FPS, while the PNG archive retains each decoded
frame. The scrolling echogram and its PNG capture follow the display loop.
Disk throughput and packet decoding can still limit capture/save frequency.

Incomplete packet frames expire after five seconds and are bounded to 64 frames
and 32 MiB of chunk data. Individual frames over 16 MiB are rejected. Device
frame-ID resets and wraparound discard old partial assemblies; small packet
reordering is tolerated.

Recording checks available disk space once per second and stops before free
space falls below 256 MiB. It also checks that the original output volume is
still present. Recording failures remain in a visible banner until dismissed;
capture and viewing continue. Free space is displayed while recording.
Reconnect a removed volume and explicitly restart recording after fixing the
problem. PNG captures with repeated device frame IDs receive unique names.
Metadata is staged first and the completed PNG is published last. Snapshots
and echogram PNGs are also encoded to temporary files before publication.
Videos retain an `.inprogress` filename until the writer finishes and the file
is renamed. A forced kill may leave an unfinished video or hidden staging files;
previous completed recordings are preserved.

Startup normalizes ownership only for the output root and newly written files,
so existing archives do not delay launch. To migrate recordings made by older
versions, explicitly run the controller with
`--repair-recording-permissions --recordings-root /path/to/recordings` under the
same `sudo` invocation used above. This repairs existing file ownership and exits.

On macOS and Linux, network setup adds the head-unit address to the dedicated
sonar adapter and journals app-owned changes for restoration on exit. Linux
also checks that GLS10 traffic routes through the selected adapter. Reconnect the adapter
and use **Restore network settings** under **Connection diagnostics** if an
interrupted session leaves recovery pending. CLI users can also run the engine
with `--restore-network --network-state /path/to/network-recovery.json` with
the required network privileges. Recovery preserves unrelated addresses and
preexisting static-neighbor/route entries.

The browser viewer accepts external-drive destinations. Choose **Choose folder
for all recordings…**, then **Apply paths**, to move video, echogram PNGs and
snapshots together. Separate folder pickers remain available for each output.
The integrated viewer opens its capture socket with administrator privileges
then drops to the invoking user for recording and folder selection. The engine
retains the privileges needed for network setup and restoration.

Output destinations and display/format settings persist between desktop
sessions. A disconnected remembered drive blocks recording; reconnect it and
apply its paths, or select another folder. Drive removal or low disk space
stops saving while the live session continues, with an error that remains until
acknowledged. Normal video-stop errors also preserve the live session and keep
unfinished video files for recovery. The dashboard identifies its version and
source build; packaged releases embed the same build identification.

Echogram capture saves numbered PNG segments even during manual recording. It
starts a new segment when the column buffer would exceed 32 MiB, the image
reaches 8,192 columns, or the image height changes. Saving temporarily uses
additional bounded memory to combine and encode the segment. Autosave also
splits at the selected time interval, whichever limit is reached first. Finishing
capture or quitting normally saves the last partial segment; a forced kill can
lose that unfinished segment, but previously saved segments remain on disk.

Use **Hide display**, before the colour dropdown, to suspend display processing
and hide both image panels. Capture and active polar recordings continue, while
Cartesian transformation, colour rendering, histograms, JPEG encoding and
echogram generation stop. Echogram capture pauses too. **Show display** resumes
using the latest captured frame rather than replaying skipped display frames.

The emulator uses the qualified minimized protocol behavior by default. The
browser binds to localhost only; do not expose it through a proxy or tunnel
without adding authentication.

## Layout

- `head_unit_emulator.py` — protocol implementation.
- `browser_controller.py` — local browser-session controller.
- `live_viewer/` and `controller_web/` — rendering and browser assets.
- `tests/` — release-focused automated tests.

See [terminal setup](../docs/RUN_FROM_SOURCE.md), [build instructions](../docs/BUILD.md),
and [known issues](../docs/KNOWN_ISSUES.md). Only Apple Silicon MacBooks and Linux
have been tested; sonar disable can fail and range control can require a restart.
