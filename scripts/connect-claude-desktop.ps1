# Token Optimization System: connect it to Claude Desktop (Windows).
#
# Quit Claude Desktop first (tray icon > Quit), then run:
#   powershell -ExecutionPolicy Bypass -File scripts\connect-claude-desktop.ps1
# To undo the change:
#   powershell -ExecutionPolicy Bypass -File scripts\connect-claude-desktop.ps1 -Undo
#
# The script backs up Claude Desktop's config file, adds or updates one entry named
# "token-optimization-system", and checks the result. Other entries are not changed.
# -Undo backs up the config file and removes that one entry. It does not put an older
# copy of the file back, so changes made to the config since the entry was added stay.
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

function Backup-Config {
    # A name that does not exist yet, so a second backup in the same second cannot replace the first.
    $stamp = $Config + ".backup_tos_" + (Get-Date -Format "yyyyMMdd_HHmmss")
    $path = $stamp
    $n = 1
    while (Test-Path -LiteralPath $path) { $n++; $path = $stamp + "_" + $n }
    Copy-Item -LiteralPath $Config -Destination $path
    Write-Host "Backup: $path"
    return $path
}

function Read-Config {
    $raw = [IO.File]::ReadAllText($Config).TrimStart([char]0xFEFF)
    if (-not $raw.Trim()) { $raw = "{}" }
    return ($raw | ConvertFrom-Json)
}

if ($Undo) {
    # Remove the one entry by name. Restoring a backup of the whole file would bring the entry
    # back after a second run of this script, and would drop later changes to the config.
    $j = Read-Config
    $names = @()
    if ($j.mcpServers) { $names = @($j.mcpServers.PSObject.Properties | ForEach-Object { $_.Name }) }
    if ($names -notcontains $Name) {
        Write-Host "The config has no entry named '$Name'. Nothing was changed."
        exit 0
    }
    $backup = Backup-Config
    try {
        $j.mcpServers.PSObject.Properties.Remove($Name)
        [IO.File]::WriteAllText($Config, ($j | ConvertTo-Json -Depth 50), (New-Object Text.UTF8Encoding($false)))
        $after = Read-Config
        if (@($after.mcpServers.PSObject.Properties | ForEach-Object { $_.Name }) -contains $Name) { throw "the entry is still in the config" }
    }
    catch {
        Copy-Item -LiteralPath $backup -Destination $Config -Force
        throw "Could not update the config; the original was restored. Details: $_"
    }
    Write-Host "Removed '$Name'. Other entries were not changed. Start Claude Desktop again."
    exit 0
}

$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
$server = Join-Path $InstallDir "server_d2.py"
if (-not (Test-Path $venvPy) -or -not (Test-Path $server)) { throw "Not installed in $InstallDir. Run scripts\install.ps1 first." }

$backup = Backup-Config

try {
    $j = Read-Config
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
