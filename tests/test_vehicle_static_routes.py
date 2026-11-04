"""Vehicle planning and synthesized motion use the same checked static route."""
import math
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
from crowd.config import CrowdSolverConfig
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from semantic_references import ResolvedTarget
from timeline_resolver import ResolvedBeat
from vehicle_routes import VehicleRoutes, check_vehicle_roots


def world(scale=1., origin=(0., 0.), height=0.):
    def point(x, z):
        return [origin[0] + x*scale, height, origin[1] + z*scale]
    scene = {'support_surfaces': [{'owner': 'floor', 'height_m': height,
                                  'polygon_xz': [[p[0], p[2]] for p in
                                      [point(-20,-20), point(20,-20), point(20,20), point(-20,20)]]}],
             'solid_boxes': [{'owner': 'wall', 'min': point(-3,-3),
                              'max': [origin[0]+3*scale, height+4*scale, origin[1]+3*scale]}]}
    manifest = {'assets': [{'id': 'transport', 'tags': ['vehicle'], 'bounds_m': [2*scale, 1.5*scale, 4*scale]}]}
    return manifest, scene, point(-12, 0), point(12, 0)


@pytest.mark.parametrize('scale,origin,height', [(1., (0.,0.), 0.), (1.5, (50.,-30.), 3.), (.7, (-40.,15.), 0.)])
@pytest.mark.parametrize('reverse', [False, True])
def test_vehicle_detours_reach_destination_without_interpolation_cutting_corners(scale, origin, height, reverse):
    manifest, scene, start, goal = world(scale, origin, height)
    synth = MotionSynthesizer(manifest, scene_data=scene)
    beat = ResolvedBeat('travel', 'car', 'reverse_to' if reverse else 'drive_to', 0., 5., 5., {'target': goal})
    target = ResolvedTarget(np.array(goal), 0., 0., 'point', 'goal')
    performance, error = synth.synthesize_actor('car', {'asset_id': 'transport', 'is_vehicle': True,
        'position': start}, [beat], {'travel': target}, {}, 5.)
    assert error is None, error
    assert performance.keyframes[-1].position == pytest.approx(goal)
    assert any(abs(k.position[2]-origin[1]) > 3*scale for k in performance.keyframes)
    assert not check_vehicle_roots({'car': performance}, synth)
    route, error = synth._static_vehicle_route(start, goal, 'transport')
    assert not error
    for vertex in route:
        assert any(math.dist(vertex, k.position) < 1e-7 for k in performance.keyframes)


@pytest.mark.parametrize('failure', ['start', 'destination', 'unsupported', 'disconnected'])
def test_invalid_vehicle_routes_fail_before_synthesis(failure):
    manifest, scene, start, goal = world()
    if failure == 'start': start = [0,0,0]
    if failure == 'destination': goal = [0,0,0]
    if failure == 'unsupported': goal = [30,0,0]
    if failure == 'disconnected':
        scene['solid_boxes'][0] = {'owner': 'wall', 'min': [-2,0,-25], 'max': [2,4,25]}
    synth = MotionSynthesizer(manifest, scene_data=scene)
    beat = ResolvedBeat('travel','car','drive_to',0.,4.,4., {'target': goal})
    result, error = synth.synthesize_actor('car', {'asset_id':'transport','is_vehicle':True,'position':start},
        [beat], {'travel': ResolvedTarget(np.array(goal),0.,0.,'point','goal')}, {}, 4.)
    assert result is None
    assert error.error_code == 'VEHICLE_ROUTE_LIMIT'


def test_authored_route_and_stationary_vehicle_cannot_bypass_static_checks():
    manifest, scene, start, goal = world()
    synth = MotionSynthesizer(manifest, scene_data=scene)
    for points in ([start, goal], [[0,0,0], [0,0,0]]):
        performance = ActorPerformance('car', asset_id='transport', is_vehicle=True)
        performance.keyframes = [Keyframe(i, point, 0.) for i, point in enumerate(points)]
        conflicts = check_vehicle_roots({'car':performance}, synth)
        assert conflicts and conflicts[0]['actor'] == 'car'


@pytest.mark.parametrize('side', ['left', 'right', 'front', 'back'])
@pytest.mark.parametrize('scale', [1., 2.])
def test_vehicle_relative_destination_reserves_both_body_sizes(side, scale):
    from semantic_references import ReferenceResolver
    manifest, scene, start, goal = world(scale)
    states = {'first': {'asset_id': 'transport', 'is_vehicle': True, 'position': start},
              'second': {'asset_id': 'transport', 'is_vehicle': True, 'position': goal}}
    target, error = ReferenceResolver(manifest, scene).resolve(
        f'beside(second, {side})', states, source_actor_id='first')
    assert not error
    radius = math.hypot(2*scale, 4*scale)/2
    assert math.dist(target.position, goal) >= 2*radius + 1.3 - 1e-8


def test_drive_to_cannot_report_arrival_after_stopping_short_of_occupied_destination():
    manifest, scene, start, goal = world()
    scene['solid_boxes'] = []
    states = {'car': {'asset_id': 'transport', 'is_vehicle': True, 'position': start},
              'parked': {'asset_id': 'transport', 'is_vehicle': True, 'position': goal}}
    synth = MotionSynthesizer(manifest, scene_data=scene)
    beat = ResolvedBeat('travel', 'car', 'drive_to', 0., 4., 4., {'target': goal})
    result, error = synth.synthesize_actor('car', states['car'], [beat],
        {'travel': ResolvedTarget(np.array(goal), 0., 0., 'point', 'goal')}, {}, 4., all_initial_states=states)
    assert result is None
    assert error.error_code == 'VEHICLE_DESTINATION_CLEARANCE'


@pytest.mark.parametrize('obstacle_height', [.1, 2.])
def test_low_obstacles_are_not_treated_as_walkable_vehicle_clearance(obstacle_height):
    manifest, scene, start, goal = world()
    scene['solid_boxes'][0]['max'][1] = obstacle_height
    synth = MotionSynthesizer(manifest, scene_data=scene)
    route, error = synth._static_vehicle_route(start, goal, 'transport')
    assert not error and len(route) > 2


def test_vehicle_height_controls_overhead_clearance():
    manifest, scene, start, goal = world()
    scene['solid_boxes'][0]['min'][1] = 2.
    synth = MotionSynthesizer(manifest, scene_data=scene)
    route, error = synth._static_vehicle_route(start, goal, 'transport')
    assert not error and len(route) == 2
    manifest['assets'][0]['bounds_m'][1] = 3.
    route, error = MotionSynthesizer(manifest, scene_data=scene)._static_vehicle_route(start, goal, 'transport')
    assert not error and len(route) > 2


def test_vehicle_crossing_between_frame_samples_is_detected_continuously():
    manifest = {'assets': [{'id': 'small', 'tags': ['vehicle'], 'bounds_m': [.02, .02, .02]}]}
    other = ActorPerformance('crossing', asset_id='small', is_vehicle=True)
    other.keyframes = [Keyframe(0., [-1,0,0], 0.), Keyframe(.001, [-1,0,0], 0.),
                       Keyframe(.002, [1,0,0], 0.), Keyframe(1., [1,0,0], 0.)]
    states = {'car': {'asset_id':'small', 'is_vehicle':True, 'position':[0,0,0]},
              'crossing': {'asset_id':'small', 'is_vehicle':True, 'position':[-1,0,0]}}
    beat = ResolvedBeat('travel', 'car', 'drive_to', 0., 1., 1., {'target':[0,0,5]})
    result, error = MotionSynthesizer(manifest, fps=1).synthesize_actor(
        'car', states['car'], [beat],
        {'travel':ResolvedTarget(np.array([0.,0.,5.]),0.,0.,'point','goal')},
        {'crossing':other},1.,all_initial_states=states)
    assert result is None and error.error_code == 'MOVING_OBJECT_CLEARANCE'


def test_final_vehicle_check_includes_holds_after_shorter_tracks():
    manifest = {'assets': [{'id':'transport', 'bounds_m':[2.,1.,4.]}]}
    stopped = ActorPerformance('stopped', asset_id='transport', is_vehicle=True)
    stopped.keyframes = [Keyframe(0., [0,0,0], 0.), Keyframe(1., [0,0,0], 0.)]
    later = ActorPerformance('later', asset_id='transport', is_vehicle=True)
    later.keyframes = [Keyframe(0., [-12,0,0], 0.), Keyframe(2., [-12,0,0], 0.), Keyframe(4., [12,0,0], 0.)]
    conflicts = check_vehicle_roots({'stopped':stopped,'later':later}, MotionSynthesizer(manifest))
    assert conflicts and conflicts[0]['reason'] == 'vehicle_trajectory_overlap'
    assert conflicts[0]['time_s'] > 1.


def test_parked_vehicle_support_copy_does_not_collide_with_its_own_source():
    manifest, scene, start, goal = world()
    scene['solid_boxes'] = [{'owner':'car','min':[-1,0,-2], 'max':[1,1.5,2]}]
    performance = ActorPerformance('car', asset_id='transport', is_vehicle=True)
    performance.keyframes = [Keyframe(0.,[0,0,0],0.), Keyframe(2.,[0,0,0],0.)]
    assert not check_vehicle_roots({'car':performance}, MotionSynthesizer(manifest,scene_data=scene))
    # A distinct scene solid at the same location must still be checked.
    scene['solid_boxes'][0]['owner'] = 'wall'
    assert check_vehicle_roots({'car':performance}, MotionSynthesizer(manifest,scene_data=scene))
