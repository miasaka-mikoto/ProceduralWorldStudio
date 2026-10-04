#!/usr/bin/env python3
"""Lightweight integrity checks for a generated demo directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    world_path = root / "world.json"
    if not world_path.exists():
        raise SystemExit(f"Missing {world_path}")
    world = json.loads(world_path.read_text(encoding="utf-8"))
    if not isinstance(world, dict):
        raise SystemExit("world.json must contain an object")
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256_world_json")
        if expected:
            import hashlib

            actual = hashlib.sha256(world_path.read_bytes()).hexdigest()
            if actual != expected:
                raise SystemExit("world.json hash does not match manifest")
    geojson = root / "world.geojson"
    if geojson.exists():
        candidate = json.loads(geojson.read_text(encoding="utf-8"))
        if candidate.get("type") != "FeatureCollection":
            raise SystemExit("world.geojson is not a FeatureCollection")
    print(f"Export validation passed: {root}")
    print(f"JSON keys: {len(world)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

