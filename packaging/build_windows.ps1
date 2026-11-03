# Builds the portable Windows zip. Run from the repository root on Windows
# (GitHub Actions windows-latest). Needs Python 3.11+, Chocolatey.
param([string]$Version = "dev")
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install . pyinstaller

# --- bundled tools: Tesseract (eng) and ffmpeg ---------------------------
choco install tesseract -y --no-progress | Out-Null
choco install ffmpeg -y --no-progress | Out-Null
$tessDir = "C:\Program Files\Tesseract-OCR"
if (-not (Test-Path "$tessDir\tesseract.exe")) { throw "tesseract not found in $tessDir" }
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
# choco puts a shim on PATH; resolve the real binary
$real = Get-ChildItem "C:\ProgramData\chocolatey\lib\ffmpeg" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($real) { $ffmpeg = $real.FullName }

# --- freeze ----------------------------------------------------------------
Push-Location packaging
pyinstaller behsard.spec --noconfirm --distpath ..\dist --workpath ..\build
Pop-Location

$out = "dist\Behsard"
New-Item -ItemType Directory -Force "$out\tools\tesseract\tessdata" | Out-Null
New-Item -ItemType Directory -Force "$out\tools\ffmpeg" | Out-Null
Copy-Item "$tessDir\*.exe", "$tessDir\*.dll" "$out\tools\tesseract\"
Copy-Item "$tessDir\tessdata\eng.traineddata" "$out\tools\tesseract\tessdata\"
if (Test-Path "$tessDir\tessdata\osd.traineddata") { Copy-Item "$tessDir\tessdata\osd.traineddata" "$out\tools\tesseract\tessdata\" }
Copy-Item $ffmpeg "$out\tools\ffmpeg\ffmpeg.exe"
Copy-Item packaging\start.bat "$out\start.bat"
Copy-Item README.fa.md "$out\README.fa.md"
Copy-Item docs\field-test.fa.md "$out\field-test.fa.md" -ErrorAction SilentlyContinue
Set-Content "$out\VERSION.txt" $Version

# smoke check of the frozen app
& "$out\tools\tesseract\tesseract.exe" --version
if ($LASTEXITCODE -ne 0) { throw "bundled tesseract does not run" }

$zip = "dist\Behsard-windows-x64-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $out -DestinationPath $zip
Write-Host "built $zip"
