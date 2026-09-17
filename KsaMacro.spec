# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['ksa_macro_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ship_icon.ico', '.'),
        ('ship_icon.png', '.'),
    ],
    hiddenimports=[
        'keyring.backends.Windows',
        'keyring.backends',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'requests',
    ],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='KsaMacro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='ship_icon.ico',
)
