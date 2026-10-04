"""Portable exporters for :mod:`procedural_world_studio` worlds.

The generator intentionally has no dependency on a particular rendering
engine.  This module is the small bridge between the serialisable world model
and common interchange formats used by GIS and DCC tools:

* JSON keeps the complete generator state and is suitable for round-trips.
* GeoJSON exposes every world feature as a ``FeatureCollection``.
* PNG is a quick, deterministic top-down map preview (Pillow only).
* OBJ and glTF contain a lightweight extruded representation of buildings,
  lots, districts and other polygon features.

The exporters accept a ``WorldState`` instance, a mapping returned by
``WorldState.to_dict()``, or a list of feature-like dictionaries.  Keeping the
adapter permissive is useful while a project evolves and also makes the
formatters convenient in scripts and tests.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
import mimetypes
import os
import struct
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:  # Pillow is present in the desktop/runtime bundle, but stay import-safe.
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - exercised only on minimal installations
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


EXPORT_VERSION = "0.1"


# ---------------------------------------------------------------------------
# Generic model adapter
# ---------------------------------------------------------------------------

def _convert(value: Any) -> Any:
    """Convert model/dataclass values to JSON-compatible Python values."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _convert(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _convert(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        # JSON cannot represent non-finite values.  Null is a safer interchange
        # value than emitting invalid JSON.
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_convert(v) for v in value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        try:
            return [_convert(v) for v in value]
        except TypeError:
            pass
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _convert(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _convert(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def world_payload(world: Any) -> dict[str, Any]:
    """Return a JSON-compatible dictionary for ``world``.

    ``WorldState.to_dict`` is preferred so generator metadata and lock state are
    retained.  A mapping is copied recursively; feature lists are wrapped in a
    stable ``entities`` field when necessary.
    """

    if isinstance(world, Mapping):
        payload = _convert(world)
    elif hasattr(world, "to_dict") and callable(world.to_dict):
        payload = _convert(world.to_dict())
    else:
        payload = _convert(world)
    if isinstance(payload, list):
        payload = {"entities": payload}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    # Keep a small explicit export marker without changing generator content.
    payload.setdefault("schema", "procedural-world-studio")
    payload.setdefault("export_version", EXPORT_VERSION)
    return payload


def _entity_items(world: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extract ``(id, feature)`` pairs from common world model layouts."""

    payload = world_payload(world)
    raw: Any = None
    for key in ("entities", "features", "objects", "items"):
        if key in payload:
            raw = payload[key]
            break
    if isinstance(raw, Mapping) and raw.get("type") == "FeatureCollection":
        raw = raw.get("features", [])
    if raw is None:
        # WorldState stores entities in stage-specific dictionaries.  Flatten
        # those groups while retaining the stage on each feature.  This branch
        # is the normal path for the built-in generator and is intentionally
        # ordered to keep exports deterministic.
        stage_keys = ("water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois")
        grouped: list[tuple[str, Any]] = []
        for stage in stage_keys:
            value = payload.get(stage)
            if isinstance(value, Mapping):
                if stage == "harbor" and any(key in value for key in ("docks", "warehouses", "station_reservation", "coastline")):
                    # The alternate dictionary-based engine keeps harbor
                    # subcollections nested instead of using a flat Feature
                    # dictionary.  Promote only actual geometric objects.
                    coast = value.get("coastline")
                    if coast:
                        grouped.append(("harbor-coastline", {"id": "harbor-coastline", "type": "Coastline", "geometry": coast, "stage": "harbor"}))
                    for subkey in ("docks", "warehouses"):
                        subitems = value.get(subkey, [])
                        if isinstance(subitems, Sequence) and not isinstance(subitems, (str, bytes, bytearray)):
                            grouped.extend((str(item.get("id", f"harbor-{subkey}-{index}")) if isinstance(item, Mapping) else f"harbor-{subkey}-{index}", item) for index, item in enumerate(subitems, 1))
                    reservation = value.get("station_reservation")
                    if isinstance(reservation, Mapping):
                        grouped.append((str(reservation.get("id", "harbor-station-reservation")), reservation))
                else:
                    grouped.extend((str(key), item) for key, item in value.items())
        if grouped:
            raw = grouped
        else:
            raw = None
    if isinstance(raw, list) and raw and all(isinstance(v, tuple) and len(v) == 2 for v in raw):
        # Already flattened ``(id, value)`` pairs from stage dictionaries.
        iterator = raw
    elif raw is None:
        # A single feature mapping is also useful in tiny examples.
        if any(k in payload for k in ("geometry", "coordinates", "footprint", "x", "y")):
            raw = [payload]
        else:
            raw = []
        iterator = enumerate(raw)
    elif isinstance(raw, Mapping):
        iterator = raw.items()
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        iterator = enumerate(raw)
    else:
        iterator = []
    pairs: list[tuple[str, dict[str, Any]]] = []
    for key, value in iterator:
        item = _convert(value)
        if not isinstance(item, dict):
            item = {"value": item}
        fid = item.get("id", item.get("uid", item.get("feature_id", key)))
        item.setdefault("id", str(fid))
        pairs.append((str(fid), item))
    return pairs


def _feature_kind(item: Mapping[str, Any]) -> str:
    props = item.get("properties")
    if isinstance(props, Mapping):
        for key in ("type", "feature_type", "kind", "zone", "category"):
            if props.get(key) is not None:
                return str(props[key]).lower()
    for key in ("type", "feature_type", "kind", "category", "zone"):
        value = item.get(key)
        if value is not None and not isinstance(value, Mapping):
            return str(value).lower()
    return "feature"


def _geometry(item: Mapping[str, Any]) -> Any:
    """Find a geometry in a feature and normalize common shorthand."""

    geometry = item.get("geometry")
    if isinstance(geometry, Mapping):
        if "type" in geometry and "coordinates" in geometry:
            return dict(geometry)
        for key in ("polygon", "footprint", "shape", "path", "line", "points"):
            if key in geometry:
                geometry = geometry[key]
                break
    if geometry is None:
        for key in ("polygon", "footprint", "shape", "path", "line", "points", "coordinates"):
            if key in item:
                geometry = item[key]
                break
    if geometry is None and item.get("x") is not None and item.get("y") is not None:
        return {"type": "Point", "coordinates": [item["x"], item["y"]]}
    if geometry is None:
        return None
    # Already in GeoJSON geometry form.
    if isinstance(geometry, Mapping) and "type" in geometry and "coordinates" in geometry:
        return dict(geometry)
    return _infer_geometry(geometry, _feature_kind(item))


def _is_num_pair(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) >= 2
        and all(isinstance(v, (int, float)) for v in value[:2])
    )


def _infer_geometry(coords: Any, kind: str = "feature") -> dict[str, Any] | None:
    """Infer a GeoJSON geometry from nested coordinate shorthand."""

    if _is_num_pair(coords):
        return {"type": "Point", "coordinates": [coords[0], coords[1]]}
    if not isinstance(coords, Sequence) or isinstance(coords, (str, bytes, bytearray)):
        return None
    coords = _convert(coords)
    if not coords:
        return None
    # A list of coordinate pairs is either a line or polygon.  Roads and paths
    # are lines; everything else is a footprint polygon (closed below).
    if all(_is_num_pair(p) for p in coords):
        typ = "LineString" if any(w in kind for w in ("road", "street", "alley", "river", "path", "rail", "coast", "dock")) else "Polygon"
        if typ == "Polygon":
            ring = [[p[0], p[1]] for p in coords]
            if ring[0] != ring[-1]:
                ring.append(ring[0][:])
            return {"type": typ, "coordinates": [ring]}
        return {"type": typ, "coordinates": [[p[0], p[1]] for p in coords]}
    # A nested list of rings is normally a polygon.  If there is one extra
    # level, preserve it as MultiPolygon when appropriate.
    if all(isinstance(r, Sequence) and r and all(_is_num_pair(p) for p in r) for r in coords):
        rings: list[list[list[float]]] = []
        for ring in coords:
            out = [[p[0], p[1]] for p in ring]
            if out[0] != out[-1]:
                out.append(out[0][:])
            rings.append(out)
        return {"type": "Polygon", "coordinates": rings}
    if all(isinstance(poly, Sequence) for poly in coords):
        polygons = []
        for poly in coords:
            geom = _infer_geometry(poly, "feature")
            if geom and geom["type"] == "Polygon":
                polygons.append(geom["coordinates"])
        if polygons:
            return {"type": "MultiPolygon", "coordinates": polygons}
    return None


def _properties(item: Mapping[str, Any]) -> dict[str, Any]:
    skip = {"geometry", "coordinates", "polygon", "footprint", "shape", "path", "line", "points"}
    props: dict[str, Any] = {}
    nested = item.get("properties")
    if isinstance(nested, Mapping):
        props.update(_convert(nested))
    # A GeoJSON Feature's top-level ``type`` must literally be ``Feature``;
    # preserve the world object's semantic type under an unambiguous property.
    if item.get("type") is not None:
        props.setdefault("feature_type", _convert(item.get("type")))
    for key, value in item.items():
        if key in skip or key in {"id", "uid", "feature_id", "type"}:
            continue
        if key == "properties":
            continue
        # Keep scalar/structured parameters; geometry-like nested values are
        # deliberately omitted to avoid duplicating large coordinate arrays.
        if key not in props:
            props[key] = _convert(value)
    return props


def _geojson_feature(fid: str, item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": fid,
        "geometry": _geometry(item),
        "properties": _properties(item),
    }


def world_to_geojson(world: Any) -> dict[str, Any]:
    """Build a GeoJSON FeatureCollection from a world."""

    payload = world_payload(world)
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {"value": metadata}
    collection_name = payload.get("name") or metadata.get("city_name") or payload.get("project_name") or payload.get("project") or "Procedural World"
    return {
        "type": "FeatureCollection",
        "name": collection_name,
        "metadata": _convert(metadata),
        "features": [_geojson_feature(fid, item) for fid, item in _entity_items(payload)],
    }


# ---------------------------------------------------------------------------
# Public JSON / GeoJSON functions
# ---------------------------------------------------------------------------

def _write_json(data: Any, path: os.PathLike[str] | str, *, indent: int = 2) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=indent, allow_nan=False) + "\n", encoding="utf-8")
    return output


def export_json(world: Any, path: os.PathLike[str] | str, *, indent: int = 2) -> Path:
    """Export complete generator state as deterministic UTF-8 JSON."""

    return _write_json(world_payload(world), path, indent=indent)


def export_geojson(world: Any, path: os.PathLike[str] | str, *, indent: int = 2) -> Path:
    """Export world entities as a GeoJSON FeatureCollection."""

    return _write_json(world_to_geojson(world), path, indent=indent)


# ---------------------------------------------------------------------------
# Geometry helpers used by PNG/OBJ/glTF
# ---------------------------------------------------------------------------

def _rings(geometry: Mapping[str, Any] | None) -> list[list[tuple[float, float]]]:
    if not geometry:
        return []
    typ = str(geometry.get("type", ""))
    coords = geometry.get("coordinates")
    if typ == "Point" and _is_num_pair(coords):
        return [[(float(coords[0]), float(coords[1]))]]
    if typ in ("LineString", "MultiPoint") and isinstance(coords, Sequence):
        return [[(float(p[0]), float(p[1])) for p in coords if _is_num_pair(p)]]
    if typ == "Polygon" and isinstance(coords, Sequence):
        return [[(float(p[0]), float(p[1])) for p in ring if _is_num_pair(p)] for ring in coords]
    if typ == "MultiPolygon" and isinstance(coords, Sequence):
        return [
            [(float(p[0]), float(p[1])) for p in ring if _is_num_pair(p)]
            for poly in coords
            for ring in (poly[0] if isinstance(poly, Sequence) and poly else [])
        ]
    return []


def _all_geometry_features(world: Any) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    result = []
    for fid, item in _entity_items(world):
        result.append((fid, item, _geometry(item)))
    return result


def _bounds(features: Sequence[tuple[str, dict[str, Any], dict[str, Any] | None]]) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for _, _, geometry in features:
        for ring in _rings(geometry):
            points.extend(ring)
    if not points:
        return (0.0, 0.0, 100.0, 100.0)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    # Avoid zero-size transforms for a point-only world.
    if maxx - minx < 1e-9:
        minx -= 50.0
        maxx += 50.0
    if maxy - miny < 1e-9:
        miny -= 50.0
        maxy += 50.0
    pad = max(maxx - minx, maxy - miny) * 0.04
    return minx - pad, miny - pad, maxx + pad, maxy + pad


def _height(item: Mapping[str, Any]) -> float:
    properties = item.get("properties") if isinstance(item.get("properties"), Mapping) else {}
    for key in ("height", "building_height", "z", "elevation"):
        value = item.get(key, properties.get(key))
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
    params = item.get("parameters")
    if isinstance(params, Mapping):
        for key in ("height", "building_height"):
            if isinstance(params.get(key), (int, float)):
                return max(0.0, float(params[key]))
        floors = params.get("floors")
    else:
        floors = item.get("floors", properties.get("floors"))
    if isinstance(floors, (int, float)):
        return max(0.0, float(floors) * 3.0)
    return 0.0


def _is_building(item: Mapping[str, Any]) -> bool:
    kind = _feature_kind(item)
    return any(w in kind for w in ("building", "house", "warehouse", "station", "school", "hospital", "mall", "government", "temple", "cafe"))


def _map_color(item: Mapping[str, Any]) -> tuple[int, int, int, int]:
    kind = _feature_kind(item)
    if any(w in kind for w in ("water", "river", "coast", "sea")):
        return (69, 139, 201, 210)
    if "road" in kind or any(w in kind for w in ("street", "alley", "rail")):
        return (90, 90, 90, 255)
    if any(w in kind for w in ("park", "green", "forest")):
        return (88, 170, 92, 190)
    if "industrial" in kind or "warehouse" in kind:
        return (197, 145, 89, 230)
    if any(w in kind for w in ("commercial", "business", "mall")):
        return (232, 179, 77, 230)
    if any(w in kind for w in ("residential", "house", "building")):
        return (205, 117, 117, 230)
    if "district" in kind:
        return (180, 180, 180, 80)
    if any(w in kind for w in ("poi", "station", "school", "hospital", "government", "temple", "cafe")):
        return (151, 75, 183, 255)
    return (130, 130, 130, 180)


def export_png(
    world: Any,
    path: os.PathLike[str] | str,
    *,
    width: int = 1600,
    height: int = 1000,
    background: tuple[int, int, int, int] = (245, 242, 232, 255),
) -> Path:
    """Render a deterministic top-down PNG map using Pillow."""

    if Image is None or ImageDraw is None:
        raise RuntimeError("PNG export requires Pillow (pip install Pillow)")
    width, height = max(1, int(width)), max(1, int(height))
    features = _all_geometry_features(world)
    minx, miny, maxx, maxy = _bounds(features)
    sx = (width - 20) / (maxx - minx)
    sy = (height - 20) / (maxy - miny)
    scale = min(sx, sy)
    ox = (width - (maxx - minx) * scale) / 2.0
    oy = (height - (maxy - miny) * scale) / 2.0

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return (ox + (x - minx) * scale, height - (oy + (y - miny) * scale))

    image = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(image, "RGBA")
    # Paint broad polygons first, then lines and point features on top.
    def _paint_rank(row: tuple[str, dict[str, Any], dict[str, Any] | None]) -> tuple[int, str]:
        kind = _feature_kind(row[1])
        if any(w in kind for w in ("terrain", "water", "sea", "river", "lake")):
            rank = 0
        elif "district" in kind or "park" in kind:
            rank = 1
        elif any(w in kind for w in ("block", "lot")):
            rank = 2
        elif any(w in kind for w in ("road", "street", "alley", "rail", "dock")):
            rank = 3
        elif _is_building(row[1]):
            rank = 4
        else:
            rank = 5
        return rank, row[0]

    ordered = sorted(features, key=_paint_rank)
    for _, item, geometry in ordered:
        rings = _rings(geometry)
        if not rings:
            continue
        color = _map_color(item)
        typ = str((geometry or {}).get("type", ""))
        if typ in ("Polygon", "MultiPolygon"):
            for ring in rings:
                if len(ring) >= 3:
                    draw.polygon([transform(p) for p in ring], fill=color)
                    # Thin outlines improve editability/readability at overview scale.
                    draw.line([transform(p) for p in ring], fill=(40, 40, 40, 100), width=max(1, width // 1200))
        elif typ in ("LineString", "MultiLineString"):
            for ring in rings:
                if len(ring) >= 2:
                    line_width = 4 if "main" in _feature_kind(item) else 2
                    draw.line([transform(p) for p in ring], fill=color, width=line_width, joint="curve")
        else:
            for ring in rings:
                if ring:
                    x, y = transform(ring[0])
                    radius = 5 if _is_building(item) else 4
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=(30, 30, 30, 220))
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, format="PNG", optimize=False)
    return output


# ---------------------------------------------------------------------------
# OBJ exporter
# ---------------------------------------------------------------------------

def _polygon_rings(geometry: Mapping[str, Any] | None) -> list[list[tuple[float, float]]]:
    return [ring for ring in _rings(geometry) if len(ring) >= 3]


def _add_obj_polygon(vertices: list[tuple[float, float, float]], faces: list[tuple[int, ...]], ring: list[tuple[float, float]], z: float, *, top: bool = False) -> None:
    start = len(vertices) + 1
    vertices.extend((x, y, z) for x, y in ring)
    # Fan triangulation is deterministic and handles the simple footprints used
    # by the procedural generator.  Reverse the bottom winding.
    for i in range(1, len(ring) - 1):
        tri = (start, start + i, start + i + 1)
        faces.append(tri if top else (tri[0], tri[2], tri[1]))


def world_to_obj(world: Any) -> str:
    """Return a Wavefront OBJ string with extruded polygon features."""

    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    lines: list[tuple[int, ...]] = []
    groups: list[tuple[str, list[int], list[int]]] = []
    for fid, item, geometry in _all_geometry_features(world):
        rings = _polygon_rings(geometry)
        typ = str((geometry or {}).get("type", ""))
        before_v, before_f = len(vertices), len(faces)
        h = _height(item) if _is_building(item) else 0.0
        if rings:
            for ring in rings:
                # Remove repeated closing point for clean side quads.
                if len(ring) > 1 and ring[0] == ring[-1]:
                    ring = ring[:-1]
                if len(ring) < 3:
                    continue
                _add_obj_polygon(vertices, faces, ring, 0.0, top=False)
                if h > 0:
                    base = len(vertices) - len(ring)
                    top_start = len(vertices) + 1
                    vertices.extend((x, y, h) for x, y in ring)
                    # top
                    for i in range(1, len(ring) - 1):
                        faces.append((top_start, top_start + i + 1, top_start + i))
                    # walls
                    for i in range(len(ring)):
                        j = (i + 1) % len(ring)
                        faces.append((base + i + 1, base + j + 1, top_start + j, top_start + i))
        elif typ in ("LineString", "MultiLineString"):
            for ring in _rings(geometry):
                if len(ring) >= 2:
                    start = len(vertices) + 1
                    vertices.extend((x, y, 0.05) for x, y in ring)
                    lines.append(tuple(start + i for i in range(len(ring))))
        if len(vertices) > before_v or len(faces) > before_f:
            groups.append((fid, list(range(before_v + 1, len(vertices) + 1)), list(range(before_f + 1, len(faces) + 1))))

    out: list[str] = ["# Procedural World Studio OBJ export", f"# vertices={len(vertices)} faces={len(faces)}"]
    for v in vertices:
        out.append(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
    # Emit each feature's faces under its own group.  Keeping the ranges from
    # the construction pass makes the OBJ immediately useful in Blender while
    # retaining compact, globally indexed vertices.
    for fid, _, face_indices in groups:
        out.append(f"g {str(fid).replace(' ', '_')}")
        for face_index in face_indices:
            out.append("f " + " ".join(str(i) for i in faces[face_index - 1]))
    for line in lines:
        out.append("l " + " ".join(str(i) for i in line))
    return "\n".join(out) + "\n"


def export_obj(world: Any, path: os.PathLike[str] | str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(world_to_obj(world), encoding="utf-8")
    return output


# ---------------------------------------------------------------------------
# glTF 2.0 exporter (embedded binary buffer)
# ---------------------------------------------------------------------------

def _triangulated_mesh(world: Any) -> tuple[list[float], list[int]]:
    positions: list[float] = []
    indices: list[int] = []

    def add_tri(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> None:
        base = len(positions) // 3
        positions.extend((*a, *b, *c))
        indices.extend((base, base + 1, base + 2))

    for _, item, geometry in _all_geometry_features(world):
        h = _height(item) if _is_building(item) else 0.0
        for ring in _polygon_rings(geometry):
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]
            if len(ring) < 3:
                continue
            for i in range(1, len(ring) - 1):
                add_tri((ring[0][0], ring[0][1], 0.0), (ring[i][0], ring[i][1], 0.0), (ring[i + 1][0], ring[i + 1][1], 0.0))
                if h > 0:
                    add_tri((ring[0][0], ring[0][1], h), (ring[i + 1][0], ring[i + 1][1], h), (ring[i][0], ring[i][1], h))
                    for j in range(len(ring)):
                        k = (j + 1) % len(ring)
                        a, b = ring[j], ring[k]
                        add_tri((a[0], a[1], 0.0), (b[0], b[1], 0.0), (b[0], b[1], h))
                        add_tri((a[0], a[1], 0.0), (b[0], b[1], h), (a[0], a[1], h))
    return positions, indices


def world_to_gltf(world: Any) -> dict[str, Any]:
    """Build a self-contained glTF 2.0 dictionary with a data URI buffer."""

    positions, indices = _triangulated_mesh(world)
    if not positions:
        positions = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        indices = [0, 1, 2]
    pos_bytes = struct.pack("<" + "f" * len(positions), *positions)
    # glTF accessors require 4-byte alignment.  Add padding between buffers.
    idx_offset = (len(pos_bytes) + 3) & ~3
    blob = pos_bytes + b"\x00" * (idx_offset - len(pos_bytes)) + struct.pack("<" + "I" * len(indices), *indices)
    min_pos = [min(positions[i::3]) for i in range(3)]
    max_pos = [max(positions[i::3]) for i in range(3)]
    uri = "data:application/octet-stream;base64," + base64.b64encode(blob).decode("ascii")
    return {
        "asset": {"version": "2.0", "generator": "Procedural World Studio " + EXPORT_VERSION},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "ProceduralWorld", "mesh": 0}],
        "meshes": [{"name": "WorldGeometry", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(blob), "uri": uri}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": idx_offset, "byteLength": len(blob) - idx_offset, "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions) // 3, "type": "VEC3", "min": min_pos, "max": max_pos},
            {"bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"},
        ],
        "extras": {"featureCount": len(_entity_items(world))},
    }


def export_gltf(world: Any, path: os.PathLike[str] | str, *, indent: int = 2) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(world_to_gltf(world), ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    return output


# ---------------------------------------------------------------------------
# Convenience bundle / compatibility aliases
# ---------------------------------------------------------------------------

def export_all(world: Any, directory: os.PathLike[str] | str, *, prefix: str = "world", png_size: tuple[int, int] = (1600, 1000)) -> dict[str, Path]:
    """Export all supported formats and return paths keyed by format."""

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    width, height = png_size
    return {
        "json": export_json(world, out / f"{prefix}.json"),
        "geojson": export_geojson(world, out / f"{prefix}.geojson"),
        "png": export_png(world, out / f"{prefix}.png", width=width, height=height),
        "obj": export_obj(world, out / f"{prefix}.obj"),
        "gltf": export_gltf(world, out / f"{prefix}.gltf"),
    }


# Friendly names used by scripts from early prototypes.
export_map_png = export_png
export_geo_json = export_geojson
export_glTF = export_gltf


__all__ = [
    "EXPORT_VERSION",
    "world_payload",
    "world_to_geojson",
    "world_to_obj",
    "world_to_gltf",
    "export_json",
    "export_geojson",
    "export_png",
    "export_map_png",
    "export_obj",
    "export_gltf",
    "export_all",
]
