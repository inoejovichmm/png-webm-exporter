from dataclasses import asdict, replace
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import tempfile

from PySide6.QtCore import QEvent, Qt, QSettings, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QGroupBox, QPushButton, QRadioButton, QSizePolicy, QSlider, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .encoding import BUILTIN_PRESETS, ExportWorker, Settings, extract_poster, file_signature, inspect_movie
from .resources import readme_text
from .sequence import MovieSource, detect_sequence
from .size_search import megabytes_to_bytes


def make_spin(minimum, maximum, value):
    control = QSpinBox()
    control.setRange(minimum, maximum)
    control.setValue(value)
    control.setMinimumWidth(100)
    return control


def add_field(form, text, control, explanation):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setToolTip(explanation)
    control.setToolTip(explanation)
    control.setAccessibleName(text)
    form.addRow(label, control)
    return label


CRF_INTRO = (
    "CRF means Constant Rate Factor: the encoder's quality setting, not a file-size setting. "
    "It ranges from 0 to 63 for VP9 and 1 to 63 for AV1.\n\n"
    "Lower CRF usually retains more detail and produces larger files. Higher CRF usually "
    "produces smaller files but can lose detail, smooth away grain/dither, or expose banding. "
    "It is not a percentage or a linear size scale.\n\n")


class CodecPanel(QWidget):
    """Encoder-specific settings for one codec ("vp9" or "av1")."""

    def __init__(self, codec, on_change, parent=None):
        super().__init__(parent)
        self.codec = codec
        self.on_change = on_change
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        standard = QGroupBox()
        content_layout.addWidget(standard)
        form = QFormLayout(standard)
        self.delivery_form = form
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fps = QComboBox()
        self.fps.setEditable(True)
        self.fps.addItems(["25", "24", "24000/1001", "30", "30000/1001", "50", "60"])
        add_field(form, "Frame rate", self.fps, "Frames per second. Fractions such as 24000/1001 are accepted. Every selected frame is encoded once; duration is frame count divided by FPS.")
        self.mode = QComboBox()
        self.mode.addItems(["Manual CRF", "Target size"])
        add_field(form, "Quality mode", self.mode, "Target size tries up to nine complete encodes and keeps the lowest tested CRF that fits. It changes only CRF and forces bitrate to zero. It can take several times longer than a manual export.")
        self.crf = make_spin(0 if codec == "vp9" else 1, 63, 12 if codec == "vp9" else 30)
        crf_row = QWidget()
        crf_layout = QHBoxLayout(crf_row)
        crf_layout.setContentsMargins(0, 0, 0, 0)
        crf_layout.addWidget(self.crf, 1)
        self.crf_help = QPushButton("Explain")
        self.crf_help.setToolTip("Explain CRF and the selected quality mode")
        self.crf_help.setAccessibleName("Explain CRF")
        self.crf_help.clicked.connect(self.show_crf_help)
        crf_layout.addWidget(self.crf_help)
        self.crf_label = add_field(form, "CRF", crf_row, "")
        self.target = QLineEdit("10")
        add_field(form, "Target size (MB)", self.target, "Hard cap in decimal megabytes: 1 MB = 1,000,000 bytes, including WebM overhead. Not an exact fill. If the target is impossible, the smallest tested video is delivered instead. CRF-to-size is not guaranteed monotonic.")
        self.range = QComboBox()
        self.range.addItems(["Full (0-255)", "Limited (16-235)"])
        add_field(form, "Output range", self.range, "Controls RGB-to-YUV conversion and range metadata together. Full is the delivery preset; choose Limited only when required by the playback pipeline. Both use a Rec.709 matrix and tags.")
        if codec == "av1":
            self.fgs_enabled = QCheckBox()
            self.fgs_enabled.setChecked(True)
            add_field(form, "Film grain synthesis", self.fgs_enabled, "When on, the AV1 decoder regenerates fine grain at playback from bitstream metadata instead of the encoder storing it. This breaks up 8-bit banding on shallow gradients while keeping the encode clean and small. Requires a decoder that applies film grain; modern Pixel devices do.")
            self.fgs_level = make_spin(0, 50, 8)
            self.fgs_level_label = add_field(form, "Film grain level", self.fgs_level, "Strength of the synthesized grain, 0-50. Higher adds more visible grain. Start near 8 and tune on the least capable target panel, which is the worst case for banding. Zero disables synthesis even when the toggle is on.")
        standard_form = form
        if codec == "vp9":
            self.bitrate = make_spin(0, 1_000_000, 0)
            self.bitrate.setSuffix(" kb/s")
            add_field(standard_form, "Bitrate", self.bitrate, "Zero selects constant-quality encoding. A nonzero bitrate constrains quality using libvpx rate control; it is not an exact size cap. Target size always uses zero.")
            self.alt_ref = QCheckBox()
            add_field(standard_form, "Alternate reference frames", self.alt_ref, "Allows hidden alternate reference frames. This can improve compression but temporal filtering can change fine grain. Default off.")
            self.arnr = make_spin(0, 15, 0)
            add_field(standard_form, "Temporal filter frames", self.arnr, "Alternate-reference temporal filtering window, 0-15 frames. Zero disables filtering. Relevant when alternate reference frames are enabled.")
            self.aq = QComboBox()
            self.aq.addItems(["0 - Off", "1 - Variance", "2 - Complexity", "3 - Cyclic refresh", "4 - Equator360"])
            add_field(standard_form, "Adaptive quantization", self.aq, "Redistributes quantization across the image. Off preserves the baseline behavior; other modes can change gradients and grain. Equator360 is for 360-degree video.")
            self.row_mt = QCheckBox()
            self.row_mt.setChecked(True)
            add_field(standard_form, "Row multithreading", self.row_mt, "Parallel row encoding, where supported by libvpx. Default on.")
            self.tiles = make_spin(0, 6, 2)
            add_field(standard_form, "Tile columns (log2)", self.tiles, "Requested power-of-two tile columns: 0 = 1, 1 = 2, 2 = 4. libvpx limits this according to frame width. More tiles can trade compression efficiency for parallelism.")
        else:
            self.preset = make_spin(0, 13, 8)
            add_field(standard_form, "Encoder preset", self.preset, "SVT-AV1 speed/quality preset, 0 (slowest, best compression) to 13 (fastest). Lower is slower but smaller for the same quality. 8 is a balanced default.")
            self.tune = QComboBox()
            self.tune.addItems(["0 - Visual quality (VQ)", "1 - PSNR", "2 - SSIM"])
            self.tune.setCurrentIndex(1)
            add_field(standard_form, "Visual tuning", self.tune, "SVT-AV1 tuning objective. VQ (0) prioritizes perceptual quality and texture; PSNR (1) preserves the current objective-metric behavior; SSIM (2) can suit smooth gradients and structural detail.")
            self.fgs_denoise = QCheckBox()
            add_field(standard_form, "Film grain denoise source", self.fgs_denoise, "Denoise the source before analyzing grain for synthesis. Leave off for already-clean renders; turn on only when the source itself carries grain you want removed and re-synthesized.")
        outer.addWidget(content)
        common = QGroupBox()
        common.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        common_form = QFormLayout(common)
        common_form.setVerticalSpacing(8)
        self.threads = make_spin(0, 256, 0)
        self.threads.setSpecialValueText("Auto")
        add_field(common_form, "Threads", self.threads, "Maximum encoder threads. Zero lets the encoder choose. More threads do not guarantee faster encoding.")
        self.gop = make_spin(0, 1_000_000, 0)
        self.gop.setSpecialValueText("Auto")
        add_field(common_form, "Max keyframe distance", self.gop, "Maximum frames between keyframes. Zero leaves the encoder default. Shorter distances improve seeking but can increase file size.")
        self.export_frames = QCheckBox()
        export_frames_label = add_field(common_form, "Export first and last frames (WebP)", self.export_frames, "After the WebM is verified, also save the first and last frames next to it as <name>_first_frame.webp and <name>_last_frame.webp. Frames are extracted from the finished video and encoded with libwebp.")
        export_frames_label.setWordWrap(False)
        self.frame_quality = make_spin(0, 100, 85)
        self.frame_quality_label = add_field(common_form, "Frame WebP quality", self.frame_quality, "libwebp quality passed as -q:v, from 0 (smallest) to 100. Setting 100 switches libwebp into lossless mode instead of using the quality scale.")
        outer.addWidget(common)
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.mode.currentIndexChanged.connect(lambda *_: self.on_change())
        self.fps.currentTextChanged.connect(lambda *_: self.on_change())
        self.crf.valueChanged.connect(lambda *_: self.on_change())
        self.target.textChanged.connect(lambda *_: self.on_change())
        self.range.currentIndexChanged.connect(lambda *_: self.on_change())
        if codec == "vp9":
            self.bitrate.valueChanged.connect(lambda *_: self.on_change())
            self.alt_ref.toggled.connect(lambda *_: self.on_change())
            self.arnr.valueChanged.connect(lambda *_: self.on_change())
            self.aq.currentIndexChanged.connect(lambda *_: self.on_change())
            self.row_mt.toggled.connect(lambda *_: self.on_change())
            self.tiles.valueChanged.connect(lambda *_: self.on_change())
        else:
            self.preset.valueChanged.connect(lambda *_: self.on_change())
            self.tune.currentIndexChanged.connect(lambda *_: self.on_change())
            self.fgs_enabled.toggled.connect(lambda *_: self.on_change())
            self.fgs_level.valueChanged.connect(lambda *_: self.on_change())
            self.fgs_denoise.toggled.connect(lambda *_: self.on_change())
        self.export_frames.toggled.connect(self.update_frame_controls)
        self.frame_quality.valueChanged.connect(self.update_frame_controls)
        self.threads.valueChanged.connect(lambda *_: self.on_change())
        self.gop.valueChanged.connect(lambda *_: self.on_change())
        if codec == "av1":
            self.fgs_enabled.toggled.connect(self.update_fgs_controls)

    def update_mode(self):
        target_mode = self.mode.currentIndex() == 1
        self.delivery_form.setRowVisible(self.target, target_mode)
        self.target.setEnabled(target_mode)
        if self.codec == "vp9":
            self.bitrate.setEnabled(not target_mode)
        label = "Initial CRF" if target_mode else "CRF"
        self.crf_label.setText(label)
        self.crf.setAccessibleName(label)
        explanation = CRF_INTRO
        if self.codec == "av1":
            explanation += (
                "AV1 (SVT-AV1): CRF 0 is unavailable because SVT-AV1 treats it as its default rate "
                "factor rather than quality 0. With film "
                "grain synthesis you can encode clean at a higher CRF for a smaller file and let the "
                "decoder add grain back at playback. Start around 30 and tune per device.\n\n")
        else:
            explanation += (
                "VP9 (libvpx): CRF 0 does not guarantee a lossless export with this 8-bit YUV 4:2:0 "
                "conversion. The default here is 12; lower it to keep more baked dither/grain.\n\n")
        if target_mode:
            explanation += (
                "Target size: Initial CRF is used only for the first trial. The search can then try "
                "values above or below it, across 0-63, running up to nine full encodes with bitrate "
                "forced to zero. The lowest tested CRF that fits the cap is kept. If none fit, the "
                "smallest valid tested video is delivered and reported as exceeding the target. Size "
                "is not perfectly monotonic, so a global optimum is not guaranteed.")
        else:
            explanation += (
                "Manual CRF: one encode uses the value you choose. There is no automatic size search "
                "or size cap. Inspect the output and adjust.")
        self.crf.setToolTip(explanation)
        self.crf_label.setToolTip(explanation)

    def update_frame_controls(self):
        enabled = self.export_frames.isChecked()
        self.frame_quality.setEnabled(enabled)
        self.frame_quality_label.setEnabled(enabled)
        self.frame_quality.setSuffix(" (lossless)" if self.frame_quality.value() >= 100 else "")
        self.on_change()

    def update_fgs_controls(self):
        enabled = self.fgs_enabled.isChecked()
        self.fgs_level.setEnabled(enabled)
        self.fgs_level_label.setEnabled(enabled)
        self.fgs_denoise.setEnabled(enabled)

    def show_crf_help(self):
        QMessageBox.information(self, f"{self.crf_label.text()} explained", self.crf.toolTip())

    def settings(self):
        common = dict(
            fps=self.fps.currentText().strip(), codec=self.codec, crf=self.crf.value(),
            target_bytes=megabytes_to_bytes(self.target.text()) if self.mode.currentIndex() else None,
            full_range=self.range.currentIndex() == 0, threads=self.threads.value(),
            gop=self.gop.value(), export_frames=self.export_frames.isChecked(),
            frame_quality=self.frame_quality.value())
        if self.codec == "vp9":
            return Settings(**common, bitrate_kbps=self.bitrate.value(),
                            auto_alt_ref=self.alt_ref.isChecked(), arnr_maxframes=self.arnr.value(),
                            aq_mode=self.aq.currentIndex(), row_mt=self.row_mt.isChecked(),
                            tile_columns=self.tiles.value())
        return Settings(**common, preset=self.preset.value(),
                        tune=self.tune.currentIndex(),
                        fgs_enabled=self.fgs_enabled.isChecked(), fgs_level=self.fgs_level.value(),
                        fgs_denoise=self.fgs_denoise.isChecked())

    def apply(self, settings):
        self.fps.setCurrentText(settings.fps)
        self.crf.setValue(settings.crf)
        self.mode.setCurrentIndex(int(settings.target_bytes is not None))
        self.target.setText(str(settings.target_bytes / 1_000_000) if settings.target_bytes else "10")
        self.range.setCurrentIndex(0 if settings.full_range else 1)
        self.threads.setValue(settings.threads)
        self.gop.setValue(settings.gop)
        self.export_frames.setChecked(settings.export_frames)
        self.frame_quality.setValue(settings.frame_quality)
        if self.codec == "vp9":
            self.bitrate.setValue(settings.bitrate_kbps)
            self.alt_ref.setChecked(settings.auto_alt_ref)
            self.arnr.setValue(settings.arnr_maxframes)
            self.aq.setCurrentIndex(settings.aq_mode)
            self.row_mt.setChecked(settings.row_mt)
            self.tiles.setValue(settings.tile_columns)
        else:
            self.preset.setValue(settings.preset)
            self.tune.setCurrentIndex(settings.tune)
            self.fgs_enabled.setChecked(settings.fgs_enabled)
            self.fgs_level.setValue(settings.fgs_level)
            self.fgs_denoise.setChecked(settings.fgs_denoise)
            self.update_fgs_controls()
        self.update_mode()
        self.update_frame_controls()

    def standard_settings(self):
        data = {
            "codec": self.codec,
            "crf": self.crf.value(),
            "target_bytes": megabytes_to_bytes(self.target.text()) if self.mode.currentIndex() else None,
            "full_range": self.range.currentIndex() == 0,
        }
        if self.codec == "vp9":
            data.update({
                "bitrate_kbps": self.bitrate.value(),
                "auto_alt_ref": self.alt_ref.isChecked(),
                "arnr_maxframes": self.arnr.value(),
                "aq_mode": self.aq.currentIndex(),
                "row_mt": self.row_mt.isChecked(),
                "tile_columns": self.tiles.value(),
            })
        else:
            data.update({
                "preset": self.preset.value(),
                "tune": self.tune.currentIndex(),
                "fgs_enabled": self.fgs_enabled.isChecked(),
                "fgs_level": self.fgs_level.value(),
                "fgs_denoise": self.fgs_denoise.isChecked(),
            })
        return data

    def apply_standard(self, data):
        if "crf" in data:
            self.crf.setValue(data["crf"])
        if "target_bytes" in data:
            target_bytes = data["target_bytes"]
            self.mode.setCurrentIndex(1 if target_bytes is not None else 0)
            self.target.setText(str(target_bytes / 1_000_000) if target_bytes is not None else "10")
        if "full_range" in data:
            self.range.setCurrentIndex(0 if data["full_range"] else 1)
        if self.codec == "vp9":
            if "bitrate_kbps" in data:
                self.bitrate.setValue(data["bitrate_kbps"])
            if "auto_alt_ref" in data:
                self.alt_ref.setChecked(data["auto_alt_ref"])
            if "arnr_maxframes" in data:
                self.arnr.setValue(data["arnr_maxframes"])
            if "aq_mode" in data:
                self.aq.setCurrentIndex(data["aq_mode"])
            if "row_mt" in data:
                self.row_mt.setChecked(data["row_mt"])
            if "tile_columns" in data:
                self.tiles.setValue(data["tile_columns"])
        else:
            if "preset" in data:
                self.preset.setValue(data["preset"])
            if "tune" in data:
                self.tune.setCurrentIndex(data["tune"])
            if "fgs_enabled" in data:
                self.fgs_enabled.setChecked(data["fgs_enabled"])
            if "fgs_level" in data:
                self.fgs_level.setValue(data["fgs_level"])
            if "fgs_denoise" in data:
                self.fgs_denoise.setChecked(data["fgs_denoise"])
            self.update_fgs_controls()
        self.update_mode()
        self.on_change()


class ExportWindow(QMainWindow):
    def __init__(self, preferences=None):
        super().__init__()
        self.preferences = preferences if preferences is not None else QSettings()
        self.sequence = None
        self.worker = None
        self.last_output = None
        self.closing = False
        self.readme_dialog = None
        self.source_pixmap = QPixmap()
        self.user_presets = {}
        self._applying_preset = False
        self.setWindowTitle("PNG to WebM")
        self.resize(900, 760)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(10)
        self.readme_button = QPushButton("README")
        self.readme_button.setToolTip("Read the application README")
        self.readme_button.clicked.connect(self.show_readme)
        self.inputs = QWidget()
        columns = QHBoxLayout(self.inputs)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(24)
        source = QVBoxLayout()
        select_row = QHBoxLayout()
        self.choose_input = QPushButton("Select input...")
        self.choose_input.setToolTip("Choose a PNG sequence folder with frames or a single ProRes 4444 movie.")
        input_menu = QMenu(self.choose_input)
        folder_action = QAction("PNG sequence folder with frames", self.choose_input)
        folder_action.triggered.connect(self.choose_folder)
        movie_action = QAction("ProRes 4444 movie", self.choose_input)
        movie_action.triggered.connect(self.choose_movie)
        input_menu.addAction(folder_action)
        input_menu.addAction(movie_action)
        self.choose_input.setMenu(input_menu)
        select_row.addWidget(self.choose_input, 1)
        self.input_help = self.help_button("How to export either input from After Effects",
                                            self.show_input_help)
        select_row.addWidget(self.input_help)
        source.addLayout(select_row)
        self.input_path = QLabel("No input selected")
        self.input_path.setWordWrap(True)
        self.input_path.setStyleSheet("color: #b9c4c1;")
        self.input_path.setToolTip("Selected input folder or movie")
        source.addWidget(self.input_path)
        source.setSpacing(5)
        self.preview = QLabel("No frames selected")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(180, 130)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.preview.setMaximumHeight(300)
        self.preview.setStyleSheet("background: #202323; color: #d7dedb; border-radius: 4px;")
        self.preview.installEventFilter(self)
        source.addWidget(self.preview)
        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 0)
        self.scrubber.setToolTip("Preview a selected source frame. Preview is not a color-managed output monitor.")
        self.scrubber.valueChanged.connect(self.show_frame)
        source.addWidget(self.scrubber)
        self.frame_name = QLabel("")
        self.frame_name.setWordWrap(True)
        source.addWidget(self.frame_name)
        self.summary = QLabel("No sequence")
        self.summary.setWordWrap(True)
        source.addWidget(self.summary)
        self.color_reminder = QLabel("Source must be standard SDR (Rec.709/sRGB) with flattened transparency.")
        self.color_reminder.setWordWrap(True)
        self.color_reminder.setStyleSheet("color: #8b9895; font-size: 11px;")
        self.color_reminder.setToolTip("The exporter tags output as Rec.709 and does not color-manage: ensure After Effects compositions use sRGB / Rec.709. Non-standard color profiles/tags (such as P3/HDR) and transparent pixels are automatically rejected.")
        source.addWidget(self.color_reminder)
        source.addStretch()
        columns.addLayout(source, 1)
        self.codec_toggle = QButtonGroup(self)
        codec_switch = QHBoxLayout()
        codec_switch.setContentsMargins(0, 0, 0, 0)
        codec_switch.setSpacing(0)
        self.vp9_button = QRadioButton("VP9")
        self.av1_button = QRadioButton("AV1")
        for button in (self.vp9_button, self.av1_button):
            button.setAutoExclusive(True)
            self.codec_toggle.addButton(button)
            codec_switch.addWidget(button)
        self.vp9_button.toggled.connect(lambda checked: self.set_codec("vp9") if checked else None)
        self.av1_button.toggled.connect(lambda checked: self.set_codec("av1") if checked else None)
        self.codec_stack = QStackedWidget()
        self.vp9_panel = CodecPanel("vp9", self.on_panel_change)
        self.av1_panel = CodecPanel("av1", self.on_panel_change)
        self.codec_stack.addWidget(self.vp9_panel)
        self.codec_stack.addWidget(self.av1_panel)
        self._connect_common_sync()
        self.vp9_button.setChecked(True)
        codec_column = QVBoxLayout()
        codec_column.setSpacing(4)
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_label = QLabel("Preset:")
        self.preset_combo = QComboBox()
        self.preset_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.setMinimumContentsLength(10)
        self.preset_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.preset_combo.setToolTip("Select a built-in or custom preset to apply standard settings.")
        self.preset_combo.setAccessibleName("Presets")
        self.preset_menu_button = QPushButton("...")
        self.preset_menu_button.setToolTip("Manage presets")
        self.preset_menu_button.setAccessibleName("Manage presets")
        self.preset_menu = QMenu(self.preset_menu_button)
        self.save_preset_action = QAction("Save current settings as a preset", self.preset_menu_button)
        self.preset_menu_separator = QAction(self.preset_menu_button)
        self.preset_menu_separator.setSeparator(True)
        self.rename_preset_action = QAction("Rename preset", self.preset_menu_button)
        self.delete_preset_action = QAction("Delete preset", self.preset_menu_button)
        self.preset_menu_button.setMenu(self.preset_menu)
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.preset_menu_button)
        self.preset_combo.currentIndexChanged.connect(self.on_preset_combo_changed)
        self.save_preset_action.triggered.connect(self.save_current_as_preset)
        self.rename_preset_action.triggered.connect(self.rename_preset)
        self.delete_preset_action.triggered.connect(self.delete_preset)
        codec_column.addLayout(preset_row)
        codec_column.addLayout(codec_switch)
        codec_column.addWidget(self.codec_stack)
        columns.addLayout(codec_column, 1)
        layout.addWidget(self.inputs, 1)
        output_row = QHBoxLayout()
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Output folder")
        self.destination.setToolTip("Destination folder. Existing files are replaced only after successful verification and your confirmation.")
        self.browse = QPushButton("Browse\u2026")
        self.browse.setToolTip("Choose output folder")
        self.browse.setAccessibleName("Choose output folder")
        self.browse.clicked.connect(self.choose_output)
        output_row.addWidget(self.destination, 1)
        output_row.addWidget(self.browse)
        layout.addLayout(output_row)
        self.output_names_form = QFormLayout()
        self.output_names_form.setVerticalSpacing(6)
        self.output_names_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.webm_name = QLineEdit()
        self.webm_name.setToolTip("Output file name, without extension; \".webm\" is added automatically. Initialized from the input's name.")
        add_field(self.output_names_form, "Output file name", self.webm_name, self.webm_name.toolTip())
        self.first_frame_name = QLineEdit()
        self.first_frame_name.setToolTip("First-frame file name, without extension; \".webp\" is added automatically.")
        add_field(self.output_names_form, "First frame file name", self.first_frame_name, self.first_frame_name.toolTip())
        self.last_frame_name = QLineEdit()
        self.last_frame_name.setToolTip("Last-frame file name, without extension; \".webp\" is added automatically.")
        add_field(self.output_names_form, "Last frame file name", self.last_frame_name, self.last_frame_name.toolTip())
        layout.addLayout(self.output_names_form)
        buttons = QHBoxLayout()
        buttons.addWidget(self.readme_button)
        self.reset = QPushButton("Reset settings")
        self.reset.clicked.connect(self.reset_settings)
        buttons.addWidget(self.reset)
        self.log_button = QPushButton("Log")
        self.log_button.clicked.connect(self.show_log)
        buttons.addWidget(self.log_button)
        buttons.addStretch()
        self.export = QPushButton("Export WebM")
        self.export.setDefault(True)
        self.export.clicked.connect(self.start_export)
        buttons.addWidget(self.export)
        layout.addLayout(buttons)
        self.destination.textChanged.connect(self.update_output_summary)
        self.restore_settings()
        self.update_output_summary()

    def active_panel(self):
        return self.codec_stack.currentWidget()

    def _sync_common(self, source_panel):
        if getattr(self, "_syncing_common", False):
            return
        self._syncing_common = True
        try:
            target = self.av1_panel if source_panel is self.vp9_panel else self.vp9_panel
            if target.fps.currentText() != source_panel.fps.currentText():
                target.fps.setCurrentText(source_panel.fps.currentText())
            if target.threads.value() != source_panel.threads.value():
                target.threads.setValue(source_panel.threads.value())
            if target.gop.value() != source_panel.gop.value():
                target.gop.setValue(source_panel.gop.value())
            if target.export_frames.isChecked() != source_panel.export_frames.isChecked():
                target.export_frames.setChecked(source_panel.export_frames.isChecked())
            if target.frame_quality.value() != source_panel.frame_quality.value():
                target.frame_quality.setValue(source_panel.frame_quality.value())
            target.update_frame_controls()
        finally:
            self._syncing_common = False

    def _connect_common_sync(self):
        self._syncing_common = False

        def make_sync(source, target):
            def sync_fps(text):
                if self._syncing_common:
                    return
                self._syncing_common = True
                try:
                    if target.fps.currentText() != text:
                        target.fps.setCurrentText(text)
                finally:
                    self._syncing_common = False

            def sync_threads(val):
                if self._syncing_common:
                    return
                self._syncing_common = True
                try:
                    if target.threads.value() != val:
                        target.threads.setValue(val)
                finally:
                    self._syncing_common = False

            def sync_gop(val):
                if self._syncing_common:
                    return
                self._syncing_common = True
                try:
                    if target.gop.value() != val:
                        target.gop.setValue(val)
                finally:
                    self._syncing_common = False

            def sync_export_frames(checked):
                if self._syncing_common:
                    return
                self._syncing_common = True
                try:
                    if target.export_frames.isChecked() != checked:
                        target.export_frames.setChecked(checked)
                        target.update_frame_controls()
                finally:
                    self._syncing_common = False

            def sync_frame_quality(val):
                if self._syncing_common:
                    return
                self._syncing_common = True
                try:
                    if target.frame_quality.value() != val:
                        target.frame_quality.setValue(val)
                        target.update_frame_controls()
                finally:
                    self._syncing_common = False

            source.fps.currentTextChanged.connect(sync_fps)
            source.threads.valueChanged.connect(sync_threads)
            source.gop.valueChanged.connect(sync_gop)
            source.export_frames.toggled.connect(sync_export_frames)
            source.frame_quality.valueChanged.connect(sync_frame_quality)

        make_sync(self.vp9_panel, self.av1_panel)
        make_sync(self.av1_panel, self.vp9_panel)

    def set_codec(self, codec):
        current = self.active_panel()
        if current is not None:
            self._sync_common(current)
        if codec == "av1":
            self.codec_stack.setCurrentWidget(self.av1_panel)
        else:
            self.codec_stack.setCurrentWidget(self.vp9_panel)
        self.on_panel_change()

    def on_panel_change(self):
        self.update_fps_state()
        self.update_summary()
        self.update_output_summary()
        self.update_preset_selection()

    def help_button(self, description, handler):
        button = QPushButton("How to export")
        button.setToolTip(description)
        button.setAccessibleName(description)
        button.clicked.connect(handler)
        return button

    def show_input_help(self):
        QMessageBox.information(self, "Export either input from After Effects",
            "PNG sequence folder with frames\n\n"
            "1. Select your composition, then choose Composition > Add to Render Queue.\n"
            "2. Click the Output Module (the blue text next to \"Output Module\").\n"
            "3. Set Format to \"PNG Sequence\".\n"
            "4. Set Channels to \"RGB\" (not RGB + Alpha). This app does not use alpha, so flatten "
            "any transparency onto a solid background in your comp first.\n"
            "5. Set Depth to \"Millions of Colors\" (8-bit) or, for a 16-bit master, "
            "\"Trillions of Colors\".\n"
            "6. Click OK, set the Output To folder to an empty folder, and render.\n"
            "7. Back here, choose \"PNG sequence folder with frames\" and select that folder. It must "
            "contain only the frames of one sequence, numbered consecutively with no gaps.\n\n"
            "ProRes 4444 movie (single file)\n\n"
            "1. Select your composition, then choose Composition > Add to Render Queue.\n"
            "2. Click the Output Module (the blue text next to \"Output Module\").\n"
            "3. Set Format to \"QuickTime\".\n"
            "4. Click \"Format Options...\" and set the Video Codec to \"Apple ProRes 4444\", then click OK.\n"
            "5. Set Channels to \"RGB\" (not RGB + Alpha). This app rejects movies that carry an "
            "alpha channel, so flatten any transparency onto a solid background in your comp first.\n"
            "6. Set the ProRes 4444 depth to 10-bit or 12-bit.\n"
            "7. Click OK, set the Output To file (a .mov), and render.\n"
            "8. Back here, choose \"ProRes 4444 movie\" and select that .mov file.\n\n"
            "Both: work in an sRGB / Rec.709 project. This app tags output as Rec.709 and does not "
            "convert ICC profiles, so P3 or HDR sources will look wrong.")

    def show_readme(self):
        try:
            contents = readme_text()
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, "README unavailable", f"Could not read the application README.\n{error}")
            return
        if self.readme_dialog is None:
            self.readme_dialog = QDialog(self)
            self.readme_dialog.setWindowTitle("README - PNG to WebM")
            self.readme_dialog.resize(760, 600)
            layout = QVBoxLayout(self.readme_dialog)
            self.readme_view = QPlainTextEdit()
            self.readme_view.setReadOnly(True)
            self.readme_view.setAccessibleName("README contents")
            layout.addWidget(self.readme_view)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.readme_dialog.close)
            layout.addWidget(buttons)
        self.readme_view.setPlainText(contents)
        self.readme_dialog.show()
        self.readme_dialog.raise_()
        self.readme_dialog.activateWindow()

    def choose_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Select a folder of PNG frames")
        if not directory:
            return
        pngs = sorted(path for path in Path(directory).iterdir()
                      if path.is_file() and path.suffix.lower() == ".png")
        if not pngs:
            QMessageBox.warning(self, "No PNG frames found", "The selected folder contains no PNG files.")
            return
        self.set_frames(pngs)

    def choose_movie(self):
        name, _ = QFileDialog.getOpenFileName(
            self, "Select a ProRes 4444 movie", str(Path.home() / "Downloads"),
            "Movie files (*.mov *.MOV);;All files (*)")
        if name:
            self.set_movie(Path(name))

    def set_frames(self, paths):
        try:
            sequence = detect_sequence(paths)
        except ValueError as error:
            QMessageBox.warning(self, "Cannot use selection", str(error))
            return
        self.sequence = sequence
        self.scrubber.setEnabled(True)
        self.scrubber.setRange(0, sequence.count - 1)
        self.scrubber.setValue(0)
        self.show_frame(0)
        self.update_fps_state()
        self.update_summary()
        self.input_path.setText(str(sequence.files[0].parent))
        self.destination.setText(str(sequence.files[0].parent))
        self.set_default_output_names(sequence.stem)
        self.update_output_summary()

    def set_movie(self, path):
        try:
            source = inspect_movie(path)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Cannot use movie", str(error))
            return
        self.sequence = source
        self.scrubber.setRange(0, 0)
        self.scrubber.setValue(0)
        self.scrubber.setEnabled(False)
        self.load_movie_poster(source)
        self.update_fps_state()
        self.update_summary()
        self.input_path.setText(str(source.path))
        self.destination.setText(str(source.path.parent))
        self.set_default_output_names(source.stem)
        self.update_output_summary()

    def load_movie_poster(self, source):
        self.source_pixmap = QPixmap()
        try:
            with tempfile.TemporaryDirectory(prefix="png-webm-poster-") as folder:
                poster = Path(folder) / "poster.png"
                extract_poster(source.path, poster)
                self.source_pixmap = QPixmap(str(poster))
        except (OSError, ValueError, subprocess.SubprocessError):
            self.source_pixmap = QPixmap()
        self.frame_name.setText(f"ProRes 4444 movie: {source.path.name}")
        self.scale_preview()

    def show_frame(self, index):
        if self.sequence is None or isinstance(self.sequence, MovieSource):
            return
        path = self.sequence.files[index]
        self.source_pixmap = QPixmap(str(path))
        self.frame_name.setText(f"{index + 1} / {self.sequence.count}: {path.name}")
        self.scale_preview()

    def scale_preview(self):
        if not self.source_pixmap.isNull():
            self.preview.setPixmap(self.source_pixmap.scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        elif self.sequence:
            self.preview.setText("Preview unavailable")

    def eventFilter(self, watched, event):
        if watched is self.preview and event.type() == QEvent.Type.Resize:
            self.scale_preview()
        return super().eventFilter(watched, event)

    def update_summary(self):
        if self.sequence is None:
            return
        try:
            fps_str = self.sequence.fps if isinstance(self.sequence, MovieSource) else self.active_panel().fps.currentText()
            rate = Fraction(fps_str)
            duration = f"{float(self.sequence.count / rate):.3f} s" if rate > 0 else "Invalid FPS"
        except (ValueError, ZeroDivisionError):
            duration = "Invalid FPS"
        if isinstance(self.sequence, MovieSource):
            self.summary.setText(f"{self.sequence.count} frames | {duration}\n"
                                 f"{self.sequence.width} x {self.sequence.height} px | {self.sequence.depth}-bit RGB\n"
                                 f"ProRes 4444 movie")
        else:
            self.summary.setText(f"{self.sequence.count} frames | {duration}\n"
                                 f"{self.source_pixmap.width()} x {self.source_pixmap.height()} px\n"
                                 f"Sequence {self.sequence.start} to {self.sequence.end}")

    def update_fps_state(self):
        is_movie = isinstance(self.sequence, MovieSource)
        for panel in (self.vp9_panel, self.av1_panel):
            if is_movie:
                panel.fps.setCurrentText(self.sequence.fps)
                panel.fps.setEnabled(False)
                panel.fps.setToolTip(f"Locked to movie frame rate ({self.sequence.fps} fps).")
            else:
                panel.fps.setEnabled(True)
                panel.fps.setToolTip("Frames per second. Fractions such as 24000/1001 are accepted. Every selected frame is encoded once; duration is frame count divided by FPS.")

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Select output folder", self.destination.text())
        if directory:
            self.destination.setText(directory)
            self.update_output_summary()

    def set_default_output_names(self, stem):
        self.webm_name.setText(stem)
        self.first_frame_name.setText(f"{stem}_first_frame")
        self.last_frame_name.setText(f"{stem}_last_frame")

    def output_paths(self, folder, include_frames=True):
        def valid_name(text, label, suffix):
            name = text.strip()
            if not name or name in (".", "..") or Path(name).name != name:
                raise ValueError(f"Enter a valid {label} (no folders or path separators).")
            return f"{name}{suffix}"

        webm = folder / valid_name(self.webm_name.text(), "output file name", ".webm")
        frames = []
        if include_frames:
            frames = [folder / valid_name(self.first_frame_name.text(), "first frame file name", ".webp"),
                      folder / valid_name(self.last_frame_name.text(), "last frame file name", ".webp")]
        return folder, webm, frames

    def update_output_summary(self):
        if not hasattr(self, "output_names_form"):
            return
        export_frames = self.sequence is not None and self.active_panel().export_frames.isChecked()
        self.output_names_form.setRowVisible(self.first_frame_name, export_frames)
        self.output_names_form.setRowVisible(self.last_frame_name, export_frames)

    def settings(self):
        return self.active_panel().settings()

    def load_user_presets(self):
        raw = self.preferences.value("user_presets")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, TypeError):
                pass
        return {}

    def save_user_presets(self):
        self.preferences.setValue("user_presets", json.dumps(self.user_presets))

    def all_presets(self):
        return {**BUILTIN_PRESETS, **self.user_presets}

    def refresh_preset_combo(self, selected=None):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom")
        for name in BUILTIN_PRESETS:
            self.preset_combo.addItem(name)
        for name in sorted(self.user_presets.keys()):
            self.preset_combo.addItem(name)
        if selected is not None:
            index = self.preset_combo.findText(selected)
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
            else:
                self.preset_combo.setCurrentIndex(0)
        else:
            self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        self.update_preset_buttons()

    def on_preset_combo_changed(self, index):
        if self._applying_preset:
            return
        name = self.preset_combo.currentText()
        if name == "Custom":
            self.update_preset_buttons()
            return
        presets = self.all_presets()
        if name in presets:
            self.apply_preset(name)

    def apply_preset(self, name):
        presets = self.all_presets()
        if name not in presets:
            return
        data = presets[name]
        self._applying_preset = True
        try:
            codec = data.get("codec", "vp9")
            if codec == "av1":
                self.av1_button.setChecked(True)
            else:
                self.vp9_button.setChecked(True)
            self.active_panel().apply_standard(data)
            index = self.preset_combo.findText(name)
            if index >= 0 and self.preset_combo.currentIndex() != index:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(index)
                self.preset_combo.blockSignals(False)
        finally:
            self._applying_preset = False
        self.update_preset_buttons()

    def save_current_as_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if not ok or not name or not name.strip():
            return
        name = name.strip()
        if name == "Custom":
            QMessageBox.warning(self, "Invalid preset name", "Cannot use 'Custom' as a preset name.")
            return
        if name in BUILTIN_PRESETS:
            QMessageBox.warning(self, "Cannot overwrite built-in preset",
                                f"'{name}' is a built-in preset and cannot be overwritten.")
            return
        if name in self.user_presets:
            if QMessageBox.question(
                    self, "Overwrite preset?", f"A preset named '{name}' already exists. Overwrite it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        self.user_presets[name] = self.active_panel().standard_settings()
        self.save_user_presets()
        self.refresh_preset_combo(selected=name)

    def rename_preset(self):
        name = self.preset_combo.currentText()
        if name in BUILTIN_PRESETS or name == "Custom" or name not in self.user_presets:
            return
        new_name, ok = QInputDialog.getText(self, "Rename preset", "New preset name:", text=name)
        if not ok or not new_name or not new_name.strip() or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if new_name == "Custom":
            QMessageBox.warning(self, "Invalid preset name", "Cannot use 'Custom' as a preset name.")
            return
        if new_name in BUILTIN_PRESETS:
            QMessageBox.warning(self, "Invalid preset name",
                                f"'{new_name}' is a built-in preset name and cannot be used.")
            return
        if new_name in self.user_presets:
            if QMessageBox.question(
                    self, "Overwrite preset?", f"A preset named '{new_name}' already exists. Overwrite it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        data = self.user_presets.pop(name)
        self.user_presets[new_name] = data
        self.save_user_presets()
        self.refresh_preset_combo(selected=new_name)

    def delete_preset(self):
        name = self.preset_combo.currentText()
        if name in BUILTIN_PRESETS or name == "Custom" or name not in self.user_presets:
            return
        if QMessageBox.question(
                self, "Delete preset", f"Are you sure you want to delete preset '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        del self.user_presets[name]
        self.save_user_presets()
        self.refresh_preset_combo(selected="Custom")

    def update_preset_selection(self):
        if self._applying_preset or not hasattr(self, "preset_combo"):
            return
        current_settings = self.active_panel().standard_settings()
        current_name = self.preset_combo.currentText()
        presets = self.all_presets()
        if current_name in presets and presets[current_name] == current_settings:
            self.update_preset_buttons()
            return
        for name, data in presets.items():
            if data == current_settings:
                index = self.preset_combo.findText(name)
                if index >= 0:
                    self.preset_combo.blockSignals(True)
                    self.preset_combo.setCurrentIndex(index)
                    self.preset_combo.blockSignals(False)
                    self.update_preset_buttons()
                    return
        index = self.preset_combo.findText("Custom")
        if index >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(index)
            self.preset_combo.blockSignals(False)
        self.update_preset_buttons()

    def update_preset_buttons(self):
        if not hasattr(self, "preset_combo"):
            return
        for action in (self.save_preset_action, self.preset_menu_separator,
                       self.rename_preset_action, self.delete_preset_action):
            self.preset_menu.removeAction(action)
        self.preset_menu.addAction(self.save_preset_action)
        if self.preset_combo.currentText() in self.user_presets:
            self.preset_menu.addAction(self.preset_menu_separator)
            self.preset_menu.addAction(self.rename_preset_action)
            self.preset_menu.addAction(self.delete_preset_action)

    def restore_settings(self):
        self.user_presets = self.load_user_presets()
        self.refresh_preset_combo()
        defaults = {"vp9": Settings(), "av1": Settings(codec="av1", crf=30)}
        legacy = self.preferences.value("encoding")
        for panel in (self.vp9_panel, self.av1_panel):
            key = f"encoding_{panel.codec}"
            stored = self.preferences.value(key)
            if stored is None and panel.codec == "vp9" and legacy is not None:
                stored = legacy
            try:
                settings = Settings(**json.loads(stored)) if stored else defaults[panel.codec]
                settings.validate()
            except (ValueError, TypeError):
                settings = defaults[panel.codec]
            panel.apply(settings)
        active = self.preferences.value("codec", "vp9")
        self.av1_button.setChecked(active == "av1")
        self.vp9_button.setChecked(active != "av1")
        self._sync_common(self.active_panel())
        self.update_fps_state()
        self.update_preset_selection()

    def reset_settings(self):
        panel = self.active_panel()
        panel.apply(Settings() if panel.codec == "vp9" else Settings(codec="av1", crf=30))
        self.preferences.remove(f"encoding_{panel.codec}")
        self._sync_common(panel)
        self.update_fps_state()
        self.update_preset_selection()

    def create_export_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Export WebM")
        dialog.setModal(True)
        dialog.setMinimumSize(620, 260)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        self.export_phase = QLabel("Starting export")
        self.export_phase.setWordWrap(True)
        layout.addWidget(self.export_phase)
        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        layout.addWidget(self.export_progress)
        self.export_details = QLabel("")
        self.export_details.setWordWrap(True)
        layout.addWidget(self.export_details)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.export_open = QPushButton("Open output folder")
        self.export_open.setVisible(False)
        self.export_open.clicked.connect(self.open_output)
        buttons.addWidget(self.export_open)
        self.export_done = QPushButton("Done")
        self.export_done.setVisible(False)
        self.export_done.clicked.connect(dialog.close)
        buttons.addWidget(self.export_done)
        self.export_cancel = QPushButton("Cancel")
        self.export_cancel.clicked.connect(self.cancel_export)
        buttons.addWidget(self.export_cancel)
        layout.addLayout(buttons)
        self.export_dialog = dialog

    def start_export(self):
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            if self.sequence is None:
                raise ValueError("Select a folder of PNG frames or a ProRes 4444 movie first.")
            settings = self.settings()
            if isinstance(self.sequence, MovieSource):
                settings = replace(settings, fps=self.sequence.fps)
            settings.validate()
            folder = Path(self.destination.text().strip()).expanduser().absolute()
            if not folder.is_dir():
                raise ValueError("Choose an existing output folder.")
            _, destination, frames = self.output_paths(folder, include_frames=settings.export_frames)
            signature = file_signature(destination)
            if signature is not None and QMessageBox.question(
                    self, "Replace existing file?", f"Replace {destination.name} after a successful export?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Check export settings", str(error))
            return
        self.preferences.setValue(f"encoding_{settings.codec}", json.dumps(asdict(settings)))
        self.preferences.setValue("codec", settings.codec)
        self.last_output = None
        self.create_export_dialog()
        if self.worker is not None:
            self.worker.deleteLater()
        frame_paths = {"first": frames[0], "last": frames[1]} if settings.export_frames else None
        self.worker = ExportWorker(self.sequence, settings, destination, signature, frame_paths, self)
        self.worker.update.connect(self.on_progress)
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.canceled.connect(self.on_canceled)
        self.worker.ready.connect(self.confirm_changed_destination)
        self.worker.finished.connect(self.on_finished)
        self.set_busy(True)
        self.export_dialog.show()
        self.worker.start()

    def set_busy(self, busy):
        for control in (self.inputs, self.vp9_button, self.av1_button, self.destination, self.browse,
                self.webm_name, self.first_frame_name, self.last_frame_name,
                self.reset, self.log_button, self.readme_button, self.export,
                self.preset_combo, self.preset_menu_button):
            control.setEnabled(not busy)
        if not busy:
            self.update_preset_buttons()
        if hasattr(self, "export_cancel"):
            self.export_cancel.setEnabled(busy)
        if not busy:
            self.update_fps_state()

    def on_progress(self, event):
        self.export_phase.setText(event["phase"])
        percent = event.get("percent", 0)
        self.export_progress.setRange(0, 0 if percent < 0 else 100)
        if percent >= 0:
            self.export_progress.setValue(percent)
        if "elapsed" in event:
            detail = f"{event['elapsed']:.0f} s elapsed | Current trial: {event['size'] / 1_000_000:.3f} MB"
            if event.get("best_size") is not None:
                detail += f" | Best fitting: {event['best_size'] / 1_000_000:.3f} MB"
            self.export_details.setText(detail)

    def on_success(self, result):
        self.last_output = Path(result["path"])
        self.export_phase.setText("Export complete and verified" if result.get("target_met", True)
                                  else "Export complete; target exceeded")
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(100)
        self.export_details.setText(f"{result['size'] / 1_000_000:.3f} MB ({result['size']:,} bytes) | "
                                    f"CRF {result['crf']} | {result['trials']} trial(s) | {result['frames']} frames")
        frame_files = result.get("frame_files")
        if frame_files:
            names = ", ".join(Path(path).name for path in frame_files)
            self.export_phase.setText("Export complete and verified; frames saved")
            self.export_details.setText(self.export_details.text() + f" | WebP frames: {names}")
        self.export_cancel.setVisible(False)
        self.export_open.setVisible(True)
        self.export_done.setVisible(True)

    def on_failure(self, message):
        self.export_phase.setText("Export failed; destination unchanged")
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_details.setText(message.splitlines()[0])
        self.export_cancel.setVisible(False)
        self.export_done.setText("Exit")
        self.export_done.setVisible(True)

    def on_canceled(self):
        self.export_phase.setText("Canceled; destination unchanged")
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_details.setText("")
        self.export_dialog.close()

    def on_finished(self):
        self.set_busy(False)
        if self.closing:
            self.close()

    def confirm_changed_destination(self, message, result):
        if self.closing or self.worker.stop.is_set():
            self.worker.approve(False)
            return
        answer = QMessageBox.question(self, "Destination changed", message,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        self.worker.approve(answer == QMessageBox.StandardButton.Yes)

    def cancel_export(self):
        if self.worker is not None:
            self.worker.cancel()
            self.export_cancel.setEnabled(False)
            self.export_phase.setText("Canceling...")

    def show_log(self):
        if not hasattr(self, "log_dialog"):
            self.log_dialog = QDialog(self)
            self.log_dialog.setWindowTitle("Export log")
            self.log_dialog.resize(1000, 700)
            layout = QVBoxLayout(self.log_dialog)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.log_view.setAccessibleName("Export log contents")
            layout.addWidget(self.log_view)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            self.log_export = QPushButton("Export TXT")
            self.log_export.clicked.connect(self.export_log)
            buttons.addButton(self.log_export, QDialogButtonBox.ButtonRole.ActionRole)
            buttons.rejected.connect(self.log_dialog.close)
            layout.addWidget(buttons)
        self.log_view.setPlainText("\n".join(self.worker.log) if self.worker else "No export yet.")
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export log as TXT", "export-log.txt", "Text files (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Export log failed", f"Could not write the log file.\n{error}")

    def open_output(self):
        if self.last_output is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output.parent)))

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if not self.closing and QMessageBox.question(
                    self, "Cancel export and close?", "The current export is still running.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.closing = True
            self.cancel_export()
            event.ignore()
            return
        super().closeEvent(event)