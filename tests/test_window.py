from pathlib import Path
import shutil

from PIL import Image
from PySide6.QtCore import QSize, QSettings
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QSizePolicy
import pytest

from png_webm_exporter.window import ExportWindow
from png_webm_exporter import resources
from test_encoding import make_quicktime_png


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
    preferences = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    widget = ExportWindow(preferences)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def preset_menu_texts(window):
    return [action.text() for action in window.preset_menu.actions() if not action.isSeparator()]


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


def test_codec_toggle_preserves_common_settings(window):
    # Set custom common settings on VP9
    window.vp9_button.click()
    vp9_panel = window.active_panel()
    vp9_panel.fps.setCurrentText("60")
    vp9_panel.threads.setValue(16)
    vp9_panel.gop.setValue(50)
    vp9_panel.export_frames.setChecked(True)
    vp9_panel.frame_quality.setValue(95)
    vp9_panel.crf.setValue(15)

    # Switch to AV1
    window.av1_button.click()
    av1_panel = window.active_panel()
    assert av1_panel.codec == "av1"

    # Common settings must NOT change when switching encoder
    assert av1_panel.fps.currentText() == "60"
    assert av1_panel.threads.value() == 16
    assert av1_panel.gop.value() == 50
    assert av1_panel.export_frames.isChecked()
    assert av1_panel.frame_quality.value() == 95
    # Encoder-dependent setting (CRF) remains encoder-specific
    assert av1_panel.crf.value() == 30

    # Modify common settings while in AV1
    av1_panel.threads.setValue(4)
    av1_panel.gop.setValue(100)
    av1_panel.export_frames.setChecked(False)

    # Switch back to VP9
    window.vp9_button.click()
    assert window.active_panel().codec == "vp9"
    assert vp9_panel.fps.currentText() == "60"
    assert vp9_panel.threads.value() == 4
    assert vp9_panel.gop.value() == 100
    assert not vp9_panel.export_frames.isChecked()
    assert vp9_panel.frame_quality.value() == 95
    assert vp9_panel.crf.value() == 15


def test_webp_frame_controls_are_common(window):
    panel = window.active_panel()
    common = panel.export_frames.parentWidget()
    common_form = common.layout()
    export_frames_label = common_form.labelForField(panel.export_frames)
    assert export_frames_label.text() == "Export first and last frames (WebP)"
    assert not export_frames_label.wordWrap()
    assert common.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
    assert common_form.labelForField(panel.frame_quality) is panel.frame_quality_label
    assert panel.delivery_form.labelForField(panel.export_frames) is None


def test_common_defaults(window):
    panel = window.active_panel()
    assert panel.threads.value() == 0
    assert panel.threads.text() == "Auto"
    assert panel.frame_quality.value() == 85


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
    assert window.color_reminder.geometry().bottom() < window.inputs.height()
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


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_movie_locks_fps(window, tmp_path):
    movie = make_quicktime_png(tmp_path, count=4, fps="24000/1001")
    assert window.vp9_panel.fps.isEnabled()
    assert window.av1_panel.fps.isEnabled()

    window.set_movie(movie)

    assert not window.vp9_panel.fps.isEnabled()
    assert not window.av1_panel.fps.isEnabled()
    assert window.vp9_panel.fps.currentText() == "24000/1001"
    assert window.av1_panel.fps.currentText() == "24000/1001"
    assert window.settings().fps == "24000/1001"

    # Switching codec retains locked fps
    window.av1_button.click()
    assert not window.av1_panel.fps.isEnabled()
    assert window.av1_panel.fps.currentText() == "24000/1001"
    assert window.settings().fps == "24000/1001"

    # Resetting settings while movie is loaded retains locked fps
    window.reset_settings()
    assert not window.av1_panel.fps.isEnabled()
    assert window.av1_panel.fps.currentText() == "24000/1001"
    assert window.settings().fps == "24000/1001"

    # Switching back to PNG sequence unlocks fps
    frames = [tmp_path / f"seq_{index:04d}.png" for index in range(2)]
    for frame in frames:
        Image.new("RGB", (32, 32), (10, 20, 30)).save(frame)
    window.set_frames(frames)
    assert window.vp9_panel.fps.isEnabled()
    assert window.av1_panel.fps.isEnabled()


def test_preset_combo_contains_builtins(window):
    items = [window.preset_combo.itemText(i) for i in range(window.preset_combo.count())]
    assert "Custom" in items
    assert "Good gradients on Android" in items
    assert "Good gradients on Android with Embedded Grain" in items
    assert preset_menu_texts(window) == ["Save current settings as a preset"]


def test_apply_builtin_presets_and_common_settings_untouched(window):
    # Set custom common settings and FPS on AV1 panel
    window.av1_button.click()
    av1_panel = window.active_panel()
    av1_panel.fps.setCurrentText("60")
    av1_panel.threads.setValue(16)
    av1_panel.gop.setValue(50)
    av1_panel.export_frames.setChecked(True)
    av1_panel.frame_quality.setValue(95)

    # Apply "Good gradients on Android"
    idx = window.preset_combo.findText("Good gradients on Android")
    window.preset_combo.setCurrentIndex(idx)

    assert window.active_panel().codec == "av1"
    av1_panel = window.active_panel()
    assert av1_panel.crf.value() == 8
    assert av1_panel.mode.currentIndex() == 0  # Manual CRF
    assert not av1_panel.fgs_enabled.isChecked()
    assert av1_panel.preset.value() == 8
    assert av1_panel.tune.currentIndex() == 0  # Visual Quality (0)
    assert not av1_panel.fgs_denoise.isChecked()

    # Verify common settings and fps were NOT touched
    assert av1_panel.fps.currentText() == "60"
    assert av1_panel.threads.value() == 16
    assert av1_panel.gop.value() == 50
    assert av1_panel.export_frames.isChecked()
    assert av1_panel.frame_quality.value() == 95

    # Builtin preset cannot be renamed or deleted
    assert preset_menu_texts(window) == ["Save current settings as a preset"]

    # Apply "Good gradients on Android with Embedded Grain"
    idx_grain = window.preset_combo.findText("Good gradients on Android with Embedded Grain")
    window.preset_combo.setCurrentIndex(idx_grain)

    assert window.active_panel().codec == "av1"
    assert av1_panel.crf.value() == 8
    assert av1_panel.mode.currentIndex() == 0
    assert av1_panel.fgs_enabled.isChecked()
    assert av1_panel.fgs_level.value() == 4
    assert av1_panel.preset.value() == 8
    assert av1_panel.tune.currentIndex() == 0
    assert not av1_panel.fgs_denoise.isChecked()

    # Common settings and fps still untouched
    assert av1_panel.fps.currentText() == "60"
    assert av1_panel.threads.value() == 16
    assert av1_panel.gop.value() == 50
    assert av1_panel.export_frames.isChecked()
    assert av1_panel.frame_quality.value() == 95
    assert preset_menu_texts(window) == ["Save current settings as a preset"]


def test_manual_tweak_switches_preset_to_custom(window):
    idx = window.preset_combo.findText("Good gradients on Android")
    window.preset_combo.setCurrentIndex(idx)
    assert window.preset_combo.currentText() == "Good gradients on Android"

    # Changing a standard setting switches combo to "Custom"
    window.active_panel().crf.setValue(16)
    assert window.preset_combo.currentText() == "Custom"

    # Changing it back matches the preset again
    window.active_panel().crf.setValue(8)
    assert window.preset_combo.currentText() == "Good gradients on Android"

    # Changing a common setting does NOT change preset selection
    window.active_panel().threads.setValue(24)
    assert window.preset_combo.currentText() == "Good gradients on Android"


def test_save_rename_delete_user_preset(window, monkeypatch):
    # Setup custom settings on VP9
    window.vp9_button.click()
    panel = window.active_panel()
    panel.crf.setValue(18)
    panel.bitrate.setValue(1500)
    panel.tiles.setValue(3)

    # Attempt to save with builtin name -> blocked
    warnings = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Good gradients on Android", True))
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, msg: warnings.append((title, msg)))
    window.save_preset_action.trigger()
    assert len(warnings) == 1
    assert "Cannot overwrite built-in preset" in warnings[0][0]

    # Save with valid custom name
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("My VP9 Preset", True))
    window.save_preset_action.trigger()

    assert window.preset_combo.currentText() == "My VP9 Preset"
    assert preset_menu_texts(window) == [
        "Save current settings as a preset", "Rename preset", "Delete preset"]
    assert "My VP9 Preset" in window.user_presets
    saved = window.user_presets["My VP9 Preset"]
    assert saved["codec"] == "vp9"
    assert saved["crf"] == 18
    assert saved["bitrate_kbps"] == 1500
    assert saved["tile_columns"] == 3
    assert "threads" not in saved
    assert "gop" not in saved
    assert "fps" not in saved

    # Rename preset
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("My Renamed Preset", True))
    window.rename_preset_action.trigger()

    assert window.preset_combo.currentText() == "My Renamed Preset"
    assert "My VP9 Preset" not in window.user_presets
    assert "My Renamed Preset" in window.user_presets

    # Delete preset
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.delete_preset_action.trigger()

    assert window.preset_combo.currentText() == "Custom"
    assert "My Renamed Preset" not in window.user_presets
    assert preset_menu_texts(window) == ["Save current settings as a preset"]


def test_user_presets_persisted(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings_presets.ini"
    prefs = QSettings(str(settings_file), QSettings.Format.IniFormat)
    win1 = ExportWindow(prefs)
    win1.vp9_button.click()
    win1.active_panel().crf.setValue(22)

    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Persisted Preset", True))
    win1.save_preset_action.trigger()
    assert "Persisted Preset" in win1.user_presets
    win1.close()

    # Reopen window with same preferences
    prefs2 = QSettings(str(settings_file), QSettings.Format.IniFormat)
    win2 = ExportWindow(prefs2)
    items = [win2.preset_combo.itemText(i) for i in range(win2.preset_combo.count())]
    assert "Persisted Preset" in items
    win2.apply_preset("Persisted Preset")
    assert win2.active_panel().crf.value() == 22
    assert preset_menu_texts(win2) == [
        "Save current settings as a preset", "Rename preset", "Delete preset"]
    win2.close()
