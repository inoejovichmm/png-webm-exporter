# PNG to WebM

Standalone Qt desktop exporter that turns consecutive PNG frames into VP9 WebM.
Supported platforms: macOS (Apple Silicon) and Windows (x64).

## Download

- **[Download for Mac](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-macos-arm64.zip)** — Apple Silicon (`.zip` containing the app bundle)
- **[Download for Windows](https://github.com/inoejovichmm/png-webm-exporter/releases/latest/download/PNG-to-WebM-windows-x64.exe)** — x64 (single `.exe`)

These links always point to the newest published release. Each release also
ships a matching `.sha256` file on the [latest release page](https://github.com/inoejovichmm/png-webm-exporter/releases/latest)
so you can verify your download.

## What it does

Select the exact consecutive PNG frames to include, choose the FPS and quality
(manual CRF or a bounded target-size search), pick an output file, then export a
VP9 WebM. Builds are fully self-contained: FFmpeg and ffprobe are bundled, and
the app never downloads anything at runtime. See [README.txt](README.txt) for the
complete in-app documentation, encoding defaults, and input requirements.

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
binaries and package the desktop app locally, see [tools/build_ffmpeg.py](tools/build_ffmpeg.py)
and [tools/package.py](tools/package.py).

## License

Third-party components and their licenses are listed in [THIRD_PARTY.txt](THIRD_PARTY.txt)
and under [vendor/notices](vendor/notices).
