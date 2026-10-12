import argparse
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "org.pngwebmexporter.ae"
ZXPSIGN_COMMIT = "fcee8de5537d2dd2ed3d91d5a495b7041155b280"
ZXPSIGN = {
    "darwin": ("ZXPSignCMD/4.1.3/macOS/ZXPSignCmd",
               "bc773fae0b97416fc7a462e7dadcc00270428a9913480c9b78b5606ff1cfb095"),
    "win32": ("ZXPSignCMD/4.1.103/win64/ZXPSignCmd.exe",
              "dc2e711a46504830062ac1afede1f8ee08c1e21399b6c30d7d38cf907c35bcf5"),
}
TIMESTAMP_URL = "http://timestamp.digicert.com"


def platform_label() -> str:
    return "macos-arm64" if sys.platform == "darwin" else "windows-x64"


def write_checksum(artifact: Path) -> str:
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (artifact.parent / f"{artifact.name}.sha256").write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    return digest


def main():
    parser = argparse.ArgumentParser(description="Package the desktop app or the After Effects extension.")
    parser.add_argument("--target", choices=("app", "extension"), default="app")
    target = parser.parse_args().target
    os.chdir(ROOT)
    notices, binaries = prepare()
    artifact = package_app(notices, binaries) if target == "app" else package_extension(notices, binaries)
    digest = write_checksum(artifact)
    print(f"Package: {artifact}\nSHA256: {digest}\nBuilt on {platform.platform()}")


def prepare():
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
    return notices, binaries


def bundle_arguments(notices: Path, binaries: list[Path]) -> list[str]:
    arguments = ["--paths", str(ROOT / "src"), "--specpath", "build", "--noupx",
                 "--add-data", f"{notices}{os.pathsep}notices",
                 "--add-data", f"{ROOT / 'src'}{os.pathsep}application-source/src",
                 "--add-data", f"{ROOT / 'tools'}{os.pathsep}application-source/tools",
                 "--add-data", f"{ROOT / 'aftereffects'}{os.pathsep}application-source/aftereffects",
                 "--add-data", f"{ROOT / 'pyproject.toml'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'requirements-build.txt'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'THIRD_PARTY.txt'}{os.pathsep}application-source",
                 "--add-data", f"{ROOT / 'README.txt'}{os.pathsep}application-source"]
    for binary in binaries:
        arguments += ["--add-binary", f"{binary}{os.pathsep}bin"]
    if sys.platform == "darwin":
        arguments += ["--target-architecture", "arm64"]
    return arguments


def package_app(notices: Path, binaries: list[Path]) -> Path:
    package_mode = "--onedir" if sys.platform == "darwin" else "--onefile"
    arguments = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", package_mode,
                 "--name", "PNG to WebM", *bundle_arguments(notices, binaries)]
    if sys.platform == "darwin":
        arguments += ["--osx-bundle-identifier", "org.pngwebmexporter.desktop"]
    arguments += ["src/png_webm_exporter/__main__.py"]
    subprocess.run(arguments, check=True)
    if sys.platform == "darwin":
        app = ROOT / "dist" / "PNG to WebM.app"
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        artifact = ROOT / "dist" / "PNG-to-WebM-macos-arm64.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(artifact)], check=True)
    else:
        artifact = ROOT / "dist" / "PNG-to-WebM-windows-x64.exe"
        artifact.unlink(missing_ok=True)
        (ROOT / "dist" / "PNG to WebM.exe").rename(artifact)
    return artifact


def zxpsign() -> Path:
    if sys.platform not in ZXPSIGN:
        raise SystemExit("ZXP signing supports macOS and Windows.")
    relative, expected = ZXPSIGN[sys.platform]
    tool = ROOT / "build" / "tools" / Path(relative).name
    if not tool.is_file():
        tool.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/Adobe-CEP/CEP-Resources/{ZXPSIGN_COMMIT}/{relative}"
        urllib.request.urlretrieve(url, tool)
    with tool.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            tool.unlink()
            raise SystemExit(f"ZXPSignCmd checksum mismatch: {tool}")
    tool.chmod(0o755)
    return tool


def package_extension(notices: Path, binaries: list[Path]) -> Path:
    work = ROOT / "build" / "extension"
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console", "--onedir",
                    "--name", "png-webm-encoder", "--distpath", str(work / "encoder"),
                    "--workpath", str(work / "pyinstaller"),
                    "--exclude-module", "PySide6", "--exclude-module", "shiboken6",
                    *bundle_arguments(notices, binaries), "src/png_webm_exporter/cli.py"], check=True)
    stage = work / EXTENSION_ID
    shutil.copytree(ROOT / "aftereffects", stage, ignore=shutil.ignore_patterns(".DS_Store", "__MACOSX"))
    # Signature checks fail on symlinks inside ZXPs, so copy their targets instead.
    shutil.copytree(work / "encoder" / "png-webm-encoder", stage / "bin" / "png-webm-encoder", symlinks=False)
    tool = zxpsign()
    password = secrets.token_urlsafe(24)
    certificate = work / "self-signed.p12"
    subprocess.run([str(tool), "-selfSignedCert", "US", "NA", "PNG to WebM", "PNG to WebM internal",
                    password, str(certificate), "-validityDays", "3650"], check=True)
    zxp = work / "PNG-to-WebM-AE.zxp"
    signed = subprocess.run([str(tool), "-sign", str(stage), str(zxp), str(certificate), password,
                             "-tsa", TIMESTAMP_URL])
    if signed.returncode != 0:
        print("Timestamping failed; signing without a timestamp.", file=sys.stderr)
        zxp.unlink(missing_ok=True)
        subprocess.run([str(tool), "-sign", str(stage), str(zxp), str(certificate), password], check=True)
    subprocess.run([str(tool), "-verify", str(zxp)], check=True)
    certificate.unlink()
    artifact = ROOT / "dist" / f"PNG-to-WebM-AE-{platform_label()}.zip"
    artifact.parent.mkdir(exist_ok=True)
    suffix = ".command" if sys.platform == "darwin" else ".bat"
    with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(zxp, zxp.name)
        for script in sorted((ROOT / "tools" / "extension_installers").glob(f"*{suffix}")):
            entry = zipfile.ZipInfo.from_file(script, script.name)
            entry.external_attr = 0o100755 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, script.read_bytes())
    return artifact


if __name__ == "__main__":
    main()