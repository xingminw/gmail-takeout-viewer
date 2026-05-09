param(
  [string]$PythonVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$runtimeDir = Join-Path $projectRoot "runtime\python-windows-x64"
$downloadDir = Join-Path $projectRoot "runtime\downloads"
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $downloadDir $zipName
$url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

if (-not (Test-Path $zipPath)) {
  Write-Host "Downloading $url"
  Invoke-WebRequest -Uri $url -OutFile $zipPath
}

Write-Host "Extracting portable Python to $runtimeDir"
Expand-Archive -Path $zipPath -DestinationPath $runtimeDir -Force

$pth = Get-ChildItem -Path $runtimeDir -Filter "python*._pth" | Select-Object -First 1
if ($pth) {
  $content = Get-Content -Path $pth.FullName
  $content = $content | ForEach-Object {
    if ($_ -eq "#import site") { "import site" } else { $_ }
  }
  Set-Content -Path $pth.FullName -Value $content -Encoding ASCII
}

$python = Join-Path $runtimeDir "python.exe"
& $python -B -c "import sys, sqlite3, email; print(sys.version); print(sqlite3.sqlite_version)"

Write-Host ""
Write-Host "Portable Python is ready."
Write-Host "Use start_portable.bat to launch the viewer without system Python."
