# Windows 便携版构建

面向 Windows x64、Python 3.11 的 PyInstaller `onedir` 构建。使用发行包不需要 Python；本页供构建者使用。

1. 在构建环境安装主仓库的基础、CPU 节拍分析和可选歌词识别依赖，再安装 `requirements-build.txt`。
2. 使用 `prepare_resources.py --help` 指定已有 FFmpeg、small0 checkpoint、Whisper base 权重及资源输出目录。脚本收集组件许可，不使用私人的歌曲和项目。
3. 设置 `MOSAICO_BUILD_RESOURCES` 为资源目录，执行构建：

```powershell
python -m PyInstaller --noconfirm --clean `
  --distpath build/windows-release/dist `
  --workpath build/windows-release/work `
  tools/windows/Mosaico-Vinyl-Skater.spec
```

4. 验证独立 EXE：

```powershell
build/windows-release/dist/Mosaico-Vinyl-Skater/Mosaico-Vinyl-Skater.exe --self-test build/windows-release/self-test.json
```

自检不打开音频或串口设备。默认检查真实歌曲导入、内置节拍模型、歌词识别子进程、工程保存和 MP4 编码；报告写入传入的 JSON。还需实际查看界面并试听播放。可临时设置 `MOSAICO_SELF_TEST_LEVEL=basic` 只排查基础运行库。

5. 仅对验收通过的完整目录打包：

```powershell
python tools/windows/package_release.py `
  --dist-dir build/windows-release/dist/Mosaico-Vinyl-Skater `
  --output-dir build/windows-release/release-assets --version 1.20.9
```

输出便携 ZIP、SHA256 校验值和相对文件名清单。测试用工程、日志、模型缓存和构建机绝对路径不会作为发布包根目录附件加入。依赖本身的许可和内置模型保留在 `_internal/licenses` 与 `_internal/models`。
