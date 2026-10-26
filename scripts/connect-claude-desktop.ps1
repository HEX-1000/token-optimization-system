# Token Optimization System: connect it to Claude Desktop (Windows).
#
# Quit Claude Desktop first (tray icon > Quit), then run:
#   powershell -ExecutionPolicy Bypass -File scripts\connect-claude-desktop.ps1
# To undo the change:
#   powershell -ExecutionPolicy Bypass -File scripts\connect-claude-desktop.ps1 -Undo
#
# The script backs up Claude Desktop's config file, adds or updates one entry named
# "token-optimization-system", and checks the result. Other entries are not changed.
# If Claude Desktop has never had an MCP server, open Settings > Developer > Edit Config
# once so the config file exists, quit Claude Desktop, and run this script.

param(
    [switch]$Undo,
    [string]$InstallDir = (Join-Path $env:USERPROFILE "Programs\TokenOptimizationSystem"),
    [string]$Config = "",
    [string]$Name = "token-optimization-system"
)
$ErrorActionPreference = "Stop"

if (-not $Config) {
    $hits = @(@(
        Get-ChildItem -Path "$env:APPDATA\Claude\claude_desktop_config.json" -ErrorAction SilentlyContinue
        Get-ChildItem -Path "$env:LOCALAPPDATA\Packages\*Claude*\LocalCache\Roaming\Claude\claude_desktop_config.json" -ErrorAction SilentlyContinue
        Get-ChildItem -Path "$env:LOCALAPPDATA\Claude-Data\*\claude_desktop_config.json" -ErrorAction SilentlyContinue
    ) | Sort-Object FullName -Unique)
    if ($hits.Count -eq 0) {
        Write-Host "Searching your app data folders for claude_desktop_config.json (this can take a minute)..."
        $hits = @(Get-ChildItem -Path $env:APPDATA, $env:LOCALAPPDATA -Recurse -Depth 6 -Filter "claude_desktop_config.json" -File -ErrorAction SilentlyContinue | Sort-Object FullName -Unique)
    }
    $hits | ForEach-Object { Write-Host ("  found: {0}  (modified {1})" -f $_.FullName, $_.LastWriteTime) }
    if ($hits.Count -eq 1) { $Config = $hits[0].FullName }
    elseif ($hits.Count -gt 1) { throw "Several config files were found. Run again with -Config ""<path>"" for the one Claude Desktop uses (usually the most recently modified)." }
    else { throw "Claude Desktop config not found. Open Claude Desktop > Settings > Developer > Edit Config once, quit Claude Desktop, and run this again." }
}
if (-not (Test-Path -LiteralPath $Config)) { throw "Config file not found: $Config" }
if (Get-Process -Name "claude" -ErrorAction SilentlyContinue) { throw "Claude Desktop is still running. Quit it from the tray icon (right-click > Quit) and run this again." }
Write-Host "Config: $Config"

if ($Undo) {
    $b = Get-ChildItem -Path ($Config + ".backup_tos_*") -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $b) { throw "No backup made by this script was found." }
    Copy-Item -LiteralPath $b.FullName -Destination $Config -Force
    Write-Host "Restored $($b.Name). Start Claude Desktop again."
    exit 0
}

$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
$server = Join-Path $InstallDir "server_d2.py"
if (-not (Test-Path $venvPy) -or -not (Test-Path $server)) { throw "Not installed in $InstallDir. Run scripts\install.ps1 first." }

$backup = $Config + ".backup_tos_" + (Get-Date -Format "yyyyMMdd_HHmmss")
Copy-Item -LiteralPath $Config -Destination $backup
Write-Host "Backup: $backup"

try {
    $raw = [IO.File]::ReadAllText($Config).TrimStart([char]0xFEFF)
    if (-not $raw.Trim()) { $raw = "{}" }
    $j = $raw | ConvertFrom-Json
    if (-not $j.mcpServers) { $j | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force }
    $entry = [pscustomobject]@{ command = $venvPy; args = @("-B", $server) }
    $j.mcpServers | Add-Member -NotePropertyName $Name -NotePropertyValue $entry -Force
    [IO.File]::WriteAllText($Config, ($j | ConvertTo-Json -Depth 50), (New-Object Text.UTF8Encoding($false)))
    & $venvPy -B -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); e=d['mcpServers'][sys.argv[2]]; assert e['args'][1].endswith('server_d2.py'); print('Config checked:', e['command'])" $Config $Name
    if ($LASTEXITCODE -ne 0) { throw "config check failed" }
}
catch {
    Copy-Item -LiteralPath $backup -Destination $Config -Force
    throw "Could not update the config; the original was restored. Details: $_"
}
Write-Host "Done. Start Claude Desktop. Under Settings > Developer, '$Name' should show as running."
