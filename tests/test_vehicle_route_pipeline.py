"""Ground routes must preserve physical exits and avoid placed moving solids."""
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))

from crowd.movements import route_movements
from crowd.st_planner import Reservation, blocked_intervals
from crowd.config import CrowdSolverConfig
from crowd_spacing import ActorEnvelopes
from motion_synthesis import MotionSynthesizer
from plan_model import Beat, ShotPlan, SetupAction, TimingCondition
from semantic_references import ReferenceResolver
from timeline_resolver import TimelineResolver


@pytest.mark.parametrize('bounds,heading,exit_duration', [
    ([2., 1.5, 4.], 0., 2.13), ([3., 2., 6.], 90., 1.87)])
def test_exit_then_reserved_run_uses_actual_exit_and_avoids_vehicle(bounds, heading, exit_duration):
    manifest = {'assets': [
        {'id': 'vehicle', 'bounds_m': bounds,
         'anchors': {'driver_seat': {'pos': [-.3, .3, 0]},
                     'door_fl_stand': {'pos': [-1.5, 0, -.5]}}},
        {'id': 'person', 'tags': ['character']},
    ], 'clips': {'sitting': {}, 'standing_up': {}, 'run': {},
                 'acknowledging': {}, 'groving_to_music_energetically': {}}}
    scene = {'support_surfaces': [{'height_m': 0.,
             'polygon_xz': [[-25, -25], [25, -25], [25, 25], [-25, 25]]}]}
    states = {
        'host': {'asset_id': 'vehicle', 'is_vehicle': True, 'position': [0, 0, 0],
                 'facing_deg': heading},
        'visitor': {'asset_id': 'person', 'is_vehicle': False, 'position': [-.3, .3, 0],
                    'at_reference': 'host:driver_seat'},
        'other_host': {'asset_id': 'vehicle', 'is_vehicle': True, 'position': [12, 0, 0],
                       'facing_deg': heading},
        'partner': {'asset_id': 'person', 'is_vehicle': False, 'position': [11.7, .3, 0],
                    'at_reference': 'other_host:driver_seat'},
    }
    plan = ShotPlan(beats=[
        Beat('leave', 'exit_vehicle', 'visitor', {'vehicle': 'host'},
             TimingCondition('at', 0), TimingCondition('for_duration', exit_duration)),
        Beat('partner_leaves', 'exit_vehicle', 'partner', {'vehicle': 'other_host'},
             TimingCondition('at', 0), TimingCondition('for_duration', exit_duration)),
        Beat('arrive', 'run_to', 'visitor', {'target': 'partner'},
             TimingCondition('after', 'leave.finished'), TimingCondition('for_duration', 4)),
        Beat('pose', 'dance', 'visitor', {'style': 'groove'},
             TimingCondition('after', 'arrive.finished'), TimingCondition('for_duration', 2)),
    ])
    envelopes = ActorEnvelopes({'visitor': .3, 'partner': .3}, {'pose': 1.2},
                               solid_travel_reach={'run_to': .9},
                               solid_beat_reach={'pose': 1.2})
    references = ReferenceResolver(manifest, scene)
    resolver = TimelineResolver(ref_resolver=references)
    synthesizer = MotionSynthesizer(manifest, ref_resolver=references,
                                    solid_envelopes=envelopes, scene_data=scene)
    routed, error = route_movements(plan, states, resolver, synthesizer, scene, envelopes)
    assert error is None, error
    timeline, error = resolver.resolve(routed, states)
    assert error is None
    performances = {}
    for actor in ('host', 'other_host', 'partner', 'visitor'):
        performance, error = synthesizer.synthesize_actor(
            actor, states[actor], timeline.actor_schedules.get(actor, []), {}, performances,
            timeline.total_duration_s, ref_resolver=references, scene_data=scene,
            all_initial_states=states)
        assert error is None, error
        performances[actor] = performance
    path_beats = [b for b in timeline.beats if b.params.get('brief_path')]
    assert path_beats
    first = min(path_beats, key=lambda b: b.start_time)
    exit_time = next(b.end_time for b in timeline.beats if b.beat_id == 'leave')
    exit_keys = [k for k in performances['visitor'].keyframes if k.time_s <= exit_time]
    actual_exit = exit_keys[-1].position
    assert first.params['brief_path']['points'][0] == pytest.approx(actual_exit)
    from crowd.trajectories import planning_tracks
    tracks, _, error = planning_tracks(timeline, states, resolver, synthesizer, scene)
    assert error is None
    assert resolver._sample_traj(tracks['partner'], exit_time) == pytest.approx(
        performances['partner'].sample(exit_time)[0])
    vehicles = [Reservation(actor, [(0., *states[actor]['position'])],
                            math.hypot(bounds[0], bounds[2]) / 2)
                for actor in ('host', 'other_host')]
    for beat in path_beats:
        from director_brief import resolve_path_for_actor
        samples = resolve_path_for_actor(beat.params['brief_path'], (beat.start_time, beat.end_time))
        for (start, a, _), (end, b, _) in zip(samples, samples[1:]):
            assert not blocked_intervals(a, b, start, end, .9, vehicles, CrowdSolverConfig())
    final, _ = performances['visitor'].sample(timeline.total_duration_s)
    assert all(math.dist(final, vehicle.position(timeline.total_duration_s))
               >= vehicle.radius + 1.2 + envelopes.buffer_m for vehicle in vehicles)


def test_stale_generated_route_cannot_teleport_actor():
    from timeline_resolver import ResolvedBeat
    beat = ResolvedBeat('move', 'person', 'run_to', 0., 2., 2., {
        'auto_crowd_movement': 'move',
        'brief_path': {'points': [[3, 0, 0], [5, 0, 0]]},
    })
    performance, error = MotionSynthesizer({'assets': [], 'clips': {'run': {}}}).synthesize_actor(
        'person', {'position': [0, 0, 0]}, [beat], {}, {}, 2.)
    assert performance is None
    assert error.error_code == 'PLANNED_ROUTE_DISCONTINUITY'


@pytest.mark.parametrize('asset_id,drives', [
    pytest.param('cars.car1', False, id='compact'),
    pytest.param('cars.car2', False, id='larger'),
    pytest.param('cars.car1', True, id='moving'),
])
def test_real_baked_exit_run_and_pose_pass_collision_validation(tmp_path, asset_id, drives):
    from compiler import ShotCompiler
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
    plan = ShotPlan(setup=[
        SetupAction('place', 'host', asset_id, position=[-8, 0, 0] if drives else [0, 0, 0]),
        SetupAction('place', 'visitor', 'character.male', at_reference='host:driver_seat'),
        SetupAction('place', 'partner', 'character.female', position=[8, 0, 0]),
    ], beats=[
        Beat('park', 'drive_to' if drives else 'wait', 'host',
             {'target': [0, 0, 0]} if drives else {}, TimingCondition('at', 0),
             TimingCondition('for_duration', 2 if drives else 8)),
        Beat('leave', 'exit_vehicle', 'visitor', {'vehicle': 'host'},
             TimingCondition('after', 'park.finished') if drives else TimingCondition('at', 0),
             TimingCondition('for_duration', 2)),
        Beat('arrive', 'run_to', 'visitor', {'target': 'partner'},
             TimingCondition('after', 'leave.finished'), TimingCondition('for_duration', 4)),
        Beat('pose', 'dance', 'visitor', {'style': 'groove'},
             TimingCondition('after', 'arrive.finished'), TimingCondition('for_duration', 2)),
    ])
    ok, report = ShotCompiler(ROOT).compile(
        plan, tmp_path / 'shot' / 'shot.usda', scene_path, validate=True)
    assert ok, json.dumps(report, indent=2)
    assert report['validation']['collision_passed']
