# Token Optimization System: index a folder so Claude can search it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\index.ps1 -Folder "C:\path\to\documents"
#
# Run it again after files change: only new or changed files are read again.
# Add -Rebuild to clear this folder's entries and index it from scratch.
# The index lives in <InstallDir>\data\optimizer.db and never leaves your computer.

param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [string]$InstallDir = (Join-Path $env:USERPROFILE "Programs\TokenOptimizationSystem"),
    [switch]$Rebuild
)
$ErrorActionPreference = "Stop"
$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Not installed in $InstallDir. Run scripts\install.ps1 first." }
if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { throw "Folder not found: $Folder" }

$pyArgs = @("-B", (Join-Path $InstallDir "indexer.py"), (Resolve-Path -LiteralPath $Folder).Path)
if ($Rebuild) { $pyArgs += "--rebuild" }
& $venvPy @pyArgs
if ($LASTEXITCODE -ne 0) { throw "Indexing failed." }
Write-Host ""
Write-Host "Done. Claude can now search this folder (restart Claude Desktop if it was not connected yet)."
