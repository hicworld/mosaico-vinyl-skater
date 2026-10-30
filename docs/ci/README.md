# 自动检查模板

`tests.yml` 是供维护者按需启用的 GitHub Actions 检查模板。当前项目不自动安装工作流；本机可直接执行：

```powershell
python tools/build_renderer.py --check
python tools/create_demo.py --verify
python -m unittest discover -s tests -v
```

维护者具有工作流管理权限时，可将模板放入 `.github/workflows/tests.yml` 后提交。模板使用 Windows 和 Python 3.11，安装基础依赖，不下载节拍或语音模型；没有 FFmpeg 时跳过实际 MP4 编码测试。
