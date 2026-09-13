# -----------------------------------------------------------------------------
# Hermes Google Antigravity Plugin Installer
# -----------------------------------------------------------------------------
[CmdletBinding()]
param (
    [string]$CustomHermesHome = ""
)

$ErrorActionPreference = "Stop"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   Hermes Google Antigravity Plugin - 1-Click Installer    " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Check Antigravity CLI (agy)
Write-Host "`n[1/4] Checking Google Antigravity CLI (agy)..." -ForegroundColor Yellow
$agyCmd = Get-Command "agy" -ErrorAction SilentlyContinue
$agyPath = if ($agyCmd) { $agyCmd.Source } else { Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe" }

if (!(Test-Path $agyPath) -and !$agyCmd) {
    Write-Host " [!] WARNING: Antigravity CLI (agy) was not found in PATH or standard directory." -ForegroundColor Yellow
    Write-Host "     Please install Google Antigravity CLI and run 'agy auth login'." -ForegroundColor Yellow
} else {
    Write-Host " [+] Found Antigravity CLI: $agyPath" -ForegroundColor Green
}

# 2. Locate HERMES_HOME
Write-Host "`n[2/4] Locating Hermes installation directory..." -ForegroundColor Yellow
$hermesHome = ""
if ($CustomHermesHome -and (Test-Path $CustomHermesHome)) {
    $hermesHome = $CustomHermesHome
} elseif ($env:HERMES_HOME -and (Test-Path $env:HERMES_HOME)) {
    $hermesHome = $env:HERMES_HOME
} else {
    $candidates = @(
        (Join-Path $env:USERPROFILE ".hermes"),
        "C:\Hermes"
    )
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            $hermesHome = $cand
            break
        }
    }
}

if (!$hermesHome) {
    $hermesHome = Join-Path $env:USERPROFILE ".hermes"
    Write-Host " [*] HERMES_HOME not set; defaulting to user directory: $hermesHome" -ForegroundColor Gray
}

Write-Host " [+] Target Hermes Home: $hermesHome" -ForegroundColor Green

# 3. Copy Plugin Files
Write-Host "`n[3/4] Installing plugin into Hermes model-providers..." -ForegroundColor Yellow
$targetPluginDir = Join-Path $hermesHome "plugins\model-providers\antigravity"
if (!(Test-Path $targetPluginDir)) {
    New-Item -ItemType Directory -Path $targetPluginDir -Force | Out-Null
}

$sourceDir = $PSScriptRoot
$filesToCopy = @(
    "plugin.yaml",
    "__init__.py",
    "agy_bridge.py",
    "bridge_manager.py",
    "README.md"
)

foreach ($file in $filesToCopy) {
    $src = Join-Path $sourceDir $file
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination (Join-Path $targetPluginDir $file) -Force
        Write-Host "  -> Copied $file" -ForegroundColor Gray
    } else {
        Write-Host "  [!] Missing source file: $file" -ForegroundColor Red
    }
}

# 4. Verify Installation
Write-Host "`n[4/4] Verifying installation..." -ForegroundColor Yellow
if (Test-Path (Join-Path $targetPluginDir "__init__.py")) {
    Write-Host "`n============================================================" -ForegroundColor Green
    Write-Host " [SUCCESS] Google Antigravity plugin installed successfully!" -ForegroundColor Green
    Write-Host " Location: $targetPluginDir" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "`nTo use Antigravity models in Hermes:" -ForegroundColor Cyan
    Write-Host "  1. Start Hermes CLI or Hermes Desktop" -ForegroundColor White
    Write-Host "  2. Choose Provider: 'Google Antigravity' (or run: hermes model)" -ForegroundColor White
    Write-Host "  3. Select any model (e.g. gemini-3.8-flash, claude-sonnet-4.6)" -ForegroundColor White
    Write-Host "`nLogs will be saved to: $(Join-Path $hermesHome 'logs\agy_tools.log')" -ForegroundColor Gray
} else {
    Write-Host " [ERROR] Failed to install plugin files." -ForegroundColor Red
}
