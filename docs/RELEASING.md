# Release checklist

See [the preparation validation record](VALIDATION.md) for completed checks and
remaining validation work.

## Prepare

- [ ] Select a version and update desktop/package.json and package-lock.json together.
- [ ] Choose a beta tag (for example v0.1.0-beta.1) and update CHANGELOG.md.
- [ ] Confirm the release commit contains all required source, licenses, lockfiles,
      build scripts, browser assets, icons, and documentation.
- [ ] Review bundled dependency licenses, copyright notices, codec/native-library
      notices, and any source-distribution obligations. Do not assume the MIT
      project license replaces dependency licenses. Follow [LICENSING.md](LICENSING.md).
- [ ] Review the joint release with both co-authors and retain AUTHORS.md plus
      applicable third-party code/artwork notices.
- [ ] Establish the private security-reporting channel and enable it on GitHub.
- [ ] Review npm audit results and address or document remaining findings.
- [ ] Run Python tests, desktop tests, and the source backend smoke check.

## Build and qualify each offered platform

- [ ] Build natively using docs/BUILD.md; record Python/Node versions, OS, architecture,
      source commit, and artifact filenames in release notes.
- [ ] Verify macOS packaging and decide signing/notarization policy.
- [ ] Install on a clean machine without Python/Node or the development environment.
- [ ] Verify embedded controller, engine and viewer startup and local authentication.
- [ ] With hardware, test connection, live sonar, echogram, range, enable/disable,
      recording formats, external-drive removal, and shutdown/finalization.
- [ ] Test administrator cancellation and interrupted startup; inspect network cleanup.
- [ ] Verify dependency notices, full license texts, Chromium notices, and the
      licensing guide are present inside each app. Run from `desktop/`:
      `node scripts/verify-licenses.js /path/to/resources --strict-native`.
- [ ] Resolve every missing native notice in `native-inventory.json`; inspect
      `runtime-build.json` for actual FFmpeg flags and bundled Qt versions.
- [ ] Supply exact corresponding library sources, patches, and build recipes;
      complete and retain the platform source review record from LICENSING.md.
- [ ] Exercise the documented LGPL library replacement/rebuild procedure.
- [ ] Record exact tested OS versions, models, adapters, and firmware. Keep the
      tested macOS/Windows/Linux platforms, unsigned installer disclosure,
      administrator networking prompt, and both known control limitations in
      the published notes.

The 0.1.0 packaged apps have been hardware-tested on macOS Apple Silicon,
Windows x64, and Linux x64 (DEB and AppImage). Verify subsequent builds on each
offered platform and record any changes in test coverage.

## Package and publish

The primary downloads are the four installers: DMG, EXE, DEB, and AppImage.
Also publish reviewed corresponding third-party source archives, release notes,
quick-start/build instructions, and matching application source at the release tag.
Full notices ship inside each app; a separate notices ZIP is optional. Users only
need their platform's installer to run OpenScope. GitHub provides application
source archives for tags; confirm they include the demo image and all build inputs.
This branch omits historical research/report sources.

Create a reviewed tag and GitHub draft release; attach final installers and
corresponding third-party sources. Include the demo video link only if sharing
its unlisted URL with release readers is intended. Link the already published technical report using
its actual publication URL; do not rebuild or alter the released report here.
Publish only after reviewing the release assets and documented limitations.
This repository's build workflow uploads review artifacts and does not publish.
