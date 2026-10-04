# Demo Cities

## Hills Town (alternate fixed seed)

`demo/hills_town/` uses seed `31415`, Hills terrain and Organic roads.  It is
included to exercise a non-coastal world (no harbor) while retaining the same
editable JSON/GeoJSON/PNG/OBJ/glTF export contract.

## Coastal City (fixed seed)

The canonical demo is generated with a fixed seed and contains:

- Coast terrain with a navigable water edge
- Harbor docks, warehouses, industrial road and station reservation
- Downtown/commercial core
- Residential district and park/green area
- Main, secondary, street and alley road hierarchy
- Buildings, POIs and population metadata

Generate or refresh it with:

```powershell
python scripts/generate_demo.py --output demo/coastal_city
```

Expected files (depending on enabled exporters):

```text
demo/coastal_city/
  world.json
  world.geojson
  world.png
  world.obj       # optional
  world.gltf      # optional
  manifest.json
```

`manifest.json` records the seed, generator version, parameters, counts and file hashes. It is deliberately deterministic, so it is useful for comparing future generator revisions without treating a PNG as the source of truth.
