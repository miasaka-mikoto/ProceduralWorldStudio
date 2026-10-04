"""Capture a deterministic off-screen editor screenshot for release QA.

On Windows this can be run with a normal display; in CI/Linux use
``QT_QPA_PLATFORM=offscreen python scripts/capture_ui_smoke.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets

from procedural_world_studio.ui_editor import MainWindow, WorldAdapter


def main() -> int:
    out = Path("demo/coastal_city/editor_screenshot.png")
    iso_out = out.with_name("editor_screenshot_isometric.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow(WorldAdapter())
    window.resize(1480, 900)
    window.show()
    app.processEvents()
    window.grab().save(str(out), "PNG")
    # Keep a second view in the demo package so the 3-D-ish building volume
    # renderer is inspectable without launching Qt.  This is still the same
    # deterministic world and merely changes the camera selector.
    window.camera_combo.setCurrentText("Isometric")
    app.processEvents()
    window.grab().save(str(iso_out), "PNG")
    window.close()
    app.processEvents()
    print(out)
    print(iso_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
