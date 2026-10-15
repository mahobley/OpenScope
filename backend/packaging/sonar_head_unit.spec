"""Native PyInstaller spec for macOS, Windows, and Linux."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


PROJECT_ROOT = Path(SPECPATH).parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"

datas = [
    (str(PROJECT_ROOT / "build" / "build-manifest.json"), "."),
    (str(BACKEND_ROOT / "controller_web"), "controller_web"),
    (str(BACKEND_ROOT / "live_viewer" / "web"), "live_viewer/web"),
]
binaries = []
hiddenimports = []
runtime_hooks = []
for package in ("cv2", "numpy", "PIL", "dpkt", "serial", "zeroconf"):
    # ``collect_all`` recursively imports each package to discover every
    # submodule.  In particular, that pulls in optional protocol
    # modules and NumPy's test suite, making a release build needlessly slow
    # and fragile.  Application imports are discovered by Analysis; bundle
    # only the package data and native libraries that cannot be inferred.
    datas.extend(collect_data_files(package))
    binaries.extend(collect_dynamic_libs(package))

# ctypes loads libpcap at runtime. Bundle it explicitly on Linux; macOS uses
# Apple's system library and Windows requires separately installed Npcap.
import sys
qt_platforms = Path(sys.base_prefix) / "lib/qt6/plugins/platforms"
if sys.platform == "win32":
    qt_platforms = Path(sys.base_prefix) / "Library/lib/qt6/plugins/platforms"
if sys.platform == "linux" and qt_platforms.is_dir():
    for name in ("libqxcb.so", "libqoffscreen.so", "libqminimal.so"):
        plugin = qt_platforms / name
        if not plugin.is_file():
            raise RuntimeError(f"Missing Qt GUI plugin: {plugin}")
        binaries.append((str(plugin), "qt6/plugins/platforms"))
    runtime_hooks.append(str(BACKEND_ROOT / "packaging/qt_runtime_hook.py"))
if sys.platform == "win32" and qt_platforms.is_dir():
    for name in ("qwindows.dll", "qoffscreen.dll", "qminimal.dll"):
        plugin = qt_platforms / name
        if not plugin.is_file():
            if name == "qwindows.dll":
                raise RuntimeError(f"Missing Qt GUI plugin: {plugin}")
            continue
        binaries.append((str(plugin), "qt6/plugins/platforms"))
    runtime_hooks.append(str(BACKEND_ROOT / "packaging/qt_runtime_hook.py"))
from ctypes.util import find_library
from PyInstaller.depend.utils import _resolveCtypesImports
if sys.platform == "linux":
    library = find_library("pcap")
    resolved = _resolveCtypesImports([library]) if library else []
    if not resolved:
        raise RuntimeError("Install libpcap before building the Linux backend")
    binaries.extend((source, ".") for _, source, _ in resolved)


def analysis(script: str) -> Analysis:
    return Analysis(
        [str(BACKEND_ROOT / script)],
        pathex=[str(BACKEND_ROOT)],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=runtime_hooks,
        # Test/debug modules are not runtime dependencies. In particular,
        # don't accidentally pull GNU Readline in through optional test imports.
        excludes=["tkinter", "scapy", "pytest", "_pytest", "readline"],
        noarchive=False,
    )


controller_analysis = analysis("browser_controller.py")
engine_analysis = analysis("head_unit_emulator.py")
viewer_analysis = analysis("live_viewer/live_viewer.py")

# Conda's NumPy hook recursively collects Python's optional Readline library
# even though the readline module is excluded above. Keep it out only after
# proving no retained native input requires it.
if sys.platform == "linux" and (Path(sys.base_prefix) / "conda-meta").is_dir():
    from PyInstaller.depend.bindepend import get_imports
    checked = set()
    for graph in (controller_analysis, engine_analysis, viewer_analysis):
        retained = [entry for entry in graph.binaries if not Path(entry[0]).name.startswith(("libreadline.", "libhistory."))]
        for name, source, kind in retained:
            if kind not in {"BINARY", "EXTENSION"} or source in checked:
                continue
            checked.add(source)
            if any(Path(dependency).name.startswith(("libreadline.", "libhistory.")) for dependency, _ in get_imports(source)):
                raise RuntimeError(f"Unexpected Readline dependency requires review: {source}")
        graph.binaries = retained
        graph.datas = [entry for entry in graph.datas if not Path(entry[0]).name.startswith(("libreadline.", "libhistory."))]

controller_pyz = PYZ(controller_analysis.pure)
engine_pyz = PYZ(engine_analysis.pure)
viewer_pyz = PYZ(viewer_analysis.pure)

controller_exe = EXE(
    controller_pyz,
    controller_analysis.scripts,
    [],
    name="SonarHeadUnitController",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    # A real onedir build keeps native libraries beside the executable.
    # Embedding them in EXE creates one-file extraction at each startup.
    exclude_binaries=True,
)
engine_exe = EXE(
    engine_pyz,
    engine_analysis.scripts,
    [],
    name="SonarHeadUnitEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
)
viewer_exe = EXE(
    viewer_pyz,
    viewer_analysis.scripts,
    [],
    name="SonarHeadUnitViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
)

COLLECT(
    controller_exe,
    engine_exe,
    viewer_exe,
    controller_analysis.binaries,
    controller_analysis.zipfiles,
    controller_analysis.datas,
    engine_analysis.binaries,
    engine_analysis.zipfiles,
    engine_analysis.datas,
    viewer_analysis.binaries,
    viewer_analysis.zipfiles,
    viewer_analysis.datas,
    strip=False,
    upx=False,
    name="SonarHeadUnit",
)
