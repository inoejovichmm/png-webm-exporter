import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    notices = ROOT / "vendor" / "notices"
    manifest_path = notices / "build-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("Run tools/build_ffmpeg.py first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    suffix = ".exe" if os.name == "nt" else ""
    binaries = [ROOT / "vendor" / "bin" / f"{name}{suffix}" for name in ("ffmpeg", "ffprobe")]
    for binary in binaries:
        with binary.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != manifest["binaries"][binary.stem]["sha256"]:
            raise SystemExit(f"Binary checksum mismatch: {binary}")
        if sys.platform == "darwin":
            linked = subprocess.check_output(["otool", "-L", str(binary)], text=True)
            for line in linked.splitlines()[1:]:
                if not line.strip().startswith(("/usr/lib/", "/System/Library/")):
                    raise SystemExit(f"Non-system dependency in {binary}: {line}")
    versions = {}
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "pillow", "pyinstaller"):
        package = distribution(name)
        versions[name] = package.version
        target = notices / name
        target.mkdir(exist_ok=True)
        (target / "METADATA.txt").write_text(package.read_text("METADATA") or "", encoding="utf-8")
        for entry in package.files or []:
            if "licenses" in entry.parts or entry.name.upper().startswith(("COPYING", "LICENSE")):
                source = Path(package.locate_file(entry))
                if source.is_file():
                    destination = target / str(entry).replace("/", "_")
                    shutil.copy2(source, destination)
    for name in ("LGPL-3.0-only", "GPL-3.0-only"):
        destination = notices / f"{name}.txt"
        if not destination.exists():
            urllib.request.urlretrieve(f"https://raw.githubusercontent.com/qt/qtbase/v6.11.2/LICENSES/{name}.txt", destination)
    (notices / "python-LICENSE.txt").write_text(__import__("pydoc").render_doc("license"), encoding="utf-8")
    import _sitebuiltins
    license_object = __import__("builtins").license
    if isinstance(license_object, _sitebuiltins._Printer):
        license_object._Printer__setup()
        (notices / "python-LICENSE.txt").write_text("\n".join(license_object._Printer__lines), encoding="utf-8")
    shutil.copy2(ROOT / "THIRD_PARTY.txt", notices)
    (notices / "runtime-versions.json").write_text(json.dumps({"python": sys.version, **versions}, indent=2), encoding="utf-8")
    arguments = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
                 "--name", "PNG to WebM", "--paths", str(ROOT / "src"), "--specpath", "build",
                 "--add-data", f"{notices}{os.pathsep}notices",
                 "--add-data", f"{ROOT / 'src'}{os.pathsep}application-source/src",
                 "--add-data", f"{ROOT / 'tools'}{os.pathsep}application-source/tools",
                 "--add-data", f"{ROOT / 'pyproject.toml'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'requirements-build.txt'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'THIRD_PARTY.txt'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'README.txt'}{os.pathsep}application-source"]
    for binary in binaries:
        arguments += ["--add-binary", f"{binary}{os.pathsep}bin"]
    if sys.platform == "darwin":
        arguments += ["--target-architecture", "arm64", "--osx-bundle-identifier", "org.pngwebmexporter.desktop"]
    arguments += ["src/png_webm_exporter/__main__.py"]
    subprocess.run(arguments, check=True)
    if sys.platform == "darwin":
        app = ROOT / "dist" / "PNG to WebM.app"
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        archive = ROOT / "dist" / "PNG-to-WebM-macos-arm64.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True)
    else:
        archive = Path(shutil.make_archive(str(ROOT / "dist" / "PNG-to-WebM-windows-x64"), "zip",
                                           ROOT / "dist", "PNG to WebM"))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Package: {archive}\nSHA256: {digest}\nBuilt on {platform.platform()}")


if __name__ == "__main__":
    main()