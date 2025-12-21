# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['monitor_radio.py'],
    pathex=[],
    binaries=[
        ('ffmpeg.exe', 'ffmpeg'),
        ('ffprobe.exe', 'ffmpeg'),
    ],
    datas=[],
    hiddenimports=[
        'flet',
        'flet.canvas',
        'flet_audio',
        'aiohttp',
        'numpy',
        'google.cloud.firestore',
        'google.oauth2.service_account',
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
    name='RadioMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # SIN CONSOLA
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
