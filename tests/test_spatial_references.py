"""Advertised region IDs work as body-sized setup and movement references."""
import copy
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))
from semantic_references import ReferenceResolver
from spatial_regions import derive_spatial_regions, region_contains
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids


def scene_fixture(scale=1., height=0.):
    scene = {'support_surfaces': [{'owner': 'Floor', 'height_m': height,
               'polygon_xz': [[-24*scale, -24*scale], [24*scale, -24*scale],
                              [24*scale, 24*scale], [-24*scale, 24*scale]]}],
             'solid_boxes': [], 'objects': []}
    manifest = {'assets': [{'id': 'person', 'tags': ['character'], 'bounds_m': [.6, 1.8, .6]},
                           {'id': 'transport', 'tags': ['vehicle'], 'bounds_m': [2., 1.5, 4.]}]}
    return manifest, scene


@pytest.mark.parametrize('normal', [[1, 0], [-1, 0], [0, 1], [0, -1]])
@pytest.mark.parametrize('asset,height', [('person', 0.), ('transport', 2.)])
def test_boundary_target_resolves_on_requested_side_for_mover_size(normal, asset, height):
    manifest, scene = scene_fixture(height=height)
    region = next(r for r in derive_spatial_regions(scene, manifest)
                  if r['kind'] == 'entry' and r['entry_normal'] == normal)
    states = {'mover': {'asset_id': asset, 'is_vehicle': asset == 'transport', 'position': [0, height, 0]}}
    target, error = ReferenceResolver(manifest, scene).resolve(region['id'], states, source_actor_id='mover')
    assert not error and target.target_type == 'region'
    assert target.target_name == region['id']
    assert region_contains(region, target.position, target.clearance_radius, CrowdSolverConfig())
    assert target.clearance_radius >= (math.hypot(2, 4)/2 if asset == 'transport' else .35)
    assert target.facing_deg == pytest.approx((math.degrees(math.atan2(normal[0], -normal[1]))) % 360)


def test_blocked_preferred_point_uses_another_clear_point_inside_same_region():
    manifest, scene = scene_fixture()
    region = next(r for r in derive_spatial_regions(scene, manifest) if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    scene['solid_boxes'] = [{'owner': 'Obstacle', 'min': [-4, 0, 17], 'max': [4, 5, 23]}]
    states = {'mover': {'asset_id': 'transport', 'is_vehicle': True, 'position': [0, 0, 0]}}
    target, error = ReferenceResolver(manifest, scene).resolve(region['id'], states, source_actor_id='mover')
    assert not error
    assert region_contains(region, target.position, target.clearance_radius, CrowdSolverConfig())
    assert clears_solids([target.position], scene['solid_boxes'], target.clearance_radius + .3)


def test_region_cannot_silently_widen_for_a_large_vehicle():
    manifest, scene = scene_fixture(scale=.2)
    region = next(r for r in derive_spatial_regions(scene, manifest) if r['kind'] == 'entry')
    target, error = ReferenceResolver(manifest, scene).resolve(region['id'], {'v': {'asset_id': 'transport'}}, source_actor_id='v')
    assert target is None and 'not widened' in error


def test_stale_region_does_not_bind_to_changed_floor_geometry():
    manifest, scene = scene_fixture()
    reference = next(r['id'] for r in derive_spatial_regions(scene, manifest) if r['kind'] == 'entry')
    changed = copy.deepcopy(scene)
    changed['support_surfaces'][0]['height_m'] = 3.
    target, error = ReferenceResolver(manifest, changed).resolve(reference, {})
    assert target is None and error


def test_resolution_cache_distinguishes_mover_size_and_live_position():
    manifest, scene = scene_fixture()
    reference = next(r['id'] for r in derive_spatial_regions(scene, manifest)
                     if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    resolver = ReferenceResolver(manifest, scene)
    first, error = resolver.resolve(reference, {'a': {'asset_id': 'person', 'position': [-8, 0, 0]}}, source_actor_id='a')
    second, error = resolver.resolve(reference, {'a': {'asset_id': 'transport', 'position': [8, 0, 0]}}, source_actor_id='a')
    assert not error
    assert first.position[0] < second.position[0]
    assert first.clearance_radius < second.clearance_radius
    expanded, error = resolver.resolve(reference, {'a': {'asset_id': 'person', 'position': [-8, 0, 0]}},
                                       source_actor_id='a', clearance_radius_m=2.)
    assert not error and expanded.clearance_radius >= 2.


def test_timeline_preserves_vehicle_size_when_resolving_region_targets():
    from timeline_resolver import TimelineResolver
    from plan_model import Beat, ShotPlan
    manifest, scene = scene_fixture(scale=.2)
    reference = next(r['id'] for r in derive_spatial_regions(scene, manifest) if r['kind'] == 'entry')
    states = {'v': {'asset_id': 'transport', 'is_vehicle': True, 'position': [0, 0, 0]}}
    resolver = TimelineResolver(ref_resolver=ReferenceResolver(manifest, scene))
    tracks = {'v': [(0., 0., 0., 0.)]}
    # A failed provisional reference remains unresolved; it must not resolve
    # using a smaller generic-person footprint after asset metadata is lost.
    resolver._update_provisional_trajectory('v', 0., 2., Beat('drive', 'drive_to', 'v', {'target': reference}), tracks, states)
    target, error = resolver.ref_resolver.resolve(reference, states, source_actor_id='v')
    assert target is None and error
    assert resolver.ref_resolver._spatial_resolver.cache == {}


@pytest.mark.parametrize('asset_id', ['cars.car1', 'cars.car2'])
def test_boundary_placement_and_drive_compile_and_regenerate(tmp_path, asset_id):
    from compiler import ShotCompiler
    from plan_model import Beat, SetupAction, ShotPlan, TimingCondition
    from pxr import Gf, Usd, UsdGeom
    path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(24, .1, 24))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(ROOT)
    scene = compiler._read_scene_context(path)
    regions = derive_spatial_regions(scene, compiler.manifest)
    source = next(r for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [-1, 0])
    destination = next(r for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    plan = ShotPlan(setup=[SetupAction('place', 'v', asset_id, at_reference=source['id'], facing_reference='east')],
                    beats=[Beat('center', 'drive_to', 'v', {'target': [0, 0, 0]},
                                duration_condition=TimingCondition('for_duration', 3)),
                           Beat('south', 'drive_to', 'v', {'target': destination['id']},
                                duration_condition=TimingCondition('for_duration', 3))])
    for folder in ('first', 'replay'):
        output = tmp_path / folder / 'shot.usda'
        ok, report = compiler.compile(plan, output, path, validate=True)
        assert ok, report
        assert report['validation']['collision_passed']
        saved = ShotPlan.from_json(output.with_name('shot_plan.json').read_text())
        assert saved.setup[0].at_reference == source['id']
        assert saved.beats[1].params['target'] == destination['id']
        from pxr import UsdGeom
        shot = Usd.Stage.Open(str(output))
        # Final compiled vehicle roots remain inside the selected south band.
        root = next(p for p in shot.Traverse() if p.GetCustomDataByKey('actor_id') == 'v')
        end = shot.GetEndTimeCode()
        position = UsdGeom.Xformable(root).ComputeLocalToWorldTransform(end).ExtractTranslation()
        assert region_contains(destination, position, 0., CrowdSolverConfig())
        plan = saved


def test_unknown_setup_reference_fails_instead_of_falling_back_to_origin(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction, ShotPlan
    plan = ShotPlan(setup=[SetupAction('place', 'a', 'character.human', at_reference='Missing:entry_unknown')])
    ok, report = ShotCompiler(ROOT).compile(plan, tmp_path / 'shot.usda', validate=False)
    assert not ok
    assert report['error']['error_code'] == 'UNRESOLVED_REFERENCE'
    assert report['error']['details']['role'] == 'starting_location'


@pytest.mark.parametrize('compass', [False, True])
def test_ordinary_walk_arrival_alternatives_remain_inside_boundary(monkeypatch, compass):
    import crowd.movements as movements
    from crowd.movements import route_movements
    from crowd_spacing import ActorEnvelopes
    from timeline_resolver import TimelineResolver
    from plan_model import Beat, ShotPlan, TimingCondition
    manifest, scene = scene_fixture()
    reference = next(r['id'] for r in derive_spatial_regions(scene, manifest)
                     if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    plan = ShotPlan(beats=[Beat('travel', 'walk_to', 'a', {'target': 'south' if compass else reference},
                                duration_condition=TimingCondition('for_duration', 15))])
    states = {'a': {'asset_id': 'person', 'position': [0, 0, 0]}}
    original = movements.allocate_destinations
    checks = []
    def allocate(requests, clear, occupied, config):
        assert requests[0]['direction_region'] if compass else requests[0]['destination_region'] == reference
        assert not clear(requests[0], [0, 0, 0])
        checks.append(True)
        return original(requests, clear, occupied, config)
    monkeypatch.setattr(movements, 'allocate_destinations', allocate)
    class Straight:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, start, goal, obstacles): return [start, goal]
    resolver = TimelineResolver(ref_resolver=ReferenceResolver(manifest, scene))
    routed, error = route_movements(plan, states, resolver, Straight(), scene, ActorEnvelopes({'a': .35}, {}))
    assert error is None and checks


def test_region_resolution_uses_configured_boundary_geometry():
    manifest, scene = scene_fixture(scale=2.)
    config = CrowdSolverConfig(entry_region_fraction=.1)
    region = next(r for r in derive_spatial_regions(scene, manifest, config) if r['kind'] == 'entry')
    resolver = ReferenceResolver(manifest, scene, crowd_config=config)
    target, error = resolver.resolve(region['id'], {'v': {'asset_id': 'transport'}}, source_actor_id='v')
    assert not error
    assert region_contains(region, target.position, target.clearance_radius, config)


@pytest.mark.parametrize('direction,normal', [
    ('east', (1, 0)), ('west', (-1, 0)), ('north', (0, -1)), ('south', (0, 1)),
    ('north_east', (1, -1)), ('southwest', (-1, 1))])
@pytest.mark.parametrize('scale,height', [(1., 0.), (2., 3.)])
def test_compass_locations_use_measured_supported_body_clear_regions(direction, normal, scale, height):
    manifest, scene = scene_fixture(scale=scale, height=height)
    # Translated geometry must not resolve around the world origin.
    for face in scene['support_surfaces']:
        face['polygon_xz'] = [[x + 80, z - 60] for x, z in face['polygon_xz']]
    states = {'v': {'asset_id': 'transport', 'is_vehicle': True}}
    resolver = ReferenceResolver(manifest, scene)
    target, error = resolver.resolve(direction, states, source_actor_id='v')
    assert not error, error
    region = resolver._spatial_resolver.regions[target.target_name]
    assert region_contains(region, target.position, target.clearance_radius, CrowdSolverConfig())
    assert target.position[1] == height
    assert all(sign == 0 or (target.position[axis] - center) * sign > 0
               for axis, center, sign in [(0, 80, normal[0]), (2, -60, normal[1])])
    assert target.clearance_radius >= math.hypot(2, 4) / 2


def test_direction_fails_without_geometry_or_when_entire_side_is_blocked():
    manifest, scene = scene_fixture()
    for geometry in ({}, dict(scene, solid_boxes=[
            {'owner': 'wall', 'min': [10, 0, -30], 'max': [30, 5, 30]}])):
        target, error = ReferenceResolver(manifest, geometry).resolve(
            'east', {'v': {'asset_id': 'transport', 'is_vehicle': True}}, source_actor_id='v')
        assert target is None
        assert 'No clear supported east region' in error


def test_direction_search_avoids_blocked_preferred_point():
    manifest, scene = scene_fixture()
    scene['solid_boxes'] = [{'owner': 'wall', 'min': [15, 0, -4], 'max': [24, 5, 4]}]
    resolver = ReferenceResolver(manifest, scene)
    target, error = resolver.resolve('east', {'v': {'asset_id': 'transport', 'is_vehicle': True}}, source_actor_id='v')
    assert not error
    assert clears_solids([target.position], scene['solid_boxes'], target.clearance_radius + CrowdSolverConfig().buffer_m)


def test_vehicle_directional_placement_does_not_step_over_low_geometry():
    from vehicle_routes import VehicleRoutes
    manifest, scene = scene_fixture()
    scene['solid_boxes'] = [{'owner': 'curb', 'min': [15, 0, -4], 'max': [24, .1, 4]}]
    target, error = ReferenceResolver(manifest, scene).resolve(
        'east', {'v': {'asset_id': 'transport', 'is_vehicle': True}}, source_actor_id='v')
    assert not error
    assert VehicleRoutes(scene, CrowdSolverConfig()).clear_vehicle(
        tuple(target.position), tuple(target.position), target.clearance_radius, 1.5)


def test_compass_facing_does_not_need_a_navigable_destination():
    resolver = ReferenceResolver({'assets': []}, {})
    states = {'a': {'position': [50, 3, -20]}}
    target, error = resolver.resolve_facing('west', states, source_actor_id='a')
    assert not error
    assert target.facing_deg == 270.
    assert target.position == pytest.approx([49, 3, -20])


def test_region_cache_distinguishes_vehicle_heights_with_same_footprint():
    manifest, scene = scene_fixture()
    manifest['assets'].append({'id':'tall', 'tags':['vehicle'], 'bounds_m':[2.,3.,4.]})
    scene['solid_boxes'] = [{'owner':'ceiling','min':[10,2,-30], 'max':[30,4,30]}]
    resolver = ReferenceResolver(manifest,scene)
    short, error = resolver.resolve('east', {'v':{'asset_id':'transport','is_vehicle':True}}, source_actor_id='v')
    assert not error and short is not None
    tall, error = resolver.resolve('east', {'v':{'asset_id':'tall','is_vehicle':True}}, source_actor_id='v')
    assert tall is None and error
