"""Asset ingestion derives support surfaces for placed props and vehicles."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "collision"))

from ingest_asset import measured_asset_surfaces


def _top(width, depth, height):
    x, z = width / 2, depth / 2
    points = [[-x, height, -z], [-x, height, z],
              [x, height, z], [x, height, -z]]
    return np.asarray([[points[0], points[1], points[2]],
                       [points[0], points[2], points[3]]], float)


def test_flat_prop_top_gets_generic_placeable_reference():
    surfaces = measured_asset_surfaces(_top(2, 1.5, 0.8), [], 0, tags=["furniture"])
    assert surfaces
    assert all(name.startswith("surface_") for name in surfaces)
    assert all(surface["height_m"] == 0.8 for surface in surfaces.values())


def test_vehicle_roof_alias_follows_measured_geometry():
    low = measured_asset_surfaces(_top(2, 3, 1.4), [], 0, tags=["vehicle"])
    high = measured_asset_surfaces(_top(2.5, 5, 2.8), [], 0, tags=["truck"])
    assert low["roof"]["height_m"] == 1.4
    assert high["roof"]["height_m"] == 2.8


def test_narrow_or_blocked_asset_top_is_not_placeable():
    assert measured_asset_surfaces(_top(0.3, 0.3, 1), [], 0) == {}
    overhead = [{"type": "box", "center": [0, 1.6, 0], "size": [2, 0.5, 2]}]
    surfaces = measured_asset_surfaces(_top(2, 2, 0.8), overhead, 0)
    assert all(surface["height_m"] != 0.8 for surface in surfaces.values())
    assert any(surface["height_m"] == 1.85 for surface in surfaces.values())
