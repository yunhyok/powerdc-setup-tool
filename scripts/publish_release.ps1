param(
    [string]$Version,
    [string]$Repo = "powerdc-setup-tool"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Version) {
    throw "Usage: publish_release.ps1 -Version <version> [-Repo <name>]"
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw "GitHub CLI 'gh' was not found. Install it, run 'gh auth login', then rerun this script."
}

gh auth status

if (-not (git remote get-url origin 2>$null)) {
    Write-Host "No 'origin' remote found; creating a new public GitHub repository '$Repo'..."
    gh repo create $Repo --public --source . --remote origin
    if ($LASTEXITCODE -ne 0) {
        throw "gh repo create failed with exit code $LASTEXITCODE."
    }
}

git push -u origin (git branch --show-current)
if ($LASTEXITCODE -ne 0) {
    throw "git push failed with exit code $LASTEXITCODE."
}

$installer = "dist/installer/PowerDC-Setup-Tool-Setup-$Version.exe"
if (-not (Test-Path $installer)) {
    throw "Installer artifact not found: $installer. Run scripts/build.ps1 first."
}

gh release create "v$Version" $installer --title "SPD Manipulator for PowerDC v$Version" --notes "Release v$Version of SPD Manipulator for PowerDC."
if ($LASTEXITCODE -ne 0) {
    throw "gh release create failed with exit code $LASTEXITCODE."
}
