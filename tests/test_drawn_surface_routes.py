"""Drawn character routes follow measured, reachable top surfaces."""
import sys
from pathlib import Path

from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from support_surfaces import project_drawn_route
from crowd.geometry import clears_solids
from timeline_resolver import ResolvedBeat


def _top(height, half_width=1.0, half_depth=1.0):
    return {"owner": "vehicle", "height_m": height,
            "polygon_xz": [[-half_width, -half_depth], [half_width, -half_depth],
                           [half_width, half_depth], [-half_width, half_depth]]}


def test_route_crosses_reachable_prop_top_and_returns_to_ground():
    points, jumps = project_drawn_route([[0, 0, -3], [0, 0, 3]], [_top(0.9)], 0)
    assert len(jumps) == 2
    assert any(abs(point[1] - 0.9) < 1e-6 for point in points)
    assert points[0] == [0, 0, -3]
    assert points[-1] == [0, 0, 3]
    assert all(abs(point[0]) < 1e-9 for point in points)


def test_low_raised_strip_is_walked_over_with_lifted_root():
    strip = _top(0.165, half_width=2, half_depth=.06)
    box = {'min': [-2, .115, -.06], 'max': [2, .165, .06]}
    route = [[0, 0, -3], [0, 0, 3]]
    assert clears_solids(route, [box], .4)
    points, jumps = project_drawn_route(route, [strip], 0, [box])
    assert jumps == []
    assert len(points) > len(route)
    assert any(point[1] >= .165 for point in points)
    assert points[0] == route[0] and points[-1] == route[-1]


def test_walk_animation_lifts_over_low_strip_without_jump_clip():
    strip = _top(.165, half_width=2, half_depth=.06)
    box = {'min': [-2, .115, -.06], 'max': [2, .165, .06]}
    synth = MotionSynthesizer({'assets': [], 'clips': {'walk': {}}},
                             scene_data={'support_surfaces': [strip], 'solid_boxes': [box]})
    beat = ResolvedBeat('route', 'human_1', 'walk_to', 0, 5, 5,
                        {'brief_path': {'points': [[0, 0, -3], [0, 0, 3]]},
                         'action': 'walk_to'})
    performance, error = synth.synthesize_actor(
        'human_1', {'asset_id': 'character.human', 'position': [0, 0, -3]},
        [beat], {}, {}, 5)
    assert error is None
    assert all(action['clip'] == 'walk' for action in performance.actions)
    assert any(key.position[1] >= .165 for key in performance.keyframes)


def test_taller_barrier_still_blocks_ordinary_walk():
    barrier = {'min': [-2, 0, -.06], 'max': [2, .5, .06]}
    assert not clears_solids([[0, 0, -3], [0, 0, 3]], [barrier], .4)


def test_distant_roof_does_not_lift_ground_route():
    original = [[0, 0, -3], [0, 0, 3]]
    points, jumps = project_drawn_route(original, [_top(8)], 0)
    assert points == original
    assert jumps == []


def test_blocked_top_and_explicit_height_are_preserved():
    original = [[0, 0, -3], [0, 0, 3]]
    ceiling = {"min": [-2, 1.3, -2], "max": [2, 1.6, 2]}
    points, jumps = project_drawn_route(original, [_top(0.9)], 0, [ceiling])
    assert points == original and jumps == []
    authored = [[0, 2, -3], [0, 2, 3]]
    points, jumps = project_drawn_route(authored, [_top(0.9)], 0)
    assert points == authored and jumps == []


def test_synthesis_uses_jump_at_edges_and_run_across_top():
    manifest = {"assets": [], "clips": {"run": {}, "jump": {}}}
    synth = MotionSynthesizer(manifest, scene_data={"support_surfaces": [_top(0.9)]})
    beat = ResolvedBeat("route", "human_1", "run_to", 0, 5, 5,
                        {"brief_path": {"points": [[0, 0, -3], [0, 0, 3]]},
                         "action": "run_to"})
    performance, error = synth.synthesize_actor(
        "human_1", {"asset_id": "character.human", "position": [0, 0, -3]},
        [beat], {}, {}, 5)
    assert error is None
    clips = [action["clip"] for action in performance.actions]
    assert clips == ["run", "jump", "run", "jump", "run"]
    assert any(abs(key.position[1] - 0.9) < 1e-6 and key.action_clip == "run"
               for key in performance.keyframes)


def test_synthesis_keeps_ground_route_at_ground_height():
    synth = MotionSynthesizer({"assets": [], "clips": {"run": {}}},
                             scene_data={"support_surfaces": [_top(0.9)]})
    beat = ResolvedBeat("route", "human_1", "run_to", 0, 3, 3,
                        {"brief_path": {"points": [[3, 0, -2], [3, 0, 2]]},
                         "action": "run_to"})
    performance, error = synth.synthesize_actor(
        "human_1", {"asset_id": "character.human", "position": [3, 0, -2]},
        [beat], {}, {}, 3)
    assert error is None
    assert all(abs(key.position[1]) < 1e-9 for key in performance.keyframes)


def test_compiler_reads_hood_height_for_drawn_route(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Ground")
    ground = UsdGeom.Cube.Define(stage, "/World/Ground/Slab")
    ground.AddTranslateOp().Set(Gf.Vec3d(0, -0.05, 0))
    ground.AddScaleOp().Set(Gf.Vec3f(8, 0.05, 8))
    UsdGeom.Xform.Define(stage, "/World/Vehicle")
    hood = UsdGeom.Cube.Define(stage, "/World/Vehicle/Hood")
    hood.AddTranslateOp().Set(Gf.Vec3d(0, 0.45, 0))
    hood.AddScaleOp().Set(Gf.Vec3f(1, 0.45, 1))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    assert any(face["owner"] == "Vehicle" and abs(face["height_m"] - 0.9) < 1e-6
               for face in scene["support_surfaces"])
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene)
    catalog.place("character.human", position=[0, 0, -3])
    catalog.run_to("human_1", target="Ground:standing_left_01")
    catalog.plan.beats[0].params["brief_path"] = {"points": [[0, 0, -3], [0, 0, 3]]}
    success, report = compiler.compile(catalog.plan, tmp_path / "shot" / "shot.usda",
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:2500]


def test_stationary_placed_car_contributes_measured_hood_support():
    compiler = ShotCompiler(project_root=ROOT)
    car = ActorPerformance("car_1", is_vehicle=True, asset_id="cars.car1",
                           keyframes=[Keyframe(0, [0, 0, 0], 0)])
    state = {"car_1": {"asset_id": "cars.car1", "position": [0, 0, 0],
                       "facing_deg": 0}}
    support = compiler._read_placed_vehicle_support(
        {"car_1": car}, state, {"objects": []})
    points, jumps = project_drawn_route(
        [[0, 0, -3], [0, 0, -1.1]], support["support_surfaces"], 0,
        support["solid_boxes"])
    assert jumps
    assert any(0.5 < point[1] < 1.2 for point in points)


def test_compiled_route_can_run_onto_placed_car_hood(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Ground")
    ground = UsdGeom.Cube.Define(stage, "/World/Ground/Slab")
    ground.AddTranslateOp().Set(Gf.Vec3d(0, -0.05, 0))
    ground.AddScaleOp().Set(Gf.Vec3f(8, 0.05, 8))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene)
    catalog.place("cars.car1", position=[0, 0, 0])
    catalog.place("character.human", position=[0, 0, -3])
    catalog.run_to("human_1", target="Ground:standing_left_01")
    catalog.plan.beats[0].params["brief_path"] = {"points": [[0, 0, -3], [0, 0, -1.1]]}
    success, report = compiler.compile(catalog.plan, tmp_path / "shot" / "shot.usda",
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:2500]
