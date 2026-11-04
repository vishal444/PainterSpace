"""Crowd combat shares timing and opposing groups, without pair scheduling."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from director_solver import link_prompt_interactions, schedule_prompt_interactions
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from motion_synthesis import MotionSynthesizer
from plan_model import TimingCondition
from timeline_resolver import TimelineResolver


def crowd(sizes):
    catalog = FunctionCatalog({"assets": [{"id": "character.human"}],
                               "clips": {name: {"duration_s": 1.0} for name in
                                         ("left_hook", "elbow_punch", "punching", "boxing_block")}})
    groups, states = [], {}
    for side, count in enumerate(sizes):
        members = []
        for index in range(count):
            label = f"side_{side}_member_{index}"
            _, record = catalog.place("character.human", label=label)
            members.append(label)
            states[record["actor_id"]] = {"position": [side * 10., 0., index * 3.], "label": label}
        groups.append(members)
    return catalog, groups, states


@pytest.mark.parametrize("sizes", [(1, 1), (3, 2), (10, 10)])
def test_group_combat_never_creates_pair_arenas(sizes):
    catalog, groups, states = crowd(sizes)
    for index, actor in enumerate(states):
        catalog.wait(actor, duration=float(index + 1))
    ok, result = catalog.group_fight(*groups)
    assert ok, result
    linked = link_prompt_interactions(catalog.plan, states)
    fights = [b for b in linked.beats if b.function == "group_fight"]
    assert len(fights) == sum(sizes)
    assert all(not b.params.get("group_id") and not b.params.get("target") for b in fights)
    scheduled, error = schedule_prompt_interactions(
        linked, states, TimelineResolver(), object(), {})
    assert error is None, error
    assert len(scheduled.beats) == len(linked.beats)
    timeline, error = TimelineResolver().resolve(scheduled, states)
    assert error is None
    resolved = [b for b in timeline.beats if b.function == "group_fight"]
    assert {b.start_time for b in resolved} == {float(sum(sizes))}
    assert len({b.end_time for b in resolved}) == 1
    for beat in fights:
        own_group = next(group for group in groups if states[beat.actor_id]["label"] in group)
        assert len(beat.params["opponents"]) == sizes[1 - groups.index(own_group)]


def test_group_runins_wait_for_trigger_and_preserve_individual_destinations():
    catalog, groups, states = crowd((2, 3))
    _, opening = catalog.wait(groups[0][0], duration="long")
    refs = [[f"Venue:standing_left_{i:02d}" for i in range(1, 3)],
            [f"Venue:standing_right_{i:02d}" for i in range(1, 4)]]
    ok, result = catalog.group_fight(*groups, destinations_a=refs[0], destinations_b=refs[1],
                                     after=opening["beat_id"] + ".finished")
    assert ok, result
    runs = [b for b in catalog.plan.beats if b.function == "run_to"]
    assert [b.params["target"] for b in runs] == refs[0] + refs[1]
    durations = {b.beat_id: float(i + 1) for i, b in enumerate(runs)}
    timeline, error = TimelineResolver().resolve(catalog.plan, states, durations)
    assert error is None
    arrivals = [b for b in timeline.beats if b.function == "run_to"]
    fights = [b for b in timeline.beats if b.function == "group_fight"]
    assert min(b.start_time for b in arrivals) >= timeline.events[opening["beat_id"] + ".finished"]
    assert {b.start_time for b in fights} == {max(b.end_time for b in arrivals)}


@pytest.mark.parametrize("invalid", ["empty", "overlap", "unknown", "destination_count", "duplicate_destination", "clip"])
def test_invalid_group_fight_does_not_mutate_plan(invalid):
    catalog, groups, _ = crowd((2, 2))
    kwargs = {}
    if invalid == "empty": groups[0] = []
    if invalid == "overlap": groups[1][0] = groups[0][0]
    if invalid == "unknown": groups[1][0] = "missing"
    if invalid == "destination_count": kwargs["destinations_a"] = ["Venue:standing_left_01"]
    if invalid == "duplicate_destination": kwargs["destinations_a"] = ["Venue:standing_left_01"] * 2
    if invalid == "clip": kwargs["moves"] = ["missing_clip"]
    ok, error = catalog.group_fight(*groups, **kwargs)
    assert not ok, error
    assert not catalog.plan.beats


def test_group_fight_synthesizes_combat_without_partner_contacts():
    catalog, groups, states = crowd((2, 3))
    ok, result = catalog.group_fight(*groups, moves=["punching", "boxing_block"])
    assert ok, result
    timeline, error = TimelineResolver().resolve(catalog.plan, states)
    assert error is None
    performances = {}
    synthesizer = MotionSynthesizer(catalog.manifest)
    for actor in states:
        performance, error = synthesizer.synthesize_actor(
            actor, states[actor], timeline.actor_schedules[actor], {}, performances,
            timeline.total_duration_s, all_initial_states=states)
        assert error is None, error
        performances[actor] = performance
        assert {k.action_clip for k in performance.keyframes} >= {"punching", "boxing_block"}
        assert all(k.position == states[actor]["position"] for k in performance.keyframes)
        assert not performance.allowed_contacts


def test_explicit_attack_still_links_only_its_named_opponent():
    catalog, groups, states = crowd((2, 2))
    catalog.attack(groups[0][0], groups[1][0], moves=["punching"])
    linked = link_prompt_interactions(catalog.plan, states)
    assert len(linked.beats) == 2
    assert all(len(b.params["participants"]) == 2 for b in linked.beats)


def test_group_fight_is_exposed_to_llm():
    planner = LLMShotPlanner(FunctionCatalog({"assets": [{"id": "character.human"}]}))
    schema = next(t["function"] for t in planner.export_tool_schemas()
                  if t["function"]["name"] == "group_fight")
    assert schema["parameters"]["required"] == ["group_a", "group_b"]
    assert schema["parameters"]["properties"]["destinations_a"]["type"] == "array"


def test_crowd_joins_initial_exchange_without_new_pairs():
    catalog, groups, states = crowd((3, 4))
    _, opening = catalog.attack(groups[0][0], groups[1][0], moves=["punching"])
    ok, result = catalog.group_fight(*groups, after=opening["beat_id"] + ".finished")
    assert ok, result
    linked = link_prompt_interactions(catalog.plan, states)
    paired = [b for b in linked.beats if b.params.get("group_id")]
    assert len(paired) == 2  # Only the explicitly directed initial exchange.
    assert len([b for b in linked.beats if b.function == "group_fight"]) == 7
    timeline, error = TimelineResolver().resolve(linked, states)
    assert error is None
    fights = [b for b in timeline.beats if b.function == "group_fight"]
    assert len({b.start_time for b in fights}) == 1
    assert fights[0].start_time >= timeline.events[opening["beat_id"] + ".finished"]


@pytest.mark.parametrize("sizes", [(2, 3), (10, 10)])
def test_existing_fighters_can_depart_into_crowd_runins(sizes):
    from crowd.movements import route_movements
    from crowd_spacing import ActorEnvelopes
    from semantic_references import ReferenceResolver

    class Routes:
        def _collect_obstacles_at_time(self, *args): return []

    catalog, groups, states = crowd(sizes)
    _, opening = catalog.attack(groups[0][0], groups[1][0], moves=["punching"])
    catalog.defend_or_block(groups[1][0], threat=groups[0][0])
    references = [[f"Venue:standing_{side}_{i}" for i in range(count)]
                  for side, count in enumerate(sizes)]
    ok, result = catalog.group_fight(*groups, destinations_a=references[0],
                                     destinations_b=references[1], after=opening["beat_id"])
    assert ok, result
    scene = {"objects": [{"name": "Venue", "asset_id": "building.venue", "position": [0., 0., 0.]}],
             "standing_spots": [{"owner": "Venue", "name": f"standing_{side}_{i}",
                                  "position": [4. + side * 2., 0., i * 3.]}
                                 for side, count in enumerate(sizes) for i in range(count)],
             "support_surfaces": [{"prim_path": "/World/Floor", "height_m": 0., "polygon_xz":
                                   [[-20., -20.], [40., -20.], [40., 40.], [-20., 40.]]}]}
    resolver = TimelineResolver(ref_resolver=ReferenceResolver(catalog.manifest, scene))
    envelopes = ActorEnvelopes({actor: .2 for actor in states}, {},
                              travel_reach={"run_to": .7})
    plan = link_prompt_interactions(catalog.plan, states)
    movement_index = next(i for i, beat in enumerate(plan.beats) if beat.function == "run_to")
    from copy import deepcopy
    prefix = deepcopy(plan)
    prefix.beats = prefix.beats[:movement_index]
    scheduled, error = schedule_prompt_interactions(
        prefix, states, resolver, Routes(), scene, envelopes=envelopes,
        continuation_beats=plan.beats[movement_index:])
    assert error is None, error
    scheduled.beats.extend(plan.beats[movement_index:])
    routed, error = route_movements(scheduled, states, resolver, Routes(), scene,
                                    envelopes, preserve_approaches=True)
    assert error is None, error
    assert sum(b.function == "group_fight" for b in routed.beats) == sum(sizes)
    assert set(result["arrival_beat_ids"]) <= {b.beat_id for b in routed.beats}


def test_group_facing_uses_arrivals_before_opponents_are_synthesized():
    catalog, groups, states = crowd((1, 1))
    actor, opponent = states
    _, run = catalog.run_to(opponent, "unused_reference")
    route = catalog.plan.beats[-1]
    route.params["brief_path"] = {"points": [[10., 0., 0.], [-10., 0., 0.]]}
    route.duration_condition = TimingCondition("for_duration", 3.)
    catalog.group_fight(*groups, moves=["punching"])
    timeline, error = TimelineResolver().resolve(catalog.plan, states)
    assert error is None
    fight = next(b for b in timeline.beats if b.actor_id == actor and b.function == "group_fight")
    assert fight.params["opponent_positions"][opponent] == [-10., 0., 0.]
    performance, error = MotionSynthesizer(catalog.manifest).synthesize_actor(
        actor, states[actor], [fight], {}, {}, timeline.total_duration_s, all_initial_states=states)
    assert error is None
    from semantic_references import heading_from_vector
    assert performance.keyframes[0].heading_deg == pytest.approx(heading_from_vector(-10., 0.))


def test_routed_runins_keep_barrier_events_and_recompile_without_pairs():
    from crowd.movements import route_movements
    from crowd_spacing import ActorEnvelopes
    from semantic_references import ReferenceResolver

    catalog, groups, states = crowd((2, 2))
    scene = {"objects": [{"name": "Venue", "asset_id": "building.venue", "position": [0., 0., 0.]}],
             "standing_spots": [{"owner": "Venue", "name": f"standing_{side}_{i:02d}",
                                  "position": [4. if side == "left" else 6., 0., i * 3.]}
                                 for side in ("left", "right") for i in range(2)],
             "support_surfaces": [{"height_m": 0., "polygon_xz":
                                   [[-20., -20.], [20., -20.], [20., 20.], [-20., 20.]]}]}
    references = [[f"Venue:standing_{side}_{i:02d}" for i in range(2)] for side in ("left", "right")]
    ok, result = catalog.group_fight(*groups, destinations_a=references[0], destinations_b=references[1])
    assert ok, result
    resolver = TimelineResolver(ref_resolver=ReferenceResolver(catalog.manifest, scene))
    envelopes = ActorEnvelopes({actor: .3 for actor in states}, {})

    class Routes:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]

    routed, error = route_movements(catalog.plan, states, resolver, Routes(), scene, envelopes)
    assert error is None, error
    timeline, error = resolver.resolve(routed, states)
    assert error is None
    fights = [b for b in timeline.beats if b.function == "group_fight"]
    arrivals = [timeline.events[beat + ".finished"] for beat in result["arrival_beat_ids"]]
    assert {b.start_time for b in fights} == {max(arrivals)}
    assert not any(b.params.get("group_id") for b in routed.beats)
    again, error = route_movements(routed, states, resolver, Routes(), scene, envelopes)
    assert error is None, error
    assert again.to_dict() == routed.to_dict()


def test_group_fight_runins_wait_for_all_participants_preceding_actions_without_explicit_after():
    catalog, groups, states = crowd((2, 2))
    # One pair has a longer preceding exchange while the other pair finishes early.
    catalog.wait(groups[0][0], duration=3.0)
    catalog.wait(groups[0][1], duration=3.0)
    catalog.wait(groups[1][0], duration=12.0)
    catalog.wait(groups[1][1], duration=15.0)
    refs = [[f"Venue:standing_left_{i:02d}" for i in range(1, 3)],
            [f"Venue:standing_right_{i:02d}" for i in range(1, 3)]]
    ok, result = catalog.group_fight(*groups, destinations_a=refs[0], destinations_b=refs[1])
    assert ok, result
    runs = [b for b in catalog.plan.beats if b.function == "run_to"]
    assert len(runs) == 4
    # Every run-in beat must wait for all 4 participants' prior beats.
    for run_beat in runs:
        assert len(run_beat.params.get("wait_for", [])) == 4
    timeline, error = TimelineResolver().resolve(catalog.plan, states)
    assert error is None
    resolved_runs = [b for b in timeline.beats if b.function == "run_to"]
    assert min(b.start_time for b in resolved_runs) >= 15.0


def test_llm_planner_phases_and_phase_tags_enforce_cross_phase_barriers():
    catalog, _, _ = crowd((1, 1))
    planner = LLMShotPlanner(catalog)
    ok, plan, log = planner.plan_from_llm_json({
        "phases": [
            {
                "phase": "opening",
                "calls": [
                    {"function": "place", "args": {"asset": "character.human", "label": "hero_a"}},
                    {"function": "place", "args": {"asset": "character.human", "label": "hero_b"}},
                    {"function": "wait", "args": {"actor": "hero_a", "duration": "long"}},
                    {"function": "wait", "args": {"actor": "hero_b", "duration": "short"}},
                ],
            },
            {
                "phase": "followup",
                "calls": [
                    {"function": "wait", "args": {"actor": "hero_b", "duration": "short"}},
                ],
            },
        ]
    })
    assert ok, log
    assert len(plan.beats) == 3
    phase2_beat = plan.beats[2]
    assert set(phase2_beat.params.get("wait_for", [])) == {plan.beats[0].beat_id, plan.beats[1].beat_id}
    states = {"human_1": {"position": [0., 0., 0.]}, "human_2": {"position": [3., 0., 0.]}}
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    resolved_phase1_a = next(b for b in timeline.beats if b.beat_id == plan.beats[0].beat_id)
    resolved_phase2 = next(b for b in timeline.beats if b.beat_id == phase2_beat.beat_id)
    assert resolved_phase2.start_time >= resolved_phase1_a.end_time



@pytest.mark.parametrize('function', ['attack', 'shake_hands'])
@pytest.mark.parametrize('distance', [4., 10.])
def test_phase_barrier_covers_generated_partners_and_approaches(function, distance):
    catalog, _, _ = crowd((1, 1))
    planner = LLMShotPlanner(catalog)
    ok, plan, log = planner.plan_from_llm_json({'phases': [
        {'calls': [
            *[{'function': 'place', 'args': {'asset': 'character.human', 'label': a}}
              for a in ('a', 'b', 'observer')],
            {'function': 'wait', 'args': {'actor': 'observer', 'duration': 'long'}},
        ]},
        {'calls': [{'function': function, 'args': {'actor': 'a', 'target': 'b'}}]},
    ]})
    assert ok, log
    states = {f'human_{i+1}': {'position': [x, 0., 0.]}
              for i, x in enumerate((0., distance, distance * 4))}
    from crowd_spacing import ActorEnvelopes
    envelopes = ActorEnvelopes({a: .3 for a in states}, {})
    resolver = TimelineResolver()
    for _ in range(2):  # Regeneration must retain the same phase contract.
        plan, error = schedule_prompt_interactions(
            plan, states, resolver, MotionSynthesizer(catalog.manifest, scene_data={}),
            {}, envelopes=envelopes)
        assert error is None, error
        timeline, error = resolver.resolve(plan, states)
        assert error is None, error
        end = timeline.events['wait_1.finished']
        generated = [b for b in timeline.beats if b.actor_id != 'human_3' and not b.is_idle_fill]
        assert any(b.params.get('auto_interaction_approach') for b in generated)
        assert all(b.start_time >= end for b in generated)
        interactions = [b for b in generated if b.params.get('auto_interaction')]
        assert len(interactions) == 2
        assert len({b.start_time for b in interactions}) == 1
