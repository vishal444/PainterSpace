"""Crowd spacing uses supported scene spots and the actors' motion envelopes."""
import math
import copy
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools" / "shot_library"), str(ROOT / "tools" / "collision")]

from crowd_spacing import (MOTION_CLEARANCE_BUFFER_M, PAIR_CENTER_SEPARATION_M,
                           _actor_reach, space_standing_actors)
from plan_model import Beat, SetupAction, ShotPlan


@pytest.mark.parametrize('count,scale,heading,height', [(2, 1., 0., 0.),
                                                       (6, .7, 90., 2.),
                                                       (4, 2., 35., 0.)])
def test_initial_layout_uses_measured_floor_and_action_reach(count, scale, heading, height, monkeypatch):
    from crowd.config import CrowdSolverConfig
    from crowd.geometry import supported_disc, clears_solids
    from crowd_spacing import _actor_reach
    from semantic_references import ReferenceResolver
    angle = math.radians(heading)

    def world(x, z):
        return [scale * (x * math.cos(angle) - z * math.sin(angle)) + 17,
                height, scale * (x * math.sin(angle) + z * math.cos(angle)) - 9]

    original = world(-.1, 0)
    scene = {'objects': [{'name': 'Venue', 'heading_deg': heading}],
             'standing_spots': [{'owner': 'Venue', 'name': 'standing_left_01',
                                 'position': original}],
             'support_surfaces': [{'owner': 'Venue', 'height_m': height,
                                   'polygon_xz': [[p[0], p[2]] for p in
                                                  [world(-12, -8), world(12, -8),
                                                   world(12, 8), world(-12, 8)]]}]}
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'] * count)
    for state in states.values():
        state['facing_deg'] = heading
    # The animation, rather than just the small standing body, needs this room.
    monkeypatch.setattr('crowd_spacing.beat_reach', lambda *args, **kwargs: 1.2 * scale)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [.6 * scale, 1.8, .6 * scale]}]}
    unchanged_scene = copy.deepcopy(scene)
    config = CrowdSolverConfig()
    assert space_standing_actors(plan, states, scene, manifest, ROOT, crowd_config=config) is None
    assert scene == unchanged_scene
    points = [state['position'] for state in states.values()]
    assert all(supported_disc(p, 1.2 * scale, scene['support_surfaces'], config) for p in points)
    assert all((p[0] - 17) * math.cos(angle) + (p[2] + 9) * math.sin(angle) < 0 for p in points)
    assert all(p[1] == height for p in points)
    reach = _actor_reach('human_1', 'character.human', plan, manifest, ROOT)
    assert all(math.dist(a, b) >= 2 * reach + config.buffer_m
               for i, a in enumerate(points) for b in points[i + 1:])
    # Save/reload contains real coordinates, never invented scene references.
    restored = ShotPlan.from_dict(plan.to_dict())
    assert any(setup.position is not None for setup in restored.setup)
    resolver = ReferenceResolver(manifest, scene)
    for setup in restored.setup:
        if setup.at_reference:
            resolved, error = resolver.resolve(setup.at_reference, states)
            assert not error
            assert list(resolved.position) == states[setup.actor_id]['position']
        else:
            assert setup.position == states[setup.actor_id]['position']
            assert setup.facing_deg == heading


def test_floor_fallback_avoids_solids_and_locked_actors():
    from crowd.config import CrowdSolverConfig
    from crowd.geometry import clears_solids
    scene = _scene({'left': [(-1, 0)]})
    scene['support_surfaces'] = [{'owner': 'Venue', 'height_m': 0,
                                  'polygon_xz': [[-10, -6], [10, -6], [10, 6], [-10, 6]]}]
    scene['solid_boxes'] = [{'min': [-2, 0, -1], 'max': [0, 3, 1]}]
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'] * 3)
    states['human_3']['position'] = [-4, 0, 0]
    before_locked = copy.deepcopy(states['human_3'])
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}]}
    assert space_standing_actors(plan, states, scene, manifest, ROOT, locked_actors={'human_3'}) is None
    assert states['human_3'] == before_locked
    assert plan.setup[2].at_reference == 'Venue:standing_left_01'
    config = CrowdSolverConfig()
    for actor in ('human_1', 'human_2'):
        point = states[actor]['position']
        assert clears_solids([point], scene['solid_boxes'], 1 + config.buffer_m)
        assert math.dist(point, states['human_3']['position']) >= 2 + config.buffer_m


@pytest.mark.parametrize('reason', ['unsupported', 'solids', 'occupied', 'pair_conflicts'])
def test_failed_floor_search_reports_rejections_without_partial_edits(reason):
    from crowd.config import CrowdSolverConfig
    scene = _scene({'left': [(-.5, 0)]})
    scene['support_surfaces'] = [{'owner': 'Venue', 'height_m': 0,
                                  'polygon_xz': [[-2, -1.2], [2, -1.2], [2, 1.2], [-2, 1.2]]}]
    count = 2 if reason == 'pair_conflicts' else 1
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'] * count)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}]}
    if reason == 'unsupported':
        manifest['assets'][0]['bounds_m'] = [6, 1.8, 6]
    elif reason == 'solids':
        scene['solid_boxes'] = [{'min': [-2, 0, -2], 'max': [2, 3, 2]}]
    elif reason == 'occupied':
        states['fixed'] = {'asset_id': 'character.human', 'position': [-1, 0, 0]}
    original_plan, original_states, original_scene = copy.deepcopy((plan.to_dict(), states, scene))
    config = CrowdSolverConfig(standing_samples=100, standing_search_levels=2)
    error = space_standing_actors(plan, states, scene, manifest, ROOT, crowd_config=config)
    assert error.error_code == 'CROWD_SPACING'
    details = error.details
    assert len(details['search_attempts']) == 3
    if reason in ('unsupported', 'solids'):
        assert details['geometry_rejections'][reason] > 0
    elif reason == 'occupied':
        assert any(group['occupied'] for attempt in details['search_attempts']
                   for group in attempt['groups'].values())
    else:
        assert any(attempt['pair_conflicts'] for attempt in details['search_attempts'])
    assert plan.to_dict() == original_plan and states == original_states and scene == original_scene


def test_floor_sampler_has_a_shared_bound_and_does_not_borrow_another_floor():
    from crowd.config import CrowdSolverConfig
    from crowd.standing_layout import floor_candidates
    config = CrowdSolverConfig(standing_samples=37)
    scene = {'support_surfaces': [
        {'owner': owner, 'height_m': height,
         'polygon_xz': [[-20, -20], [20, -20], [20, 20], [-20, 20]]}
        for owner, height in [('Venue', 0), ('Venue', 3), ('Other', 0)]]}
    samples = [spot for level in range(config.standing_search_levels)
               for spot in floor_candidates(scene, 'Venue', 'left', [0, 0, 0], config, level)]
    assert 0 < len(samples) <= config.standing_samples
    assert all(spot['position'][1] == 0 and spot['position'][0] < 0 for spot in samples)
    assert floor_candidates(scene, 'Missing', 'left', [0, 0, 0], config, 0) == []


@pytest.mark.parametrize('kwargs', [{'standing_samples': 0}, {'standing_samples': 1.5},
                                   {'standing_search_levels': 0}, {'standing_search_levels': 1.5}])
def test_invalid_standing_budgets_are_rejected(kwargs):
    from crowd.config import CrowdSolverConfig
    with pytest.raises(ValueError):
        CrowdSolverConfig(**kwargs)


def test_initial_layout_widens_a_pruned_joint_search():
    from crowd.config import CrowdSolverConfig
    scene = _scene({'left': [(0, 0), (4, 0)], 'right': [(-.1, 0), (.1, 0)]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01', 'Venue:standing_right_01'])
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}]}
    config = CrowdSolverConfig(layout_beam_width=1, standing_search_levels=2)
    assert space_standing_actors(plan, states, scene, manifest, ROOT, crowd_config=config) is None
    assert states['human_1']['position'] == [4, 0, 0]
    assert math.dist(states['human_1']['position'], states['human_2']['position']) >= 2 + config.buffer_m


def test_explicit_coordinates_are_kept_when_a_reference_is_also_present():
    scene = _scene({'left': [(0, 0), (4, 0), (8, 0)]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'] * 2)
    plan.setup[0].position = [0, 0, 0]
    original = copy.deepcopy(plan.setup[0].to_dict())
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}]}
    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert plan.setup[0].to_dict() == original
    assert states['human_1']['position'] == [0, 0, 0]
    assert states['human_2']['position'] == [4, 0, 0]


@pytest.mark.parametrize('dance_before_departure', [False, True])
def test_initial_placement_reserves_only_the_starting_visit(dance_before_departure, monkeypatch):
    from types import SimpleNamespace
    from crowd_spacing import actor_envelopes
    scene = _scene({'left': [(-.5, 0)]})
    scene['support_surfaces'] = [{'owner': 'Venue', 'height_m': 0,
                                  'polygon_xz': [[-1, -2], [1, -2], [1, 2], [-1, 2]]}]
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'], dance=False)
    # Deliberately different list/timeline order: explicit timing is authoritative.
    plan.beats = [Beat('dance', 'dance', 'human_1'), Beat('walk', 'walk_to', 'human_1',
                                                      {'target': 'point(5, 0, 0)'})]
    dance_start = 0 if dance_before_departure else 2
    resolved = [SimpleNamespace(beat_id='dance', actor_id='human_1', function='dance',
                                start_time=dance_start, end_time=dance_start + 1),
                SimpleNamespace(beat_id='walk', actor_id='human_1', function='walk_to',
                                start_time=1, end_time=2)]
    monkeypatch.setattr('crowd_spacing.beat_reach',
                        lambda beat, *args, **kwargs: 2 if beat.function == 'dance' else .4)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [.7, 1.8, .7]}]}
    # Without resolved timing, retain the conservative envelope.
    assert space_standing_actors(copy.deepcopy(plan), copy.deepcopy(states), scene,
                                 manifest, ROOT).error_code == 'CROWD_SPACING'
    error = space_standing_actors(plan, states, scene, manifest, ROOT, timeline_beats=resolved)
    if dance_before_departure:
        assert error.error_code == 'CROWD_SPACING'
    else:
        assert error is None
    # Later destination and routing checks retain the full dance envelope.
    envelopes = actor_envelopes(plan, states, manifest, ROOT)
    assert envelopes.beat_reach['dance'] == 2


@pytest.mark.parametrize('duration', [1., 4., 8.])
def test_standing_allocation_reserves_authored_dance_root_travel(duration):
    from plan_model import TimingCondition
    from timeline_resolver import TimelineResolver
    scene = _scene({'left': [(x, 0) for x in (0, 3, 6, 9, 12, 15, 18, 21)]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01',
                                            'Venue:standing_left_02'])
    plan.beats[1].params['style'] = 'moonwalk'
    plan.beats[1].params['travel'] = 'travel'
    plan.beats[1].duration_condition = TimingCondition('for_duration', duration)
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    durations = {b.beat_id: b.duration for b in timeline.beats}
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}], 'clips': {}}
    assert space_standing_actors(plan, states, scene, manifest, ROOT, durations=durations) is None
    from crowd_spacing import dance_root_speed
    a, b = (states[f'human_{i}']['position'] for i in (1, 2))
    # Every point of the translated root must retain the initial body clearance.
    for fraction in (0., .25, .5, .75, 1.):
        translated = [b[0], b[1], b[2] - dance_root_speed(plan.beats[1].params)*duration*fraction]
        assert math.dist(a, translated) >= 2 + MOTION_CLEARANCE_BUFFER_M
    assert math.dist(a, b) >= 2 + MOTION_CLEARANCE_BUFFER_M + .9*duration


def _scene(points_by_side, clearance=None):
    return {"standing_spots": [
        {"owner": "Venue", "name": f"standing_{side}_{index:02d}",
         "position": [float(x), 0.0, float(z)], "clearance_m": clearance}
        for side, points in points_by_side.items()
        for index, (x, z) in enumerate(points, 1)
    ]}


def _plan_and_states(scene, references, dance=True):
    spots = {f"{item['owner']}:{item['name']}": item["position"]
             for item in scene["standing_spots"]}
    setup = [SetupAction("place", f"human_{index}", asset_id="character.human",
                         at_reference=reference)
             for index, reference in enumerate(references, 1)]
    beats = [Beat(f"dance_{index}", "dance", f"human_{index}", {"style": "groove"})
             for index in range(1, len(references) + 1)] if dance else []
    states = {item.actor_id: {"asset_id": item.asset_id, "position": list(spots[item.at_reference]),
                              "at_reference": item.at_reference, "is_vehicle": False}
              for item in setup}
    return ShotPlan(setup=setup, beats=beats), states


def test_spreads_close_dancers_without_changing_their_side():
    scene = _scene({"left": [(x, z) for x in (0, 1.5, 3, 4.5, 6, 7.5)
                             for z in (0, 3)],
                    "right": [(20, 0), (23, 0), (26, 0)]})
    refs = ["Venue:standing_left_01", "Venue:standing_left_03",
            "Venue:standing_left_05", "Venue:standing_right_01"]
    plan, states = _plan_and_states(scene, refs)
    manifest = {"assets": [{"id": "character.human", "bounds_m": [2.0, 1.8, 0.5]}],
                "clips": {}}

    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert all(item.at_reference.startswith("Venue:standing_left_") for item in plan.setup[:3])
    assert plan.setup[3].at_reference.startswith("Venue:standing_right_")
    points = [states[item.actor_id]["position"] for item in plan.setup]
    assert all(math.hypot(a[0] - b[0], a[2] - b[2]) >= 2.0 + MOTION_CLEARANCE_BUFFER_M
               for index, a in enumerate(points) for b in points[index + 1:])


@pytest.mark.parametrize('height', [.4, 1.5])
def test_automatic_meeting_uses_participants_floor_instead_of_foreign_ground(height):
    scene = _scene({'left': [(0, 0), (4, 0), (8, 0), (12, 0)]})
    for spot in scene['standing_spots']:
        spot['position'][1] = height
    scene['standing_spots'].append({'owner': 'Outside', 'name': 'standing_left_01',
                                     'position': [0, 0, 0]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01', 'Venue:standing_left_02'], dance=False)
    pair = list(states)
    plan.beats = [Beat('stage_'+actor, 'turn_to_face', actor,
                       {'target': target, 'participants': pair, 'group_id': 'meeting',
                        'center': 'Outside:standing_left_01', 'spacing': 'combat'})
                  for actor, target in (pair, pair[::-1])]
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [.5, 1.8, .5]}], 'clips': {}}
    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert all(b.params['center'].startswith('Venue:') for b in plan.beats)


def test_fixed_actor_and_solid_clearance_reserve_space():
    scene = _scene({"left": [(0, 0), (3, 0), (6, 0)]})
    scene["standing_spots"][1]["clearance_m"] = 0.5
    plan, states = _plan_and_states(scene, ["Venue:standing_left_01"])
    states["fixed"] = {"asset_id": "character.human", "position": [0, 0, 0],
                       "is_vehicle": False}
    manifest = {"assets": [{"id": "character.human", "bounds_m": [2.0, 1.8, 0.5]}],
                "clips": {}}

    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert plan.setup[0].at_reference == "Venue:standing_left_03"


def test_insufficient_room_fails_before_mutating_plan():
    scene = _scene({"left": [(0, 0), (1.5, 0)]})
    plan, states = _plan_and_states(scene, ["Venue:standing_left_01",
                                             "Venue:standing_left_02"])
    manifest = {"assets": [{"id": "character.human", "bounds_m": [2.0, 1.8, 0.5]}],
                "clips": {}}
    original = [item.at_reference for item in plan.setup]

    error = space_standing_actors(plan, states, scene, manifest, ROOT)
    assert error is not None and error.error_code == "CROWD_SPACING"
    assert [item.at_reference for item in plan.setup] == original


@pytest.mark.parametrize('scale', [1., 1.5, 2.])
def test_joint_placement_reconsiders_nearest_spot_that_blocks_later_actor(scale):
    scene = _scene({'left': [(x * scale, 0) for x in (0, 1.5, 3)]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_02'] * 2)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2 * scale, 1.8, .5]}], 'clips': {}}
    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert {s.at_reference for s in plan.setup} == {'Venue:standing_left_01', 'Venue:standing_left_03'}
    assert math.dist(states['human_1']['position'], states['human_2']['position']) >= 2 * scale + MOTION_CLEARANCE_BUFFER_M


def test_standing_motion_envelope_cannot_overhang_floor_or_move_to_other_level():
    scene = _scene({'left': [(0, 0), (3, 0), (6, 0)]})
    scene['standing_spots'][2]['position'][1] = 2
    scene['support_surfaces'] = [{'height_m': 0, 'polygon_xz': [[-.5, -2], [7, -2], [7, 2], [-.5, 2]]}]
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'])
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}], 'clips': {}}
    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert plan.setup[0].at_reference == 'Venue:standing_left_02'
    # Occupying the only safe spot must fail, rather than choose the upstairs
    # marker or put part of the animated body outside the measured floor.
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01'])
    states['fixed'] = {'asset_id': 'character.human', 'position': [3, 0, 0]}
    before = list(states['human_1']['position'])
    assert space_standing_actors(plan, states, scene, manifest, ROOT).error_code == 'CROWD_SPACING'
    assert states['human_1']['position'] == before


@pytest.mark.parametrize('count', [4, 10, 20])
def test_joint_layout_keeps_entire_cast_separated(count):
    scene = _scene({'left': [(x * 1.5, z * 1.5) for x in range(count) for z in range(3)]})
    plan, states = _plan_and_states(scene, [f'Venue:standing_left_{i + 1:02d}' for i in range(count)])
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [2, 1.8, .5]}], 'clips': {}}
    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    points = [s['position'] for s in states.values()]
    assert all(math.dist(a, b) >= 2 + MOTION_CLEARANCE_BUFFER_M
               for i, a in enumerate(points) for b in points[i + 1:])


@pytest.mark.parametrize('style', ['groove', 'silly', 'breakdance', 'moonwalk'])
@pytest.mark.parametrize('travel', ['in_place', 'travel'])
def test_dance_travel_policy_matches_synthesis_and_clearance(style, travel):
    from function_catalog import FunctionCatalog
    from motion_synthesis import MotionSynthesizer
    from timeline_resolver import TimelineResolver
    from crowd_spacing import dance_root_speed
    catalog = FunctionCatalog({'assets': [{'id': 'character.human'}],
                               'clips': {'moonwalk': {}, 'acknowledging': {}}})
    _, actor = catalog.place('character.human')
    aid = actor['actor_id']
    ok, result = catalog.dance(aid, style=style, travel=travel, duration='long')
    assert ok, result
    states = {aid: {'position': [0., 0., 0.], 'facing_deg': 0.}}
    timeline, error = TimelineResolver().resolve(catalog.plan, states)
    assert error is None
    performance, error = MotionSynthesizer(catalog.manifest).synthesize_actor(
        aid, states[aid], timeline.actor_schedules[aid], {}, {}, timeline.total_duration_s)
    assert error is None
    beat = catalog.plan.beats[0]
    expected_distance = abs(dance_root_speed(beat.params)) * timeline.beats[0].duration
    assert math.dist(performance.keyframes[0].position, performance.keyframes[-1].position) == pytest.approx(expected_distance)
    if travel == 'in_place':
        assert all(k.position == [0., 0., 0.] for k in performance.keyframes)


def test_dance_defaults_to_in_place_and_rejects_unknown_travel_policy():
    from function_catalog import FunctionCatalog
    from crowd_spacing import dance_root_speed
    catalog = FunctionCatalog({'assets': [{'id': 'character.human'}]})
    _, actor = catalog.place('character.human')
    ok, _ = catalog.dance(actor['actor_id'], style='moonwalk')
    assert ok and dance_root_speed(catalog.plan.beats[0].params) == 0
    # Existing saved calls with no travel setting use the same safe default.
    assert dance_root_speed({'style': 'moonwalk'}) == 0
    count = len(catalog.plan.beats)
    ok, error = catalog.dance(actor['actor_id'], travel='unknown')
    assert not ok and error['error_code'] == 'DANCE_TRAVEL'
    assert len(catalog.plan.beats) == count


def test_authored_dance_clip_extends_clearance_beyond_rest_asset_bounds():
    import json
    manifest = json.loads((ROOT / "manifest.json").read_text())
    plan = ShotPlan(beats=[Beat("dance", "dance", "human_1", {"style": "groove"})])
    reach = _actor_reach("human_1", "character.human", plan, manifest, ROOT)
    asset = next(item for item in manifest["assets"] if item["id"] == "character.human")
    assert reach > asset["bounds_m"][0] * 0.5


def test_independent_fight_centers_spread_while_repeat_pair_reuses_center():
    scene = _scene({"left": [(x * 1.5, 0) for x in range(10)]})
    beats = []
    for group, participants, center in (
        ("first", ("a", "b"), "Venue:standing_left_01"),
        ("second", ("c", "d"), "Venue:standing_left_02"),
        ("first_again", ("a", "b"), "Venue:standing_left_03"),
    ):
        beats.extend(Beat(f"{group}_{actor}", "turn_to_face", actor,
                          {"group_id": group, "participants": list(participants),
                           "target": target, "center": center, "spacing": "combat"})
                     for actor, target in (participants, participants[::-1]))
    plan = ShotPlan(beats=beats)
    assert space_standing_actors(plan, {}, scene, {"assets": [], "clips": {}}, ROOT) is None
    centers = {beat.params["group_id"]: beat.params["center"] for beat in plan.beats}
    points = {f"Venue:{spot['name']}": spot["position"] for spot in scene["standing_spots"]}
    first, second = points[centers["first"]], points[centers["second"]]
    assert math.hypot(first[0] - second[0], first[2] - second[2]) >= PAIR_CENTER_SEPARATION_M
    assert centers["first_again"] == centers["first"]


@pytest.mark.parametrize('pair_count', [2, 4, 8])
def test_prompt_attacks_receive_separate_supported_arenas(pair_count):
    from director_solver import link_prompt_interactions
    scene = _scene({'left': [(3.0 * index, 0) for index in range(pair_count * 3)],
                    'right': [(60.0 + 3.0 * index, 0) for index in range(pair_count * 3)]})
    refs = ([f'Venue:standing_left_{index + 1:02d}' for index in range(pair_count)]
            + [f'Venue:standing_right_{index + 1:02d}' for index in range(pair_count)])
    plan, states = _plan_and_states(scene, refs, dance=False)
    for index in range(pair_count):
        left, right = f'human_{index + 1}', f'human_{pair_count + index + 1}'
        plan.beats.append(Beat(f'attack_{index}', 'attack', left, {'target': right}))
    plan = link_prompt_interactions(plan, states)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [.5, 1.8, .5]}], 'clips': {}}

    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    centers = {}
    for beat in plan.beats:
        assert beat.params['center_auto']
        centers.setdefault(beat.params['group_id'], beat.params['center'])
        assert beat.params['center'] == centers[beat.params['group_id']]
    positions = {f"Venue:{spot['name']}": spot['position'] for spot in scene['standing_spots']}
    chosen = [positions[reference] for reference in centers.values()]
    assert len(chosen) == pair_count
    combat_radius = .55 + _actor_reach('human_1', 'character.human', plan, manifest, ROOT)
    assert all(math.dist(first, second) + 1e-7 >= 2 * combat_radius + MOTION_CLEARANCE_BUFFER_M
               for index, first in enumerate(chosen) for second in chosen[index + 1:])


def test_later_fight_keeps_pairs_authored_semantic_center():
    from director_solver import link_prompt_interactions
    scene = _scene({'left': [(0, 0), (4, 0), (8, 0), (12, 0)]})
    plan, states = _plan_and_states(scene, ['Venue:standing_left_01',
                                            'Venue:standing_left_02'], dance=False)
    pair = list(states)
    plan.beats = [Beat(f'stage_{actor}', 'turn_to_face', actor,
                       {'group_id': 'stage', 'participants': pair,
                        'target': target, 'center': 'plaza.center', 'spacing': 'combat'})
                  for actor, target in (pair, pair[::-1])]
    plan.beats.append(Beat('strike', 'attack', pair[0], {'target': pair[1]}))
    plan = link_prompt_interactions(plan, states)
    manifest = {'assets': [{'id': 'character.human', 'bounds_m': [.5, 1.8, .5]}], 'clips': {}}

    assert space_standing_actors(plan, states, scene, manifest, ROOT) is None
    assert all(beat.params['center'] == 'plaza.center' for beat in plan.beats)
    assert all(not beat.params.get('center_auto') for beat in plan.beats)
