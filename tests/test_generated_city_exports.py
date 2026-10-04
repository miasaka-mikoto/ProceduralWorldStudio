"""Exercise every export format against a real generated city."""

from __future__ import annotations

import json
from pathlib import Path

from procedural_world_studio import generate_demo_city
from procedural_world_studio.export_formats import export_all, export_geojson


def test_demo_city_export_bundle_contains_real_geometry(tmp_path: Path) -> None:
    world = generate_demo_city(seed=20261004)
    outputs = export_all(world, tmp_path, prefix="coastal_city", png_size=(320, 240))

    assert set(outputs) == {"json", "geojson", "png", "obj", "gltf"}
    assert all(path.exists() and path.stat().st_size > 0 for path in outputs.values())

    payload = json.loads(outputs["json"].read_text(encoding="utf-8"))
    # Built-in WorldState uses stage dictionaries rather than a flat entity
    # array; ensure export preserves all major layers and provenance.
    assert payload["seed"] == 20261004
    assert payload["buildings"]
    assert payload["harbor"]
    assert payload["generator_history"]

    geo = json.loads(outputs["geojson"].read_text(encoding="utf-8"))
    assert geo["type"] == "FeatureCollection"
    assert len(geo["features"]) >= len(world.buildings) + len(world.roads)
    ids = {str(feature["id"]) for feature in geo["features"]}
    assert "harbor-dock-1" in ids

    png_header = outputs["png"].read_bytes()[:8]
    assert png_header == b"\x89PNG\r\n\x1a\n"
    obj_text = outputs["obj"].read_text(encoding="utf-8")
    assert "# Procedural World Studio OBJ export" in obj_text
    assert obj_text.count("\nv ") > 100
    gltf = json.loads(outputs["gltf"].read_text(encoding="utf-8"))
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["extras"]["featureCount"] >= len(world.buildings)


def test_geojson_has_valid_geometry_for_points_lines_and_polygons() -> None:
    geo = export_geojson(generate_demo_city(seed=31), "/tmp/procedural_world_test.geojson")
    data = json.loads(geo.read_text(encoding="utf-8"))
    kinds = {feature["geometry"]["type"] for feature in data["features"] if feature["geometry"]}
    assert {"Point", "LineString", "Polygon"} <= kinds
    for feature in data["features"]:
        geometry = feature["geometry"]
        if geometry is None:
            continue
        assert geometry["type"] in {"Point", "LineString", "Polygon", "MultiPolygon"}
        assert feature["id"]

