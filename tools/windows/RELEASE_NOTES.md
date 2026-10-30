# v1.20.9 · Windows x64 便携版

黑胶轮滑编排台的首个 Windows 免 Python 发行包。

## 下载使用

在本页 **Assets** 下载 `Mosaico-Vinyl-Skater-1.20.9-windows-x64.zip`，完整解压后运行 **`Mosaico-Vinyl-Skater.exe`**。请一起保留 `_internal` 和其他随附文件夹。

使用发行包无需安装 Python 或运行源码安装命令。程序默认打开原创合成示例，可直接试听动作、编辑时间线与导出动画。

## 主要功能

- 52 套可用动作，支持单独演示、拖放吸附拍点与编排保存。
- 歌曲节拍、鼓点与音量分析，以及可继续修改的自动动作计划。
- 4 个角色、6 个动态场景、唱片配色与脚边特效。
- LRC 歌词显示与开关，按歌词句首／句尾或每两秒自动切场景。
- 全屏预览：F11／双击进入，Esc 返回，空格播放／暂停。
- MP4 动画导出：音乐与歌词可选，整首或指定片段，480／960／1080 方形画面，24／30 fps。

## 包含内容

完整发行包包含运行时、渲染器、原创示例、FFmpeg、节拍模型与人声识别模型。具体组件版本和 SHA256 见随附清单。

程序默认在 EXE 同目录保存 `projects`、`exports` 和 `logs`。如果该目录不能写入，使用 `%LOCALAPPDATA%/MosaicoVinylSkater/`。可用 `MOSAICO_DATA_DIR` 显式指定可写的数据目录。

## 使用说明与来源

- [Windows 使用说明](https://github.com/hicworld/mosaico-vinyl-skater/blob/main/docs/WINDOWS.md)
- [完整操作说明](https://github.com/hicworld/mosaico-vinyl-skater/blob/main/docs/USAGE.md)
- [来源与权利说明](https://github.com/hicworld/mosaico-vinyl-skater/blob/main/NOTICE.md)

这是电脑端编排与动画导出应用。原生渲染画面为 480 × 480，960／1080 为等比放大。本项目暂未附加开源许可证，第三方组件适用各自的许可。


## 发布检查

已通过 37 项源码检查，并在清除本机 Python、模型缓存和 FFmpeg 路径影响的环境中验证独立 EXE：中文与空格路径、原始示例、六场景渲染、工程保存与 LRC、真实 MP3 节拍分析、内置 Whisper 子进程以及 H.264/AAC 视频导出。

随包附带组件许可和版本清单；本页另提供 SHA256 校验文件。
