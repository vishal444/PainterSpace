"""Joint coordination must reconsider who yields, with continuous clearance."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'shot_library'))
from crowd.config import CrowdSolverConfig
from crowd.joint import PrefixReservation, ancestors, first_conflict, priority_search
from crowd.st_planner import Reservation, blocked_intervals, plan_route
from plan_model import StructuredError


@pytest.mark.parametrize('scale', [1., 1.5, 2.])
def test_root_route_failure_reorders_blocking_actor_instead_of_stopping(scale):
    config = CrowdSolverConfig(cell_size_m=.4 * scale)
    tasks = {'a': ([-2 * scale, 0, 1.6 * scale], [0, 0, 0]),
             'b': ([3.2 * scale, 0, 0], [-3.2 * scale, 0, 0])}
    def static(points):
        for p, q in zip(points, points[1:] or points):
            for i in range(101):
                x = (p[0] + (q[0] - p[0]) * i / 100) / scale
                z = (p[2] + (q[2] - p[2]) * i / 100) / scale
                if not (abs(z) < .05 and -4 <= x <= 4 or
                        abs(x + 2) < .05 and -1e-9 <= z <= 1.6 + 1e-9):
                    return False
        return True
    attempts = []
    def solve(order):
        attempts.append(order)
        paths = {}
        for actor in sorted(tasks, key=lambda a: len(order.get(a, ()))):
            source, goal = tasks[actor]
            reservations = [Reservation(a, path, .2 * scale) for a, path in paths.items()]
            route = plan_route(source, goal, 0, scale, .2 * scale, reservations,
                               static, (-4 * scale, 4 * scale, -.4 * scale, 2 * scale),
                               config, hold_until=15)
            if route.points is None:
                return None, StructuredError('CROWD_ROUTE_LIMIT', 'blocked', actor_id=actor,
                    details={'blocking_actors': route.blocking_actors})
            paths[actor] = route.points
        return paths, None
    def conflict(paths):
        return first_conflict([Reservation(a, p, .2 * scale) for a, p in paths.items()],
                              0, 15, config)
    solution, error, visited = priority_search(solve, conflict, config.max_joint_nodes,
        failure_conflicts=lambda error: [(error.actor_id, other)
                                        for other in error.details['blocking_actors']])
    assert error is None and solution is not None
    assert visited > 1 and attempts[1]['a'] == {'b'}
    assert conflict(solution) is None
    assert any(math.dist(a[1:], b[1:]) < 1e-7 and b[0] > a[0]
               for a, b in zip(solution['a'], solution['a'][1:]))


@pytest.mark.parametrize('budget', [1, 2, 4])
def test_failed_joint_nodes_remain_bounded_and_never_return_partial_solution(budget):
    error = StructuredError('CROWD_ROUTE_LIMIT', 'blocked', actor_id='a')
    result, returned_error, visited = priority_search(lambda order: (None, error),
        lambda result: pytest.fail('A failed route cannot be a solution'), budget,
        failure_conflicts=lambda error: [('a', 'b'), ('b', 'c')])
    assert result is None and returned_error is error and 1 <= visited <= budget
    # Fixed scenery or an unmeasurable obstruction supplies no negotiable pair.
    assert priority_search(lambda order: (None, error), lambda result: None, budget,
                           failure_conflicts=lambda error: []) == (None, error, 1)


@pytest.mark.parametrize('scale', [.5, 1., 2.])
def test_temporary_obstruction_uses_checked_departure_wait_before_grid_search(scale):
    config = CrowdSolverConfig(max_expansions=1, buffer_m=.12)
    blocker = Reservation('blocker', [(0, 0, 0, 0), (3, 0, 0, 0),
                                       (4, 0, 0, 3 * scale)], .25 * scale)
    result = plan_route([-2 * scale, 0, 0], [2 * scale, 0, 0], 0, scale, .25 * scale,
                        [blocker], lambda p: True, (-3 * scale, 3 * scale, -scale, scale),
                        config, hold_until=10)
    assert result.status == 'ok' and result.expansions == 0
    assert len(result.points) == 3 and result.points[1][0] > 0
    for a, b in zip(result.points, result.points[1:]):
        assert not blocked_intervals(a[1:], b[1:], a[0], b[0], .25 * scale, [blocker], config)
    arrival = result.points[-1]
    assert not blocked_intervals(arrival[1:], arrival[1:], arrival[0], 10, .25 * scale, [blocker], config)


def test_departure_wait_cannot_ignore_an_actor_crossing_the_waiting_spot():
    config = CrowdSolverConfig(max_expansions=1, buffer_m=.12)
    blocker = Reservation('blocker', [(0, 0, 0, 0), (3, 0, 0, 0), (4, 0, 0, 3)], .25)
    passer = Reservation('passer', [(0, -2, 0, -1), (1, -2, 0, 0), (2, -2, 0, 1)], .25)
    result = plan_route([-2, 0, 0], [2, 0, 0], 0, 1, .25, [blocker, passer],
                        lambda p: True, (-3, 3, -1, 1), config)
    assert result.points is None
    assert {'blocker', 'passer'} <= set(result.blocking_actors)


@pytest.mark.parametrize('wall_length', [12., 18., 24.])
def test_large_static_detour_fits_existing_budget_with_coarse_to_fine_grid(wall_length):
    from crowd.geometry import clears_solids
    wall = {'min': [-1, 0, -wall_length], 'max': [1, 3, wall_length]}
    static = lambda points: clears_solids(points, [wall], .3)
    args = ([-20, 0, 0], [20, 0, 0], 0, 4, .3, [], static, (-30, 30, -30, 30))
    fine = plan_route(*args, CrowdSolverConfig(route_grid_levels=1))
    assert fine.points is None and fine.status == 'expansion_limit'
    config = CrowdSolverConfig()
    result = plan_route(*args, config)
    assert result.status == 'ok' and result.expansions <= config.max_expansions
    assert result.points[0][1:] == (-20, 0, 0) and result.points[-1][1:] == (20, 0, 0)
    assert result.points[-1][0] <= 10 + config.max_delay_s
    for a, b in zip(result.points, result.points[1:]):
        assert static([a[1:], b[1:]])


def test_narrow_passage_can_fall_back_to_base_grid_without_relaxing_obstacles():
    from crowd.geometry import clears_solids
    # The only passage through the wall is missed by coarse horizontal rows.
    # The base grid has a row at z=0 and must still be tried.
    walls = [{'min': [-.5, 0, -3], 'max': [.5, 3, -.3]},
             {'min': [-.5, 0, .3], 'max': [.5, 3, 3]}]
    static = lambda points: clears_solids(points, walls, .2)
    config = CrowdSolverConfig(cell_size_m=.4)
    result = plan_route([-2, 0, -.4], [2, 0, -.4], 0, 1, .2, [], static,
                        (-3, 3, -2, 2), config)
    assert result.status == 'ok' and result.expansions <= config.max_expansions
    assert all(static([a[1:], b[1:]]) for a, b in zip(result.points, result.points[1:]))


@pytest.mark.parametrize('levels', [0, -1, 1.5])
def test_invalid_route_grid_levels_are_rejected(levels):
    with pytest.raises(ValueError):
        CrowdSolverConfig(route_grid_levels=levels)


def test_movement_pipeline_uses_low_level_blockers_to_retry_failed_root(monkeypatch):
    import crowd.movements as movements
    from crowd_spacing import ActorEnvelopes
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver
    class Synth:
        def _collect_obstacles_at_time(self, *args): return []
    states = {'a': {'position': [-4, 0, 0]}, 'b': {'position': [0, 0, -4]}}
    plan = ShotPlan(beats=[
        Beat('move_a', 'run_to', 'a', {'target': [4, 0, 0]}, TimingCondition('at', 0),
             TimingCondition('for_duration', 4)),
        Beat('move_b', 'run_to', 'b', {'target': [0, 0, 4]}, TimingCondition('at', 0),
             TimingCondition('for_duration', 4))])
    original = plan.to_dict()
    real_solve = movements._route_movements
    attempts = []
    def solve(*args, **kwargs):
        order = args[8]
        attempts.append(order)
        # Inject budget exhaustion at the public low-level solver boundary.
        # Subsequent attempts execute the real allocator/router and are checked.
        if len(attempts) == 1:
            return None, StructuredError('CROWD_ROUTE_LIMIT', 'expansion limit', actor_id='b',
                                         details={'blocking_actors': ['a']})
        return real_solve(*args, **kwargs)
    monkeypatch.setattr(movements, '_route_movements', solve)
    resolver = TimelineResolver()
    routed, error = movements.route_movements(plan, states, resolver, Synth(), {},
                                              ActorEnvelopes({'a': .25, 'b': .25}, {}))
    assert error is None and len(attempts) > 1
    assert plan.to_dict() == original
    timeline, error = resolver.resolve(routed, states)
    assert error is None and {'move_a.finished', 'move_b.finished'} <= set(timeline.events)
    tracks = {a: [(0, *s['position'])] for a, s in states.items()}
    by_id = {b.beat_id: b for b in routed.beats}
    for b in sorted(timeline.beats, key=lambda b: (b.start_time, b.end_time)):
        if b.beat_id in by_id:
            resolver._update_provisional_trajectory(b.actor_id, b.start_time, b.end_time,
                                                   by_id[b.beat_id], tracks)
    assert first_conflict([Reservation(a, p, .25) for a, p in tracks.items()],
                          0, timeline.total_duration_s, CrowdSolverConfig()) is None


@pytest.mark.parametrize('scale', [1., 1.5, 2.])
def test_corridor_coordination_reverses_failed_priority_and_waits(scale):
    # A exits a side aisle and stops in the corridor. B must traverse it first;
    # giving A precedence makes B's goal unreachable. Trying the sibling order
    # produces a wait in the side aisle, then safe passage to both destinations.
    config = CrowdSolverConfig(cell_size_m=.4 * scale)
    tasks = {'a': ([-2 * scale, 0, 1.6 * scale], [0, 0, 0]),
             'b': ([3.2 * scale, 0, 0], [-3.2 * scale, 0, 0])}
    def static(points):
        for p, q in zip(points, points[1:] or points):
            steps = max(1, math.ceil(math.dist(p, q) / (.05 * scale)))
            for i in range(steps + 1):
                x = (p[0] + (q[0] - p[0]) * i / steps) / scale
                z = (p[2] + (q[2] - p[2]) * i / steps) / scale
                if not (abs(z) < .05 and -4 <= x <= 4 or
                        abs(x + 2) < .05 and -1e-9 <= z <= 1.6 + 1e-9):
                    return False
        return True
    tried = []
    def solve(order):
        tried.append(order)
        paths = {}
        for actor in sorted(tasks, key=lambda a: len(order.get(a, ()))):
            source, goal = tasks[actor]
            reservations = [Reservation(a, paths[a], .2 * scale) for a in order.get(actor, ())]
            route = plan_route(source, goal, 0, scale, .2 * scale, reservations,
                               static, (-4 * scale, 4 * scale, -.4 * scale, 2 * scale),
                               config, hold_until=15)
            if route.points is None:
                return None, route.status
            paths[actor] = route.points
        return paths, None
    def conflict(paths):
        return first_conflict([Reservation(a, p, .2 * scale) for a, p in paths.items()],
                              0, 15, config)
    paths, error, visited = priority_search(solve, conflict, config.max_joint_nodes)
    assert error is None and paths is not None and visited == 3
    assert tried[1]['b'] == {'a'} and tried[2]['a'] == {'b'}
    assert conflict(paths) is None
    assert any(math.dist(a[1:], b[1:]) < 1e-7 and b[0] > a[0]
               for a, b in zip(paths['a'], paths['a'][1:]))
    assert paths['a'][-1][1:] == tuple(tasks['a'][1])
    assert paths['b'][-1][1:] == tuple(tasks['b'][1])


def test_fixed_prefix_is_reserved_until_adjustable_departure():
    config = CrowdSolverConfig()
    r = PrefixReservation(Reservation('dancer', [(0, 0, 0, 0), (4, 0, 0, 0),
                                                (5, 2, 0, 0)], .3), 4)
    assert blocked_intervals([-1, 0, 0], [1, 0, 0], 2, 3, .3, [r], config)
    assert not blocked_intervals([-1, 0, 0], [1, 0, 0], 5, 6, .3, [r], config)


def test_conflict_detection_accounts_for_both_actors_radius_windows():
    a = Reservation('a', [(0, 0, 0, 0)], .2, [(2, 4, .8)])
    b = Reservation('b', [(0, 1, 0, 0)], .2)
    config = CrowdSolverConfig()
    assert first_conflict([a, b], 0, 5, config) == ('a', 'b')
    assert first_conflict([b, a], 0, 5, config) == ('b', 'a')
    assert first_conflict([a, b], 0, 5, config, [({'a', 'b'}, 2, 4)]) is None


def test_contact_approach_relaxes_extra_buffer_only_for_its_pair():
    config = CrowdSolverConfig()
    assert config.buffer_m == .3
    a = Reservation('a', [(0, 0, 0, 0)], .2)
    b = Reservation('b', [(0, .6, 0, 0)], .2)
    outsider = Reservation('outsider', [(0, -.6, 0, 0)], .2)
    window = ({'a', 'b'}, 0, 1)
    assert first_conflict([a, b], 0, 1, config, body_only=[window]) == ('a', 'b')
    assert first_conflict([a, b], 0, 1, config, body_only=[(*window, False)]) == ('a', 'b')
    assert first_conflict([a, b], 0, 1, config, body_only=[(*window, True)]) is None
    assert first_conflict([a, b, outsider], 0, 1, config,
                          body_only=[(*window, True)]) == ('a', 'outsider')
    b.points = [(0, .3, 0, 0)]
    assert first_conflict([a, b], 0, 1, config, body_only=[(*window, True)]) == ('a', 'b')


def test_joint_budget_returns_failure_instead_of_conflicting_paths():
    solution, error, visited = priority_search(lambda order: ({}, None),
                                               lambda paths: ('a', 'b'), 1)
    assert solution is None and visited == 1
    assert ancestors({('a', 'b'), ('b', 'c')})['c'] == {'a', 'b'}
    assert ancestors({('a', 'b'), ('b', 'a')}) is None


@pytest.mark.parametrize('reach', [.8, 1.4, 2.])
def test_meeting_slot_rotates_or_moves_to_clear_subsequent_limbs(reach):
    from crowd.geometry import clears_solids
    from crowd_spacing import ActorEnvelopes
    from director_solver import schedule_prompt_interactions
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver
    class Synth:
        def _collect_obstacles_at_time(self, *args): return []
    states = {'a': {'position': [-4, 0, -1]}, 'b': {'position': [-4, 0, 1]}}
    beats = [Beat('stage_' + a, 'turn_to_face', a,
                  {'target': b, 'group_id': 'stage', 'participants': ['a', 'b'], 'spacing': 'combat'},
                  TimingCondition('at', 0)) for a, b in [('a', 'b'), ('b', 'a')]]
    beats.append(Beat('gesture', 'react', 'a', {'mood': 'startled'}))
    wall = {'min': [-3, 0, -5], 'max': [-2, 3, 5]}
    env = ActorEnvelopes({'a': .2, 'b': .2}, {}, solid_beat_reach={'gesture': reach})
    scheduled, error = schedule_prompt_interactions(ShotPlan(beats=beats), states,
        TimelineResolver(), Synth(), {'solid_boxes': [wall]}, envelopes=env)
    assert error is None, error
    slot = next(b for b in scheduled.beats if b.beat_id == 'stage_a').params['interaction_positions']['a']
    assert clears_solids([slot], [wall], reach + env.buffer_m)


def test_reordered_wave_reserves_groups_later_in_source_list():
    from crowd_spacing import ActorEnvelopes
    from director_solver import schedule_prompt_interactions
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver
    class Synth:
        def _collect_obstacles_at_time(self, *args): return []
    states = {a: {'position': p} for a, p in {
        'e': [100, 0, -2], 'f': [100, 0, 2],
        'a': [8, 0, -1], 'b': [16, 0, -1],
        'c': [9, 0, 4], 'd': [9, 0, -4]}.items()}
    beats = []
    for group, pair, trigger in [('anchor', ('e', 'f'), 0), ('crossing', ('a', 'b'), 3),
                                  ('wave_peer', ('c', 'd'), 0)]:
        for actor, target in (pair, pair[::-1]):
            beats.append(Beat(group + '_' + actor, 'turn_to_face', actor,
                {'group_id': group, 'participants': list(pair), 'target': target, 'spacing': 'combat'},
                TimingCondition('at', trigger)))
    resolver = TimelineResolver()
    config = CrowdSolverConfig()
    scheduled, error = schedule_prompt_interactions(ShotPlan(beats=beats), states,
        resolver, Synth(), {}, envelopes=ActorEnvelopes({a: .3 for a in states}, {}))
    assert error is None, error
    timeline, error = resolver.resolve(scheduled, states)
    assert error is None
    tracks = {a: [(0, *s['position'])] for a, s in states.items()}
    by_id = {b.beat_id: b for b in scheduled.beats}
    for b in sorted(timeline.beats, key=lambda b: (b.start_time, b.end_time)):
        if b.beat_id in by_id:
            resolver._update_provisional_trajectory(b.actor_id, b.start_time, b.end_time,
                                                   by_id[b.beat_id], tracks)
    reservations = [Reservation(a, p, .3) for a, p in tracks.items()]
    assert first_conflict(reservations, 0, timeline.total_duration_s, config) is None


def test_priority_search_deprioritizes_failed_constrained_branch_so_sibling_order_runs_next():
    visited_orders = []

    def solve(order):
        visited_orders.append(order)
        if not order:
            # Root succeeds provisionally; post-solve conflict will branch ('a', 'b') and ('b', 'a').
            return {'ok': False}, None
        if order.get('b') == {'a'}:
            # First child ('a', 'b') fails low-level routing and reports spurious blockers.
            return None, StructuredError('CROWD_ROUTE_LIMIT', 'blocked', actor_id='b',
                                         details={'blocking_actors': ['c', 'd', 'e']})
        if order.get('a') == {'b'}:
            # Sibling child ('b', 'a') succeeds cleanly.
            return {'ok': True}, None
        return None, StructuredError('CROWD_ROUTE_LIMIT', 'dead subtree', actor_id='b',
                                     details={'blocking_actors': ['c', 'd']})

    def conflict(sol):
        return None if sol.get('ok') else ('a', 'b')

    solution, error, visited = priority_search(
        solve, conflict, max_nodes=10,
        failure_conflicts=lambda err: [(err.actor_id, other) for other in err.details.get('blocking_actors', [])],
    )
    assert error is None and solution == {'ok': True}
    # Root (1), failed child ('a', 'b') (2), and immediately sibling ('b', 'a') (3).
    assert visited == 3


def test_destination_allocation_rejects_slots_on_swept_non_moving_paths():
    import crowd.movements as movements
    from crowd_spacing import ActorEnvelopes
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver

    class Synth:
        def _collect_obstacles_at_time(self, *args): return []

    # Actor 'walker' has a pre-scheduled brief_path from (-4, 0, 0) at t=0 to (4, 0, 0) at t=10,
    # passing right through (0, 0, 0) at t=5. Its trajectory only has vertices at t=0 and t=10.
    # Actor 'mover' arrives at (0, 0, 0) at t=2 and holds until t=10.
    states = {'walker': {'position': [-4., 0., 0.]}, 'mover': {'position': [0., 0., -2.]}}
    plan = ShotPlan(beats=[
        Beat('locked_walk', 'walk_to', 'walker',
             {'brief_path': {'points': [[-4., 0., 0.], [4., 0., 0.]]}},
             TimingCondition('at', 0.), TimingCondition('for_duration', 10.)),
        Beat('crowd_move', 'walk_to', 'mover',
             {'target': [0., 0., 0.]},
             TimingCondition('at', 0.), TimingCondition('for_duration', 2.)),
    ])
    scene = {'support_surfaces': [{'prim_path': '/World/Floor', 'height_m': 0.,
                                   'polygon_xz': [[-10., -10.], [10., -10.], [10., 10.], [-10., 10.]]}]}
    resolver = TimelineResolver()
    envelopes = ActorEnvelopes({'walker': .3, 'mover': .3}, {})
    routed, error = movements.route_movements(plan, states, resolver, Synth(), scene, envelopes)
    assert error is None, error
    arrival = next(b for b in routed.beats if b.beat_id == 'crowd_move').params['arrival_position']
    # The allocated arrival slot must be nudged off z=0 so 'walker' does not sweep through 'mover'.
    assert abs(arrival[2]) >= 0.6


def test_nearby_stop_fallback_computes_speed_from_stop_distance():
    import crowd.movements as movements
    from crowd_spacing import ActorEnvelopes
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver

    class Synth:
        def _collect_obstacles_at_time(self, *args): return []

    # 'mover' requests a short 0.5m move from (0, 0, 0) to (0.5, 0, 0) over 4s.
    # 'blocker' moves across (0.5, 0, 0) right after t=4s, forcing 'mover' to use a nearby_stop
    # up to ~1.5m away; using the primary 0.125 m/s speed for a 1.5m stop would take 12s (> max_delay_s=8s).
    states = {'mover': {'position': [0., 0., 0.]}, 'blocker': {'position': [0.5, 0., -4.]}}
    plan = ShotPlan(beats=[
        Beat('move_short', 'walk_to', 'mover',
             {'target': [0.5, 0., 0.]},
             TimingCondition('at', 0.), TimingCondition('for_duration', 4.)),
        Beat('sweep_goal', 'walk_to', 'blocker',
             {'brief_path': {'points': [[0.5, 0., -4.], [0.5, 0., 0.], [0.5, 0., 4.]]}},
             TimingCondition('at', 0.), TimingCondition('for_duration', 10.)),
    ])
    scene = {'support_surfaces': [{'prim_path': '/World/Floor', 'height_m': 0.,
                                   'polygon_xz': [[-10., -10.], [10., -10.], [10., 10.], [-10., 10.]]}]}
    resolver = TimelineResolver()
    envelopes = ActorEnvelopes({'mover': .3, 'blocker': .3}, {})
    routed, error = movements.route_movements(plan, states, resolver, Synth(), scene, envelopes)
    assert error is None, error



def test_occupied_start_reports_only_present_blockers_and_fails_without_expansion():
    fixed = Reservation('fixed', [(0., 0., 0., 0.)], .3)
    future = Reservation('future', [(0., 0., 0., -4.), (4., 0., 0., 0.)], .3)
    result = plan_route([0., 0., 0.], [4., 0., 0.], 0., 1., .3,
                        [fixed, future], lambda points: True, (-5., 5., -5., 5.))
    assert result.status == 'start_occupied' and result.expansions == 0
    assert result.blocking_actors == result.critical_blockers == ('fixed',)


def test_fixed_start_blocker_does_not_branch_on_unrelated_adjustable_actors():
    import crowd.movements as movements
    from crowd_spacing import ActorEnvelopes
    from plan_model import Beat, ShotPlan, TimingCondition
    from timeline_resolver import TimelineResolver

    class Synth:
        def _collect_obstacles_at_time(self, *args): return []

    states = {'mover': {'position': [0., 0., 0.]},
              'fixed': {'position': [0., 0., 0.]},
              'other': {'position': [0., 0., -5.]}}
    plan = ShotPlan(beats=[
        Beat('move', 'walk_to', 'mover', {'target': [4., 0., 0.]},
             TimingCondition('at', 0.), TimingCondition('for_duration', 4.)),
        Beat('other_move', 'walk_to', 'other', {'target': [4., 0., -5.]},
             TimingCondition('at', 0.), TimingCondition('for_duration', 4.)),
    ])
    result, error = movements.route_movements(
        plan, states, TimelineResolver(), Synth(), {},
        ActorEnvelopes({a: .3 for a in states}, {}))
    assert result is None and error.error_code == 'CROWD_ROUTE_LIMIT'
    assert error.details['planner_status'] == 'start_occupied'
    assert error.details['joint_nodes'] == 1
    assert error.details['blocking_actors'] == ['fixed']
    assert error.details['negotiable_blockers'] == []


def test_repeated_failure_yields_to_novel_queries_without_discarding_combinations():
    visited = []

    def solve(order):
        visited.append(order)
        if not order:
            return None, StructuredError('CROWD_ROUTE_LIMIT', 'root', actor_id='a',
                                        details={'signature': 'root', 'blockers': ['b', 'c']})
        # The b-before-a child repeats the root query. Its deeper combinations
        # must yield to the still-unexplored sibling involving c.
        if 'c' not in order.get('a', ()):
            return None, StructuredError('CROWD_ROUTE_LIMIT', 'same query', actor_id='a',
                                        details={'signature': 'root', 'blockers': ['c']})
        if 'b' not in order.get('a', ()):
            return None, StructuredError('CROWD_ROUTE_LIMIT', 'changed query', actor_id='a',
                                        details={'signature': 'changed', 'blockers': ['b']})
        return {'ok': True}, None

    result, error, count = priority_search(
        solve, lambda result: None, 32,
        failure_conflicts=lambda e: [('a', b) for b in e.details['blockers']],
        failure_signature=lambda e: e.details['signature'])
    assert error is None and result == {'ok': True}
    assert count < 32
    assert any({'b', 'c'} <= order.get('a', set()) for order in visited)
    # c-before-a is tried before any descendant of the unchanged b branch.
    c_index = next(i for i, order in enumerate(visited) if order.get('a') == {'c'})
    combined_index = next(i for i, order in enumerate(visited) if {'b', 'c'} <= order.get('a', set()))
    assert c_index < combined_index
