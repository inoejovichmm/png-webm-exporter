import os
from pathlib import Path
import shutil
import sys


def readme_text() -> str:
    if getattr(sys, "frozen", False):
        path = Path(getattr(sys, "_MEIPASS")) / "application-source" / "README.txt"
    else:
        path = Path(__file__).resolve().parents[2] / "README.txt"
    return path.read_text(encoding="utf-8")


def binary_path(name: str) -> Path:
    filename = name + (".exe" if sys.platform == "win32" else "")
    if getattr(sys, "frozen", False):
        path = Path(getattr(sys, "_MEIPASS")) / "bin" / filename
        if path.is_file():
            return path
        raise FileNotFoundError(f"Bundled {name} is missing. Reinstall the application.")
    override = os.environ.get(f"PNG_WEBM_{name.upper()}")
    path = Path(override) if override else Path(shutil.which(name) or filename)
    if path.is_file():
        return path.absolute()
    raise FileNotFoundError(f"Development setup: install {name} or set PNG_WEBM_{name.upper()}. Packaged builds include it.")
