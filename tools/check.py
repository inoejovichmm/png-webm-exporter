import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "org.pngwebmexporter.ae"


def check_extension(environment):
    """Install the packaged extension into a scratch profile and encode with its bundled encoder."""
    from PIL import Image

    label = "macos-arm64" if sys.platform == "darwin" else "windows-x64"
    artifact = ROOT / "dist" / f"PNG-to-WebM-AE-{label}.zip"
    directory = Path(tempfile.mkdtemp(prefix="png webm extension "))
    with zipfile.ZipFile(artifact) as archive:
        archive.extractall(directory / "download")
    if sys.platform == "darwin":
        environment["HOME"] = str(directory / "home")
        installed = Path(environment["HOME"]) / "Library" / "Application Support" / "Adobe" / "CEP" / "extensions"
        subprocess.run(["bash", str(directory / "download" / "Install PNG to WebM.command")], env=environment, check=True)
        encoder = installed / EXTENSION_ID / "bin" / "png-webm-encoder" / "png-webm-encoder"
    else:
        environment["APPDATA"] = str(directory / "appdata")
        environment["PWE_NO_PAUSE"] = "1"
        installed = Path(environment["APPDATA"]) / "Adobe" / "CEP" / "extensions"
        subprocess.run(["cmd", "/c", str(directory / "download" / "Install PNG to WebM.bat")], env=environment, check=True)
        encoder = installed / EXTENSION_ID / "bin" / "png-webm-encoder" / "png-webm-encoder.exe"
    extension = installed / EXTENSION_ID
    for required in ("CSXS/manifest.xml", "META-INF/signatures.xml", "index.html", "panel.js", "host/host.jsx"):
        if not (extension / required).is_file():
            raise SystemExit(f"Installed extension is missing {required}")
    presets = json.loads(subprocess.run([str(encoder), "presets"], env=environment, check=True,
                                        capture_output=True, text=True, timeout=120).stdout)
    if not presets["presets"] or presets["defaults"]["av1"]["codec"] != "av1":
        raise SystemExit(f"Unexpected presets output: {presets}")
    frames = directory / "frames"
    frames.mkdir()
    for index in range(4):
        image = Image.new("RGB", (512, 320))
        image.putdata([(24 + column // 5, 40 + row // 3, 90 + (column + index * 10) // 5)
                       for row in range(320) for column in range(512)])
        image.save(frames / f"frame_{index:05d}.png")
    output = directory / "smoke.webm"
    settings = json.dumps({"fps": "25", "export_frames": True})
    result = subprocess.run([str(encoder), "encode", "--frames", str(frames), "--output", str(output),
                             "--target-mb", "0.05", "--settings", settings],
                            env=environment, capture_output=True, text=True, timeout=300)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    done = events[-1] if events else {}
    if result.returncode or done.get("event") != "done" or done["size"] > 50_000 or len(done["frame_files"]) != 2:
        raise SystemExit(f"Extension encoder smoke check failed ({result.returncode}): {events[-3:]}\n{result.stderr}")
    print(json.dumps(done, indent=2))
    print(f"Installed extension and sample export: {directory}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", action="store_true", help="Smoke test the packaged desktop app.")
    parser.add_argument("--extension", action="store_true", help="Install and smoke test the packaged AE extension.")
    arguments = parser.parse_args()
    environment = dict(os.environ)
    if not arguments.frozen and not arguments.extension:
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("ffmpeg", "ffprobe"):
            binary = ROOT / "vendor" / "bin" / f"{name}{suffix}"
            if not binary.is_file():
                raise SystemExit("Build bundled FFmpeg before running these checks.")
            environment[f"PNG_WEBM_{name.upper()}"] = str(binary)
        environment["PATH"] = str(ROOT / "vendor" / "bin") + os.pathsep + environment.get("PATH", "")
        environment["QT_QPA_PLATFORM"] = "offscreen"
        return subprocess.call([sys.executable, "-m", "pytest", "-x", "-q"], cwd=ROOT, env=environment)
    if sys.platform == "darwin":
        executable = ROOT / "dist" / "PNG to WebM.app" / "Contents" / "MacOS" / "PNG to WebM"
        environment["PATH"] = "/usr/bin:/bin"
    elif os.name == "nt":
        executable = ROOT / "dist" / "PNG-to-WebM-windows-x64.exe"
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment["PATH"] = os.pathsep.join(str(Path(system_root) / name) for name in
                                              ("System32", r"System32\WindowsPowerShell\v1.0"))
    else:
        raise SystemExit("Frozen checks support macOS and Windows.")
    environment["PNG_WEBM_FFMPEG"] = "deliberately-unavailable"
    environment["PNG_WEBM_FFPROBE"] = "deliberately-unavailable"
    if arguments.extension:
        return check_extension(environment)
    directory = Path(tempfile.mkdtemp(prefix="png webm % frozen "))
    subprocess.run([str(executable), "--smoke-test", str(directory)], cwd=directory,
                   env=environment, check=True, timeout=120)
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if not report["success"] or not report["frozen"] or report["bytes"] > 50_000:
        raise SystemExit(f"Frozen smoke check failed: {report}")
    print(json.dumps(report, indent=2))
    print(f"Screenshots and sample export: {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())