"""Generated contact permissions include exact frame boundaries, never adjacent frames."""
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
sys.path.insert(0, str(ROOT / 'tools' / 'collision'))
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from timeline_resolver import ResolvedBeat
from validate_collisions import _allowed


@pytest.mark.parametrize('fps', [24, 30, 60, 120, 24000/1001])
def test_generated_vehicle_contacts_survive_json_at_fractional_frame_boundaries(fps):
    manifest = {'assets': [
        {'id':'transport', 'bounds_m':[2.,1.5,4.],
         'anchors':{'driver_seat':{'pos':[-.4,.3,0.]}, 'door_fl_stand':{'pos':[-1.5,0.,0.]}}},
        {'id':'person', 'bounds_max':[.3,1.8,.2]},
    ], 'clips':{'sitting':{},'standing_up':{},'driving':{},'crouch_to_stand':{},'acknowledging':{}}}
    # Different frame phases exercise both rounding directions at transitions.
    cuts = [0., 49.5/fps, 97/fps, 119.5/fps, 167/fps, 221/fps]
    functions = ['drive','exit_vehicle','wait','enter_vehicle','drive']
    beats = [ResolvedBeat(f'action_{i}','occupant',fn,a,b,b-a,{'vehicle':'host'})
             for i,(fn,a,b) in enumerate(zip(functions,cuts,cuts[1:]))]
    car = ActorPerformance('host',asset_id='transport',is_vehicle=True)
    car.keyframes = [Keyframe(0.,[0.,0.,0.],0.), Keyframe(cuts[-1],[0.,0.,0.],0.)]
    state = {'asset_id':'person','position':[-.4,.3,0.], 'at_reference':'host:driver_seat'}
    performance, error = MotionSynthesizer(manifest,fps=fps).synthesize_actor(
        'occupant',state,beats,{}, {'host':car},cuts[-1])
    assert error is None
    rules = json.loads(json.dumps(performance.allowed_contacts))
    assert {r['asset'] for r in rules} == {'host'}
    rules = [r for r in rules if r['asset']=='host']
    obj = SimpleNamespace(asset_id='transport',name='host')
    # Seated contact must cover the actual final sample; entry permission must
    # start at the action boundary, even when decimal formatting would move it.
    ride = max((r for r in rules if r.get('is_occupant')), key=lambda r:r['t0'])
    entry = next(r for r in rules if r['t0'] == cuts[-3])
    assert ride['t1'] == cuts[-1]
    assert entry['t0'] == cuts[-3]
    assert entry['t1'] == cuts[-2]
    assert _allowed([ride],'occupant',obj,'body_1','body','Spine',cuts[-1],.19)
    assert _allowed([entry],'occupant',obj,'door_fl_1','door_fl','RightHand',cuts[-3],.06)
    for rule in rules:
        other_car = SimpleNamespace(asset_id='transport', name='unrelated_host')
        assert not _allowed([rule], 'occupant', other_car, 'body_1', 'body', 'Spine', rule['t0'], .1)
        for boundary in ('t0','t1'):
            assert _allowed([rule],'occupant',obj,'body_1','body','Spine',rule[boundary],.1)
        for outside in (rule['t0']-1/(2*fps),rule['t1']+1/(2*fps)):
            assert not _allowed([rule],'occupant',obj,'body_1','body','Spine',outside,.1)
        assert not _allowed([rule],'occupant',obj,'body_1','body','Spine',rule['t0'],rule['max_depth_m']+.01)


@pytest.mark.parametrize('boundary,direction', [('t0',-math.inf),('t1',math.inf)])
def test_boundary_comparison_handles_float_roundoff_without_millisecond_slack(boundary,direction):
    timestamp = 83/24
    rule = {'actor':'person','asset':'vehicle',boundary:timestamp,'part':'door','joints':['Hand'],'max_depth_m':.1}
    obj = SimpleNamespace(asset_id='transport',name='vehicle')
    sample = math.nextafter(timestamp,direction)
    assert _allowed([rule],'person',obj,'door_1','door','RightHand',sample,.05)
    outside = timestamp + (-1e-5 if boundary=='t0' else 1e-5)
    assert not _allowed([rule],'person',obj,'door_1','door','RightHand',outside,.05)
    assert not _allowed([rule],'stranger',obj,'door_1','door','RightHand',timestamp,.05)
    assert not _allowed([rule],'person',obj,'body_1','body','RightHand',timestamp,.05)
    assert not _allowed([rule],'person',obj,'door_1','door','Spine',timestamp,.05)


@pytest.mark.parametrize('times', [
    {'t0': float('nan')}, {'t1': float('inf')}, {'t0': None},
    {'t1': 'invalid'}, {'t0': 2., 't1': 1.},
])
@pytest.mark.parametrize('parallel', [False, True])
def test_invalid_windows_fail_before_sampling_and_cannot_trigger_spatial_repair(times, parallel):
    from validate_collisions import validate, validate_parallel
    from crowd.config import CrowdSolverConfig
    from crowd.repair import next_clearance

    rule = {'actor': 'person', 'asset': 'vehicle', **times}
    if parallel:
        report = validate_parallel(None, None, None, None, allow=[rule], workers=2)
    else:
        report = validate(None, None, allow=[rule])
    assert not report['ok']
    assert report['time_steps'] == 0
    assert report['error']['error_code'] == 'CONTACT_WINDOW_INVALID'
    assert report['error']['details']['rules'][0]['actor'] == 'person'
    config = CrowdSolverConfig()
    assert next_clearance(config, config.buffer_m,
                          {'validation': {'collision_report': report}}) is None


@pytest.mark.parametrize('fps,asset_id', [(24, 'cars.car1'), (30, 'cars.car2')])
def test_baked_exit_enter_drive_preserves_fractional_contact_boundaries(tmp_path, fps, asset_id):
    from compiler import ShotCompiler
    from plan_model import Beat, ShotPlan, SetupAction, TimingCondition
    from pxr import Gf, Usd, UsdGeom

    scene_path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Ground')
    floor.GetSizeAttr().Set(2.)
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(20, .1, 20))
    stage.GetRootLayer().Save()
    durations = [49.5/fps, 61/fps, 25/fps, 73.5/fps, 97/fps]
    actions = [('park', 'wait', 'host', {}),
               ('leave', 'exit_vehicle', 'visitor', {'vehicle': 'host'}),
               ('pause', 'wait', 'visitor', {}),
               ('enter', 'enter_vehicle', 'visitor', {'vehicle': 'host'}),
               ('depart', 'drive_to', 'host', {'target': [0, 0, -6]})]
    beats = [Beat(name, function, actor, params,
                  TimingCondition('after', actions[i-1][0] + '.finished') if i else TimingCondition('at', 0),
                  TimingCondition('for_duration', durations[i]))
             for i, (name, function, actor, params) in enumerate(actions)]
    plan = ShotPlan(setup=[
        SetupAction('place', 'host', asset_id, position=[0, 0, 0]),
        SetupAction('place', 'visitor', 'character.male', at_reference='host:driver_seat'),
    ], beats=beats)
    output = tmp_path / 'shot' / 'shot.usda'
    ok, report = ShotCompiler(ROOT, fps=fps).compile(plan, output, scene_path, validate=True)
    assert ok, json.dumps(report, indent=2)
    assert report['validation']['collision_passed']
    assert report['crowd_repair']['retries'] == 0
    rules = json.loads((output.parent / 'allow_collisions.json').read_text())
    actions = json.loads((output.parent / 'actions_visitor.json').read_text())['actions']
    entry_start = next(a['start_s'] for a in actions if a['clip'] == 'crouch_to_stand')
    assert entry_start != round(entry_start, 3)
    assert any(r.get('t0') == entry_start for r in rules)

    # Contact permission must never exempt a head above the roof, including
    # at the final inclusive endpoint (with arithmetic roundoff).
    from colliders import AssetDB
    from validate_collisions import validate
    actor_name = report['validation']['collision_report']['actors'][0]
    end = max(r['t1'] for r in rules if r.get('actor') == actor_name and r.get('is_occupant'))
    baked_stage = Usd.Stage.Open(str(output))
    baked_stage.SetEditTarget(baked_stage.GetSessionLayer())
    actor_prim = next(p for p in baked_stage.Traverse() if p.GetName() == actor_name)
    translate = next(op for op in UsdGeom.Xformable(actor_prim).GetOrderedXformOps()
                     if op.GetOpType() == UsdGeom.XformOp.TypeTranslate)
    frame = math.nextafter(end, math.inf) * fps
    position = translate.Get(frame)
    db = AssetDB(str(ROOT / 'manifest.json'))
    roof_lift = db.lookup_id(asset_id)['bounds_m'][1]
    translate.Set(position + Gf.Vec3d(0, roof_lift, 0), frame)
    invalid = validate(baked_stage, db,
                       allow=rules, sample_times=[frame], check_levels=False)
    assert not invalid['ok']
    assert any(v['part'] == 'cabin_roof' for v in invalid['violations'])
