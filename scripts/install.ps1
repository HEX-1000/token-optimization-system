# Token Optimization System: installer for Windows.
#
# Run from the folder you downloaded (the one that contains README.md):
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Options:
#   -InstallDir <folder>   where to install (default: %USERPROFILE%\Programs\TokenOptimizationSystem)
#   -Python <python.exe>   which Python to use, if the script cannot find one
#
# Safe to run again: program files are replaced and re-checked, your index is kept.

param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE "Programs\TokenOptimizationSystem"),
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$files = @("server.py", "server_d2.py", "indexer.py", "db.py")

function Test-Python([string]$exe) {
    # Native stderr must not stop the script here (Windows PowerShell 5.1), so relax locally.
    $ErrorActionPreference = "Continue"
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try { $ok = & $exe -c "import sys; print(sys.version_info >= (3, 10))" 2>$null } catch { return $false }
    return ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq "True")
}

function Get-PyLauncherPython {
    $ErrorActionPreference = "Continue"
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { return $null }
    try { return (& $py.Source -3 -c "import sys; print(sys.executable)" 2>$null) } catch { return $null }
}

Write-Host "[1/4] Looking for Python 3.10 or newer..."
if (-not $Python) {
    $candidates = @()
    $fromLauncher = Get-PyLauncherPython
    if ($fromLauncher) { $candidates += "$fromLauncher".Trim() }
    $candidates += @(Get-ChildItem -Path "$env:LOCALAPPDATA\Python\pythoncore-3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $Python = $candidates | Where-Object { Test-Python $_ } | Select-Object -First 1
}
if (-not (Test-Python $Python)) {
    throw "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), open a new PowerShell window and run this script again. Or pass -Python <path to python.exe>."
}
Write-Host "      Using $Python"

Write-Host "[2/4] Copying program files to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$sums = @{}
Get-Content (Join-Path $repo "SHA256SUMS") | ForEach-Object {
    $parts = $_ -split "\s+", 2
    if ($parts.Count -eq 2) { $sums[$parts[1].Trim()] = $parts[0].ToLower() }
}
foreach ($f in $files) {
    $src = Join-Path $repo "src\$f"
    $h = (Get-FileHash -Path $src -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $sums[$f]) { throw "$f does not match SHA256SUMS. Download the repository again; do not edit files in src\." }
    Copy-Item -LiteralPath $src -Destination (Join-Path $InstallDir $f) -Force
}
Write-Host "      4 files copied and checked."

Write-Host "[3/4] Creating a private Python environment and installing packages..."
$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $Python -m venv (Join-Path $InstallDir ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $repo "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Package installation failed. Check your internet connection and run the script again." }

Write-Host "[4/4] Checking that the server starts..."
& $venvPy -B -c "import sys; sys.path.insert(0, r'$InstallDir'); import server_d2; print('      ' + server_d2.HEADER_PREFIX + 'ready')"
if ($LASTEXITCODE -ne 0) { throw "The server could not be loaded." }

Write-Host ""
Write-Host "Installed. Next steps:"
Write-Host "  Index a folder:          powershell -ExecutionPolicy Bypass -File scripts\index.ps1 -Folder ""C:\path\to\your\documents"""
Write-Host "  Connect Claude Desktop:  powershell -ExecutionPolicy Bypass -File scripts\connect-claude-desktop.ps1"
