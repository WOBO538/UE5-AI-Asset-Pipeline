# -*- mode: python ; coding: utf-8 -*-

import os

try:
    # SPECPATH 已是 spec 文件所在目录（PyInstaller 注入），无需再 dirname
    _SPEC_DIR = os.path.abspath(SPECPATH)
except NameError:
    _SPEC_DIR = os.getcwd()
# env_path.py 位于 src/ai_layer/，是 spec 所在目录（src/external_pipeline/）的上一级 ai_layer — 供 hiddenimport 定位
AI_LAYER_DIR = os.path.abspath(os.path.join(_SPEC_DIR, '..', 'ai_layer'))


a = Analysis(
    ['pipeline_watcher.py'],
    pathex=[AI_LAYER_DIR],
    binaries=[],
    datas=[('config', 'config')],
    hiddenimports=['comfy_client', 'env_path'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AssetPipeline',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
