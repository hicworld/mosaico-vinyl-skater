"""Create a Windows portable ZIP and SHA256SUMS from a tested onedir build."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def package(dist_dir, output_dir, version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a numeric major.minor.patch version")
    dist, output = Path(dist_dir).resolve(), Path(output_dir).resolve()
    if not (dist / "Mosaico-Vinyl-Skater.exe").is_file() or not (dist / "_internal").is_dir():
        raise ValueError("Select the complete PyInstaller onedir application")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"Mosaico-Vinyl-Skater-{version}-windows-x64.zip"
    if destination.exists():
        raise FileExistsError("Preserve existing release assets; choose a new output directory")
    readme = ("黑胶轮滑编排台 Windows x64 便携版\n\n"
              "请完整解压整个文件夹，再双击 Mosaico-Vinyl-Skater.exe。\n"
              "保留 _internal 文件夹，不需要安装 Python。\n"
              "歌曲、LRC、全屏和MP4导出均在程序中操作。\n"
              "工程、导出和日志默认保存在程序旁的 projects、exports、logs；\n"
              "目录不可写时保存在 LocalAppData/MosaicoVinylSkater。\n\n"
              "项目：https://github.com/hicworld/mosaico-vinyl-skater\n"
              "说明：WINDOWS.md；第三方组件许可位于 _internal/licenses。\n")
    prefix = "Mosaico-Vinyl-Skater"
    with tempfile.NamedTemporaryFile(prefix="mosaico-release-", suffix=".zip", dir=output, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(dist / "Mosaico-Vinyl-Skater.exe", f"{prefix}/Mosaico-Vinyl-Skater.exe")
            for path in sorted((dist / "_internal").rglob("*")):
                if path.is_file():
                    archive.write(path, f"{prefix}/{path.relative_to(dist).as_posix()}")
            for path, name in ((ROOT / "docs/WINDOWS.md", "WINDOWS.md"),
                               (ROOT / "docs/USAGE.md", "USAGE.md"),
                               (ROOT / "NOTICE.md", "NOTICE.md")):
                content = path.read_text(encoding="utf8").replace("(../NOTICE.md)", "(NOTICE.md)")
                archive.writestr(f"{prefix}/{name}", content.encode("utf8"))
            archive.writestr(f"{prefix}/开始使用.txt", readme.encode("utf-8-sig"))
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError("ZIP verification failed: " + bad)
            members = len(archive.infolist())
            expanded = sum(item.file_size for item in archive.infolist())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    with destination.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    summary = {"version": version, "asset": destination.name, "bytes": destination.stat().st_size,
               "expanded_bytes": expanded, "members": members, "sha256": digest}
    (output / "package-info.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", default="1.20.9")
    arguments = parser.parse_args()
    print(json.dumps(package(arguments.dist_dir, arguments.output_dir, arguments.version)))
