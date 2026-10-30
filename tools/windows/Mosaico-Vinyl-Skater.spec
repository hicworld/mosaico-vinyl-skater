# PyInstaller one-folder distribution; build resources are prepared separately.
from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parents[1]
resources = Path(os.environ.get("MOSAICO_BUILD_RESOURCES", root / "build/windows-release/resources"))
datas = [(str(root / "studio/assets"), "studio/assets"),
         (str(root / "examples"), "examples"),
         (str(resources / "resource-manifest.json"), ".")]
for folder in ("bin", "models", "licenses"):
    if not (resources / folder).is_dir():
        raise RuntimeError("Prepare build resources before packaging: " + folder)
    datas.append((str(resources / folder), folder))
binaries = []
hidden = ["studio.frozen_check", "studio.lyric_recognition", "tiktoken_ext.openai_public"]
for name in ("wasmtime", "beat_this", "whisper", "tiktoken_ext", "soxr", "einops"):
    datas += collect_data_files(name)
    binaries += collect_dynamic_libs(name)
    hidden += collect_submodules(name, filter=lambda module: ".tests" not in module and ".test_" not in module)
for name in ("torchaudio", "llvmlite", "_sounddevice_data", "_soundfile_data"):
    binaries += collect_dynamic_libs(name)
for name in ("torch", "torchaudio", "beat-this", "openai-whisper", "numpy", "scipy",
             "soundfile", "tiktoken", "numba", "llvmlite", "einops"):
    datas += copy_metadata(name)
a = Analysis([str(root / "motion_studio.py")], pathex=[str(root)],
    binaries=binaries, datas=datas, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "IPython", "notebook", "jupyter", "tensorflow", "tensorboard", "pandas", "matplotlib",
              "onnxruntime", "onnx", "onnxscript", "torch.testing._internal", "torch.utils.benchmark"],
    noarchive=False, module_collection_mode={"whisper": "pyz+py"})
# Torch's developer headers/static .lib archives are not runtime dependencies.
a.datas = [entry for entry in a.datas if not entry[0].lower().endswith((".lib", ".h", ".hpp"))
           and not entry[0].replace("\\", "/").startswith("torch/include/")]
a.binaries = [entry for entry in a.binaries if "libportaudio" not in entry[0].lower()
              or entry[0].replace("\\", "/").split("/")[-1] == "libportaudio64bit.dll"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Mosaico-Vinyl-Skater",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Mosaico-Vinyl-Skater")
