"""Launch the desktop motion studio; no board is needed for preview."""
from pathlib import Path
import os
import sys
import tempfile
import traceback


def _configure_frozen_environment():
    from studio.paths import is_frozen, resource_path
    if is_frozen():
        binary = resource_path("bin")
        if binary.is_dir():
            os.environ["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")


def _record_error(error):
    try:
        from studio.paths import log_path
        log = log_path()
        log.write_text(error, encoding="utf8")
        return log
    except Exception:
        # Startup may itself fail because both normal output locations are
        # protected. Keep a diagnostic without writing to bundled resources.
        try:
            descriptor, filename = tempfile.mkstemp(prefix="mosaico-startup-", suffix=".log")
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                stream.write(error)
            return Path(filename)
        except OSError:
            return None


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    headless = bool(arguments and arguments[0] in ("--lyric-worker", "--self-test"))
    try:
        _configure_frozen_environment()
        if arguments[:1] == ["--lyric-worker"]:
            # This must be dispatched before importing/building the Tk editor.
            # A frozen EXE cannot invoke itself with Python's ``-m`` option.
            from studio.lyric_recognition import _worker_main
            _worker_main(["--worker", *arguments[1:]])
            return 0
        if arguments[:1] == ["--self-test"]:
            if len(arguments) != 2:
                raise ValueError("--self-test requires a report JSON path")
            from studio.frozen_check import main as check
            return check(Path(arguments[1])) or 0
        from studio.app import main as launch
        previous = sys.argv
        try:
            sys.argv = [previous[0], *arguments]
            launch()
        finally:
            sys.argv = previous
        return 0
    except Exception:
        error = traceback.format_exc()
        log = _record_error(error)
        if headless:
            if sys.stderr is not None:
                print(error, file=sys.stderr)
            return 1
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("动作工作台启动失败", f"{error}\n错误日志：{log or '无法写入'}")
            root.destroy()
        finally:
            return 1


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(main())
