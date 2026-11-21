# Build the Windows download: dist\MajimaVpet-<version>-windows.zip (+ its SHA-256)
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root 'program\venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'program\venv not found. Set it up first (see README, "Setup (Windows)").' }

$version = (& $py -c "import sys; sys.path.insert(0, 'program'); from version import APP_VERSION; print(APP_VERSION)").Trim()
Write-Host "Building MajimaVpet $version for Windows"

& $py -m pip install --quiet -r program\requirements.txt -r packaging\requirements-build.txt
if ($LASTEXITCODE) { throw 'pip install failed' }

& $py packaging\make_icon.py build\MajimaVpet.ico
if ($LASTEXITCODE) { throw 'icon failed' }

$pyiArgs = @(
    '-m', 'PyInstaller', "$root\program\vpet_win.py",
    '--name', 'MajimaVpet',
    '--onedir', '--windowed', '--noconfirm', '--clean',
    '--icon', "$root\build\MajimaVpet.ico",
    '--paths', "$root\program",
    '--distpath', "$root\build\dist", '--workpath', "$root\build\work", '--specpath', "$root\build",
    '--add-data', "$root\fonts;fonts"
)
foreach ($gif in Get-ChildItem "$root\majima-*.gif") { $pyiArgs += @('--add-data', "$($gif.FullName);.") }
foreach ($mod in 'QtNetwork', 'QtQml', 'QtQuick', 'QtPdf', 'QtSvg', 'QtOpenGL', 'QtDBus') {
    $pyiArgs += @('--exclude-module', "PySide6.$mod")  # not used: keeps the download smaller
}
& $py @pyiArgs
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

$app = 'build\dist\MajimaVpet'
Copy-Item 'packaging\READ ME FIRST.txt' $app

New-Item -ItemType Directory -Force dist | Out-Null
$zip = "dist\MajimaVpet-$version-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$hash  $(Split-Path -Leaf $zip)" | Set-Content -Encoding ascii "$zip.sha256"

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Done: $zip ($mb MB)"
Write-Host "SHA-256: $hash"
