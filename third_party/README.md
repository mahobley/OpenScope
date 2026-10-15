# License reference files

These files preserve upstream terms; their presence does not apply GPL/LGPL to
OpenScope's own MIT-licensed source. Each dependency remains under its own terms.

- `notices/pyserial-3.5-LICENSE.txt` is the unmodified, version-matched upstream
  license and copyright notice. The published pyserial 3.5 wheel omits this file,
  so the build collector uses this checked-in copy.
- `licenses/` supplies complete Apache-2.0, GPLv2/v3, and LGPLv2.1/v3 texts for
  bundled components whose notices refer to them. LGPLv3 distributions must also
  include the GPLv3 text; both are provided.
- `license-origins.json` records download origins and SHA-256 hashes of these
  reference files. Check these hashes when updating their contents.
- `source-manifest.json` identifies exact sources for the pinned Python packages
  and the observed Linux FFmpeg/Qt versions. It is an input to source preparation, not a declaration
  that all native sources or all platform builds have been cleared.

Keep dependency-specific copyright notices and exceptions in addition to these
generic license texts. See [the licensing guide](../docs/LICENSING.md) for source
distribution, library replacement, and final-build review instructions.
