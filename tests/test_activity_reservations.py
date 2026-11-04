"""Action space stays reserved even when its actors also have ground movements."""
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'shot_library'))

from crowd.activities import activity_areas, first_activity_conflict
from crowd.config import CrowdSolverConfig
from crowd.movements import route_movements
from crowd.st_planner import Reservation, blocked_intervals
from crowd_spacing import ActorEnvelopes
from crowd.conformance import check_conformance
from motion_synthesis import ActorPerformance, Keyframe
from plan_model import Beat, ShotPlan, TimingCondition
from timeline_resolver import ResolvedBeat, TimelineResolver


class Scene:
    def _collect_obstacles_at_time(self, *args):
        return []


def trajectories(plan, states, resolver):
    timeline, error = resolver.resolve(plan, states)
    assert error is None
    tracks = {actor: [(0, *state['position'])] for actor, state in states.items()}
    by_id = {b.beat_id: b for b in plan.beats}
    for beat in sorted(timeline.beats, key=lambda b: (b.start_time, b.end_time)):
        if beat.beat_id in by_id:
            resolver._update_provisional_trajectory(beat.actor_id, beat.start_time, beat.end_time,
                                                   by_id[beat.beat_id], tracks)
    return timeline, tracks


def shared_action(function, walker_start, goal, scale=1.):
    slots = {'a': [-scale, 0, 0], 'b': [scale, 0, 0]}
    states = {actor: {'position': [p[0], 0, -3 * scale]} for actor, p in slots.items()}
    states['walker'] = {'position': [0, 0, -4 * scale]}
    beats = [Beat('approach_' + a, 'walk_to', a, {'target': p}, TimingCondition('at', 0),
                  TimingCondition('for_duration', 2)) for a, p in slots.items()]
    beats.extend(Beat('action_' + a, function, a,
                      {'group_id': 'shared_action', 'participants': list(slots),
                       'interaction_positions': slots, 'auto_interaction': True,
                       'target': other}, TimingCondition('at', 3), TimingCondition('for_duration', 17))
                 for a, other in [('a', 'b'), ('b', 'a')])
    beats.append(Beat('cross', 'walk_to', 'walker', {'target': goal},
                      TimingCondition('at', walker_start), TimingCondition('for_duration', 4)))
    beats.extend(Beat('depart_' + a, 'walk_to', a, {'target': [p[0] * 5, 0, 0]},
                      TimingCondition('at', 22), TimingCondition('for_duration', 3)) for a, p in slots.items())
    env = ActorEnvelopes({a: .2 * scale for a in states},
                         {'action_a': .35 * scale, 'action_b': .35 * scale},
                         buffer_m=.12)
    return ShotPlan(beats=beats), states, env


@pytest.mark.parametrize('function', ['attack', 'argue'])
@pytest.mark.parametrize('scale', [.75, 1., 1.5])
def test_route_goes_around_shared_action_even_after_actors_earlier_departure(function, scale):
    plan, states, env = shared_action(function, 4, [0, 0, 4 * scale], scale)
    original = plan.to_dict()
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, Scene(), {}, env)
    assert error is None, error
    assert plan.to_dict() == original
    timeline, tracks = trajectories(routed, states, resolver)
    areas = [a for a in activity_areas(timeline, tracks, env, resolver._sample_traj)
             if a.members == frozenset(['a', 'b'])]
    assert areas
    # The unrelated walker cannot cross the space *between* the participants,
    # even though the two individual body discs leave a gap there.
    points = tracks['walker']
    assert max(abs(p[1]) for p in points) > scale
    for area in areas:
        for left, right in zip(points, points[1:]):
            assert not blocked_intervals(left[1:], right[1:], left[0], right[0], env.body['walker'],
                                         [area.reservation()], CrowdSolverConfig())


def test_route_can_use_the_gap_after_shared_action_finishes():
    plan, states, env = shared_action('attack', 21, [0, 0, 4])
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, Scene(), {}, env)
    assert error is None, error
    _, tracks = trajectories(routed, states, resolver)
    assert all(abs(p[1]) < 1e-7 for p in tracks['walker'])


def test_arrival_does_not_stop_inside_an_active_shared_action_area():
    plan, states, env = shared_action('argue', 4, [0, 0, 0])
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, Scene(), {}, env)
    assert error is None, error
    arrival = next(b for b in routed.beats if b.beat_id == 'cross').params['arrival_position']
    # Allocation must reserve the action even though both participants also
    # have earlier and later movements in this same plan.
    assert math.hypot(arrival[0], arrival[2]) + 1e-7 >= 1.35 + .2 + env.buffer_m


def test_passing_actor_keeps_timed_motion_instead_of_reserving_whole_crossing_area():
    states = {'passer': {'position': [-3, 0, 0]}}
    plan = ShotPlan(beats=[Beat('pass', 'walk_to', 'passer', {'target': [3, 0, 0]},
                               TimingCondition('at', 0), TimingCondition('for_duration', 4))])
    resolver = TimelineResolver()
    timeline, tracks = trajectories(plan, states, resolver)
    areas = activity_areas(timeline, tracks, ActorEnvelopes({'passer': .3}, {}), resolver._sample_traj)
    assert not any(a.overlaps(0, 4) for a in areas)
    assert all(a.position == (3., 0., 0.) for a in areas)


def test_action_reservation_is_time_scoped_and_reports_all_participants():
    plan, states, env = shared_action('attack', 21, [0, 0, 4])
    resolver = TimelineResolver()
    timeline, tracks = trajectories(plan, states, resolver)
    area = next(a for a in activity_areas(timeline, tracks, env, resolver._sample_traj)
                if len(a.members) == 2)
    reservation = area.reservation()
    blockers = set()
    assert blocked_intervals([0, 0, -3], [0, 0, 3], 5, 9, .2, [reservation], CrowdSolverConfig(), blockers)
    assert blockers == {'a', 'b'}
    assert not blocked_intervals([0, 0, -3], [0, 0, 3], 21, 25, .2, [reservation], CrowdSolverConfig())


@pytest.mark.parametrize('function', ['attack', 'hug_or_kiss'])
def test_prompt_linked_contact_is_shared_before_interaction_scheduling(function):
    states = {'a': {'position': [-.65, 0, 0]}, 'b': {'position': [.65, 0, 0]}}
    beats = [Beat('contact_' + actor, function, actor,
                  {'group_id': 'contact', 'participants': ['a', 'b'],
                   'target': other}, TimingCondition('at', 2),
                  TimingCondition('for_duration', 3))
             for actor, other in [('a', 'b'), ('b', 'a')]]
    resolver = TimelineResolver()
    timeline, tracks = trajectories(ShotPlan(beats=beats), states, resolver)
    areas = activity_areas(timeline, tracks,
                           ActorEnvelopes({'a': .3, 'b': .3},
                                          {'contact_a': 1.1, 'contact_b': 1.1}),
                           resolver._sample_traj)
    contact_areas = [area for area in areas if area.overlaps(2, 5)]
    assert contact_areas and all(area.members == frozenset({'a', 'b'}) for area in contact_areas)
    assert first_activity_conflict(
        [Reservation('a', tracks['a'], .3), Reservation('b', tracks['b'], .3)],
        areas, 2, 5, CrowdSolverConfig()) is None
    assert first_activity_conflict(
        [Reservation('outsider', [(0, 0, 0, 0)], .3)],
        areas, 2, 5, CrowdSolverConfig()) is not None


def test_waiting_partner_inherits_prompt_linked_contact_window():
    states = {'a': {'position': [-.65, 0, 0]}, 'b': {'position': [.65, 0, 0]}}
    plan = ShotPlan(beats=[Beat('strike', 'attack', 'a',
                               {'group_id': 'contact', 'participants': ['a', 'b'],
                                'target': 'b'}, TimingCondition('at', 2),
                               TimingCondition('for_duration', 3))])
    resolver = TimelineResolver()
    timeline, tracks = trajectories(plan, states, resolver)
    areas = activity_areas(timeline, tracks,
                           ActorEnvelopes({'a': .3, 'b': .3}, {'strike': 1.1}),
                           resolver._sample_traj)
    assert all(area.members == frozenset({'a', 'b'})
               for area in areas if area.overlaps(2, 5))


@pytest.mark.parametrize('start,blocked', [(4, True), (21, False)])
def test_final_check_detects_walking_through_action_area_even_between_clear_bodies(start, blocked):
    plan, states, env = shared_action('attack', start, [0, 0, 4])
    timeline, _ = trajectories(plan, states, TimelineResolver())
    performances = {a: ActorPerformance(a, keyframes=[
        Keyframe(0, list(states[a]['position']), 0), Keyframe(2, [x, 0, 0], 0),
        Keyframe(22, [x, 0, 0], 0), Keyframe(25, [x * 5, 0, 0], 0)]) for a, x in [('a', -1), ('b', 1)]}
    performances['walker'] = ActorPerformance('walker', keyframes=[
        Keyframe(0, [0, 0, -4], 0), Keyframe(start, [0, 0, -4], 0),
        Keyframe(start + 4, [0, 0, 4], 0), Keyframe(25.5, [0, 0, 4], 0)])
    conflicts = check_conformance(plan, timeline, performances, env, 24)
    assert bool(conflicts) == blocked
    if blocked:
        assert conflicts[0]['occupied_area_members'] == ['a', 'b']
        assert 3 <= conflicts[0]['time_s'] < 20


def test_single_actor_performance_is_reserved_between_its_movements():
    states = {'performer': {'position': [0, 0, -3]}, 'walker': {'position': [-4, 0, 0]}}
    plan = ShotPlan(beats=[
        Beat('arrive', 'walk_to', 'performer', {'target': [0, 0, 0]},
             TimingCondition('at', 0), TimingCondition('for_duration', 2)),
        Beat('gesture', 'react', 'performer', {}, TimingCondition('at', 3), TimingCondition('for_duration', 17)),
        Beat('cross', 'walk_to', 'walker', {'target': [4, 0, 0]},
             TimingCondition('at', 4), TimingCondition('for_duration', 4)),
        Beat('leave', 'walk_to', 'performer', {'target': [0, 0, 5]},
             TimingCondition('at', 22), TimingCondition('for_duration', 3))])
    env = ActorEnvelopes({a: .2 for a in states}, {'gesture': 1.2})
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, Scene(), {}, env)
    assert error is None, error
    timeline, tracks = trajectories(routed, states, resolver)
    gesture = next(a for a in activity_areas(timeline, tracks, env, resolver._sample_traj)
                   if a.start == 3 and a.end == 20)
    for left, right in zip(tracks['walker'], tracks['walker'][1:]):
        assert not blocked_intervals(left[1:], right[1:], left[0], right[0], .2,
                                     [gesture.reservation()], CrowdSolverConfig())


def test_partial_idle_overlap_uses_pair_contract_only_during_approach():
    beats = [
        ResolvedBeat('idle_a', 'a', 'wait', 12., 15., 3.),
        ResolvedBeat('pair_approach_b', 'b', 'walk_to', 13., 15., 2.,
                     {'auto_interaction_approach': 'pair'}),
        ResolvedBeat('pair_a', 'a', 'turn_to_face', 15., 16., 1.,
                     {'group_id': 'pair', 'participants': ['a', 'b'],
                      'interaction_positions': {'a': [0., 0., 0.], 'b': [1., 0., 0.]}}),
    ]
    tracks = {'a': [(0., 0., 0., 0.)],
              'b': [(0., 3., 0., 0.), (15., 1., 0., 0.)]}
    env = ActorEnvelopes({'a': .3, 'b': .3, 'outsider': .3}, {'idle_a': 1.2})
    areas = activity_areas(SimpleNamespace(beats=beats), tracks, env,
                           TimelineResolver()._sample_traj)
    before = next(area for area in areas if area.start == 12. and area.end == 13.)
    during = next(area for area in areas if area.start == 13. and area.end == 15.)
    assert before.members == frozenset({'a'})
    assert during.members == frozenset({'a', 'b'})
    partner = Reservation('b', [(14., 0., 0., 0.)], .3)
    outsider = Reservation('outsider', [(14., 0., 0., 0.)], .3)
    assert first_activity_conflict([partner], [during], 14., 14.5,
                                   CrowdSolverConfig()) is None
    assert first_activity_conflict([outsider], [during], 14., 14.5,
                                   CrowdSolverConfig()) is not None
