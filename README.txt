PNG to WebM
===========

Standalone Qt desktop exporter for consecutive PNG frames to VP9 WebM.
Target platforms: macOS Apple Silicon and Windows x64.

Select the exact consecutive PNG frames to include. Numeric order is detected,
including nonzero starts, padding, brackets, and unpadded numbers. Gaps and mixed
sequences are rejected. A single PNG exports as one frame. Choose the FPS,
confirm flattened SDR Rec.709 sources, choose an output file, then Export WebM.
Hover a setting or its label for its explanation. The help button beside CRF
opens an explanation for the selected quality mode. The README button opens
this document in a scrollable, selectable, read-only window inside the app.

The default is CRF 12, 25 FPS, VP9 Profile 0, 8-bit YUV 4:2:0, full-range
Rec.709, no audio. Existing noise/dither is encoded; no additional noise is
generated. ICC color profiles are not converted. Source preview is not a
color-managed monitor. PNGs must have matching, even dimensions and RGB format.
8-bit RGB / opaque RGBA and 16-bit RGB are accepted. Flattened RGB is recommended.
16-bit RGBA is rejected because this validator cannot reliably inspect its alpha.

CRF (Constant Rate Factor) is the encoder's quality setting, from 0 to 63.
Lower values usually retain more detail and produce larger files. Higher values
usually produce smaller files but can lose detail, smooth grain/dither, or expose
banding. CRF is not a percentage or a linear size scale. CRF 0 does not guarantee
a lossless export with the app's 8-bit YUV 4:2:0 conversion.

Manual CRF runs one encode at your chosen value; there is no automatic size cap.
The target-size field is hidden in this mode. Start with CRF 12 and inspect the
output before deciding to trade more detail for a larger file, or vice versa.

In Target size mode, the field is labeled Initial CRF. It sets only the first
trial, not the final CRF or a quality limit. The search can go above or below it,
across 0-63. Leave it at 12 unless you want a different starting point. Changing
it changes the search order and can affect runtime and the bounded search result.
The chosen final CRF is reported after export.

Target size uses decimal MB (1,000,000 bytes). Up to nine complete trials vary
only CRF, with bitrate forced to zero. The lowest tested fitting CRF is kept;
nonmonotonic size behavior means it is not a guaranteed global quality optimum.
The cap includes container overhead, is not an exact fill, and can be impossible.
Per-trial progress is shown. Neither dimensions nor FPS are silently changed.
Temporary candidates are on the output volume; leave space for two encodes plus
the existing destination. Cancel/error leaves the destination unchanged. A
successful file is checked with ffprobe before atomic replacement.

Development (Python 3.11+; tested with 3.14.7)
--------------------------------------------
macOS:
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements-build.txt
  .venv/bin/python -m pip install -e .
  PNG_WEBM_FFMPEG="$PWD/vendor/bin/ffmpeg" \
  PNG_WEBM_FFPROBE="$PWD/vendor/bin/ffprobe" \
  .venv/bin/python -m png_webm_exporter

Development can also use ffmpeg and ffprobe on PATH. Frozen apps always use
their bundled tools and never download anything at runtime.

Native macOS package
--------------------
Requires Xcode command-line tools, make and pkg-config (brew install pkgconf).
  .venv/bin/python tools/build_ffmpeg.py
  .venv/bin/python tools/check.py
  .venv/bin/python tools/package.py
Open dist/PNG to WebM.app. The zip beside it preserves the application bundle.
The native encoder targets macOS 13; the final runtime's minimum is also subject
to the installed Python/Qt wheels. Older macOS versions need a separate test.

Windows x64 package (requires a native Windows build; not tested on macOS)
------------------------------------------------------------------------
Install Python x64 and MSYS2. In MSYS2 UCRT64, install:
  pacman -S --needed make diffutils perl python \
    mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-pkgconf \
    mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-zlib
In that shell, run: /usr/bin/python tools/build_ffmpeg.py
Then in native PowerShell:
  py -3.14 -m venv .venv
  .venv\Scripts\python -m pip install -r requirements-build.txt
  .venv\Scripts\python -m pip install -e .
  $env:PATH = "$PWD\vendor\bin;$env:PATH"
  $env:QT_QPA_PLATFORM = "offscreen"
  .venv\Scripts\python -m pytest
  .venv\Scripts\python tools/package.py
Distribute the whole dist/PNG to WebM folder or its zip, not the EXE alone.
Validate the package on a clean Windows machine without MSYS2 or Python.
The Windows desktop build GitHub Actions workflow performs the native build,
tests and frozen smoke check, then uploads an unsigned zip. It must be run in
your repository; it has not been executed as part of the local macOS build.

Frozen smoke check
------------------
Run the application executable with --smoke-test followed by a fresh directory.
It creates sample frames, performs a size-targeted export, and writes report.json
and screenshots. On macOS:
  env PATH=/usr/bin:/bin "dist/PNG to WebM.app/Contents/MacOS/PNG to WebM" \
    --smoke-test /tmp/png-webm-frozen-check
  Or run .venv/bin/python tools/check.py --frozen (Windows: .venv\Scripts\python).
  That runner chooses a fresh directory containing spaces and a percent sign,
  restricts PATH, and verifies that the app ignores development tool overrides.

Build source archives are SHA256-pinned from their upstream HTTPS URLs. The
hashes pin fetched bytes; they are not a claim of PGP verification. Build flags
and binary hashes are recorded. The toolchain and all transitive Python wheels
are not fully hermetically locked. See THIRD_PARTY.txt for distribution gates.
The current macOS app is ad-hoc signed for local testing, not notarized. Do not
present it as a public signed release. A Windows package requires native tests.