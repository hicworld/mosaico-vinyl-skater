# C / WASI 预览渲染器源码

这里是电脑编排台实际使用的场景与动作渲染实现，来自角色惯性摆动版本的源码冻结。电脑编排台的后续界面、歌词和视频导出功能继续使用同一份渲染器。

包括动作计算、歌曲包解析、唱片与背景、人物网格、脚边特效、环境动画和 WASI 调用包装。这里没有 ESP-IDF 工程、板卡 BSP、USB 下载工具、应用平台或设备备份，不能直接用它烧录 Mosaico。

普通使用不需要编译。仓库中的 `studio/assets/renderer.wasm` 已可运行，Python 加载它时会核对随附清单中的 SHA256。

## 可选：重建 WASM

需要 Python 3.11+ 和 WASI SDK。通过参数或 `WASI_SDK_PATH` 指定 SDK 目录，构建脚本不依赖作者电脑上的安装位置。

```powershell
python tools/build_renderer.py --check
python tools/build_renderer.py --wasi-sdk "你的 WASI SDK 目录"
```

也可以指定输出位置：

```powershell
python tools/build_renderer.py --wasi-sdk "你的 WASI SDK 目录" --output build/renderer/custom.wasm
```

默认结果是 `build/renderer/renderer.wasm`，同时生成 `build-report.json`。构建不会替换编辑器正在使用的 WASM，也不会更新它的运行清单。修改渲染代码后应先验证自己的输出，再成套更新编辑器的资源和 SHA256。

构建选项与冻结版本一致：`wasm32-wasip1`、`-O2`、电脑端连续人物插值、524288 字节场景及人物缓存。模块文件名、SDK 版本和工具链变化可能影响生成文件的哈希；报告会记录与随附 WASM 是否逐字节一致，不把编译成功视为视觉完全一致。

## 为什么有一个压缩头文件

`adaptive_scene.c` 使用生成的图集数据 `adaptive_art.h`。原始 C 数组文本约 89 MB，仓库以无损的 `assets/adaptive_art.h.gz` 保存，约 21 MB。构建时先验证压缩文件哈希，再流式解压到 `build/renderer/generated/`，验证原始字节数和哈希，之后才编译。这个文件不是从网络下载的素材包，也不包含可执行的解压脚本。

无需提交解压后的头文件。旧版不再使用的 `art_frames.h`、构建缓存、调试画面和设备固件均不在本目录内。

## 源码清单

`source-manifest.json` 保存本目录的相对文件名、冻结源码哈希、图集压缩前后哈希、WASI 导出函数和参考 WASM 哈希，不包含作者的私人文件路径。`--check` 可检查这份原始冻结；有意修改源码后，正常构建仍可进行，并把修改过的文件记入构建报告。

人物由源码中的几何与骨架绘制，没有附带外部角色模型、字体安装文件、参考动画视频或用户歌曲。角色和项目授权范围请同时阅读仓库的资源说明；渲染器源码清单不授予任何第三方角色权利。

## 已完成的重建检查

随附 `rebuild-validation.json` 记录了实际编译检查：20 个冻结输入一致；重建与预编译 WASM 的可执行段和数据段逐字节一致，仅自定义名称节中的模块文件名不同，因此整文件哈希不同。另对 6 个场景、4 位人物、3 个时间点共 72 帧进行逐像素比较，差异为 0。重建产物只保存在忽略的 `build/` 目录，没有替换编辑器资源。
