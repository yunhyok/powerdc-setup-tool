"""Packaging/CI/docs sanity checks -- design.md §E `test_project_files`.

Pure text/metadata assertions against the packaging, CI, and documentation
files owned by chunk 6. No PyInstaller/Inno Setup/gh invocation here -- this
just guards that the generated files say what they need to say so a human
(or CI) build actually works.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - repo targets Python >=3.12
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]

OLD_APP_APPID = "E2D43B9E-8A63-44F0-8E26-3D528D875B58"
NEW_APP_APPID = "929DA9C6-4698-4C9D-8CD2-18A555E547E7"


def _read(relpath: str) -> str:
    path = ROOT / relpath
    assert path.is_file(), f"expected file missing: {relpath}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_packaging_and_ci_files_exist() -> None:
    for relpath in (
        "packaging/powerdc-setup-tool.spec",
        "packaging/powerdc-setup-tool.iss",
        "scripts/build.ps1",
        "scripts/publish_release.ps1",
        ".github/workflows/build.yml",
        "README.md",
        "LICENSE",
        ".gitignore",
        ".gitattributes",
    ):
        assert (ROOT / relpath).is_file(), f"expected file missing: {relpath}"


# ---------------------------------------------------------------------------
# packaging/powerdc-setup-tool.spec
# ---------------------------------------------------------------------------


def test_spec_defines_entry_path_and_app_name() -> None:
    spec = _read("packaging/powerdc-setup-tool.spec")

    assert "src/powerdc_setup_tool/app.py" in spec
    assert '"SPD Manipulator for PowerDC"' in spec
    # onedir build: EXE ships without bundled binaries, COLLECT gathers them.
    assert "exclude_binaries=True" in spec
    assert "console=False" in spec
    assert "upx=True" in spec
    assert "COLLECT(" in spec
    # `pathex` must be anchored to the spec's own directory: relative entries are
    # resolved against the invocation CWD, and both `scripts/build.ps1` and the
    # CI workflow run `pyinstaller packaging/...spec` from the repo root, where
    # ".."/"../src" point outside the checkout entirely.
    assert "SPECPATH" in spec
    assert 'pathex=["..", "../src"]' not in spec


# ---------------------------------------------------------------------------
# packaging/powerdc-setup-tool.iss
# ---------------------------------------------------------------------------


def test_iss_defines_app_metadata_and_fresh_appid() -> None:
    iss = _read("packaging/powerdc-setup-tool.iss")

    assert 'MyAppName "SPD Manipulator for PowerDC"' in iss
    assert 'MyAppPublisher "Yunhyok"' in iss
    assert "PowerDC-Setup-Tool-Setup" in iss
    assert NEW_APP_APPID in iss
    # Must not reuse the old app's installer identity.
    assert OLD_APP_APPID not in iss
    assert "MyAppVersion" in iss and '"0.1.0"' in iss
    assert "SPD Manipulator for PowerDC" in iss  # DefaultDirName / Files source
    assert "desktopicon" in iss
    assert "Compression=lzma" in iss
    assert "SolidCompression=yes" in iss
    assert "WizardStyle=modern" in iss
    # `x64compatible` supersedes `x64` only from Inno Setup 6.3 on, and is a hard
    # compile error on anything older (CI installs whatever choco pins), so the
    # plain `x64` spelling -- valid across all of 6.x -- is what must be here.
    assert "ArchitecturesInstallIn64BitMode=x64\n" in iss
    assert "x64compatible" not in iss
    assert "[Run]" in iss


# ---------------------------------------------------------------------------
# scripts/build.ps1
# ---------------------------------------------------------------------------


def test_build_script_contents() -> None:
    build_ps1 = _read("scripts/build.ps1")

    assert "$Version" in build_ps1
    assert "SkipInstaller" in build_ps1
    assert "SkipTests" in build_ps1
    assert "$ErrorActionPreference" in build_ps1 and '"Stop"' in build_ps1
    assert 'pytest -m "not slow"' in build_ps1
    assert "packaging/powerdc-setup-tool.spec" in build_ps1
    assert "packaging/powerdc-setup-tool.iss" in build_ps1
    assert "ISCC" in build_ps1
    assert "choco install innosetup" in build_ps1
    assert "$LASTEXITCODE" in build_ps1
    # Every external call ($LASTEXITCODE is checked, not just referenced once).
    assert build_ps1.count("$LASTEXITCODE") >= 3


# ---------------------------------------------------------------------------
# scripts/publish_release.ps1
# ---------------------------------------------------------------------------


def test_publish_release_script_contents() -> None:
    publish_ps1 = _read("scripts/publish_release.ps1")

    assert '$Repo = "powerdc-setup-tool"' in publish_ps1
    assert "gh" in publish_ps1
    assert "--public" in publish_ps1
    assert "gh repo create" in publish_ps1
    assert "PowerDC-Setup-Tool-Setup" in publish_ps1
    assert "gh release create" in publish_ps1


# ---------------------------------------------------------------------------
# .github/workflows/build.yml
# ---------------------------------------------------------------------------


def test_workflow_contents() -> None:
    workflow = _read(".github/workflows/build.yml")

    assert "windows-latest" in workflow
    assert "contents: write" in workflow
    assert "v*" in workflow
    assert "workflow_dispatch" in workflow
    assert "actions/checkout@v4" in workflow
    assert "actions/setup-python@v5" in workflow
    assert '"3.12"' in workflow
    assert '.[dev]"' in workflow
    assert 'pytest -m "not slow"' in workflow
    assert "QT_QPA_PLATFORM" in workflow and "offscreen" in workflow
    assert "packaging/powerdc-setup-tool.spec" in workflow
    assert "packaging" in workflow and "powerdc-setup-tool.iss" in workflow
    assert "ISCC" in workflow
    assert "$LASTEXITCODE" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "PowerDC-Setup-Tool-Setup" in workflow
    assert "gh release create" in workflow
    assert "GH_TOKEN" in workflow and "secrets.GITHUB_TOKEN" in workflow
    assert "0.0.0-dev" in workflow


# ---------------------------------------------------------------------------
# .gitignore / .gitattributes
# ---------------------------------------------------------------------------


def test_gitignore_excludes_spd_and_dist() -> None:
    gitignore = _read(".gitignore")

    assert "*.spd" in gitignore
    assert "dist/" in gitignore


def test_gitattributes_forces_lf_line_endings() -> None:
    gitattributes = _read(".gitattributes")

    assert "eol=lf" in gitattributes


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------


def test_readme_documents_key_features() -> None:
    readme = _read("README.md")

    assert "SPD Manipulator for PowerDC" in readme
    assert "streaming" in readme.lower()
    assert "LF" in readme  # case-sensitive: avoid false hits on words like "self"
    assert "VRM" in readme
    assert "Sink" in readme
    assert "bulk edit" in readme.lower()
    assert "never" in readme.lower() and "overwrite" in readme.lower()
    # Build workflow badge + release/build docs.
    assert "actions/workflows/build.yml" in readme
    assert "Releases" in readme
    # File-format one-liners.
    assert ".NetList" in readme
    assert ".PowerDC" in readme
    assert ".Connect" in readme
    # Korean summary section present.
    assert "한국어" in readme
    assert "License" in readme


# ---------------------------------------------------------------------------
# LICENSE
# ---------------------------------------------------------------------------


def test_license_is_mit_2026_yunhyok() -> None:
    license_text = _read("LICENSE")

    assert "MIT License" in license_text
    assert "2026" in license_text
    assert "Yunhyok" in license_text


# ---------------------------------------------------------------------------
# pyproject.toml <-> __init__.__version__ consistency
# ---------------------------------------------------------------------------


def test_pyproject_matches_package_name_script_and_version() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    project = pyproject["project"]

    assert project["name"] == "powerdc-setup-tool"
    assert project["version"] == "0.1.0"

    scripts = project.get("scripts", {})
    assert scripts.get("powerdc-setup-tool") == "powerdc_setup_tool.app:main"

    init_text = _read("src/powerdc_setup_tool/__init__.py")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert match is not None, "__version__ not found in __init__.py"
    assert match.group(1) == project["version"]
