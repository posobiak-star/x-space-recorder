param(
    [switch]$InstallFfmpeg
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectDir '.venv')
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install --upgrade --pre 'yt-dlp[default,curl-cffi]'

if ($InstallFfmpeg -and -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget が見つかりません。FFmpegを手動でインストールしてPATHへ追加してください。'
    }
    winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
    Write-Host 'FFmpegをインストールしました。新しいターミナルを開いてください。'
}

Write-Host ''
Write-Host 'セットアップ完了。例:'
Write-Host '.\record.ps1 https://x.com/i/spaces/SPACE_ID --browser chrome'
