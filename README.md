# PNG to WebM

Turns consecutive PNG frames (or a ProRes 4444 movie) into a verified VP9 or AV1
WebM. Available as a standalone desktop app and as an Adobe After Effects panel.
Supported platforms: macOS (Apple Silicon) and Windows (x64).

## Download

### Desktop app

- **[Download for Mac](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-macos-arm64.zip)** — Apple Silicon (`.zip` containing the app bundle)
- **[Download for Windows](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-windows-x64.exe)** — x64 (single `.exe`)

### After Effects panel (AE 2020 or newer)

- **[Download AE panel for Mac](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-AE-macos-arm64.zip)** — Apple Silicon
- **[Download AE panel for Windows](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-AE-windows-x64.zip)** — x64

Unzip, run **Install PNG to WebM** (`.command` on Mac, `.bat` on Windows),
restart After Effects and open **Window > Extensions > PNG to WebM**. Select a
composition and press **Export WebM**: the panel renders the work area and
encodes it in the background. No admin rights, Adobe installer, debug mode or
other software are required. If macOS or Windows warns about the installer, see
the "After Effects panel" section of [README.txt](README.txt); copying the zip
from a shared drive instead of a browser download avoids those prompts.

These links always point to the newest published release. Each release also
ships a matching `.sha256` file on the [latest release page](https://github.com/inoejovichmm/png-webm-exporter/releases/latest)
so you can verify your download.

## What it does

Select the exact consecutive PNG frames to include, choose the FPS and quality
(manual CRF or a bounded target-size search), pick an output file, then export a
WebM. Builds are fully self-contained: FFmpeg and ffprobe are bundled in both
the desktop app and the After Effects panel, and nothing is downloaded at
runtime. See [README.txt](README.txt) for the complete in-app documentation,
encoding defaults, and input requirements.

The panel uses the same Python core as the desktop app through a headless
encoder (`png-webm-encoder`, see [src/png_webm_exporter/cli.py](src/png_webm_exporter/cli.py)),
which prints JSON-lines progress. The panel itself lives in [aftereffects/](aftereffects).

## Development

Requires Python 3.11+ (tested with 3.14).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python -m pip install -e .
PNG_WEBM_FFMPEG="$PWD/vendor/bin/ffmpeg" \
PNG_WEBM_FFPROBE="$PWD/vendor/bin/ffprobe" \
.venv/bin/python -m png_webm_exporter
```

Development can also use `ffmpeg`/`ffprobe` on `PATH`. To build the bundled
binaries and package locally, see [tools/build_ffmpeg.py](tools/build_ffmpeg.py)
and [tools/package.py](tools/package.py) (`--target extension` builds the After
Effects panel; `tools/check.py --extension` installs it into a scratch profile
and runs a real export).

## License

Third-party components and their licenses are listed in [THIRD_PARTY.txt](THIRD_PARTY.txt)
and under [vendor/notices](vendor/notices).
