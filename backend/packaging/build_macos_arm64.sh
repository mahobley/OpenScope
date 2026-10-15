#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${SONAR_BUILD_PYTHON:-python3.12}"
exec "$python_bin" "$script_dir/../../scripts/build_backend.py" --target mac
