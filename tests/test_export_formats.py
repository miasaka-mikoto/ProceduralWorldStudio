"""Smoke tests for all interchange exporters.

The fixture intentionally uses plain dictionaries: exporters are required to
work before/without a GUI and remain useful for external generators.
"""

import json

from procedural_world_studio.export_formats import (
    export_all,
    export_geojson,
    export_gltf,
    export_json,
    export_obj,
    export_png,
    world_to_geojson,
)


def sample_world():
    return {
        "name": "Export Test City",
        "seed": 1234,
        "metadata": {"terrain": "coast", "generator_version": "test"},
        "entities": {
            "building-1": {
                "id": "building-1",
                "type": "building",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [20, 0], [20, 10], [0, 10], [0, 0]]],
                },
                "floors": 3,
            },
            "road-1": {
                "id": "road-1",
                "type": "main_road",
                "geometry": {"type": "LineString", "coordinates": [[-5, 5], [30, 5]]},
            },
            "park-1": {
                "id": "park-1",
                "type": "park",
                "footprint": [[25, 0], [35, 0], [35, 10], [25, 10]],
            },
        },
    }


def test_json_and_geojson_round_trip(tmp_path):
    world = sample_world()
    json_path = export_json(world, tmp_path / "city.json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["seed"] == 1234
    assert len(data["entities"]) == 3

    geo_path = export_geojson(world, tmp_path / "city.geojson")
    geo = json.loads(geo_path.read_text(encoding="utf-8"))
    assert geo["type"] == "FeatureCollection"
    assert len(geo["features"]) == 3
    assert geo["features"][0]["geometry"]["type"] == "Polygon"
    assert geo["features"][0]["properties"]["feature_type"] == "building"
    # The in-memory form should be equally valid for callers that stream data.
    assert world_to_geojson(world)["features"]


def test_png_obj_and_gltf_are_valid_files(tmp_path):
    world = sample_world()
    png = export_png(world, tmp_path / "city.png", width=320, height=200)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    obj = export_obj(world, tmp_path / "city.obj")
    obj_text = obj.read_text(encoding="utf-8")
    assert obj_text.startswith("# Procedural World Studio OBJ export")
    assert "v " in obj_text and "f " in obj_text

    gltf = export_gltf(world, tmp_path / "city.gltf")
    gltf_data = json.loads(gltf.read_text(encoding="utf-8"))
    assert gltf_data["asset"]["version"] == "2.0"
    assert gltf_data["buffers"][0]["uri"].startswith("data:application/octet-stream;base64,")
    assert gltf_data["meshes"][0]["primitives"][0]["mode"] == 4


def test_export_all_creates_demo_bundle(tmp_path):
    paths = export_all(sample_world(), tmp_path / "bundle", prefix="coastal_city", png_size=(128, 128))
    assert set(paths) == {"json", "geojson", "png", "obj", "gltf"}
    assert all(path.exists() and path.stat().st_size > 0 for path in paths.values())
