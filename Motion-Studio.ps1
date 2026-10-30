[CmdletBinding(PositionalBinding=$false)]
param(
    [switch]$Setup,
    [switch]$Check,
    [switch]$Console,
    [string]$BasePython = '',
    [Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$StudioArgs
)
$ErrorActionPreference = 'Stop'

function Show-StudioError([string]$Text) {
    if ($Check -or $Console) { Write-Host $Text; return }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Text, 'Mosaico 动作工作台', 'OK', 'Error') | Out-Null
}

function Quote-StudioArgument([string]$Value) {
    $taskQuoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $taskQuoted = [regex]::Replace($taskQuoted, '(\\+)$', '$1$1')
    return '"' + $taskQuoted + '"'
}

try {
    $taskStudioRoot = $PSScriptRoot
    $taskStudioCandidates = @()
    if ($env:MOSAICO_STUDIO_PYTHON) { $taskStudioCandidates += $env:MOSAICO_STUDIO_PYTHON }
    $taskVenvPython = Join-Path $taskStudioRoot '.venv\Scripts\python.exe'
    $taskStudioCandidates += $taskVenvPython
    $taskPathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($taskPathPython -and $taskPathPython.Source -notlike '*\WindowsApps\*') {
        $taskStudioCandidates += $taskPathPython.Source
    }
    $taskStudioPython = $taskStudioCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if ($Setup -and -not (Test-Path -LiteralPath $taskVenvPython)) {
        if (-not $BasePython) {
            $taskStudioBaseCandidates = @()
            $taskPyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($taskPyCommand) {
                $taskPreviousPreference = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    $taskDetected = & $taskPyCommand.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
                    $taskDetectedExit = $LASTEXITCODE
                } finally {
                    $ErrorActionPreference = $taskPreviousPreference
                }
                if ($taskDetectedExit -eq 0 -and $taskDetected) { $taskStudioBaseCandidates += "$taskDetected" }
            }
            $taskStudioCommand = Get-Command python.exe -ErrorAction SilentlyContinue
            if ($taskStudioCommand) { $taskStudioBaseCandidates += $taskStudioCommand.Source }
            $BasePython = $taskStudioBaseCandidates | Where-Object {
                (Test-Path -LiteralPath $_) -and ($_ -notlike '*\WindowsApps\*')
            } | Select-Object -First 1
        }
        if (-not $BasePython -or -not (Test-Path -LiteralPath $BasePython)) {
            throw '未找到 Python。请先安装带 Tkinter 的 64 位 Python 3.11，再用 Motion-Studio.ps1 -Setup -BasePython "你的 python.exe 完整路径"。'
        }
        $taskStudioEnvironment = Join-Path $taskStudioRoot '.venv'
        & $BasePython -m venv $taskStudioEnvironment
        if ($LASTEXITCODE) { throw '创建动作工作台环境失败。' }
        $taskStudioPython = Join-Path $taskStudioEnvironment 'Scripts\python.exe'
    }

    if (-not $taskStudioPython) {
        throw "尚未安装电脑模拟器环境。`n`n请在当前文件夹打开 PowerShell，运行：`n.\Motion-Studio.ps1 -Setup`n`n安装说明见 studio\README.md。"
    }
    if ($Setup) {
        $taskStudioPython = $taskVenvPython
        & $taskStudioPython -m pip install -r (Join-Path $taskStudioRoot 'requirements.txt')
        if ($LASTEXITCODE) { throw '安装模拟器依赖失败，请检查网络后重试。' }
    }

    $taskStudioPreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $taskStudioCheck = & $taskStudioPython -c "import tkinter,numpy,scipy,PIL,wasmtime,sounddevice,serial; print('Mosaico Motion Studio dependencies ready')" 2>&1
    $taskStudioCheckExit = $LASTEXITCODE
    $ErrorActionPreference = $taskStudioPreviousPreference
    if ($taskStudioCheckExit) {
        throw "模拟器依赖不完整。请在当前文件夹打开 PowerShell，运行：`n.\Motion-Studio.ps1 -Setup`n`n$taskStudioCheck"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $taskStudioRoot 'studio\assets\renderer.wasm'))) {
        throw '缺少设备画面模拟资源 studio\assets\renderer.wasm，请重新解压完整发布包。'
    }
    if ($Check) {
        Write-Host "Mosaico 动作工作台已就绪：$taskStudioPython"
        exit 0
    }

    $taskStudioEntry = Join-Path $taskStudioRoot 'motion_studio.py'
    if ($Console) {
        & $taskStudioPython $taskStudioEntry @StudioArgs
        exit $LASTEXITCODE
    }
    $taskStudioPythonw = Join-Path (Split-Path $taskStudioPython -Parent) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $taskStudioPythonw)) { $taskStudioPythonw = $taskStudioPython }
    $taskStudioArguments = @((Quote-StudioArgument $taskStudioEntry))
    if ($StudioArgs) { $taskStudioArguments += @($StudioArgs | ForEach-Object { Quote-StudioArgument $_ }) }
    Start-Process -FilePath $taskStudioPythonw -ArgumentList $taskStudioArguments -WorkingDirectory $taskStudioRoot -WindowStyle Hidden
    exit 0
} catch {
    Show-StudioError $_.Exception.Message
    exit 1
}
