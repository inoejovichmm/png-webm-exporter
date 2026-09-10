import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("system,expected,excluded", [
    ("MSYS_NT-10.0-26100", "--enable-w32threads", "--enable-pthreads"),
    ("Windows", "--enable-w32threads", "--enable-pthreads"),
    ("Darwin", "--enable-pthreads", "--enable-w32threads"),
])
def test_ffmpeg_thread_backend(tmp_path, monkeypatch, system, expected, excluded):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "build_ffmpeg.py"))
    build = module["main"]
    monkeypatch.setitem(build.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(build.__globals__, "SOURCES", {})
    monkeypatch.setattr(module["platform"], "system", lambda: system)
    monkeypatch.setattr(module["platform"], "machine", lambda: "arm64")
    monkeypatch.setattr(module["shutil"], "which", lambda tool: f"/tools/{tool}")
    commands = []

    class ConfigureReached(Exception):
        pass

    def capture(arguments, cwd, environment):
        commands.append(arguments)
        if cwd.name.startswith("ffmpeg-"):
            raise ConfigureReached

    monkeypatch.setitem(build.__globals__, "run", capture)
    with pytest.raises(ConfigureReached):
        build()
    assert expected in commands[-1]
    assert excluded not in commands[-1]