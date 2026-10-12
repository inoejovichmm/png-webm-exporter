from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal

from .encoding import Canceled, ExportJob, Settings
from .sequence import MovieSource, Sequence


class ExportWorker(QThread):
    update = Signal(dict)
    succeeded = Signal(dict)
    failed = Signal(str)
    canceled = Signal()
    ready = Signal(str, dict)

    def __init__(self, source: Sequence | MovieSource, settings: Settings, destination: Path,
                 signature, frame_paths: dict[str, Path] | None = None, parent=None):
        super().__init__(parent)
        self.decision = threading.Event()
        self.approved = False
        self.job = ExportJob(source, settings, destination, signature, frame_paths,
                             self.update.emit, self.await_decision)

    @property
    def stop(self) -> threading.Event:
        return self.job.stop

    @property
    def log(self) -> list[str]:
        return self.job.log

    def execute(self, program: Path, arguments: list[str], on_line=None) -> str:
        return self.job.execute(program, arguments, on_line)

    def cancel(self) -> None:
        self.job.cancel()
        self.decision.set()

    def approve(self, approved: bool) -> None:
        self.approved = approved
        self.decision.set()

    def await_decision(self, message: str, result: dict) -> bool:
        self.ready.emit(message, result)
        self.decision.wait()
        return self.approved

    def run(self) -> None:
        try:
            self.succeeded.emit(self.job.run())
        except Canceled:
            self.canceled.emit()
        except Exception as error:
            self.failed.emit(str(error))
