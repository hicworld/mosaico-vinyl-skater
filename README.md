# Mosaico Vinyl Skater · 黑胶轮滑编排台

把黑胶唱片变成一个随音乐运动的小舞台：试听动作、拖到拍点、调整场景，再导出带音乐的动画视频。

![原创合成示例的动画预览](assets/preview.gif)

当前电脑端版本：**1.20.9**。程序使用 Tkinter 编排界面与 C/WASM 渲染器，原始画面为 480 × 480。运行电脑预览无需连接 Mosaico。

## 可以做什么

| 功能 | 用法 |
| --- | --- |
| 预制动作编排 | 52 套可用动作，支持搜索、分级浏览、单独演示与拖放吸附拍点 |
| 音乐驱动 | 导入歌曲，分析节拍、鼓点和音量，生成可继续修改的动作编排 |
| 人物与场景 | 4 个角色、6 个动态场景、4 种手动唱片配色和多种脚边特效 |
| LRC 歌词 | 导入时间轴、逐句显示、开关字幕，按歌词句首切场景 |
| 自动切换 | 支持每两秒、歌词句首、歌词句尾切换，场景与唱片同色系，可同步换人物 |
| 全屏预览 | 按钮、双击或 F11 进入；Esc 返回，空格播放或暂停 |
| 动画导出 | MP4 / H.264 + AAC，整首或指定片段，24 / 30 fps，可选音乐和歌词 |
| 工程保存 | 保存为 `.vskproj`，保留编排、歌词与外观设置，支持撤销和重做 |

动作库登记了 55 个稳定编号，其中 3 个动作停用，界面显示 **52 / 52** 属于正常结果。动作是参考轮滑脚法制作的动漫化表演，适合音乐动画编排。

## Windows 下载使用

不想安装 Python 时，前往 [Releases](https://github.com/hicworld/mosaico-vinyl-skater/releases) 下载 Windows x64 便携包。完整解压后运行 `Mosaico-Vinyl-Skater.exe`，并一起保留 `_internal` 和其他随附文件夹。

下载步骤、数据目录和更新方法见 [Windows 便携版说明](docs/WINDOWS.md)。希望修改源码或自行安装依赖时，继续使用下面的源码运行步骤。

## 先运行示例

主要在 Windows 上使用与验证，建议 Python 3.11。获取源码：

```powershell
git clone https://github.com/hicworld/mosaico-vinyl-skater.git
cd mosaico-vinyl-skater
```

在项目根目录创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe motion_studio.py examples/demo.vskproj
```

也可以使用 Windows 启动器：

```powershell
.\Motion-Studio.ps1 -Setup
.\Motion-Studio.ps1 -Console
```

先打开示例、演示动作、修改编排不需要下载节拍或语音模型。示例使用程序生成的配乐与演示文字，不包含私人歌曲。

### 导入自己的歌曲

导入新音频还需要 FFmpeg 和节拍分析依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
.\.venv\Scripts\python.exe import_music.py prepare-model
```

也可运行 `.\importer\Setup-Importer.ps1`，自动创建项目环境、安装 CPU 分析依赖并准备模型。

让 `ffmpeg` 可以在命令行调用，或将 `VINYL_FFMPEG` 环境变量设为可执行文件路径。首次准备节拍模型需要网络；缓存后可以在本机分析。支持 MP3、WAV、M4A、FLAC、OGG、AAC、WMA。

已有 LRC 可直接导入，不需要语音识别。若需要尝试从人声估算句尾，再按需安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lyrics.txt
```

节拍分析、音频停顿和语音识别都有误差，生成结果可以在时间线上或句尾表中试听、微调。

## 导出视频

点击顶部 **导出动画…**，选择保存位置、时间范围、画面大小和帧率，再开始导出。可选择包含音乐、叠加歌词，过程显示实际帧数、百分比和耗时，也可以取消。

导出使用独立渲染器按固定时间逐帧生成，不录制屏幕；窗口遮挡、界面布局和预览慢帧不会进入视频。支持 480、960、1080 像素方形画面，后两种为原始画面的等比放大。MP4 导出需要 FFmpeg。

## 目录

```text
motion_studio.py           电脑编排台入口
import_music.py            音乐分析与歌曲包命令行入口
studio/                    界面、工程模型、时间线、WASM 运行时和视频导出
importer/                  节拍分析、动作计划与歌曲包处理
native/renderer/           预览使用的 C 渲染源码与 WASI 包装
studio/assets/             已构建的 WASM 与资源清单
examples/                 可直接打开的演示工程和音频
tools/                    演示生成、渲染器构建等开发工具
tests/                    自动化检查
docs/USAGE.md             详细操作说明
```

更详细的安装、编排与导出步骤见 [使用说明](docs/USAGE.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。

## 源码与素材说明

仓库保留电脑端实现与预览渲染源码，不包含设备整机备份、原应用平台、私人歌曲、外部参考视频或模型缓存。当前仓库用于电脑编排与动画导出；设备烧录工程和经过核验的硬件发布不在本仓库范围内。

本仓库目前不附加开源许可证，仅公开项目源码与随附示例。角色、参考来源及第三方依赖的归属见 [NOTICE.md](NOTICE.md)；第三方组件适用各自的许可。自行导入的音乐和歌词请保留来源与使用授权。

GitHub 仓库：[hicworld/mosaico-vinyl-skater](https://github.com/hicworld/mosaico-vinyl-skater)。
