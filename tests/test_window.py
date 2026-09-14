from pathlib import Path
import shutil

from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox
import pytest

from png_webm_exporter.window import ExportWindow
from png_webm_exporter import resources


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    widget = ExportWindow(preferences)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_target_controls_and_reset(window):
    assert not window.target.isEnabled()
    assert not window.target.isVisible()
    assert not window.delivery_form.labelForField(window.target).isVisible()
    assert window.crf_label.text() == "CRF"
    window.mode.setCurrentIndex(1)
    assert window.target.isEnabled()
    assert window.target.isVisible()
    assert window.delivery_form.labelForField(window.target).isVisible()
    assert window.crf_label.text() == "Initial CRF"
    assert not window.bitrate.isEnabled()
    window.crf.setValue(20)
    assert window.settings().crf == 20
    window.target.setText("2.5")
    assert window.settings().target_bytes == 2_500_000
    window.mode.setCurrentIndex(0)
    window.target.setText("invalid hidden target")
    assert window.settings().target_bytes is None
    assert window.crf_label.text() == "CRF"
    window.reset_settings()
    assert window.settings().crf == 12
    assert window.settings().target_bytes is None
    assert not window.target.isVisible()


def test_crf_help_matches_mode(window, monkeypatch):
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, message: messages.append((title, message)))
    window.crf_help.click()
    assert messages[-1][0] == "CRF explained"
    assert "Constant Rate Factor" in messages[-1][1]
    assert "one encode uses the value you choose" in messages[-1][1]
    window.mode.setCurrentIndex(1)
    window.crf_help.click()
    assert messages[-1][0] == "Initial CRF explained"
    assert "used only for the first trial" in messages[-1][1]
    assert "not a quality limit" in messages[-1][1]


def test_readme_viewer(window):
    window.readme_button.click()
    assert window.readme_dialog.isVisible()
    assert window.readme_view.isReadOnly()
    assert window.readme_view.toPlainText() == resources.readme_text()
    dialog = window.readme_dialog
    dialog.close()
    window.readme_button.click()
    assert window.readme_dialog is dialog
    assert dialog.isVisible()


def test_frozen_readme_path(tmp_path, monkeypatch):
    source = tmp_path / "application-source"
    source.mkdir()
    (source / "README.txt").write_text("Bundled README contents", encoding="utf-8")
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resources.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.readme_text() == "Bundled README contents"


def test_compact_layout(window, qtbot):
    window.resize(720, 640)
    qtbot.wait(20)
    assert window.preview.geometry().bottom() < window.scrubber.geometry().top()
    assert window.color_confirm.geometry().bottom() < window.inputs.height()
    window.tabs.setCurrentIndex(1)
    qtbot.wait(20)
    assert window.gop.width() >= 100
    assert window.gop.specialValueText() == "Auto"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_gui_export(window, qtbot, tmp_path):
    frames = [tmp_path / f"frame_{index:04d}.png" for index in range(3)]
    for path in frames:
        Image.new("RGB", (64, 64), (40, 120, 180)).save(path)
    window.set_frames(list(reversed(frames)))
    assert window.sequence.files == tuple(frames)
    assert not window.source_pixmap.isNull()
    window.scrubber.setValue(2)
    assert "3 / 3" in window.frame_name.text()
    window.color_confirm.setChecked(True)
    window.mode.setCurrentIndex(1)
    window.target.setText("0.02")
    window.destination.setText(str(tmp_path))
    destination = tmp_path / "frame.webm"
    window.start_export()
    assert not window.export.isEnabled()
    qtbot.waitUntil(lambda: window.export.isEnabled(), timeout=30_000)
    assert window.last_output == destination
    assert destination.stat().st_size <= 20_000
    assert window.progress.value() == 100
    assert window.open_button.isEnabled()