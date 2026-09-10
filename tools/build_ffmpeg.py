import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "ffmpeg-9.0.1.tar.xz": {
        "url": "https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz",
        "sha256": "cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635",
    },
    "libvpx-1.15.2.tar.gz": {
        "url": "https://codeload.github.com/webmproject/libvpx/tar.gz/refs/tags/v1.15.2",
        "sha256": "26fcd3db88045dee380e581862a6ef106f49b74b6396ee95c2993a260b4636aa",
    },
}


def run(arguments, cwd, environment):
    print(" ".join(str(argument) for argument in arguments), flush=True)
    subprocess.run(arguments, cwd=cwd, env=environment, check=True)


def main():
    system = platform.system()
    windows = system.startswith(("MINGW", "MSYS")) or system == "Windows"
    if system != "Darwin" and not windows:
        raise SystemExit("Build on macOS arm64 or Windows x64 in an MSYS2 UCRT64 shell.")
    if system == "Darwin" and platform.machine() != "arm64":
        raise SystemExit("The macOS target is Apple Silicon (arm64).")
    for tool in ("sh", "make", "pkg-config", "cc"):
        if shutil.which(tool) is None:
            raise SystemExit(f"Missing build tool: {tool}")
    work = ROOT / "build" / "native"
    downloads = ROOT / "build" / "downloads"
    prefix = work / "prefix"
    vendor = ROOT / "vendor"
    notices = vendor / "notices"
    for directory in (work, downloads, prefix, vendor / "bin", notices / "sources"):
        directory.mkdir(parents=True, exist_ok=True)
    for filename, source in SOURCES.items():
        archive = downloads / filename
        if not archive.exists():
            urllib.request.urlretrieve(source["url"], archive)
        if hashlib.file_digest(archive.open("rb"), "sha256").hexdigest() != source["sha256"]:
            raise SystemExit(f"Source checksum mismatch: {archive}")
        directory = work / filename.removesuffix(".tar.xz").removesuffix(".tar.gz")
        if not directory.exists():
            with tarfile.open(archive) as stream:
                stream.extractall(work, filter="data")
        shutil.copy2(archive, notices / "sources" / filename)
    environment = dict(os.environ)
    environment["PKG_CONFIG_PATH"] = str(prefix / "lib" / "pkgconfig")
    if system == "Darwin":
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    jobs = str(min(os.cpu_count() or 2, 8))
    vpx = work / "libvpx-1.15.2"
    vpx_args = ["sh", "./configure", f"--prefix={prefix}",
                f"--target={'x86_64-win64-gcc' if windows else 'arm64-darwin22-gcc'}",
                "--disable-examples", "--disable-tools", "--disable-docs", "--disable-unit-tests",
                "--disable-vp8", "--enable-vp9-highbitdepth", "--enable-pic", "--disable-shared", "--enable-static"]
    run(vpx_args, vpx, environment)
    run(["make", "-j", jobs], vpx, environment)
    run(["make", "install"], vpx, environment)
    ffmpeg = work / "ffmpeg-9.0.1"
    ffmpeg_args = ["sh", "./configure", f"--prefix={prefix}", "--disable-everything",
                   "--disable-autodetect", "--disable-network", "--disable-programs",
                   "--enable-ffmpeg", "--enable-ffprobe", "--disable-doc", "--disable-debug",
                   "--disable-shared", "--enable-static",
                   "--enable-w32threads" if windows else "--enable-pthreads", "--enable-libvpx",
                   "--enable-zlib", "--enable-encoder=libvpx_vp9", "--enable-decoder=png,vp9",
                   "--enable-parser=png,vp9", "--enable-demuxer=image2,matroska",
                   "--enable-muxer=webm", "--enable-bsf=vp9_superframe", "--enable-protocol=file,pipe",
                   "--enable-filter=scale,format,setparams", "--enable-swscale", "--pkg-config-flags=--static"]
    if windows:
        ffmpeg_args += ["--extra-ldflags=-static", "--extra-libs=-lstdc++"]
    run(ffmpeg_args, ffmpeg, environment)
    run(["make", "-j", jobs], ffmpeg, environment)
    suffix = ".exe" if windows else ""
    binaries = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = vendor / "bin" / f"{name}{suffix}"
        shutil.copy2(ffmpeg / f"{name}{suffix}", binary)
        if system == "Darwin":
            subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True)
        version = subprocess.check_output([str(binary), "-version"], text=True)
        binaries[name] = {"sha256": hashlib.file_digest(binary.open("rb"), "sha256").hexdigest(),
                          "version": version}
    for source, name in ((ffmpeg / "COPYING.LGPLv2.1", "FFmpeg-LGPL-2.1.txt"),
                         (ffmpeg / "LICENSE.md", "FFmpeg-LICENSE.md"),
                         (vpx / "LICENSE", "libvpx-LICENSE.txt"),
                         (vpx / "PATENTS", "libvpx-PATENTS.txt")):
        shutil.copy2(source, notices / name)
    shutil.copy2(__file__, notices / "build_ffmpeg.py")
    metadata = {"platform": platform.platform(), "sources": SOURCES,
                "configure": {"libvpx": vpx_args, "ffmpeg": ffmpeg_args}, "binaries": binaries}
    (notices / "build-manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Built binaries and notices in {vendor}")


if __name__ == "__main__":
    main()