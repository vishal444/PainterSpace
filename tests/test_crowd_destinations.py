"""Shared targets are arrival regions with separate clearance reservations."""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))

from crowd.config import CrowdSolverConfig
from crowd.destinations import allocate_destinations, _arrival_candidates
from crowd.geometry import clears_solids, supported_disc
from crowd.movements import route_movements
from crowd.st_planner import Reservation, blocked_intervals
from crowd_spacing import ActorEnvelopes
from plan_model import Beat, ShotPlan, SetupAction, TimingCondition
from timeline_resolver import TimelineResolver


@pytest.mark.parametrize('count', [4, 10, 20, 40])
def test_shared_point_produces_distinct_nearby_slots(count):
    config = CrowdSolverConfig()
    requests = [{'key': f'move_{i}', 'actor': f'actor_{i}', 'goal': [0, 0, 0],
                 'source': [10 * math.cos(i * math.tau / count), 0, 10 * math.sin(i * math.tau / count)],
                 'radius': .35, 'start': 2., 'end': 10.} for i in range(count)]
    original = json.dumps(requests)
    slots, failed = allocate_destinations(requests, lambda r, p: True, lambda r: [], config)
    assert failed is None and len(slots) == count
    assert (slots, failed) == allocate_destinations(requests, lambda r, p: True, lambda r: [], config)
    assert json.dumps(requests) == original
    positions = list(slots.values())
    assert all(math.dist(a, b) + 1e-7 >= .7 + config.buffer_m
               for i, a in enumerate(positions) for b in positions[i + 1:])
    assert all(math.dist(p, [0, 0, 0]) <= (.7 + config.buffer_m) * config.destination_rings
               for p in positions)


def test_stationary_target_actor_and_obstacle_reserve_space():
    request = {'key': 'arrival', 'actor': 'visitor', 'goal': [0, 0, 0],
               'source': [-5, 0, 0], 'radius': .4, 'start': 1, 'end': 10}
    obstacle = {'min': [-2, 0, -1], 'max': [-.5, 2, 1]}
    slots, failed = allocate_destinations([request],
        lambda r, p: clears_solids([p], [obstacle], r['radius']),
        lambda r: [([0, 0, 0], .6)])
    assert failed is None
    point = slots['arrival']
    assert math.dist(point, [0, 0, 0]) >= 1.12
    assert clears_solids([point], [obstacle], .4)


def test_full_destination_fails_without_stacking_or_partial_output():
    requests = [{'key': str(i), 'actor': str(i), 'goal': [0, 0, 0],
                 'source': [-5, 0, 0], 'radius': .5, 'start': 0, 'end': 4,
                 'search_radius_m': 5.} for i in range(2)]
    surface = {'height_m': 0., 'polygon_xz': [[-.6, -.6], [.6, -.6], [.6, .6], [-.6, .6]]}
    slots, failed = allocate_destinations(requests,
        lambda r, p: supported_disc(p, r['radius'], [surface], CrowdSolverConfig()), lambda r: [])
    assert slots is None and failed == '1'


def test_blocked_nearby_arrival_expands_to_supported_ground():
    config = CrowdSolverConfig()
    floor = {'height_m': 0., 'polygon_xz': [[-20, -20], [20, -20],
                                           [20, 20], [-20, 20]]}
    obstacle = {'min': [-5, 0, -5], 'max': [5, 2, 5]}
    request = {'key': 'arrival', 'actor': 'visitor', 'goal': [0, 0, 0],
               'source': [-15, 0, 0], 'radius': .3, 'start': 2., 'end': 5.,
               'search_radius_m': math.hypot(20, 20)}
    slots, failed = allocate_destinations([request],
        lambda r, p: supported_disc(p, r['radius'], [floor], config)
        and clears_solids([p], [obstacle], r['radius'] + config.buffer_m),
        lambda r: [], config)
    assert failed is None
    assert math.dist(slots['arrival'], request['goal']) > 5.
    assert math.dist(slots['arrival'], request['goal']) <= request['search_radius_m']


def test_ground_move_uses_farther_arrival_when_target_area_is_blocked():
    scene = {'support_surfaces': [{'height_m': 0.,
              'polygon_xz': [[-20, -20], [20, -20], [20, 20], [-20, 20]]}],
             'solid_boxes': [{'min': [-5, 0, -5], 'max': [5, 2, 5]}]}
    states = {'visitor': {'position': [-15, 0, 0]}}
    plan = ShotPlan(beats=[Beat('arrival', 'walk_to', 'visitor', {'target': [0, 0, 0]},
                               TimingCondition('at', 0), TimingCondition('for_duration', 5))])
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(),
                                    scene, ActorEnvelopes({'visitor': .3}, {}))
    assert error is None, error
    arrival = routed.beats[0].params['arrival_position']
    assert math.dist(arrival, [0, 0, 0]) > 5.
    assert clears_solids([arrival], scene['solid_boxes'], .3)


def test_vehicle_exit_updates_ground_destination_before_following_walk():
    from semantic_references import ReferenceResolver
    floor = {'height_m': 0., 'polygon_xz': [[-20, -20], [20, -20],
                                            [20, 20], [-20, 20]]}
    states = {
        'car_a': {'position': [-4, 0, 0], 'asset_id': 'cars.car1', 'is_vehicle': True},
        'car_b': {'position': [4, 0, 0], 'asset_id': 'cars.car1', 'is_vehicle': True},
        'driver_a': {'position': [-4.36, .35, -.15], 'asset_id': 'character.male'},
        'driver_b': {'position': [3.64, .35, -.15], 'asset_id': 'character.male'},
    }
    plan = ShotPlan(beats=[
        Beat('exit_a', 'exit_vehicle', 'driver_a',
             {'vehicle': 'car_a', 'target': 'car_a:door_fl_stand'},
             TimingCondition('at', 0), TimingCondition('for_duration', 2)),
        Beat('exit_b', 'exit_vehicle', 'driver_b',
             {'vehicle': 'car_b', 'target': 'car_b:door_fl_stand'},
             TimingCondition('at', 0), TimingCondition('for_duration', 2)),
        Beat('walk', 'walk_to', 'driver_a', {'target': 'driver_b'},
             TimingCondition('after', 'exit_a.finished'), TimingCondition('for_duration', 4)),
    ])
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({'assets': []}, {}))
    routed, error = route_movements(plan, states, resolver, StraightRoutes(),
                                    {'support_surfaces': [floor]},
                                    ActorEnvelopes({'driver_a': .3, 'driver_b': .3}, {}))
    assert error is None, error
    arrival = next(b for b in routed.beats if b.beat_id == 'walk').params['arrival_position']
    assert arrival[1] == pytest.approx(0.)


def test_nonoverlapping_visits_can_reuse_destination():
    requests = [{'key': str(i), 'actor': str(i), 'goal': [0, 0, 0], 'source': [-5, 0, 0],
                 'radius': .5, 'start': i * 4, 'end': (i + 1) * 4} for i in range(2)]
    slots, failed = allocate_destinations(requests, lambda r, p: True, lambda r: [])
    assert failed is None and slots['0'] == slots['1'] == [0, 0, 0]


@pytest.mark.parametrize('radius', [.2, .6, 1.2])
def test_joint_scatter_revisits_earlier_slot_when_it_blocks_both_later_choices(radius):
    config = CrowdSolverConfig()
    pitch = 2 * radius + config.buffer_m
    offset = pitch / 4
    requests = [{'key': key, 'actor': key, 'goal': [goal, 0, 0],
                 'source': [-10 * pitch, 0, 0], 'radius': radius, 'start': 0, 'end': 4}
                for key, goal in [('a', 0), ('b', offset)]]
    # Each actor has two clear slots, as with two narrow approach pockets.
    # A's preferred goal obstructs both B choices; A must take its other slot.
    allowed = {'a': [[0, 0, 0], [-pitch, 0, 0]],
               'b': [[offset, 0, 0], [offset - pitch, 0, 0]]}
    def clear(r, p):
        return any(math.dist(p, q) < 1e-7 for q in allowed[r['key']])
    original = json.dumps(requests)
    slots, failed = allocate_destinations(requests, clear, lambda r: [], config)
    assert failed is None
    assert slots['a'] == pytest.approx([-pitch, 0, 0])
    assert slots['b'] == pytest.approx([offset, 0, 0])
    assert math.dist(slots['a'], slots['b']) >= pitch
    assert json.dumps(requests) == original
    # The same search with no room to revisit must fail without partial output.
    from dataclasses import replace
    slots, failed = allocate_destinations(requests, clear, lambda r: [],
                                          replace(config, max_destination_nodes=1))
    assert slots is None and failed in allowed


@pytest.mark.parametrize('pitch', [.3, 1., 2.5])
def test_scatter_ring_neighbors_have_required_chord_clearance(pitch):
    request = {'goal': [0, 0, 0], 'source': [-20, 0, 0]}
    points = _arrival_candidates(request, pitch, CrowdSolverConfig(arrival_tolerance_m=0))
    for ring in range(1, CrowdSolverConfig().destination_rings + 1):
        members = [p for p in points if abs(math.dist(p, request['goal']) - pitch * ring) < 1e-7]
        assert len(members) >= 6 * ring
        assert all(math.dist(a, b) + 1e-7 >= pitch
                   for i, a in enumerate(members) for b in members[i + 1:])


@pytest.mark.parametrize('count', [3, 12, 24])
def test_mixed_size_crowd_scatters_on_supported_floor_around_occupied_target(count):
    config = CrowdSolverConfig()
    goal = [2., .8, -3.]
    floor = {'height_m': goal[1], 'polygon_xz': [[-12, -17], [16, -17], [16, 11], [-12, 11]]}
    table = {'min': [1.5, .8, -3.5], 'max': [2.5, 2, -2.5]}
    requests = [{'key': str(i), 'actor': str(i), 'goal': list(goal),
                 'source': [goal[0] + 10 * math.cos(math.tau * i / count), goal[1],
                            goal[2] + 10 * math.sin(math.tau * i / count)],
                 'radius': .25 + (i % 3) * .15, 'start': 0, 'end': 5} for i in range(count)]
    def clear(r, p):
        return (supported_disc(p, r['radius'], [floor], config)
                and clears_solids([p], [table], r['radius'] + config.buffer_m))
    slots, failed = allocate_destinations(requests, clear, lambda r: [(goal, .4)], config)
    assert failed is None
    assert (slots, failed) == allocate_destinations(requests, clear, lambda r: [(goal, .4)], config)
    for i, request in enumerate(requests):
        point = slots[request['key']]
        assert point[1] == goal[1] and clear(request, point)
        assert math.dist(point, goal) + 1e-7 >= request['radius'] + .4 + config.buffer_m
        for other in requests[i + 1:]:
            assert math.dist(point, slots[other['key']]) + 1e-7 >= request['radius'] + other['radius'] + config.buffer_m


def test_scatter_does_not_block_visitors_on_separate_floors():
    requests = [{'key': str(i), 'actor': str(i), 'goal': [0, 2 * i, 0],
                 'source': [-5, 2 * i, 0], 'radius': .5, 'start': 0, 'end': 4} for i in range(2)]
    slots, failed = allocate_destinations(requests, lambda r, p: True, lambda r: [])
    assert failed is None
    assert slots == {r['key']: r['goal'] for r in requests}


def test_scatter_empty_input_and_budget_validation():
    assert allocate_destinations([], lambda r, p: True, lambda r: []) == ({}, None)
    with pytest.raises(ValueError):
        CrowdSolverConfig(max_destination_nodes=0)


def test_scatter_candidates_stay_on_destination_level_and_respect_candidate_cap():
    request = {'goal': [0, 0, 0], 'source': [0, 1, 0]}
    config = CrowdSolverConfig(destination_rings=10000, destination_candidates=8)
    points = _arrival_candidates(request, 1., config)
    assert len(points) == 8
    assert all(p[1] == 0 for p in points)


def test_swept_body_cannot_cross_furniture_and_floor_is_walkable():
    floor = {'min': [-10, -.2, -10], 'max': [10, 0, 10]}
    furniture = {'min': [-.5, 0, -.5], 'max': [.5, 1, .5]}
    assert clears_solids([[-2, 0, 0], [2, 0, 0]], [floor], .3)
    assert not clears_solids([[-2, 0, 0], [2, 0, 0]], [floor, furniture], .3)
    assert not clears_solids([[.6, 0, 0]], [furniture], .3)
    assert clears_solids([[-2, 0, 1], [2, 0, 1]], [furniture], .3)


def test_floor_change_requires_an_explicit_vertical_movement_contract():
    states = {'a': {'position': [0, 0, 0]}}
    plan = ShotPlan(beats=[Beat('vertical', 'walk_to', 'a', {'target': [2, 2, 0]})])
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), {},
                                    ActorEnvelopes({'a': .3}, {}))
    assert routed is None and error.error_code == 'CROWD_LEVEL_TRANSITION'
    # An authored path has its own support/vertical contract and stays intact.
    plan.beats[0].params['brief_path'] = {'points': [[0, 0, 0], [2, 2, 0]]}
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), {},
                                    ActorEnvelopes({'a': .3}, {}))
    assert error is None and routed.to_dict() == plan.to_dict()


def test_already_at_destination_stays_there_and_recompiles():
    states = {'a': {'position': [0, 0, 0]}}
    plan = ShotPlan(beats=[Beat('stay', 'walk_to', 'a', {'target': [0, 0, 0]})])
    env = ActorEnvelopes({'a': .3}, {})
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), {}, env)
    assert error is None
    assert routed.beats[0].params['brief_path']['points'] == [[0, 0, 0], [0, 0, 0]]
    again, error = route_movements(routed, states, TimelineResolver(), StraightRoutes(), {}, env)
    assert error is None and again.to_dict() == routed.to_dict()


class StraightRoutes:
    def _collect_obstacles_at_time(self, *args): return []
    def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]


def test_actor_already_near_blocked_target_can_stay_put():
    states = {'visitor': {'position': [-1.5, 0, 0]}, 'occupant': {'position': [0, 0, 0]}}
    plan = ShotPlan(beats=[Beat('arrival', 'walk_to', 'visitor', {'target': [0, 0, 0]},
                               TimingCondition('at', 0), TimingCondition('for_duration', 3))])
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, StraightRoutes(), {},
                                    ActorEnvelopes({a: .3 for a in states}, {}))
    assert error is None
    arrival = next(b for b in routed.beats if b.beat_id == 'arrival')
    assert arrival.params['arrival_position'] == states['visitor']['position']
    assert arrival.params['crowd_movement_source']['params']['target'] == [0, 0, 0]
    timeline, error = resolver.resolve(routed, states)
    assert error is None and 'arrival.finished' in timeline.events


@pytest.mark.parametrize('source_x,can_stay', [(-.8, True), (-3., False)])
def test_unreachable_exact_goal_accepts_only_a_clear_nearby_stop(source_x, can_stay):
    scene = {'support_surfaces': [{'height_m': 0.,
              'polygon_xz': [[-4, -2], [4, -2], [4, 2], [-4, 2]]}],
             'solid_boxes': [{'min': [-.1, 0, -3], 'max': [.1, 2, 3]}]}
    states = {'visitor': {'position': [source_x, 0, 0]}}
    plan = ShotPlan(beats=[Beat('arrival', 'walk_to', 'visitor', {'target': [.8, 0, 0]},
                               TimingCondition('at', 0), TimingCondition('for_duration', 3))])
    config = CrowdSolverConfig(arrival_tolerance_m=2., max_expansions=256)
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(), scene,
                                    ActorEnvelopes({'visitor': .2}, {}), config)
    if can_stay:
        assert error is None
        arrival = next(b for b in routed.beats if b.beat_id == 'arrival')
        assert arrival.function == 'wait'
        assert arrival.params['arrival_position'] == states['visitor']['position']
        assert clears_solids([arrival.params['arrival_position']], scene['solid_boxes'], .32)
    else:
        assert routed is None and error.error_code == 'CROWD_ROUTE_LIMIT'


def test_approaching_occupied_nearby_stop_is_not_accepted():
    from crowd.destinations import nearby_stops
    config = CrowdSolverConfig(arrival_tolerance_m=.75)
    points = nearby_stops([-3, 0, 0], [0, 0, 0], config)
    assert [-3, 0, 0] not in points
    assert all(math.dist(p, [0, 0, 0]) <= .75+1e-9 for p in points)


@pytest.mark.parametrize('with_generated_approach', [False, True])
@pytest.mark.parametrize('interaction', ['turn_to_face', 'attack'])
def test_move_into_automatic_meeting_uses_its_floor_and_survives_regeneration(
        with_generated_approach, interaction):
    from semantic_references import ReferenceResolver
    scene = {'objects': [{'id': 'Venue', 'name': 'Venue', 'position': [0, .5, 0]}],
             'standing_spots': [{'owner': 'Venue', 'name': 'standing_left_01',
                                 'position': [0, .5, 0]}]}
    states = {'visitor': {'position': [-4, .5, 0]}, 'partner': {'position': [4, .5, 0]}}
    beats = [Beat('arrival', 'run_to', 'visitor', {'target': [0, 0, 0]},
                  TimingCondition('at', 0), TimingCondition('for_duration', 4))]
    if with_generated_approach:
        beats.append(Beat('generated_wait', 'wait', 'visitor', {'auto_interaction_approach': 'pair'},
                          TimingCondition('at', 4), TimingCondition('for_duration', .2)))
    beats.append(Beat('stage', interaction, 'visitor',
                     {'target': 'partner', 'participants': list(states), 'group_id': 'pair',
                      'center_auto': True, 'center': 'Venue:standing_left_01'}))
    plan = ShotPlan(beats=beats)
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({'assets': []}, scene))
    env = ActorEnvelopes({a: .3 for a in states}, {})
    routed, error = route_movements(plan, states, resolver, StraightRoutes(), scene, env,
                                    preserve_approaches=True)
    assert error is None, error
    arrival = next(b for b in routed.beats if b.beat_id == 'arrival')
    assert arrival.params['arrival_position'][1] == .5
    assert arrival.params['crowd_movement_source']['params']['target'] == [0, 0, 0]
    again, error = route_movements(routed, states, resolver, StraightRoutes(), scene, env,
                                   preserve_approaches=True)
    assert error is None and again.to_dict() == routed.to_dict()


@pytest.mark.parametrize('reach', [.8, 1.4, 2.])
def test_arrival_moves_away_from_scenery_for_following_gesture(reach):
    states = {'a': {'position': [-5, 0, 0]}}
    wall = {'min': [.5, 0, -3], 'max': [1, 3, 3]}
    plan = ShotPlan(beats=[
        Beat('move', 'walk_to', 'a', {'target': [0, 0, 0]},
             TimingCondition('at', 0), TimingCondition('for_duration', 4)),
        Beat('gesture', 'react', 'a', {'mood': 'startled'})])
    env = ActorEnvelopes({'a': .2}, {}, solid_beat_reach={'gesture': reach})
    routed, error = route_movements(plan, states, TimelineResolver(), StraightRoutes(),
                                    {'solid_boxes': [wall]}, env)
    assert error is None, error
    arrival = next(b for b in routed.beats if b.beat_id == 'move').params['arrival_position']
    assert math.dist(arrival, [0, 0, 0]) > 0
    assert clears_solids([arrival], [wall], reach + env.buffer_m)


def test_shared_target_routes_and_recompilation_keep_distinct_arrivals():
    count = 4
    states = {f'actor_{i}': {'position': [5 * math.cos(i * math.tau / count), 0,
                                        5 * math.sin(i * math.tau / count)]} for i in range(count)}
    plan = ShotPlan(beats=[Beat(f'move_{a}', 'walk_to', a, {'target': [0, 0, 0]},
        TimingCondition('at', 0), TimingCondition('for_duration', 4)) for a in states])
    env = ActorEnvelopes({a: .3 for a in states}, {})
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, StraightRoutes(), {}, env)
    assert error is None, error
    repeated, error = route_movements(routed, states, resolver, StraightRoutes(), {}, env)
    assert error is None and routed.to_dict() == repeated.to_dict()
    timeline, error = resolver.resolve(routed, states)
    assert error is None
    by_id = {b.beat_id: b for b in routed.beats}
    tracks = {a: [(0, *s['position'])] for a, s in states.items()}
    for b in sorted(timeline.beats, key=lambda b: (b.start_time, b.end_time)):
        if b.beat_id in by_id:
            resolver._update_provisional_trajectory(b.actor_id, b.start_time, b.end_time, by_id[b.beat_id], tracks)
    reservations = [Reservation(a, track, .3) for a, track in tracks.items()]
    for i, a in enumerate(reservations):
        for b in reservations[i + 1:]:
            for t0, t1, p, q, r in a.intervals(0, timeline.total_duration_s):
                assert not blocked_intervals(p, q, t0, t1, r, [b], CrowdSolverConfig())
    assert all(b.params['target'] == [0, 0, 0] for b in plan.beats)


@pytest.mark.parametrize('gesture', [False, True])
def test_real_characters_reach_separate_slots_around_solid_target(tmp_path, gesture):
    from pxr import Gf, Usd, UsdGeom
    from compiler import ShotCompiler
    scene = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene))
    world = UsdGeom.Xform.Define(stage, '/World')
    stage.SetDefaultPrim(world.GetPrim())
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(10, .1, 10))
    obstacle = UsdGeom.Cube.Define(stage, '/World/Obstacle')
    obstacle.CreateSizeAttr(1.)
    obstacle.AddTranslateOp().Set(Gf.Vec3d(0, .5, 0))
    stage.GetRootLayer().Save()
    setup = [SetupAction('place', a, 'character.human', position=[x, 0, z])
             for a, x, z in [('a', -5, -3), ('b', 5, 3)]]
    beats = [Beat('move_' + s.actor_id, 'walk_to', s.actor_id, {'target': [0, 0, 0]},
                  TimingCondition('at', 0), TimingCondition('for_duration', 4)) for s in setup]
    if gesture:
        beats.extend(Beat('react_' + s.actor_id, 'react', s.actor_id, {'mood': 'startled'},
                          TimingCondition('after', 'move_' + s.actor_id + '.finished')) for s in setup)
    ok, report = ShotCompiler(ROOT).compile(ShotPlan(setup=setup, beats=beats),
        tmp_path / 'shot' / 'shot.usda', scene_usda_path=scene, validate=True)
    assert ok, report.get('error') or report.get('validation')
    collision = json.loads((tmp_path / 'shot' / 'collision_report.json').read_text())
    assert collision['ok'] and not collision['violations']


@pytest.mark.parametrize('extent', [8., 14.])
def test_measured_compound_open_passage_is_walkable_without_markers(extent):
    from motion_synthesis import MotionSynthesizer
    from crowd.geometry import fallback_obstacle_scene, snapshot_solids
    manifest = {'assets': [{'id': 'environment.compound', 'bounds_m': [extent * 2, 6., extent * 2]}]}
    scene = {'objects': [{'name': 'Compound', 'asset_id': 'environment.compound', 'position': [0, 0, 0]}],
             'solid_boxes': [{'owner': 'Compound', 'min': [-extent, 0, -3], 'max': [extent, 4, -2]},
                             {'owner': 'Compound', 'min': [-extent, 0, 2], 'max': [extent, 4, 3]}],
             'support_surfaces': [{'owner': 'Base', 'height_m': 0.,
                                  'polygon_xz': [[-extent-5, -4], [extent+5, -4],
                                                 [extent+5, 4], [-extent-5, 4]]}]}
    source, goal = [-extent-3, 0, 0], [extent+3, 0, 0]
    synth = MotionSynthesizer(manifest)
    # Whole-object approximation blocks the open middle; actual walls do not.
    assert synth._collect_obstacles_at_time(0, 20, 'walker', source, goal, {}, scene)
    filtered = fallback_obstacle_scene(dict(scene, solid_boxes=snapshot_solids(scene['solid_boxes'])))
    assert not synth._collect_obstacles_at_time(0, 20, 'walker', source, goal, {}, filtered)
    assert len(scene['objects']) == 1
    plan = ShotPlan(beats=[Beat('travel', 'walk_to', 'walker', {'target': goal},
                               TimingCondition('at', 0), TimingCondition('for_duration', 30))])
    states = {'walker': {'position': source}}
    routed, error = route_movements(plan, states, TimelineResolver(), synth, scene,
                                    ActorEnvelopes({'walker': .3}, {}))
    assert error is None, error
    path = next(b for b in routed.beats if b.beat_id == 'travel').params['brief_path']['points']
    assert path[0] == source and path[-1] == goal
    assert clears_solids(path, scene['solid_boxes'], .42)
    # The same geometry still forbids passing through an actual wall.
    assert not clears_solids([[0, 0, -4], [0, 0, 4]], scene['solid_boxes'], .42)


@pytest.mark.parametrize('uncovered', [False, True])
def test_unmeasured_or_incomplete_objects_keep_fallback_blockers(uncovered):
    from crowd.geometry import fallback_obstacle_scene
    scene = {'objects': [{'name': 'Unknown'}, {'name': 'Measured'}],
             'solid_boxes': [{'owner': 'Measured', 'min': [0, 0, 0], 'max': [1, 1, 1]}],
             'unprotected_scene_objects': ['Measured'] if uncovered else []}
    result = fallback_obstacle_scene(scene)
    assert {o['name'] for o in result['objects']} == ({'Unknown', 'Measured'} if uncovered else {'Unknown'})
