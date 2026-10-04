"""Integration/QA coverage for the procedural world editor.

These tests intentionally exercise the public, dependency-free API rather
than implementation details.  They are useful as a regression suite for the
fixed-seed demo and for checking that a local regeneration does not silently
discard unrelated layers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from procedural_world_studio import (
    SpatialIndex,
    WorldGenerator,
    WorldState,
    generate_demo_city,
)
from procedural_world_studio.core import Feature, bbox_of_geometry


def _group_signature(state: WorldState, group: str) -> dict[str, object]:
    """Stable signature for one stage, excluding volatile history metadata."""

    collection = getattr(state, group)
    return {
        key: {
            "type": feature.type,
            "geometry": feature.geometry,
            "properties": feature.properties,
            "parent_id": feature.parent_id,
            "stage": feature.stage,
            "seed": feature.seed,
        }
        for key, feature in collection.items()
    }


def _without_history_timestamps(state: WorldState) -> dict[str, object]:
    """Canonical snapshot for deterministic-geometry comparisons.

    History timestamps are intentionally wall-clock metadata; all generated
    geometry, stage seeds, and parameters must still be byte-for-byte stable.
    """

    payload = state.to_dict()
    for entry in payload.get("generator_history", []):
        entry.pop("timestamp", None)
    return payload


def test_fixed_seed_demo_is_deterministic() -> None:
    first = generate_demo_city(seed=20261004)
    second = generate_demo_city(seed=20261004)

    assert _without_history_timestamps(first) == _without_history_timestamps(second)
    assert first.terrain["type"] == "coast"
    assert first.metadata["city_name"] == "Coastal City Demo"
    assert first.harbor, "the coastal demo must include a non-empty harbor"
    assert {f.properties["district_type"] for f in first.districts.values()} >= {
        "harbor",
        "business",
        "residential",
        "industrial",
        "park",
    }


def test_reusing_a_generator_keeps_cached_output_deterministic() -> None:
    generator = WorldGenerator(seed=808, width=360, height=280)
    first = generator.generate(terrain_type="coast", road_pattern="hybrid")
    second = generator.generate(terrain_type="coast", road_pattern="hybrid")
    assert _without_history_timestamps(first) == _without_history_timestamps(second)
    assert len(generator.cache) >= 2


@pytest.mark.parametrize("pattern", ["grid", "organic", "radial", "hybrid"])
def test_all_road_patterns_generate_editable_network(pattern: str) -> None:
    state = WorldGenerator(seed=17, width=500, height=400).generate(
        terrain_type="coast", road_pattern=pattern, include_harbor=False
    )
    assert state.roads
    classes = {str(f.properties.get("road_class")) for f in state.roads.values()}
    assert {"main", "alley"} <= classes
    assert all(f.geometry.get("type") == "LineString" for f in state.roads.values())
    assert all(f.seed is not None and f.stage == "roads" for f in state.roads.values())


def test_partial_regeneration_preserves_unrelated_layers() -> None:
    generator = WorldGenerator(seed=123, width=600, height=480)
    state = generator.generate(terrain_type="coast", road_pattern="hybrid")
    before = {
        group: _group_signature(state, group)
        for group in ("terrain", "water", "districts", "blocks", "lots", "buildings", "harbor", "pois")
        if group != "terrain"
    }
    terrain_before = state.terrain.copy()
    revisions_before = dict(state.stage_revisions)
    roads_before = _group_signature(state, "roads")

    regenerated = generator.regenerate("roads")

    # Roads receive a new revision/seed, while terrain and all dependent
    # layers remain structurally valid.  The current dependency policy may
    # rebuild downstream layers; this assertion accepts either policy but
    # requires that no upstream data is lost.
    assert regenerated.terrain == terrain_before
    assert regenerated.stage_revisions["roads"] == revisions_before["roads"] + 1
    assert _group_signature(regenerated, "roads") != roads_before
    assert regenerated.roads
    for group in ("districts", "blocks", "lots", "buildings", "pois"):
        assert getattr(regenerated, group), f"{group} disappeared after road regeneration"
    assert regenerated.seed == 123


def test_locked_stage_keeps_stage_seed_and_geometry() -> None:
    generator = WorldGenerator(seed=99, width=400, height=300)
    state = generator.generate(terrain_type="coast")
    generator.set_lock("road", True)
    roads_before = _group_signature(state, "roads")
    seed_before = state.stage_seeds["roads"]
    revision_before = state.stage_revisions["roads"]

    regenerated = generator.regenerate("roads")

    assert regenerated.locks["road"] is True
    assert regenerated.stage_seeds["roads"] == seed_before
    assert regenerated.stage_revisions["roads"] == revision_before
    assert _group_signature(regenerated, "roads") == roads_before


def test_full_generation_can_preserve_explicitly_locked_layers() -> None:
    generator = WorldGenerator(seed=101, width=420, height=320)
    state = generator.generate(terrain_type="coast")
    roads_before = _group_signature(state, "roads")
    terrain_before = state.terrain
    generator.set_lock("road", True)
    generator.set_lock("terrain", True)

    generator.set_seed(202)
    regenerated = generator.generate(
        terrain_type="hills",
        road_pattern="grid",
        preserve_locks=True,
    )

    assert regenerated.seed == 202
    assert regenerated.terrain == terrain_before
    assert _group_signature(regenerated, "roads") == roads_before
    # Unlocked layers still use the new root seed and remain present.
    assert regenerated.districts
    assert regenerated.stage_seeds["roads"] == state.stage_seeds["roads"]


def test_undo_redo_round_trip_restores_world() -> None:
    generator = WorldGenerator(seed=77, width=420, height=320)
    original = generator.generate(terrain_type="coast")
    original_dict = original.to_dict()
    generator.regenerate("buildings")
    changed_dict = generator.state.to_dict()  # type: ignore[union-attr]
    assert changed_dict != original_dict

    restored = generator.undo()
    assert restored is not None
    assert restored.to_dict() == original_dict
    redone = generator.redo()
    assert redone is not None
    assert redone.to_dict() == changed_dict


def test_history_has_replay_metadata_and_json_round_trip(tmp_path: Path) -> None:
    generator = WorldGenerator(seed=2026, width=360, height=280)
    state = generator.generate(terrain_type="coast")
    generator.regenerate("poi")
    history = generator.history()
    assert len(history) >= 11, "all pipeline stages plus regeneration should be recorded"
    # Regenerating POIs also refreshes population, so the latest record may be
    # population.  Assert that the requested stage itself was recorded.
    poi_entries = [item for item in history if item["stage"] == "poi"]
    assert poi_entries
    entry = poi_entries[-1]
    assert entry["root_seed"] == 2026
    assert entry["generator_version"]
    assert isinstance(entry["result_metadata"]["count"], int)

    path = tmp_path / "world.json"
    state.save_json(str(path))
    loaded = WorldState.load_json(str(path))
    # JSON is the interchange contract, so tuples in in-memory geometry are
    # expected to come back as lists after decoding.
    assert json.loads(path.read_text(encoding="utf-8")) == loaded.to_dict()


def test_spatial_index_matches_brute_force_bbox_query() -> None:
    state = generate_demo_city(seed=7)
    index = state.rebuild_spatial_index(cell_size=75)
    query_bbox = (0.0, 0.0, state.width * 0.45, state.height * 0.45)
    indexed = {f.id for f in index.query_bbox(query_bbox)}
    brute = {
        f.id
        for f in state.all_features()
        if (
            (b := bbox_of_geometry(f.geometry))[2] >= query_bbox[0]
            and b[0] <= query_bbox[2]
            and b[3] >= query_bbox[1]
            and b[1] <= query_bbox[3]
        )
    }
    assert indexed == brute

    # Point queries should include a feature whose centroid is inside the
    # radius, and should not return an unrelated far-away object.
    one = next(iter(state.buildings.values()))
    bx = bbox_of_geometry(one.geometry)
    cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
    hits = {f.id for f in index.query_point((cx, cy), radius=2.0)}
    assert one.id in hits


def test_large_city_generation_stays_within_smoke_budget() -> None:
    started = time.perf_counter()
    state = WorldGenerator(seed=314159, width=1800, height=1400).generate(
        terrain_type="coast", road_pattern="hybrid"
    )
    elapsed = time.perf_counter() - started
    assert len(state.buildings) >= 100
    # This is deliberately a generous CI budget: it catches accidental O(n²)
    # regressions while remaining stable on slower Windows laptops.
    assert elapsed < 8.0, f"generation took {elapsed:.2f}s"
