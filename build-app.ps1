param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$ToolsDir = Join-Path $ProjectDir '.tools'
$YtDlpExe = Join-Path $ToolsDir 'yt-dlp.exe'
$DistExe = Join-Path $ProjectDir 'dist\X Space Recorder.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    & (Join-Path $ProjectDir 'setup.ps1') -InstallFfmpeg
}

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp.exe' -OutFile $YtDlpExe
& $VenvPython -m pip install --upgrade pyinstaller

Push-Location $ProjectDir
try {
    & $VenvPython -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name 'X Space Recorder' `
        --add-binary "$YtDlpExe;." `
        gui.py
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $DistExe)) {
    throw 'アプリのビルドに失敗しました。'
}

if ($Install) {
    $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\X Space Recorder'
    $InstalledExe = Join-Path $InstallDir 'X Space Recorder.exe'
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $DistExe -Destination $InstalledExe -Force

    $Desktop = [Environment]::GetFolderPath('Desktop')
    $ShortcutPath = Join-Path $Desktop 'X Space Recorder.lnk'
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $InstalledExe
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Description = 'X Spaceを音声ファイルとして録音'
    $Shortcut.Save()
    Write-Host "インストール先: $InstalledExe"
    Write-Host "ショートカット: $ShortcutPath"
}

Write-Host "ビルド完了: $DistExe"
