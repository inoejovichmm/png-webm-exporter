from pathlib import Path
import shutil

from PIL import Image
from PySide6.QtCore import QSize, QSettings
from PySide6.QtWidgets import QFileDialog, QMessageBox
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
    panel = window.active_panel()
    assert not panel.target.isEnabled()
    assert not panel.target.isVisible()
    assert not panel.delivery_form.labelForField(panel.target).isVisible()
    assert panel.crf_label.text() == "CRF"
    panel.mode.setCurrentIndex(1)
    assert panel.target.isEnabled()
    assert panel.target.isVisible()
    assert panel.delivery_form.labelForField(panel.target).isVisible()
    assert panel.crf_label.text() == "Initial CRF"
    assert not panel.bitrate.isEnabled()
    panel.crf.setValue(20)
    assert window.settings().crf == 20
    panel.target.setText("2.5")
    assert window.settings().target_bytes == 2_500_000
    panel.mode.setCurrentIndex(0)
    panel.target.setText("invalid hidden target")
    assert window.settings().target_bytes is None
    assert panel.crf_label.text() == "CRF"
    window.reset_settings()
    assert window.settings().crf == 12
    assert window.settings().target_bytes is None
    assert not panel.target.isVisible()


def test_codec_toggle(window):
    assert window.active_panel().codec == "vp9"
    window.av1_button.click()
    assert window.active_panel().codec == "av1"
    assert window.settings().codec == "av1"
    window.vp9_button.click()
    assert window.active_panel().codec == "vp9"


def test_webp_frame_controls_are_common(window):
    panel = window.active_panel()
    common_form = panel.export_frames.parentWidget().layout()
    assert common_form.labelForField(panel.export_frames).text() == "Export first and last frames (WebP)"
    assert common_form.labelForField(panel.frame_quality) is panel.frame_quality_label
    assert panel.delivery_form.labelForField(panel.export_frames) is None


def test_crf_help_matches_mode(window, monkeypatch):
    panel = window.active_panel()
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, message: messages.append((title, message)))
    panel.crf_help.click()
    assert messages[-1][0] == "CRF explained"
    assert "Constant Rate Factor" in messages[-1][1]
    assert "one encode uses the value you choose" in messages[-1][1]
    panel.mode.setCurrentIndex(1)
    panel.crf_help.click()
    assert messages[-1][0] == "Initial CRF explained"
    assert "used only for the first trial" in messages[-1][1]
    assert "global optimum is not guaranteed" in messages[-1][1]


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


def test_log_viewer_exports_txt(window, monkeypatch, tmp_path):
    window.show_log()
    assert window.log_dialog.width() >= 900
    assert window.log_view.toPlainText() == "No export yet."
    destination = tmp_path / "export-log.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(destination), "Text files (*.txt)"))
    window.export_log()
    assert destination.read_text(encoding="utf-8") == "No export yet."


def test_cancel_closes_export_dialog(window, qtbot):
    window.create_export_dialog()
    window.export_dialog.show()
    assert window.export_dialog.isVisible()
    window.on_canceled()
    qtbot.wait(10)
    assert not window.export_dialog.isVisible()


def test_export_failure_shows_exit(window):
    window.create_export_dialog()
    window.export_dialog.show()
    window.on_failure("encoding failed")
    assert not window.export_cancel.isVisible()
    assert not window.export_open.isVisible()
    assert window.export_done.isVisible()
    assert window.export_done.text() == "Exit"


def test_frozen_readme_path(tmp_path, monkeypatch):
    source = tmp_path / "application-source"
    source.mkdir()
    (source / "README.txt").write_text("Bundled README contents", encoding="utf-8")
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resources.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.readme_text() == "Bundled README contents"


def test_new_source_updates_output_defaults(window, tmp_path):
    source_dir = tmp_path / "sequence"
    source_dir.mkdir()
    frames = [source_dir / f"new_{index:04d}.png" for index in range(2)]
    for frame in frames:
        Image.new("RGB", (32, 32), (10, 20, 30)).save(frame)

    window.destination.setText(str(tmp_path / "old-output"))
    window.webm_name.setText("custom-name")
    window.first_frame_name.setText("custom-first")
    window.last_frame_name.setText("custom-last")

    window.set_frames(frames)

    assert window.destination.text() == str(source_dir)
    assert window.webm_name.text() == "new"
    assert window.first_frame_name.text() == "new_first_frame"
    assert window.last_frame_name.text() == "new_last_frame"


def test_input_path_label_updates(window, tmp_path):
    source_dir = tmp_path / "sequence"
    source_dir.mkdir()
    frames = [source_dir / f"input_{index:04d}.png" for index in range(2)]
    for frame in frames:
        Image.new("RGB", (32, 32), (10, 20, 30)).save(frame)

    window.set_frames(frames)

    assert window.input_path.text() == str(source_dir)


def test_compact_layout(window, qtbot):
    qtbot.wait(20)
    assert window.size().width() >= window.minimumSize().width()
    assert window.size().height() >= window.minimumSize().height()
    assert window.size().width() <= 960
    assert window.size().height() <= 900
    assert window.preview.geometry().bottom() < window.scrubber.geometry().top()
    assert window.color_confirm.geometry().bottom() < window.inputs.height()
    assert window.active_panel().gop.width() >= 100
    assert window.active_panel().gop.specialValueText() == "Auto"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_gui_export(window, qtbot, tmp_path, monkeypatch):
    frames = [tmp_path / f"frame_{index:04d}.png" for index in range(3)]
    for path in frames:
        Image.new("RGB", (64, 64), (40, 120, 180)).save(path)
    window.set_frames(list(reversed(frames)))
    assert window.sequence.files == tuple(frames)
    assert not window.source_pixmap.isNull()
    window.scrubber.setValue(2)
    assert "3 / 3" in window.frame_name.text()
    window.color_confirm.setChecked(True)
    panel = window.active_panel()
    panel.mode.setCurrentIndex(1)
    panel.target.setText("0.02")
    window.destination.setText(str(tmp_path))
    destination = tmp_path / "frame.webm"
    window.start_export()
    assert not window.export.isEnabled()
    qtbot.waitUntil(lambda: window.export.isEnabled(), timeout=30_000)
    assert window.last_output == destination
    assert destination.stat().st_size <= 20_000
    assert window.export_progress.value() == 100
    assert not window.export_cancel.isVisible()
    assert window.export_open.isVisible()
    assert window.export_done.isVisible()
    window.export_done.click()
    assert not window.export_dialog.isVisible()
    window.export_dialog.close()