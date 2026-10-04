"""Headless editor adapter checks (no Qt/display required)."""

from __future__ import annotations

import json
from pathlib import Path

from procedural_world_studio.ui_editor import WorldAdapter, calculate_statistics


def test_adapter_uses_core_generator_and_exposes_statistics() -> None:
    adapter = WorldAdapter()
    world = adapter.generate_sync(seed=42, parameters={"terrain_type": "coast", "road_pattern": "hybrid"})
    assert world["seed"] == 42
    assert world["generator_version"]
    assert world["roads"]
    assert world["buildings"]
    assert world["pois"]
    stats = adapter.statistics()
    assert stats["road_length"] > 0
    assert stats["building_count"] == len(world["buildings"])
    assert stats["average_building_height"] > 0
    assert calculate_statistics(world)["building_count"] == float(len(world["buildings"]))


def test_adapter_local_regeneration_and_undo_redo(tmp_path: Path) -> None:
    adapter = WorldAdapter()
    original = adapter.generate_sync(seed=11)
    original_buildings = json.dumps(original["buildings"], sort_keys=True)

    changed = adapter.regenerate("roads", seed=11)
    assert changed["roads"]
    # Road regeneration must not erase buildings, even when the core backend
    # rebuilds dependent stages internally.
    assert json.dumps(changed["buildings"], sort_keys=True) == original_buildings

    restored = adapter.undo()
    assert restored is not None
    assert json.dumps(restored["buildings"], sort_keys=True) == original_buildings
    redone = adapter.redo()
    assert redone is not None
    assert redone["roads"]

    paths = adapter.export_all(tmp_path)
    assert {path.suffix for path in paths} == {".json", ".geojson", ".png", ".obj"}
    assert all(path.exists() for path in paths)


def test_adapter_edit_feature_is_serialisable_and_undoable() -> None:
    adapter = WorldAdapter()
    world = adapter.generate_sync(seed=91)
    building_id = next(iter(world["buildings"]))
    before = json.dumps(world["buildings"][building_id], sort_keys=True)
    adapter.edit_feature(building_id, dx=4.0, dy=-2.0, properties={"floors": 8})
    edited = adapter.world["buildings"][building_id]
    assert edited["properties"]["floors"] == 8
    assert edited["properties"]["height"] == 25.6
    assert json.dumps(edited, sort_keys=True) != before
    assert adapter.world["generator_history"][-1]["stage"] == "edit"
    adapter.undo()
    assert json.dumps(adapter.world["buildings"][building_id], sort_keys=True) == before


def test_adapter_full_generate_honours_seed_locks() -> None:
    adapter = WorldAdapter()
    first = adapter.generate_sync(seed=303)
    roads_before = json.dumps(first["roads"], sort_keys=True)
    terrain_before = json.dumps(first["terrain"], sort_keys=True)
    adapter.set_lock("roads", True)
    adapter.set_lock("terrain", True)

    second = adapter.generate_sync(seed=404, parameters={"terrain_type": "hills", "road_pattern": "grid"})

    assert second["seed"] == 404
    assert json.dumps(second["roads"], sort_keys=True) == roads_before
    assert json.dumps(second["terrain"], sort_keys=True) == terrain_before
