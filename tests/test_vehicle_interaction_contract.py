"""Public boarding calls must use the same physical transition as the compiler."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
from compiler import ShotCompiler
from function_catalog import FunctionCatalog


@pytest.mark.parametrize('method', ['enter_vehicle', 'board_vehicle'])
@pytest.mark.parametrize('seat', ['driver_seat', 'passenger_seat', 'seat_rear_left', 'seat_rear_right'])
def test_public_boarding_emits_entry_not_seated_teleport(method, seat):
    catalog = FunctionCatalog({'assets': [{'id': 'vehicle'}, {'id': 'person'}]})
    _, host = catalog.place('vehicle')
    _, person = catalog.place('person')
    host, person = host['actor_id'], person['actor_id']
    if method == 'enter_vehicle':
        ok, result = catalog.enter_vehicle(person, host, seat=seat)
    else:
        ok, result = catalog.board_vehicle(host, [person], seats=[seat])
    assert ok, result
    beat = catalog.plan.beats[-1]
    assert beat.function == 'enter_vehicle'
    assert beat.params['vehicle'] == host
    assert beat.params['seat'] == seat


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('fps,asset_id,heading', [(24, 'cars.car1', 90), (30, 'cars.car2', 180)])
def test_public_exit_shoot_reenter_depart_validates(tmp_path, fps, asset_id, heading, legacy):
    from pxr import Gf, Usd, UsdGeom
    scene = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(20, .1, 20))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(ROOT, fps=fps)
    catalog = FunctionCatalog(compiler.manifest)
    _, host = catalog.place(asset_id, position=[0, 0, 0], facing_deg=heading)
    host = host['actor_id']
    _, person = catalog.place('character.male', in_vehicle=host)
    person = person['actor_id']
    assert catalog.exit_vehicle(person, host)[0]
    assert catalog.use_weapon(person, target='point(8, 0, 8)', weapon='pistol', action='shoot')[0]
    assert catalog.enter_vehicle(person, host)[0]
    entry = catalog.plan.beats[-1]
    if legacy:
        entry.function = 'sit_in'
        del entry.params['seat']  # Old plans encoded the seat in target.
    assert catalog.drive_to(host, target='point(6, 0, -6)', after=entry.beat_id + '.finished')[0]
    output = tmp_path / 'shot' / 'shot.usda'
    ok, report = compiler.compile(catalog.plan, output, scene, validate=True)
    assert ok, json.dumps(report, indent=2)
    assert report['crowd_repair']['retries'] == 0
    actions = json.loads((output.parent / f'actions_{person}.json').read_text())['actions']
    assert any(a['clip'] == 'crouch_to_stand' for a in actions)


def test_llm_entry_reference_sequences_departure():
    from llm_planner import LLMShotPlanner
    catalog = FunctionCatalog({'assets': [{'id': 'vehicle'}, {'id': 'person'}]})
    planner = LLMShotPlanner(catalog)
    ok, plan, logs = planner.plan_from_llm_json({'calls': [
        {'function': 'place', 'args': {'asset_id': 'vehicle', 'label': 'host'}},
        {'function': 'place', 'args': {'asset_id': 'person', 'label': 'rider'}},
        {'function': 'declare_interaction', 'args': {
            'interaction_id': 'boarding', 'participants': ['rider'],
            'actions': ['rider.enter_vehicle'], 'timing': 'sequence'}},
        {'function': 'declare_interaction', 'args': {
            'interaction_id': 'departure', 'participants': ['host'],
            'actions': ['host.drive_to'], 'timing': 'sequence', 'after': ['boarding']}},
        {'function': 'enter_vehicle', 'args': {'actor': 'rider', 'vehicle': 'host'}},
        {'function': 'drive_to', 'args': {'vehicle': 'host', 'target': 'east'}},
    ]})
    assert ok, logs
    entry = next(b for b in plan.beats if b.function == 'enter_vehicle')
    from interaction_plan import apply_interactions
    states = {state['actor_id']: state for state in catalog.registry.list_actors()}
    assert apply_interactions(plan, states) is None
    departure = next(b for b in plan.beats if b.function == 'drive_to')
    assert entry.beat_id in departure.params['wait_for']
    assert plan.interactions[1].after == ['boarding']


@pytest.mark.parametrize('function', ['enter_vehicle', 'sit_in'])
@pytest.mark.parametrize('seat', ['driver_seat', 'passenger_seat'])
def test_entry_preserves_outside_start_and_requested_seat(function, seat):
    from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
    from timeline_resolver import ResolvedBeat
    seats = {'driver_seat': {'pos': [-.4, .2, 0.]},
             'passenger_seat': {'pos': [.4, .2, 0.]}}
    manifest = {'assets': [{'id': 'transport', 'anchors': seats, 'bounds_m': [2., 1.5, 4.]},
                           {'id': 'person', 'bounds_max': [.3, 1.8, .2]}],
                'clips': {'sitting': {}, 'crouch_to_stand': {}}}
    host = ActorPerformance('host', asset_id='transport', is_vehicle=True)
    host.keyframes = [Keyframe(0., [0., 0., 0.], 0.), Keyframe(3., [0., 0., 0.], 0.)]
    start = [-3. if seat == 'driver_seat' else 3., 0., 0.]
    beat = ResolvedBeat('board', 'rider', function, 0., 3., 3., {'target': f'host:{seat}'})
    performance, error = MotionSynthesizer(manifest).synthesize_actor(
        'rider', {'asset_id': 'person', 'position': start}, [beat], {}, {'host': host}, 3.)
    assert error is None
    assert performance.sample(0.)[0] == start
    assert performance.sample(.75)[0][0] == start[0]
    assert performance.sample(3.)[0][0] == pytest.approx(seats[seat]['pos'][0])
    assert performance.actions[0]['clip'] == 'crouch_to_stand'
