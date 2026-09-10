import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", action="store_true")
    arguments = parser.parse_args()
    environment = dict(os.environ)
    if not arguments.frozen:
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
        executable = ROOT / "dist" / "PNG to WebM" / "PNG to WebM.exe"
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment["PATH"] = str(Path(system_root) / "System32")
    else:
        raise SystemExit("Frozen checks support macOS and Windows.")
    environment["PNG_WEBM_FFMPEG"] = "deliberately-unavailable"
    environment["PNG_WEBM_FFPROBE"] = "deliberately-unavailable"
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