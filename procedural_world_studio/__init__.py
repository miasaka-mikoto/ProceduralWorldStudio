"""Procedural World Studio core package.

The package deliberately keeps the generation engine independent from any
GUI toolkit.  A desktop front end can consume :class:`WorldState` as a
serialisable model, call :class:`WorldGenerator.regenerate`, and render the
GeoJSON-like geometries exposed by the model.
"""

from .core import (
    Feature,
    GeometryCache,
    SpatialIndex,
    WorldGenerator,
    WorldState,
    generate_demo_city,
)
from .export_formats import (
    export_all,
    export_geojson,
    export_gltf,
    export_json,
    export_obj,
    export_png,
    world_to_geojson,
    world_to_gltf,
    world_to_obj,
)

__all__ = [
    "Feature",
    "GeometryCache",
    "SpatialIndex",
    "WorldGenerator",
    "WorldState",
    "generate_demo_city",
    "export_all",
    "export_json",
    "export_geojson",
    "export_png",
    "export_obj",
    "export_gltf",
    "world_to_geojson",
    "world_to_obj",
    "world_to_gltf",
]

__version__ = "0.1.0"

__all__ += ["MainWindow", "WorldAdapter", "launch_editor"]


def __getattr__(name: str):
    """Lazily expose the optional desktop shell.

    Importing ``ui_editor`` here makes ``python -m
    procedural_world_studio.ui_editor`` load the module twice and triggers a
    runpy warning.  A lazy attribute keeps the convenient package aliases
    while making CLI/packaged startup clean and leaving core-only use free of
    GUI imports.
    """

    if name in {"MainWindow", "WorldAdapter", "launch_editor"}:
        from .ui_editor import MainWindow, WorldAdapter, main

        values = {
            "MainWindow": MainWindow,
            "WorldAdapter": WorldAdapter,
            "launch_editor": main,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
