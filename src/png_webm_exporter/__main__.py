import sys
import json
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from png_webm_exporter.window import ExportWindow


def smoke_test(application, window, directory):
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(4):
        path = directory / f"sample_{index:04d}.png"
        image = Image.new("RGB", (512, 320))
        image.putdata([(24 + column // 5, 40 + row // 3, 90 + (column + index * 10) // 5)
                       for row in range(320) for column in range(512)])
        image.save(path)
        paths.append(path)
    window.set_frames(paths)
    window.color_confirm.setChecked(True)
    panel = window.active_panel()
    panel.mode.setCurrentIndex(1)
    panel.target.setText("0.05")
    window.destination.setText(str(directory))
    window.on_failure = lambda message: window.details.setText(message)

    def finish():
        report = {"success": window.last_output is not None, "phase": window.phase.text(),
                  "details": window.details.text(), "frozen": bool(getattr(sys, "frozen", False))}
        if window.last_output:
            report["bytes"] = window.last_output.stat().st_size
        for name, width, height, tab in (("delivery", 960, 760, 0), ("compact", 720, 640, 0),
                                          ("advanced", 960, 760, 1)):
            window.resize(width, height)
            window.active_panel().tabs.setCurrentIndex(tab)
            application.processEvents()
            window.grab().save(str(directory / f"{name}.png"))
        window.codec_tabs.setCurrentWidget(window.av1_panel)
        window.av1_panel.tabs.setCurrentIndex(0)
        application.processEvents()
        window.grab().save(str(directory / "av1.png"))
        report["av1_fgs_default"] = window.av1_panel.settings().fgs_enabled
        window.codec_tabs.setCurrentWidget(window.vp9_panel)
        window.vp9_panel.tabs.setCurrentIndex(0)
        window.resize(720, 640)
        panel = window.active_panel()
        panel.mode.setCurrentIndex(0)
        application.processEvents()
        report["manual_target_hidden"] = (not panel.target.isVisible()
                                          and not panel.delivery_form.labelForField(panel.target).isVisible())
        window.grab().save(str(directory / "manual.png"))
        window.readme_button.click()
        application.processEvents()
        report["readme_loaded"] = (window.readme_dialog is not None
                                    and "Initial CRF" in window.readme_view.toPlainText())
        if window.readme_dialog is not None:
            window.readme_dialog.grab().save(str(directory / "readme.png"))
            window.readme_dialog.close()
        report["success"] = report["success"] and report["manual_target_hidden"] and report["readme_loaded"]
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        application.exit(0 if report["success"] else 1)

    def start():
        window.start_export()
        if window.worker is None:
            finish()
        else:
            window.worker.finished.connect(finish)
            QTimer.singleShot(90_000, window.cancel_export)

    QTimer.singleShot(0, start)


def main():
    application = QApplication(sys.argv)
    application.setOrganizationName("PNGWebMExporter")
    application.setApplicationName("PNG to WebM")
    smoke_directory = None
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        smoke_directory = Path(sys.argv[2]).absolute()
        if (smoke_directory / "smoke.webm").exists():
            raise SystemExit("Choose a fresh smoke-test directory.")
    preferences = QSettings(str(smoke_directory / "settings.ini"), QSettings.Format.IniFormat) if smoke_directory else None
    window = ExportWindow(preferences)
    window.show()
    if smoke_directory:
        smoke_test(application, window, smoke_directory)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())