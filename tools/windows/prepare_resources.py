"""Collect local Windows release resources without downloading models.

Run with the Python environment used to build the desktop application. The
generated manifest contains bundle-relative paths, versions, sizes and hashes;
it deliberately omits the build machine's source paths.
"""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGES = (
    "numpy", "scipy", "Pillow", "wasmtime", "sounddevice", "pyserial",
    "torch", "torchaudio", "beat-this", "soundfile", "openai-whisper",
    "packaging", "setuptools",
)
LICENSE_WORDS = ("license", "licence", "copying", "notice", "copyright")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record(path: Path, output: Path) -> dict:
    return {"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
            "sha256": digest(path)}


def copy_resource(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Required build input is missing: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if (not target.is_file() or target.stat().st_size != source.stat().st_size
            or digest(target) != digest(source)):
        shutil.copy2(source, target)


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_relative(value) -> Path:
    # RECORD entries may use parent components to refer to an installed
    # script. Their names are useful for provenance, but never as output paths.
    parts = str(value).replace("\\", "/").split("/")
    if str(value).replace("\\", "/").startswith("/") or any(":" in part for part in parts):
        raise ValueError("Distribution license paths must be relative.")
    return Path(*(p for p in parts if p not in ("", ".", "..")))


def runtime_distributions(names):
    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise RuntimeError("Install the build dependency 'packaging' first.") from exc
    queue, seen, result = deque(names), set(), []
    while queue:
        name = queue.popleft()
        distribution = metadata.distribution(name)
        canonical = _name(distribution.metadata["Name"])
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(distribution)
        for requirement in distribution.requires or ():
            dependency = Requirement(requirement)
            if dependency.marker is None or dependency.marker.evaluate({"extra": ""}):
                queue.append(dependency.name)
    return result


def collect_distribution(distribution, output: Path, *, kind="python-package") -> dict:
    name = distribution.metadata["Name"]
    folder = output / "licenses" / "python-packages" / _name(name)
    copied = []
    for item in distribution.files or ():
        if not any(word in str(item).lower() for word in LICENSE_WORDS):
            continue
        source = Path(distribution.locate_file(item))
        if not source.is_file():
            continue
        target = folder / _safe_relative(item)
        copy_resource(source, target)
        copied.append(record(target, output))
    if not copied:
        override = ROOT / "tools/windows/license-overrides" / (_name(name) + "-LICENSE.txt")
        if override.is_file():
            target = folder / "LICENSE.txt"
            copy_resource(override, target)
            copied.append(record(target, output))
    if not copied:
        raise RuntimeError(f"No complete license text found for {name}; provide a license override.")
    return {"component": name, "version": distribution.version, "kind": kind,
            "license_expression": distribution.metadata.get("License-Expression"),
            "files": copied}


def collect_python_licenses(output: Path) -> dict:
    bases = [Path(sys.base_prefix), Path(sys.prefix)]
    candidates = []
    for base in bases:
        for name in ("LICENSE_PYTHON.txt", "LICENSE.txt", "LICENSE"):
            candidate = base / name
            if candidate.is_file():
                candidates.append((candidate, "Python-" + name))
                break
        for library in (base / "tcl", base / "Library/lib"):
            if library.exists():
                for pattern in ("tcl*/license.terms", "tk*/license.terms"):
                    for source in library.glob(pattern):
                        candidates.append((source, source.parent.name + "-license.terms"))
    copied, hashes = [], set()
    for source, filename in candidates:
        sha = digest(source)
        if sha in hashes:
            continue
        hashes.add(sha)
        target = output / "licenses/python-runtime" / filename
        copy_resource(source, target)
        copied.append(record(target, output))
    if not copied:
        raise RuntimeError("The Python runtime license could not be located.")
    return {"component": "Python and bundled Tk runtime", "version": sys.version.split()[0],
            "kind": "python-runtime", "files": copied}


def collect_native_licenses(output: Path) -> list[dict]:
    """Make the native media-library notices visible independently of bindings.

    Some wheels list their native license in RECORD under a package-data
    directory rather than dist-info. Keep both that original relative location
    in the Python component and a clearly named native-library component.
    """
    components = []
    for name, distribution_name, prefix, license_expression in (
        ("libsndfile", "soundfile", "_soundfile_data/", "LGPL-2.1-or-later"),
        ("portaudio", "sounddevice", "_sounddevice_data/portaudio-binaries/", "MIT"),
    ):
        distribution = metadata.distribution(distribution_name)
        copied = []
        for item in distribution.files or ():
            relative = str(item).replace("\\", "/")
            native_license = (relative.startswith(prefix)
                              and any(word in relative.lower() for word in LICENSE_WORDS))
            native_readme = relative == prefix + "README.md"
            soundfile_notes = name == "libsndfile" and relative == "licensing/license_notes.md"
            if not (native_license or native_readme or soundfile_notes):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            target = output / "licenses/native-libraries" / name / source.name
            copy_resource(source, target)
            copied.append(record(target, output))
        override = ROOT / "tools/windows/license-overrides" / (name + "-LICENSE.txt")
        if override.is_file():
            target = output / "licenses/native-libraries" / name / "LICENSE.txt"
            copy_resource(override, target)
            copied.append(record(target, output))
        if copied:
            components.append({"component": name, "version": None,
                "bundled_by": distribution_name, "bundled_by_version": distribution.version,
                "kind": "native-library", "license_expression": license_expression,
                "files": copied})
    return components


def collect(ffmpeg_dir: Path, beat_model: Path, whisper_model: Path, output: Path,
            extra_distribution_paths=()) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for folder in ("bin", "models", "licenses"):
        (output / folder).mkdir(exist_ok=True)
    executable = ffmpeg_dir / "bin/ffmpeg.exe"
    if not executable.is_file():
        executable = ffmpeg_dir / "ffmpeg.exe"
    copy_resource(executable, output / "bin/ffmpeg.exe")
    copy_resource(beat_model, output / "models/beat-this-small0.ckpt")
    copy_resource(whisper_model, output / "models/whisper-base/base.pt")
    version_output = subprocess.check_output([str(executable), "-version"], text=True,
                    encoding="utf8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = version_output.splitlines()[0]
    if "--enable-nonfree" in version_output:
        raise RuntimeError("A nonfree FFmpeg build cannot be prepared for redistribution.")
    gpl, version3 = "--enable-gpl" in version_output, "--enable-version3" in version_output
    ffmpeg_license = ("GPL-3.0-or-later" if version3 else "GPL-2.0-or-later") if gpl else (
        "LGPL-3.0-or-later" if version3 else "LGPL-2.1-or-later")
    ffmpeg_files = [record(output / "bin/ffmpeg.exe", output)]
    for filename in ("LICENSE", "README.txt"):
        source = ffmpeg_dir / filename
        if not source.is_file():
            source = ffmpeg_dir.parent / filename
        target = output / "licenses/ffmpeg" / filename
        copy_resource(source, target)
        ffmpeg_files.append(record(target, output))
    components = [{"component": "FFmpeg", "version": version, "kind": "external-executable",
                   "license_expression": ffmpeg_license, "files": ffmpeg_files}]
    components.append(collect_python_licenses(output))
    for distribution in runtime_distributions(DEFAULT_PACKAGES):
        components.append(collect_distribution(distribution, output))
    components.extend(collect_native_licenses(output))
    for folder in extra_distribution_paths:
        for distribution in metadata.distributions(path=[str(folder)]):
            if _name(distribution.metadata["Name"]) == "pyinstaller":
                components.append(collect_distribution(distribution, output, kind="build-tool-bootloader"))
    model_files = [record(output / "models/beat-this-small0.ckpt", output),
                   record(output / "models/whisper-base/base.pt", output)]
    notice = output / "licenses/THIRD-PARTY-RESOURCES.md"
    notice.write_text("""# Third-party resources in the Windows portable build

The application does not grant a new license over bundled third-party software.
Complete upstream license and notice texts are retained in this directory.

## Beat This! and Whisper

The Beat This! small0 checkpoint is the official pretrained checkpoint published
by CPJKU / the Institute of Computational Perception, JKU Linz. The Beat This!
software's MIT license is included under `python-packages/beat-this`.
Upstream: https://github.com/CPJKU/beat_this
Checkpoint: https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/small0.ckpt

The Whisper multilingual base checkpoint is published by OpenAI. The Whisper
software and model weights are released under the MIT license; its text is
included under `python-packages/openai-whisper`.
Upstream: https://github.com/openai/whisper

## FFmpeg source availability

`ffmpeg/README.txt` identifies the unmodified Gyan Windows build, its exact FFmpeg
source commit, build configuration and upstream source-code URL. `ffmpeg/LICENSE`
contains the upstream license text. The executable remains a separate
command-line program; it is invoked to decode audio and encode video.

The corresponding source version and build information can be obtained using
the source-code link in the original build README. The original supplier's build
page is https://www.gyan.dev/ffmpeg/builds/ . Source for FFmpeg and incorporated
third-party libraries remains governed by their respective upstream licenses.

`resource-manifest.json` records component versions and SHA256 checksums using
bundle-relative paths. It contains no paths from the build machine.
""", encoding="utf8")
    manifest = {"format": 1, "bundle": "Mosaico Vinyl Skater Windows resources",
                "components": components, "models": model_files,
                "notice": record(notice, output)}
    (output / "resource-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg-dir", type=Path, required=True)
    parser.add_argument("--beat-model", type=Path, required=True)
    parser.add_argument("--whisper-model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/windows-release/resources")
    parser.add_argument("--extra-distribution-path", type=Path, action="append", default=[])
    args = parser.parse_args()
    manifest = collect(args.ffmpeg_dir, args.beat_model, args.whisper_model,
                       args.output_dir, args.extra_distribution_path)
    print(json.dumps({"ready": True, "components": len(manifest["components"]),
                      "model_bytes": sum(item["bytes"] for item in manifest["models"])}))


if __name__ == "__main__":
    main()
