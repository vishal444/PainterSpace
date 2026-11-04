"""The compiler routes against the same manifest geometry as final validation."""
import json
import math
from pathlib import Path
import sys

import pytest
from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
from compiler import ShotCompiler
from colliders import SceneObject, world_groups
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids
from crowd.st_planner import plan_route
from route_placement import PlacementRoutes
from plan_model import Beat, SetupAction, ShotPlan, TimingCondition


def environment(tmp_path, mixed, articulated, rotation=0., scale=1.):
    path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(15, .1, 15))
    prop = UsdGeom.Xform.Define(stage, '/World/Structure')
    prop.GetPrim().SetCustomDataByKey('asset_id', 'environment.test')
    prop.AddRotateYOp().Set(rotation)
    prop.AddScaleOp().Set(Gf.Vec3f(scale, 1., scale))
    if mixed:
        # A decorative cube must not make the mesh's collision shapes disappear.
        cube = UsdGeom.Cube.Define(stage, '/World/Structure/Trim')
        cube.AddTranslateOp().Set(Gf.Vec3d(0, 4, 0))
        cube.AddScaleOp().Set(Gf.Vec3f(.1))
    UsdGeom.Mesh.Define(stage, '/World/Structure/Geometry')
    shape = {'id': 'wall', 'type': 'box', 'center': [0, 1.5, 0], 'size': [3, 3, 5]}
    entry = {'id': 'environment.test', 'tags': ['environment'],
             'collision': {'shapes': [shape]}}
    if articulated:
        shape['part'] = 'gate'
        part = UsdGeom.Xform.Define(stage, '/World/Structure/Gate')
        part.AddTranslateOp().Set(Gf.Vec3d(2, 0, 0))
        part.AddRotateYOp().Set(35.)
        entry['parts'] = {'gate': {'prim': 'Gate', 'collider_frame':
                         [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}}
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(ROOT)
    compiler.manifest['assets'].append(entry)
    return compiler, stage, path, entry


@pytest.mark.parametrize('mixed,articulated,rotation,scale', [
    (True, False, 0., 1.), (True, False, 90., 1.5),
    (False, True, 0., 1.), (True, True, 110., .8),
])
def test_environment_snapshot_contains_every_validator_shape(tmp_path, mixed, articulated, rotation, scale):
    compiler, stage, path, entry = environment(tmp_path, mixed, articulated, rotation, scale)
    scene = compiler._read_scene_context(path)
    assert not scene.get('read_error')
    assert not scene['unprotected_scene_objects']
    obj = SceneObject(stage.GetPrimAtPath('/World/Structure'), entry, 'asset')
    groups = world_groups(stage, obj, Usd.TimeCode.Default(), UsdGeom.XformCache())
    for group in groups:
        for corners in group.corners_world():
            lo, hi = corners.min(0), corners.max(0)
            assert any(box['min'] == pytest.approx(lo) and box['max'] == pytest.approx(hi)
                       for box in scene['solid_boxes'])
            center = [(lo[0] + hi[0])/2, 0., (lo[2] + hi[2])/2]
            assert not clears_solids([center], scene['solid_boxes'], .4)


@pytest.mark.parametrize('shape_type', ['box', 'cylinder'])
def test_articulated_support_uses_live_transform_without_inventing_cylinder_corners(tmp_path, shape_type):
    compiler, stage, path, entry = environment(tmp_path, False, True, 40., 1.5)
    entry['collision']['shapes'] = [
        {'id': 'platform', 'part': 'gate', 'type': shape_type,
         'center': [0, .05, 0], 'size': [4, .1, 4], 'radius': 2, 'y0': 0, 'y1': .1}]
    scene = compiler._read_scene_context(path)
    assert not scene.get('read_error')
    supports = [s for s in scene['support_surfaces'] if s['owner'] == 'Structure']
    if shape_type == 'cylinder':
        # Its circumscribed blocker box cannot certify support at the corners.
        assert not supports
    else:
        assert len(supports) == 1
        matrix = UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath('/World/Structure/Gate'))
        expected = [matrix.Transform(Gf.Vec3d(x, .1, z))
                    for x, z in ((-2, -2), (2, -2), (2, 2), (-2, 2))]
        for actual, point in zip(supports[0]['polygon_xz'], expected):
            assert actual == pytest.approx([point[0], point[2]])
        assert supports[0]['height_m'] == pytest.approx(.1)


@pytest.mark.parametrize('rotation', [0., 90., 180., 270.])
def test_corner_route_detours_around_manifest_wall(tmp_path, rotation):
    compiler, stage, path, entry = environment(tmp_path, True, False, rotation)
    scene = compiler._read_scene_context(path)
    config = CrowdSolverConfig()
    routes = PlacementRoutes(scene, config)
    turn = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 1, 0), rotation))
    start, via, goal = [tuple(turn.Transform(Gf.Vec3d(*p)))
                        for p in ((8, 0, 0), (-4, 0, 0), (-4, 0, 8))]
    result = plan_route(start, goal, 0., 1.4, .4, [],
                        lambda points: all(routes.clear(tuple(a), tuple(b), .4)
                                           for a, b in zip(points, points[1:] or points)),
                        routes.bounds, config, via=[via])
    assert result.status == 'ok'
    # Check against validator geometry independently of the compiler snapshot.
    obj = SceneObject(stage.GetPrimAtPath('/World/Structure'), entry, 'asset')
    boxes = [{'min': c.min(0), 'max': c.max(0)}
             for g in world_groups(stage, obj, Usd.TimeCode.Default(), UsdGeom.XformCache())
             for c in g.corners_world()]
    assert clears_solids([p[1:] for p in result.points], boxes, .4 + config.buffer_m)
    assert any(math.dist(p[1:], via) < 1e-7 for p in result.points)
    assert result.points[-1][1:] == pytest.approx(goal)


@pytest.mark.parametrize('articulated', [False, True])
def test_full_compile_avoids_environment_and_reaches_south_road(tmp_path, articulated):
    compiler, stage, path, entry = environment(tmp_path, True, articulated)
    # Keep the validator subprocess on the same fixture manifest as planning.
    compiler.manifest_path = tmp_path / 'manifest.json'
    compiler.manifest_path.write_text(json.dumps(compiler.manifest))
    (tmp_path / 'assets').symlink_to(ROOT / 'assets', target_is_directory=True)
    speed = compiler.manifest['clips']['walk']['stride_speed_mps']
    plan = ShotPlan(setup=[SetupAction('place', 'walker', 'character.human', position=[8, 0, 0])],
                    beats=[Beat('corner', 'walk_to', 'walker', {'target': [-4, 0, 0]},
                                duration_condition=TimingCondition('for_duration', 12 / speed)),
                           Beat('south', 'walk_to', 'walker', {'target': [-4, 0, 8]},
                                TimingCondition('after', 'corner.end'),
                                TimingCondition('for_duration', 8 / speed))])
    output = tmp_path / 'shot' / 'shot.usda'
    ok, report = compiler.compile(plan, output, path, validate=True)
    assert ok, json.dumps(report, indent=2)
    assert report['validation']['collision_passed']
    compiled = Usd.Stage.Open(str(output))
    actor = next(p for p in compiled.Traverse() if p.GetCustomDataByKey('actor_id') == 'walker')
    position = UsdGeom.XformCache(Usd.TimeCode(compiled.GetEndTimeCode())).GetLocalToWorldTransform(actor).ExtractTranslation()
    assert list(position) == pytest.approx([-4, 0, 8], abs=.01)


def test_residential_intersection_east_to_south_corner(tmp_path):
    from function_catalog import FunctionCatalog
    compiler = ShotCompiler(ROOT)
    asset = next(a for a in compiler.manifest['assets']
                 if a['id'] == 'environment.residential_intersection')
    scene_path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, '/World')
    junction = UsdGeom.Xform.Define(stage, '/World/Junction').GetPrim()
    junction.SetCustomDataByKey('asset_id', asset['id'])
    junction.GetReferences().AddReference(str(ROOT / asset['path']))
    stage.GetRootLayer().Save()
    scene = compiler._read_scene_context(scene_path)
    assert not scene.get('read_error')
    regions = scene['spatial_regions']
    floor = min((r for r in regions if r['kind'] == 'floor'), key=lambda r: r['height_m'])
    entries = [r for r in regions if r['kind'] == 'entry' and r['support_region'] == floor['id']]
    east = next(r for r in entries if r['entry_normal'] == [1, 0])
    south = next(r for r in entries if r['entry_normal'] == [0, 1])
    # The inner southeast street corner is the nearest southwest-facing
    # obstacle corner in that quadrant, derived from the actual asset bounds.
    center = floor['preferred']
    corner = min((r for r in regions if r['kind'] == 'corner'
                  and r['support_region'] == floor['id'] and r['corner_normal'] == [-1, -1]
                  and r['corner'][0] > center[0] and r['corner'][2] > center[2]),
                 key=lambda r: math.dist(r['corner'], center))
    catalog = FunctionCatalog(compiler.manifest, scene)
    ok, result = catalog.stage_region_approach('turn_south', ['walker'], east['id'], south['id'],
                                              via_regions=[corner['id']])
    assert ok, result
    catalog.plan.prompt = 'Start from the east by walking, take the corner and move towards the south road.'
    output = tmp_path / 'shot' / 'shot.usda'
    ok, report = compiler.compile(catalog.plan, output, scene_path, validate=True)
    assert ok, repr(report)[:5000]
    assert report['validation']['collision_passed']
    binding = report['validation']['spatial_intents'][0]['bindings']['walker']
    assert binding['source'][0] > center[0]
    assert binding['destination'][2] > center[2]
    assert binding['via_points']
