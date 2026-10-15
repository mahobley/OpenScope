"""Build and finalize the patched Apple Silicon runtime, or verify an existing one."""
import argparse
import subprocess
import sys
from native_runtime import PROFILE, ROOT, load_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if PROFILE.is_file() and not args.rebuild:
        load_profile()
        print("Verified existing patched runtime:", PROFILE)
        return
    for script, options in [
        ("build_macos_native.py", ["all", "--jobs", str(args.jobs)]),
        ("repair_macos_pillow.py", []),
        ("finalize_macos_native.py", []),
    ]:
        subprocess.run([sys.executable, str(ROOT / "scripts" / script), *options], check=True)
    load_profile()


if __name__ == "__main__":
    main()
