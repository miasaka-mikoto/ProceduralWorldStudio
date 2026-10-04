"""Smoke test the packaged-entry-point-compatible headless mode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_module_headless_export_has_no_double_import_warning(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "procedural_world_studio.ui_editor",
            "--headless",
            "--seed",
            "9",
            "--export",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "RuntimeWarning" not in result.stderr
    assert '"seed": 9' in result.stdout
    assert {path.name for path in tmp_path.iterdir()} == {
        "world.json",
        "world.geojson",
        "world.png",
        "world.obj",
    }

