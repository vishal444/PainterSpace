"""Crowd geometry properties across cast sizes and dynamic bottlenecks."""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))

from crowd.config import CrowdSolverConfig
from crowd.st_planner import Reservation, blocked_intervals, plan_route
from crowd.arena_layout import assign_arenas
from director_solver import _approach_beats
from director_brief import resolve_path_for_actor
from director_solver import schedule_prompt_interactions
from crowd_spacing import ActorEnvelopes, _beat_clip_names
from plan_model import Beat, ShotPlan, TimingCondition
from timeline_resolver import TimelineResolver
from crowd.movements import route_movements
from crowd.conformance import check_conformance
from motion_synthesis import ActorPerformance, Keyframe


def check_route(result, radius, reservations, config):
    assert result.status == 'ok', result
    for a, b in zip(result.points, result.points[1:]):
        assert not blocked_intervals(a[1:], b[1:], a[0], b[0], radius, reservations, config)


def test_waits_inside_corridor_for_actual_departure():
    config = CrowdSolverConfig(cell_size_m=.4, max_delay_s=6)
    blocker = Reservation('dancer', [(0, 0, 0, 0), (3, 0, 0, 0), (4, 0, 0, 3)], .25)
    route = plan_route([-2, 0, 0], [2, 0, 0], 0, 1, .25, [blocker],
                       lambda ps: all(abs(p[2]) < .01 for p in ps), (-3, 3, -.8, .8), config)
    check_route(route, .25, [blocker], config)
    assert route.points[-1][0] > 4
    assert any(math.dist(a[1:], b[1:]) < 1e-7 and b[0] > a[0]
               for a, b in zip(route.points, route.points[1:]))
    # Beat serialization must retain every wait and route endpoint.
    beats = _approach_beats('pair', 'traveler', route.points)
    assert any(b.function == 'wait' for b in beats)
    for beat in beats:
        if beat.function != 'walk_to': continue
        samples = resolve_path_for_actor(beat.params['brief_path'],
                                         [beat.start_condition.value,
                                          beat.start_condition.value + beat.duration_condition.value])
        for a, b in zip(samples, samples[1:]):
            assert not blocked_intervals(a[1], b[1], a[0], b[0], .25, [blocker], config)


def test_swept_edges_detect_swaps_and_fast_crossings():
    config = CrowdSolverConfig()
    crossing = Reservation('crossing', [(0, 0, 0, -10), (1, 0, 0, 10)], .1)
    assert blocked_intervals([-1, 0, 0], [1, 0, 0], 0, 1, .1, [crossing], config)
    swap = Reservation('swap', [(0, 1, 0, 0), (1, -1, 0, 0)], .1)
    assert blocked_intervals([-1, 0, 0], [1, 0, 0], 0, 1, .1, [swap], config)


def test_finished_action_does_not_remove_stationary_actor():
    actor = Reservation('still_here', [(0, 0, 0, 0), (1, 0, 0, 0)], .25,
                        [(0, 1, 1.2)])
    config = CrowdSolverConfig()
    assert blocked_intervals([-1, 0, 0], [1, 0, 0], 10, 11, .25, [actor], config)
    assert not blocked_intervals([1.1, 0, 0], [1.1, 0, 0], 10, 11, .25, [actor], config)
    assert blocked_intervals([.8, 0, 0], [.8, 0, 0], 0, 1, .25, [actor], config)


def test_goal_is_reserved_until_partner_arrives():
    config = CrowdSolverConfig()
    crossing = Reservation('late_arrival', [(0, 0, 0, 5), (2, 0, 0, 0), (3, 0, 0, 5)], .25)
    route = plan_route([-1, 0, 0], [0, 0, 0], 0, 1, .25, [crossing],
                       lambda ps: True, (-3, 3, -3, 3), config, hold_s=3)
    check_route(route, .25, [crossing], config)
    arrival = route.points[-1][0]
    assert not blocked_intervals([0, 0, 0], [0, 0, 0], arrival, arrival + 3, .25, [crossing], config)


@pytest.mark.parametrize('hold_radius', [.8, 1.2])
def test_goal_hold_reserves_following_action_reach_not_only_walking_body(hold_radius):
    config = CrowdSolverConfig(buffer_m=0, max_delay_s=0)
    # The passer is clear of a walking capsule, but crosses the space needed
    # for the gesture performed after arrival, midway through the goal hold.
    passer = Reservation('passer', [(0, 4, 0, 1), (2, 0, 0, .6), (4, 4, 0, 1)], .2)
    args = ([-1, 0, 0], [0, 0, 0], 0, 1, .2, [passer], lambda points: True,
            (-2, 2, -2, 2), config)
    assert plan_route(*args, hold_until=4).points is not None
    assert plan_route(*args, hold_until=4, hold_radius=hold_radius).points is None


def test_long_goal_hold_does_not_extend_the_arrival_delay_budget():
    config = CrowdSolverConfig(max_delay_s=0)
    crossing = Reservation('later_crossing', [(0, 0, 0, 4), (2, 0, 0, 0), (3, 0, 0, 4)], .25)
    route = plan_route([-1, 0, 0], [0, 0, 0], 0, 1, .25, [crossing],
                       lambda ps: True, (-2, 2, -2, 2), config, hold_until=20)
    assert route.points is None


def test_search_failure_is_not_an_unreachability_proof():
    config = CrowdSolverConfig(max_expansions=1)
    obstacle = Reservation('locked', [(0, 0, 0, 0)], 1)
    route = plan_route([-2, 0, 0], [2, 0, 0], 0, 1, .25, [obstacle],
                       lambda ps: True, (-3, 3, -3, 3), config)
    assert route.status == 'expansion_limit'
    assert route.points is None


@pytest.mark.parametrize('actor_count', [4, 10, 20, 40])
def test_joint_layout_is_separated_balanced_and_deterministic(actor_count):
    groups, candidates = [], {}
    for i in range(actor_count // 2):
        key = f'pair_{i}'
        groups.append({'key': key, 'sources': [[-5, 0, i * 3], [5, 0, i * 3]],
                       'preferred': [-5, 0, 0], 'radius': 1.1})
        candidates[key] = [{'name': f'spot_{j}', 'position': [0, 0, j * 3]}
                           for j in range(actor_count // 2)]
    result = assign_arenas(groups, candidates, [], lambda p, r: True)
    assert result == assign_arenas(groups, candidates, [], lambda p, r: True)
    assert result is not None
    positions = [s['position'] for s in result.values()]
    assert all(math.dist(a, b) >= 2.2 + .12 for i, a in enumerate(positions) for b in positions[i + 1:])
    assert all(result[g['key']]['position'][2] == g['sources'][0][2] for g in groups)


def test_arena_layout_excludes_stationary_envelopes_and_reports_exhaustion():
    group = {'key': 'pair', 'sources': [[-2, 0, 0], [2, 0, 0]], 'preferred': [0, 0, 0], 'radius': 1}
    candidates = {'pair': [{'name': 'blocked', 'position': [0, 0, 0]},
                           {'name': 'clear', 'position': [0, 0, 4]}]}
    occupied = [([0, 0, 0], 1)]
    result = assign_arenas([group], candidates, occupied, lambda p, r: True)
    assert result['pair']['name'] == 'clear'
    assert assign_arenas([group], candidates, occupied, lambda p, r: False) is None


class StraightRoutes:
    def _collect_obstacles_at_time(self, *args): return []
    def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]


def test_shared_trigger_wave_stays_synchronized_and_survives_recompile():
    actors = {'a': {'position': [-4, 0, -3]}, 'b': {'position': [4, 0, -3]},
              'c': {'position': [-2, 0, 3]}, 'd': {'position': [2, 0, 3]},
              'trigger': {'position': [10, 0, 10]}}
    beats = [Beat('trigger', 'wait', 'trigger', {}, TimingCondition('at', 0),
                  TimingCondition('for_duration', 2))]
    for index, pair in enumerate((('a', 'b'), ('c', 'd'))):
        for actor, target in (pair, pair[::-1]):
            beats.append(Beat('stage_' + actor, 'turn_to_face', actor,
                              {'group_id': f'pair_{index}', 'participants': list(pair),
                               'target': target, 'spacing': 'combat'},
                              TimingCondition('after', 'trigger.finished')))
        beats.append(Beat('attack_' + pair[0], 'attack', pair[0], {'target': pair[1]},
                          TimingCondition('after', 'stage_' + pair[0] + '.finished')))
    resolver = TimelineResolver()
    env = ActorEnvelopes({a: .25 for a in actors}, {})
    scheduled, error = schedule_prompt_interactions(ShotPlan(beats=beats), actors, resolver,
                                                     StraightRoutes(), {}, envelopes=env)
    assert error is None
    stages = [b for b in scheduled.beats if b.beat_id.startswith('stage_')]
    assert len({b.start_condition.value for b in stages}) == 1
    assert stages[0].start_condition.value > 2
    timeline, error = resolver.resolve(scheduled, actors)
    assert error is None
    resolved = {b.beat_id: b for b in timeline.beats}
    assert resolved['attack_a'].start_time >= resolved['stage_a'].end_time
    again, error = schedule_prompt_interactions(scheduled, actors, resolver, StraightRoutes(), {}, envelopes=env)
    assert error is None
    assert [b.start_condition.value for b in again.beats if b.beat_id.startswith('stage_')] == [
        b.start_condition.value for b in stages]


def test_subframe_route_durations_are_not_stretched_by_resolver():
    beats = _approach_beats('pair', 'a', [(0, 0, 0, 0), (.05, .05, 0, 0),
                                        (.1, .05, 0, 0), (.15, .1, 0, 0)])
    timeline, error = TimelineResolver().resolve(ShotPlan(beats=beats), {'a': {'position': [0, 0, 0]}})
    assert error is None
    assert [(b.start_time, b.end_time) for b in timeline.beats if b.beat_id.startswith('pair')] == [
        (0, .05), (.05, .1), (.1, .15)]


@pytest.mark.parametrize('function,params,expected', [
    ('defend_or_block', {'style': 'dodge_back'}, ['dodging_back']),
    ('defend_or_block', {'style': 'block'}, ['boxing_block']),
    ('hit_reaction', {}, ['head_hit_during_fight']),
    ('react', {'style': 'happy'}, ['clapping']),
    ('argue', {}, ['pointing_forward', 'reaction', 'acknowledging']),
    ('custom_clip', {}, ['custom_clip']),
    ('wait', {'in_place_clip': 'custom_clip'}, ['custom_clip']),
])
def test_reach_uses_the_same_clip_choices_as_synthesis(function, params, expected):
    assert _beat_clip_names(Beat('beat', function, 'a', params), {'custom_clip': {}}) == expected


def test_ordinary_crossing_movements_are_reserved_and_keep_their_events():
    states = {'a': {'position': [-4, 0, 0]}, 'b': {'position': [0, 0, -4]}}
    original = ShotPlan(beats=[
        Beat('move_a', 'run_to', 'a', {'target': [4, 0, 0]}, TimingCondition('at', 0),
             TimingCondition('for_duration', 4)),
        Beat('move_b', 'run_to', 'b', {'target': [0, 0, 4]}, TimingCondition('at', 0),
             TimingCondition('for_duration', 4)),
        Beat('following', 'wait', 'b', {}, TimingCondition('after', 'move_a.finished'))])
    resolver = TimelineResolver()
    env = ActorEnvelopes({'a': .25, 'b': .25}, {})
    plan, error = route_movements(original, states, resolver, StraightRoutes(), {}, env)
    assert error is None
    assert original.beats[0].params.get('brief_path') is None
    timeline, error = resolver.resolve(plan, states)
    assert error is None
    paths = {a: [(0, *s['position'])] for a, s in states.items()}
    by_id = {b.beat_id: b for b in plan.beats}
    for b in sorted(timeline.beats, key=lambda b: (b.start_time, b.end_time)):
        if b.beat_id in by_id:
            resolver._update_provisional_trajectory(b.actor_id, b.start_time, b.end_time, by_id[b.beat_id], paths)
    a, b = (Reservation(actor, paths[actor], .25) for actor in ('a', 'b'))
    assert not blocked_intervals(a.position(0), a.position(0), 0, .01, .25, [b], CrowdSolverConfig())
    for t0, t1, p, q, radius in a.intervals(0, timeline.total_duration_s):
        assert not blocked_intervals(p, q, t0, t1, radius, [b], CrowdSolverConfig())
    assert {'move_a.finished', 'move_b.finished'} <= set(timeline.events)
    again, error = route_movements(plan, states, resolver, StraightRoutes(), {}, env)
    assert error is None
    assert plan.to_dict() == again.to_dict()


def test_authored_paths_and_fixed_windows_are_not_rerouted():
    states = {'a': {'position': [-2, 0, 0]}}
    plan = ShotPlan(beats=[Beat('move', 'walk_to', 'a',
        {'brief_path': {'points': [[-2, 0, 0], [2, 0, 0]]}, 'exact_window': [0, 4]},
        TimingCondition('at', 0), TimingCondition('for_duration', 4))])
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), {},
                                    ActorEnvelopes({'a': .25}, {}))
    assert error is None and routed.to_dict() == plan.to_dict()


@pytest.mark.parametrize('heights', [(0., .3), (.3, 0.), (1., 3.)])
def test_unroutable_level_transition_cannot_bypass_crowd_reservations(heights):
    start, end = heights
    states = {'traveler': {'position': [-2, start, 0]}}
    plan = ShotPlan(beats=[Beat('move', 'run_to', 'traveler',
        {'target': [2, end, 0]}, TimingCondition('at', 0),
        TimingCondition('for_duration', 4))])
    original = plan.to_dict()
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), {},
                                    ActorEnvelopes({'traveler': .25}, {}))
    assert routed is None
    assert error.error_code == 'CROWD_LEVEL_TRANSITION'
    assert error.beat_id == 'move' and error.actor_id == 'traveler'
    assert error.details['source_height_m'] == start
    assert error.details['destination_height_m'] == end
    assert plan.to_dict() == original


@pytest.mark.parametrize('sample_hz', [3, 7, 24, 48])
@pytest.mark.parametrize('profile', ['constant', 'ease_in_out'])
def test_path_sampling_preserves_checked_corners_continuously(sample_hz, profile):
    # A chord across the inside corner intersects this obstacle even though
    # both checked route edges have the required clearance.
    path = {'points': [[-1, 0, 0], [0, 0, 0], [0, 0, 1.3]],
            'speed_profile': profile}
    samples = resolve_path_for_actor(path, (0, 1.37), sample_hz=sample_hz)
    assert any(math.dist(pos, [0, 0, 0]) < 1e-12 for _, pos, _ in samples)
    blocker = Reservation('inside_corner', [(0, -.2, 0, .2)], .1)
    config = CrowdSolverConfig(buffer_m=0)
    for (t0, a, _), (t1, b, _) in zip(samples, samples[1:]):
        assert not blocked_intervals(a, b, t0, t1, .1, [blocker], config)


def test_prebake_check_detects_root_conflict_and_scopes_shared_exemption():
    plan = ShotPlan(beats=[Beat('route', 'walk_to', 'a', {'auto_crowd_movement': 'route'})])
    performances = {actor: ActorPerformance(actor_id=actor, keyframes=[Keyframe(0, [0, 0, 0], 0),
                                                                      Keyframe(1, [0, 0, 0], 0)])
                    for actor in ('a', 'b')}
    timeline, error = TimelineResolver().resolve(ShotPlan(beats=[
        Beat('exchange_a', 'turn_to_face', 'a', {'auto_interaction': True, 'participants': ['a', 'b']},
             TimingCondition('at', 0), TimingCondition('for_duration', 1))]), {'a': {'position': [0, 0, 0]}})
    assert error is None
    env = ActorEnvelopes({'a': .25, 'b': .25}, {})
    # The exemption expires at the shared window's end, even when both actors
    # subsequently remain at the same position in the idle tail.
    conflict = check_conformance(plan, timeline, performances, env, 24)
    assert conflict[0]['time_s'] >= 1
    timeline.total_duration_s = 1
    assert not check_conformance(plan, timeline, performances, env, 24)
    timeline.beats[0].params['auto_interaction'] = False
    assert check_conformance(plan, timeline, performances, env, 24)[0]['time_s'] == 0


@pytest.mark.parametrize('duration', [.01, .02, .04])
@pytest.mark.parametrize('sample_hz', [1, 24, 120])
def test_prebake_check_catches_crossings_between_sample_frames(duration, sample_hz):
    states = {'a': {'position': [-2, 0, 0]}, 'b': {'position': [2, 0, 0]}}
    plan = ShotPlan(beats=[Beat('idle', 'wait', 'a')])
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    performances = {
        a: ActorPerformance(a, keyframes=[Keyframe(0, p, 0),
                                          Keyframe(duration, [-p[0], p[1], p[2]], 0)])
        for a, p in ((a, s['position']) for a, s in states.items())}
    env = ActorEnvelopes({a: .2 for a in states}, {})
    conflicts = check_conformance(plan, timeline, performances, env, sample_hz)
    assert conflicts and 0 < conflicts[0]['time_s'] < duration
    assert conflicts[0]['distance_m'] < conflicts[0]['required_separation_m']
    # A vertical separation outside the standing-level contract stays clear.
    for k in performances['b'].keyframes:
        k.position[1] = 2
    assert not check_conformance(plan, timeline, performances, env, sample_hz)


def test_approach_clearance_protects_partner_bodies_and_unrelated_actors():
    from types import SimpleNamespace
    env = ActorEnvelopes({'a': .2, 'b': .2, 'observer': .2}, {'approach': 1.5})
    approach = SimpleNamespace(beat_id='approach', actor_id='a', function='walk_to',
                               start_time=0., end_time=1.,
                               params={'auto_interaction_approach': 'pair'})
    meeting = SimpleNamespace(beat_id='meeting', actor_id='a', function='turn_to_face',
                               start_time=1., end_time=2.,
                               params={'auto_interaction': True, 'group_id': 'pair',
                                       'participants': ['a', 'b']})
    timeline = SimpleNamespace(beats=[approach, meeting], total_duration_s=1.)
    performances = {a: ActorPerformance(a, keyframes=[Keyframe(0, [x, 0, 0], 0)])
                    for a, x in [('a', 0), ('b', 1.0), ('observer', 4)]}
    assert not check_conformance(ShotPlan(), timeline, performances, env, 24)
    performances['b'].keyframes[0].position[0] = .3
    assert check_conformance(ShotPlan(), timeline, performances, env, 24)
    performances['b'].keyframes[0].position[0] = 1.0
    performances['observer'].keyframes[0].position[0] = -1
    conflict = check_conformance(ShotPlan(), timeline, performances, env, 24)[0]
    assert {conflict['actor'], conflict['other_actor']} == {'a', 'observer'}


def test_continuous_check_observes_short_radius_windows_and_end_holds():
    from types import SimpleNamespace
    env = ActorEnvelopes({'a': .2, 'b': .2}, {'gesture': 1.})
    gesture = SimpleNamespace(beat_id='gesture', actor_id='a', function='gesture',
                               start_time=.01, end_time=.02, params={})
    timeline = SimpleNamespace(beats=[gesture], total_duration_s=1.)
    performances = {a: ActorPerformance(a, keyframes=[Keyframe(0, [x, 0, 0], 0)])
                    for a, x in [('a', 0), ('b', 1)]}
    conflict = check_conformance(ShotPlan(), timeline, performances, env, 1)[0]
    assert .01 <= conflict['time_s'] < .02
    timeline.beats = []
    performances['b'].keyframes.append(Keyframe(.5, [0, 0, 0], 0))
    assert check_conformance(ShotPlan(), timeline, performances, env, 1)


@pytest.mark.parametrize('start,duration,speed', [
    (1.00049, .00001, .00001), (12.12351, .00002, .0004), (2., .04, 1.)])
def test_action_serialization_preserves_valid_short_routes(start, duration, speed, tmp_path):
    import json
    sys.path.insert(0, str(ROOT / 'tools'))
    from bake_animation import bake

    performance = ActorPerformance('actor')
    performance.add_action('run', start, start + duration, speed=speed)
    action = json.loads(json.dumps(performance.actions))[0]
    assert action['end_s'] > action['start_s']
    assert action['start_s'] == start and action['end_s'] == start + duration
    assert action['speed'] == speed and action['speed'] > 0
    manifest_path = ROOT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    character = next(a for a in manifest['assets'] if a['id'] == 'character.human')
    actions = tmp_path / 'actions.json'
    actions.write_text(json.dumps({'actions': performance.actions}))
    output = tmp_path / 'animation.usda'
    frames, _ = bake(ROOT / character['path'], manifest_path, actions, output,
                     fps=24, duration=start + duration)
    assert frames > 0 and output.exists()


@pytest.mark.parametrize('function', ['walk_to', 'run_to', 'jog_to'])
@pytest.mark.parametrize('profile', ['constant', 'ease_in_out'])
def test_already_reached_path_becomes_a_valid_stationary_hold(function, profile):
    from motion_synthesis import MotionSynthesizer
    position = [2., 0., -3.]
    states = {'actor': {'position': position, 'facing_deg': 73.}}
    plan = ShotPlan(beats=[Beat('arrival', function, 'actor',
        {'brief_path': {'points': [position, position], 'speed_profile': profile}, 'action': function},
        TimingCondition('at', 0), TimingCondition('for_duration', 1))])
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    perf, error = MotionSynthesizer({'clips': {'walk': {}, 'run': {}, 'acknowledging': {}}}).synthesize_actor(
        'actor', states['actor'], timeline.actor_schedules['actor'], {}, {}, timeline.total_duration_s)
    assert error is None
    assert all(a['speed'] > 0 and a['end_s'] > a['start_s'] for a in perf.actions)
    assert perf.actions[0]['clip'] == 'acknowledging'
    assert all(k.position == position and k.heading_deg == 73. for k in perf.keyframes)
    assert timeline.events['arrival.finished'] == 1
