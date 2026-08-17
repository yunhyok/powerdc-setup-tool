# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# `pathex` entries are resolved against the *invocation* CWD, not against this
# file, so the plain relative ".."/"../src" pair only found the package when
# PyInstaller happened to be run from `packaging/`. Anchor them to SPECPATH (the
# directory holding this .spec, injected by PyInstaller) so `pyinstaller
# packaging/powerdc-setup-tool.spec` works from the repo root -- which is how
# both `scripts/build.ps1` and `.github/workflows/build.yml` invoke it.
_REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller global
_SRC = os.path.join(_REPO_ROOT, "src")

a = Analysis(
    ["../src/powerdc_setup_tool/app.py"],
    pathex=[_REPO_ROOT, _SRC],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SPD Manipulator for PowerDC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SPD Manipulator for PowerDC",
)
