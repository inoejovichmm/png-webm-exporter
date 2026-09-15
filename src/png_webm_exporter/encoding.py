from dataclasses import dataclass
from fractions import Fraction
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time

from PIL import Image
from PySide6.QtCore import QThread, Signal

from .resources import binary_path
from .sequence import Sequence, detect_sequence
from .size_search import SizeSearch


@dataclass(frozen=True)
class Settings:
    fps: str = "25"
    crf: int = 12
    target_bytes: int | None = None
    bitrate_kbps: int = 0
    full_range: bool = True
    auto_alt_ref: bool = False
    arnr_maxframes: int = 0
    aq_mode: int = 0
    row_mt: bool = True
    tile_columns: int = 2
    threads: int = 8
    gop: int = 0
    export_frames: bool = False
    frame_quality: int = 90

    def validate(self) -> None:
        try:
            fps = Fraction(self.fps)
        except (ValueError, ZeroDivisionError) as error:
            raise ValueError("Frame rate must be a number or fraction, such as 25 or 24000/1001.") from error
        if not 0 < fps <= 240:
            raise ValueError("Frame rate must be greater than 0 and at most 240.")
        for name, value, minimum, maximum in (
            ("CRF", self.crf, 0, 63), ("Bitrate", self.bitrate_kbps, 0, 1_000_000),
            ("Temporal filter frames", self.arnr_maxframes, 0, 15),
            ("Adaptive quantization", self.aq_mode, 0, 4),
            ("Tile exponent", self.tile_columns, 0, 6), ("Threads", self.threads, 0, 256),
            ("Keyframe distance", self.gop, 0, 1_000_000),
            ("Frame WebP quality", self.frame_quality, 0, 100),
        ):
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}.")
        if self.target_bytes is not None and self.target_bytes < 1:
            raise ValueError("Target size must be positive.")


def build_args(sequence: Sequence, settings: Settings, output: Path, crf: int) -> list[str]:
    settings.validate()
    if not 0 <= crf <= 63:
        raise ValueError("CRF must be between 0 and 63.")
    fps = str(Fraction(settings.fps))
    args = ["-hide_banner", "-nostdin", "-nostats", "-y", "-xerror",
            "-progress", "pipe:1", "-f", "image2", "-framerate", fps]
    if sequence.count == 1:
        args += ["-pattern_type", "none"]
    else:
        args += ["-start_number", str(sequence.start), "-start_number_range", "1"]
    args += ["-i", sequence.pattern, "-map", "0:v:0", "-frames:v", str(sequence.count),
             "-vf", f"scale=out_color_matrix=bt709:out_range={'full' if settings.full_range else 'limited'},format=yuv420p,"
             f"setparams=range={'full' if settings.full_range else 'limited'}:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
             "-c:v", "libvpx-vp9", "-profile:v", "0", "-crf", str(crf),
             "-b:v", "0" if settings.target_bytes is not None else f"{settings.bitrate_kbps}k",
             "-color_range", "pc" if settings.full_range else "tv",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-auto-alt-ref", str(int(settings.auto_alt_ref)),
             "-arnr-maxframes", str(settings.arnr_maxframes), "-aq-mode", str(settings.aq_mode),
             "-row-mt", str(int(settings.row_mt)), "-tile-columns", str(settings.tile_columns),
             "-threads", str(settings.threads)]
    if settings.gop:
        args += ["-g", str(settings.gop)]
    return args + ["-an", "-f", "webm", str(output)]


def build_frame_args(source: Path, output: Path, quality: int, frame_index: int) -> list[str]:
    if not 0 <= quality <= 100:
        raise ValueError("Frame WebP quality must be between 0 and 100.")
    if frame_index < 0:
        raise ValueError("Frame index must not be negative.")
    args = ["-hide_banner", "-nostdin", "-nostats", "-y", "-xerror",
            "-i", str(source), "-map", "0:v:0",
            "-vf", f"select=eq(n\\,{frame_index})", "-frames:v", "1",
            "-fps_mode", "passthrough", "-c:v", "libwebp"]
    if quality >= 100:
        args += ["-lossless", "1"]
    else:
        args += ["-lossless", "0", "-q:v", str(quality)]
    return args + ["-an", "-f", "webp", str(output)]


class Canceled(Exception):
    pass


class TrialTooLarge(Exception):
    pass


def inspect_frames(sequence: Sequence, canceled: threading.Event, progress) -> tuple[int, int, int]:
    expected = None
    for index, path in enumerate(sequence.files):
        if canceled.is_set():
            raise Canceled
        with path.open("rb") as stream:
            header = stream.read(29)
        if len(header) != 29 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise ValueError(f"Invalid PNG: {path.name}")
        width, height, depth, color, compression, filtering, interlace = struct.unpack(
            ">IIBBBBB", header[16:29])
        if depth not in (8, 16) or color not in (2, 6):
            raise ValueError(f"Export RGB PNGs at 8 or 16 bits per channel: {path.name}")
        if width % 2 or height % 2:
            raise ValueError(f"Use even frame dimensions for this delivery preset: {width} x {height}.")
        properties = (width, height, depth, color)
        if expected is not None and properties != expected:
            raise ValueError(f"Frame dimensions, bit depth or RGB format differ: {path.name}")
        expected = properties
        with Image.open(path) as image:
            if getattr(image, "is_animated", False):
                raise ValueError(f"Animated PNG is not supported: {path.name}")
            image.verify()
        with Image.open(path) as image:
            image.load()
            if color == 6 and (depth == 16 or image.getchannel("A").getextrema() != (255, 255)):
                raise ValueError(f"Flatten transparency in AE and export RGB PNGs: {path.name}. 16-bit RGBA is not accepted.")
            if "transparency" in image.info:
                raise ValueError(f"Remove PNG transparency before export: {path.name}")
        progress(index + 1, sequence.count)
    if expected is None:
        raise ValueError("No frames selected.")
    return expected[:3]


def file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def verify_probe(data: dict, sequence: Sequence, settings: Settings, dimensions: tuple[int, int, int]) -> None:
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "video":
        raise ValueError("Output must contain exactly one video stream and no audio.")
    video = streams[0]
    expected = {"codec_name": "vp9", "profile": "Profile 0", "pix_fmt": "yuv420p",
                "color_range": "pc" if settings.full_range else "tv", "color_space": "bt709",
                "color_transfer": "bt709", "color_primaries": "bt709",
                "width": dimensions[0], "height": dimensions[1]}
    for key, value in expected.items():
        if video.get(key) != value:
            raise ValueError(f"Output verification failed: {key} is {video.get(key)!r}, expected {value!r}.")
    if int(video.get("nb_read_frames", -1)) != sequence.count:
        raise ValueError("Output frame count does not match the selection.")
    rate = Fraction(settings.fps)
    duration = float(data.get("format", {}).get("duration", -1))
    if abs(duration - float(sequence.count / rate)) > max(0.005, float(1 / rate) / 10):
        raise ValueError("Output duration does not match the selected frame rate.")
    if abs(float(Fraction(video.get("avg_frame_rate", "0/1"))) - float(rate)) > 0.001:
        raise ValueError("Output frame rate does not match the settings.")


class ExportWorker(QThread):
    update = Signal(dict)
    succeeded = Signal(dict)
    failed = Signal(str)
    canceled = Signal()
    ready = Signal(str, dict)

    def __init__(self, paths: list[Path], settings: Settings, destination: Path,
                 signature, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.settings = settings
        self.destination = destination.absolute()
        self.signature = signature
        self.stop = threading.Event()
        self.decision = threading.Event()
        self.approved = False
        self.log: list[str] = []
        self.owned: set[Path] = set()

    def cancel(self) -> None:
        self.stop.set()
        self.decision.set()

    def approve(self, approved: bool) -> None:
        self.approved = approved
        if approved:
            self.signature = file_signature(self.destination)
        self.decision.set()

    def execute(self, program: Path, arguments: list[str], on_line=None) -> str:
        if self.stop.is_set():
            raise Canceled
        messages: queue.Queue = queue.Queue(maxsize=1024)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with subprocess.Popen([str(program), *arguments], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=flags) as process:
            def read_stream(stream, kind):
                for raw in iter(stream.readline, b""):
                    messages.put((kind, raw.decode("utf-8", errors="replace").rstrip()))
                stream.close()
                messages.put((kind, None))

            readers = [threading.Thread(target=read_stream, args=(process.stdout, "out")),
                       threading.Thread(target=read_stream, args=(process.stderr, "err"))]
            for reader in readers:
                reader.start()
            output = []
            closed = 0
            terminated_at = None
            trial_too_large = False
            try:
                while closed < 2:
                    if self.stop.is_set() and process.poll() is None:
                        if terminated_at is None:
                            process.terminate()
                            terminated_at = time.monotonic()
                        elif time.monotonic() - terminated_at > 2:
                            process.kill()
                    try:
                        kind, line = messages.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        closed += 1
                    elif kind == "err":
                        self.log.append(line)
                        self.log = self.log[-500:]
                    elif on_line:
                        if on_line(line):
                            trial_too_large = True
                            if process.poll() is None and terminated_at is None:
                                process.terminate()
                                terminated_at = time.monotonic()
                    else:
                        output.append(line)
                code = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                while any(reader.is_alive() for reader in readers):
                    try:
                        messages.get(timeout=0.1)
                    except queue.Empty:
                        pass
                for reader in readers:
                    reader.join()
            if self.stop.is_set():
                raise Canceled
            if trial_too_large:
                raise TrialTooLarge
            if code:
                raise RuntimeError(f"{program.name} exited with code {code}.\n" + "\n".join(self.log[-15:]))
            return "\n".join(output)

    def run(self) -> None:
        try:
            self.perform()
        except Canceled:
            self.canceled.emit()
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            for path in self.owned:
                path.unlink(missing_ok=True)

    def perform(self) -> None:
        self.settings.validate()
        sequence = detect_sequence(self.paths)
        self.update.emit({"phase": "Validating PNG frames", "percent": 0})
        dimensions = inspect_frames(sequence, self.stop, lambda done, total:
                                    self.update.emit({"phase": "Validating PNG frames", "percent": done * 100 // total}))
        if self.destination.suffix.lower() != ".webm":
            raise ValueError("Choose an output filename ending in .webm.")
        if not self.destination.parent.is_dir():
            raise ValueError("The output folder does not exist.")
        ffmpeg, ffprobe = binary_path("ffmpeg"), binary_path("ffprobe")
        encoders = self.execute(ffmpeg, ["-hide_banner", "-encoders"])
        if "libvpx-vp9" not in encoders:
            raise ValueError("This FFmpeg build does not include libvpx-vp9.")
        if self.settings.export_frames and "libwebp" not in encoders:
            raise ValueError("This FFmpeg build does not include libwebp; frame export is unavailable.")
        search = SizeSearch(self.settings.target_bytes, self.settings.crf) if self.settings.target_bytes else None
        best_path = None
        chosen_crf = self.settings.crf
        trial = 0
        started = time.monotonic()
        while True:
            current_crf = search.next_crf() if search else (self.settings.crf if trial == 0 else None)
            if current_crf is None:
                break
            if self.stop.is_set():
                raise Canceled
            trial += 1
            descriptor, name = tempfile.mkstemp(prefix=".png-webm-", suffix=".webm", dir=self.destination.parent)
            os.close(descriptor)
            candidate = Path(name)
            self.owned.add(candidate)
            label = f"Trial {trial} of up to 9 - CRF {current_crf}" if search else f"Encoding - CRF {current_crf}"
            self.update.emit({"phase": label, "percent": 0})

            def progress(line):
                key, separator, value = line.partition("=")
                if separator and key == "frame" and value.strip().isdigit():
                    frame = int(value)
                    size = candidate.stat().st_size
                    self.update.emit({"phase": label, "percent": min(99, frame * 100 // sequence.count),
                                      "elapsed": time.monotonic() - started, "size": size,
                                      "best_size": best_path.stat().st_size if best_path else None})
                    return search is not None and current_crf < 63 and size > search.budget

            args = build_args(sequence, self.settings, candidate, current_crf)
            self.log.append(json.dumps([str(ffmpeg), *args]))
            try:
                self.execute(ffmpeg, args, progress)
            except TrialTooLarge:
                size = candidate.stat().st_size
                search.record(current_crf, size)
                candidate.unlink(missing_ok=True)
                self.owned.discard(candidate)
                continue
            else:
                size = candidate.stat().st_size
                if size <= 0:
                    raise ValueError("FFmpeg produced an empty file.")
                if search:
                    search.record(current_crf, size)
            selected_crf = current_crf if search is None else search.preferred
            fallback = search is not None and search.best is None and current_crf == 63
            if selected_crf == current_crf or fallback:
                if best_path:
                    best_path.unlink(missing_ok=True)
                    self.owned.discard(best_path)
                best_path, chosen_crf = candidate, current_crf
            else:
                candidate.unlink(missing_ok=True)
                self.owned.discard(candidate)
        if best_path is None:
            raise ValueError("FFmpeg did not produce a usable output.")
        self.update.emit({"phase": "Verifying complete output", "percent": -1})
        data = self.execute(ffprobe, ["-v", "error", "-count_frames", "-show_streams",
                                      "-show_format", "-of", "json", str(best_path)])
        verify_probe(json.loads(data), sequence, self.settings, dimensions)
        result = {"path": str(self.destination), "size": best_path.stat().st_size, "crf": chosen_crf,
                  "trials": trial, "frames": sequence.count, "duration": float(sequence.count / Fraction(self.settings.fps))}
        result["target_met"] = not search or result["size"] <= search.budget
        if file_signature(self.destination) != self.signature:
            self.update.emit({"phase": "Awaiting overwrite confirmation", "percent": -1})
            self.ready.emit("The destination changed during export. Replace it with the verified result?", result)
            self.decision.wait()
            if not self.approved:
                raise Canceled
        if self.stop.is_set():
            raise Canceled
        if file_signature(self.destination) != self.signature:
            raise ValueError("Destination changed again. Choose another filename and retry.")
        os.replace(best_path, self.destination)
        self.owned.discard(best_path)
        if self.settings.export_frames:
            result["frame_files"] = self.export_boundary_frames(ffmpeg, sequence.count)
        self.succeeded.emit(result)

    def export_boundary_frames(self, ffmpeg: Path, frame_count: int) -> list[str]:
        quality = self.settings.frame_quality
        mode = "lossless" if quality >= 100 else f"q {quality}"
        exports = {"first": 0, "last": max(frame_count - 1, 0)}
        written: list[str] = []
        for position, index in exports.items():
            if self.stop.is_set():
                raise Canceled
            self.update.emit({"phase": f"Exporting {position} frame as WebP ({mode})", "percent": -1})
            target = self.destination.with_name(f"{self.destination.stem}_{position}.webp")
            args = build_frame_args(self.destination, target, quality, index)
            self.log.append(json.dumps([str(ffmpeg), *args]))
            self.execute(ffmpeg, args)
            if not target.is_file() or target.stat().st_size <= 0:
                raise ValueError(f"FFmpeg did not produce the {position} frame WebP.")
            written.append(str(target))
        return written
