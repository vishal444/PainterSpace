"""Scene-derived region contracts work across casts, geometry and regeneration."""
import copy
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools' / 'shot_library'), str(ROOT / 'tools' / 'collision')]
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from plan_model import ShotPlan, SpatialIntent
from plan_review import review_plan
from spatial_regions import derive_spatial_regions, region_contains, source_direction_ok, sample_region
from spatial_intents import bind_spatial_intents, validate_spatial_intents
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids


def fixture(count=3, scale=1., side=1, height=0.):
    manifest = {'assets': [{'id': 'character.human', 'tags': ['character'],
                            'bounds_m': [.7 * scale, 1.8 * scale, .7 * scale]},
                           {'id': 'environment.depot', 'tags': ['building']}]}
    scene = {'objects': [{'name': 'Depot', 'asset': 'environment.depot', 'position': [side * 14, height, 0]}],
             'support_surfaces': [{'owner': 'Floor', 'height_m': height,
                                   'polygon_xz': [[-20, -20], [20, -20], [20, 20], [-20, 20]]}],
             'solid_boxes': [{'owner': 'Depot', 'min': [12 if side > 0 else -16, height, -8],
                              'max': [16 if side > 0 else -12, height + 6, 8]}]}
    catalog = FunctionCatalog(manifest, scene)
    actors = [catalog.place('character.human', label=f'person_{i}')[1]['actor_id'] for i in range(count)]
    regions = catalog.list_spatial_regions()
    source = next(r['id'] for r in regions if r['kind'] == 'approach')
    destination = next(r['id'] for r in regions if r['kind'] == 'floor')
    return catalog, scene, actors, source, destination


def authored(catalog, actors, source, destination):
    ok, result = catalog.stage_region_approach('arrival', actors, source, destination)
    assert ok, result
    for actor in actors:
        assert catalog.dance(actor)[0]
    assert catalog.declare_interaction('performance', actors, [f'{a}.dance' for a in actors],
                                       timing='simultaneous', after=['arrival'])[0]
    return catalog.plan


def initial_states(catalog):
    return {s.actor_id: {'asset_id': s.asset_id, 'position': list(s.position or [0, 0, 0]),
                         'label': s.label, 'appearance': s.appearance, 'is_vehicle': False}
            for s in catalog.plan.setup}


@pytest.mark.parametrize('count,asset_id', [(1, 'character.human'), (7, 'cast.performer')])
def test_staging_provisions_fresh_cast_without_place_calls(count, asset_id):
    catalog, scene, _, source, destination = fixture()
    # The default is data, not an actor-name or built-in asset heuristic.
    manifest = copy.deepcopy(catalog.manifest)
    manifest['assets'][0]['id'] = asset_id
    manifest['assets'].append({'id': 'cast.alternative', 'tags': ['character']})
    manifest['defaults'] = {'character_asset_id': asset_id}
    catalog = FunctionCatalog(manifest, scene)
    planner = LLMShotPlanner(catalog)
    planner.camera_mode = 'manual'
    actors = [f'guest_{index}' for index in range(count)]
    calls = [{'function': 'stage_region_approach', 'args': {
        'intent_id': 'arrival', 'actors': actors, 'source_region': source,
        'destination_region': destination}},
        {'function': 'declare_interaction', 'args': {'interaction_id': 'performance',
         'participants': actors, 'actions': [f'{actor}.dance' for actor in actors],
         'timing': 'simultaneous', 'after': ['arrival']}}]
    calls.extend({'function': 'dance', 'args': {'actor': actor}} for actor in actors)
    ok, plan, logs = planner.plan_from_llm_json({'calls': calls})
    assert ok, logs
    assert [item.actor_id for item in plan.setup] == actors
    assert all(item.asset_id == asset_id for item in plan.setup)
    assert bind_spatial_intents(plan, initial_states(catalog), scene, manifest, ROOT) is None
    assert plan.interactions[0].timing == plan.interactions[1].timing == 'simultaneous'
    assert plan.interactions[1].after == ['arrival']


def test_staging_uses_existing_identity_and_requires_explicit_asset_for_additions():
    catalog, scene, _, source, destination = fixture()
    scene['existing_actors'] = [{'actor_id': 'resident_id', 'label': 'Resident',
        'asset_id': 'character.human', 'prim_path': '/World/Resident',
        'appearance': {'height_scale': 1.1}}]
    catalog.reset()
    before = catalog.plan.to_dict()
    ok, _ = catalog.stage_region_approach('arrival', ['Resident', 'resident_id'], source, destination)
    assert not ok
    assert not catalog.registry.list_actors() and catalog.plan.to_dict() == before
    ok, _ = catalog.stage_region_approach('arrival', ['Resident', 'typo'], source, destination)
    assert not ok
    assert not catalog.registry.list_actors() and catalog.plan.to_dict() == before
    ok, result = catalog.stage_region_approach('arrival', ['Resident', 'newcomer'], source,
                                               destination, asset_id='character.human')
    assert ok, result
    assert catalog.plan.spatial_intents[0].actors == ['resident_id', 'newcomer']
    assert catalog.registry.get('resident_id').prim_path == '/World/Resident'
    assert catalog.registry.get('resident_id').appearance.height_scale == 1.1
    assert len(catalog.plan.setup) == 2


@pytest.mark.parametrize('failure', ['ambiguous_asset', 'stale_region', 'duplicate', 'invalid_asset'])
def test_cast_provisioning_failures_are_atomic(failure):
    catalog, _, _, source, destination = fixture()
    catalog.reset()
    actors, kwargs = ['visitor'], {}
    if failure == 'ambiguous_asset':
        catalog.manifest['assets'].append({'id': 'another.performer', 'tags': ['character']})
    elif failure == 'stale_region':
        source = 'stale'
    elif failure == 'duplicate':
        actors = ['visitor', 'VISITOR']
    else:
        kwargs['asset_id'] = 'environment.depot'
    before = catalog.plan.to_dict()
    ok, _ = catalog.stage_region_approach('arrival', actors, source, destination, **kwargs)
    assert not ok
    assert catalog.plan.to_dict() == before and catalog.registry.list_actors() == []


def test_executor_stops_after_failed_staging_before_dependent_actions():
    catalog, _, _, _, destination = fixture()
    planner = LLMShotPlanner(catalog)
    ok, plan, logs = planner.plan_from_llm_json({'calls': [
        {'function': 'stage_region_approach', 'args': {'intent_id': 'arrival',
         'actors': ['visitor'], 'source_region': 'stale', 'destination_region': destination}},
        {'function': 'dance', 'args': {'actor': 'visitor'}}]})
    assert not ok
    assert not plan.setup and not plan.beats and not plan.interactions
    assert not any('Called dance' in entry for entry in logs)


@pytest.mark.parametrize('layout,count,scale,side', [
    ('opposite', 2, 1., 1), ('opposite', 7, 1.5, -1),
    ('opposite', 19, 1., 1), ('surrounding', 8, 1., -1)])
def test_general_origin_layouts_are_jointly_allocated(layout, count, scale, side):
    from spatial_regions import origin_layout_valid
    catalog, scene, actors, _, destination = fixture(count, scale, side)
    ok, result = catalog.stage_region_approach('arrival', actors, destination, destination, origin_layout=layout)
    assert ok, result
    states = initial_states(catalog)
    error = bind_spatial_intents(catalog.plan, states, scene, catalog.manifest, ROOT)
    assert error is None, error
    intent = catalog.plan.spatial_intents[0]
    regions = {r['id']: r for r in derive_spatial_regions(scene, catalog.manifest)}
    assert origin_layout_valid(intent,
        {a: regions[intent.bindings[a]['source_region']] for a in actors},
        {a: intent.bindings[a]['source'] for a in actors},
        {a: intent.bindings[a]['destination'] for a in actors})
    restored = ShotPlan.from_json(catalog.plan.to_json())
    assert restored.spatial_intents[0].origin_layout == layout


def test_explicit_per_actor_origins_and_ordered_corner_waypoints():
    catalog, scene, actors, _, destination = fixture(2)
    regions = derive_spatial_regions(scene, catalog.manifest)
    entries = [r for r in regions if r['kind'] == 'entry']
    west = next(r['id'] for r in entries if r['entry_normal'] == [-1, 0])
    east = next(r['id'] for r in entries if r['entry_normal'] == [1, 0])
    corners = [r['id'] for r in regions if r['kind'] == 'corner' and r['corner_normal'][1] == -1]
    assert catalog.stage_region_approach('arrival', actors, destination, destination,
                                        source_regions=[west, east], via_regions=corners)[0]
    states = initial_states(catalog)
    error = bind_spatial_intents(catalog.plan, states, scene, catalog.manifest, ROOT)
    assert error is None, error
    for actor, region in zip(actors, [west, east]):
        binding = catalog.plan.spatial_intents[0].bindings[actor]
        assert binding['source_region'] == region
        assert len(binding['via_points']) == len(corners)
        beat = next(b for b in catalog.plan.beats if b.actor_id == actor)
        assert beat.params['spatial_via_points'] == binding['via_points']


@pytest.mark.parametrize('prompt,kwargs,code', [
    ('People come from opposite sides.', {}, 'SPATIAL_ORIGIN_CONTRACT'),
    ('People come from all directions.', {}, 'SPATIAL_ORIGIN_CONTRACT'),
    ('People come around the corner.', {}, 'SPATIAL_ROUTE_CONTRACT')])
def test_review_rejects_missing_origin_or_route_contract(prompt, kwargs, code):
    catalog, scene, actors, source, destination = fixture()
    assert catalog.stage_region_approach('arrival', actors, source, destination, **kwargs)[0]
    catalog.plan.prompt = prompt
    report = review_plan(catalog.plan, scene, catalog.manifest)
    assert not report['passed']
    assert any(v['code'] == code for v in report['violations'])


def test_route_search_preserves_ordered_waypoints_and_shared_budget():
    from crowd.st_planner import plan_route
    from spatial_regions import visits_in_order
    start, goal, via = [-5, 0, 0], [5, 0, 0], [[-2, 0, 3], [2, 0, 3]]
    route = plan_route(start, goal, 0., 1.4, .3, [], lambda points: True,
                       (-10, 10, -10, 10), via=via)
    assert route.points is not None
    points = [list(p[1:]) for p in route.points]
    assert visits_in_order(points, via, 1e-6)
    assert not visits_in_order([start, goal], via, .4)
    assert not visits_in_order(points, list(reversed(via)), .4)
    blocked = plan_route(start, goal, 0., 1.4, .3, [],
                         lambda ps: all(p[2] < 2 for p in ps), (-10, 10, -10, 10), via=via)
    assert blocked.points is None


@pytest.mark.parametrize('count,scale,side,height', [(1, 1, 1, 0), (4, 1.5, -1, 2), (13, 1, 1, 0)])
def test_region_contract_allocates_the_cast_and_preserves_direction(count, scale, side, height):
    catalog, scene, actors, source, destination = fixture(count, scale, side, height)
    plan = authored(catalog, actors, source, destination)
    plan.prompt = 'People approach simultaneously from the direction of Depot, then dance together after everyone arrives.'
    before_scene = copy.deepcopy(scene)
    pending = review_plan(plan, scene, catalog.manifest)
    assert pending['passed'] and pending['pending_spatial_bindings']
    states = initial_states(catalog)
    error = bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT)
    assert error is None, error
    assert scene == before_scene
    report = review_plan(plan, scene, catalog.manifest, states)
    assert report['passed'] and not report['pending_spatial_bindings'], report
    regions = {r['id']: r for r in derive_spatial_regions(scene, catalog.manifest)}
    bindings = plan.spatial_intents[0].bindings
    for actor in actors:
        binding = bindings[actor]
        assert region_contains(regions[source], binding['source'], binding['source_solid_radius_m'], CrowdSolverConfig())
        assert region_contains(regions[destination], binding['destination'], binding['destination_solid_radius_m'], CrowdSolverConfig())
        assert source_direction_ok(regions[source], binding['source'], binding['destination'])
        assert clears_solids([binding['source']], scene['solid_boxes'], binding['source_solid_radius_m'] + .3)
        assert states[actor]['position'] == binding['source']
        assert binding['source'][1] == height
    for role, radius_key in [('source', 'source_radius_m'), ('destination', 'destination_radius_m')]:
        for i, actor in enumerate(actors):
            for other in actors[i + 1:]:
                assert math.dist(bindings[actor][role], bindings[other][role]) + 1e-7 >= bindings[actor][radius_key] + bindings[other][radius_key] + .3
    restored = ShotPlan.from_json(plan.to_json())
    assert bind_spatial_intents(restored, states, scene, catalog.manifest, ROOT) is None
    assert restored.spatial_intents[0].bindings == bindings


def test_inventory_ids_are_geometry_owned_stable_and_stale_ids_fail():
    catalog, scene, actors, source, destination = fixture()
    assert catalog.list_spatial_regions() == catalog.list_spatial_regions()
    descriptors = catalog.list_spatial_regions()
    assert all('faces' not in r and 'source_boxes' not in r for r in descriptors)
    before = catalog.plan.to_json()
    ok, error = catalog.stage_region_approach('bad', actors, 'invented', destination)
    assert not ok and error['error_code'] == 'SPATIAL_REGION_REFERENCE'
    assert catalog.plan.to_json() == before
    scene['solid_boxes'][0]['min'][0] += 1
    ok, error = catalog.stage_region_approach('bad', actors, source, destination)
    assert not ok and error['error_code'] == 'SPATIAL_REGION_REFERENCE'


@pytest.mark.parametrize('reason', ['locked', 'no_room', 'unknown_region', 'authored_route', 'cross_level'])
def test_failures_never_commit_partial_placements(reason):
    catalog, scene, actors, source, destination = fixture()
    plan = authored(catalog, actors, source, destination)
    states = initial_states(catalog)
    locked = []
    if reason == 'locked':
        locked = actors[:1]
    elif reason == 'no_room':
        asset = next(a for a in catalog.manifest['assets'] if a['id'] == 'character.human')
        asset['bounds_m'] = [100, 2, 100]
    elif reason == 'unknown_region':
        plan.spatial_intents[0].source_region = 'stale'
    elif reason == 'authored_route':
        plan.beats[0].params['brief_path'] = {'points': [[0, 0, 0], [1, 0, 0]]}
    else:
        scene['support_surfaces'].append({'owner': 'Upper', 'height_m': 3,
                                          'polygon_xz': [[-20, -20], [20, -20], [20, 20], [-20, 20]]})
        upper = next(r['id'] for r in derive_spatial_regions(scene) if r['kind'] == 'floor' and r['height_m'] == 3)
        plan.spatial_intents[0].destination_region = upper
    before = copy.deepcopy((plan.to_dict(), states, scene))
    error = bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT, locked_actors=locked)
    assert error is not None
    assert (plan.to_dict(), states, scene) == before


def test_explicit_coordinate_is_kept_or_conflict_is_reported():
    catalog, scene, actors, source, destination = fixture(1)
    plan = authored(catalog, actors, source, destination)
    plan.setup[0].position = [10, 0, -2]
    states = initial_states(catalog)
    assert bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT) is None
    assert plan.setup[0].position == [10, 0, -2]
    assert not plan.spatial_intents[0].bindings[actors[0]]['generated_start']
    plan.setup[0].position = [-18, 0, 0]
    before = plan.to_json()
    assert bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT) is not None
    assert plan.to_json() == before


def test_authored_source_reference_and_duration_remain_authoritative():
    from plan_model import TimingCondition
    catalog, scene, actors, source, destination = fixture(1)
    plan = authored(catalog, actors, source, destination)
    plan.setup[0].at_reference = 'point(10, 0, -2)'
    plan.beats[0].duration_condition = TimingCondition('for_duration', 12)
    states = initial_states(catalog)
    assert bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT) is None
    assert states[actors[0]]['position'] == [10, 0, -2]
    assert plan.setup[0].at_reference == 'point(10, 0, -2)'
    assert plan.beats[0].duration_condition.value == 12
    assert not plan.spatial_intents[0].bindings[actors[0]]['generated_start']
    assert not plan.spatial_intents[0].bindings[actors[0]]['generated_duration']


def test_uncovered_sources_are_not_advertised_as_approach_regions():
    catalog, scene, _, _, _ = fixture()
    scene['unprotected_scene_objects'] = ['Depot']
    assert not any(r['kind'] == 'approach' for r in catalog.list_spatial_regions())


def test_queue_shaped_calls_use_regions_and_defer_geometry_to_compiler():
    catalog, scene, _, source, destination = fixture()
    planner = LLMShotPlanner(FunctionCatalog(catalog.manifest, scene))
    labels = ['Alice', 'Ben']
    calls = [{'function': 'place', 'args': {'asset_id': 'character.human', 'label': label}} for label in labels]
    calls += [{'function': 'stage_region_approach', 'args': {'intent_id': 'arrive', 'actors': labels,
                                                           'source_region': source, 'destination_region': destination}}]
    calls += [{'function': 'dance', 'args': {'actor': label}} for label in labels]
    calls += [{'function': 'declare_interaction', 'args': {'interaction_id': 'perform', 'participants': labels,
                                                          'actions': [f'{label}.dance' for label in labels],
                                                          'timing': 'simultaneous', 'after': ['arrive']}}]
    ok, plan, log = planner.plan_from_llm_json({'calls': calls}, 'People approach from the direction of Depot, then dance together.')
    assert ok, log
    assert planner.last_plan_review['pending_spatial_bindings']
    assert len(plan.spatial_intents) == 1 and len(plan.beats) == 4
    tools = {item['function']['name']: item['function'] for item in planner.export_tool_schemas()}
    assert 'stage_region_approach' in tools and 'list_spatial_regions' in tools
    assert 'stage_region_approach' in planner.build_system_prompt()


def test_sampling_is_bounded_and_retry_variants_change_candidates():
    catalog, scene, _, _, destination = fixture()
    region = next(r for r in derive_spatial_regions(scene) if r['id'] == destination)
    config = CrowdSolverConfig(standing_samples=60)
    points = [spot for level in range(config.standing_search_levels) for spot in sample_region(region, scene, config, level)]
    assert len(points) <= config.standing_samples + 1  # optional measured preferred point
    assert sample_region(region, scene, config, 2) != sample_region(region, scene, config, 2, variant=1)


def test_floor_region_allows_walkover_lift_but_not_another_floor():
    catalog, scene, _, _, destination = fixture()
    region = next(r for r in derive_spatial_regions(scene) if r['id'] == destination)
    assert region_contains(region, [0, .17, 0], .35, CrowdSolverConfig())
    assert not region_contains(region, [0, 2, 0], .35, CrowdSolverConfig())


def test_rotated_geometry_and_gait_choices_are_not_scene_specific():
    results = {}
    for gait in ('walk', 'jog', 'run'):
        catalog, scene, actors, _, _ = fixture(3)
        angle = math.radians(35)
        def transform(x, z):
            return [17 + x * math.cos(angle) - z * math.sin(angle),
                    -9 + x * math.sin(angle) + z * math.cos(angle)]
        face = scene['support_surfaces'][0]
        face['polygon_xz'] = [transform(x, z) for x, z in face['polygon_xz']]
        obj = scene['objects'][0]
        x, z = transform(obj['position'][0], obj['position'][2])
        obj['position'] = [x, 0, z]
        box = scene['solid_boxes'][0]
        corners = [transform(x, z) for x in (box['min'][0], box['max'][0])
                   for z in (box['min'][2], box['max'][2])]
        box['min'] = [min(p[0] for p in corners), 0, min(p[1] for p in corners)]
        box['max'] = [max(p[0] for p in corners), 6, max(p[1] for p in corners)]
        regions = catalog.list_spatial_regions()
        source = next(r['id'] for r in regions if r['kind'] == 'approach')
        destination = next(r['id'] for r in regions if r['kind'] == 'floor')
        assert catalog.stage_region_approach('arrive', actors, source, destination, gait=gait)[0]
        states = initial_states(catalog)
        assert bind_spatial_intents(catalog.plan, states, scene, catalog.manifest, ROOT) is None
        results[gait] = max(beat.duration_condition.value for beat in catalog.plan.beats)
    assert results['run'] < results['jog'] < results['walk']


@pytest.mark.parametrize('boundary', [False, True])
def test_arrival_search_cannot_escape_destination_region(monkeypatch, boundary):
    from crowd.movements import route_movements
    import crowd.movements as movements
    from crowd_spacing import ActorEnvelopes
    from timeline_resolver import TimelineResolver
    catalog, scene, actors, source, destination = fixture(2)
    if boundary:
        regions = derive_spatial_regions(scene, catalog.manifest)
        destination = next(r['id'] for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [-1, 0])
    plan = authored(catalog, actors, source, destination)
    scene['support_surfaces'].append({'owner': 'Neighbor', 'height_m': 0,
                                      'polygon_xz': [[20, -20], [40, -20], [40, 20], [20, 20]]})
    states = initial_states(catalog)
    assert bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT) is None
    original = movements.allocate_destinations
    checked = []
    def allocate(requests, clear, occupied, config):
        assert not clear(requests[0], [30, 0, 0])
        if boundary:
            assert not clear(requests[0], [0, 0, 0])
        checked.append(True)
        return original(requests, clear, occupied, config)
    monkeypatch.setattr(movements, 'allocate_destinations', allocate)
    class Straight:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]
    _, error = route_movements(plan, states, TimelineResolver(), Straight(), scene,
                                ActorEnvelopes({actor: .35 for actor in actors}, {}))
    assert error is None and checked


@pytest.mark.parametrize('eventual_success', [False, True])
def test_layout_retries_rebuild_original_input_and_preserve_published_shot(tmp_path, monkeypatch, eventual_success):
    from compiler import ShotCompiler
    compiler = ShotCompiler(project_root=ROOT, crowd_config=CrowdSolverConfig(max_repair_attempts=1))
    catalog, _, actors, source, destination = fixture(2)
    plan = authored(catalog, actors, source, destination)
    original = plan.to_json()
    output = tmp_path / 'shot' / 'shot.usda'
    output.parent.mkdir()
    output.write_text('#usda 1.0\ndef Xform "Accepted" {}\n')
    before = output.read_text()
    attempts = []
    def compile_once(self, candidate, target, scene, validate):
        assert validate and candidate.to_json() == original
        assert output.read_text() == before
        attempts.append(self._spatial_layout_variant)
        target.write_text('#usda 1.0\ndef Xform "Candidate" {}\n')
        if eventual_success and self._spatial_layout_variant:
            return True, {'success': True, 'validation': {'success': True, 'collision_passed': True}}
        return False, {'success': False, 'error': {'error_code': 'CROWD_ROUTE_LIMIT'}}
    monkeypatch.setattr(ShotCompiler, '_compile_once', compile_once)
    ok, report = compiler.compile(plan, output)
    assert ok == eventual_success and attempts == [0, 1]
    assert plan.to_json() == original
    if eventual_success:
        assert output.read_text() != before
    else:
        assert output.read_text() == before
        assert report['crowd_repair']['stopped'] == 'spatial_layout_attempt_limit'


def test_post_synthesis_conformance_checks_actual_motion():
    from motion_synthesis import ActorPerformance, Keyframe
    from timeline_resolver import ResolvedTimeline, ResolvedBeat
    catalog, scene, actors, source, destination = fixture(1)
    plan = authored(catalog, actors, source, destination)
    states = initial_states(catalog)
    assert bind_spatial_intents(plan, states, scene, catalog.manifest, ROOT) is None
    actor = actors[0]
    binding = plan.spatial_intents[0].bindings[actor]
    beat = plan.spatial_intents[0].movements[actor]
    timeline = ResolvedTimeline(4, beats=[ResolvedBeat(beat, actor, 'walk_to', 0, 4, 4)])
    perf = ActorPerformance(actor_id=actor)
    perf.keyframes = [Keyframe(0, binding['source'], 0), Keyframe(4, binding['destination'], 0)]
    assert validate_spatial_intents(plan, timeline, {actor: perf}, scene, catalog.manifest) is None
    perf.keyframes[-1].position = [100, 0, 0]
    assert validate_spatial_intents(plan, timeline, {actor: perf}, scene, catalog.manifest).error_code == 'SPATIAL_INTENT_CONFORMANCE'


def test_final_conformance_rejects_shortcut_that_skips_corner():
    from motion_synthesis import ActorPerformance, Keyframe
    from timeline_resolver import ResolvedTimeline, ResolvedBeat
    catalog, scene, actors, source, destination = fixture(1)
    corner = next(r['id'] for r in derive_spatial_regions(scene, catalog.manifest) if r['kind'] == 'corner')
    assert catalog.stage_region_approach('arrival', actors, source, destination, via_regions=[corner])[0]
    # Fix a clear source away from the waypoint so the straight shortcut
    # actually skips it, even when generated starts favor clear corner approaches.
    catalog.plan.setup[0].position = [10, 0, -2]
    assert bind_spatial_intents(catalog.plan, initial_states(catalog), scene, catalog.manifest, ROOT) is None
    intent = catalog.plan.spatial_intents[0]
    actor = actors[0]
    binding = intent.bindings[actor]
    timeline = ResolvedTimeline(4, beats=[ResolvedBeat(intent.movements[actor], actor, 'walk_to', 0, 4, 4)])
    performance = ActorPerformance(actor_id=actor)
    performance.keyframes = [Keyframe(0, binding['source'], 0),
                             Keyframe(4, binding['destination'], 0)]
    error = validate_spatial_intents(catalog.plan, timeline, {actor: performance}, scene, catalog.manifest)
    assert error.error_code == 'SPATIAL_ROUTE_CONFORMANCE'
    performance.keyframes.insert(1, Keyframe(2, binding['via_points'][0], 0))
    assert validate_spatial_intents(catalog.plan, timeline, {actor: performance}, scene, catalog.manifest) is None


@pytest.mark.parametrize('kwargs', [
    {'source_regions': ['stale']}, {'via_regions': ['stale']},
    {'origin_layout': 'surrounding'}, {'source_regions': [] , 'origin_layout': 'unknown'}])
def test_invalid_general_arrival_declarations_do_not_create_cast(kwargs):
    catalog, _, _, source, destination = fixture()
    catalog.reset()
    ok, _ = catalog.stage_region_approach('arrival', ['guest_a', 'guest_b'], source, destination, **kwargs)
    assert not ok and not catalog.plan.setup and not catalog.registry.list_actors()


def test_new_spatial_tool_parameters_are_exposed_to_planners():
    catalog, _, _, _, _ = fixture()
    schema = next(t['function'] for t in LLMShotPlanner(catalog).export_tool_schemas()
                  if t['function']['name'] == 'stage_region_approach')['parameters']['properties']
    assert schema['source_regions']['items']['type'] == 'string'
    assert schema['via_regions']['items']['type'] == 'string'
    assert set(schema['origin_layout']['enum']) == {'specified', 'opposite', 'surrounding'}


def test_compound_corner_inventory_excludes_internal_and_duplicate_corners():
    from spatial_regions import region_descriptors
    catalog, scene, _, _, _ = fixture()
    scene['solid_boxes'].extend([copy.deepcopy(scene['solid_boxes'][0]),
        {'owner': 'Depot', 'min': [13, 0, -2], 'max': [15, 6, 2]}])
    corners = [r for r in derive_spatial_regions(scene, catalog.manifest) if r['kind'] == 'corner']
    assert len(corners) == 4
    compact = region_descriptors(corners)
    assert all('corner' in r and 'corner_normal' in r and 'faces' not in r for r in compact)
    assert len({r['id'] for r in compact}) == len(compact)


@pytest.mark.parametrize('mode,count', [('placed', 3), ('provisioned', 3), ('opposite', 3),
                                      ('opposite', 19), ('corner', 1), ('boundary_corner', 3), ('unpositioned', 3)])
def test_validated_compile_and_saved_plan_regeneration(tmp_path, mode, count):
    from pxr import Gf, Usd, UsdGeom
    from compiler import ShotCompiler
    stage = Usd.Stage.CreateNew(str(tmp_path / 'scene.usda'))
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(20, .1, 24))
    depot = UsdGeom.Cube.Define(stage, '/World/Depot')
    depot.AddTranslateOp().Set(Gf.Vec3d(14, 3, 0))
    depot.AddScaleOp().Set(Gf.Vec3f(2, 3, 8))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(tmp_path / 'scene.usda')
    assert not scene.get('read_error')
    catalog = FunctionCatalog(compiler.manifest, scene)
    actors = ([f'performer_{index}' for index in range(count)] if mode != 'placed' else
              [catalog.place('character.human')[1]['actor_id'] for _ in range(count)])
    source = next(r['id'] for r in catalog.list_spatial_regions() if r['kind'] == 'approach' and r['source_owner'] == 'Depot')
    destination = next(r['id'] for r in catalog.list_spatial_regions() if r['kind'] == 'floor' and r['owner'] == 'Floor')
    if mode in ('opposite', 'corner', 'boundary_corner'):
        kwargs = {'origin_layout': 'opposite'} if mode == 'opposite' else {}
        regions = derive_spatial_regions(scene, compiler.manifest)
        if mode in ('corner', 'boundary_corner'):
            source = next(r['id'] for r in regions if r['kind'] == 'entry'
                          and r['entry_normal'] == [1, 0] and r['support_region'] == destination)
            kwargs['via_regions'] = [next(r['id'] for r in regions if r['kind'] == 'corner'
                                         and r['source_owner'] == 'Depot' and r['corner_normal'] == normal
                                         and r['support_region'] == destination)
                                     for normal in ([1, -1], [-1, -1])]
        else:
            source = destination
        if mode == 'boundary_corner':
            destination = next(r['id'] for r in regions if r['kind'] == 'entry'
                               and r['entry_normal'] == [0, -1] and r['support_region'] == destination)
        assert catalog.stage_region_approach('arrival', actors, source, destination, **kwargs)[0]
        for actor in actors:
            catalog.dance(actor)
        catalog.declare_interaction('performance', actors, [f'{a}.dance' for a in actors],
                                    timing='simultaneous', after=['arrival'])
        plan = catalog.plan
        plan.prompt = ('People come from opposite sides, then dance together after everyone arrives.' if mode == 'opposite'
                       else 'People come around the corner, then dance together after everyone arrives.')
    elif mode == 'unpositioned':
        actors = [catalog.place('character.human')[1]['actor_id'] for _ in range(count)]
        for index, actor in enumerate(actors):
            catalog.walk_to(actor, [-8, 0, (index - (count-1)/2) * 4])
        plan = catalog.plan
    else:
        plan = authored(catalog, actors, source, destination)
        plan.prompt = 'People approach simultaneously from the direction of Depot, then dance together after everyone arrives.'
    for folder in ('first', 'replay'):
        output = tmp_path / folder / 'shot.usda'
        ok, report = compiler.compile(plan, output, scene_usda_path=tmp_path / 'scene.usda', validate=True)
        assert ok, repr(report)[:3500]
        assert report['validation']['collision_passed']
        if mode != 'unpositioned':
            assert report['validation']['spatial_intents']
        assert not report['validation']['plan_review']['pending_spatial_bindings']
        plan = ShotPlan.from_json(output.with_name('shot_plan.json').read_text())
        if mode == 'unpositioned':
            assert all(setup.position is not None for setup in plan.setup)
            continue
        assert plan.spatial_intents[0].source_region == source
        assert plan.spatial_intents[0].destination_region == destination
        region = next(r for r in derive_spatial_regions(scene, compiler.manifest) if r['id'] == destination)
        assert all(region_contains(region, plan.spatial_intents[0].bindings[a]['destination'],
                                   plan.spatial_intents[0].bindings[a]['destination_solid_radius_m'],
                                   compiler.crowd_config) for a in actors)


@pytest.mark.parametrize('normal,count,height', [([0, 1], 5, 0.), ([0, -1], 2, 3.),
                                                 ([-1, 0], 3, 0.), ([1, 0], 1, 0.)])
def test_boundary_arrivals_retain_selected_area(normal, count, height):
    catalog, scene, actors, _, _ = fixture(count, height=height)
    regions = derive_spatial_regions(scene, catalog.manifest)
    destination = next(r for r in regions if r['kind'] == 'entry' and r['entry_normal'] == normal)
    source = next(r for r in regions if r['kind'] == 'entry'
                  and r['entry_normal'] == [-v for v in normal])
    ok, result = catalog.stage_region_approach('travel', actors, source['id'], destination['id'])
    assert ok, result
    assert review_plan(catalog.plan, scene, catalog.manifest)['passed']
    assert bind_spatial_intents(catalog.plan, initial_states(catalog), scene, catalog.manifest, ROOT) is None
    intent = catalog.plan.spatial_intents[0]
    assert intent.destination_region == destination['id']
    for actor in actors:
        binding = intent.bindings[actor]
        assert region_contains(destination, binding['destination'], binding['destination_solid_radius_m'], CrowdSolverConfig())
        assert source_direction_ok(source, binding['source'], binding['destination'])
    assert ShotPlan.from_json(catalog.plan.to_json()).spatial_intents[0].destination_region == destination['id']


@pytest.mark.parametrize('kind', ['approach', 'corner', 'stale', 'unsupported_entry'])
def test_invalid_arrival_areas_fail_atomically_with_actionable_details(kind):
    catalog, scene, actors, source, floor = fixture()
    regions = derive_spatial_regions(scene, catalog.manifest)
    if kind == 'stale':
        destination = 'missing-region'
    else:
        destination = next(r['id'] for r in regions if r['kind'] == ('entry' if kind == 'unsupported_entry' else kind))
    if kind == 'unsupported_entry':
        next(r for r in regions if r['id'] == destination)['support_region'] = 'missing-floor'
        scene['spatial_regions'] = regions
    before = catalog.plan.to_json()
    ok, error = catalog.stage_region_approach('travel', actors, source, destination)
    assert not ok and error['error_code'] == 'SPATIAL_REGION_REFERENCE'
    assert error['details']['destination_region'] == destination
    assert error['details']['allowed_destination_kinds'] == ['floor', 'entry']
    assert catalog.plan.to_json() == before


def test_boundary_destination_origin_distribution_uses_underlying_floor():
    from spatial_regions import intent_source_ids
    catalog, scene, actors, _, floor = fixture(4)
    regions = {r['id']: r for r in derive_spatial_regions(scene, catalog.manifest)}
    destination = next(r for r in regions.values() if r['kind'] == 'entry')
    assert catalog.stage_region_approach('travel', actors, floor, destination['id'], origin_layout='surrounding')[0]
    intent = catalog.plan.spatial_intents[0]
    sources = intent_source_ids(intent, regions)
    assert len({tuple(regions[key]['entry_normal']) for key in sources.values()}) == 4
    assert all(regions[key]['support_region'] == floor for key in sources.values())


def test_boundary_final_conformance_rejects_arrival_elsewhere_on_same_floor():
    from motion_synthesis import ActorPerformance, Keyframe
    from timeline_resolver import ResolvedTimeline, ResolvedBeat
    catalog, scene, actors, _, _ = fixture(1)
    regions = derive_spatial_regions(scene, catalog.manifest)
    source = next(r['id'] for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [0, -1])
    destination = next(r['id'] for r in regions if r['kind'] == 'entry' and r['entry_normal'] == [0, 1])
    assert catalog.stage_region_approach('travel', actors, source, destination)[0]
    assert bind_spatial_intents(catalog.plan, initial_states(catalog), scene, catalog.manifest, ROOT) is None
    intent, actor = catalog.plan.spatial_intents[0], actors[0]
    binding = intent.bindings[actor]
    timeline = ResolvedTimeline(4, beats=[ResolvedBeat(intent.movements[actor], actor, 'walk_to', 0, 4, 4)])
    performance = ActorPerformance(actor_id=actor)
    performance.keyframes = [Keyframe(0, binding['source'], 0), Keyframe(4, binding['destination'], 0)]
    assert validate_spatial_intents(catalog.plan, timeline, {actor: performance}, scene, catalog.manifest) is None
    performance.keyframes[-1].position = [0, 0, 0]
    error = validate_spatial_intents(catalog.plan, timeline, {actor: performance}, scene, catalog.manifest)
    assert error.error_code == 'SPATIAL_INTENT_CONFORMANCE'
