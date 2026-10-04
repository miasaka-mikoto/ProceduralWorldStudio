#!/usr/bin/env python3
"""Generate a deterministic demo world and write export samples.

The script intentionally talks to the public package API instead of duplicating
generator rules.  It is therefore also useful as a small smoke test for a
source checkout or a portable build.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_SEED = 20261004


def _jsonable(value: Any) -> Any:
    """Convert common world model objects to JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dict__"):
        return {str(k): _jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _world_dict(world: Any) -> dict[str, Any]:
    for name in ("to_dict", "as_dict", "serialize"):
        method = getattr(world, name, None)
        if callable(method):
            result = method()
            if isinstance(result, dict):
                return _jsonable(result)
    result = _jsonable(world)
    return result if isinstance(result, dict) else {"world": result}


def _call_demo(seed: int, terrain: str, road_pattern: str) -> tuple[Any, Any | None]:
    # Prefer the core API so custom terrain/road options are passed with their
    # canonical names and the full WorldState metadata is retained.
    try:
        from procedural_world_studio.core import WorldGenerator
    except ImportError as exc:  # pragma: no cover - helpful CLI error
        # During early/minimal builds the GUI adapter may be available before
        # the core module is installed.  It has a deterministic fallback and
        # keeps this script useful in that intermediate state.
        try:
            from procedural_world_studio.ui_editor import WorldAdapter
        except ImportError as adapter_error:
            raise SystemExit(f"Cannot import generator package: {exc}; adapter error: {adapter_error}") from exc
        adapter = WorldAdapter()
        world = adapter.generate_sync(seed=seed, parameters={"terrain_type": terrain, "road_pattern": road_pattern})
        return world, adapter

    generator = WorldGenerator(seed=seed, width=1200, height=900)
    return generator.generate(
        terrain_type=terrain,
        road_pattern=road_pattern,
        include_harbor=terrain in {"coast", "island"},
        city_name="Coastal City Demo" if terrain == "coast" else "Procedural Demo",
    ), None

def _write_export_helpers(world: Any, data: dict[str, Any], output: Path, exporter: Any | None = None) -> list[str]:
    """Use optional exporter methods when available.

    JSON is always written here.  Other formats are delegated to the world or
    exporter module so this script remains valid while exporters evolve.
    """
    written: list[str] = []
    json_path = output / "world.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    written.append(json_path.name)

    # Prefer a single public export method if the model/adapter exposes one.
    target = exporter or world
    for method_name in ("export_all", "export", "write_exports"):
        method = getattr(target, method_name, None)
        if not callable(method):
            continue
        for kwargs in ({"output_dir": output}, {"directory": output}, {"path": output}):
            try:
                result = method(**kwargs)
                if isinstance(result, (list, tuple, set)):
                    written.extend(Path(item).name for item in result)
                break
            except (TypeError, AttributeError):
                continue
        break

    # Export module names are kept intentionally broad for compatibility with
    # the standalone exporter package used by the project.
    try:
        from procedural_world_studio import export_formats as exporters  # type: ignore
    except ImportError:
        exporters = None
    if exporters is not None:
        all_fn = getattr(exporters, "export_all", None)
        if callable(all_fn):
            try:
                result = all_fn(world, output)
                if isinstance(result, dict):
                    written.extend(Path(item).name for item in result.values())
                elif isinstance(result, (list, tuple, set)):
                    written.extend(Path(item).name for item in result)
            except (TypeError, AttributeError, ValueError, OSError):
                pass
        for name in ("export_geojson", "export_png", "export_obj", "export_gltf"):
            fn = getattr(exporters, name, None)
            if not callable(fn):
                continue
            for args in ((world, output), (data, output), (world, output / name.removeprefix("export_").replace("geojson", "world.geojson").replace("png", "world.png").replace("obj", "world.obj").replace("gltf", "world.gltf"))):
                try:
                    result = fn(*args)
                    if result:
                        written.append(Path(result).name)
                    break
                except (TypeError, AttributeError, ValueError, OSError):
                    continue
    return sorted(set(written))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the fixed Procedural World Studio demo city")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--terrain", default="coast", choices=("flat", "island", "coast", "river", "hills"))
    parser.add_argument("--road-pattern", default="hybrid", choices=("grid", "organic", "radial", "hybrid"))
    parser.add_argument("--output", type=Path, default=Path("demo/coastal_city"))
    args = parser.parse_args(argv)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    world, exporter = _call_demo(args.seed, args.terrain, args.road_pattern)
    data = _world_dict(world)
    files = _write_export_helpers(world, data, output, exporter)

    payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    collections: dict[str, int] = {}
    for key in ("terrain", "water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois"):
        value = data.get(key)
        if key == "terrain":
            # Terrain is a height-field document, not a mapping of ten
            # entities; expose it as one generated stage in the manifest.
            collections[key] = 1 if isinstance(value, dict) and value else 0
        elif isinstance(value, (list, tuple, dict)):
            collections[key] = len(value)
    manifest = {
        "seed": args.seed,
        "terrain": args.terrain,
        "road_pattern": args.road_pattern,
        "generator_version": data.get("generator_version", data.get("version", metadata.get("generator_version", "unknown"))),
        "feature_count": sum(collections.values()),
        "collection_counts": collections,
        "sha256_world_json": hashlib.sha256((output / "world.json").read_bytes()).hexdigest(),
        "files": files,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Generated demo in {output}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
