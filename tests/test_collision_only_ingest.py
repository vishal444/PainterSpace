import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "collision"))

from ingest_asset import IngestError, collision_only_entry


@pytest.mark.parametrize("asset_path", ["assets/house.usdc", "assets/car.usdc"])
def test_collision_only_preserves_authored_metadata(asset_path):
    entry = {"id": asset_path, "path": asset_path, "file_hash": "source_hash",
             "anchors": {"entry": {"pos": [1, 0, 2]}}, "tags": ["building"]}
    shapes = [{"id": "body_0", "type": "box", "center": [0, 1, 0], "size": [2, 2, 2]}]
    generated = {"path": asset_path, "file_hash": "source_hash",
                 "anchors": {"generated": {"pos": [0, 0, 0]}},
                 "collision": {"frame": "asset", "shapes": shapes}}

    updated = collision_only_entry(entry, generated)

    assert updated["anchors"] == entry["anchors"]
    assert updated["tags"] == entry["tags"]
    assert updated["collision"]["shapes"] == shapes
    assert "collision" not in entry


@pytest.mark.parametrize("change", [
    {"path": "assets/normalized.usda"},
    {"file_hash": "different"},
    {"collision": {"shapes": []}},
])
def test_collision_only_rejects_mismatched_or_empty_geometry(change):
    entry = {"path": "assets/original.usdc", "file_hash": "source_hash"}
    generated = {"path": entry["path"], "file_hash": entry["file_hash"],
                 "collision": {"shapes": [{"id": "box"}]}}
    generated.update(change)

    with pytest.raises(IngestError):
        collision_only_entry(entry, generated)


def test_all_non_character_manifest_assets_have_collision_shapes():
    import json
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    missing = [
        asset["id"]
        for asset in manifest.get("assets", [])
        if not asset.get("id", "").startswith("character.")
        and not (asset.get("collision") or {}).get("shapes")
    ]
    assert missing == []

