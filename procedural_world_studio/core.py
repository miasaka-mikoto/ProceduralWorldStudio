"""Deterministic, editable procedural world generation engine.

This module contains the data model and a dependency-aware generator for the
Procedural World Studio application.  It intentionally uses only Python's
standard library so the same engine can run in a Windows desktop build, a
command-line smoke test, or a future game-engine exporter.

Geometry is represented using small GeoJSON-compatible dictionaries.  The
engine is not a renderer: every stage creates inspectable features with an
ID, parent relationship, parameters and a stage seed.  This makes partial
regeneration and reproducibility practical rather than just visual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple


Point = Tuple[float, float]
Polygon = List[Point]

GENERATOR_VERSION = "0.1.0"
STAGES = (
    "terrain",
    "water",
    "roads",
    "districts",
    "blocks",
    "lots",
    "buildings",
    "harbor",
    "poi",
    "population",
)
ENTITY_STAGES = ("water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois")


# ---------------------------------------------------------------------------
# Deterministic helpers and geometry primitives
# ---------------------------------------------------------------------------


def stable_seed(*parts: Any) -> int:
    """Return a stable 64-bit seed for arbitrary JSON-like values.

    Python's built-in ``hash`` is intentionally salted per process.  We use a
    SHA-256 digest instead so a world can be regenerated on another machine
    and still produce the same geometry.
    """

    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False)


def _rng(*parts: Any) -> random.Random:
    return random.Random(stable_seed(*parts))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_point(p: Point, digits: int = 3) -> Point:
    return (round(float(p[0]), digits), round(float(p[1]), digits))


def _closed(poly: Sequence[Point]) -> List[Point]:
    points = [_round_point(p) for p in poly]
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def polygon_area(poly: Sequence[Point]) -> float:
    points = list(poly)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return 0.0
    return abs(sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points))) / 2.0)


def polygon_centroid(poly: Sequence[Point]) -> Point:
    points = list(poly)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        if not points:
            return (0.0, 0.0)
        return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    signed = sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))
    if abs(signed) < 1e-9:
        return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    cx = sum((points[i][0] + points[(i + 1) % len(points)][0]) * (points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]) for i in range(len(points))) / (3 * signed)
    cy = sum((points[i][1] + points[(i + 1) % len(points)][1]) * (points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]) for i in range(len(points))) / (3 * signed)
    return (cx, cy)


def line_length(points: Sequence[Point]) -> float:
    return sum(math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]) for i in range(len(points) - 1))


def bbox_of_geometry(geometry: Mapping[str, Any]) -> Tuple[float, float, float, float]:
    coords = geometry.get("coordinates", [])
    kind = geometry.get("type")
    flat: List[Point] = []
    if kind == "Point":
        flat = [tuple(coords[:2])] if coords else []
    elif kind == "LineString":
        flat = [tuple(p[:2]) for p in coords]
    elif kind == "Polygon":
        flat = [tuple(p[:2]) for ring in coords for p in ring]
    elif kind == "MultiLineString":
        flat = [tuple(p[:2]) for line in coords for p in line]
    elif kind == "MultiPolygon":
        flat = [tuple(p[:2]) for poly in coords for ring in poly for p in ring]
    if not flat:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    return (min(xs), min(ys), max(xs), max(ys))


def geometry_centroid(geometry: Mapping[str, Any]) -> Point:
    kind = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if kind == "Point":
        return (float(coords[0]), float(coords[1]))
    if kind == "LineString":
        if not coords:
            return (0.0, 0.0)
        return (sum(p[0] for p in coords) / len(coords), sum(p[1] for p in coords) / len(coords))
    if kind == "Polygon" and coords:
        return polygon_centroid(coords[0])
    if kind == "MultiPolygon" and coords:
        poly = coords[0][0] if coords[0] else []
        return polygon_centroid(poly)
    return (0.0, 0.0)


def translate_geometry(geometry: Mapping[str, Any], dx: float, dy: float) -> Dict[str, Any]:
    """Return a translated copy of a simple GeoJSON geometry.

    This is deliberately geometry-type aware instead of recursively adding to
    every number (which would corrupt a 3-D Z coordinate or a feature
    property).  It supports all geometry forms emitted by the generator and
    is used by the manual editing API below.
    """

    result = deepcopy(dict(geometry))
    kind = result.get("type")
    coords = result.get("coordinates")

    def move(point: Sequence[float]) -> List[float]:
        moved = list(point)
        if len(moved) >= 2:
            moved[0] = round(float(moved[0]) + dx, 3)
            moved[1] = round(float(moved[1]) + dy, 3)
        return moved

    if kind == "Point" and isinstance(coords, Sequence):
        result["coordinates"] = move(coords)
    elif kind == "LineString" and isinstance(coords, Sequence):
        result["coordinates"] = [move(point) for point in coords]
    elif kind == "Polygon" and isinstance(coords, Sequence):
        result["coordinates"] = [[move(point) for point in ring] for ring in coords]
    elif kind == "MultiLineString" and isinstance(coords, Sequence):
        result["coordinates"] = [[move(point) for point in line] for line in coords]
    elif kind == "MultiPolygon" and isinstance(coords, Sequence):
        result["coordinates"] = [[[move(point) for point in ring] for ring in poly] for poly in coords]
    return result


def point_in_polygon(point: Point, poly: Sequence[Point]) -> bool:
    """Ray-casting point-in-polygon test, including boundary tolerance."""

    x, y = point
    points = list(poly)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    inside = False
    n = len(points)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        intersects = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi)
        if intersects:
            inside = not inside
        j = i
    return inside


def rectangle(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _clip_ray_to_bounds(origin: Point, target: Point, width: float, height: float) -> Point:
    """Return the first rectangle-boundary point along an in-bounds ray.

    Radial roads are authored as rays from a downtown centre.  The original
    prototype used a fixed world-size multiplier for their endpoints, which
    let small custom worlds spill outside the editable bounds.  Clipping here
    keeps the topology intact while guaranteeing every generated road remains
    inside the world rectangle.
    """

    ox, oy = origin
    dx, dy = target[0] - ox, target[1] - oy
    candidates: list[float] = []
    if dx > 1e-12:
        candidates.append((width - ox) / dx)
    elif dx < -1e-12:
        candidates.append((0.0 - ox) / dx)
    if dy > 1e-12:
        candidates.append((height - oy) / dy)
    elif dy < -1e-12:
        candidates.append((0.0 - oy) / dy)
    valid = [t for t in candidates if t >= 0.0]
    t = min(valid) if valid else 1.0
    t = min(1.0, max(0.0, t))
    return _round_point((ox + dx * t, oy + dy * t))


def _feature_geometry(kind: str, coordinates: Any) -> Dict[str, Any]:
    return {"type": kind, "coordinates": coordinates}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Feature:
    """An editable world entity with GeoJSON-like geometry and provenance."""

    id: str
    type: str
    geometry: Dict[str, Any]
    properties: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    stage: str = ""
    seed: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "geometry": deepcopy(self.geometry),
            "properties": deepcopy(self.properties),
            "parent_id": self.parent_id,
            "stage": self.stage,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Feature":
        return cls(
            id=str(data["id"]),
            type=str(data.get("type", "Feature")),
            geometry=deepcopy(dict(data.get("geometry", {}))),
            properties=deepcopy(dict(data.get("properties", {}))),
            parent_id=data.get("parent_id"),
            stage=str(data.get("stage", "")),
            seed=data.get("seed"),
        )


@dataclass
class WorldState:
    """Complete serialisable state of a generated world."""

    project_name: str = "Procedural World Studio"
    seed: int = 20261004
    width: float = 1200.0
    height: float = 900.0
    terrain_type: str = "coast"
    terrain: Dict[str, Any] = field(default_factory=dict)
    water: Dict[str, Feature] = field(default_factory=dict)
    roads: Dict[str, Feature] = field(default_factory=dict)
    districts: Dict[str, Feature] = field(default_factory=dict)
    blocks: Dict[str, Feature] = field(default_factory=dict)
    lots: Dict[str, Feature] = field(default_factory=dict)
    buildings: Dict[str, Feature] = field(default_factory=dict)
    harbor: Dict[str, Feature] = field(default_factory=dict)
    pois: Dict[str, Feature] = field(default_factory=dict)
    population: Dict[str, Any] = field(default_factory=dict)
    locks: Dict[str, bool] = field(default_factory=lambda: {s: False for s in ("terrain", "road", "district", "building")})
    stage_revisions: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAGES})
    stage_seeds: Dict[str, int] = field(default_factory=dict)
    generator_history: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _spatial_index: Any = field(default=None, repr=False, compare=False)

    def entity_groups(self) -> Dict[str, Dict[str, Feature]]:
        return {
            "water": self.water,
            "roads": self.roads,
            "districts": self.districts,
            "blocks": self.blocks,
            "lots": self.lots,
            "buildings": self.buildings,
            "harbor": self.harbor,
            "pois": self.pois,
        }

    def all_features(self) -> Iterator[Feature]:
        for group in self.entity_groups().values():
            yield from group.values()

    def get_feature(self, feature_id: str) -> Optional[Feature]:
        for group in self.entity_groups().values():
            if feature_id in group:
                return group[feature_id]
        return None

    def feature_group(self, feature_id: str) -> Optional[Dict[str, Feature]]:
        """Return the owning collection for an entity ID, if present."""

        for group in self.entity_groups().values():
            if feature_id in group:
                return group
        return None

    def height_at(self, x: float, y: float) -> float:
        """Bilinearly sample the generated terrain height map at world XY."""

        heights = self.terrain.get("heights", [])
        if not heights or not heights[0]:
            return 0.0
        rows, cols = len(heights), len(heights[0])
        fx = _clamp(float(x) / max(self.width, 1e-9), 0.0, 1.0) * (cols - 1)
        fy = _clamp(float(y) / max(self.height, 1e-9), 0.0, 1.0) * (rows - 1)
        x0, y0 = int(fx), int(fy)
        x1, y1 = min(x0 + 1, cols - 1), min(y0 + 1, rows - 1)
        tx, ty = fx - x0, fy - y0
        top = float(heights[y0][x0]) * (1.0 - tx) + float(heights[y0][x1]) * tx
        bottom = float(heights[y1][x0]) * (1.0 - tx) + float(heights[y1][x1]) * tx
        return top * (1.0 - ty) + bottom * ty

    def invalidate_index(self) -> None:
        self._spatial_index = None

    def rebuild_spatial_index(self, cell_size: Optional[float] = None) -> "SpatialIndex":
        self._spatial_index = SpatialIndex(cell_size=cell_size)
        self._spatial_index.insert_many(self.all_features())
        return self._spatial_index

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "terrain_type": self.terrain_type,
            "terrain": deepcopy(self.terrain),
            "water": {k: v.to_dict() for k, v in self.water.items()},
            "roads": {k: v.to_dict() for k, v in self.roads.items()},
            "districts": {k: v.to_dict() for k, v in self.districts.items()},
            "blocks": {k: v.to_dict() for k, v in self.blocks.items()},
            "lots": {k: v.to_dict() for k, v in self.lots.items()},
            "buildings": {k: v.to_dict() for k, v in self.buildings.items()},
            "harbor": {k: v.to_dict() for k, v in self.harbor.items()},
            "pois": {k: v.to_dict() for k, v in self.pois.items()},
            "population": deepcopy(self.population),
            "locks": deepcopy(self.locks),
            "stage_revisions": deepcopy(self.stage_revisions),
            "stage_seeds": deepcopy(self.stage_seeds),
            "generator_history": deepcopy(self.generator_history),
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorldState":
        kwargs: Dict[str, Any] = {
            "project_name": str(data.get("project_name", "Procedural World Studio")),
            "seed": int(data.get("seed", 20261004)),
            "width": float(data.get("width", 1200.0)),
            "height": float(data.get("height", 900.0)),
            "terrain_type": str(data.get("terrain_type", "coast")),
            "terrain": deepcopy(dict(data.get("terrain", {}))),
            "population": deepcopy(dict(data.get("population", {}))),
            "locks": deepcopy(dict(data.get("locks", {}))),
            "stage_revisions": deepcopy(dict(data.get("stage_revisions", {}))),
            "stage_seeds": deepcopy(dict(data.get("stage_seeds", {}))),
            "generator_history": deepcopy(list(data.get("generator_history", []))),
            "metadata": deepcopy(dict(data.get("metadata", {}))),
        }
        for key in ("water", "roads", "districts", "blocks", "lots", "buildings", "harbor", "pois"):
            kwargs[key] = {str(k): Feature.from_dict(v) for k, v in dict(data.get(key, {})).items()}
        default_locks = {s: False for s in ("terrain", "road", "district", "building")}
        default_locks.update(kwargs["locks"])
        kwargs["locks"] = default_locks
        default_revisions = {s: 0 for s in STAGES}
        default_revisions.update(kwargs["stage_revisions"])
        kwargs["stage_revisions"] = default_revisions
        result = cls(**kwargs)
        result.invalidate_index()
        return result

    def save_json(self, path: str, *, indent: int = 2) -> None:
        # Match the standalone exporters: saving a project to a new folder is
        # a normal editor action and should not require callers to pre-create
        # its parent directory.
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=indent)

    @classmethod
    def load_json(cls, path: str) -> "WorldState":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


class SpatialIndex:
    """Small uniform-grid spatial index used by the inspector and UI.

    It is intentionally lightweight but reduces object picking from a full
    scan to the nearby cells for large generated cities.
    """

    def __init__(self, cell_size: Optional[float] = None) -> None:
        self.cell_size = float(cell_size or 100.0)
        self._cells: Dict[Tuple[int, int], List[str]] = defaultdict(list)
        self._features: Dict[str, Feature] = {}

    def _keys(self, bbox: Tuple[float, float, float, float]) -> Iterator[Tuple[int, int]]:
        x0, y0, x1, y1 = bbox
        ix0, iy0 = math.floor(x0 / self.cell_size), math.floor(y0 / self.cell_size)
        ix1, iy1 = math.floor(x1 / self.cell_size), math.floor(y1 / self.cell_size)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                yield (ix, iy)

    def insert(self, feature: Feature) -> None:
        self._features[feature.id] = feature
        for key in self._keys(bbox_of_geometry(feature.geometry)):
            if feature.id not in self._cells[key]:
                self._cells[key].append(feature.id)

    def insert_many(self, features: Iterable[Feature]) -> None:
        for feature in features:
            self.insert(feature)

    def query_bbox(self, bbox: Tuple[float, float, float, float]) -> List[Feature]:
        ids: set[str] = set()
        for key in self._keys(bbox):
            ids.update(self._cells.get(key, ()))
        x0, y0, x1, y1 = bbox
        result = []
        for feature_id in ids:
            fb = bbox_of_geometry(self._features[feature_id].geometry)
            if fb[2] >= x0 and fb[0] <= x1 and fb[3] >= y0 and fb[1] <= y1:
                result.append(self._features[feature_id])
        return result

    def query_point(self, point: Point, radius: float = 0.0) -> List[Feature]:
        x, y = point
        return self.query_bbox((x - radius, y - radius, x + radius, y + radius))


class GeometryCache:
    """In-memory stage cache; keys include stage seed and generator revision."""

    def __init__(self) -> None:
        self._cache: Dict[Tuple[Any, ...], Any] = {}

    def get(self, *key: Any) -> Any:
        value = self._cache.get(tuple(key))
        return deepcopy(value) if value is not None else None

    def put(self, value: Any, *key: Any) -> None:
        self._cache[tuple(key)] = deepcopy(value)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# World generator
# ---------------------------------------------------------------------------


class WorldGenerator:
    """Generate and edit a procedural world through dependency-aware stages."""

    DISTRICT_TYPES = ("residential", "commercial", "industrial", "old_town", "business", "harbor", "park", "station_area")
    ROAD_CLASSES = ("main", "secondary", "street", "alley")

    def __init__(self, seed: int = 20261004, width: float = 1200.0, height: float = 900.0, *, generator_version: str = GENERATOR_VERSION) -> None:
        self.seed = int(seed)
        self.width = float(width)
        self.height = float(height)
        self.generator_version = generator_version
        self.state: Optional[WorldState] = None
        self.cache = GeometryCache()
        self._undo: List[Dict[str, Any]] = []
        self._redo: List[Dict[str, Any]] = []
        self._last_params: Dict[str, Any] = {}

    # ---- seed/lock and lifecycle -------------------------------------------------

    def _stage_seed(self, state: WorldState, stage: str) -> int:
        revision = int(state.stage_revisions.get(stage, 0))
        if stage in state.stage_seeds and state.locks.get(self._lock_name(stage), False):
            return int(state.stage_seeds[stage])
        return stable_seed(state.seed, stage, revision, self.generator_version)

    def _cache_key(self, state: WorldState, stage: str, *parts: Any) -> tuple[Any, ...]:
        """Build a hashable cache key for deterministic stage geometry."""

        normalized = tuple(json.dumps(part, sort_keys=True, default=str, separators=(",", ":")) for part in parts)
        return (self.generator_version, round(state.width, 6), round(state.height, 6), int(state.seed), stage, *normalized)

    @staticmethod
    def _lock_name(stage: str) -> str:
        return {"roads": "road", "districts": "district", "buildings": "building"}.get(stage, stage)

    def set_lock(self, stage: str, locked: bool = True, state: Optional[WorldState] = None) -> WorldState:
        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        name = self._lock_name(stage)
        if name not in target.locks:
            raise ValueError(f"Unknown lock stage: {stage}")
        target.locks[name] = bool(locked)
        return target

    def set_seed(self, seed: int, *, preserve_locks: bool = True) -> None:
        self.seed = int(seed)
        if self.state is not None:
            self.state.seed = self.seed
            if not preserve_locks:
                self.state.stage_seeds.clear()
                self.state.locks = {k: False for k in self.state.locks}

    def _push_undo(self, state: Optional[WorldState] = None) -> None:
        snapshot_source = state or self.state
        if snapshot_source is not None:
            self._undo.append(snapshot_source.to_dict())
            if len(self._undo) > 30:
                self._undo.pop(0)
            self._redo.clear()

    def undo(self) -> Optional[WorldState]:
        if not self._undo or self.state is None:
            return self.state
        self._redo.append(self.state.to_dict())
        self.state = WorldState.from_dict(self._undo.pop())
        return self.state

    def redo(self) -> Optional[WorldState]:
        if not self._redo or self.state is None:
            return self.state
        self._undo.append(self.state.to_dict())
        self.state = WorldState.from_dict(self._redo.pop())
        return self.state

    def _record_manual_edit(self, state: WorldState, action: str, feature: Feature) -> None:
        """Add a deterministic history entry for a direct editor operation."""

        run_index = len(state.generator_history) + 1
        state.generator_history.append({
            "timestamp": f"run-{run_index:04d}",
            "run_index": run_index,
            "stage": "edit",
            "action": action,
            "feature_id": feature.id,
            "seed": feature.seed,
            "root_seed": state.seed,
            "parameters": {"type": feature.type, "parent_id": feature.parent_id},
            "generator_version": self.generator_version,
            "result_metadata": {"count": 1, "stats": self.statistics(state)},
        })
        state.metadata["last_edit"] = {"action": action, "feature_id": feature.id, "run_index": run_index}

    def add_feature(self, feature: Feature, *, group: Optional[str] = None, state: Optional[WorldState] = None) -> Feature:
        """Insert an inspector-created feature into a world collection.

        ``group`` defaults to the feature stage (``poi`` maps to ``pois``).
        The method is intentionally explicit about collections to avoid
        silently changing generated infrastructure when a user adds an
        annotation or custom POI.
        """

        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        group_name = group or ("pois" if feature.stage == "poi" else feature.stage)
        groups = target.entity_groups()
        if group_name not in groups:
            raise ValueError(f"Unknown feature group: {group_name}")
        if target.get_feature(feature.id) is not None:
            raise ValueError(f"Duplicate feature ID: {feature.id}")
        self._push_undo(target)
        groups[group_name][feature.id] = Feature.from_dict(feature.to_dict())
        inserted = groups[group_name][feature.id]
        target.invalidate_index()
        target.rebuild_spatial_index()
        self._record_manual_edit(target, "add", inserted)
        self.state = target
        return inserted

    def delete_feature(self, feature_id: str, *, cascade: bool = False, state: Optional[WorldState] = None) -> Feature:
        """Remove a feature, optionally including its descendant entities."""

        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        owner = target.feature_group(feature_id)
        if owner is None:
            raise KeyError(f"Unknown feature: {feature_id}")
        self._push_undo(target)
        deleted = owner.pop(feature_id)
        if cascade:
            pending = {feature_id}
            # Descendants can span several groups (district -> block -> lot ->
            # building), so iterate to a fixed point.
            while pending:
                parents = pending
                pending = set()
                for group in target.entity_groups().values():
                    for child_id, child in list(group.items()):
                        if child.parent_id in parents:
                            del group[child_id]
                            pending.add(child_id)
        target.invalidate_index()
        target.rebuild_spatial_index()
        self._record_manual_edit(target, "delete", deleted)
        self.state = target
        return deleted

    def _record(self, state: WorldState, stage: str, params: Mapping[str, Any], result_count: int) -> None:
        # Use a logical run marker rather than wall-clock time.  A generated
        # world can therefore be byte-for-byte reproducible for a fixed seed,
        # while the monotonically increasing index still makes generator
        # history easy to read and replay.
        run_index = len(state.generator_history) + 1
        state.generator_history.append({
            "timestamp": f"run-{run_index:04d}",
            "run_index": run_index,
            "stage": stage,
            "seed": state.stage_seeds.get(stage),
            "root_seed": state.seed,
            "parameters": deepcopy(dict(params)),
            "generator_version": self.generator_version,
            "result_metadata": {"count": result_count, "stats": self.statistics(state)},
        })

    def _new_state(self, terrain_type: str) -> WorldState:
        state = WorldState(seed=self.seed, width=self.width, height=self.height, terrain_type=terrain_type)
        state.metadata.update({
            "generator_version": self.generator_version,
            "pipeline": list(STAGES),
            "created_seed": self.seed,
            "future_engine_targets": ["godot", "unreal"],
        })
        return state

    def generate(
        self,
        terrain_type: str = "coast",
        *,
        road_pattern: str = "hybrid",
        include_harbor: bool = True,
        terrain_params: Optional[Mapping[str, Any]] = None,
        road_params: Optional[Mapping[str, Any]] = None,
        city_name: str = "Coastal City",
        # Compatibility aliases used by the desktop adapter and early CLI
        # prototypes.  Keeping them here means the UI can pass a plain
        # ``terrain=...`` parameter without a bespoke adapter branch.
        terrain: Optional[str] = None,
        road_type: Optional[str] = None,
        seed: Optional[int] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        existing_world: Optional[Mapping[str, Any]] = None,
        world: Optional[Mapping[str, Any]] = None,
        regenerate: Optional[str] = None,
        component: Optional[str] = None,
        difficulty: Optional[int] = None,
        preserve_locks: bool = False,
        **kwargs: Any,
    ) -> WorldState:
        """Generate all pipeline stages and return a fresh :class:`WorldState`."""

        # Normalize aliases before validating.  ``parameters`` is accepted as
        # a convenience for adapters that send a single options dictionary.
        if seed is not None:
            self.seed = int(seed)
        merged_parameters = dict(parameters or {})
        merged_parameters.update(kwargs)
        if terrain is not None:
            terrain_type = terrain
        elif "terrain" in merged_parameters:
            terrain_type = merged_parameters.pop("terrain")
        if road_type is not None:
            road_pattern = road_type
        elif "road_pattern" in merged_parameters:
            road_pattern = merged_parameters.get("road_pattern", road_pattern)
        if difficulty is not None:
            merged_parameters["difficulty"] = difficulty
        if existing_world is None:
            existing_world = world
        requested_regeneration = regenerate or component
        if requested_regeneration and existing_world is not None:
            # This is the compatibility path for WorldAdapter.  It preserves
            # all unaffected collections while using the same dependency-aware
            # implementation as direct callers.
            existing_state = existing_world if isinstance(existing_world, WorldState) else WorldState.from_dict(existing_world)
            self.state = existing_state
            self.seed = existing_state.seed
            regen_params: Dict[str, Any] = dict(merged_parameters)
            if terrain_type:
                regen_params.setdefault("terrain_type", terrain_type)
            if road_pattern:
                regen_params.setdefault("road_pattern", road_pattern)
            return self.regenerate(str(requested_regeneration), existing_state, **regen_params)
        terrain_type = str(terrain_type).lower().replace(" ", "_")
        if terrain_type not in {"flat", "island", "coast", "river", "hills"}:
            raise ValueError("terrain_type must be flat, island, coast, river or hills")
        # A normal full generation starts from a clean state.  The desktop
        # editor can opt into ``preserve_locks`` when the user changes the
        # root seed while keeping selected layers stable.  Keep a reference to
        # the prior state before constructing the fresh state; locked layers
        # are copied explicitly below and unlocked layers are regenerated from
        # the new root seed.
        previous_state = self.state if preserve_locks and self.state is not None else None
        locked_stages: set[str] = set()
        if previous_state is not None:
            locked_stages = {
                stage
                for stage, locked in previous_state.locks.items()
                if locked and stage in {"terrain", "road", "district", "building"}
            }
        self._undo.clear()
        self._redo.clear()
        state = self._new_state(terrain_type)
        state.metadata["city_name"] = city_name
        if previous_state is not None:
            # Preserve provenance and explicit lock choices in the new
            # document.  The generated collections are still rebuilt below;
            # only requested locked stages are copied verbatim.
            state.locks = deepcopy(previous_state.locks)
            state.generator_history = deepcopy(previous_state.generator_history)
            state.stage_revisions = deepcopy(previous_state.stage_revisions)
            state.metadata.update(deepcopy(previous_state.metadata))
            state.metadata["city_name"] = city_name
        self._last_params = {"terrain_type": terrain_type, "road_pattern": road_pattern, "include_harbor": include_harbor, "preserve_locks": bool(preserve_locks), "terrain_params": dict(terrain_params or {}), "road_params": dict(road_params or {}), **merged_parameters}
        if "terrain" in locked_stages:
            state.terrain = deepcopy(previous_state.terrain)  # type: ignore[union-attr]
            state.terrain_type = previous_state.terrain_type  # type: ignore[union-attr]
            state.stage_seeds["terrain"] = int(previous_state.stage_seeds.get("terrain", stable_seed(previous_state.seed, "terrain", self.generator_version)))  # type: ignore[union-attr]
            self._record(state, "terrain", {"preserved": True}, len(state.terrain.get("heights", [])) ** 2)
        else:
            self.generate_terrain(state, params=terrain_params)
        self.generate_water(state)
        if "road" in locked_stages:
            state.roads = deepcopy(previous_state.roads)  # type: ignore[union-attr]
            state.stage_seeds["roads"] = int(previous_state.stage_seeds.get("roads", stable_seed(previous_state.seed, "roads", self.generator_version)))  # type: ignore[union-attr]
            state.metadata["road_pattern"] = previous_state.metadata.get("road_pattern", road_pattern)  # type: ignore[union-attr]
            self._record(state, "roads", {"preserved": True, "pattern": state.metadata["road_pattern"]}, len(state.roads))
        else:
            self.generate_roads(state, pattern=road_pattern, params=road_params)
        if "district" in locked_stages:
            state.districts = deepcopy(previous_state.districts)  # type: ignore[union-attr]
            state.stage_seeds["districts"] = int(previous_state.stage_seeds.get("districts", stable_seed(previous_state.seed, "districts", self.generator_version)))  # type: ignore[union-attr]
            self._record(state, "districts", {"preserved": True}, len(state.districts))
        else:
            self.generate_districts(state)
        self.generate_blocks(state)
        self.generate_lots(state)
        if "building" in locked_stages:
            state.buildings = deepcopy(previous_state.buildings)  # type: ignore[union-attr]
            state.stage_seeds["buildings"] = int(previous_state.stage_seeds.get("buildings", stable_seed(previous_state.seed, "buildings", self.generator_version)))  # type: ignore[union-attr]
            self._record(state, "buildings", {"preserved": True}, len(state.buildings))
        else:
            self.generate_buildings(state)
        if include_harbor and terrain_type in ("coast", "island"):
            self.generate_harbor(state)
        self.generate_pois(state)
        self.generate_population(state)
        state.rebuild_spatial_index()
        self.state = state
        return state

    # ---- individual pipeline stages ---------------------------------------------

    def generate_terrain(self, state: Optional[WorldState] = None, *, terrain_type: Optional[str] = None, params: Optional[Mapping[str, Any]] = None) -> WorldState:
        state = state or self.state
        if state is None:
            state = self._new_state(terrain_type or "coast")
        if terrain_type:
            state.terrain_type = terrain_type.lower().replace(" ", "_")
        mode = state.terrain_type
        params = dict(params or {})
        seed = self._stage_seed(state, "terrain")
        state.stage_seeds["terrain"] = seed
        cache_key = self._cache_key(state, "terrain", mode, params, seed)
        cached = self.cache.get(*cache_key)
        if isinstance(cached, Mapping):
            state.terrain = cached
            # Cache hits are an implementation detail; keep serialized
            # history identical to a cold generation for reproducibility.
            self._record(state, "terrain", {"type": mode, **params}, len(cached.get("heights", [])) ** 2)
            state.invalidate_index()
            return state
        n = int(params.get("grid_size", 48))
        n = max(12, min(n, 128))
        rng = random.Random(seed)
        coarse_n = max(4, n // 6)
        coarse = [[rng.random() for _ in range(coarse_n + 1)] for _ in range(coarse_n + 1)]

        def noise(ix: int, iy: int) -> float:
            ix = max(0, min(coarse_n, ix)); iy = max(0, min(coarse_n, iy))
            return coarse[iy][ix]

        heights: List[List[float]] = []
        for gy in range(n):
            row: List[float] = []
            ny = gy / (n - 1)
            for gx in range(n):
                nx = gx / (n - 1)
                fx, fy = nx * coarse_n, ny * coarse_n
                ix, iy = int(fx), int(fy)
                tx, ty = fx - ix, fy - iy
                a = noise(ix, iy) * (1 - tx) + noise(ix + 1, iy) * tx
                b = noise(ix, iy + 1) * (1 - tx) + noise(ix + 1, iy + 1) * tx
                value = a * (1 - ty) + b * ty
                fine = 0.5 + 0.5 * math.sin(nx * 17.0 + ny * 9.0 + seed % 31)
                value = 0.72 * value + 0.28 * fine
                if mode == "flat":
                    value = 0.42 + 0.05 * (value - 0.5)
                elif mode == "island":
                    radial = math.sqrt((nx - 0.5) ** 2 + (ny - 0.52) ** 2) / 0.707
                    value = value * 0.75 + (1.0 - _clamp(radial, 0, 1)) * 0.45 - 0.16
                elif mode == "coast":
                    # Southern edge is water; land rises inland.
                    value = value * 0.55 + ny * 0.52 + 0.06
                elif mode == "river":
                    center = 0.52 + 0.12 * math.sin(nx * math.pi * 2.0)
                    valley = math.exp(-((ny - center) ** 2) / 0.008)
                    value = value * 0.74 + 0.26 - valley * 0.42
                elif mode == "hills":
                    value = 0.25 + value * 0.75 + 0.1 * math.sin(nx * 4 * math.pi) * math.sin(ny * 3 * math.pi)
                row.append(_clamp(value, 0.0, 1.0))
            heights.append(row)
        threshold = float(params.get("water_threshold", 0.32 if mode in ("island", "coast") else 0.18))
        state.terrain = {
            "type": mode,
            "width": state.width,
            "height": state.height,
            "grid_size": n,
            "heights": heights,
            "min": min(min(row) for row in heights),
            "max": max(max(row) for row in heights),
            "water_threshold": threshold,
            "seed": seed,
            "parameters": params,
        }
        self.cache.put(state.terrain, *cache_key)
        self._record(state, "terrain", {"type": mode, **params}, n * n)
        state.invalidate_index()
        return state

    def generate_water(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None:
            raise RuntimeError("Generate terrain first")
        seed = self._stage_seed(state, "water")
        state.stage_seeds["water"] = seed
        state.water.clear()
        mode = state.terrain_type
        if mode in ("coast", "island"):
            # A southern sea gives the harbor generator a stable coastline.
            sea = Feature("water-sea", "Sea", _feature_geometry("Polygon", [_closed(rectangle(0, -state.height * 0.22, state.width, state.height * 0.24))]), {"kind": "sea", "depth": "shallow_to_deep", "coast_edge": "south"}, stage="water", seed=seed)
            state.water[sea.id] = sea
        if mode == "river":
            pts = []
            for i in range(17):
                y = -20 + (state.height + 40) * i / 16
                x = state.width * (0.5 + 0.16 * math.sin(i / 16 * math.pi * 2.0))
                pts.append(_round_point((x, y)))
            river = Feature("water-river-main", "River", _feature_geometry("LineString", pts), {"kind": "river", "width": 48.0, "navigable": False}, stage="water", seed=seed)
            state.water[river.id] = river
        # A small deterministic park lake is useful for all non-coast modes.
        if mode in ("hills", "flat"):
            rng = random.Random(seed)
            cx, cy = state.width * (0.58 + rng.random() * 0.1), state.height * (0.55 + rng.random() * 0.1)
            rx, ry = state.width * 0.06, state.height * 0.04
            ring = [(_round_point((cx + math.cos(i / 20 * math.tau) * rx, cy + math.sin(i / 20 * math.tau) * ry))) for i in range(21)]
            ring[-1] = ring[0]
            lake = Feature("water-lake-01", "Lake", _feature_geometry("Polygon", [ring]), {"kind": "lake", "depth": 8.0}, stage="water", seed=seed)
            state.water[lake.id] = lake
        self._record(state, "water", {}, len(state.water))
        state.invalidate_index()
        return state

    def generate_roads(self, state: Optional[WorldState] = None, *, pattern: str = "hybrid", params: Optional[Mapping[str, Any]] = None) -> WorldState:
        state = state or self.state
        if state is None:
            raise RuntimeError("Generate a world first")
        params = dict(params or {})
        pattern = pattern.lower()
        if pattern not in ("grid", "organic", "radial", "hybrid"):
            raise ValueError("road pattern must be grid, organic, radial or hybrid")
        seed = self._stage_seed(state, "roads")
        state.stage_seeds["roads"] = seed
        cache_key = self._cache_key(state, "roads", pattern, params, seed)
        cached = self.cache.get(*cache_key)
        if isinstance(cached, Mapping):
            state.roads = cached
            state.metadata["road_pattern"] = pattern
            self._record(state, "roads", {"pattern": pattern, **params}, len(state.roads))
            state.invalidate_index()
            return state
        state.roads.clear()
        rng = random.Random(seed)
        w, h = state.width, state.height

        def add(name: str, cls: str, pts: Sequence[Point], width: float, *, parent: Optional[str] = None) -> None:
            if len(pts) < 2:
                return
            # Keep every editable road inside the world extent.  In
            # particular, radial spokes are initially constructed with a
            # generous ray length so they can reach the boundary; clipping
            # here prevents their endpoints from leaking outside the map and
            # keeps spatial queries/exported geometry well-defined.
            clipped_pts = [
                (_clamp(float(point[0]), 0.0, w), _clamp(float(point[1]), 0.0, h))
                for point in pts
            ]
            if len(clipped_pts) < 2 or clipped_pts[0] == clipped_pts[-1]:
                return
            rid = f"road-{name}-{len(state.roads)+1:03d}"
            feature = Feature(
                rid,
                "Road",
                _feature_geometry("LineString", [_round_point(p) for p in clipped_pts]),
                {
                    "road_class": cls,
                    "road_type": {"main": "Main Road", "secondary": "Secondary Road", "street": "Street", "alley": "Alley"}.get(cls, cls.title()),
                    "width": width,
                    "pattern": pattern,
                    "surface": "asphalt",
                    "speed_limit": {"main": 60, "secondary": 40, "street": 30, "alley": 15}.get(cls, 30),
                },
                parent_id=parent,
                stage="roads",
                seed=seed,
            )
            state.roads[rid] = feature

        # Main spine(s) always establish connectivity.
        add("main-west", "main", [(0, h * 0.50), (w, h * 0.50)], 18)
        add("main-east", "main", [(w * 0.12, 0), (w * 0.84, h)], 16)
        if pattern in ("grid", "hybrid"):
            for i, frac in enumerate((0.22, 0.38, 0.62, 0.78)):
                y = h * frac + rng.uniform(-12, 12)
                add(f"secondary-h-{i}", "secondary", [(0, y), (w, y + rng.uniform(-8, 8))], 10)
            for i, frac in enumerate((0.22, 0.42, 0.62, 0.82)):
                x = w * frac + rng.uniform(-12, 12)
                add(f"secondary-v-{i}", "secondary", [(x, 0), (x + rng.uniform(-8, 8), h)], 10)
            for i in range(5):
                y = h * (0.12 + i * 0.17) + rng.uniform(-8, 8)
                add(f"street-h-{i}", "street", [(0, y), (w, y + rng.uniform(-15, 15))], 6)
            for i in range(6):
                x = w * (0.10 + i * 0.15) + rng.uniform(-8, 8)
                add(f"street-v-{i}", "street", [(x, 0), (x + rng.uniform(-15, 15), h)], 6)
        if pattern in ("radial", "hybrid"):
            center = (w * 0.53, h * 0.56)
            for i in range(8):
                angle = i * math.tau / 8.0 + rng.uniform(-0.08, 0.08)
                raw_end = (center[0] + math.cos(angle) * w, center[1] + math.sin(angle) * h)
                end = _clip_ray_to_bounds(center, raw_end, w, h)
                add(f"radial-{i}", "secondary" if i % 2 else "main", [center, end], 9 if i % 2 else 13)
            for radius in (w * 0.16, w * 0.28):
                pts = [(center[0] + math.cos(i / 32 * math.tau) * radius, center[1] + math.sin(i / 32 * math.tau) * radius * h / w) for i in range(33)]
                add(f"ring-{int(radius)}", "street", pts, 6)
        if pattern == "organic":
            for i in range(9):
                y = h * (0.1 + i * 0.1)
                pts = []
                for j in range(13):
                    x = w * j / 12
                    yy = y + math.sin(j * 0.9 + i) * h * 0.035 + rng.uniform(-10, 10)
                    pts.append((x, yy))
                add(f"organic-{i}", "secondary" if i < 3 else "street", pts, 9 if i < 3 else 5)
        # Fine-grained alleys make lot access explicit without overwhelming the UI.
        for i in range(8):
            x = w * (0.06 + i * 0.12)
            add(f"alley-{i}", "alley", [(x, h * 0.18), (x + rng.uniform(-20, 20), h * 0.82)], 3.5)
        state.metadata["road_pattern"] = pattern
        self.cache.put(state.roads, *cache_key)
        self._record(state, "roads", {"pattern": pattern, **params}, len(state.roads))
        state.invalidate_index()
        return state

    def generate_districts(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None:
            raise RuntimeError("Generate roads first")
        seed = self._stage_seed(state, "districts")
        state.stage_seeds["districts"] = seed
        state.districts.clear()
        w, h = state.width, state.height
        # Hand-authored macro layout is itself procedural: jitter is seeded and
        # the rules are explicit, making the result editable and repeatable.
        rng = random.Random(seed)
        xcuts = [0.0, 0.26, 0.52, 0.76, 1.0]
        ycuts = [0.0, 0.30, 0.56, 0.78, 1.0]
        layout = [
            ["industrial", "harbor" if state.terrain_type in ("coast", "island") else "park", "commercial", "residential"],
            ["industrial", "business", "old_town", "residential"],
            ["park", "station_area", "commercial", "residential"],
            ["park", "residential", "residential", "business"],
        ]
        for row in range(4):
            for col in range(4):
                x0, x1 = xcuts[col] * w, xcuts[col + 1] * w
                y0, y1 = ycuts[row] * h, ycuts[row + 1] * h
                jitter_x = rng.uniform(-8, 8) if col not in (0, 3) else 0
                jitter_y = rng.uniform(-8, 8) if row not in (0, 3) else 0
                poly = rectangle(x0 + jitter_x, y0 + jitter_y, x1 + jitter_x, y1 + jitter_y)
                dtype = layout[row][col]
                did = f"district-{dtype}-{row+1}{col+1}"
                density = {"industrial": 0.40, "park": 0.08, "residential": 0.55, "commercial": 0.82, "business": 0.95, "old_town": 0.67, "harbor": 0.50, "station_area": 0.88}[dtype]
                green_ratio = {"park": 0.78, "residential": 0.22, "old_town": 0.08, "industrial": 0.06, "business": 0.04, "commercial": 0.10, "harbor": 0.05, "station_area": 0.12}[dtype]
                feature = Feature(did, "District", _feature_geometry("Polygon", [_closed(poly)]), {"district_type": dtype, "density": density, "green_ratio": green_ratio, "far": round(0.5 + density * 3.2, 2), "rule_set": f"{dtype}_v1", "area": polygon_area(poly)}, stage="districts", seed=seed)
                state.districts[did] = feature
        self._record(state, "districts", {}, len(state.districts))
        state.invalidate_index()
        return state

    def generate_blocks(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None or not state.districts:
            raise RuntimeError("Generate districts first")
        seed = self._stage_seed(state, "blocks")
        state.stage_seeds["blocks"] = seed
        state.blocks.clear()
        rng = random.Random(seed)
        for district in state.districts.values():
            poly = district.geometry["coordinates"][0]
            x0, y0, x1, y1 = bbox_of_geometry(district.geometry)
            dtype = district.properties.get("district_type", "residential")
            cols = 2 if dtype in ("park", "industrial", "harbor") else 3
            rows = 2 if dtype == "park" else 3
            gap = min(5.0, max(1.5, (x1 - x0) / 100))
            for r in range(rows):
                for c in range(cols):
                    ax = x0 + (x1 - x0) * c / cols + gap
                    bx = x0 + (x1 - x0) * (c + 1) / cols - gap
                    ay = y0 + (y1 - y0) * r / rows + gap
                    by = y0 + (y1 - y0) * (r + 1) / rows - gap
                    if bx <= ax or by <= ay:
                        continue
                    # Tiny irregularity keeps organic-looking boundaries while
                    # preserving rectangular topology for the parcel splitter.
                    skew = rng.uniform(-3.0, 3.0)
                    # Jitter is intentionally subtle, but clipping keeps the
                    # resulting editable block within the world at edge
                    # districts (where a negative skew could otherwise push a
                    # vertex below y=0 or above the northern boundary).
                    bpoly = [
                        (
                            _clamp(float(px), 0.0, state.width),
                            _clamp(float(py), 0.0, state.height),
                        )
                        for px, py in ((ax, ay), (bx, ay + skew), (bx, by), (ax, by - skew), (ax, ay))
                    ]
                    bid = f"block-{district.id}-{r+1}{c+1}"
                    state.blocks[bid] = Feature(bid, "Block", _feature_geometry("Polygon", [_closed(bpoly)]), {"district": dtype, "area": polygon_area(bpoly), "road_access": True, "index": [r, c]}, parent_id=district.id, stage="blocks", seed=seed)
        self._record(state, "blocks", {}, len(state.blocks))
        state.invalidate_index()
        return state

    def generate_lots(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None or not state.blocks:
            raise RuntimeError("Generate blocks first")
        seed = self._stage_seed(state, "lots")
        state.stage_seeds["lots"] = seed
        state.lots.clear()
        rng = random.Random(seed)
        for block in state.blocks.values():
            x0, y0, x1, y1 = bbox_of_geometry(block.geometry)
            width, depth = x1 - x0, y1 - y0
            count = 3 if width >= depth else 2
            district_type = block.properties.get("district", "residential")
            for i in range(count):
                if width >= depth:
                    ax = x0 + width * i / count + 1.5
                    bx = x0 + width * (i + 1) / count - 1.5
                    ay, by = y0 + 1.5, y1 - 1.5
                    orientation = "east_west"
                else:
                    ax, bx = x0 + 1.5, x1 - 1.5
                    ay = y0 + (y1 - y0) * i / count + 1.5
                    by = y0 + (y1 - y0) * (i + 1) / count - 1.5
                    orientation = "north_south"
                if bx <= ax or by <= ay:
                    continue
                lot_poly = rectangle(ax, ay, bx, by)
                lid = f"lot-{block.id}-{i+1}"
                state.lots[lid] = Feature(lid, "Lot", _feature_geometry("Polygon", [_closed(lot_poly)]), {"size": round(polygon_area(lot_poly), 2), "orientation": orientation, "road_access": True, "zone": district_type, "frontage": round((bx - ax) if orientation == "east_west" else (by - ay), 2), "vacant": rng.random() < (0.14 if district_type == "park" else 0.04)}, parent_id=block.id, stage="lots", seed=seed)
        self._record(state, "lots", {}, len(state.lots))
        state.invalidate_index()
        return state

    def generate_buildings(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None or not state.lots:
            raise RuntimeError("Generate lots first")
        seed = self._stage_seed(state, "buildings")
        state.stage_seeds["buildings"] = seed
        state.buildings.clear()
        rng = random.Random(seed)
        for lot in state.lots.values():
            if lot.properties.get("vacant"):
                continue
            x0, y0, x1, y1 = bbox_of_geometry(lot.geometry)
            zone = lot.properties.get("zone", "residential")
            chance = {"park": 0.18, "industrial": 0.88, "harbor": 0.80, "business": 0.96, "commercial": 0.90, "old_town": 0.84, "station_area": 0.92, "residential": 0.76}.get(zone, 0.75)
            if rng.random() > chance:
                continue
            inset = min((x1 - x0), (y1 - y0)) * (0.10 if zone not in ("park", "industrial") else 0.06)
            bx0, by0, bx1, by1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
            footprint = rectangle(bx0, by0, bx1, by1)
            floor_range = {"business": (6, 18), "station_area": (5, 14), "commercial": (3, 9), "old_town": (2, 5), "industrial": (1, 4), "harbor": (1, 3), "park": (1, 2), "residential": (2, 7)}.get(zone, (1, 4))
            floors = rng.randint(*floor_range)
            height = round(floors * 3.2, 2)
            roof = "flat" if floors >= 4 or zone in ("business", "industrial", "harbor") else rng.choice(("gable", "hip", "flat"))
            bid = f"building-{lot.id}"
            state.buildings[bid] = Feature(bid, "Building", _feature_geometry("Polygon", [_closed(footprint)]), {"footprint": round(polygon_area(footprint), 2), "floors": floors, "height": height, "roof": roof, "setback": round(inset, 2), "use": zone, "material": "concrete" if zone in ("business", "commercial") else "mixed", "editable_volume": {"width": round(bx1 - bx0, 2), "depth": round(by1 - by0, 2), "height": height}}, parent_id=lot.id, stage="buildings", seed=seed)
        self._record(state, "buildings", {}, len(state.buildings))
        state.invalidate_index()
        return state

    def generate_harbor(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None:
            raise RuntimeError("Generate a world first")
        seed = self._stage_seed(state, "harbor")
        state.stage_seeds["harbor"] = seed
        state.harbor.clear()
        if state.terrain_type not in ("coast", "island"):
            return state
        rng = random.Random(seed)
        w, h = state.width, state.height
        # Keep the shoreline as an explicit editable feature.  The water
        # polygon describes the sea volume, while this line is the harbor's
        # actual land/sea interface that docks and future rail links can
        # reference.
        coastline = Feature(
            "harbor-coastline",
            "Coastline",
            _feature_geometry("LineString", [_round_point((0.0, h * 0.24)), _round_point((w, h * 0.24))]),
            {"edge": "south", "orientation": "east_west", "water_id": "water-sea"},
            stage="harbor",
            seed=seed,
        )
        state.harbor[coastline.id] = coastline
        # Docks run perpendicular to the southern coastline.  Their spacing,
        # orientation and length are explicit parameters instead of decorative
        # noise, so a user can later edit/export them as infrastructure.
        harbor_x = w * 0.19
        for i in range(4):
            x = harbor_x + i * w * 0.055 + rng.uniform(-5, 5)
            pts = [(x, h * 0.24), (x + rng.uniform(-3, 3), h * 0.035)]
            fid = f"harbor-dock-{i+1}"
            state.harbor[fid] = Feature(fid, "Dock", _feature_geometry("LineString", [_round_point(p) for p in pts]), {"orientation": "south", "berths": 2 + i % 3, "length": round(line_length(pts), 2), "access": "industrial_road"}, stage="harbor", seed=seed)
        # Warehouse pads north of the docks.
        for i in range(5):
            x0 = harbor_x - w * 0.025 + i * w * 0.058
            poly = rectangle(x0, h * 0.26, x0 + w * 0.045, h * 0.35)
            fid = f"harbor-warehouse-{i+1}"
            state.harbor[fid] = Feature(fid, "Warehouse", _feature_geometry("Polygon", [_closed(poly)]), {"use": "warehouse", "loading_side": "south", "rail_spur_reserved": True}, stage="harbor", seed=seed)
        road_pts = [(harbor_x - w * 0.06, h * 0.37), (harbor_x + w * 0.29, h * 0.37)]
        state.harbor["harbor-industrial-road"] = Feature("harbor-industrial-road", "HarborRoad", _feature_geometry("LineString", road_pts), {"road_class": "industrial", "width": 14, "connects": ["warehouse", "dock", "station"]}, stage="harbor", seed=seed)
        station_poly = rectangle(harbor_x + w * 0.20, h * 0.39, harbor_x + w * 0.27, h * 0.46)
        state.harbor["harbor-station-reserve"] = Feature("harbor-station-reserve", "StationReserve", _feature_geometry("Polygon", [_closed(station_poly)]), {"rail_connection": "northwest", "platforms": 2, "future_use": "station"}, stage="harbor", seed=seed)
        commercial_poly = rectangle(harbor_x + w * 0.30, h * 0.26, harbor_x + w * 0.48, h * 0.39)
        commercial = Feature(
            "harbor-commercial-area",
            "CommercialArea",
            _feature_geometry("Polygon", [_closed(commercial_poly)]),
            {
                "district_type": "commercial",
                "position": "east_of_warehouses",
                "road_access": "harbor-industrial-road",
                "station_access": "harbor-station-reserve",
                "area": round(polygon_area(commercial_poly), 2),
            },
            parent_id="district-commercial-13" if "district-commercial-13" in state.districts else None,
            stage="harbor",
            seed=seed,
        )
        state.harbor[commercial.id] = commercial
        state.metadata["harbor"] = {
            "coastline": "harbor-coastline",
            "dock_orientation": "south",
            "dock_count": 4,
            "warehouse_count": 5,
            "station_reserved": True,
            "commercial_zone": commercial.id,
            "industrial_zone": "district-industrial-11",
            "station_connection": "harbor-station-reserve",
        }
        self._record(state, "harbor", {"enabled": True}, len(state.harbor))
        state.invalidate_index()
        return state

    def generate_pois(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None or not state.districts:
            raise RuntimeError("Generate districts first")
        seed = self._stage_seed(state, "poi")
        state.stage_seeds["poi"] = seed
        state.pois.clear()
        rng = random.Random(seed)
        by_type: Dict[str, Feature] = {}
        for district in state.districts.values():
            dtype = district.properties.get("district_type")
            center = geometry_centroid(district.geometry)
            poi_type: Optional[str] = None
            if dtype == "station_area": poi_type = "Station"
            elif dtype == "park": poi_type = "Park"
            elif dtype == "business": poi_type = "Government"
            elif dtype == "commercial": poi_type = "Mall"
            elif dtype == "old_town": poi_type = "Temple"
            elif dtype == "industrial": poi_type = "Warehouse"
            if poi_type:
                pid = f"poi-{poi_type.lower()}-{district.id.split('-')[-1]}"
                p = (center[0] + rng.uniform(-12, 12), center[1] + rng.uniform(-12, 12))
                feature = Feature(pid, "POI", _feature_geometry("Point", _round_point(p)), {"poi_type": poi_type, "district": dtype, "service_radius": {"Station": 240, "School": 180, "Hospital": 300, "Mall": 260, "Park": 160, "Government": 220, "Temple": 140, "Warehouse": 200, "Cafe": 90}.get(poi_type, 120)}, parent_id=district.id, stage="poi", seed=seed)
                state.pois[pid] = feature
                by_type[poi_type] = feature
        # Fill city services exactly once, choosing the best eligible districts.
        targets = (("School", "residential"), ("Hospital", "station_area"), ("Cafe", "commercial"))
        for poi_type, preferred in targets:
            if poi_type in by_type:
                continue
            candidates = [d for d in state.districts.values() if d.properties.get("district_type") == preferred]
            if not candidates:
                candidates = list(state.districts.values())
            district = candidates[0]
            center = geometry_centroid(district.geometry)
            pid = f"poi-{poi_type.lower()}-central"
            p = (center[0] + rng.uniform(-24, 24), center[1] + rng.uniform(-24, 24))
            state.pois[pid] = Feature(pid, "POI", _feature_geometry("Point", _round_point(p)), {"poi_type": poi_type, "district": district.properties.get("district_type"), "service_radius": 180}, parent_id=district.id, stage="poi", seed=seed)
        self._record(state, "poi", {}, len(state.pois))
        state.invalidate_index()
        return state

    def generate_population(self, state: Optional[WorldState] = None) -> WorldState:
        state = state or self.state
        if state is None:
            raise RuntimeError("Generate a world first")
        seed = self._stage_seed(state, "population")
        state.stage_seeds["population"] = seed
        population: Dict[str, Any] = {}
        total = 0
        total_jobs = 0
        for district in state.districts.values():
            dtype = district.properties.get("district_type", "residential")
            area = float(district.properties.get("area", polygon_area(district.geometry.get("coordinates", [[]])[0])))
            density = float(district.properties.get("density", 0.5))
            residents = int(area * density * (0.025 if dtype != "park" else 0.002))
            jobs = int(area * ({"business": 0.030, "commercial": 0.018, "industrial": 0.012, "station_area": 0.016}.get(dtype, 0.004)))
            households = max(1, int(residents / 2.4))
            population[district.id] = {"district_type": dtype, "residents": residents, "households": households, "jobs": jobs, "density_per_ha": round(residents / max(area / 10000, 0.01), 2), "metadata_seed": stable_seed(seed, district.id)}
            total += residents
            total_jobs += jobs
        state.population = {"total_residents": total, "total_jobs": total_jobs, "districts": population, "method": "rule_based_density_v1", "seed": seed}
        self._record(state, "population", {}, total)
        return state

    # ---- partial regeneration ----------------------------------------------------

    def regenerate(self, stage: str, state: Optional[WorldState] = None, **params: Any) -> WorldState:
        """Regenerate one stage and its dependents while preserving prior data.

        ``stage`` accepts ``terrain``, ``water``, ``roads``, ``districts``,
        ``buildings`` or ``poi`` (plus any pipeline stage).  An unlocked stage
        receives a new revision seed; a locked stage keeps its current seed.
        Regenerating a stage never changes the root seed or unrelated upstream
        stages.
        """

        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        aliases = {"road": "roads", "district": "districts", "building": "buildings", "pois": "poi"}
        stage = aliases.get(stage.lower(), stage.lower())
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}; expected one of {STAGES}")
        self._push_undo(target)
        lock_name = self._lock_name(stage)
        if not target.locks.get(lock_name, False):
            target.stage_revisions[stage] = int(target.stage_revisions.get(stage, 0)) + 1
        # Determine the earliest dependency to rebuild.  Water depends on
        # terrain, blocks/lots/buildings depend on districts, etc.
        order = list(STAGES)
        start = order.index(stage)
        if stage == "poi":
            start = order.index("poi")
        if stage == "buildings":
            start = order.index("buildings")
        if stage == "districts":
            start = order.index("districts")
        if stage == "roads":
            start = order.index("roads")
        if stage == "terrain":
            start = 0
        if stage == "water":
            start = 1
        for current in order[start:]:
            if current == "terrain": self.generate_terrain(target, terrain_type=params.get("terrain_type"), params=params.get("terrain_params"))
            elif current == "water": self.generate_water(target)
            elif current == "roads": self.generate_roads(target, pattern=params.get("road_pattern", target.metadata.get("road_pattern", "hybrid")), params=params.get("road_params"))
            elif current == "districts": self.generate_districts(target)
            elif current == "blocks": self.generate_blocks(target)
            elif current == "lots": self.generate_lots(target)
            elif current == "buildings": self.generate_buildings(target)
            elif current == "harbor":
                if target.terrain_type in ("coast", "island"):
                    self.generate_harbor(target)
                else:
                    target.harbor.clear()
            elif current == "poi": self.generate_pois(target)
            elif current == "population": self.generate_population(target)
        target.rebuild_spatial_index()
        self.state = target
        return target

    # ---- direct editing --------------------------------------------------------

    def update_feature(
        self,
        feature_id: str,
        *,
        properties: Optional[Mapping[str, Any]] = None,
        geometry: Optional[Mapping[str, Any]] = None,
        state: Optional[WorldState] = None,
    ) -> WorldState:
        """Apply an inspectable edit to one generated feature.

        The generator never treats an edit as a hidden raster change: the
        modified properties/geometry remain in the serialised world document,
        participate in the spatial index, and can be undone/redone.  This is
        the small editing API used by the desktop Inspector and is also useful
        for future Godot/Unreal bridge tools.
        """

        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        feature = target.get_feature(feature_id)
        if feature is None:
            raise KeyError(f"Unknown feature: {feature_id}")
        self._push_undo(target)
        if properties:
            feature.properties.update(deepcopy(dict(properties)))
            # Floors are a first-class building parameter; keep the simple
            # procedural volume coherent when a user changes them in the UI.
            if feature.type == "Building" and "floors" in properties and "height" not in properties:
                try:
                    floors = max(1, int(properties["floors"]))
                    feature.properties["floors"] = floors
                    feature.properties["height"] = round(floors * 3.2, 2)
                    volume = feature.properties.get("editable_volume")
                    if isinstance(volume, MutableMapping):
                        volume["height"] = feature.properties["height"]
                except (TypeError, ValueError):
                    pass
        if geometry is not None:
            if "type" not in geometry or "coordinates" not in geometry:
                raise ValueError("geometry must be a GeoJSON-like mapping with type and coordinates")
            feature.geometry = deepcopy(dict(geometry))
        target.invalidate_index()
        target.rebuild_spatial_index()
        self._record(target, "edit", {"feature_id": feature_id, "properties": sorted((properties or {}).keys()), "geometry_changed": geometry is not None}, 1)
        self.state = target
        return target

    def move_feature(
        self,
        feature_id: str,
        dx: float,
        dy: float,
        *,
        state: Optional[WorldState] = None,
    ) -> WorldState:
        """Translate a feature's geometry, preserving its type and hierarchy."""

        target = state or self.state
        if target is None:
            raise RuntimeError("No world has been generated yet")
        feature = target.get_feature(feature_id)
        if feature is None:
            raise KeyError(f"Unknown feature: {feature_id}")

        def translate(value: Any) -> Any:
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                    # Preserve ordinary floating-point addition.  An editor
                    # move should be reversible and should not silently snap
                    # user coordinates to a 1 mm/1-unit grid.
                    shifted = [float(value[0]) + float(dx), float(value[1]) + float(dy)]
                    shifted.extend(value[2:])
                    return tuple(shifted) if isinstance(value, tuple) else shifted
                translated = [translate(v) for v in value]
                return tuple(translated) if isinstance(value, tuple) else translated
            return value

        geometry = deepcopy(feature.geometry)
        geometry["coordinates"] = translate(geometry.get("coordinates", []))
        return self.update_feature(feature_id, geometry=geometry, state=target)

    # ---- inspection and metrics --------------------------------------------------

    def inspect(self, feature_id: str, state: Optional[WorldState] = None) -> Optional[Dict[str, Any]]:
        target = state or self.state
        if target is None:
            return None
        feature = target.get_feature(feature_id)
        if feature is None:
            return None
        parent = target.get_feature(feature.parent_id) if feature.parent_id else None
        return {
            "id": feature.id,
            "type": feature.type,
            "stage": feature.stage,
            "parent": parent.id if parent else feature.parent_id,
            "parent_type": parent.type if parent else None,
            "seed": feature.seed,
            "geometry": deepcopy(feature.geometry),
            "parameters": deepcopy(feature.properties),
            "world_seed": target.seed,
        }

    def statistics(self, state: Optional[WorldState] = None) -> Dict[str, Any]:
        target = state or self.state
        if target is None:
            return {"road_length": 0.0, "building_count": 0, "density": 0.0, "district_area": 0.0, "green_area": 0.0, "average_building_height": 0.0}
        road_length = sum(line_length(f.geometry.get("coordinates", [])) for f in target.roads.values() if f.geometry.get("type") == "LineString")
        district_area = sum(float(f.properties.get("area", polygon_area(f.geometry.get("coordinates", [[]])[0]))) for f in target.districts.values())
        green_area = sum(float(f.properties.get("area", polygon_area(f.geometry.get("coordinates", [[]])[0]))) * float(f.properties.get("green_ratio", 0.0)) for f in target.districts.values())
        heights = [float(f.properties.get("height", 0.0)) for f in target.buildings.values()]
        return {
            "road_length": round(road_length, 2),
            "building_count": len(target.buildings),
            "lot_count": len(target.lots),
            "district_count": len(target.districts),
            "poi_count": len(target.pois),
            "density": round(len(target.buildings) / max(district_area / 10000.0, 0.01), 3),
            "district_area": round(district_area, 2),
            "green_area": round(green_area, 2),
            "average_building_height": round(sum(heights) / len(heights), 2) if heights else 0.0,
            "population": int(target.population.get("total_residents", 0)),
            "jobs": int(target.population.get("total_jobs", 0)),
        }

    def query(self, bbox: Tuple[float, float, float, float], state: Optional[WorldState] = None) -> List[Feature]:
        target = state or self.state
        if target is None:
            return []
        index = target._spatial_index or target.rebuild_spatial_index()
        return index.query_bbox(bbox)

    def history(self, state: Optional[WorldState] = None) -> List[Dict[str, Any]]:
        target = state or self.state
        return deepcopy(target.generator_history if target else [])


def generate_demo_city(seed: int = 20261004) -> WorldState:
    """Generate the fixed-seed coastal demo used by the application smoke test."""

    return WorldGenerator(seed=seed, width=1200, height=900).generate(
        terrain_type="coast",
        road_pattern="hybrid",
        include_harbor=True,
        city_name="Coastal City Demo",
    )


__all__ = [
    "Feature",
    "GeometryCache",
    "SpatialIndex",
    "WorldGenerator",
    "WorldState",
    "generate_demo_city",
    "stable_seed",
    "polygon_area",
    "polygon_centroid",
    "line_length",
]
