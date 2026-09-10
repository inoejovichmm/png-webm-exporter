from pathlib import Path
import json
import shutil
import subprocess
import sys
import threading

from PIL import Image
import pytest

from png_webm_exporter.encoding import Canceled, Settings, build_args, inspect_frames, verify_probe, ExportWorker, file_signature
from png_webm_exporter.sequence import detect_sequence


@pytest.fixture
def frames(tmp_path):
    files = []
    for number in range(1001, 1007):
        path = tmp_path / f"shot v2_[{number:05d}].png"
        Image.new("RGB", (64, 64), (number % 255, 50, 130)).save(path)
        files.append(path)
    return files


def test_args(frames, tmp_path):
    args = build_args(detect_sequence(frames[:3]), Settings(fps="24000/1001", target_bytes=1000, bitrate_kbps=900), tmp_path / "out.webm", 12)
    assert args[args.index("-frames:v") + 1] == "3"
    assert args[args.index("-b:v") + 1] == "0"
    assert args[args.index("-vf") + 1].startswith("scale=out_color_matrix=bt709:out_range=full,format=yuv420p")
    assert "24000/1001" in args


def test_transparency_rejected(tmp_path):
    path = tmp_path / "frame.png"
    Image.new("RGBA", (32, 32), (20, 30, 40, 128)).save(path)
    with pytest.raises(ValueError, match="transparency"):
        inspect_frames(detect_sequence([path]), threading.Event(), lambda *_: None)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_encode_and_probe(frames, tmp_path):
    sequence = detect_sequence(frames[:3])
    settings = Settings(fps="24000/1001")
    dimensions = inspect_frames(sequence, threading.Event(), lambda *_: None)
    output = tmp_path / "result.webm"
    encoded = subprocess.run([shutil.which("ffmpeg"), *build_args(sequence, settings, output, 12)], capture_output=True, text=True)
    assert encoded.returncode == 0, encoded.stderr
    probe = subprocess.check_output([shutil.which("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(output)])
    verify_probe(json.loads(probe), sequence, settings, dimensions)
    assert output.stat().st_size > 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_worker_target_and_failed_target(frames, tmp_path, qtbot):
    output = tmp_path / "output.webm"
    worker = ExportWorker(frames, Settings(target_bytes=20000), output, None)
    with qtbot.waitSignal(worker.succeeded, timeout=60000) as outcome:
        worker.start()
    worker.wait()
    assert outcome.args[0]["size"] <= 20000
    assert outcome.args[0]["crf"] == 0
    original = output.read_bytes()
    worker = ExportWorker(frames, Settings(target_bytes=1), output, file_signature(output))
    with qtbot.waitSignal(worker.failed, timeout=60000):
        worker.start()
    worker.wait()
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".png-webm-*"))


def test_canceled_worker_preserves_output(frames, tmp_path, qtbot):
    output = tmp_path / "existing.webm"
    output.write_bytes(b"original")
    worker = ExportWorker(frames, Settings(), output, file_signature(output))
    worker.cancel()
    with qtbot.waitSignal(worker.canceled, timeout=5000):
        worker.start()
    worker.wait()
    assert output.read_bytes() == b"original"


def test_cancel_running_subprocess(frames, tmp_path):
    worker = ExportWorker(frames, Settings(), tmp_path / "output.webm", None)
    with pytest.raises(Canceled):
        worker.execute(Path(sys.executable), ["-c", "import threading; print('ready', flush=True); threading.Event().wait()"],
                       lambda line: worker.cancel())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
@pytest.mark.parametrize("approved", [False, True])
def test_changed_destination_requires_confirmation(frames, tmp_path, qtbot, approved):
    output = tmp_path / "output.webm"
    output.write_bytes(b"changed during export")
    worker = ExportWorker(frames, Settings(), output, None)
    confirmations = []

    def confirm(message, result):
        confirmations.append(message)
        worker.approve(approved)

    worker.ready.connect(confirm)
    with qtbot.waitSignal(worker.finished, timeout=10_000):
        worker.start()
    assert confirmations
    assert (output.read_bytes() != b"changed during export") == approved
    assert not list(tmp_path.glob(".png-webm-*"))


@pytest.mark.parametrize("fps", ["0", "-1", "nan", "1/0", "241"])
def test_invalid_fps(fps):
    with pytest.raises(ValueError):
        Settings(fps=fps).validate()