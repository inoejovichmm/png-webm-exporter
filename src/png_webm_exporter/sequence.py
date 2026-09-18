from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Sequence:
    files: tuple[Path, ...]
    pattern: str
    start: int
    end: int
    stem: str = "output"

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class MovieSource:
    path: Path
    count: int
    width: int
    height: int
    depth: int
    stem: str = "output"
    fps: str = "25"


def clean_stem(text: str) -> str:
    stem = Path(text).stem.strip(" _-.#[](){}")
    return stem or "output"


def detect_sequence(paths: list[Path]) -> Sequence:
    if not paths:
        raise ValueError("Select the PNG frames first.")
    files = tuple(Path(path).absolute() for path in paths)
    if len(set(files)) != len(files):
        raise ValueError("The selection contains duplicate files.")
    if len({path.parent for path in files}) != 1:
        raise ValueError("Select one sequence from a single folder.")
    if any(path.suffix.lower() != ".png" for path in files):
        raise ValueError("Only PNG frames are supported.")
    if any(not path.is_file() for path in files):
        raise ValueError("One or more selected frames no longer exist.")
    if len(files) == 1:
        return Sequence(files, str(files[0]), 0, 0, clean_stem(files[0].name))
    candidates = []
    gap = None
    for token in re.finditer(r"[0-9]+", files[0].name):
        prefix = files[0].name[:token.start()]
        suffix = files[0].name[token.end():]
        matcher = re.compile(re.escape(prefix) + r"([0-9]+)" + re.escape(suffix))
        matches = [matcher.fullmatch(path.name) for path in files]
        if not all(matches):
            continue
        digits = [match.group(1) for match in matches if match]
        indices = [int(value) for value in digits]
        if len(set(indices)) != len(files):
            continue
        padded = any(len(value) > 1 and value.startswith("0") for value in digits)
        width = min(len(value) for value in digits) if padded else 0
        if any(value != (str(index).zfill(width) if padded else str(index))
               for value, index in zip(digits, indices)):
            continue
        ordered = sorted(zip(indices, files))
        start, end = ordered[0][0], ordered[-1][0]
        if end - start + 1 != len(files):
            gap = next(left + 1 for (left, _), (right, _) in
                       zip(ordered, ordered[1:]) if right != left + 1)
            continue
        field = f"%0{width}d" if padded else "%d"
        directory = str(files[0].parent).replace("%", "%%")
        pattern = str(Path(directory) / (prefix.replace("%", "%%") + field +
                                        suffix.replace("%", "%%")))
        candidates.append(Sequence(tuple(path for _, path in ordered), pattern, start, end,
                                   clean_stem(prefix + suffix)))
    if not candidates and gap is not None:
        raise ValueError(f"Frame {gap} is missing. Select a continuous sequence.")
    if len(candidates) != 1:
        raise ValueError("Cannot identify one frame-number pattern. Select consistently named frames with unique indices.")
    return candidates[0]
