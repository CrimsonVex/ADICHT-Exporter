# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AdichtExporter with size trimming.
# Build:  python -m PyInstaller AdichtExporter.spec --noconfirm
#
# Trims (verified unused by the app's selftest + smoketest):
#  - pyarrow Flight/Dataset/Acero/Substrait/Gandiva DLLs and C headers
#    (only Table/csv/parquet writing is used)
#  - all .lib/.h/.c files (link-time only, never needed at runtime)
#  - adi: non-3.14 compiled modules and the 32-bit SDK DLL
#  - tkinterdnd2: everything but the win-x64 Tcl8.6 binary
#  - PIL / cryptography / lxml: optional deps of openpyxl, unused here

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['_cffi_backend']
for pkg in ('adi', 'tkinterdnd2'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['adicht_exporter.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pandas', 'matplotlib', 'scipy', 'PIL', 'cryptography', 'lxml'],
    noarchive=False,
)

_DROP_PREFIXES = (
    'pyarrow/arrow_flight', 'pyarrow/_flight',
    'pyarrow/arrow_python_flight',
    'pyarrow/arrow_substrait', 'pyarrow/_substrait',
    'pyarrow/arrow_dataset', 'pyarrow/_dataset',
    'pyarrow/arrow_acero', 'pyarrow/_acero',
    'pyarrow/gandiva', 'pyarrow/_gandiva',
    'pyarrow/include/', 'pyarrow/tests/',
    'adi/adidatiowin.dll', 'adi/_adi_cffi2',
)


def _keep(entry):
    dest = entry[0].lower().replace('\\', '/')
    if dest.endswith(('.lib', '.pdb', '.h', '.c')):
        return False
    if any(dest.startswith(p) for p in _DROP_PREFIXES):
        return False
    if dest.startswith('adi/_adi_cffi') and 'cp314' not in dest:
        return False
    if (dest.startswith('tkinterdnd2/tkdnd/')
            and not dest.startswith('tkinterdnd2/tkdnd/win-x64/')):
        return False
    return True


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AdichtExporter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
