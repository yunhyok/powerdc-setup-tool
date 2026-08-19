param(
    [string]$Version = "0.1.4",
    [switch]$SkipInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Resolve-Iscc {
    # windows-latest ships Inno Setup 6 preinstalled at this fixed path; `iscc`
    # is NOT guaranteed to be on PATH there, so check the known path first.
    $fixedPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (Test-Path $fixedPath) {
        return $fixedPath
    }

    $isccCmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($isccCmd) {
        return $isccCmd.Source
    }

    Write-Host "Inno Setup compiler not found; attempting to install via Chocolatey..."
    choco install innosetup -y
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Inno Setup via Chocolatey (exit code $LASTEXITCODE)."
    }

    if (Test-Path $fixedPath) {
        return $fixedPath
    }

    throw "Inno Setup compiler (ISCC.exe) was not found, even after attempting a Chocolatey install. " +
        "Install Inno Setup 6 manually (https://jrsoftware.org/isdl.php) and ensure ISCC.exe is on PATH, " +
        "or rerun with -SkipInstaller."
}

if ($SkipTests) {
    Write-Host "Skipping tests (-SkipTests)."
} else {
    python -m pytest -m "not slow"
    if ($LASTEXITCODE -ne 0) {
        throw "pytest failed with exit code $LASTEXITCODE."
    }
}

pyinstaller --clean --noconfirm packaging/powerdc-setup-tool.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if ($SkipInstaller) {
    Write-Host "Skipping Inno Setup installer (-SkipInstaller)."
    exit 0
}

$isccPath = Resolve-Iscc

& $isccPath "/DMyAppVersion=$Version" packaging/powerdc-setup-tool.iss
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
