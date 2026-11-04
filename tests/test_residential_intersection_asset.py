"""The residential junction must leave both through roads open to vehicles."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_residential_intersection import outside_road_segments


@pytest.mark.parametrize("block_width,road_half_width", [(180, 16.8), (80, 10), (240, 25)])
def test_corner_spans_leave_the_crossing_open(block_width, road_half_width):
    spans = outside_road_segments(block_width, road_half_width)
    assert len(spans) == 2
    for center, length in spans:
        assert length > 0
        assert abs(center) - length / 2 == pytest.approx(road_half_width)
        assert abs(center) + length / 2 == pytest.approx(block_width / 2)


def test_intersection_colliders_leave_both_center_lanes_open():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    asset = next(item for item in manifest["assets"]
                 if item["id"] == "environment.residential_intersection")
    asset_path = ROOT / asset["path"]
    assert asset_path.is_file()
    assert hashlib.sha256(asset_path.read_bytes()).hexdigest()[:16] == asset["file_hash"]

    # Each center lane is two metres wide and runs through the entire asset.
    # A sidewalk, curb, hydrant, planter or oversized compound box crossing
    # either lane would prevent a vehicle from passing the junction.
    shapes = asset["collision"]["shapes"]
    assert shapes
    for shape in shapes:
        x, _, z = shape["center"]
        width, _, depth = shape["size"]
        assert abs(x) - width / 2 >= 2
        assert abs(z) - depth / 2 >= 2
