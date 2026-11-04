"""Automatic departures prefer reachable routes without moving authored starts."""
import copy
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids
from plan_model import Beat, SetupAction, ShotPlan
from route_placement import PlacementRoutes, place_unpositioned_movers


def layout(scale=1., vehicle=False):
    manifest = {'assets': [{'id': 'mover', 'tags': ['vehicle' if vehicle else 'character'],
                            'bounds_m': [scale, 2*scale, 2*scale]}]}
    scene = {'support_surfaces': [{'owner': 'Floor', 'height_m': 0.,
              'polygon_xz': [[-15*scale, -10*scale], [15*scale, -10*scale],
                             [15*scale, 10*scale], [-15*scale, 10*scale]]}],
             'solid_boxes': [{'owner': 'Barrier', 'min': [-8*scale, 0, -3*scale],
                              'max': [-2*scale, 4*scale, 3*scale]}]}
    plan = ShotPlan(setup=[SetupAction('place', 'a', asset_id='mover')],
                    beats=[Beat('travel', 'drive_to' if vehicle else 'walk_to', 'a',
                                {'target': [8*scale, 0, 0]})])
    states = {'a': {'asset_id': 'mover', 'position': [-6*scale, 0, 0], 'is_vehicle': vehicle}}
    return manifest, scene, plan, states


@pytest.mark.parametrize('scale,vehicle', [(1., False), (2., False), (1., True), (1.5, True)])
def test_new_movers_get_clear_reachable_departures(scale, vehicle):
    manifest, scene, plan, states = layout(scale, vehicle)
    config = CrowdSolverConfig()
    assert place_unpositioned_movers(plan, states, scene, manifest, ROOT, config) is None
    point = states['a']['position']
    assert point != [-6*scale, 0, 0]
    radius = math.hypot(scale, 2*scale)/2 if vehicle else scale
    assert clears_solids([point, plan.beats[0].params['target']], scene['solid_boxes'], radius + config.buffer_m)
    assert plan.setup[0].position == point
    assert math.dist(point, plan.beats[0].params['target']) > radius


@pytest.mark.parametrize('authority', ['coordinates', 'reference', 'locked', 'path', 'window', 'seat'])
def test_authored_or_locked_start_is_never_relocated(authority):
    manifest, scene, plan, states = layout()
    if authority == 'coordinates': plan.setup[0].position = [-6, 0, 0]
    if authority == 'reference': plan.setup[0].at_reference = 'custom.origin'
    if authority == 'path': plan.beats[0].params['brief_path'] = {'points': [[-6, 0, 0], [8, 0, 0]]}
    if authority == 'window': plan.beats[0].params['exact_window'] = True
    if authority == 'seat': states['a']['seated_vehicle'] = 'host'
    before = copy.deepcopy((plan.to_dict(), states))
    assert place_unpositioned_movers(plan, states, scene, manifest, ROOT, CrowdSolverConfig(),
                                     locked_actors={'a'} if authority == 'locked' else ()) is None
    assert (plan.to_dict(), states) == before


def test_failed_start_assignment_is_atomic():
    manifest, scene, plan, states = layout()
    scene['solid_boxes'] = [{'owner': 'Block', 'min': [-20, 0, -20], 'max': [20, 10, 20]}]
    before = copy.deepcopy((plan.to_dict(), states))
    error = place_unpositioned_movers(plan, states, scene, manifest, ROOT, CrowdSolverConfig())
    assert error.error_code == 'START_ROUTE_LIMIT'
    assert (plan.to_dict(), states) == before


def test_clear_start_is_preferred_to_nearer_blocked_route():
    _, scene, _, _ = layout()
    routes = PlacementRoutes(scene, CrowdSolverConfig())
    goal = (8., 0., 0.)
    assert routes.cost((-1., 0., 0.), [goal], .4) < routes.cost((-9., 0., 0.), [goal], .4)
    assert routes.reachable((-1., 0., 0.), (goal,), .4, 1.4)


@pytest.mark.parametrize('count,scale,side', [(1, 1., 1), (4, 1., 1), (3, 2., -1)])
def test_region_starts_approach_corner_instead_of_aligning_with_final_goal(count, scale, side):
    from function_catalog import FunctionCatalog
    from spatial_regions import derive_spatial_regions
    from spatial_intents import bind_spatial_intents
    manifest = {'assets': [{'id': 'person', 'tags': ['character'], 'bounds_m': [.6, 1.8, .6]}]}
    scene = {'support_surfaces': [{'owner': 'Floor', 'height_m': 0.,
                                  'polygon_xz': [[-30*scale, -30*scale], [30*scale, -30*scale],
                                                 [30*scale, 30*scale], [-30*scale, 30*scale]]}],
             'objects': [{'name': 'Block', 'asset': 'wall', 'position': [-10*side*scale, 0, 10*scale]}],
             'solid_boxes': [{'owner': 'Block', 'min': [min(-30*side, -4*side)*scale, 0, 6*scale],
                              'max': [max(-30*side, -4*side)*scale, 5*scale, 22*scale]}]}
    catalog = FunctionCatalog(manifest, scene)
    actors = [catalog.place('person')[1]['actor_id'] for _ in range(count)]
    regions = derive_spatial_regions(scene, manifest)
    source = next(r['id'] for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [-side, 0])
    destination = next(r['id'] for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    corner = next(r['id'] for r in regions if r['kind'] == 'corner' and r['corner_normal'] == [side, -1])
    assert catalog.stage_region_approach('arrive', actors, source, destination, via_regions=[corner])[0]
    states = {actor: {'asset_id': 'person', 'position': [0, 0, 0]} for actor in actors}
    assert bind_spatial_intents(catalog.plan, states, scene, manifest, ROOT) is None
    routes = PlacementRoutes(scene, CrowdSolverConfig())
    reservations = []
    for actor in actors:
        binding = catalog.plan.spatial_intents[0].bindings[actor]
        stops = [binding['source'], *binding['via_points'], binding['destination']]
        assert all(routes.clear(tuple(a), tuple(b), binding['source_solid_radius_m'])
                   for a, b in zip(stops, stops[1:]))
        setup = next(s for s in catalog.plan.setup if s.actor_id == actor)
        a, b = stops[:2]
        assert setup.facing_deg == pytest.approx(math.degrees(math.atan2(b[0]-a[0], a[2]-b[2])) % 360)
        from crowd.st_planner import Reservation
        elapsed, points = 0., [(0., *stops[0])]
        for a, b in zip(stops, stops[1:]):
            elapsed += math.dist(a, b) / 1.4
            points.append((elapsed, *b))
        reservations.append(Reservation(actor, points, binding['source_radius_m']))
    from crowd.joint import first_conflict
    assert first_conflict(reservations, 0., max(r.points[-1][0] for r in reservations), CrowdSolverConfig()) is None


@pytest.mark.parametrize('asset_id', ['cars.car1', 'cars.car2'])
@pytest.mark.parametrize('reference', [None, 'east', 'west'])
def test_unpositioned_vehicle_compiles_from_clear_reachable_start(tmp_path, asset_id, reference):
    import json
    from pxr import Gf, Usd, UsdGeom
    from compiler import ShotCompiler
    from plan_model import TimingCondition
    path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(20, .1, 20))
    obstacle = UsdGeom.Cube.Define(stage, '/World/Obstacle')
    obstacle.AddTranslateOp().Set(Gf.Vec3d(0, 2, 0))
    obstacle.AddScaleOp().Set(Gf.Vec3f(3, 2, 3))
    stage.GetRootLayer().Save()
    plan = ShotPlan(setup=[SetupAction('place', 'vehicle', asset_id=asset_id,
                                          at_reference=reference, facing_reference='west')],
                    beats=[Beat('move', 'drive_to', 'vehicle', {'target': [-12, 0, 0]},
                                TimingCondition('at', 0), TimingCondition('for_duration', 3))])
    output = tmp_path / 'shot' / 'shot.usda'
    ok, report = ShotCompiler(ROOT).compile(plan, output, path, validate=True)
    assert ok, json.dumps(report, indent=2)
    assert report['validation']['collision_passed']
    saved = ShotPlan.from_json(output.with_name('shot_plan.json').read_text())
    assert saved.setup[0].position != [0, 0, 0]
    assert saved.setup[0].position is not None


@pytest.mark.parametrize('direction,sign,scale', [('east', 1, 1.), ('west', -1, 2.)])
def test_directional_starts_participate_in_reachable_placement_search(direction, sign, scale):
    manifest, scene, plan, states = layout(scale, vehicle=True)
    plan.setup[0].at_reference = direction
    states['a']['position'] = [12 * scale * sign, 0, 0]
    scene['solid_boxes'] = [{'owner': 'wall', 'min': [9*scale*sign-2*scale, 0, -3*scale],
                            'max': [9*scale*sign+2*scale, 4*scale, 3*scale]}]
    plan.beats[0].params['target'] = [0, 0, 0]
    config = CrowdSolverConfig()
    assert place_unpositioned_movers(plan, states, scene, manifest, ROOT, config) is None
    point = states['a']['position']
    assert point[0] * sign > 0
    assert point != [12 * scale * sign, 0, 0]
    radius = math.hypot(scale, 2*scale)/2
    assert PlacementRoutes(scene, config).clear(tuple(point), (0, 0, 0), radius)
    assert plan.setup[0].at_reference == direction


def test_vehicle_start_search_uses_the_same_detour_as_synthesis():
    manifest, scene, plan, states = layout(vehicle=True)
    plan.setup[0].at_reference = 'east'
    states['a']['position'] = [12, 0, 0]
    plan.beats[0].params['target'] = [-10, 0, 0]
    # No direct departure fits; the shared vehicle planner must supply a detour.
    scene['solid_boxes'] = [{'owner': 'wall', 'min': [-2, 0, -5], 'max': [2, 4, 5]}]
    error = place_unpositioned_movers(plan, states, scene, manifest, ROOT, CrowdSolverConfig())
    assert error is None
    from motion_synthesis import MotionSynthesizer
    route, error = MotionSynthesizer(manifest, scene_data=scene)._static_vehicle_route(
        states['a']['position'], plan.beats[0].params['target'], 'mover')
    assert not error and len(route) > 2
    assert clears_solids(route, scene['solid_boxes'], math.hypot(1, 2)/2 + CrowdSolverConfig().buffer_m)
