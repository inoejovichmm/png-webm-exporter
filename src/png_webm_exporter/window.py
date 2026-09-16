from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import tempfile

from PySide6.QtCore import QEvent, Qt, QSettings, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from .encoding import ExportWorker, Settings, extract_poster, file_signature, inspect_movie
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
    "It ranges from 0 to 63.\n\n"
    "Lower CRF usually retains more detail and produces larger files. Higher CRF usually "
    "produces smaller files but can lose detail, smooth away grain/dither, or expose banding. "
    "It is not a percentage or a linear size scale.\n\n")


class CodecPanel(QWidget):
    """Encoder-specific settings for one codec ("vp9" or "av1")."""

    def __init__(self, codec, on_change, parent=None):
        super().__init__(parent)
        self.codec = codec
        self.on_change = on_change
        tabs = QTabWidget(self)
        self.tabs = tabs
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(tabs)
        delivery = QWidget()
        form = QFormLayout(delivery)
        self.delivery_form = form
        form.setVerticalSpacing(16)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.fps = QComboBox()
        self.fps.setEditable(True)
        self.fps.addItems(["25", "24", "24000/1001", "30", "30000/1001", "50", "60"])
        add_field(form, "Frame rate", self.fps, "Frames per second. Fractions such as 24000/1001 are accepted. Every selected frame is encoded once; duration is frame count divided by FPS.")
        self.mode = QComboBox()
        self.mode.addItems(["Manual CRF", "Target size"])
        add_field(form, "Quality mode", self.mode, "Target size tries up to nine complete encodes and keeps the lowest tested CRF that fits. It changes only CRF and forces bitrate to zero. It can take several times longer than a manual export.")
        self.crf = make_spin(0, 63, 12 if codec == "vp9" else 30)
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
        self.export_frames = QCheckBox()
        add_field(form, "Export first and last frames (WebP)", self.export_frames, "After the WebM is verified, also save the first and last frames next to it as <name>_first.webp and <name>_last.webp. Frames are extracted from the finished video and encoded with libwebp.")
        self.frame_quality = make_spin(0, 100, 90)
        self.frame_quality_label = add_field(form, "Frame WebP quality", self.frame_quality, "libwebp quality passed as -q:v, from 0 (smallest) to 100. Setting 100 switches libwebp into lossless mode instead of using the quality scale.")
        tabs.addTab(delivery, "Delivery")
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        advanced_form.setVerticalSpacing(12)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        advanced_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if codec == "vp9":
            self.bitrate = make_spin(0, 1_000_000, 0)
            self.bitrate.setSuffix(" kb/s")
            add_field(advanced_form, "Bitrate", self.bitrate, "Zero selects constant-quality encoding. A nonzero bitrate constrains quality using libvpx rate control; it is not an exact size cap. Target size always uses zero.")
            self.alt_ref = QCheckBox()
            add_field(advanced_form, "Alternate reference frames", self.alt_ref, "Allows hidden alternate reference frames. This can improve compression but temporal filtering can change fine grain. Default off.")
            self.arnr = make_spin(0, 15, 0)
            add_field(advanced_form, "Temporal filter frames", self.arnr, "Alternate-reference temporal filtering window, 0-15 frames. Zero disables filtering. Relevant when alternate reference frames are enabled.")
            self.aq = QComboBox()
            self.aq.addItems(["0 - Off", "1 - Variance", "2 - Complexity", "3 - Cyclic refresh", "4 - Equator360"])
            add_field(advanced_form, "Adaptive quantization", self.aq, "Redistributes quantization across the image. Off preserves the baseline behavior; other modes can change gradients and grain. Equator360 is for 360-degree video.")
            self.row_mt = QCheckBox()
            self.row_mt.setChecked(True)
            add_field(advanced_form, "Row multithreading", self.row_mt, "Parallel row encoding, where supported by libvpx. Default on.")
            self.tiles = make_spin(0, 6, 2)
            add_field(advanced_form, "Tile columns (log2)", self.tiles, "Requested power-of-two tile columns: 0 = 1, 1 = 2, 2 = 4. libvpx limits this according to frame width. More tiles can trade compression efficiency for parallelism.")
        else:
            self.preset = make_spin(0, 13, 8)
            add_field(advanced_form, "Encoder preset", self.preset, "SVT-AV1 speed/quality preset, 0 (slowest, best compression) to 13 (fastest). Lower is slower but smaller for the same quality. 8 is a balanced default.")
            self.fgs_denoise = QCheckBox()
            add_field(advanced_form, "Film grain denoise source", self.fgs_denoise, "Denoise the source before analyzing grain for synthesis. Leave off for already-clean renders; turn on only when the source itself carries grain you want removed and re-synthesized.")
        self.threads = make_spin(0, 256, 8)
        self.threads.setSpecialValueText("Auto")
        add_field(advanced_form, "Threads", self.threads, "Maximum encoder threads. Zero lets the encoder choose. More threads do not guarantee faster encoding.")
        self.gop = make_spin(0, 1_000_000, 0)
        self.gop.setSpecialValueText("Auto")
        add_field(advanced_form, "Max keyframe distance", self.gop, "Maximum frames between keyframes. Zero leaves the encoder default. Shorter distances improve seeking but can increase file size.")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(advanced)
        tabs.addTab(scroll, "Advanced")
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.mode.currentIndexChanged.connect(lambda *_: self.on_change())
        self.fps.currentTextChanged.connect(lambda *_: self.on_change())
        self.export_frames.toggled.connect(self.update_frame_controls)
        self.frame_quality.valueChanged.connect(self.update_frame_controls)
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
                "AV1 (SVT-AV1): CRF 0 is not lossless with this 8-bit 4:2:0 conversion. With film "
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
            self.fgs_enabled.setChecked(settings.fgs_enabled)
            self.fgs_level.setValue(settings.fgs_level)
            self.fgs_denoise.setChecked(settings.fgs_denoise)
            self.update_fgs_controls()
        self.update_mode()
        self.update_frame_controls()


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
        self.setWindowTitle("PNG to WebM")
        self.resize(960, 760)
        self.setMinimumSize(720, 640)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        heading = QLabel("PNG to WebM")
        heading.setStyleSheet("font-size: 24px; font-weight: 600;")
        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch()
        self.readme_button = QPushButton("README")
        self.readme_button.setToolTip("Read the application README")
        self.readme_button.clicked.connect(self.show_readme)
        header.addWidget(self.readme_button)
        layout.addLayout(header)
        self.inputs = QWidget()
        columns = QHBoxLayout(self.inputs)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(24)
        source = QVBoxLayout()
        select_row = QHBoxLayout()
        self.choose_input = QPushButton("Select input...")
        self.choose_input.setToolTip("Choose a PNG sequence folder with frames or a single QuickTime PNG movie.")
        input_menu = QMenu(self.choose_input)
        folder_action = QAction("PNG sequence folder with frames", self.choose_input)
        folder_action.triggered.connect(self.choose_folder)
        movie_action = QAction("QuickTime PNG", self.choose_input)
        movie_action.triggered.connect(self.choose_movie)
        input_menu.addAction(folder_action)
        input_menu.addAction(movie_action)
        self.choose_input.setMenu(input_menu)
        select_row.addWidget(self.choose_input, 1)
        self.input_help = self.help_button("How to export either input from After Effects",
                                            self.show_input_help)
        select_row.addWidget(self.input_help)
        source.addLayout(select_row)
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
        self.color_confirm = QCheckBox("I confirm my PNGs are standard SDR (Rec.709) with transparency already flattened")
        self.color_confirm.setToolTip("The exporter does not color-manage: it tags output as Rec.709 and does not convert ICC profiles, so P3/HDR sources will look wrong. Alpha is discarded, so any transparency must already be flattened onto a background. Output is 8-bit YUV 4:2:0 (VP9 Profile 0 or AV1 Main), no audio. VP9 adds no dither; AV1 can synthesize film grain at playback when film grain synthesis is enabled.")
        source.addWidget(self.color_confirm)
        source.addStretch()
        columns.addLayout(source, 1)
        self.codec_tabs = QTabWidget()
        self.vp9_panel = CodecPanel("vp9", self.on_panel_change)
        self.av1_panel = CodecPanel("av1", self.on_panel_change)
        self.codec_tabs.addTab(self.vp9_panel, "VP9")
        self.codec_tabs.addTab(self.av1_panel, "AV1")
        self.codec_tabs.currentChanged.connect(lambda *_: self.on_panel_change())
        columns.addWidget(self.codec_tabs, 1)
        layout.addWidget(self.inputs, 1)
        output_row = QHBoxLayout()
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Output folder")
        self.destination.setToolTip("Destination folder. The output file name is derived from the frame name pattern. Existing files are replaced only after successful verification and your confirmation.")
        self.browse = QPushButton("Browse\u2026")
        self.browse.setToolTip("Choose output folder")
        self.browse.setAccessibleName("Choose output folder")
        self.browse.clicked.connect(self.choose_output)
        output_row.addWidget(self.destination, 1)
        output_row.addWidget(self.browse)
        layout.addLayout(output_row)
        self.output_name = QLabel("")
        self.output_name.setWordWrap(True)
        self.output_name.setStyleSheet("color: #9aa6a2;")
        layout.addWidget(self.output_name)
        self.phase = QLabel("Ready")
        self.phase.setWordWrap(True)
        layout.addWidget(self.phase)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        buttons = QHBoxLayout()
        self.reset = QPushButton("Reset settings")
        self.reset.clicked.connect(self.reset_settings)
        buttons.addWidget(self.reset)
        self.log_button = QPushButton("Log")
        self.log_button.clicked.connect(self.show_log)
        buttons.addWidget(self.log_button)
        self.open_button = QPushButton("Open output folder")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_output)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        self.cancel = QPushButton("Cancel")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_export)
        buttons.addWidget(self.cancel)
        self.export = QPushButton("Export WebM")
        self.export.setDefault(True)
        self.export.clicked.connect(self.start_export)
        buttons.addWidget(self.export)
        layout.addLayout(buttons)
        self.destination.textChanged.connect(self.update_output_name)
        self.restore_settings()
        self.update_output_name()

    def active_panel(self):
        return self.codec_tabs.currentWidget()

    def on_panel_change(self):
        self.update_summary()
        self.update_output_name()

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
            "QuickTime PNG movie (single file)\n\n"
            "1. Select your composition, then choose Composition > Add to Render Queue.\n"
            "2. Click the Output Module (the blue text next to \"Output Module\").\n"
            "3. Set Format to \"QuickTime\".\n"
            "4. Click \"Format Options...\" and set the Video Codec to \"PNG\", then click OK.\n"
            "5. Set Channels to \"RGB\" (not RGB + Alpha). This app rejects movies that carry an "
            "alpha channel, so flatten any transparency onto a solid background in your comp first.\n"
            "6. Set Depth to \"Millions of Colors\" (8-bit) or, for a 16-bit master, "
            "\"Trillions of Colors\".\n"
            "7. Click OK, set the Output To file (a .mov), and render.\n"
            "8. Back here, choose \"QuickTime PNG\" and select that .mov file.\n\n"
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
        name, _ = QFileDialog.getOpenFileName(self, "Select a QuickTime PNG movie", "",
                                              "QuickTime PNG movie (*.mov *.MOV)")
        if name:
            self.set_movie(Path(name))

    def set_frames(self, paths):
        try:
            sequence = detect_sequence(paths)
        except ValueError as error:
            QMessageBox.warning(self, "Cannot use selection", str(error))
            return
        self.sequence = sequence
        self.color_confirm.setChecked(False)
        self.scrubber.setEnabled(True)
        self.scrubber.setRange(0, sequence.count - 1)
        self.scrubber.setValue(0)
        self.show_frame(0)
        self.update_summary()
        if not self.destination.text():
            self.destination.setText(str(sequence.files[0].parent))
        self.update_output_name()

    def set_movie(self, path):
        try:
            source = inspect_movie(path)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Cannot use movie", str(error))
            return
        self.sequence = source
        self.color_confirm.setChecked(False)
        self.scrubber.setRange(0, 0)
        self.scrubber.setValue(0)
        self.scrubber.setEnabled(False)
        self.load_movie_poster(source)
        self.update_summary()
        if not self.destination.text():
            self.destination.setText(str(source.path.parent))
        self.update_output_name()

    def load_movie_poster(self, source):
        self.source_pixmap = QPixmap()
        try:
            with tempfile.TemporaryDirectory(prefix="png-webm-poster-") as folder:
                poster = Path(folder) / "poster.png"
                extract_poster(source.path, poster)
                self.source_pixmap = QPixmap(str(poster))
        except (OSError, ValueError):
            self.source_pixmap = QPixmap()
        self.frame_name.setText(f"QuickTime PNG movie: {source.path.name}")
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
            rate = Fraction(self.active_panel().fps.currentText())
            duration = f"{float(self.sequence.count / rate):.3f} s" if rate > 0 else "Invalid FPS"
        except (ValueError, ZeroDivisionError):
            duration = "Invalid FPS"
        if isinstance(self.sequence, MovieSource):
            self.summary.setText(f"{self.sequence.count} frames | {duration}\n"
                                 f"{self.sequence.width} x {self.sequence.height} px | {self.sequence.depth}-bit RGB\n"
                                 f"QuickTime PNG movie")
        else:
            self.summary.setText(f"{self.sequence.count} frames | {duration}\n"
                                 f"{self.source_pixmap.width()} x {self.source_pixmap.height()} px\n"
                                 f"Sequence {self.sequence.start} to {self.sequence.end}")

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Select output folder", self.destination.text())
        if directory:
            self.destination.setText(directory)
            self.update_output_name()

    def output_paths(self):
        folder = Path(self.destination.text().strip()).expanduser().absolute()
        webm = folder / f"{self.sequence.stem}.webm"
        frames = [webm.with_name(f"{webm.stem}_{position}.webp") for position in ("first", "last")]
        return folder, webm, frames

    def update_output_name(self):
        if self.sequence is None or not self.destination.text().strip():
            self.output_name.setText("")
            return
        _, webm, frames = self.output_paths()
        text = f"Output file: {webm.name}"
        if self.active_panel().export_frames.isChecked():
            text += " | WebP frames: " + ", ".join(frame.name for frame in frames)
        self.output_name.setText(text)

    def settings(self):
        return self.active_panel().settings()

    def restore_settings(self):
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
        self.codec_tabs.setCurrentIndex(1 if active == "av1" else 0)

    def reset_settings(self):
        panel = self.active_panel()
        panel.apply(Settings() if panel.codec == "vp9" else Settings(codec="av1", crf=30))
        self.preferences.remove(f"encoding_{panel.codec}")

    def start_export(self):
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            if self.sequence is None:
                raise ValueError("Select a folder of PNG frames or a QuickTime PNG movie first.")
            if not self.color_confirm.isChecked():
                raise ValueError("Confirm the source color space and flattened transparency before exporting.")
            settings = self.settings()
            settings.validate()
            folder = Path(self.destination.text().strip()).expanduser().absolute()
            if not folder.is_dir():
                raise ValueError("Choose an existing output folder.")
            destination = folder / f"{self.sequence.stem}.webm"
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
        self.open_button.setEnabled(False)
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = ExportWorker(self.sequence, settings, destination, signature, self)
        self.worker.update.connect(self.on_progress)
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.canceled.connect(self.on_canceled)
        self.worker.ready.connect(self.confirm_changed_destination)
        self.worker.finished.connect(self.on_finished)
        self.set_busy(True)
        self.phase.setText("Starting export")
        self.details.setText("")
        self.worker.start()

    def set_busy(self, busy):
        for control in (self.inputs, self.codec_tabs, self.destination, self.browse, self.reset, self.export):
            control.setEnabled(not busy)
        self.cancel.setEnabled(busy)
        self.log_button.setEnabled(not busy)

    def on_progress(self, event):
        self.phase.setText(event["phase"])
        percent = event.get("percent", 0)
        self.progress.setRange(0, 0 if percent < 0 else 100)
        if percent >= 0:
            self.progress.setValue(percent)
        if "elapsed" in event:
            detail = f"{event['elapsed']:.0f} s elapsed | Current trial: {event['size'] / 1_000_000:.3f} MB"
            if event.get("best_size") is not None:
                detail += f" | Best fitting: {event['best_size'] / 1_000_000:.3f} MB"
            self.details.setText(detail)

    def on_success(self, result):
        self.last_output = Path(result["path"])
        self.phase.setText("Export complete and verified" if result.get("target_met", True)
                           else "Export complete; target exceeded")
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.details.setText(f"{result['size'] / 1_000_000:.3f} MB ({result['size']:,} bytes) | "
                             f"CRF {result['crf']} | {result['trials']} trial(s) | {result['frames']} frames")
        frame_files = result.get("frame_files")
        if frame_files:
            names = ", ".join(Path(path).name for path in frame_files)
            self.phase.setText("Export complete and verified; frames saved")
            self.details.setText(self.details.text() + f" | WebP frames: {names}")
        self.open_button.setEnabled(True)

    def on_failure(self, message):
        self.phase.setText("Export failed; destination unchanged")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.details.setText(message.splitlines()[0])
        if not self.closing:
            dialog = QMessageBox(QMessageBox.Icon.Warning, "Export failed", message.splitlines()[0], parent=self)
            dialog.setDetailedText(message + "\n\n" + "\n".join(self.worker.log))
            dialog.exec()

    def on_canceled(self):
        self.phase.setText("Canceled; destination unchanged")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.details.setText("")

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
            self.cancel.setEnabled(False)
            self.phase.setText("Canceling...")

    def show_log(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Export log")
        dialog.setText("FFmpeg / ffprobe")
        dialog.setDetailedText("\n".join(self.worker.log) if self.worker else "No export yet.")
        dialog.exec()

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