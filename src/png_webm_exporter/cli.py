import argparse
from dataclasses import asdict, fields, replace
import json
from pathlib import Path
import signal
import sys
import threading

from png_webm_exporter.encoding import BUILTIN_PRESETS, Canceled, ExportJob, Settings, file_signature, inspect_movie
from png_webm_exporter.sequence import detect_sequence
from png_webm_exporter.size_search import megabytes_to_bytes

EXIT_FAILED = 1
EXIT_CANCELED = 3


def emit(event: str, **data) -> None:
    sys.stdout.write(json.dumps({"event": event, **data}) + "\n")
    sys.stdout.flush()


def parse_settings(text: str | None) -> Settings:
    if not text:
        return Settings()
    data = json.loads(Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text)
    if not isinstance(data, dict):
        raise ValueError("Settings must be a JSON object.")
    unknown = set(data) - {field.name for field in fields(Settings)}
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}.")
    return Settings(**data)


def presets() -> int:
    emit("presets", presets=BUILTIN_PRESETS,
         defaults={"vp9": asdict(Settings()), "av1": asdict(Settings(codec="av1", crf=30))})
    return 0


def encode(arguments) -> int:
    if arguments.frames:
        folder = Path(arguments.frames).absolute()
        if not folder.is_dir():
            raise ValueError("The frames folder does not exist.")
        source = detect_sequence(sorted(path for path in folder.iterdir() if path.suffix.lower() == ".png"))
    else:
        source = inspect_movie(Path(arguments.movie))
    settings = parse_settings(arguments.settings)
    if arguments.target_mb:
        settings = replace(settings, target_bytes=megabytes_to_bytes(arguments.target_mb))
    if arguments.movie:
        settings = replace(settings, fps=source.fps)
    settings.validate()
    destination = Path(arguments.output).absolute()
    signature = file_signature(destination)
    if signature is not None and not arguments.overwrite:
        raise ValueError(f"{destination.name} already exists. Pass --overwrite to replace it.")
    frame_paths = {}
    if settings.export_frames:
        frame_paths = {
            "first": Path(arguments.first_frame or destination.with_name(f"{destination.stem}_first_frame.webp")),
            "last": Path(arguments.last_frame or destination.with_name(f"{destination.stem}_last_frame.webp")),
        }

    def destination_changed(message, result):
        raise ValueError("The destination changed during export; it was not replaced.")

    job = ExportJob(source, settings, destination, signature, frame_paths,
                    lambda update: emit("progress", **update), destination_changed)
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), lambda *_: job.cancel())
    if arguments.control_stdin:
        def watch_stdin():
            # A closed pipe means the host panel went away, so stop too.
            for line in sys.stdin:
                if line.strip() == "cancel":
                    break
            job.cancel()

        threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        result = job.run()
    except Canceled:
        emit("log", lines=job.log)
        emit("canceled")
        return EXIT_CANCELED
    except Exception:
        emit("log", lines=job.log)
        raise
    emit("log", lines=job.log)
    emit("done", **result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="png-webm-encoder",
                                     description="Headless PNG sequence / ProRes 4444 to WebM encoder.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("presets", help="Print built-in presets and per-codec defaults as JSON.")
    job = commands.add_parser("encode", help="Encode and verify one WebM, printing JSON-lines events.")
    source = job.add_mutually_exclusive_group(required=True)
    source.add_argument("--frames", help="Folder containing one continuous PNG sequence.")
    source.add_argument("--movie", help="QuickTime PNG or ProRes 4444 movie.")
    job.add_argument("--output", required=True, help="Destination .webm file.")
    job.add_argument("--settings", help="Settings JSON object, or @path to a JSON file.")
    job.add_argument("--target-mb", help="Target size in decimal MB; overrides target_bytes.")
    job.add_argument("--first-frame", help="First-frame WebP path when export_frames is on.")
    job.add_argument("--last-frame", help="Last-frame WebP path when export_frames is on.")
    job.add_argument("--overwrite", action="store_true", help="Replace an existing destination.")
    job.add_argument("--control-stdin", action="store_true",
                     help="Cancel when stdin receives 'cancel' or closes.")
    arguments = parser.parse_args(argv)
    try:
        return presets() if arguments.command == "presets" else encode(arguments)
    except Exception as error:
        emit("error", message=str(error))
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
