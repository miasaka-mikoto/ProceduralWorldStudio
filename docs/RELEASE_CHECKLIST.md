# Release checklist

Use this checklist before handing a build to a Windows user.

## Source and deterministic generation

- [ ] `python -m pytest -q` is green.
- [ ] `python scripts/generate_demo.py --output demo/coastal_city` succeeds.
- [ ] `python scripts/validate_exports.py demo/coastal_city` succeeds.
- [ ] `demo/coastal_city/manifest.json` contains the intended Seed and generator version.
- [ ] `world.json` is treated as the editable source; PNG/OBJ/glTF are derived samples.

## UI smoke test

- [ ] Launch the executable and load **Coastal City**.
- [ ] Switch Top Down, Isometric, Perspective and Overview cameras.
- [ ] Click a road, district, building and POI; Inspector shows ID/type/parent/seed/parameters.
- [ ] Regenerate Roads, Buildings and POI independently.
- [ ] Lock Terrain/Road/District/Building, generate again, and confirm locked layer remains stable.
- [ ] Exercise Ctrl+Z/Ctrl+Y after a generation and after an edit.
- [ ] Load the exported JSON through `WorldState.load_json()` and confirm object IDs remain stable.

## Windows packaging

```powershell
.\scripts\build_windows.ps1
# Optional single-file smoke build
.\scripts\build_windows.ps1 -OneFile
```

Expected portable output:

```text
dist/ProceduralWorldStudio/ProceduralWorldStudio.exe
```

The one-file build is emitted as `dist/ProceduralWorldStudio.exe` on Windows.  Keep the adjacent `README.md` and `demo/coastal_city` directory with an onedir release so a user can inspect the generated samples.
