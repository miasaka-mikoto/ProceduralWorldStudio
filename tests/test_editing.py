"""Regression coverage for direct, serialisable world edits."""

from __future__ import annotations

import math

from procedural_world_studio import WorldGenerator
from procedural_world_studio.core import bbox_of_geometry


def test_building_edit_moves_geometry_updates_volume_and_undoes() -> None:
    generator = WorldGenerator(seed=901, width=460, height=340)
    state = generator.generate(terrain_type="coast", include_harbor=True)
    building = next(iter(state.buildings.values()))
    original_bbox = bbox_of_geometry(building.geometry)
    original = state.to_dict()

    generator.move_feature(building.id, 3.5, -2.0)
    generator.update_feature(building.id, properties={"floors": 9})
    edited = generator.state
    assert edited is not None
    edited_building = edited.get_feature(building.id)
    assert edited_building is not None
    moved_bbox = bbox_of_geometry(edited_building.geometry)
    assert math.isclose(moved_bbox[0], original_bbox[0] + 3.5, abs_tol=1e-6)
    assert math.isclose(moved_bbox[1], original_bbox[1] - 2.0, abs_tol=1e-6)
    assert edited_building.properties["floors"] == 9
    assert edited_building.properties["height"] == 28.8
    assert edited_building.properties["editable_volume"]["height"] == 28.8
    assert edited.generator_history[-1]["stage"] == "edit"

    # Two editing operations create two reversible snapshots.
    assert generator.undo() is not None
    assert generator.undo() is not None
    assert generator.state is not None
    assert generator.state.to_dict() == original
