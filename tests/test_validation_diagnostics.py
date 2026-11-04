import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from compile_shot import _scene_diagnostics
from compiler import ShotCompiler
from plan_model import ShotPlan
from semantic_references import ReferenceResolver
from support_surfaces import derive_standing_spots, contains, ACTOR_SUPPORT_MARGIN_M

sys.path.insert(0, str(ROOT / "tools"))
from process_agent_queue import _anchor_support_references


def test_scene_diagnostics_reports_missing_sublayer_and_object_mismatch(tmp_path):
    scene = tmp_path / "scene" / "scene.usda"
    scene.parent.mkdir()
    scene.write_text('#usda 1.0\n(subLayers = [@../missing.usda@])\ndef Xform "World" {}\n')
    observed = {"objects": [{"name": "Club"}], "support_surfaces": [{"prim_path": "/Floor"}]}
    task = {"current_shot_state": {"objects": [{"id": "Club"}, {"id": "Car"}]}}

    diagnostics = _scene_diagnostics(scene, observed, task)

    assert diagnostics["observed_scene_objects"] == ["Club"]
    assert diagnostics["missing_expected_objects"] == ["Car"]
    assert diagnostics["support_surface_count"] == 1
    assert diagnostics["missing_sublayers"] == [{
        "source_layer": str(scene),
        "authored_path": "../missing.usda",
        "resolved_path": str(tmp_path / "missing.usda"),
    }]


def test_compiler_rejects_missing_nested_scene_sublayer_before_writing_shot(tmp_path):
    scene = tmp_path / "scene.usda"
    child = tmp_path / "child.usda"
    scene.write_text('#usda 1.0\n(subLayers = [@child.usda@])\ndef Xform "World" {}\n')
    child.write_text('#usda 1.0\n(subLayers = [@missing.usda@])\n')
    shot = tmp_path / "shot" / "shot.usda"

    success, report = ShotCompiler(project_root=ROOT).compile(
        ShotPlan(), shot, scene_usda_path=scene, validate=False)

    assert not success
    assert report["error"]["error_code"] == "SCENE_SUBLAYER_MISSING"
    assert report["error"]["details"]["missing_sublayers"] == [{
        "source_layer": str(child),
        "authored_path": "missing.usda",
        "resolved_path": str(tmp_path / "missing.usda"),
    }]
    assert not shot.exists()


def test_anchor_support_context_distinguishes_covered_and_uncovered_references():
    manifest = {"assets": [{"id": "building", "anchors": {
        "inside": {"pos": [0, 0, 0]}, "outside": {"pos": [5, 0, 0]},
    }}]}
    scene = {"objects": [{"name": "Building", "asset": "building", "position": [0, 0, 0],
                          "heading_deg": 0}],
             "support_surfaces": [{"height_m": 0, "polygon_xz": [
                 [-2, -2], [2, -2], [2, 2], [-2, 2],
             ]}]}

    result = _anchor_support_references(manifest, scene)

    assert result == {
        "supported_anchors": ["Building:inside"],
        "unsupported_anchors": ["Building:outside"],
        "unverified_anchors": [],
    }


def test_anchor_support_is_unverified_when_building_mesh_has_no_collision_coverage():
    manifest = {"assets": [{"id": "building", "anchors": {"entry": {"pos": [0, 0, 0]}}}]}
    scene = {"objects": [{"name": "Building", "asset": "building", "position": [0, 0, 0],
                           "heading_deg": 0}],
             "support_surfaces": [{"height_m": 0, "polygon_xz": [
                 [-2, -2], [2, -2], [2, 2], [-2, 2]]}],
             "unprotected_scene_objects": ["Building"]}

    result = _anchor_support_references(manifest, scene)

    assert result["supported_anchors"] == []
    assert result["unverified_anchors"] == ["Building:entry"]


def test_scene_derived_standing_spots_use_floor_height_and_avoid_obstacles():
    floor = {"owner": "Hall", "height_m": 0.4,
             "polygon_xz": [[-6, -6], [6, -6], [6, 6], [-6, 6]]}
    ceiling = {"owner": "Hall", "height_m": 4,
               "polygon_xz": floor["polygon_xz"]}
    cabinet = {"owner": "Hall", "min": [-1, 0.4, -1], "max": [1, 2, 1]}
    spots = derive_standing_spots([floor, ceiling], [cabinet])

    assert len(spots) >= 20
    assert {spot["name"].split("_")[1] for spot in spots} == {"left", "right"}
    assert all(abs(spot["position"][1] - 0.4) < 1e-6 for spot in spots)
    assert all(contains(floor, spot["position"], ACTOR_SUPPORT_MARGIN_M) for spot in spots)
    assert all(not (-1.35 <= spot["position"][0] <= 1.35
                    and -1.35 <= spot["position"][2] <= 1.35) for spot in spots)

    scene = {"objects": [{"name": "Hall", "asset": "hall", "position": [0, 0, 0],
                          "heading_deg": 0}], "support_surfaces": [floor, ceiling],
             "standing_spots": spots}
    refs = _anchor_support_references({"assets": [{"id": "hall"}]}, scene)
    assert len(refs["supported_anchors"]) == len(spots)
    first = refs["supported_anchors"][0]
    target, error = ReferenceResolver({"assets": [{"id": "hall"}]}, scene).resolve(first, {})
    assert not error
    assert list(target.position) == spots[0]["position"]


def test_scene_derived_standing_spots_do_not_cross_a_full_height_divider():
    floor = {"owner": "Hall", "height_m": 0,
             "polygon_xz": [[-8, -4], [8, -4], [8, 4], [-8, 4]]}
    divider = {"owner": "Hall", "min": [-0.1, 0, -4], "max": [0.1, 3, 4]}
    spots = derive_standing_spots([floor], [divider])

    assert spots
    assert len({spot["name"].split("_")[1] for spot in spots}) == 1


def test_scene_derived_spot_sides_follow_object_heading():
    floor = {"owner": "Hall", "height_m": 0,
             "polygon_xz": [[-4, -4], [4, -4], [4, 4], [-4, 4]]}
    spots = derive_standing_spots([floor], [], {"Hall": 90})

    assert any(spot["name"].startswith("standing_left") for spot in spots)
    assert any(spot["name"].startswith("standing_right") for spot in spots)
    assert all(spot["position"][2] < 0 for spot in spots
               if spot["name"].startswith("standing_left"))
    assert all(spot["position"][2] > 0 for spot in spots
               if spot["name"].startswith("standing_right"))


def test_ground_spots_exclude_another_objects_footprint():
    ground = {"owner": "Ground", "height_m": 0,
              "polygon_xz": [[-10, -10], [10, -10], [10, 10], [-10, 10]]}
    building = {"owner": "Building", "min": [-3, 0, -3], "max": [3, 3, 3]}
    spots = derive_standing_spots([ground], [building])

    assert spots
    assert all(not (-3.35 <= spot["position"][0] <= 3.35
                    and -3.35 <= spot["position"][2] <= 3.35) for spot in spots)


def test_tiled_floor_area_and_support_are_combined_before_choosing_level():
    from support_surfaces import supports_disc
    # Each tile is smaller than the single elevated ledge; together the floor
    # is broader. Tile seams must not split its connected standing region.
    floors = [{'owner': 'Hall', 'height_m': 0,
               'polygon_xz': [[x, z], [x + 2, z], [x + 2, z + 2], [x, z + 2]]}
              for x in range(-8, 8, 2) for z in range(-6, 6, 2)]
    ledge = {'owner': 'Hall', 'height_m': 2,
             'polygon_xz': [[-4, -2], [4, -2], [4, 2], [-4, 2]]}
    spots = derive_standing_spots(floors + [ledge], [])
    assert len(spots) > 12
    assert all(spot['position'][1] == 0 for spot in spots)
    assert any(spot['position'][0] < -4 for spot in spots)
    assert any(spot['position'][0] > 4 for spot in spots)
    assert all(supports_disc(floors, spot['position']) for spot in spots)
