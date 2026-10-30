"""Desktop / CLI entry point for reusable beat-synchronised Mosaico songs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "projects" / "analysed"


def main():
    parser = argparse.ArgumentParser(description="Mosaico 歌曲节拍分析与独立播放导入")
    sub = parser.add_subparsers(dest="command")
    analyse = sub.add_parser("analyse", help="分析歌曲，生成动作计划和设备歌曲包")
    analyse.add_argument("file", type=Path)
    analyse.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    analyse.add_argument("--title")
    analyse.add_argument("--bpm", type=float, help="手动 BPM；不填写则用预训练模型")
    analyse.add_argument("--offset", type=float, default=0., help="手动模式的第一拍位置，秒")
    analyse.add_argument("--upload", action="store_true")
    analyse.add_argument("--port")
    analyse.add_argument("--expected-mac", help="上传目标设备的 MAC；也可设置 MOSAICO_EXPECTED_DEVICE_MAC")
    upload = sub.add_parser("upload", help="把已分析的歌曲包导入 Mosaico")
    upload.add_argument("package", type=Path)
    upload.add_argument("--port")
    upload.add_argument("--expected-mac", help="上传目标设备的 MAC；也可设置 MOSAICO_EXPECTED_DEVICE_MAC")
    builtin = sub.add_parser("builtin", help="重新生成仓库的原创演示工程")
    builtin.add_argument("--output", type=Path, default=ROOT / "examples")
    preload = sub.add_parser("prepare-model", help="首次下载并缓存 small0，之后离线分析")
    args = parser.parse_args()
    try:
        if not args.command:
            from importer.gui import launch
            launch(DEFAULT_OUTPUT)
        elif args.command == "prepare-model":
            from importer.analysis import configure_model_cache
            configure_model_cache()
            from beat_this.inference import File2Beats
            File2Beats(checkpoint_path="small0", device="cpu", dbn=False)
            print("Beat This! small0 ready for offline analysis.")
        elif args.command == "analyse":
            from importer.workflow import analyse_song
            result = analyse_song(args.file, args.output, args.bpm, args.offset, args.title, print)
            if args.upload:
                from importer.upload import upload_package
                result["upload"] = upload_package(Path(result["package"]["path"]), args.port, print, expected_mac=args.expected_mac)
            print(json.dumps({k: v for k, v in result.items() if k not in ("analysis", "plan")}, ensure_ascii=False, indent=2))
        elif args.command == "upload":
            from importer.upload import upload_package
            print(json.dumps(upload_package(args.package, args.port, print, expected_mac=args.expected_mac), ensure_ascii=False, indent=2))
        elif args.command == "builtin":
            import subprocess
            subprocess.run([sys.executable, str(ROOT / "tools" / "create_demo.py"),
                            "--output-dir", str(args.output)], check=True)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
