# Native backend packaging

See [the build guide](../../docs/BUILD.md) for all three operating systems.
The common PyInstaller spec freezes the controller, engine, and viewer into
`build/backend/SonarHeadUnit/`. Keep the complete directory together.

The macOS wrapper is retained for compatibility. The native build helper is
`scripts/build_backend.py`; it requires Python 3.12 on the target OS/architecture.
Packaged apps have been tested on macOS Apple Silicon, Windows x64, and Linux x64.
