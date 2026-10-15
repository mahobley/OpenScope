#!/usr/bin/env bash
# Build and verify the complete unsigned Apple Silicon Electron distribution.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$script_dir/../.." && pwd)"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This build must run on an Apple Silicon Mac." >&2
  exit 1
fi
for command_name in node npm python3.12; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required build command: $command_name" >&2
    exit 1
  fi
done

build_python="${SONAR_BUILD_PYTHON:-$repo_dir/build/native-security/python/bin/python3.12}"
if [[ ! -x "$build_python" ]]; then
  echo "Prepare the patched Mac runtime first; see docs/BUILD.md." >&2
  exit 1
fi
if ! "$build_python" -c 'import sys; sys.exit(0 if (3, 12, 15) <= sys.version_info[:3] < (3, 13, 0) else 1)'; then
  echo "Use native arm64 Python 3.12.15 or a later 3.12 security release for packaging." >&2
  exit 1
fi

if ! "$build_python" -c 'import shutil,sys; sys.exit(0 if shutil.disk_usage(sys.argv[1]).free >= 2 * 1024**3 else 1)' "$repo_dir"; then
  echo "At least 2 GiB of actually available disk space is required for packaging." >&2
  exit 1
fi

cd "$repo_dir/desktop"
# Finder can recreate .DS_Store during npm/Electron's directory cleanup.
# Move disposable build trees out of the way first so both tools start fresh.
cleanup_dir="$(mktemp -d "${TMPDIR:-/tmp}/openscope-build-cleanup.XXXXXX")"
cleanup_stale_build_files() {
  if ! rm -rf -- "$cleanup_dir"; then
    echo "Build cleanup left temporary files in $cleanup_dir" >&2
  fi
}
trap cleanup_stale_build_files EXIT
for stale_path in node_modules ../dist/mac-arm64; do
  if [[ -e "$stale_path" || -L "$stale_path" ]]; then
    mv -- "$stale_path" "$cleanup_dir/$(basename -- "$stale_path")"
  fi
done
npm ci
npm run build:mac

echo "DMG ready: $repo_dir/dist/OpenScope-0.1.0-arm64.dmg"
