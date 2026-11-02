param([switch]$Console)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    uv sync --frozen --group build
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    $mode = if ($Console) { '--console' } else { '--windowed' }
    uv run --frozen --group build pyinstaller --noconfirm --clean --onedir $mode `
        --name BilibiliDownloader --collect-data imageio_ffmpeg --collect-data bili_downloader `
        --collect-all playwright main_window.py
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
} finally {
    Pop-Location
}
