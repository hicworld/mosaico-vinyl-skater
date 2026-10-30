"""Build the exact C scene implementation as a standalone WASI renderer.

Normal editor use needs only the prebuilt studio/assets/renderer.wasm.
This optional developer command writes to build/, never installs over it.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "renderer"
MANIFEST = SOURCE / "source-manifest.json"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_status(manifest: dict) -> dict:
    result = {}
    for name, expected in manifest["source_sha256"].items():
        path = SOURCE / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing renderer source: {name}")
        actual = sha(path)
        result[name] = {"sha256": actual, "matches_reference": actual == expected}
    return result


def unpack_atlas(manifest: dict, output_directory: Path) -> Path:
    atlas = manifest["atlas"]
    compressed = SOURCE / atlas["file"]
    if sha(compressed) != atlas["compressed_sha256"]:
        raise ValueError("The compressed renderer atlas failed its SHA256 check.")
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / atlas["output"]
    if target.is_file() and target.stat().st_size == atlas["bytes"] and sha(target) == atlas["sha256"]:
        return target
    # The output name is fixed metadata; gzip is streamed as file bytes, never
    # treated as an archive containing paths to extract into the workspace.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="atlas-", suffix=".tmp", dir=output_directory, delete=False) as writer:
            temporary = Path(writer.name)
            digest, size = hashlib.sha256(), 0
            with gzip.open(compressed, "rb") as reader:
                while block := reader.read(1024 * 1024):
                    size += len(block)
                    if size > atlas["bytes"]:
                        raise ValueError("The decompressed renderer atlas is larger than expected.")
                    digest.update(block)
                    writer.write(block)
        if size != atlas["bytes"] or digest.hexdigest() != atlas["sha256"]:
            raise ValueError("The decompressed renderer atlas failed its integrity check.")
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def compiler_from_sdk(value: str | None) -> tuple[Path, Path]:
    if not value:
        raise ValueError("Pass --wasi-sdk PATH or set WASI_SDK_PATH to a WASI SDK installation.")
    sdk = Path(value).expanduser().resolve()
    compiler = sdk / "bin" / ("clang.exe" if os.name == "nt" else "clang")
    sysroot = sdk / "share" / "wasi-sysroot"
    if not compiler.is_file() or not sysroot.is_dir():
        raise ValueError("The SDK must contain bin/clang and share/wasi-sysroot.")
    return compiler, sysroot


def build(arguments=None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasi-sdk", default=os.environ.get("WASI_SDK_PATH"), help="WASI SDK directory")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "renderer" / "renderer.wasm")
    parser.add_argument("--check", action="store_true", help="Validate frozen inputs without requiring a compiler")
    options = parser.parse_args(arguments)
    manifest = json.loads(MANIFEST.read_text(encoding="utf8"))
    sources = source_status(manifest)
    compressed = SOURCE / manifest["atlas"]["file"]
    if not compressed.is_file() or sha(compressed) != manifest["atlas"]["compressed_sha256"]:
        raise ValueError("The compressed renderer atlas is missing or corrupt.")
    changed = [name for name, value in sources.items() if not value["matches_reference"]]
    if options.check:
        if changed:
            raise ValueError("Source files differ from the frozen reference: " + ", ".join(changed))
        print(json.dumps({"passed": True, "source_files": len(sources),
                          "atlas_bytes": compressed.stat().st_size}, indent=2))
        return {"passed": True}
    compiler, sysroot = compiler_from_sdk(options.wasi_sdk)
    destination = options.output.expanduser().resolve()
    reference = ROOT / "studio" / "assets" / "renderer.wasm"
    if destination == reference.resolve():
        raise ValueError("Build to a separate path first; do not overwrite the editor's verified renderer.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    generated = destination.parent / "generated"
    atlas = unpack_atlas(manifest, generated)
    with tempfile.TemporaryDirectory(prefix="renderer-build-", dir=destination.parent) as directory:
        partial = Path(directory) / "renderer.wasm"
        command = [str(compiler), "--target=" + manifest["target"], "--sysroot=" + str(sysroot),
                   "-O2", "-Wall", "-Wextra", "-Werror"]
        command += ["-D" + value for value in manifest["defines"]]
        command += ["-I" + str(SOURCE), "-I" + str(generated)]
        command += [str(SOURCE / name) for name in manifest["source_files"]]
        command += ["-Wl,--no-entry"] + ["-Wl,--export=" + name for name in manifest["exports"]]
        command += ["-lm", "-o", str(partial)]
        print("Building the C/WASI scene renderer…", flush=True)
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf8", errors="replace")
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "WASI compiler failed.")
        if not partial.is_file() or partial.stat().st_size < 8 or partial.read_bytes()[:4] != b"\0asm":
            raise RuntimeError("The compiler did not produce a WebAssembly module.")
        os.replace(partial, destination)
    actual = sha(destination)
    report = {"passed": True, "target": manifest["target"], "output": destination.name,
              "wasm_sha256": actual, "reference_wasm_sha256": manifest["reference_wasm_sha256"],
              "bitwise_identical_to_reference": actual == manifest["reference_wasm_sha256"],
              "atlas_sha256": sha(atlas), "sources": sources, "modified_source_files": changed,
              "editor_renderer_replaced": False,
              "compiler_version": subprocess.check_output([str(compiler), "--version"], text=True).splitlines()[0]}
    (destination.parent / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({key: report[key] for key in ("passed", "wasm_sha256", "bitwise_identical_to_reference", "editor_renderer_replaced")}, indent=2))
    return report


if __name__ == "__main__":
    try:
        build()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Renderer build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
