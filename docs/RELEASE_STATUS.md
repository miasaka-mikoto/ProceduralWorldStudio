# Release status — 0.1.0

The fixed demo is generated from seed `20261004` with Coast terrain and Hybrid
roads.  Its deterministic collections are:

| Stage | Count |
| --- | ---: |
| Water | 1 |
| Roads | 39 |
| Districts | 16 |
| Blocks | 125 |
| Lots | 375 |
| Buildings | 284 |
| Harbor objects | 13 |
| POIs | 13 |

Validated release checks:

- `python -m pytest -q` — 40 passed
- `python scripts/validate_exports.py demo/coastal_city` — passed
- `python scripts/validate_exports.py demo/hills_town` — passed
- deterministic JSON/GeoJSON/PNG/OBJ/glTF exports — checked
- PyInstaller onedir and one-file Linux smoke builds — checked
- Windows build recipe — `scripts/build_windows.ps1` (run on Windows to emit
  the native `ProceduralWorldStudio.exe`)

The generated world remains the source of truth: screenshots are included for
UI smoke review, while JSON preserves geometry, parent links, parameters,
locks, history and population metadata.
