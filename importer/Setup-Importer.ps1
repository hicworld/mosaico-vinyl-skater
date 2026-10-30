param([string]$BasePython='', [switch]$SkipModelDownload)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path $PSScriptRoot -Parent
$taskParameters = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $taskRepo 'Motion-Studio.ps1'), '-Setup', '-Check')
if ($BasePython) { $taskParameters += @('-BasePython', $BasePython) }
& powershell.exe @taskParameters
if ($LASTEXITCODE) { throw '创建工作台运行环境失败。' }
$taskImportEnv = Join-Path $taskRepo '.venv'
$taskImportPython = Join-Path $taskImportEnv 'Scripts\python.exe'
& $taskImportPython -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE) { throw '安装 CPU 音频分析依赖失败。' }
& $taskImportPython -m pip install -r (Join-Path $taskRepo 'requirements-analysis.txt')
if ($LASTEXITCODE) { throw '安装歌曲导入依赖失败。' }
if (-not $SkipModelDownload) {
    & $taskImportPython (Join-Path $taskRepo 'import_music.py') prepare-model
    if ($LASTEXITCODE) { throw '缓存预训练模型失败；可稍后重新运行 prepare-model。' }
}
