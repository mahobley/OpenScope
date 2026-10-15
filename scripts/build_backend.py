"""Build the Python backend on its native OS using Python 3.12."""
from pathlib import Path
import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BUILD = ROOT / "build"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("mac", "win", "linux"), required=True)
    args = parser.parse_args()
    expected = {"mac": "darwin", "win": "win32", "linux": "linux"}[args.target]
    if sys.platform != expected:
        parser.error(f"Build {args.target} on its native OS; this host is {sys.platform}")
    if sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12 for the backend build")
    if sys.version_info[:3] < (3, 12, 15):
        parser.error("Use Python 3.12.15 or a later 3.12 security release for packaging; "
                     f"this interpreter is {platform.python_version()}")
    machine = platform.machine().lower()
    supported = {"mac": {"arm64"}, "win": {"amd64", "x86_64"}, "linux": {"x86_64", "amd64"}}
    if machine not in supported[args.target]:
        parser.error(f"Unsupported {args.target} build architecture: {machine}")
    native_wheels = []
    from native_runtime import PROFILE, load_profile, checked_file
    profile = None
    if args.target == "mac" and not PROFILE.is_file():
        parser.error("Prepare the patched Mac runtime first: see docs/BUILD.md")
    if PROFILE.is_file():
        profile = load_profile()
        native_wheels = [checked_file(PROFILE.parent, item) for item in profile["wheels"]]
        if profile.get("kind") == "conda":
            prefix = PROFILE.parent / profile["environment"]
            if Path(sys.base_prefix).resolve() != prefix.resolve():
                parser.error(f"Use the prepared interpreter: {prefix}")
            if sys.platform == "win32":
                os.environ["PATH"] = os.pathsep.join(map(str, (prefix, prefix / "Library/bin", prefix / "Scripts"))) + os.pathsep + os.environ["PATH"]
            else:
                os.environ["LD_LIBRARY_PATH"] = str(prefix / "lib") + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    free = shutil.disk_usage(ROOT).free
    # The prepared minimal Mac media stack has a much smaller freeze footprint
    # than the Conda/Qt Linux stack. Native compilation has its own space checks.
    required_gib = 1 if args.target == "mac" else 2
    if free < required_gib * 1024 ** 3:
        parser.error(f"Backend packaging needs {required_gib} GiB free on {ROOT}; "
                     f"currently {free / 1024**3:.2f} GiB actually available")
    environment = BUILD / "packaging-venv"
    conda = bool(profile and profile.get("kind") == "conda")
    if conda:
        # PyInstaller's Conda support requires the real Conda prefix, not a
        # nested venv. This prefix is already isolated under this checkout.
        environment = PROFILE.parent / profile["environment"]
    else:
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / (("python.exe" if conda else "Scripts/python.exe") if sys.platform == "win32" else "bin/python")
    def run(*command):
        subprocess.run([str(python), *map(str, command)], cwd=BACKEND, check=True)
    if native_wheels:
        run("-m", "pip", "install", "--no-deps", *native_wheels)
    requirements = "packaging/requirements-conda.txt" if conda else "packaging/requirements-build.txt"
    run("-m", "pip", "install", "-r", requirements)
    run(ROOT / "scripts/verify_media_runtime.py", "--report", BUILD / "media-acceptance.json")
    run("build_info.py")
    notices = BUILD / "THIRD_PARTY_LICENSES"
    if notices.exists():
        shutil.rmtree(notices)
    run(ROOT / "scripts" / "collect_licenses.py", notices)
    run("-m", "PyInstaller", "--clean", "--noconfirm",
        "--workpath", BUILD / "pyinstaller", "--distpath", BUILD / "backend",
        "packaging/sonar_head_unit.spec")
    run(ROOT / "scripts" / "collect_licenses.py", notices,
        "--native-toc", BUILD / "pyinstaller" / "sonar_head_unit" / "COLLECT-00.toc")
    if conda:
        run(ROOT / "scripts" / "prepare_conda_sources.py")
        run(ROOT / "scripts" / "collect_licenses.py", notices,
            "--native-toc", BUILD / "pyinstaller" / "sonar_head_unit" / "COLLECT-00.toc", "--strict-native")
    output = BUILD / "backend" / "SonarHeadUnit"
    shutil.copytree(notices, output / "THIRD_PARTY_LICENSES", dirs_exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", output / "OpenScope-LICENSE")
    if args.target == "mac":
        launcher = output / "Launch Sonar Head Unit.command"
        shutil.copy2(BACKEND / "packaging" / "launch_macos.command", launcher)
        launcher.chmod(0o755)
    print(f"Backend ready: {output}")


if __name__ == "__main__":
    main()
