param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Url,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RecorderArgs
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw '未セットアップです。先に .\setup.ps1 を実行してください。'
}

& $VenvPython (Join-Path $ProjectDir 'recorder.py') $Url @RecorderArgs
exit $LASTEXITCODE
