"""Desktop editor for Procedural World Studio.

The editor is deliberately a thin, well-tested shell around the procedural
generator.  It accepts ordinary dictionaries as well as dataclass/model
objects, which makes it possible to iterate on the generator without having
to rewrite the UI.  PySide6 is preferred on Windows; a small tkinter fallback
is provided for environments where Qt is not installed.

Useful non-GUI API (also used by smoke tests)::

    adapter = WorldAdapter()
    adapter.generate_sync(seed=42)
    adapter.regenerate("roads")
    adapter.export_json("city.json")

The GUI itself is started with ``python -m procedural_world_studio.ui_editor``.
Use ``--headless`` to generate a deterministic demo and export it without
opening a window.
"""

from __future__ import annotations

import argparse
import copy
import csv
import importlib
import json
import math
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Generic model/adapter layer
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> Any:
    """Convert model/dataclass objects to JSON-like values.

    The core generator has changed shape during development.  Keeping this
    conversion permissive means the UI can inspect objects from a dataclass,
    pydantic model, or a plain dictionary without importing that core package.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _as_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_as_dict(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _as_dict(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return _as_dict(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _as_dict(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    # Be friendly to camel/snake/case variations.
    normalized = {str(k).replace("_", "").lower(): v for k, v in mapping.items()}
    for key in keys:
        n = key.replace("_", "").lower()
        if n in normalized:
            return normalized[n]
    return default


def _point(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return (
            float(_first(value, "x", "lon", "lng", default=0.0) or 0.0),
            float(_first(value, "y", "lat", default=0.0) or 0.0),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) >= 2:
            return float(value[0]), float(value[1])
    return 0.0, 0.0


def _geometry_points(obj: Mapping[str, Any]) -> list[tuple[float, float]]:
    """Read common polygon/line geometry forms from one object."""

    geom = _first(obj, "geometry", "polygon", "points", "vertices", "path")
    geom_type = str(_first(geom, "type", default="")) if isinstance(geom, Mapping) else ""
    if isinstance(geom, Mapping):
        coords = _first(geom, "coordinates", "points", "vertices", default=[])
    else:
        coords = geom
    if not isinstance(coords, Sequence) or isinstance(coords, (str, bytes)):
        # A rectangle represented by x/y/width/height is common in prototypes.
        x = float(_first(obj, "x", default=0) or 0)
        y = float(_first(obj, "y", default=0) or 0)
        w = float(_first(obj, "width", "w", default=0) or 0)
        h = float(_first(obj, "height", "h", default=0) or 0)
        if w or h:
            return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return []
    if geom_type == "Point" and isinstance(coords, Sequence) and len(coords) >= 2:
        return [_point(coords)]
    if geom_type == "MultiLineString" and coords and isinstance(coords[0], Sequence):
        # The map painter currently draws one polyline per object; joining
        # segments is adequate for inspection and preserves all coordinates.
        coords = [p for line in coords for p in line]
    # GeoJSON polygon has one extra ring nesting level; LineString does not.
    if coords and isinstance(coords[0], Sequence) and coords[0] and isinstance(coords[0][0], Sequence):
        coords = coords[0]
    points: list[tuple[float, float]] = []
    for item in coords:
        try:
            points.append(_point(item))
        except Exception:
            continue
    return points


def _object_id(obj: Mapping[str, Any], fallback: str) -> str:
    value = _first(obj, "id", "ID", "uid", "uuid", default=fallback)
    return str(value)


def _object_type(obj: Mapping[str, Any], fallback: str = "Object") -> str:
    value = _first(obj, "type", "kind", "category", "object_type", default=fallback)
    return str(value)


def _flatten_objects(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return drawable objects from a flexible world schema.

    A core generator may expose ``objects`` directly or separate collections
    such as ``roads``, ``districts`` and ``buildings``.  The result preserves
    each object's source collection in ``_layer``.
    """

    objects: list[dict[str, Any]] = []
    direct = _first(world, "objects", "entities", "features", default=None)
    if isinstance(direct, Mapping):
        direct = list(direct.values())
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes, Mapping)):
        for i, item in enumerate(direct):
            d = _as_dict(item)
            if isinstance(d, Mapping):
                d = dict(d)
                props = d.get("properties")
                if isinstance(props, Mapping):
                    for pk, pv in props.items():
                        d.setdefault(str(pk), _as_dict(pv))
                d.setdefault("_layer", str(_object_type(d, "objects")).lower())
                d.setdefault("id", _object_id(d, f"object-{i + 1}"))
                objects.append(d)
    collections = {
        "terrain": "terrain",
        "water": "water",
        "roads": "roads",
        "districts": "districts",
        "blocks": "blocks",
        "lots": "lots",
        "buildings": "buildings",
        "harbor": "harbor",
        "pois": "poi",
        "poi": "poi",
        "landmarks": "poi",
    }
    existing_ids = {_object_id(o, "") for o in objects}
    for key, layer in collections.items():
        values = _first(world, key, default=None)
        if isinstance(values, Mapping):
            values = list(values.values())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for i, item in enumerate(values):
            d = _as_dict(item)
            if not isinstance(d, Mapping):
                continue
            d = dict(d)
            props = d.get("properties")
            if isinstance(props, Mapping):
                for pk, pv in props.items():
                    d.setdefault(str(pk), _as_dict(pv))
            d.setdefault("_layer", layer)
            d.setdefault("id", _object_id(d, f"{layer}-{i + 1}"))
            if d["id"] in existing_ids and any(o.get("_layer") == layer for o in objects):
                # Do not duplicate a direct ``objects`` entry that already has
                # the same id, but retain distinct layer entries.
                continue
            objects.append(d)
            existing_ids.add(str(d["id"]))
    # Core WorldState stores the height field separately from entity groups.
    # Add a lightweight drawable terrain frame; keep the full raster in the
    # world document for future contour/height rendering without copying it
    # into every painter pass.
    if not any(str(o.get("_layer")) == "terrain" and _geometry_points(o) for o in objects):
        terrain = _first(world, "terrain", default={})
        if isinstance(terrain, Mapping):
            width = float(_first(world, "width", default=_first(terrain, "width", default=100.0)) or 100.0)
            height = float(_first(world, "height", default=_first(terrain, "height", default=100.0)) or 100.0)
            heights = terrain.get("heights", [])
            rows = len(heights) if isinstance(heights, Sequence) else 0
            cols = len(heights[0]) if rows and isinstance(heights[0], Sequence) else 0
            objects.insert(0, {
                "id": "terrain-1",
                "type": "Terrain",
                "_layer": "terrain",
                "terrain_type": _first(world, "terrain_type", default=_first(terrain, "type", default="terrain")),
                "height_grid": f"{cols} × {rows}" if rows else "procedural",
                "height_min": terrain.get("min"),
                "height_max": terrain.get("max"),
                "seed": terrain.get("seed", world.get("seed")),
                "geometry": [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)],
            })
    return objects


def _bbox(world: Mapping[str, Any], objects: Sequence[Mapping[str, Any]]) -> tuple[float, float, float, float]:
    extent = _first(world, "bounds", "extent", "bbox", default=None)
    if isinstance(extent, Mapping):
        xmin = float(_first(extent, "xmin", "min_x", "left", default=0) or 0)
        ymin = float(_first(extent, "ymin", "min_y", "top", default=0) or 0)
        xmax = float(_first(extent, "xmax", "max_x", "right", default=0) or 0)
        ymax = float(_first(extent, "ymax", "max_y", "bottom", default=0) or 0)
        if xmax > xmin and ymax > ymin:
            return xmin, ymin, xmax, ymax
    if isinstance(extent, Sequence) and len(extent) >= 4:
        vals = [float(x) for x in extent[:4]]
        if vals[2] > vals[0] and vals[3] > vals[1]:
            return tuple(vals)  # type: ignore[return-value]
    points: list[tuple[float, float]] = []
    for obj in objects:
        points.extend(_geometry_points(obj))
        x, y = _point(obj)
        points.append((x, y))
    if not points:
        return 0.0, 0.0, 100.0, 100.0
    xs, ys = zip(*points)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if abs(xmax - xmin) < 1e-6:
        xmax = xmin + 100
    if abs(ymax - ymin) < 1e-6:
        ymax = ymin + 100
    pad_x, pad_y = (xmax - xmin) * 0.04, (ymax - ymin) * 0.04
    return xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y


def _regular_polygon(cx: float, cy: float, radius: float, n: int = 8) -> list[tuple[float, float]]:
    return [
        (cx + math.cos(2 * math.pi * i / n) * radius, cy + math.sin(2 * math.pi * i / n) * radius)
        for i in range(n)
    ]


def _demo_world(seed: int = 20261004, width: float = 1000, height: float = 760) -> dict[str, Any]:
    """Small deterministic fallback world used before a core generator exists."""

    rng = random.Random(seed)
    world: dict[str, Any] = {
        "schema_version": "0.1",
        "id": f"world-{seed}",
        "seed": seed,
        "generator_version": "ui-demo-0.1",
        "bounds": [0, 0, width, height],
        "terrain_type": "Coast",
        "objects": [],
        "history": [],
        # Locks are opt-in.  The editor exposes them explicitly and the core
        # generator uses the same default; starting unlocked makes a first
        # regeneration behave predictably.
        "locks": {"terrain": False, "roads": False, "districts": False, "buildings": False},
    }
    objects = world["objects"]

    # Terrain and water are polygon objects, not a raster image, so they stay
    # inspectable and export cleanly to GeoJSON.
    objects.append({"id": "terrain-1", "type": "Terrain", "_layer": "terrain", "terrain": "Coast", "geometry": [(0, 0), (width, 0), (width, height), (0, height)]})
    coast = [(0, 0), (width * 0.25, 0), (width * 0.33, height * 0.16), (width * 0.28, height * 0.35), (width * 0.38, height * 0.57), (width * 0.28, height * 0.78), (width * 0.35, height), (0, height)]
    objects.append({"id": "water-1", "type": "Water", "_layer": "water", "geometry": coast, "kind": "coast"})

    # Districts: harbor west, downtown middle, residential north-east, park
    # south-east, industrial south-west.
    districts = [
        ("district-harbor", "Harbor", (70, 80, 290, 590), "#365f7d"),
        ("district-downtown", "Business", (300, 90, 690, 390), "#785f89"),
        ("district-residential", "Residential", (665, 70, 980, 360), "#5c8068"),
        ("district-park", "Park", (600, 405, 850, 680), "#3d785b"),
        ("district-industrial", "Industrial", (80, 600, 570, 715), "#806746"),
    ]
    for did, name, (x0, y0, x1, y1), color in districts:
        objects.append({"id": did, "type": "District", "_layer": "districts", "name": name, "zone": name, "color": color, "geometry": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]})

    # Main, secondary, street and alley lines.
    roads = [
        ("road-main-1", "Main Road", [(230, 0), (280, 150), (450, 315), (650, 425), (1000, 500)]),
        ("road-main-2", "Main Road", [(280, 150), (500, 110), (760, 115), (1000, 155)]),
        ("road-secondary-1", "Secondary Road", [(360, 0), (380, 160), (400, 340), (390, 580), (390, 760)]),
        ("road-secondary-2", "Secondary Road", [(650, 90), (650, 260), (625, 430), (580, 760)]),
        ("road-secondary-3", "Secondary Road", [(100, 560), (350, 550), (610, 535), (860, 550), (1000, 590)]),
    ]
    for rid, name, points in roads:
        objects.append({"id": rid, "type": "Road", "_layer": "roads", "road_type": name, "geometry": points, "width": 14 if name == "Main Road" else 9})
    for row, y in enumerate((205, 290, 375, 465, 625), 1):
        objects.append({"id": f"road-street-{row}", "type": "Road", "_layer": "roads", "road_type": "Street", "geometry": [(300, y), (940, y + rng.randint(-12, 12))], "width": 5})
    for col, x in enumerate((480, 565, 735, 825, 900), 1):
        objects.append({"id": f"road-street-v{col}", "type": "Road", "_layer": "roads", "road_type": "Street", "geometry": [(x, 140), (x + rng.randint(-10, 10), 500)], "width": 5})

    # Lots/buildings.  Building heights are real parameters used in statistics
    # and in the isometric view, not merely visual labels.
    building_n = 0
    for did, name, (x0, y0, x1, y1), _ in districts:
        if name in {"Harbor", "Park"}:
            continue
        step = 56 if name == "Industrial" else 48
        for y in range(int(y0 + 24), int(y1 - 25), step):
            for x in range(int(x0 + 24), int(x1 - 25), step):
                if rng.random() < (0.15 if name == "Industrial" else 0.28):
                    continue
                lot_id = f"lot-{building_n + 1}"
                bw = rng.uniform(25, 39)
                bh = rng.uniform(22, 38)
                objects.append({"id": lot_id, "type": "Lot", "_layer": "lots", "district": did, "zone": name, "geometry": [(x, y), (x + step - 5, y), (x + step - 5, y + step - 5), (x, y + step - 5)]})
                building_n += 1
                h = rng.randint(1, 4 if name == "Residential" else 12)
                objects.append({"id": f"building-{building_n}", "type": "Building", "_layer": "buildings", "parent": lot_id, "district": did, "use": name, "floors": h, "height": h * 3.4, "geometry": [(x + 5, y + 5), (x + bw, y + 5), (x + bw, y + bh), (x + 5, y + bh)], "roof": "flat" if name == "Business" else "gable"})

    pois = [
        ("poi-station", "Station", 575, 125),
        ("poi-school", "School", 790, 220),
        ("poi-hospital", "Hospital", 900, 315),
        ("poi-mall", "Mall", 500, 250),
        ("poi-park", "Park", 730, 540),
        ("poi-government", "Government", 455, 175),
        ("poi-temple", "Temple", 875, 470),
        ("poi-warehouse", "Warehouse", 235, 650),
        ("poi-cafe", "Cafe", 705, 300),
    ]
    for pid, name, x, y in pois:
        objects.append({"id": pid, "type": "POI", "_layer": "poi", "poi_type": name, "name": name, "geometry": _regular_polygon(x, y, 16 if name not in ("Station", "Mall") else 24, 6)})

    world["statistics"] = calculate_statistics(world)
    return world


def calculate_statistics(world: Mapping[str, Any]) -> dict[str, float]:
    objects = _flatten_objects(world)
    road_length = 0.0
    building_count = 0
    district_area = 0.0
    green_area = 0.0
    heights: list[float] = []
    for obj in objects:
        typ = _object_type(obj).lower()
        pts = _geometry_points(obj)
        if typ == "road" or obj.get("_layer") == "roads":
            road_length += sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        if typ == "building" or obj.get("_layer") == "buildings":
            building_count += 1
            try:
                heights.append(float(_first(obj, "height", default=0) or 0))
            except (TypeError, ValueError):
                pass
        if typ == "district" or obj.get("_layer") == "districts":
            district_area += abs(_polygon_area(pts))
        zone = str(_first(obj, "zone", "name", default="")).lower()
        if "park" in zone or typ == "park":
            green_area += abs(_polygon_area(pts))
    _, _, xmax, ymax = _bbox(world, objects)
    xmin, ymin, _, _ = _bbox(world, objects)
    total = max(1.0, (xmax - xmin) * (ymax - ymin))
    return {
        "road_length": round(road_length, 2),
        "building_count": float(building_count),
        "density": round(building_count / total * 10000, 3),
        "district_area": round(district_area, 2),
        "green_area": round(green_area, 2),
        "average_building_height": round(sum(heights) / len(heights), 2) if heights else 0.0,
    }


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))


def _geojson(world: Mapping[str, Any]) -> dict[str, Any]:
    features = []
    for i, obj in enumerate(_flatten_objects(world)):
        pts = _geometry_points(obj)
        if len(pts) < 2:
            continue
        layer = str(obj.get("_layer", _object_type(obj, "object")))
        typ = _object_type(obj, layer)
        if len(pts) >= 3 and (pts[0] != pts[-1]):
            geometry = {"type": "Polygon", "coordinates": [[list(p) for p in pts + [pts[0]]]]}
        else:
            geometry = {"type": "LineString", "coordinates": [list(p) for p in pts]}
        props = {k: _as_dict(v) for k, v in obj.items() if k not in {"geometry", "polygon", "points", "vertices", "path"} and not k.startswith("_")}
        props.setdefault("layer", layer)
        props.setdefault("type", typ)
        features.append({"type": "Feature", "id": _object_id(obj, f"feature-{i}"), "geometry": geometry, "properties": props})
    return {"type": "FeatureCollection", "features": features, "properties": {"seed": world.get("seed"), "generator_version": world.get("generator_version")}}


def _obj_text(world: Mapping[str, Any]) -> str:
    """Export simple extruded geometry as Wavefront OBJ."""
    lines = ["# Procedural World Studio OBJ export", f"# seed: {world.get('seed', '')}"]
    vertex_count = 0
    for obj in _flatten_objects(world):
        if _object_type(obj).lower() != "building" and obj.get("_layer") != "buildings":
            continue
        pts = _geometry_points(obj)
        if len(pts) < 3:
            continue
        try:
            z = max(0.5, float(_first(obj, "height", default=3) or 3))
        except (TypeError, ValueError):
            z = 3.0
        lines.append(f"o {_object_id(obj, 'building')}")
        for x, y in pts:
            lines.append(f"v {x:.4f} {y:.4f} 0")
        for x, y in pts:
            lines.append(f"v {x:.4f} {y:.4f} {z:.4f}")
        n = len(pts)
        bottom = list(range(vertex_count + 1, vertex_count + n + 1))
        top = list(range(vertex_count + n + 1, vertex_count + 2 * n + 1))
        lines.append("f " + " ".join(map(str, bottom)))
        lines.append("f " + " ".join(map(str, reversed(top))))
        for i in range(n):
            j = (i + 1) % n
            lines.append(f"f {bottom[i]} {bottom[j]} {top[j]} {top[i]}")
        vertex_count += n * 2
    return "\n".join(lines) + "\n"


@dataclass
class HistoryEntry:
    timestamp: str
    seed: int
    parameters: dict[str, Any]
    generator_version: str
    result_metadata: dict[str, Any]


class WorldAdapter:
    """Adapter between the editor and whichever generator is available.

    ``generator`` may be a callable, an object exposing ``generate`` or an
    object exposing ``generate_world``.  If no generator is supplied the
    deterministic fallback demo is used.  The adapter never mutates a caller's
    object in place; every generation returns a plain dictionary.
    """

    LAYERS = ("terrain", "water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "poi")

    def __init__(self, generator: Any = None, world: Optional[Mapping[str, Any]] = None):
        self.generator = generator or self._discover_generator()
        self.world: dict[str, Any] = _as_dict(world) if world is not None else _demo_world(20261004)
        self.world.setdefault("history", [])
        self.world.setdefault("locks", {"terrain": False, "roads": False, "districts": False, "buildings": False})
        self.parameters: dict[str, Any] = {
            "terrain_type": "coast",
            "road_pattern": "Hybrid",
            "difficulty": 3,
        }
        self.history: list[HistoryEntry] = []
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        # Show the real fixed-seed coastal demo on first launch when the core
        # generator is present.  If a plugin/custom generator is unavailable,
        # the lightweight deterministic demo above remains an immediate
        # functional preview.
        if world is None and self.generator is not None and hasattr(self.generator, "generate"):
            try:
                self.world = self._call_generator(20261004, self.parameters)
            except Exception:
                pass

    @staticmethod
    def _discover_generator() -> Any:
        candidates = (
            "procedural_world_studio.generator",
            "procedural_world_studio.core",
            "procedural_world_studio.world_generator",
            "world_generator",
            "core.generator",
        )
        for module_name in candidates:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            for name in ("WorldGenerator", "ProceduralWorldGenerator", "Generator", "generate_world"):
                candidate = getattr(module, name, None)
                if candidate is not None:
                    try:
                        return candidate() if isinstance(candidate, type) else candidate
                    except Exception:
                        return candidate
        return None

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.world)

    def restore(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        self.world = _as_dict(snapshot)
        return self.world

    def _call_generator(
        self,
        seed: int,
        params: Mapping[str, Any],
        existing: Optional[Mapping[str, Any]] = None,
        regenerate: Optional[str] = None,
        preserve_locks: bool = False,
    ) -> dict[str, Any]:
        gen = self.generator
        if gen is None:
            if regenerate:
                result = self._fallback_regenerate(seed, regenerate, params)
            else:
                result = _demo_world(seed)
        else:
            # The project's first-class core generator uses a mutable
            # WorldGenerator instance and a WorldState object.  Adapt that
            # API explicitly before trying the looser callable protocol below.
            core_state_cls = None
            try:
                from .core import WorldState as _CoreWorldState  # type: ignore

                core_state_cls = _CoreWorldState
            except Exception:
                pass
            if hasattr(gen, "set_seed") and hasattr(gen, "state") and hasattr(gen, "generate"):
                try:
                    requested_locks = dict(self.world.get("locks", {}))
                    gen.set_seed(seed)
                    terrain_type = str(params.get("terrain_type", params.get("terrain", "coast"))).lower().replace(" ", "_")
                    road_pattern = str(params.get("road_pattern", "hybrid")).lower()
                    if regenerate:
                        if existing is not None and core_state_cls is not None:
                            try:
                                gen.state = core_state_cls.from_dict(existing)
                            except Exception:
                                pass
                        result_state = gen.regenerate(regenerate, state=getattr(gen, "state", None), road_pattern=road_pattern, terrain_type=terrain_type)
                    else:
                        if preserve_locks and existing is not None and core_state_cls is not None:
                            try:
                                gen.state = core_state_cls.from_dict(existing)
                            except Exception:
                                pass
                        result_state = gen.generate(
                            terrain_type=terrain_type,
                            road_pattern=road_pattern,
                            include_harbor=True,
                            preserve_locks=bool(preserve_locks and existing is not None),
                        )
                    # A lock may be toggled before the first full generation.
                    # Apply the editor's explicit choice to the newly created
                    # core state rather than silently resetting it to the
                    # backend default.
                    if hasattr(result_state, "locks"):
                        for plural, singular in (("terrain", "terrain"), ("roads", "road"), ("districts", "district"), ("buildings", "building")):
                            if plural in requested_locks or singular in requested_locks:
                                result_state.locks[singular] = bool(requested_locks.get(plural, requested_locks.get(singular, False)))
                    result = result_state.to_dict() if hasattr(result_state, "to_dict") else result_state
                    world = dict(_as_dict(result))
                    world.setdefault("seed", seed)
                    world.setdefault("generator_version", getattr(gen, "generator_version", "core"))
                    # Core locks use singular keys (road/district/building),
                    # while the editor exposes plural layer names.
                    locks = dict(world.get("locks", {}))
                    for plural, singular in (("roads", "road"), ("districts", "district"), ("buildings", "building"), ("terrain", "terrain")):
                        if singular in locks and plural not in locks:
                            locks[plural] = locks[singular]
                    world["locks"] = locks
                    world["statistics"] = self._core_statistics(gen, world)
                    return world
                except Exception:
                    self.last_error = traceback.format_exc()
                    # Fall through to the generic protocol/fallback; this is
                    # particularly useful while the core API is evolving.
            fn = gen
            if not callable(fn):
                fn = getattr(gen, "generate", None) or getattr(gen, "generate_world", None)
            if not callable(fn):
                result = _demo_world(seed)
            else:
                kwargs = dict(params)
                kwargs.update({"seed": seed})
                if existing is not None:
                    kwargs.setdefault("existing_world", existing)
                    kwargs.setdefault("world", existing)
                if regenerate:
                    kwargs.setdefault("regenerate", regenerate)
                    kwargs.setdefault("component", regenerate)
                try:
                    result = fn(**kwargs)
                except TypeError:
                    # Older generators often only accept seed/parameters.
                    try:
                        result = fn(seed, kwargs)
                    except TypeError:
                        result = fn(seed)
                except Exception:
                    # Keep the editor usable when a partially implemented
                    # generator raises; callers can still inspect the error in
                    # ``last_error`` while a deterministic world is shown.
                    self.last_error = traceback.format_exc()
                    result = self._fallback_regenerate(seed, regenerate, params) if regenerate else _demo_world(seed)
        result = _as_dict(result)
        if not isinstance(result, Mapping):
            result = _demo_world(seed)
        world = dict(result)
        world.setdefault("seed", seed)
        world.setdefault("generator_version", getattr(gen, "version", "unknown") if gen else "ui-demo-0.1")
        world.setdefault("locks", copy.deepcopy(self.world.get("locks", {})))
        world["statistics"] = calculate_statistics(world)
        return world

    @staticmethod
    def _core_statistics(gen: Any, world: Mapping[str, Any]) -> dict[str, Any]:
        try:
            state_cls = getattr(importlib.import_module("procedural_world_studio.core"), "WorldState", None)
            # Core statistics understand the WorldState group schema; a custom
            # or fallback provider may expose a flat ``objects`` list instead.
            has_core_groups = any(isinstance(world.get(k), Mapping) for k in ("buildings", "roads", "districts", "pois"))
            state = state_cls.from_dict(world) if state_cls is not None and has_core_groups else None
            if state is not None and hasattr(gen, "statistics"):
                return dict(gen.statistics(state))
        except Exception:
            pass
        return calculate_statistics(world)

    def _fallback_regenerate(self, seed: int, component: str, params: Mapping[str, Any]) -> dict[str, Any]:
        # The fallback intentionally uses the existing world for unaffected
        # layers.  It demonstrates the same lock semantics as a real backend.
        base = copy.deepcopy(self.world)
        fresh = _demo_world(seed)
        component = component.lower().rstrip("s")
        if component in {"road", "roads"}:
            component = "roads"
        if component in {"district", "districts"}:
            component = "districts"
        if component in {"building", "buildings"}:
            component = "buildings"
        if component in {"poi", "pois"}:
            component = "poi"
        objects = [o for o in _flatten_objects(base) if o.get("_layer") != component]
        objects.extend([o for o in _flatten_objects(fresh) if o.get("_layer") == component])
        base["objects"] = objects
        base["seed"] = seed
        base["generator_version"] = fresh.get("generator_version", base.get("generator_version"))
        return base

    def generate_sync(self, seed: Optional[int] = None, parameters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        seed = int(self.world.get("seed", 42) if seed is None else seed)
        parameters = {**self.parameters, **(dict(parameters or {}))}
        self._undo.append(self.snapshot())
        self._redo.clear()
        self.parameters = dict(parameters)
        locks = self.world.get("locks", {})
        preserve_locks = any(bool(locks.get(key, False)) for key in ("terrain", "road", "roads", "district", "districts", "building", "buildings"))
        self.world = self._call_generator(seed, parameters, existing=self.world if preserve_locks else None, preserve_locks=preserve_locks)
        self._record_history(parameters)
        return self.world

    def regenerate(self, component: str, seed: Optional[int] = None, parameters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        component = str(component).lower()
        seed = int(self.world.get("seed", 42) if seed is None else seed)
        parameters = {**self.parameters, **(dict(parameters or {}))}
        self._undo.append(self.snapshot())
        self._redo.clear()
        self.parameters = dict(parameters)
        locks = self.world.get("locks", {})
        lock_key = component.rstrip("s")
        if bool(locks.get(lock_key, False)) and component in {"terrain", "roads", "districts", "buildings"}:
            # Explicit regenerate is allowed; lock only affects a fresh full
            # generation.  This gives the user a predictable escape hatch.
            pass
        self.world = self._call_generator(seed, parameters, existing=self.world, regenerate=component)
        self._record_history(parameters, result_metadata={"regenerated": component})
        return self.world

    def set_lock(self, component: str, locked: bool) -> None:
        locks = self.world.setdefault("locks", {})
        key = str(component).lower().rstrip("s")
        locks[key] = bool(locked)
        # Keep both spellings in serialized worlds so generic tools and the
        # core generator can consume the same document.
        locks[str(component).lower()] = bool(locked)
        if hasattr(self.generator, "set_lock"):
            try:
                self.generator.set_lock(key, bool(locked))
            except Exception:
                pass

    @staticmethod
    def _translate_geometry(geometry: Mapping[str, Any], dx: float, dy: float) -> dict[str, Any]:
        """Translate a GeoJSON-like geometry without changing its topology."""

        def translate(value: Any) -> Any:
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                    shifted = [float(value[0]) + float(dx), float(value[1]) + float(dy)]
                    shifted.extend(value[2:])
                    return tuple(shifted) if isinstance(value, tuple) else shifted
                result = [translate(item) for item in value]
                return tuple(result) if isinstance(value, tuple) else result
            return value

        result = copy.deepcopy(dict(geometry))
        result["coordinates"] = translate(result.get("coordinates", []))
        return result

    def edit_feature(self, feature_id: str, *, dx: float = 0.0, dy: float = 0.0, properties: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Edit one feature through the core when available, with a plain-dict fallback."""

        self._undo.append(self.snapshot())
        self._redo.clear()
        props = dict(properties or {})
        if self.generator is not None and hasattr(self.generator, "update_feature"):
            try:
                from .core import WorldState as _CoreWorldState

                state = _CoreWorldState.from_dict(self.world)
                feature = state.get_feature(feature_id)
                if feature is None:
                    raise KeyError(feature_id)
                geometry = self._translate_geometry(feature.geometry, float(dx), float(dy)) if dx or dy else None
                self.generator.state = state
                updated = self.generator.update_feature(feature_id, properties=props or None, geometry=geometry, state=state)
                self.world = updated.to_dict()
                self.world["statistics"] = self._core_statistics(self.generator, self.world)
                self._record_history({"edit_feature": feature_id, **props, "dx": dx, "dy": dy}, {"edited": feature_id})
                return self.world
            except Exception:
                self.last_error = traceback.format_exc()
        # Generic provider/fallback: locate a feature in a stage collection or
        # a flat object list and apply the same serialisable mutation.
        for collection in (self.world.get("objects"), self.world.get("entities")):
            if isinstance(collection, list):
                candidates = collection
            elif isinstance(collection, Mapping):
                candidates = list(collection.values())
            else:
                continue
            for obj in candidates:
                if not isinstance(obj, MutableMapping) or str(obj.get("id")) != str(feature_id):
                    continue
                if dx or dy:
                    obj["geometry"] = self._translate_geometry(obj.get("geometry", {}), dx, dy)
                obj.update(props)
                self.world["statistics"] = calculate_statistics(self.world)
                self._record_history({"edit_feature": feature_id, **props, "dx": dx, "dy": dy}, {"edited": feature_id})
                return self.world
        raise KeyError(f"Unknown feature: {feature_id}")

    def undo(self) -> Optional[dict[str, Any]]:
        if not self._undo:
            return None
        self._redo.append(self.snapshot())
        self.world = self._undo.pop()
        return self.world

    def redo(self) -> Optional[dict[str, Any]]:
        if not self._redo:
            return None
        self._undo.append(self.snapshot())
        self.world = self._redo.pop()
        return self.world

    def _record_history(self, parameters: Mapping[str, Any], result_metadata: Optional[Mapping[str, Any]] = None) -> None:
        # Logical run IDs keep a saved project reproducible while still
        # giving users an ordered generator history.
        timestamp = f"run-{len(self.world.get('history', [])) + 1:04d}"
        entry = HistoryEntry(timestamp, int(self.world.get("seed", 0)), dict(parameters), str(self.world.get("generator_version", "unknown")), dict(result_metadata or self.world.get("statistics", {})))
        self.history.append(entry)
        self.world.setdefault("history", []).append(_as_dict(entry))

    def export_json(self, path: os.PathLike[str] | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_as_dict(self.world), ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def export_geojson(self, path: os.PathLike[str] | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_geojson(self.world), ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def export_obj(self, path: os.PathLike[str] | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_obj_text(self.world), encoding="utf-8")
        return out

    def export_gltf(self, path: os.PathLike[str] | str) -> Path:
        """Export the same editable world as a self-contained glTF 2.0 file."""
        from .export_formats import export_gltf as _export_gltf

        return _export_gltf(self.world, path)

    def export_png(self, path: os.PathLike[str] | str, width: int = 1400, height: int = 1000) -> Path:
        """Render a map snapshot without requiring Qt (Pillow fallback)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image, ImageDraw
        except Exception:
            # Keep the operation explicit instead of silently producing an
            # invalid file on minimal Python installs.
            raise RuntimeError("PNG export requires Pillow")
        objects = _flatten_objects(self.world)
        xmin, ymin, xmax, ymax = _bbox(self.world, objects)
        sx, sy = (width - 40) / (xmax - xmin), (height - 40) / (ymax - ymin)
        image = Image.new("RGB", (width, height), "#111827")
        draw = ImageDraw.Draw(image)
        colors = {"water": "#2f6685", "terrain": "#273b31", "districts": "#4b5563", "roads": "#d3b983", "buildings": "#a7a9b3", "lots": "#58606b", "poi": "#f2bd4b"}
        def tr(p: tuple[float, float]) -> tuple[float, float]:
            return ((p[0] - xmin) * sx + 20, (p[1] - ymin) * sy + 20)
        for obj in objects:
            pts = _geometry_points(obj)
            if len(pts) < 2:
                continue
            layer = str(obj.get("_layer", "objects"))
            xy = [tr(p) for p in pts]
            typ = _object_type(obj).lower()
            if typ == "road" or layer == "roads":
                draw.line(xy, fill=colors["roads"], width=max(1, int(float(obj.get("width", 4)) * sx / 12)))
            elif len(pts) >= 3:
                draw.polygon(xy, fill=colors.get(layer, "#6b7280"), outline="#111827")
            else:
                draw.line(xy, fill=colors.get(layer, "#a7a9b3"), width=2)
        image.save(out)
        return out

    def export_all(self, directory: os.PathLike[str] | str) -> list[Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return [
            self.export_json(directory / "world.json"),
            self.export_geojson(directory / "world.geojson"),
            self.export_png(directory / "world.png"),
            self.export_obj(directory / "world.obj"),
        ]

    def statistics(self) -> dict[str, float]:
        if hasattr(self.generator, "statistics") and hasattr(self.generator, "state"):
            self.world["statistics"] = self._core_statistics(self.generator, self.world)
        else:
            self.world["statistics"] = calculate_statistics(self.world)
        return dict(self.world["statistics"])


# ---------------------------------------------------------------------------
# Qt editor
# ---------------------------------------------------------------------------


try:  # Import lazily so headless/export-only use never needs a display.
    from PySide6 import QtCore, QtGui, QtWidgets

    QT_AVAILABLE = True
except Exception:  # pragma: no cover - exercised on minimal installations
    QtCore = QtGui = QtWidgets = None  # type: ignore[assignment]
    QT_AVAILABLE = False


_LAYER_COLORS = {
    "terrain": "#273b31",
    "water": "#2e6b8b",
    "roads": "#e5c98e",
    "districts": "#7d6a91",
    "blocks": "#596273",
    "lots": "#74808f",
    "buildings": "#b6bbc5",
    "harbor": "#7c8795",
    "poi": "#f2bd4b",
}


if QT_AVAILABLE:

    class GenerationSignals(QtCore.QObject):
        finished = QtCore.Signal(object, str)
        failed = QtCore.Signal(str)


    class GenerationWorker(QtCore.QRunnable):
        """Run one generation/regeneration away from the GUI thread."""

        def __init__(self, adapter: WorldAdapter, action: str, seed: int, parameters: Mapping[str, Any]):
            super().__init__()
            self.adapter = adapter
            self.action = action
            self.seed = seed
            self.parameters = dict(parameters)
            self.signals = GenerationSignals()

        @QtCore.Slot()
        def run(self) -> None:  # pragma: no cover - scheduling is Qt runtime
            try:
                if self.action == "generate":
                    world = self.adapter.generate_sync(self.seed, self.parameters)
                else:
                    world = self.adapter.regenerate(self.action, self.seed, self.parameters)
                self.signals.finished.emit(world, self.action)
            except Exception:
                self.signals.failed.emit(traceback.format_exc())


    class MapCanvas(QtWidgets.QWidget):
        """Interactive vector map view.

        It intentionally paints directly from world objects rather than
        rasterizing a screenshot.  Click hit-testing therefore remains useful
        after zooming and each object remains inspectable.
        """

        objectSelected = QtCore.Signal(object)
        statusChanged = QtCore.Signal(str)

        def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
            super().__init__(parent)
            self.setMinimumSize(480, 360)
            self.setMouseTracking(True)
            self.world: dict[str, Any] = _demo_world(42)
            self.view_mode = "Top Down"
            self.visible_layers = {layer: True for layer in WorldAdapter.LAYERS}
            self.selected_id: Optional[str] = None
            self._bbox = _bbox(self.world, _flatten_objects(self.world))
            self._zoom = 1.0
            self._pan = QtCore.QPointF(0, 0)
            self._drag_start: Optional[QtCore.QPoint] = None
            self._drag_pan = QtCore.QPointF(0, 0)
            self._last_transform: Optional[tuple[float, float, float, float]] = None

        def set_world(self, world: Mapping[str, Any]) -> None:
            self.world = _as_dict(world)
            objects = _flatten_objects(self.world)
            self._bbox = _bbox(self.world, objects)
            self.selected_id = None if not any(_object_id(o, "") == self.selected_id for o in objects) else self.selected_id
            self._zoom = 1.0
            self._pan = QtCore.QPointF(0, 0)
            self.update()

        def set_view_mode(self, mode: str) -> None:
            self.view_mode = mode
            self._zoom = 1.0
            self._pan = QtCore.QPointF(0, 0)
            self.update()

        def set_layer_visible(self, layer: str, visible: bool) -> None:
            self.visible_layers[layer] = visible
            self.update()

        def _project(self, x: float, y: float, z: float = 0.0) -> tuple[float, float]:
            xmin, ymin, xmax, ymax = self._bbox
            # Fit with a small border, then apply camera mode and user zoom.
            w, h = max(1, self.width() - 36), max(1, self.height() - 36)
            base_s = min(w / max(1, xmax - xmin), h / max(1, ymax - ymin))
            if self.view_mode == "Isometric":
                # Center coordinates before rotating; this keeps the map
                # stable while switching camera modes.
                cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
                xx, yy = (x - cx) * 0.80 - (y - cy) * 0.80, (x - cx) * 0.42 + (y - cy) * 0.42
                px, py = xx * base_s * self._zoom, yy * base_s * self._zoom - z * base_s * 0.55 * self._zoom
                return self.width() / 2 + px + self._pan.x(), self.height() / 2 + py + self._pan.y()
            if self.view_mode == "Perspective":
                depth = (y - ymin) / max(1, ymax - ymin)
                perspective = 0.72 + depth * 0.46
                px = (x - xmin) * base_s * perspective * self._zoom
                py = (y - ymin) * base_s * self._zoom
                return 18 + px + self._pan.x(), 18 + py + self._pan.y()
            # Top Down and Overview
            px = (x - xmin) * base_s * self._zoom
            py = (y - ymin) * base_s * self._zoom
            if self.view_mode == "Overview":
                base_s = min(w / max(1, xmax - xmin), h / max(1, ymax - ymin)) * 0.80
                px = (x - xmin) * base_s * self._zoom
                py = (y - ymin) * base_s * self._zoom
                return self.width() / 2 - (xmax - xmin) * base_s / 2 + px + self._pan.x(), self.height() / 2 - (ymax - ymin) * base_s / 2 + py + self._pan.y()
            return 18 + px + self._pan.x(), 18 + py + self._pan.y()

        def _unproject(self, pos: QtCore.QPointF) -> tuple[float, float]:
            xmin, ymin, xmax, ymax = self._bbox
            w, h = max(1, self.width() - 36), max(1, self.height() - 36)
            base_s = min(w / max(1, xmax - xmin), h / max(1, ymax - ymin))
            if self.view_mode in {"Top Down", "Perspective"}:
                x = (pos.x() - 18 - self._pan.x()) / max(1e-9, base_s * self._zoom) + xmin
                y = (pos.y() - 18 - self._pan.y()) / max(1e-9, base_s * self._zoom) + ymin
                return x, y
            # For isometric/overview, the inverse is approximate but sufficient
            # for intuitive hit-testing around object centers.
            if self.view_mode == "Overview":
                base_s *= 0.80
                ox = self.width() / 2 - (xmax - xmin) * base_s / 2
                oy = self.height() / 2 - (ymax - ymin) * base_s / 2
            else:
                ox, oy = self.width() / 2, self.height() / 2
            xx = (pos.x() - ox - self._pan.x()) / max(1e-9, base_s * self._zoom)
            yy = (pos.y() - oy - self._pan.y()) / max(1e-9, base_s * self._zoom)
            return (xx / 1.6 + yy / 0.84) / 2 + (xmin + xmax) / 2, (yy / 0.84 - xx / 1.6) / 2 + (ymin + ymax) / 2

        def _draw_polygon(self, painter: QtGui.QPainter, pts: Sequence[tuple[float, float]], brush: QtGui.QBrush, pen: QtGui.QPen, z: float = 0.0) -> None:
            if len(pts) < 2:
                return
            qpts = [QtCore.QPointF(*self._project(x, y, z)) for x, y in pts]
            painter.setBrush(brush)
            painter.setPen(pen)
            painter.drawPolygon(QtGui.QPolygonF(qpts))

        def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QtGui.QColor("#111827"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#233044"), 1))
            # Subtle grid makes spatial scale legible without competing with
            # generated geometry.
            for i in range(0, max(self.width(), self.height()), 40):
                painter.drawLine(i, 0, i, self.height())
                painter.drawLine(0, i, self.width(), i)
            objects = _flatten_objects(self.world)
            # Draw in z-order so water/terrain sit below roads/buildings.
            order = {"terrain": 0, "water": 1, "districts": 2, "blocks": 3, "lots": 4, "harbor": 5, "roads": 6, "buildings": 7, "poi": 8}
            objects.sort(key=lambda o: order.get(str(o.get("_layer", "")), 3))
            for obj in objects:
                layer = str(obj.get("_layer", _object_type(obj, "objects")).lower())
                if not self.visible_layers.get(layer, True):
                    continue
                pts = _geometry_points(obj)
                if len(pts) < 2:
                    continue
                typ = _object_type(obj).lower()
                oid = _object_id(obj, "")
                selected = oid == self.selected_id
                color = str(obj.get("color", _LAYER_COLORS.get(layer, "#8892a4")))
                try:
                    qcolor = QtGui.QColor(color)
                except Exception:
                    qcolor = QtGui.QColor(_LAYER_COLORS.get(layer, "#8892a4"))
                if layer == "water":
                    qcolor.setAlpha(210)
                elif layer == "districts":
                    qcolor.setAlpha(75)
                elif layer in {"lots", "blocks"}:
                    qcolor.setAlpha(22)
                if typ == "road" or layer == "roads":
                    try:
                        width = float(_first(obj, "width", default=5) or 5)
                    except Exception:
                        width = 5
                    pen = QtGui.QPen(QtGui.QColor("#f2d79b"), max(1.2, width / 3.0))
                    if selected:
                        pen.setColor(QtGui.QColor("#fff4b0"))
                        pen.setWidthF(max(3.0, width / 2.0))
                    painter.setBrush(QtCore.Qt.NoBrush)
                    painter.setPen(pen)
                    painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(*self._project(x, y)) for x, y in pts]))
                elif layer == "poi" or typ == "poi":
                    center = tuple(map(float, _point(obj)))
                    if center == (0.0, 0.0) and pts:
                        center = pts[0]
                    px, py = self._project(center[0], center[1], 6)
                    r = 8 if not selected else 11
                    painter.setPen(QtGui.QPen(QtGui.QColor("#fff8d2"), 2 if selected else 1))
                    painter.setBrush(QtGui.QBrush(qcolor))
                    painter.drawEllipse(QtCore.QPointF(px, py), r, r)
                    label = str(_first(obj, "name", "poi_type", default="POI"))
                    painter.setPen(QtGui.QColor("#f5f7fb"))
                    painter.setFont(QtGui.QFont("Segoe UI", 8))
                    painter.drawText(px + 10, py - 4, label)
                else:
                    try:
                        z = float(_first(obj, "height", default=0) or 0) if layer == "buildings" else 0
                    except Exception:
                        z = 0
                    # Isometric view shows a restrained extrusion, while top
                    # down retains exact footprints.
                    if self.view_mode == "Isometric" and z > 0:
                        top = [self._project(x, y, z) for x, y in pts]
                        bottom = [self._project(x, y, 0) for x, y in pts]
                        painter.setBrush(QtGui.QColor(qcolor).darker(135))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#1d2634"), 0.6))
                        for i in range(len(pts)):
                            j = (i + 1) % len(pts)
                            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(*bottom[i]), QtCore.QPointF(*bottom[j]), QtCore.QPointF(*top[j]), QtCore.QPointF(*top[i])]))
                        self._draw_polygon(painter, [(x, y) for x, y in pts], QtGui.QBrush(qcolor), QtGui.QPen(QtGui.QColor("#263242"), 0.7), z)
                    else:
                        pen = QtGui.QPen(QtGui.QColor("#f3f4f6") if selected else QtGui.QColor("#263242"), 2 if selected else 0.7)
                        self._draw_polygon(painter, pts, QtGui.QBrush(qcolor), pen)
                if selected and layer not in {"poi", "roads"}:
                    # Selection outline is drawn above translucent fills.
                    painter.setBrush(QtCore.Qt.NoBrush)
                    painter.setPen(QtGui.QPen(QtGui.QColor("#f8e58c"), 2.2))
                    painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(*self._project(x, y)) for x, y in pts]))
            painter.end()

        def wheelEvent(self, event: QtGui.QWheelEvent) -> None:  # noqa: N802
            delta = event.angleDelta().y()
            self._zoom = max(0.25, min(8.0, self._zoom * (1.15 if delta > 0 else 1 / 1.15)))
            self.update()
            event.accept()

        def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
            if event.button() == QtCore.Qt.LeftButton:
                self._drag_start = event.position().toPoint()
                self._drag_pan = QtCore.QPointF(self._pan)
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
            if self._drag_start is not None and event.buttons() & QtCore.Qt.LeftButton:
                delta = event.position().toPoint() - self._drag_start
                self._pan = self._drag_pan + QtCore.QPointF(delta)
                self.update()
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
            if event.button() == QtCore.Qt.LeftButton:
                start = self._drag_start
                self._drag_start = None
                if start is not None and (event.position().toPoint() - start).manhattanLength() < 6:
                    self._select_at(event.position())
            super().mouseReleaseEvent(event)

        def _select_at(self, pos: QtCore.QPointF) -> None:
            x, y = self._unproject(pos)
            objects = _flatten_objects(self.world)
            best: tuple[float, Optional[dict[str, Any]]] = (float("inf"), None)
            # Prefer small/upper-layer objects when several overlap.
            order = {"poi": 0, "buildings": 1, "roads": 2, "lots": 3, "districts": 4, "terrain": 5, "water": 6}
            for obj in objects:
                layer = str(obj.get("_layer", ""))
                if not self.visible_layers.get(layer, True):
                    continue
                pts = _geometry_points(obj)
                if not pts:
                    continue
                if len(pts) == 1:
                    d = math.dist((x, y), pts[0])
                    threshold = 22
                    score = d + order.get(layer, 8) * 0.001
                    if d <= threshold and score < best[0]:
                        best = (score, obj)
                    continue
                if layer == "roads" or _object_type(obj).lower() == "road":
                    d = min((_distance_to_segment((x, y), a, b) for a, b in zip(pts, pts[1:])), default=9999)
                    threshold = 14
                else:
                    inside = _point_in_polygon((x, y), pts) if len(pts) >= 3 else False
                    center = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                    d = 0 if inside else math.dist((x, y), center)
                    threshold = max(16, math.sqrt(abs(_polygon_area(pts))) * 0.4)
                score = d + order.get(layer, 8) * 0.001
                if d <= threshold and score < best[0]:
                    best = (score, obj)
            selected = best[1]
            self.selected_id = _object_id(selected, "") if selected else None
            self.objectSelected.emit(selected)
            self.statusChanged.emit(f"Selected {self.selected_id}" if selected else "No object at cursor")
            self.update()


    def _distance_to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx == dy == 0:
            return math.dist(p, a)
        t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
        return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


    def _point_in_polygon(p: tuple[float, float], poly: Sequence[tuple[float, float]]) -> bool:
        inside = False
        j = len(poly) - 1
        for i, (xi, yi) in enumerate(poly):
            xj, yj = poly[j]
            if ((yi > p[1]) != (yj > p[1])) and p[0] < (xj - xi) * (p[1] - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
            j = i
        return inside


    class MainWindow(QtWidgets.QMainWindow):
        """The complete Procedural World Studio desktop editor."""

        def __init__(self, adapter: Optional[WorldAdapter] = None, parent: Optional[QtWidgets.QWidget] = None):
            super().__init__(parent)
            self.adapter = adapter or WorldAdapter()
            self.thread_pool = QtCore.QThreadPool.globalInstance()
            self._worker: Optional[GenerationWorker] = None
            self._busy = False
            self._selected_object: Optional[dict[str, Any]] = None
            self.setWindowTitle("Procedural World Studio — 程序化世界生成工作台")
            self.resize(1480, 900)
            self.setMinimumSize(1050, 650)
            self._build_ui()
            self._refresh_all()

        # ---- UI construction -------------------------------------------------
        def _build_ui(self) -> None:
            root = QtWidgets.QWidget(self)
            self.setCentralWidget(root)
            outer = QtWidgets.QVBoxLayout(root)
            outer.setContentsMargins(8, 8, 8, 6)
            outer.setSpacing(6)

            toolbar = QtWidgets.QToolBar("Main", self)
            toolbar.setMovable(False)
            self.addToolBar(toolbar)
            self.action_generate = QtGui.QAction("▶ Generate", self)
            self.action_generate.setToolTip("Generate a world in the background")
            self.action_generate.triggered.connect(self._generate_clicked)
            toolbar.addAction(self.action_generate)
            self.action_undo = QtGui.QAction("↶ Undo", self)
            self.action_undo.setShortcut(QtGui.QKeySequence.Undo)
            self.action_undo.triggered.connect(self._undo_clicked)
            toolbar.addAction(self.action_undo)
            self.action_redo = QtGui.QAction("↷ Redo", self)
            self.action_redo.setShortcut(QtGui.QKeySequence.Redo)
            self.action_redo.triggered.connect(self._redo_clicked)
            toolbar.addAction(self.action_redo)
            toolbar.addSeparator()
            self.action_export = QtGui.QAction("Export…", self)
            self.action_export.triggered.connect(self._export_dialog)
            toolbar.addAction(self.action_export)
            self.action_fit = QtGui.QAction("Fit map", self)
            self.action_fit.triggered.connect(self._fit_map)
            toolbar.addAction(self.action_fit)
            toolbar.addSeparator()
            self.status_label = QtWidgets.QLabel("Ready")
            self.status_label.setStyleSheet("color: #b9c4d4; padding-left: 8px")
            toolbar.addWidget(self.status_label)

            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            outer.addWidget(splitter, 1)

            splitter.addWidget(self._build_controls())
            center = QtWidgets.QWidget()
            center_layout = QtWidgets.QVBoxLayout(center)
            center_layout.setContentsMargins(4, 0, 4, 0)
            self.canvas = MapCanvas()
            self.canvas.objectSelected.connect(self._object_selected)
            self.canvas.statusChanged.connect(self._set_status)
            center_layout.addWidget(self.canvas, 1)
            self.map_hint = QtWidgets.QLabel("Click an object to inspect · wheel to zoom · drag to pan")
            self.map_hint.setStyleSheet("color: #93a4b8; padding: 3px 6px")
            center_layout.addWidget(self.map_hint)
            splitter.addWidget(center)
            splitter.addWidget(self._build_inspector())
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setStretchFactor(2, 0)
            splitter.setSizes([270, 820, 310])

            footer = QtWidgets.QHBoxLayout()
            footer.setSpacing(16)
            outer.addLayout(footer)
            self.progress = QtWidgets.QProgressBar()
            self.progress.setRange(0, 0)
            self.progress.setVisible(False)
            self.progress.setFixedWidth(180)
            footer.addWidget(self.progress)
            footer.addWidget(QtWidgets.QLabel("Camera:"))
            self.camera_combo = QtWidgets.QComboBox()
            self.camera_combo.addItems(["Top Down", "Isometric", "Perspective", "Overview"])
            self.camera_combo.currentTextChanged.connect(self.canvas.set_view_mode)
            footer.addWidget(self.camera_combo)
            footer.addWidget(QtWidgets.QLabel("Layers:"))
            self.layer_checks: dict[str, QtWidgets.QCheckBox] = {}
            for layer in ("terrain", "water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "poi"):
                check = QtWidgets.QCheckBox(layer.title())
                check.setChecked(True)
                check.toggled.connect(lambda value, l=layer: self.canvas.set_layer_visible(l, value))
                self.layer_checks[layer] = check
                footer.addWidget(check)
            footer.addStretch(1)
            self.help_label = QtWidgets.QLabel("Seed-locked, reproducible generation")
            self.help_label.setStyleSheet("color: #93a4b8")
            footer.addWidget(self.help_label)

            self.setStyleSheet(
                """
                QMainWindow, QWidget { background: #111827; color: #e5e7eb; }
                QGroupBox { border: 1px solid #374151; border-radius: 5px; margin-top: 8px; padding-top: 8px; }
                QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9fb6d0; }
                QPushButton { background: #243449; border: 1px solid #42607d; border-radius: 4px; padding: 5px 8px; }
                QPushButton:hover { background: #2e4a68; }
                QPushButton:disabled { color: #718096; background: #1a2533; }
                QLineEdit, QSpinBox, QComboBox, QTableWidget, QListWidget { background: #172234; border: 1px solid #34445b; border-radius: 3px; padding: 3px; }
                QHeaderView::section { background: #243449; color: #c8d4e4; padding: 4px; border: 0; }
                QToolBar { background: #172234; border: 0; spacing: 5px; }
                QToolButton { padding: 4px; }
                QProgressBar { border: 1px solid #425570; text-align: center; border-radius: 3px; }
                QProgressBar::chunk { background: #4f86b8; }
                """
            )

        def _group(self, title: str) -> QtWidgets.QGroupBox:
            box = QtWidgets.QGroupBox(title)
            box.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
            return box

        def _build_controls(self) -> QtWidgets.QWidget:
            panel = QtWidgets.QWidget()
            panel.setMinimumWidth(250)
            panel.setMaximumWidth(330)
            layout = QtWidgets.QVBoxLayout(panel)
            layout.setContentsMargins(2, 0, 2, 0)
            layout.setSpacing(7)

            generation = self._group("World Generation")
            form = QtWidgets.QFormLayout(generation)
            self.seed_spin = QtWidgets.QSpinBox()
            self.seed_spin.setRange(-2147483648, 2147483647)
            self.seed_spin.setValue(int(self.adapter.world.get("seed", 42)))
            self.seed_spin.setToolTip("Same seed and parameters reproduce the same world")
            form.addRow("Seed", self.seed_spin)
            self.terrain_combo = QtWidgets.QComboBox()
            self.terrain_combo.addItems(["Flat", "Island", "Coast", "River", "Hills"])
            self.terrain_combo.setCurrentText(str(self.adapter.parameters.get("terrain", "Coast")))
            form.addRow("Terrain", self.terrain_combo)
            self.road_combo = QtWidgets.QComboBox()
            self.road_combo.addItems(["Grid", "Organic", "Radial", "Hybrid"])
            self.road_combo.setCurrentText(str(self.adapter.parameters.get("road_pattern", "Hybrid")))
            form.addRow("Road pattern", self.road_combo)
            self.difficulty_spin = QtWidgets.QSpinBox()
            self.difficulty_spin.setRange(1, 5)
            self.difficulty_spin.setValue(int(self.adapter.parameters.get("difficulty", 3)))
            form.addRow("Difficulty", self.difficulty_spin)
            self.generate_button = QtWidgets.QPushButton("Generate World")
            self.generate_button.setDefault(True)
            self.generate_button.clicked.connect(self._generate_clicked)
            form.addRow(self.generate_button)
            layout.addWidget(generation)

            locks = self._group("Seed Locks")
            lock_layout = QtWidgets.QGridLayout(locks)
            self.lock_checks: dict[str, QtWidgets.QCheckBox] = {}
            for i, component in enumerate(("terrain", "roads", "districts", "buildings")):
                check = QtWidgets.QCheckBox(component.title())
                check.setChecked(bool(self.adapter.world.get("locks", {}).get(component, False)))
                check.toggled.connect(lambda value, c=component: self.adapter.set_lock(c, value))
                self.lock_checks[component] = check
                lock_layout.addWidget(check, i // 2, i % 2)
            lock_note = QtWidgets.QLabel("Locks keep components stable across full regeneration.")
            lock_note.setWordWrap(True)
            lock_note.setStyleSheet("color: #93a4b8; font-size: 11px")
            lock_layout.addWidget(lock_note, 2, 0, 1, 2)
            layout.addWidget(locks)

            regen = self._group("Partial Regeneration")
            regen_layout = QtWidgets.QGridLayout(regen)
            for i, component in enumerate(("roads", "districts", "buildings", "poi")):
                button = QtWidgets.QPushButton(component.title())
                button.setToolTip(f"Regenerate only {component}; preserve other layers")
                button.clicked.connect(lambda checked=False, c=component: self._regenerate_clicked(c))
                regen_layout.addWidget(button, i // 2, i % 2)
            layout.addWidget(regen)

            export = self._group("Export")
            export_layout = QtWidgets.QVBoxLayout(export)
            self.export_json_button = QtWidgets.QPushButton("Export JSON + GeoJSON")
            self.export_json_button.clicked.connect(lambda: self._export_dialog("json"))
            export_layout.addWidget(self.export_json_button)
            self.export_visual_button = QtWidgets.QPushButton("Export PNG + OBJ")
            self.export_visual_button.clicked.connect(lambda: self._export_dialog("visual"))
            export_layout.addWidget(self.export_visual_button)
            self.export_gltf_button = QtWidgets.QPushButton("Export glTF")
            self.export_gltf_button.clicked.connect(lambda: self._export_dialog("gltf"))
            export_layout.addWidget(self.export_gltf_button)
            layout.addWidget(export)
            layout.addStretch(1)
            return panel

        def _build_inspector(self) -> QtWidgets.QWidget:
            panel = QtWidgets.QWidget()
            panel.setMinimumWidth(280)
            panel.setMaximumWidth(380)
            layout = QtWidgets.QVBoxLayout(panel)
            layout.setContentsMargins(2, 0, 2, 0)
            layout.setSpacing(7)

            inspector = self._group("World Inspector")
            insp_layout = QtWidgets.QVBoxLayout(inspector)
            self.selection_title = QtWidgets.QLabel("No object selected")
            self.selection_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #f2d79b")
            self.selection_title.setWordWrap(True)
            insp_layout.addWidget(self.selection_title)
            self.inspector_table = QtWidgets.QTableWidget(0, 2)
            self.inspector_table.setHorizontalHeaderLabels(["Property", "Value"])
            self.inspector_table.horizontalHeader().setStretchLastSection(True)
            self.inspector_table.verticalHeader().setVisible(False)
            self.inspector_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.inspector_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            insp_layout.addWidget(self.inspector_table)
            layout.addWidget(inspector, 1)

            edit_box = self._group("Edit Selected")
            edit_layout = QtWidgets.QGridLayout(edit_box)
            self.edit_dx = QtWidgets.QDoubleSpinBox()
            self.edit_dx.setRange(-10000.0, 10000.0)
            self.edit_dx.setDecimals(2)
            self.edit_dx.setSingleStep(1.0)
            self.edit_dy = QtWidgets.QDoubleSpinBox()
            self.edit_dy.setRange(-10000.0, 10000.0)
            self.edit_dy.setDecimals(2)
            self.edit_dy.setSingleStep(1.0)
            self.edit_floors = QtWidgets.QSpinBox()
            self.edit_floors.setRange(1, 100)
            self.edit_floors.setValue(1)
            edit_layout.addWidget(QtWidgets.QLabel("Move X"), 0, 0)
            edit_layout.addWidget(self.edit_dx, 0, 1)
            edit_layout.addWidget(QtWidgets.QLabel("Move Y"), 1, 0)
            edit_layout.addWidget(self.edit_dy, 1, 1)
            edit_layout.addWidget(QtWidgets.QLabel("Floors"), 2, 0)
            edit_layout.addWidget(self.edit_floors, 2, 1)
            self.apply_edit_button = QtWidgets.QPushButton("Apply edit")
            self.apply_edit_button.setToolTip("Move the selected object; for buildings also update floors")
            self.apply_edit_button.clicked.connect(self._apply_feature_edit)
            self.apply_edit_button.setEnabled(False)
            edit_layout.addWidget(self.apply_edit_button, 3, 0, 1, 2)
            edit_note = QtWidgets.QLabel("Edits are serialised and undoable.")
            edit_note.setStyleSheet("color: #93a4b8; font-size: 11px")
            edit_layout.addWidget(edit_note, 4, 0, 1, 2)
            layout.addWidget(edit_box)

            stats = self._group("Statistics")
            stat_layout = QtWidgets.QFormLayout(stats)
            self.stat_labels: dict[str, QtWidgets.QLabel] = {}
            labels = {
                "road_length": "Road length",
                "building_count": "Buildings",
                "density": "Density / 10k",
                "district_area": "District area",
                "green_area": "Green area",
                "average_building_height": "Avg. building height",
            }
            for key, text in labels.items():
                value = QtWidgets.QLabel("—")
                value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.stat_labels[key] = value
                stat_layout.addRow(text, value)
            layout.addWidget(stats)

            history = self._group("Generator History")
            hist_layout = QtWidgets.QVBoxLayout(history)
            self.history_list = QtWidgets.QListWidget()
            self.history_list.setToolTip("Seed, parameters, generator version and result metadata")
            self.history_list.itemDoubleClicked.connect(self._history_item_clicked)
            hist_layout.addWidget(self.history_list)
            layout.addWidget(history, 1)
            return panel

        # ---- state and actions -----------------------------------------------
        def _parameters(self) -> dict[str, Any]:
            return {
                "terrain_type": self.terrain_combo.currentText().lower(),
                "road_pattern": self.road_combo.currentText(),
                "difficulty": self.difficulty_spin.value(),
            }

        def _generate_clicked(self) -> None:
            self._start_worker("generate")

        def _regenerate_clicked(self, component: str) -> None:
            self._start_worker(component)

        def _start_worker(self, action: str) -> None:
            if self._busy:
                return
            self._busy = True
            self._set_status(f"Generating {action}…")
            self.progress.setVisible(True)
            self.generate_button.setEnabled(False)
            self.action_generate.setEnabled(False)
            self._worker = GenerationWorker(self.adapter, action, self.seed_spin.value(), self._parameters())
            self._worker.signals.finished.connect(self._generation_finished)
            self._worker.signals.failed.connect(self._generation_failed)
            self.thread_pool.start(self._worker)

        @QtCore.Slot(object, str)
        def _generation_finished(self, world: Mapping[str, Any], action: str) -> None:
            self._busy = False
            self.progress.setVisible(False)
            self.generate_button.setEnabled(True)
            self.action_generate.setEnabled(True)
            self.seed_spin.setValue(int(_first(world, "seed", default=self.seed_spin.value()) or self.seed_spin.value()))
            # A locked terrain/road layer may intentionally retain its prior
            # preset when the root seed changes; reflect the effective world
            # values in the controls instead of leaving a misleading request
            # selected in the comboboxes.
            terrain_value = str(_first(world, "terrain_type", default="")).replace("_", " ").title()
            if terrain_value and self.terrain_combo.findText(terrain_value) >= 0:
                self.terrain_combo.setCurrentText(terrain_value)
            road_value = str(_first(world.get("metadata", {}) if isinstance(world.get("metadata"), Mapping) else {}, "road_pattern", default="")).replace("_", " ").title()
            if road_value and self.road_combo.findText(road_value) >= 0:
                self.road_combo.setCurrentText(road_value)
            self._set_status(f"Completed {action}: {len(_flatten_objects(world))} objects")
            self._refresh_all()

        @QtCore.Slot(str)
        def _generation_failed(self, error: str) -> None:
            self._busy = False
            self.progress.setVisible(False)
            self.generate_button.setEnabled(True)
            self.action_generate.setEnabled(True)
            self._set_status("Generation failed")
            QtWidgets.QMessageBox.critical(self, "Generation failed", error[-4000:])

        def _undo_clicked(self) -> None:
            if self.adapter.undo() is not None:
                self._refresh_all()
                self._set_status("Undo")

        def _redo_clicked(self) -> None:
            if self.adapter.redo() is not None:
                self._refresh_all()
                self._set_status("Redo")

        def _object_selected(self, obj: Optional[Mapping[str, Any]]) -> None:
            self._selected_object = dict(obj) if obj else None
            if not obj:
                self.selection_title.setText("No object selected")
                self.inspector_table.setRowCount(0)
                self.apply_edit_button.setEnabled(False)
                return
            typ = _object_type(obj)
            oid = _object_id(obj, "")
            self.selection_title.setText(f"{typ} · {oid}")
            nested_parameters = obj.get("properties") if isinstance(obj.get("properties"), Mapping) else {}
            district = _first(obj, "district", "district_id", "zone", default=_first(nested_parameters, "district", "district_id", "zone", default="—"))
            if district in (None, "", "—"):
                district = self._district_for(obj) or "—"
            parent = _first(obj, "parent", "parent_id", default="—")
            seed = _first(obj, "seed", default=self.adapter.world.get("seed", "—"))
            rows = [
                ("ID", oid),
                ("Type", typ),
                ("District", str(district) if district not in (None, "") else "—"),
                ("Parent", str(parent) if parent not in (None, "") else "—"),
                ("Seed", str(seed) if seed not in (None, "") else "—"),
                ("Parameters", json.dumps(_as_dict(nested_parameters), ensure_ascii=False) if nested_parameters else "—"),
            ]
            canonical = {"id", "type", "district", "district_id", "zone", "parent", "parent_id", "seed", "properties"}
            for key, value in obj.items():
                if key.startswith("_") or key.lower() in canonical or key in {"geometry", "polygon", "points", "vertices", "path"}:
                    continue
                text = json.dumps(_as_dict(value), ensure_ascii=False) if isinstance(value, (Mapping, list, tuple)) else str(value)
                rows.append((key, text))
            geometry = _geometry_points(obj)
            rows.append(("geometry", f"{len(geometry)} points"))
            self.inspector_table.setRowCount(len(rows))
            for row, (key, value) in enumerate(rows):
                self.inspector_table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(key)))
                self.inspector_table.setItem(row, 1, QtWidgets.QTableWidgetItem(value))
            self.inspector_table.resizeColumnsToContents()
            self.apply_edit_button.setEnabled(True)
            floors_value = _first(nested_parameters, "floors", default=_first(obj, "floors", default=1))
            try:
                self.edit_floors.setValue(max(1, int(floors_value)))
            except (TypeError, ValueError):
                self.edit_floors.setValue(1)

        def _apply_feature_edit(self) -> None:
            if not self._selected_object:
                return
            feature_id = _object_id(self._selected_object, "")
            if not feature_id:
                return
            typ = _object_type(self._selected_object).lower()
            properties: dict[str, Any] = {}
            if typ == "building":
                properties["floors"] = self.edit_floors.value()
            try:
                self.adapter.edit_feature(
                    feature_id,
                    dx=self.edit_dx.value(),
                    dy=self.edit_dy.value(),
                    properties=properties,
                )
                self.edit_dx.setValue(0.0)
                self.edit_dy.setValue(0.0)
                self._refresh_all()
                self._set_status(f"Edited {feature_id}")
            except Exception as exc:
                QtWidgets.QMessageBox.critical(self, "Edit failed", str(exc))

        def _district_for(self, obj: Mapping[str, Any]) -> Optional[str]:
            """Follow editable parent links until the containing district."""
            index = {_object_id(candidate, ""): candidate for candidate in _flatten_objects(self.adapter.world)}
            current: Optional[Mapping[str, Any]] = obj
            visited: set[str] = set()
            for _ in range(8):
                if not current:
                    return None
                oid = _object_id(current, "")
                if oid in visited:
                    return None
                visited.add(oid)
                if _object_type(current).lower() == "district" or str(current.get("_layer", "")) == "districts":
                    return str(_first(current, "name", "district_type", "id", default=oid))
                parent_id = _first(current, "parent", "parent_id", default=None)
                current = index.get(str(parent_id)) if parent_id else None
            return None

        def _history_item_clicked(self, item: QtWidgets.QListWidgetItem) -> None:
            entry = item.data(QtCore.Qt.UserRole)
            if not entry:
                return
            if isinstance(entry, str):
                try:
                    entry = json.loads(entry)
                except Exception:
                    entry = {"seed": entry}
            self._set_status(f"History: seed {entry.get('seed')} · {entry.get('generator_version')}")

        def _refresh_all(self) -> None:
            self.canvas.set_world(self.adapter.world)
            locks = self.adapter.world.get("locks", {})
            for component, check in self.lock_checks.items():
                value = bool(locks.get(component, locks.get(component.rstrip("s"), False)))
                check.blockSignals(True)
                check.setChecked(value)
                check.blockSignals(False)
            self._refresh_statistics()
            self._refresh_history()
            self.action_undo.setEnabled(bool(self.adapter._undo))
            self.action_redo.setEnabled(bool(self.adapter._redo))

        def _refresh_statistics(self) -> None:
            stats = self.adapter.statistics()
            for key, label in self.stat_labels.items():
                value = stats.get(key, 0)
                if key in {"building_count"}:
                    label.setText(f"{int(value):,}")
                elif key == "density":
                    label.setText(f"{value:,.3f}")
                else:
                    label.setText(f"{value:,.2f}")

        def _refresh_history(self) -> None:
            self.history_list.clear()
            # Keep both backend pipeline history and editor actions.  Showing
            # only the adapter's last action would hide the terrain/road/
            # district provenance that makes a world reproducible.
            raw = self.adapter.world.get("generator_history", []) or []
            raw_entries = [
                HistoryEntry(
                    str(e.get("timestamp", "")),
                    int(e.get("seed") if e.get("seed") is not None else e.get("root_seed", 0)),
                    {**dict(e.get("parameters", {})), **({"stage": e.get("stage")} if e.get("stage") else {})},
                    str(e.get("generator_version", "")),
                    dict(e.get("result_metadata", {})),
                )
                for e in raw
                if isinstance(e, Mapping)
            ]
            entries = raw_entries + list(self.adapter.history)
            for entry in entries[-100:]:
                stage = entry.parameters.get("stage", "") if isinstance(entry.parameters, Mapping) else ""
                suffix = f"  ·  {stage}" if stage else ""
                text = f"{entry.timestamp}  ·  seed {entry.seed}  ·  {entry.generator_version}{suffix}"
                item = QtWidgets.QListWidgetItem(text)
                # Qt's signed integer QVariant conversion overflows on the
                # core's 64-bit stage seeds.  Store JSON text to preserve the
                # complete metadata losslessly across platforms.
                item.setData(QtCore.Qt.UserRole, json.dumps(_as_dict(entry), ensure_ascii=False))
                self.history_list.addItem(item)

        def _set_status(self, text: str) -> None:
            self.status_label.setText(text)

        def _fit_map(self) -> None:
            self.canvas._zoom = 1.0
            self.canvas._pan = QtCore.QPointF(0, 0)
            self.canvas.update()

        def _export_dialog(self, mode: str = "all") -> None:
            directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose export directory")
            if not directory:
                return
            try:
                out = Path(directory)
                if mode == "json":
                    files = [self.adapter.export_json(out / "world.json"), self.adapter.export_geojson(out / "world.geojson")]
                elif mode == "visual":
                    files = [self.adapter.export_png(out / "world.png"), self.adapter.export_obj(out / "world.obj")]
                elif mode == "gltf":
                    files = [self.adapter.export_gltf(out / "world.gltf")]
                else:
                    files = self.adapter.export_all(out)
                    files.append(self.adapter.export_gltf(out / "world.gltf"))
                self._set_status("Exported " + ", ".join(p.name for p in files))
            except Exception as exc:
                QtWidgets.QMessageBox.critical(self, "Export failed", str(exc))

        def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
            if self._busy:
                answer = QtWidgets.QMessageBox.question(self, "Generation in progress", "Stop the current generation and close?")
                if answer != QtWidgets.QMessageBox.Yes:
                    event.ignore()
                    return
            event.accept()


else:

    class MainWindow:  # pragma: no cover - fallback used only without Qt
        """Minimal tkinter editor for machines without PySide6."""

        def __init__(self, adapter: Optional[WorldAdapter] = None, parent: Any = None):
            import tkinter as tk
            from tkinter import ttk

            self.adapter = adapter or WorldAdapter()
            self.root = parent if parent is not None else tk.Tk()
            self.root.title("Procedural World Studio — 程序化世界生成工作台")
            self.root.geometry("1180x760")
            self.seed = tk.IntVar(value=int(self.adapter.world.get("seed", 42)))
            self.status = tk.StringVar(value="Ready")
            self._canvas = tk.Canvas(self.root, background="#111827", highlightthickness=0)
            self._canvas.pack(side="right", fill="both", expand=True)
            panel = ttk.Frame(self.root, padding=8)
            panel.pack(side="left", fill="y")
            ttk.Label(panel, text="World Generation", font=("Segoe UI", 12, "bold")).pack(anchor="w")
            ttk.Label(panel, text="Seed").pack(anchor="w", pady=(12, 0))
            ttk.Entry(panel, textvariable=self.seed, width=16).pack(anchor="w")
            ttk.Button(panel, text="Generate World", command=self.generate).pack(fill="x", pady=8)
            ttk.Label(panel, text="Partial Regeneration", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(14, 2))
            for component in ("roads", "districts", "buildings", "poi"):
                ttk.Button(panel, text=component.title(), command=lambda c=component: self.regenerate(c)).pack(fill="x", pady=2)
            ttk.Button(panel, text="Undo", command=lambda: (self.adapter.undo(), self.render())).pack(fill="x", pady=(14, 2))
            ttk.Button(panel, text="Redo", command=lambda: (self.adapter.redo(), self.render())).pack(fill="x", pady=2)
            ttk.Label(panel, textvariable=self.status, wraplength=210).pack(anchor="w", pady=18)
            self.render()

        def generate(self) -> None:
            self.adapter.generate_sync(self.seed.get())
            self.status.set("World generated")
            self.render()

        def regenerate(self, component: str) -> None:
            self.adapter.regenerate(component, self.seed.get())
            self.status.set(f"Regenerated {component}")
            self.render()

        def render(self) -> None:
            self._canvas.delete("all")
            objects = _flatten_objects(self.adapter.world)
            xmin, ymin, xmax, ymax = _bbox(self.adapter.world, objects)
            width = max(1, self._canvas.winfo_width())
            height = max(1, self._canvas.winfo_height())
            scale = min((width - 20) / (xmax - xmin), (height - 20) / (ymax - ymin))
            def tr(p): return ((p[0] - xmin) * scale + 10, (p[1] - ymin) * scale + 10)
            for obj in objects:
                pts = _geometry_points(obj)
                if len(pts) < 2:
                    continue
                xy = [v for p in pts for v in tr(p)]
                layer = str(obj.get("_layer", ""))
                if layer == "roads":
                    self._canvas.create_line(*xy, fill="#e5c98e", width=2)
                elif len(pts) >= 3:
                    self._canvas.create_polygon(*xy, fill=_LAYER_COLORS.get(layer, "#8892a4"), outline="#263242")

        def show(self) -> None:
            self.root.mainloop()


def _headless(seed: int, export: Optional[str] = None) -> int:
    adapter = WorldAdapter()
    adapter.generate_sync(seed)
    stats = adapter.statistics()
    print(json.dumps({"seed": seed, "objects": len(_flatten_objects(adapter.world)), "statistics": stats}, ensure_ascii=False, indent=2))
    if export:
        files = adapter.export_all(export)
        print("Exported:")
        for path in files:
            print(path)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Procedural World Studio")
    parser.add_argument("--headless", action="store_true", help="generate/export without opening a window")
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--export", metavar="DIR", help="export JSON, GeoJSON, PNG and OBJ to DIR")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.headless:
        return _headless(args.seed, args.export)
    if QT_AVAILABLE:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        app.setApplicationName("Procedural World Studio")
        window = MainWindow(WorldAdapter())
        window.show()
        return int(app.exec())
    window = MainWindow(WorldAdapter())
    window.show()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
