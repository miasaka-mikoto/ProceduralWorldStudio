# PyInstaller spec for Procedural World Studio.
# Build with: python -m PyInstaller --clean --noconfirm procedural_world_studio.spec
from pathlib import Path
import os

root = Path(SPECPATH)
onefile = os.environ.get("PWS_ONEFILE", "") == "1"
entry = root / "main.py"
if not entry.exists():
    entry = root / "procedural_world_studio" / "ui_editor.py"

datas = []
for rel in ("README.md", "docs", "demo"):
    path = root / rel
    if path.exists():
        datas.append((str(path), rel))

a = Analysis(
    [str(entry)],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=["procedural_world_studio", "procedural_world_studio.core", "procedural_world_studio.ui_editor"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# PyInstaller 6.x may expose shared-library aliases as top-level ``SYMLINK``
# data entries on POSIX.  The corresponding real binaries are already in
# ``a.binaries`` (under their package-relative Qt/Pillow/Numpy paths), and
# COLLECT otherwise tries to create an alias over a file with the same name.
# Drop only those generated aliases; Windows builds do not normally contain
# these entries and therefore keep the native layout unchanged.
if os.name != "nt":
    a.datas = [
        item for item in a.datas
        if not (len(item) >= 3 and item[2] == "SYMLINK")
    ]
pyz = PYZ(a.pure)
if onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="ProceduralWorldStudio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
else:
    # The default build keeps an onedir layout so demo assets and exports are
    # easy to inspect and replace.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ProceduralWorldStudio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="ProceduralWorldStudio",
    )
