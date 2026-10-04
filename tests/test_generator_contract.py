"""Focused contracts for the deterministic world-generation engine.

The integration suite exercises the editor-facing lifecycle.  These tests stay
close to the generator's data contract: every pipeline object must remain
inspectable, linked to its parent, geometrically valid, and represented in the
reported metrics.  Keeping this coverage separate makes failures easier to
attribute to a generator stage instead of an exporter or GUI adapter.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping

import pytest

from procedural_world_studio import WorldGenerator, export_all
from procedural_world_studio.core import (
    STAGES,
    bbox_of_geometry,
    line_length,
    polygon_area,
    stable_seed,
)


@pytest.fixture(scope="module")
def coastal_city():
    """A modest fixed world keeps this contract suite quick on Windows CI."""

    return WorldGenerator(seed=1337, width=480, height=360).generate(
        terrain_type="coast", road_pattern="hybrid", include_harbor=True
    )


def _feature_groups(state) -> Iterable[tuple[str, Mapping[str, object]]]:
    for group_name in ("water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois"):
        yield group_name, getattr(state, group_name)


def _coordinates(feature) -> list[tuple[float, float]]:
    """Flatten the simple geometries produced by the core generator."""

    geometry = feature.geometry
    kind = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if kind == "Point":
        return [tuple(coords[:2])]
    if kind == "LineString":
        return [tuple(point[:2]) for point in coords]
    if kind == "Polygon":
        return [tuple(point[:2]) for ring in coords for point in ring]
    if kind == "MultiLineString":
        return [tuple(point[:2]) for line in coords for point in line]
    return []


def test_pipeline_objects_are_unique_linked_and_provenanced(coastal_city) -> None:
    state = coastal_city
    all_ids: list[str] = []
    for group_name, collection in _feature_groups(state):
        for feature_id, feature in collection.items():
            assert feature_id == feature.id
            assert feature.id not in all_ids, f"duplicate feature id: {feature.id}"
            all_ids.append(feature.id)
            assert feature.stage in STAGES
            assert isinstance(feature.seed, int)

            # Dependency edges are part of the editable world graph.  Harbor
            # and road objects may be roots, while parcels/buildings/POIs must
            # point into the preceding stage.
            if group_name == "blocks":
                assert feature.parent_id in state.districts
            elif group_name == "lots":
                assert feature.parent_id in state.blocks
            elif group_name == "buildings":
                assert feature.parent_id in state.lots
            elif group_name == "pois":
                assert feature.parent_id in state.districts

    assert len(all_ids) == len(set(all_ids))
    assert set(state.stage_seeds) >= {"terrain", "water", "roads", "districts", "buildings", "poi"}


def test_non_water_city_geometry_stays_inside_editable_world(coastal_city) -> None:
    state = coastal_city
    eps = 1e-6
    for group_name in ("roads", "districts", "blocks", "lots", "buildings", "pois"):
        for feature in getattr(state, group_name).values():
            xmin, ymin, xmax, ymax = bbox_of_geometry(feature.geometry)
            assert xmin >= -eps, (group_name, feature.id, xmin)
            assert ymin >= -eps, (group_name, feature.id, ymin)
            assert xmax <= state.width + eps, (group_name, feature.id, xmax)
            assert ymax <= state.height + eps, (group_name, feature.id, ymax)


def test_statistics_match_feature_geometry_and_population(coastal_city) -> None:
    state = coastal_city
    stats = WorldGenerator(seed=0).statistics(state)
    expected_road_length = sum(
        line_length(feature.geometry["coordinates"])
        for feature in state.roads.values()
    )
    expected_district_area = sum(
        polygon_area(feature.geometry["coordinates"][0])
        for feature in state.districts.values()
    )
    heights = [float(feature.properties["height"]) for feature in state.buildings.values()]

    assert stats["road_length"] == round(expected_road_length, 2)
    assert stats["district_area"] == round(expected_district_area, 2)
    assert stats["building_count"] == len(state.buildings)
    assert stats["lot_count"] == len(state.lots)
    assert stats["district_count"] == len(state.districts)
    assert stats["poi_count"] == len(state.pois)
    assert stats["average_building_height"] == round(sum(heights) / len(heights), 2)
    assert stats["population"] == state.population["total_residents"]
    assert stats["jobs"] == state.population["total_jobs"]
    assert stats["road_length"] > 0
    assert stats["district_area"] > 0


def test_harbor_is_a_structured_layout_not_untyped_blocks(coastal_city) -> None:
    harbor = coastal_city.harbor
    types = {feature.type for feature in harbor.values()}
    assert {"Coastline", "Dock", "Warehouse", "HarborRoad", "StationReserve", "CommercialArea"} <= types
    docks = [feature for feature in harbor.values() if feature.type == "Dock"]
    warehouses = [feature for feature in harbor.values() if feature.type == "Warehouse"]
    assert len(docks) == 4
    assert len(warehouses) == 5
    assert all(feature.properties.get("orientation") == "south" for feature in docks)
    assert all(feature.properties.get("rail_spur_reserved") is True for feature in warehouses)
    assert coastal_city.metadata["harbor"]["station_reserved"] is True
    assert coastal_city.metadata["harbor"]["dock_count"] == len(docks)
    commercial = harbor["harbor-commercial-area"]
    assert commercial.properties["road_access"] == "harbor-industrial-road"
    assert commercial.parent_id == "district-commercial-13"


@pytest.mark.parametrize("terrain_type", ["flat", "island", "coast", "river", "hills"])
def test_all_terrain_presets_produce_seeded_height_fields(terrain_type: str) -> None:
    state = WorldGenerator(seed=2468, width=240, height=180).generate(
        terrain_type=terrain_type, include_harbor=False
    )
    terrain = state.terrain
    heights = terrain["heights"]
    size = terrain["grid_size"]
    assert terrain["type"] == terrain_type
    assert len(heights) == size
    assert all(len(row) == size for row in heights)
    assert all(0.0 <= value <= 1.0 for row in heights for value in row)
    assert terrain["min"] <= terrain["max"]
    assert terrain["seed"] == state.stage_seeds["terrain"]
    assert state.water, f"{terrain_type} should expose a water/lake feature"

    # A second generator instance must reproduce the terrain exactly; the
    # canonical comparison deliberately ignores volatile history timestamps.
    again = WorldGenerator(seed=2468, width=240, height=180).generate(
        terrain_type=terrain_type, include_harbor=False
    )
    assert terrain == again.terrain
    assert stable_seed(terrain_type, 2468) == stable_seed(terrain_type, 2468)
    assert stable_seed(terrain_type, 2468) != stable_seed(terrain_type, 2469)


@pytest.mark.parametrize("pattern", ["grid", "organic", "radial", "hybrid"])
def test_road_pattern_changes_geometry_not_contract(pattern: str) -> None:
    state = WorldGenerator(seed=8128, width=360, height=260).generate(
        terrain_type="flat", road_pattern=pattern, include_harbor=False
    )
    classes = {feature.properties.get("road_class") for feature in state.roads.values()}
    assert {"main", "alley"} <= classes
    assert all(feature.properties.get("pattern") == pattern for feature in state.roads.values())
    assert all(feature.geometry.get("type") == "LineString" for feature in state.roads.values())
    assert all(line_length(feature.geometry["coordinates"]) > 0 for feature in state.roads.values())


def test_inspector_returns_parent_and_generation_parameters(coastal_city) -> None:
    state = coastal_city
    generator = WorldGenerator(seed=1337, width=480, height=360)
    generator.state = state
    building = next(iter(state.buildings.values()))
    inspected = generator.inspect(building.id)
    assert inspected is not None
    assert inspected["id"] == building.id
    assert inspected["type"] == "Building"
    assert inspected["stage"] == "buildings"
    assert inspected["parent"] == building.parent_id
    assert inspected["seed"] == building.seed
    assert inspected["world_seed"] == state.seed
    assert inspected["parameters"]["floors"] == building.properties["floors"]


def test_feature_dimensions_are_positive_and_building_volume_is_consistent(coastal_city) -> None:
    for feature in coastal_city.buildings.values():
        assert feature.properties["floors"] >= 1
        assert feature.properties["height"] > 0
        volume = feature.properties["editable_volume"]
        assert volume["width"] > 0
        assert volume["depth"] > 0
        assert volume["height"] == feature.properties["height"]
        assert math.isclose(
            feature.properties["height"], feature.properties["floors"] * 3.2, rel_tol=0, abs_tol=1e-9
        )


def test_generated_world_exports_complete_interchange_bundle(coastal_city, tmp_path) -> None:
    """Export the real generated state, not only a hand-written fixture."""

    paths = export_all(coastal_city, tmp_path, prefix="coastal_city", png_size=(240, 180))
    assert set(paths) == {"json", "geojson", "png", "obj", "gltf"}
    assert all(path.exists() and path.stat().st_size > 0 for path in paths.values())

    import json

    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    geojson = json.loads(paths["geojson"].read_text(encoding="utf-8"))
    gltf = json.loads(paths["gltf"].read_text(encoding="utf-8"))
    assert payload["schema"] == "procedural-world-studio"
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == sum(
        len(payload.get(group, {}))
        for group in ("water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois")
    )
    assert gltf["asset"]["version"] == "2.0"
