import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image
import pytest

from png_webm_exporter import cli
from png_webm_exporter.encoding import BUILTIN_PRESETS, Settings


def run_cli(*arguments, stdin=subprocess.DEVNULL, timeout=60):
    result = subprocess.run([sys.executable, "-m", "png_webm_exporter.cli", *map(str, arguments)],
                            stdin=stdin, capture_output=True, text=True, timeout=timeout)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]


@pytest.fixture
def frames(tmp_path):
    folder = tmp_path / "frames"
    folder.mkdir()
    for number in range(4):
        Image.new("RGB", (64, 64), (number * 20, 50, 130)).save(folder / f"comp_{number:05d}.png")
    return folder


def test_presets_lists_builtins_and_defaults(capsys):
    assert cli.main(["presets"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["presets"] == BUILTIN_PRESETS
    assert event["defaults"]["av1"]["codec"] == "av1"
    assert event["defaults"]["av1"]["crf"] == 30
    assert Settings(**event["defaults"]["vp9"]) == Settings()


def test_unknown_settings_rejected():
    with pytest.raises(ValueError, match="Unknown settings: bogus"):
        cli.parse_settings('{"bogus": 1}')


def test_settings_from_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"codec": "av1", "crf": 20}), encoding="utf-8")
    assert cli.parse_settings(f"@{path}") == Settings(codec="av1", crf=20)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_encode_emits_progress_and_result(frames, tmp_path):
    encoders = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True).stdout
    webp = b"libwebp" in encoders
    output = tmp_path / "out.webm"
    code, events = run_cli("encode", "--frames", frames, "--output", output,
                           "--settings", json.dumps({"fps": "30000/1001", "export_frames": webp}))
    assert code == 0, events
    assert any(event["event"] == "progress" for event in events)
    done = events[-1]
    assert done["event"] == "done" and done["frames"] == 4 and done["size"] == output.stat().st_size
    if webp:
        assert [Path(path).name for path in done["frame_files"]] == ["out_first_frame.webp", "out_last_frame.webp"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_existing_destination_needs_overwrite(frames, tmp_path):
    output = tmp_path / "out.webm"
    output.write_bytes(b"keep")
    code, events = run_cli("encode", "--frames", frames, "--output", output)
    assert code == cli.EXIT_FAILED
    assert "--overwrite" in events[-1]["message"]
    assert output.read_bytes() == b"keep"
    code, events = run_cli("encode", "--frames", frames, "--output", output, "--overwrite")
    assert code == 0 and output.read_bytes() != b"keep"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_target_mb_runs_size_search(frames, tmp_path):
    code, events = run_cli("encode", "--frames", frames, "--output", tmp_path / "out.webm", "--target-mb", "0.02")
    assert code == 0, events
    assert events[-1]["size"] <= 20_000
    assert any(event.get("phase", "").startswith("Trial") for event in events)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_closed_stdin_cancels(frames, tmp_path):
    output = tmp_path / "out.webm"
    code, events = run_cli("encode", "--frames", frames, "--output", output, "--control-stdin")
    assert code == cli.EXIT_CANCELED
    assert events[-1]["event"] == "canceled"
    assert not output.exists()
    assert not list(tmp_path.glob(".png-webm-*"))


def test_bad_input_reports_error(tmp_path):
    code, events = run_cli("encode", "--frames", tmp_path / "missing", "--output", tmp_path / "out.webm")
    assert code == cli.EXIT_FAILED
    assert events == [{"event": "error", "message": "The frames folder does not exist."}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_sigterm_cancels_and_cleans_up(tmp_path):
    folder = tmp_path / "frames"
    folder.mkdir()
    for number in range(120):
        Image.effect_noise((640, 480), 80).convert("RGB").save(folder / f"f_{number:04d}.png")
    process = subprocess.Popen([sys.executable, "-m", "png_webm_exporter.cli", "encode", "--frames", str(folder),
                                "--output", str(tmp_path / "out.webm"), "--settings", '{"crf": 0}'],
                               stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if json.loads(line).get("phase", "").startswith("Encoding"):
            process.terminate()
            break
    remaining = [json.loads(line) for line in process.stdout]
    assert process.wait(timeout=30) == cli.EXIT_CANCELED
    assert remaining[-1]["event"] == "canceled"
    assert not list(tmp_path.glob(".png-webm-*"))
