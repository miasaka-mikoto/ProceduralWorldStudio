# Procedural World Studio

**程序化世界生成工作台** is a standalone, editable world-generation tool.  It creates a city or settlement from deterministic rules and a seed; it does not call an image-generation model.  Every generated item remains inspectable, reproducible, and exportable.

## What it generates

The core pipeline is:

`Seed → Terrain → Water → Roads → Districts → Blocks → Lots → Buildings → POI → Population metadata → Export`

The first release includes terrain presets (`Flat`, `Island`, `Coast`, `River`, `Hills`), grid/organic/radial/hybrid roads, rule-based districts and parcels, simple building volumes, a dedicated harbour layout, POI placement, seed locks, regeneration by layer, direct Inspector edits (move objects and change building floors), undo/redo, generator history, statistics, and a world inspector.

The bundled fixed-seed demo is a **Coastal City** containing a harbour, station area, downtown, residential district, industrial district, and park.

## Quick start (source)

Python 3.10+ is recommended.  From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

If the package entry point is available, the equivalent command is:

```powershell
python -m procedural_world_studio.ui_editor
```

The UI is intentionally usable without network access.  PySide6 is preferred; a lightweight Tkinter fallback is provided when Qt is not installed.

## Generate the fixed demo and exports

```powershell
python scripts/generate_demo.py --output demo/coastal_city
```

The command writes a deterministic world and the available export formats (JSON, GeoJSON, PNG map and OBJ/glTF geometry when enabled).  Re-running with the same seed and generator version should produce the same metadata and geometry.

For a different world:

```powershell
python scripts/generate_demo.py `
  --seed 20261004 `
  --terrain coast `
  --road-pattern hybrid `
  --output demo/custom_city
```

## Windows portable build

Run this from PowerShell on a Windows machine with Python and (optionally) Inno Setup installed:

```powershell
.\scripts\build_windows.ps1
```

The script creates a clean build environment, runs the test suite, invokes PyInstaller, copies the README and demo data, and produces:

`dist/ProceduralWorldStudio/ProceduralWorldStudio.exe`

An optional `-OneFile` switch creates a single-file executable.  The default onedir build starts faster and is easier to diagnose.

To produce separate portable and source ZIP archives for handoff:

```powershell
.\scripts\package_release.ps1
```

## Project layout

| Path | Purpose |
| --- | --- |
| `procedural_world_studio/` | Generator, domain models, editor and exporters |
| `scripts/generate_demo.py` | Fixed-seed demo and export runner |
| `scripts/build_windows.ps1` | Reproducible Windows/PyInstaller build |
| `procedural_world_studio.spec` | PyInstaller application specification |
| `docs/USER_GUIDE.md` | Walk-through for editing and regeneration |
| `demo/` | Generated demo worlds and export samples |
| `tests/` | Unit and smoke tests |

## Determinism and regeneration

Each layer has its own seed lock.  Locking terrain, roads, districts, or buildings keeps that layer stable while another layer is regenerated; a full seed change from the desktop editor also preserves explicitly locked layers.  The generator history records the base seed, layer seeds, parameters, generator version and result metadata, so a world can be reproduced later.

## Export formats

- **JSON** — complete editable world document, including IDs, parent links, parameters and history.
- **GeoJSON** — terrain, roads, districts, blocks, lots, buildings and POIs as feature collections.
- **PNG** — top-down preview map for quick sharing.
- **OBJ/glTF** — basic building/road geometry for Blender, Godot and Unreal import.  These are intentionally simple geometry exports, not baked game assets.

## Development checks

```powershell
python -m pytest -q
python scripts/validate_exports.py demo/coastal_city
```

The tests use deterministic seeds and synthetic data.  No paid model API, network service, API key, or image-generation endpoint is required.

For a repeatable UI smoke screenshot (including the editable Inspector panel):

```powershell
python scripts/capture_ui_smoke.py
```

## Scope and non-goals

Procedural World Studio is an experiment and editing aid, not a claim that generated societies or cities predict real places.  It does not bypass DRM, authentication, anti-cheat, or any game service.  The first geometry pass favours transparent rules and editability over photorealistic rendering.
