# -*- mode: python ; coding: utf-8 -*-
# Compilar con:  python -m PyInstaller BuscadorNovelas.spec
# Modo CARPETA (onedir): dist/BuscadorNovelas/BuscadorNovelas.exe + librerías al lado.
# Los antivirus desconfían menos de este formato que del .exe único que se autoextrae.
a = Analysis(
    ['novel_search.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['glosario', 'traductor', 'deep_translator', 'bs4', 'requests', 'sqlite3'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'scipy', 'PIL', 'pytest', 'IPython', 'setuptools'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='BuscadorNovelas',
    debug=False, strip=False, upx=False,      # sin UPX: el empaquetado comprimido dispara falsos positivos
    console=False,
    version='version_info.txt',
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='BuscadorNovelas')
