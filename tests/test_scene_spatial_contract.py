import math
import sys
import pytest
from pathlib import Path

from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from plan_model import SetupAction, ShotPlan
from semantic_references import ReferenceResolver


def _scene(path, with_building):
    stage = Usd.Stage.CreateNew(str(path))
    world = UsdGeom.Xform.Define(stage, "/World")
    zone = UsdGeom.Cube.Define(stage, "/World/Zones/road")
    zone.CreateSizeAttr(4)
    zone.AddTranslateOp().Set(Gf.Vec3d(6, 0, -2))
    zone.AddRotateYOp().Set(90)
    zone.AddScaleOp().Set(Gf.Vec3f(0.5, 0.1, 3))
    if with_building:
        building = UsdGeom.Xform.Define(stage, "/World/Building")
        building.GetPrim().SetCustomDataByKey("asset_id", "environment.residential_neighborhood")
        building.AddTranslateOp().Set(Gf.Vec3d(10, 0, 5))
        building.AddRotateYOp().Set(90)
        UsdGeom.Mesh.Define(stage, "/World/Building/Geometry")
    stage.GetRootLayer().Save()


def _compiler_without_neighborhood_collision():
    compiler = ShotCompiler(project_root=ROOT)
    asset = next(item for item in compiler.manifest["assets"]
                 if item["id"] == "environment.residential_neighborhood")
    asset.pop("collision", None)
    return compiler


def test_scene_context_exposes_world_heading_and_authored_zone(tmp_path):
    scene_path = tmp_path / "scene.usda"
    _scene(scene_path, with_building=True)
    compiler = _compiler_without_neighborhood_collision()

    scene = compiler._read_scene_context(scene_path)

    building = next(obj for obj in scene["objects"] if obj["name"] == "Building")
    assert building["position"] == [10, 0, 5]
    assert math.isclose(building["heading_deg"], 270)
    assert scene["unprotected_scene_objects"] == ["Building"]
    target, error = ReferenceResolver(compiler.manifest, scene).resolve("road.center", {})
    assert not error
    assert all(math.isclose(actual, expected, abs_tol=1e-9)
               for actual, expected in zip(target.position, [6, 0, -2]))
    assert math.isclose(target.facing_deg, 270)
    east, error = ReferenceResolver(compiler.manifest, scene).resolve("road.east", {})
    assert not error
    assert math.isclose(east.position[0], 11)
    assert math.isclose(east.position[2], -2)


def test_scene_context_exports_composed_actor_identity_before_planning(tmp_path):
    scene_path = tmp_path / 'scene.usda'
    _scene(scene_path, with_building=False)
    shot_dir = tmp_path / 'shot'
    shot_dir.mkdir()
    root = Usd.Stage.CreateNew(str(shot_dir / 'shot.usda'))
    root.GetRootLayer().Save()
    edits = Usd.Stage.CreateNew(str(shot_dir / 'user_edits.usda'))
    actor = UsdGeom.Xform.Define(edits, '/World/Resident')
    actor.GetPrim().SetCustomDataByKey('actor_id', 'resident_id')
    actor.GetPrim().SetCustomDataByKey('asset_id', 'character.male')
    actor.AddTranslateOp().Set(Gf.Vec3d(4, 0, 3))
    hidden = UsdGeom.Xform.Define(edits, '/World/Deleted')
    hidden.GetPrim().SetCustomDataByKey('asset_id', 'character.female')
    hidden.GetPrim().SetActive(False)
    edits.GetRootLayer().Save()
    scene = ShotCompiler(project_root=ROOT)._read_scene_context(scene_path, shot_dir=shot_dir)
    assert not scene.get('read_error')
    assert len(scene['existing_actors']) == 1
    record = scene['existing_actors'][0]
    assert (record['actor_id'], record['label'], record['prim_path'], record['asset_id']) == (
        'resident_id', 'Resident', '/World/Resident', 'character.male')
    assert record['position'] == [4, 0, 3]
    # Context remains serializable for both the queue and live LLM inventories.
    import json
    assert json.loads(json.dumps(scene['existing_actors'])) == scene['existing_actors']


def test_direction_words_keep_world_and_actor_relative_meanings():
    planner = LLMShotPlanner(FunctionCatalog({"assets": []}, {"objects": [], "zones": []}))
    resolver = ReferenceResolver({"assets": []}, {
        "support_surfaces": [{"owner": "Floor", "height_m": 0.,
                              "polygon_xz": [[-27, -26], [33, -26], [33, 34], [-27, 34]]}],
        "solid_boxes": []})
    actor_states = {"human_1": {"position": [3.0, 0.0, 4.0], "facing_deg": 180.0}}

    for words, expected in (
        ("walk north", (0, -1)), ("walk southbound", (0, 1)),
        ("walk east", (1, 0)), ("walk westward", (-1, 0)),
        ("walk northeast", (1, -1)), ("walk south-west", (-1, 1)),
    ):
        ref = planner._resolve_clause_destination(words, [], ["human_1"], {})
        target, error = resolver.resolve(ref, actor_states, source_actor_id="human_1")
        assert not error
        dx = target.position[0] - 3.0
        dz = target.position[2] - 4.0
        if expected[0]:
            assert math.copysign(1, dx) == expected[0]
        else:
            assert abs(dx) < 1e-6
        if expected[1]:
            assert math.copysign(1, dz) == expected[1]
        else:
            assert abs(dz) < 1e-6

    left = planner._resolve_clause_destination("walk left", [], ["human_1"], {})
    assert left == "beside(human_1, left, far)"
    assert planner._resolve_clause_destination("walk northwest", [], ["human_1"], {}) == "northwest"


def test_scene_object_origin_is_named_and_unknown_center_lists_objects():
    scene = {"objects": [{"name": "Junction", "position": [4.0, 0.0, -2.0],
                          "asset_id": "environment.junction"}], "zones": []}
    manifest = {"assets": [{"id": "environment.junction", "pivot": "Junction center at origin"}]}
    resolver = ReferenceResolver(manifest, scene)

    target, error = resolver.resolve("Junction", {})
    assert not error
    assert list(target.position) == [4.0, 0.0, -2.0]
    assert target.target_type == "scene_object"

    missing, error = resolver.resolve("center", {})
    assert missing is None
    assert "Valid scene objects: ['Junction']" in error

    prompt = LLMShotPlanner(FunctionCatalog(manifest, scene)).build_system_prompt()
    assert "Junction center at origin" in prompt
    assert "bare word such as `center`" in prompt


def test_generic_marker_rejected_when_building_mesh_is_uncovered(tmp_path):
    scene_path = tmp_path / "scene.usda"
    _scene(scene_path, with_building=True)
    compiler = _compiler_without_neighborhood_collision()
    scene = compiler._read_scene_context(scene_path)

    target, error = ReferenceResolver(compiler.manifest, scene).resolve("lane_east.north", {})

    assert target is None
    assert "not authored" in error
    prompt = LLMShotPlanner(FunctionCatalog(compiler.manifest, scene)).build_system_prompt()
    assert "road.center" in prompt
    assert "road.east" in prompt
    assert "lane_east.north" not in prompt
    assert '"heading_deg": 270.0' in prompt


def test_validated_compile_reports_missing_building_collision_coverage(tmp_path):
    scene_path = tmp_path / "scene.usda"
    _scene(scene_path, with_building=True)
    shot_path = tmp_path / "shot" / "shot.usda"
    plan = ShotPlan(setup=[SetupAction("place", "human_1", "character.human",
                                       at_reference="road.center")])

    success, report = _compiler_without_neighborhood_collision().compile(
        plan, shot_path, scene_usda_path=scene_path, validate=True)

    assert not success
    assert report["error"]["error_code"] == "SCENE_COLLISION_COVERAGE"
    assert report["error"]["details"]["objects"] == ["Building"]
    assert not shot_path.exists()


def test_covered_scene_keeps_authored_zone_available(tmp_path):
    scene_path = tmp_path / "scene.usda"
    _scene(scene_path, with_building=False)
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)

    assert scene["unprotected_scene_objects"] == []
    target, error = ReferenceResolver(compiler.manifest, scene).resolve("road.center", {})
    assert not error
    assert all(math.isclose(actual, expected, abs_tol=1e-9)
               for actual, expected in zip(target.position, [6, 0, -2]))


def test_thin_manifest_collider_supplies_measured_standing_spots(tmp_path):
    scene_path = tmp_path / "scene.usda"
    _scene(scene_path, with_building=True)
    compiler = ShotCompiler(project_root=ROOT)
    asset = next(item for item in compiler.manifest["assets"]
                 if item["id"] == "environment.residential_neighborhood")
    asset["collision"] = {"frame": "asset", "shapes": [
        {"id": "floor", "type": "box", "center": [0, 0.2, 0], "size": [8, 0.1, 8]},
        {"id": "wall", "type": "box", "center": [0, 1.2, 0], "size": [1, 2, 1]},
    ]}

    scene = compiler._read_scene_context(scene_path)

    assert scene["unprotected_scene_objects"] == []
    assert len(scene["standing_spots"]) > 0
    assert all(spot["owner"] == "Building" for spot in scene["standing_spots"])
    assert all(math.isclose(spot["position"][1], 0.25) for spot in scene["standing_spots"])


def test_unknown_mesh_asset_is_unprotected_regardless_of_tag(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    prop = UsdGeom.Xform.Define(stage, "/World/Statue")
    prop.GetPrim().SetCustomDataByKey("asset_id", "prop.statue")
    UsdGeom.Mesh.Define(stage, "/World/Statue/Geometry")
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    compiler.manifest["assets"].append({"id": "prop.statue", "tags": ["prop"]})

    scene = compiler._read_scene_context(scene_path)
    target, error = ReferenceResolver(compiler.manifest, scene).resolve("lane_east.north", {})
    prompt = LLMShotPlanner(FunctionCatalog(compiler.manifest, scene)).build_system_prompt()

    assert scene["unprotected_scene_objects"] == ["Statue"]
    assert target is None
    assert "not authored" in error
    assert "lane_east.north" not in prompt


def test_covered_scene_without_zones_does_not_advertise_a_lane(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    ground = UsdGeom.Xform.Define(stage, "/World/Ground")
    UsdGeom.Cube.Define(stage, "/World/Ground/Floor")
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)

    target, error = ReferenceResolver(compiler.manifest, scene).resolve("lane_east.north", {})
    prompt = LLMShotPlanner(FunctionCatalog(compiler.manifest, scene)).build_system_prompt()

    assert scene["unprotected_scene_objects"] == []
    assert target is None
    assert "not authored" in error
    assert "lane_east.north" not in prompt


def test_semantic_planner_uses_standing_spots_when_scene_has_no_authored_zones(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    building = UsdGeom.Xform.Define(stage, "/World/Neighborhood")
    building.GetPrim().SetCustomDataByKey("asset_id", "environment.residential_neighborhood")
    stage.GetRootLayer().Save()

    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    neighborhood = next(obj for obj in scene["objects"] if obj["name"] == "Neighborhood")
    assert neighborhood["obstacle_bounds_xz"] is not None
    assert neighborhood["obstacle_box_count"] > 0

    planner = LLMShotPlanner(FunctionCatalog(compiler.manifest, scene))
    prompt = planner.build_system_prompt()
    assert "Neighborhood:standing_" in prompt
    assert '"obstacle_bounds_xz":' in prompt

    ok, plan, log = planner.plan_from_prompt("A car drives up and stops while two people converse.")
    assert ok, log
    refs_used = [s.at_reference for s in plan.setup if s.at_reference]
    assert refs_used
    assert all("lane_east" not in ref for ref in refs_used)
    assert any(ref.startswith("Neighborhood:standing_") for ref in refs_used)

    shot_path = tmp_path / "shot" / "shot.usda"
    compiled_ok, report = compiler.compile(plan, shot_path, scene_usda_path=scene_path, validate=True)
    assert compiled_ok, report




@pytest.mark.parametrize('width', [.4, .6])
def test_measured_support_is_not_discarded_for_lacking_placement_markers(tmp_path, width):
    from crowd.geometry import supports_body_route
    from crowd.config import CrowdSolverConfig
    path = tmp_path / 'support.usda'
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.Xform.Define(stage, '/World')
    mesh = UsdGeom.Mesh.Define(stage, '/World/Strip')
    mesh.CreatePointsAttr([(-width/2, 1, -4), (-width/2, 1, 4), (width/2, 1, 4), (width/2, 1, -4)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    stage.GetRootLayer().Save()
    scene = ShotCompiler(project_root=ROOT)._read_scene_context(path)
    faces = [face for face in scene['support_surfaces'] if face['prim_path'] == '/World/Strip']
    assert faces
    assert not scene['standing_spots']
    assert not scene['surface_spots']
    assert supports_body_route([[0, 1, -3], [0, 1, 3]], width/4, faces, CrowdSolverConfig())
