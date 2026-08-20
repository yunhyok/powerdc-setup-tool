param(
    [string]$Version
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Get-CanonicalVersion {
    $pyproject = Get-Content -Raw (Join-Path $Root "pyproject.toml")
    $projectMatch = [regex]::Match($pyproject, '(?m)^\s*version\s*=\s*"([^"]+)"\s*$')
    if (-not $projectMatch.Success) {
        throw "Canonical project version was not found in pyproject.toml."
    }

    $package = Get-Content -Raw (Join-Path $Root "src\powerdc_setup_tool\__init__.py")
    $packageMatch = [regex]::Match($package, '__version__\s*=\s*"([^"]+)"')
    if (-not $packageMatch.Success) {
        throw "Package __version__ was not found in src/powerdc_setup_tool/__init__.py."
    }
    if ($projectMatch.Groups[1].Value -ne $packageMatch.Groups[1].Value) {
        throw "pyproject.toml version $($projectMatch.Groups[1].Value) does not match package version $($packageMatch.Groups[1].Value)."
    }
    return $projectMatch.Groups[1].Value
}

$canonicalVersion = Get-CanonicalVersion
if (-not $Version) {
    $Version = $canonicalVersion
} elseif ($Version -ne $canonicalVersion) {
    throw "Requested release version '$Version' does not match canonical project version '$canonicalVersion'."
}

# Publishing is deliberately git-only.  The tag-triggered workflow is the sole
# release-asset publisher, so a stale local installer can never be attached to a
# freshly pushed tag.  A named origin is required; never create a public repo as
# a side effect of a release command run from the wrong checkout.
$status = @(git status --porcelain --untracked-files=all 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect git status."
}
if ($status.Count -gt 0) {
    throw "Working tree is dirty; commit or stash all changes before publishing."
}

$branch = (git symbolic-ref --quiet --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $branch) {
    throw "Publishing requires a named branch; detached HEAD is not allowed."
}
$head = (git rev-parse --verify HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $head) {
    throw "Could not resolve HEAD."
}
$origin = (git remote get-url origin 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $origin) {
    throw "No 'origin' remote found; refusing to create a public repository."
}

$tag = "v$Version"
$localTagType = (git cat-file -t $tag 2>$null)
if ($LASTEXITCODE -eq 0) {
    throw "Tag $tag already exists locally; refusing to reuse an existing release tag."
}
$remoteTag = @(git ls-remote --tags origin "refs/tags/$tag" 2>$null)
$remoteTagExitCode = $LASTEXITCODE
if ($remoteTagExitCode -ne 0) {
    throw "Could not query remote tags on origin (exit code $remoteTagExitCode)."
}
if ($remoteTag.Count -gt 0) {
    throw "Tag $tag already exists on origin; refusing to republish an existing release tag."
}

Write-Host "Preparing release tag $tag from $branch at $head"
git push -u origin $branch
if ($LASTEXITCODE -ne 0) {
    throw "git push failed with exit code $LASTEXITCODE."
}
$headAfterPush = (git rev-parse --verify HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or $headAfterPush -ne $head) {
    throw "HEAD changed while publishing; refusing to create a release tag."
}

# Create and push an annotated tag at the exact commit recorded above.  The
# workflow triggered by this push builds the installer from that tagged commit,
# verifies its SHA-256 asset, and publishes the release idempotently.
git tag --annotate $tag $head --message "Release $tag"
if ($LASTEXITCODE -ne 0) {
    throw "Could not create annotated tag $tag."
}
git push origin "refs/tags/$tag"
if ($LASTEXITCODE -ne 0) {
    throw "Could not push annotated tag $tag."
}

$remoteHead = (git ls-remote --exit-code origin "refs/tags/$tag^{}" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $remoteHead -or (($remoteHead -split "\s+")[0] -ne $head)) {
    throw "Remote annotated tag $tag does not resolve to recorded HEAD $head."
}
Write-Host "Tag $tag points to $head. GitHub Actions will build and publish the verified installer."
