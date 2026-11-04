"""Contract tests for the director/compiler/planner boundary and camera-only route."""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'collision'))

from director_brief import (build_digest, check_conformance, classify_gait, guard_symbolic,
                            merge_brief, resolve_path_for_actor, validate_brief)
from plan_model import Beat, ShotPlan, TimingCondition
from timeline_resolver import TimelineResolver
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from motion_synthesis import MotionSynthesizer


def brief():
    return {'version': 1, 'tracks': {'human_1': {'segments': [
        {'id': 's1', 't': [0, 5], 'action': 'walk_to', 'path': 'path_1', 'lock': 'locked'},
        {'id': 's2', 't': [5, 10], 'action': 'run_to', 'path': 'path_2', 'lock': 'preferred'},
    ]}}, 'paths': {
        'path_1': {'owner': 'human_1', 'points': [[0, 0, 0], [0, 0, -7]], 'speed_profile': 'constant'},
        'path_2': {'owner': 'human_1', 'points': [[0, 0, -7], [0, 0, -28]], 'speed_profile': 'constant'},
    }}


def test_preflight_and_qualitative_digest():
    data = brief()
    assert validate_brief(data) == []
    digest = build_digest(data)
    assert 'follows s1' in digest and 'LOCKED' in digest
    assert '-28' not in digest and 'points' not in digest and '[0' not in digest
    data['tracks']['human_1']['segments'][1]['t'] = [4, 10]
    assert 'OVERLAPPING_WINDOWS' in {x['code'] for x in validate_brief(data)}
    data = brief()
    data['paths']['path_1']['points'][-1] = [0, 0, -100]
    assert validate_brief(data)[0]['code'] == 'ACTION_SPEED_MISMATCH'


def test_shared_action_participants_and_exact_window():
    actors = ['human_1', 'human_2', 'human_3']
    data = {'version': 1, 'paths': {}, 'tracks': {
        actor: {'segments': [{'id': f'group_1_{index}', 'group_id': 'group_1',
                              'participants': actors, 't': [2, 5], 'action': 'converse',
                              'lock': 'locked'}]}
        for index, actor in enumerate(actors, 1)
    }}
    assert validate_brief(data) == []
    merged = merge_brief(data, ShotPlan())
    timeline, error = TimelineResolver().resolve(merged, {actor: {} for actor in actors})
    assert error is None
    shared = [beat for beat in timeline.beats if beat.params.get('group_id') == 'group_1']
    assert len(shared) == 3
    assert all((beat.start_time, beat.end_time, beat.params['participants']) == (2, 5, actors)
               for beat in shared)
    from director_solver import schedule_prompt_interactions
    scheduled, error = schedule_prompt_interactions(
        merged, {actor: {'position': [0, 0, 0]} for actor in actors},
        TimelineResolver(), None, {})
    assert error is None
    assert not any(beat.params.get('auto_interaction_approach') for beat in scheduled.beats)
    assert timeline.events['group_1.started'] == 2
    assert timeline.events['group_1.finished'] == 5
    data['tracks']['human_3']['segments'][0]['t'] = [3, 5]
    assert 'GROUP_MISMATCH' in {issue['code'] for issue in validate_brief(data)}


def test_social_catalog_links_selected_participants():
    catalog = FunctionCatalog({})
    selected = ['human_1', 'human_2', 'human_3']
    ok, _ = catalog.converse(selected)
    assert ok
    assert [beat.actor_id for beat in catalog.plan.beats] == selected
    assert {beat.params['group_id'] for beat in catalog.plan.beats} == {'prompt_interaction_1'}
    assert all(beat.params['participants'] == selected for beat in catalog.plan.beats)


def test_shared_action_stages_selected_participants():
    from director_solver import solve_shared_actions
    from motion_synthesis import ActorPerformance, Keyframe
    actors = ['human_1', 'human_2']
    data = {'tracks': {actor: {'segments': [{
        'id': f'group_1_{index}', 'group_id': 'group_1', 'participants': actors,
        't': [2, 5], 'action': 'converse'}]}
        for index, actor in enumerate(actors, 1)}}
    performances = {
        actor: ActorPerformance(actor, keyframes=[Keyframe(0, [x, 0, 0], 0),
                                                Keyframe(5, [x, 0, 0], 0)])
        for actor, x in [('human_1', -2), ('human_2', 2)]
    }
    class StraightRoute:
        def _collect_obstacles_at_time(self, *_args): return []
        def _plan_smooth_path(self, source, desired, _obstacles): return [source, desired]
    assert solve_shared_actions(data, performances, StraightRoute(), {}, 24) == []
    left, right = [performances[actor].sample(2)[0] for actor in actors]
    assert left[0] == pytest.approx(-.85)
    assert right[0] == pytest.approx(.85)
    assert all(perf.actions[0]['end_s'] == 2 for perf in performances.values())
    for track in data['tracks'].values(): track['segments'][0]['t'] = [0, 3]
    assert solve_shared_actions(data, performances, StraightRoute(), {}, 24)[0]['error_code'] == 'SHARED_ACTION_UNREACHABLE'
    ready = {actor: ActorPerformance(actor, keyframes=[Keyframe(0, [x, 0, 0], 0)])
             for actor, x in [('human_1', -.5), ('human_2', .5)]}
    assert solve_shared_actions(data, ready, StraightRoute(), {}, 24) == []


def test_prompt_interaction_uses_latest_route_arrival_and_waits():
    from director_solver import schedule_prompt_interactions
    actors = ['human_1', 'human_2']
    beats = [Beat('prior', 'wait', 'human_2', {}, TimingCondition('at', 0),
                  TimingCondition('for_duration', 2))]
    for actor in actors:
        beats.append(Beat('talk_' + actor, 'converse', actor,
                          {'group_id': 'meeting_1', 'participants': actors},
                          TimingCondition('at', 0), TimingCondition('for_duration', 3)))
    states = {'human_1': {'position': [-8, 0, 0]}, 'human_2': {'position': [2, 0, 0]}}
    class StraightRoute:
        def _collect_obstacles_at_time(self, *_args): return []
        def _plan_smooth_path(self, source, destination, _obstacles): return [source, destination]
    resolver = TimelineResolver()
    merged = merge_brief({'version': 1, 'tracks': {}, 'paths': {}}, ShotPlan(beats=beats))
    assert [beat.beat_id for beat in merged.beats] == [beat.beat_id for beat in beats]
    scheduled, error = schedule_prompt_interactions(merged, states,
                                                     resolver, StraightRoute(), {})
    assert error is None
    timeline, error = resolver.resolve(scheduled, states)
    assert error is None
    talks = [beat for beat in timeline.beats if beat.function == 'converse']
    assert len(talks) == 2 and talks[0].start_time == pytest.approx(talks[1].start_time)
    arrivals = {beat.actor_id: beat.end_time for beat in timeline.beats
                if beat.params.get('auto_interaction_approach') == 'meeting_1'}
    assert arrivals['human_1'] < arrivals['human_2']
    assert talks[0].start_time == pytest.approx(arrivals['human_2'])
    assert any(beat.is_idle_fill and beat.actor_id == 'human_1' and
               beat.start_time == pytest.approx(arrivals['human_1']) and
               beat.end_time == pytest.approx(arrivals['human_2']) for beat in timeline.beats)


def test_prompt_meeting_compiles_to_one_shared_start(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    actors = ['human_1', 'human_2']
    plan = ShotPlan(
        setup=[SetupAction('place', actor, 'character.human', label=actor,
                           position=[x, 0, 0])
               for actor, x in zip(actors, [-8, 2])],
        beats=[Beat('prior', 'wait', 'human_2', {}, TimingCondition('at', 0),
                    TimingCondition('for_duration', 2))] + [
            Beat('talk_' + actor, 'converse', actor,
                 {'group_id': 'meeting_1', 'participants': actors},
                 TimingCondition('at', 0), TimingCondition('for_duration', 3))
            for actor in actors])
    ok, report = ShotCompiler(ROOT).compile(plan, tmp_path / 'shot.usda', validate=False)
    assert ok, report
    timeline = json.loads((tmp_path / 'resolved_timeline.json').read_text())
    talks = [beat for beat in timeline['beats'] if beat['function'] == 'converse']
    assert len(talks) == 2
    assert talks[0]['start_time'] == talks[1]['start_time']
    arrivals = {beat['actor_id']: beat['end_time'] for beat in timeline['beats']
                if beat['params'].get('auto_interaction_approach') == 'meeting_1'}
    assert arrivals['human_1'] < arrivals['human_2']
    assert talks[0]['start_time'] == arrivals['human_2']


def test_prompt_meeting_uses_each_route_length():
    from director_solver import schedule_prompt_interactions
    actors = ['human_1', 'human_2']
    plan = ShotPlan(beats=[
        Beat('talk_' + actor, 'converse', actor,
             {'group_id': 'meeting_1', 'participants': actors},
             TimingCondition('at', 0), TimingCondition('for_duration', 3))
        for actor in actors])
    states = {'human_1': {'position': [-5, 0, 0]}, 'human_2': {'position': [5, 0, 0]}}

    class DetourRoute:
        def _collect_obstacles_at_time(self, *_args): return []

        def _plan_smooth_path(self, source, destination, _obstacles):
            if source[0] > 0:
                return [source, [source[0], source[1], 4], destination]
            return [source, destination]

    resolver = TimelineResolver()
    scheduled, error = schedule_prompt_interactions(plan, states, resolver, DetourRoute(), {})
    assert error is None
    timeline, error = resolver.resolve(scheduled, states)
    assert error is None
    approaches = {b.actor_id: b for b in timeline.beats
                  if b.params.get('auto_interaction_approach') == 'meeting_1'}
    assert approaches['human_2'].duration > approaches['human_1'].duration
    starts = {b.start_time for b in timeline.beats if b.function == 'converse'}
    assert len(starts) == 1
    assert starts.pop() == pytest.approx(approaches['human_2'].end_time)


def test_prompt_pair_contact_routes_both_participants():
    from director_solver import schedule_prompt_interactions
    plan = ShotPlan(beats=[Beat('handshake', 'shake_hands', 'human_1',
                                {'target': 'human_2'}, TimingCondition('at', 0))])
    states = {'human_1': {'position': [-3, 0, 0]}, 'human_2': {'position': [3, 0, 0]}}

    class StraightRoute:
        def _collect_obstacles_at_time(self, *_args): return []
        def _plan_smooth_path(self, source, destination, _obstacles): return [source, destination]

    resolver = TimelineResolver()
    scheduled, error = schedule_prompt_interactions(plan, states, resolver, StraightRoute(), {})
    assert error is None
    timeline, error = resolver.resolve(scheduled, states)
    assert error is None
    approaches = [b for b in timeline.beats if b.params.get('auto_interaction_approach')]
    assert {b.actor_id for b in approaches} == set(states)
    handshake = next(b for b in timeline.beats if b.beat_id == 'handshake')
    assert handshake.start_time == pytest.approx(max(b.end_time for b in approaches))
    assert handshake.params['participants'] == ['human_1', 'human_2']


@pytest.mark.parametrize('args', [{'target': [1, 2, 3]}, {'duration_s': 5}, {'for_duration': '5'},
                                  {'target': 'pos(1, 0, 2)'}, {'at_time': 'short'}, {'speed': 1.2}])
def test_numeric_guard(args):
    with pytest.raises(ValueError):
        guard_symbolic(args)


def test_schema_and_external_guard():
    planner = LLMShotPlanner(FunctionCatalog({}))
    planner.camera_mode = 'manual'
    schemas = planner.export_tool_schemas()
    assert 'camera_frame' not in [t['function']['name'] for t in schemas]
    assert 'follow_path' in [t['function']['name'] for t in schemas]
    for tool in schemas:
        props = tool['function']['parameters']['properties']
        assert 'position' not in props and 'facing_deg' not in props
        assert all(p['type'] not in ('number', 'integer') for p in props.values())
    ok, _, _ = planner.plan_from_llm_json({'calls': [
        {'fn': 'perform', 'actor': 'human_1', 'action': 'wait', 'during': 's1'},
        {'function': 'walk_to', 'args': {'actor': 'human_1', 'target': [0, 0, 0]}}
    ]})
    assert not ok  # A valid first call cannot hide an invalid second call.


def test_locks_preferred_choices_and_exact_events():
    data = brief()
    original = copy.deepcopy(data)
    plan = ShotPlan(beats=[Beat('replacement', 'run_to', 'human_1', {'during': 's1', 'action': 'run_to'}),
                          Beat('react', 'wait', 'human_2', {}, TimingCondition('after', 'human_1.s2'))])
    merged = merge_brief(data, plan)
    timeline, error = TimelineResolver().resolve(merged, {'human_1': {}, 'human_2': {}})
    assert error is None
    s1, s2 = [b for b in timeline.beats if b.params.get('brief_segment')]
    assert (s1.function, s1.start_time, s1.end_time) == ('walk_to', 0, 5)
    assert (s2.start_time, s2.end_time) == (5, 10)
    assert timeline.events['human_1.s2'] == 10
    assert next(b for b in timeline.beats if b.beat_id == 'react').start_time >= 10
    assert data == original
    bad = ShotPlan(beats=[Beat('x', 'attack', 'human_1', {'during': 's2', 'action': 'attack'})])
    with pytest.raises(ValueError, match='Preferred action'):
        merge_brief(data, bad)


def test_authored_path_playback_and_stride():
    data = brief()
    plan = merge_brief(data, ShotPlan())
    states = {'human_1': {'position': [0, 0, 0]}}
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    synth = MotionSynthesizer({'clips': {'walk': {}, 'run': {}}})
    perf, error = synth.synthesize_actor('human_1', states['human_1'], timeline.actor_schedules['human_1'], {}, {}, timeline.total_duration_s)
    assert error is None
    assert perf.sample(2.5)[0] == pytest.approx([0, 0, -3.5])
    assert perf.sample(7.5)[0] == pytest.approx([0, 0, -17.5])
    assert perf.actions[0]['clip'] == 'walk' and perf.actions[1]['clip'] == 'run'
    assert perf.actions[1]['start_s'] == 5
    assert perf.actions[1]['speed'] == 1
    assert check_conformance(data, {'human_1': perf}) == []
    perf.keyframes[50].position[0] += 10
    assert check_conformance(data, {'human_1': perf})[0]['code'] == 'PATH_CONFORMANCE'


def test_gait_and_path_sampling():
    assert classify_gait(7, 5) == 'walk'
    assert classify_gait(21, 5) == 'run'
    assert classify_gait(100, 5, 'vehicle') == 'fast'
    samples = resolve_path_for_actor(brief()['paths']['path_1'], (3, 8))
    assert samples[0][0] == 3 and samples[-1][0] == 8
    assert samples[-1][1] == [0, 0, -7]


def test_camera_only_preserves_actors_and_determinism(tmp_path, monkeypatch):
    from pxr import Usd, UsdGeom
    from camera_workflow import bake_manual_cameras, save_fingerprints, change_status
    def forbidden(*args, **kwargs): raise AssertionError("Camera-only route reached the planner")
    monkeypatch.setattr(LLMShotPlanner, 'plan_from_prompt', forbidden)
    monkeypatch.setattr(LLMShotPlanner, '_plan_with_llm_api', forbidden)
    stage = Usd.Stage.CreateNew(str(tmp_path / 'shot.usda'))
    hero = UsdGeom.Xform.Define(stage, '/World/Hero')
    hero.GetPrim().SetCustomDataByKey('actor_id', 'human_1')
    hero.AddTranslateOp().Set((0, 0, 0))
    stage.GetRootLayer().Save()
    (tmp_path / 'shot.json').write_text(json.dumps({'fps': 24, 'duration_s': 1, 'prompt': 'hello'}))
    for filename in ['director_brief.json', 'shot_plan.json']:
        (tmp_path / filename).write_text('{}')
    rig = {'version': 1, 'active_camera': 'cam_1', 'cameras': {'cam_1': {
        'path': {'waypoints': [{'p': [0, 2, 10]}, {'p': [0, 2, 8]}], 'time_range': [0, 1], 'speed': {'profile': 'constant'}},
        'aim': {'segments': [{'t': [0, 1], 'mode': 'look_at', 'targets': [{'actor': 'human_1'}]}]},
        'lens': {'focal_mm': 35}}}}
    (tmp_path / 'camera_rig.json').write_text(json.dumps(rig))
    save_fingerprints(tmp_path)
    immutable = {name: (tmp_path / name).read_bytes() for name in ['shot.usda', 'shot_plan.json', 'director_brief.json']}
    result = bake_manual_cameras(tmp_path)
    assert result['camera'] == '/World/Cameras/cam_1'
    first = (tmp_path / 'cam_cam_1.usda').read_bytes()
    bake_manual_cameras(tmp_path)
    assert (tmp_path / 'cam_cam_1.usda').read_bytes() == first
    assert immutable == {name: (tmp_path / name).read_bytes() for name in immutable}
    assert change_status(tmp_path) == 'up_to_date'
    rig['cameras']['cam_1']['lens']['focal_mm'] = 50
    (tmp_path / 'camera_rig.json').write_text(json.dumps(rig))
    assert change_status(tmp_path) == 'camera_only'
    assert change_status(tmp_path, 'new prose') == 'generate'
    (tmp_path / 'director_brief.json').write_text('{"version": 1}')
    with pytest.raises(ValueError, match='Actor inputs changed'):
        bake_manual_cameras(tmp_path)


def test_compile_brief_preserves_authoring_files(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    data = brief()
    # A short real skeletal bake exercises clip windows, binding, and guide authoring.
    data['tracks']['human_1']['segments'] = data['tracks']['human_1']['segments'][:1]
    data['tracks']['human_1']['segments'][0]['t'] = [0, 1]
    data['paths'] = {'path_1': {'owner': 'human_1', 'points': [[0, 0, 0], [0, 0, -1.4]], 'speed_profile': 'constant'}}
    authored = json.dumps(data, indent=2).encode()
    (tmp_path / 'director_brief.json').write_bytes(authored)
    plan = ShotPlan(setup=[SetupAction('place', 'human_1', asset_id='character.human', label='Hero')])
    ok, report = ShotCompiler(project_root=ROOT).compile(plan, tmp_path / 'shot.usda', validate=False)
    assert ok, json.dumps(report.get("validation", report), indent=2)
    assert (tmp_path / 'director_brief.json').read_bytes() == authored
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.Open(str(tmp_path / 'shot.usda'))
    curve = UsdGeom.BasisCurves(stage.GetPrimAtPath('/World/Paths/path_1'))
    assert curve.GetPurposeAttr().Get() == 'guide'
    hero = UsdGeom.Xformable(stage.GetPrimAtPath('/World/Hero'))
    assert list(hero.ComputeLocalToWorldTransform(12).ExtractTranslation()) == pytest.approx([0, 0, -.7], abs=.02)


def test_symbolic_dsl_rejects_hidden_numeric_gaps():
    with pytest.raises(ValueError):
        guard_symbolic({'target': 'beside(human_1, left, 1.5)'})
    guard_symbolic({'target': 'beside(human_1, left, near)', 'after': 'human_1.s2'})


def test_vehicle_corner_lint():
    from director_brief import check_turning_radius
    path = {'points': [[0, 0, 0], [0, 0, 1], [1, 0, 1]]}
    warnings = check_turning_radius(path, {'wheelbase_m': 2.7, 'max_steering_deg': 35})
    assert warnings and warnings[0]['waypoint'] == 1
    assert warnings[0]['minimum_radius_m'] > 3


def test_unassigned_drawn_vehicle_path_does_not_block_queue(tmp_path):
    from pxr import Usd, UsdGeom
    from brief_tools import inspect_brief

    scene_path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, '/World')
    stage.GetRootLayer().Save()

    route = {'owner': 'Car1', 'actor_type': 'vehicle', 'asset_id': 'cars.generic',
             'points': [[0, 0, 0], [0, 0, 4], [4, 0, 4]], 'speed_profile': 'constant'}
    data = {'version': 1, 'tracks': {}, 'paths': {'route': route}}
    manifest = {'assets': [{'id': 'cars.generic', 'wheelbase_m': 2.7, 'max_steering_deg': 35}],
                'clips': {}}
    brief_path = tmp_path / 'director_brief.json'
    brief_path.write_text(json.dumps(data))

    guide = inspect_brief(tmp_path, manifest, scene_path)
    assert guide['lint'] == []
    assert 'Available drawn route route' in guide['digest']

    data['tracks'] = {'Car1': {'asset_id': 'cars.generic', 'segments': [
        {'id': 'drive', 't': [0, 1], 'action': 'drive_to', 'path': 'route', 'lock': 'locked'}]}}
    brief_path.write_text(json.dumps(data))
    assigned = inspect_brief(tmp_path, manifest, scene_path)
    assert any(issue['code'] == 'TURNING_RADIUS' for issue in assigned['lint'])


@pytest.mark.parametrize('actor_type, expected_action, speed', [
    ('vehicle', 'drive_to', 8.0), ('human', 'walk_to', 1.4),
])
def test_llm_can_select_unassigned_drawn_route_without_timeline_segment(actor_type, expected_action, speed):
    route = {'owner': 'Traveler', 'actor_type': actor_type,
             'points': [[0, 0, 0], [0, 0, 14]], 'speed_profile': 'constant'}
    user_brief = {'version': 1, 'tracks': {}, 'paths': {'route': route}}
    planner = LLMShotPlanner(FunctionCatalog({'assets': [], 'clips': {}}, {}))
    planner.brief = user_brief
    ok, plan, _ = planner.plan_from_llm_json(
        {'calls': [{'function': 'follow_path', 'args': {'actor': 'Traveler', 'path_id': 'route'}}]},
        prompt='Travel along the drawn route')
    assert ok
    merged = merge_brief(user_brief, plan)
    assert len(merged.beats) == 1
    assert merged.beats[0].function == expected_action
    assert merged.beats[0].params['brief_path'] == route
    assert merged.beats[0].duration_condition.value == pytest.approx(14 / speed)


def test_prompt_only_generates_its_actions_without_route_or_timeline():
    plan = ShotPlan(prompt='Traveler greets a friend', beats=[Beat('greet', 'greet', 'Traveler', {})])
    merged = merge_brief({'version': 1, 'tracks': {}, 'paths': {}}, plan)
    assert [beat.beat_id for beat in merged.beats] == ['greet']


def test_prompt_and_owned_drawn_path_both_reach_timeline():
    data = {'version': 1, 'tracks': {}, 'paths': {
        'route': {'owner': 'Traveler', 'actor_type': 'human',
                  'points': [[0, 0, 0], [0, 0, -7]], 'speed_profile': 'constant'}}}
    plan = ShotPlan(prompt='Traveler greets a friend', beats=[Beat('greet', 'greet', 'Traveler', {})])
    merged = merge_brief(data, plan)
    timeline, error = TimelineResolver().resolve(merged, {'Traveler': {'position': [0, 0, 0]}})
    assert error is None
    route = next(beat for beat in timeline.beats if beat.params.get('brief_path'))
    greeting = next(beat for beat in timeline.beats if beat.beat_id == 'greet')
    assert route.params['brief_path']['points'][-1] == [0, 0, -7]
    assert greeting.start_time >= route.end_time


def test_explicit_timeline_runs_alone_or_with_prompt_action_after_it():
    data = {'version': 1, 'paths': {'background_route': {
        'owner': 'Background', 'points': [[0, 0, 0], [0, 0, 7]]}},
        'tracks': {'Traveler': {'segments': [
        {'id': 'hold', 't': [0, 3], 'action': 'wait', 'lock': 'locked'}]}}}
    alone = merge_brief(data, ShotPlan())
    assert [beat.beat_id for beat in alone.beats] == ['Traveler.hold']
    combined = merge_brief(data, ShotPlan(
        prompt='Traveler greets a friend', beats=[Beat('greet', 'greet', 'Traveler', {})]))
    timeline, error = TimelineResolver().resolve(combined, {'Traveler': {'position': [0, 0, 0]}})
    assert error is None
    hold = next(beat for beat in timeline.beats if beat.beat_id == 'Traveler.hold')
    greeting = next(beat for beat in timeline.beats if beat.beat_id == 'greet')
    assert (hold.start_time, hold.end_time) == (0, 3)
    assert greeting.start_time >= hold.end_time


def test_empty_llm_calls_allowed_only_when_no_prompt_and_brief_has_actions():
    catalog = FunctionCatalog({'assets': [], 'clips': {}}, {})
    planner = LLMShotPlanner(catalog)
    planner.brief = {'version': 1, 'tracks': {'Traveler': {'segments': [
        {'id': 'hold', 't': [0, 3], 'action': 'wait'}]}}, 'paths': {}}
    assert planner.plan_from_llm_json({'calls': []}, prompt='')[0]
    assert not planner.plan_from_llm_json({'calls': []}, prompt='Traveler greets')[0]
    planner.brief = {'version': 1, 'tracks': {}, 'paths': {
        'route': {'owner': 'Traveler', 'points': [[0, 0, 0], [0, 0, 7]]}}}
    assert planner.plan_from_llm_json({'calls': []}, prompt='')[0]
    assert not planner.plan_from_llm_json({'calls': []}, prompt='Traveler drives')[0]
