[CmdletBinding(PositionalBinding=$false)]
param([Parameter(Position=0, ValueFromRemainingArguments=$true)][string[]]$ImporterArgs)
$ErrorActionPreference = 'Stop'
$taskImportCandidates = @()
if ($env:MOSAICO_STUDIO_PYTHON) { $taskImportCandidates += $env:MOSAICO_STUDIO_PYTHON }
$taskImportCandidates += Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskPathPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($taskPathPython -and $taskPathPython.Source -notlike '*\WindowsApps\*') { $taskImportCandidates += $taskPathPython.Source }
$taskImportPython = $taskImportCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $taskImportPython) {
    throw '缺少歌曲导入环境。请先运行 importer/Setup-Importer.ps1。'
}
& $taskImportPython (Join-Path $PSScriptRoot 'import_music.py') @ImporterArgs
exit $LASTEXITCODE
