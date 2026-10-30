"""Read-only application resources and writable desktop user data.

PyInstaller's resource directory must never be used as the output workspace.
The portable Windows build prefers its EXE directory; protected install
locations transparently use the current user's Local AppData instead.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)


def _writable_directory(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Permission bits are insufficient on Windows ACLs and network drives.
        with tempfile.TemporaryFile(prefix=".mosaico-write-", dir=directory):
            pass
        return True
    except OSError:
        return False


def data_root() -> Path:
    override = os.environ.get("MOSAICO_DATA_DIR")
    if override:
        directory = Path(os.path.expandvars(override)).expanduser().resolve()
        if not _writable_directory(directory):
            raise OSError("MOSAICO_DATA_DIR 指定的目录不可写，请更换数据目录。")
        return directory
    if not is_frozen():
        return resource_root()
    portable = Path(sys.executable).resolve().parent
    if _writable_directory(portable):
        return portable
    local = os.environ.get("LOCALAPPDATA")
    directory = (Path(local) if local else Path.home() / "AppData" / "Local") / "MosaicoVinylSkater"
    if not _writable_directory(directory):
        raise OSError("程序目录和用户数据目录均不可写，请解压到可写目录后运行。")
    return directory


def log_path() -> Path:
    folder = data_root() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "motion-studio-error.log"


def lyric_worker_command(input_path: Path, output_path: Path, allow_download: bool) -> list[str]:
    arguments = [str(input_path), str(output_path), "1" if allow_download else "0"]
    if is_frozen():
        return [sys.executable, "--lyric-worker", *arguments]
    return [sys.executable, "-m", "studio.lyric_recognition", "--worker", *arguments]
