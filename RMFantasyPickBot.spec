# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the RMFantasySMX Pick Bot.

Build on Windows with:  pyinstaller --noconfirm RMFantasyPickBot.spec
(or just run build.bat)

Produces a single-file windowed executable in dist\\RMFantasyPickBot.exe.
CustomTkinter/Selenium/webdriver-manager ship data files and dynamic imports,
so we collect them explicitly to avoid "module/asset not found" at runtime.
"""

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for _pkg in ("customtkinter", "selenium", "webdriver_manager"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# keyring resolves its backends dynamically at runtime.
hiddenimports += [
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.macOS",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RMFantasyPickBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
