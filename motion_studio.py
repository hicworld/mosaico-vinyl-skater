"""Launch the desktop motion studio; no board is needed for preview."""
from pathlib import Path
import sys
import traceback

if __name__ == "__main__":
    try:
        from studio.app import main
        main()
    except Exception:
        error = traceback.format_exc()
        log = Path(__file__).with_name("motion-studio-error.log")
        log.write_text(error, encoding="utf8")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("动作工作台启动失败", f"{error}\n错误日志：{log}")
        finally:
            sys.exit(1)
