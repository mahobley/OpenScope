# Third-party notices

OpenScope's own code is provided under the [MIT License](LICENSE). Each component
below retains its upstream copyright, license, warranty disclaimer, and any
exceptions. The project MIT license does not replace those terms.

## Direct components

Versions below are the current build inputs. The Python version is selected by
the builder; the exact interpreter and bundled native components are recorded
per build. Follow the full notices for the actual artifacts being redistributed.

| Component | Version | License / attribution |
| --- | --- | --- |
| [Electron](https://github.com/electron/electron) | 44.7.2 | MIT; Electron contributors and GitHub Inc. Chromium and other embedded components retain the notices in `LICENSES.chromium.html`. |
| [CPython](https://www.python.org/) | 3.12.x | PSF-2.0 and historical component licenses; retain the Python distribution's full license and native-library notices. |
| [NumPy](https://numpy.org/) | 2.0.2 | BSD-3-Clause for NumPy; retain the wheel's notices for OpenBLAS and GNU runtime libraries, including applicable runtime exceptions and LGPL terms. |
| [opencv-python](https://github.com/opencv/opencv-python) / [OpenCV](https://opencv.org/) | 5.0.0.93 / 5.0.0 | MIT for the Python packaging project; Apache-2.0 for OpenCV. Full native-library terms are in the wheel's `LICENSE-3RD-PARTY.txt`. |
| [Pillow](https://python-pillow.org/) | 12.3.0 | MIT-CMU for this pinned release, with the wheel's additional image/codec notices. |
| [dpkt](https://github.com/kbandla/dpkt) | 1.9.8 | BSD-3-Clause; Dug Song and contributors. Preserve the complete upstream notice. |
| [libpcap](https://www.tcpdump.org/) | Native build dependent | BSD-3-Clause and retained component notices; the Regents of the University of California and contributors. Linux bundles the installed library; macOS uses its system library. |
| [pyserial](https://github.com/pyserial/pyserial) | 3.5 | BSD-3-Clause; copyright 2001–2020 Chris Liechti. The complete version-matched notice is retained in `third_party/notices/pyserial-3.5-LICENSE.txt` because the wheel omits it. |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | 0.149.17 | LGPL-2.1-or-later; Paul Scott-Murphy, William McBrine, and contributors. |
| [PyInstaller](https://pyinstaller.org/) | 6.22.2 | GPL-2.0-or-later with its distribution/bootloader exception; some files are Apache-2.0. Preserve the complete upstream `COPYING.txt`, including the exception. |

Electron-builder 26.16.1 is an MIT-licensed build tool. Build-only tools listed
in the installed-package inventory are not necessarily bundled in the app.
Scapy is no longer a dependency and must not be included in newly built installers.

## Embedded multimedia and native libraries

OpenCV wheels contain FFmpeg and other native libraries. Non-headless wheels
can also include Qt, developed by The Qt Company Ltd. and contributors.
FFmpeg is developed by the FFmpeg contributors. Those libraries' terms apply in addition to OpenCV's
Apache-2.0 license. FFmpeg's license depends on the exact enabled features;
the packaging check requires an inspected LGPL configuration without
`--enable-gpl` or `--enable-nonfree`. Corresponding
sources and build details are to be provided with each binary release as
specified in the licensing guide. Qt's LGPLv3 terms require the accompanying
GPLv3 text as well as the LGPLv3 text; both are retained in `third_party/licenses/`.

The prepared Mac runtime uses LGPL FFmpeg 8.1.3 and Cocoa. The prepared Linux
runtime uses LGPL FFmpeg 8.1.3 with OpenH264 and Qt 6.12.0. The prepared Windows
runtime uses shared FFmpeg 9.0.2 reporting LGPL v3+ with OpenH264 and Qt 6.12.0.
Mac image-library
replacements include libpng 1.6.59, libtiff 4.7.2, zlib 1.3.2 and
libjpeg-turbo 3.2.0; full notices accompany the rebuilt wheels. Linux and Windows native
components retain their exact Conda package notices, source recipes and any
applicable GNU runtime exceptions. OpenCV's upstream library version is 5.0.0
on all three platforms; the Mac wrapper uses the 5.0.0.93 packaging revision.

These statements concern the prepared runtimes, not older installers. The
previous Mac installer's GPL FFmpeg and the previous Linux wheel's FFmpeg
8.1.1/Qt 5.15.19 have been superseded. The Windows prepared runtime replaces the
PyPI wheel's older embedded FFmpeg; qualification applies to the actual built
artifact and its inventories, not other installers using the same OpenCV version.
See `docs/RELEASE_STATUS.md` for current artifact verification results.

## Finding full notices and corresponding sources

Packaged applications include the full notices and inventories under
`resources/licenses/` and `resources/backend/THIRD_PARTY_LICENSES/` (on macOS,
inside `OpenScope.app/Contents/Resources/`). Electron's exact Chromium notices
are retained in `resources/licenses/LICENSES.chromium.html`.

See [docs/LICENSING.md](docs/LICENSING.md) in the source checkout, or
`resources/licenses/LICENSING.md` in the application, for source-distribution and
library replacement/rebuild instructions. Binary releases must provide reviewed
third-party source assets alongside their installers. A generic upstream link
or a copy of a license alone does not complete those source obligations.

## Separately installed software

[Npcap](https://npcap.com/) is a separately installed Windows prerequisite under
its own license. These installers do not contain Npcap or claim redistribution
rights for it. Consult its license before changing that policy.

OpenScope is independent of Garmin. Hardware/product names describe
interoperability and do not imply endorsement.
