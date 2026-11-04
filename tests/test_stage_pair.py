import math
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from director_solver import (_clear_route_around_discs, _route_clear_of_actors, _route_clear_of_discs,
                             link_prompt_interactions,
                             schedule_prompt_interactions)
from function_catalog import FunctionCatalog
from motion_synthesis import MotionSynthesizer, stair_route_geometry
from timeline_resolver import TimelineResolver
from crowd_spacing import ActorEnvelopes
from crowd.config import CrowdSolverConfig
from plan_model import Beat, SetupAction, ShotPlan, TimingCondition
from support_surfaces import contains, supports_route
from semantic_references import GAP_BETWEEN_BODIES, ReferenceResolver
import pytest


def test_generated_approach_checks_other_actors_at_travel_time():
    resolver = TimelineResolver()
    route = [[-2.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    crossing = {"other": [(0.0, 0.0, 0.0, -2.0),
                          (2.0, 0.0, 0.0, 0.0),
                          (4.0, 0.0, 0.0, 2.0)]}
    assert not _route_clear_of_actors(
        route, 0.0, 1.0, "traveler", {"traveler", "partner"}, crossing,
        resolver._sample_traj, 1.0, 0.12, 0.25)
    separated = {"other": [(0.0, 0.0, 0.0, 3.0),
                           (4.0, 0.0, 0.0, 3.0)]}
    assert _route_clear_of_actors(
        route, 0.0, 1.0, "traveler", {"traveler", "partner"}, separated,
        resolver._sample_traj, 1.0, 0.12, 0.25)


def test_actor_arrivals_reserve_distinct_supported_positions():
    synthesizer = MotionSynthesizer({"assets": [], "clips": {}})
    scene = {"support_surfaces": [{"height_m": 0.0,
                                    "polygon_xz": [[-8.0, -8.0], [8.0, -8.0],
                                                   [8.0, 8.0], [-8.0, 8.0]]}]}
    initial = {"target": {"position": [0.0, 0.0, 0.0]},
               "first": {"position": [-5.0, 0.0, -1.0]},
               "second": {"position": [-5.0, 0.0, 1.0]}}
    first = synthesizer._spaced_actor_destination(
        "first", "target", [0.0, 0.0, 0.0], [-5.0, 0.0, -1.0],
        2.0, {}, initial, scene)
    initial["first"]["position"] = first
    second = synthesizer._spaced_actor_destination(
        "second", "target", [0.0, 0.0, 0.0], [-5.0, 0.0, 1.0],
        2.0, {}, initial, scene)
    assert first is not None and second is not None
    assert math.dist(first, second) >= 1.5
    assert all(contains(scene["support_surfaces"][0], point) for point in (first, second))


class SimpleRoutes:
    def _resolve_stair(self, reference):
        x = 0.0 if reference == "left" else 2.0
        return {"data": {"centerline": [[x, 0.0, 4.0], [x, 2.0, 0.0]]}}

    def _collect_obstacles_at_time(self, *args):
        return []

    def _plan_smooth_path(self, source, destination, obstacles):
        return [source, destination]


def test_approach_gap_is_preserved_before_pair_staging():
    states = {"man": {"position": [-4., 0., 0.]},
              "woman": {"position": [4., 0., 0.]}}
    scene = {"support_surfaces": [{"prim_path": "/World/Floor", "height_m": 0.,
                                     "polygon_xz": [[-8., -8.], [8., -8.],
                                                    [8., 8.], [-8., 8.]]}]}
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({}, scene))
    plan = ShotPlan(beats=[
        Beat("approach", "approach", "man", {"target": "woman", "gap": "near"}),
        Beat("face_man", "turn_to_face", "man",
             {"target": "woman", "participants": ["man", "woman"],
              "group_id": "pair", "spacing": "contact", "center": "woman"}),
        Beat("face_woman", "turn_to_face", "woman",
             {"target": "man", "participants": ["man", "woman"],
              "group_id": "pair", "spacing": "contact", "center": "woman"}),
    ])
    scheduled, error = schedule_prompt_interactions(
        plan, states, resolver, SimpleRoutes(), scene,
        envelopes=ActorEnvelopes({"man": .2, "woman": .2}, {}))
    assert error is None, error
    assert scheduled is not None


@pytest.mark.parametrize("gap", ["adjacent", "conversational", "near", "far"])
def test_provisional_approach_stops_at_named_gap(gap):
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({}, {}))
    trajectory = {"man": [(0., -5., 0., 0.)],
                  "woman": [(0., 5., 0., 0.)]}
    resolver._update_provisional_trajectory(
        "man", 0., 4., Beat("approach", "approach", "man",
                            {"target": "woman", "gap": gap}), trajectory)
    man_end = resolver._sample_traj(trajectory["man"], 4.)
    woman_end = resolver._sample_traj(trajectory["woman"], 4.)
    assert math.dist(man_end, woman_end) == pytest.approx(GAP_BETWEEN_BODIES[gap])


def test_dance_then_contact_pair_routes_on_open_floor():
    states = {"man": {"position": [-4., 0., 0.]},
              "woman": {"position": [4., 0., 0.]}}
    scene = {"support_surfaces": [{"prim_path": "/World/Floor", "height_m": 0.,
                                     "polygon_xz": [[-8., -8.], [8., -8.],
                                                    [8., 8.], [-8., 8.]]}]}
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({}, scene))
    plan = ShotPlan(beats=[
        Beat("smoke", "perform_activity", "man", {"activity": "smoke"}),
        Beat("dance_woman", "dance", "woman", {"style": "groove", "travel": "in_place"}),
        Beat("look", "look_at", "man", {"reference": "woman"}),
        Beat("react", "react", "man", {"mood": "excited", "look_at": "woman"}),
        Beat("approach", "approach", "man", {"target": "woman", "gap": "near"}),
        Beat("dance_man", "dance", "man", {"style": "groove", "travel": "in_place"}),
        Beat("face_man", "turn_to_face", "man",
             {"target": "woman", "participants": ["man", "woman"],
              "group_id": "pair", "spacing": "contact", "center": "woman"}),
        Beat("face_woman", "turn_to_face", "woman",
             {"target": "man", "participants": ["man", "woman"],
              "group_id": "pair", "spacing": "contact", "center": "woman"}),
        Beat("kiss", "hug_or_kiss", "man", {"target": "woman", "style": "kiss"}),
    ])
    scheduled, error = schedule_prompt_interactions(
        plan, states, resolver, SimpleRoutes(), scene,
        envelopes=ActorEnvelopes({"man": .3, "woman": .3},
                                 {"dance_man": .9, "dance_woman": .9, "kiss": .9}))
    assert error is None, error
    assert scheduled is not None
    timeline, error = resolver.resolve(scheduled, states)
    assert error is None
    dance_end = next(beat.end_time for beat in timeline.beats if beat.beat_id == "dance_man")
    assert all(beat.start_time >= dance_end - 1e-7 for beat in timeline.beats
               if beat.params.get("auto_interaction_approach") == "pair")


@pytest.mark.parametrize("initial_gap", [1.6, 2.5])
def test_contact_pair_can_keep_one_partner_stationary(initial_gap):
    states = {"first": {"position": [-initial_gap, 0., 0.]},
              "second": {"position": [0., 0., 0.]}}
    scene = {"support_surfaces": [{"prim_path": "/World/Floor", "height_m": 0.,
                                     "polygon_xz": [[-8., -8.], [8., -8.],
                                                    [8., 8.], [-8., 8.]]}]}
    plan = ShotPlan(beats=[
        Beat("face_first", "turn_to_face", "first",
             {"target": "second", "participants": ["first", "second"],
              "group_id": "pair", "spacing": "contact", "center_auto": True}),
        Beat("face_second", "turn_to_face", "second",
             {"target": "first", "participants": ["first", "second"],
              "group_id": "pair", "spacing": "contact", "center_auto": True}),
    ])
    scheduled, error = schedule_prompt_interactions(
        plan, states, TimelineResolver(), SimpleRoutes(), scene,
        envelopes=ActorEnvelopes({"first": .3, "second": .3}, {}))
    assert error is None, error
    slots = next(beat.params["interaction_positions"] for beat in scheduled.beats
                 if beat.beat_id == "face_first")
    assert any(math.dist(slots[actor], states[actor]["position"]) < 1e-6
               for actor in states)
    assert math.dist(slots["first"], slots["second"]) == pytest.approx(.9)


@pytest.mark.parametrize("travel_reach", [.65, 1.1])
@pytest.mark.parametrize("departing", [("left",), ("left", "right")])
@pytest.mark.parametrize("initial_gap", [1.1, 8.])
def test_pair_staging_leaves_clearance_for_following_ordinary_actions(travel_reach, departing, initial_gap):
    from crowd.movements import route_movements

    states = {"left": {"position": [-initial_gap / 2, 0., 0.]},
              "right": {"position": [initial_gap / 2, 0., 0.]}}
    prefix = ShotPlan(beats=[
        Beat("stage_" + actor, "turn_to_face", actor,
             {"target": target, "participants": ["left", "right"],
              "group_id": "meeting", "spacing": "combat"})
        for actor, target in (("left", "right"), ("right", "left"))])
    continuation = [Beat("depart_" + actor, "run_to", actor,
                         {"target": [-4. if actor == "left" else 4., 0., 0.]})
                    for actor in departing]
    continuation += [Beat("crowd_" + actor, "group_fight", actor,
                          {"opponents": [other], "moves": ["punching"]})
                     for actor, other in (("left", "right"), ("right", "left"))]
    envelopes = ActorEnvelopes({actor: .2 for actor in states},
                              {"crowd_left": .8, "crowd_right": .8},
                              travel_reach={"run_to": travel_reach})
    resolver = TimelineResolver()
    scheduled, error = schedule_prompt_interactions(
        prefix, states, resolver, SimpleRoutes(), {}, envelopes=envelopes,
        continuation_beats=continuation)
    assert error is None, error
    slots = next(b.params["interaction_positions"] for b in scheduled.beats
                 if b.beat_id == "stage_left")
    needed = sum(travel_reach if actor in departing else .8 for actor in states) + envelopes.buffer_m
    assert math.dist(slots["left"], slots["right"]) >= needed - 1e-7
    scheduled.beats.extend(continuation)
    routed, error = route_movements(scheduled, states, resolver, SimpleRoutes(), {},
                                    envelopes, preserve_approaches=True)
    assert error is None, error
    assert {b.beat_id for b in routed.beats} >= {"depart_" + a for a in departing}


def test_contact_staging_keeps_contact_range_despite_later_running():
    states = {"a": {"position": [-3., 0., 0.]}, "b": {"position": [3., 0., 0.]}}
    plan = ShotPlan(beats=[
        Beat("contact_" + actor, "turn_to_face", actor,
             {"target": other, "participants": ["a", "b"],
              "group_id": "contact", "spacing": "contact"})
        for actor, other in (("a", "b"), ("b", "a"))])
    envelopes = ActorEnvelopes({actor: .2 for actor in states}, {},
                              travel_reach={"run_to": 1.})
    scheduled, error = schedule_prompt_interactions(
        plan, states, TimelineResolver(), SimpleRoutes(), {}, envelopes=envelopes,
        continuation_beats=[Beat("leave", "run_to", "a", {"target": [-3., 0., 0.]})])
    assert error is None, error
    slots = next(b.params["interaction_positions"] for b in scheduled.beats
                 if b.beat_id == "contact_a")
    assert math.dist(slots["a"], slots["b"]) == pytest.approx(.9)


@pytest.mark.parametrize("count", [3, 5])
def test_shared_formation_reserves_different_following_performance_envelopes(count):
    states = {f"actor_{i}": {"position": [6 * math.cos(2 * math.pi * i / count), 0.,
                                           6 * math.sin(2 * math.pi * i / count)]}
              for i in range(count)}
    plan = ShotPlan(beats=[Beat("meet_" + actor, "huddle", actor,
                               {"participants": list(states), "group_id": "meeting"})
                           for actor in states])
    reaches = {"gesture_" + actor: .7 + i * .2 for i, actor in enumerate(states)}
    plan.beats.extend(Beat("gesture_" + actor, "react", actor) for actor in states)
    envelopes = ActorEnvelopes({actor: .2 for actor in states}, reaches)
    scheduled, error = schedule_prompt_interactions(
        plan, states, TimelineResolver(), SimpleRoutes(), {}, envelopes=envelopes)
    assert error is None, error
    slots = next(b.params["interaction_positions"] for b in scheduled.beats
                 if b.beat_id.startswith("meet_"))
    for i, actor in enumerate(states):
        for other in list(states)[i + 1:]:
            assert math.dist(slots[actor], slots[other]) >= (
                reaches["gesture_" + actor] + reaches["gesture_" + other] + envelopes.buffer_m - 1e-7)


@pytest.mark.parametrize("face_count", [1, 80])
@pytest.mark.parametrize("spacing", ["combat", "conversational"])
def test_repeated_floor_edges_do_not_exhaust_formation_search(face_count, spacing):
    from crowd.config import CrowdSolverConfig

    surface = {"prim_path": "/World/Floor", "height_m": 0.0,
               "polygon_xz": [[-8., -8.], [8., -8.], [8., 8.], [-8., 8.]]}
    actors = {"left": {"position": [-4., 0., 0.]},
              "right": {"position": [4., 0., 0.]},
              "observer": {"position": [0., 0., 0.]}}
    plan = ShotPlan(beats=[
        Beat("pair_" + actor, "turn_to_face", actor,
             {"target": target, "participants": ["left", "right"],
              "group_id": "pair", "spacing": spacing})
        for actor, target in (("left", "right"), ("right", "left"))])
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(),
        {"support_surfaces": [dict(surface, prim_path=f"/World/Floor_{i}")
                              for i in range(face_count)]},
        crowd_config=CrowdSolverConfig(max_formations=128))
    assert error is None, error
    meeting = next(b for b in scheduled.beats if b.beat_id == "pair_left")
    assert all(math.hypot(p[0], p[2]) >= 1.5
               for p in meeting.params["interaction_positions"].values())


def test_formation_budget_failure_identifies_blocking_actor():
    from crowd.config import CrowdSolverConfig

    actors = {"left": {"position": [-4., 0., 0.]},
              "right": {"position": [4., 0., 0.]},
              "observer": {"position": [0., 0., 0.]}}
    plan = ShotPlan(beats=[
        Beat("pair_" + actor, "turn_to_face", actor,
             {"target": target, "participants": ["left", "right"],
              "group_id": "pair", "spacing": "combat"})
        for actor, target in (("left", "right"), ("right", "left"))])
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(), {},
        crowd_config=CrowdSolverConfig(max_formations=1))
    assert scheduled is None
    assert error.error_code == "INTERACTION_SEARCH_LIMIT"
    assert error.details["formation_attempts"] == 1
    assert error.details["top_blockers"] == {"observer": 1}


def test_approach_path_searches_around_occupied_space():
    obstacle = {"cx": 0.0, "cz": 0.0, "radius": 1.5}
    route = _clear_route_around_discs(
        [-4.0, 0.0, 0.0], [4.0, 0.0, 0.0], [obstacle],
        lambda points: all(abs(point[2]) < 5.0 for point in points))
    assert route is not None and len(route) > 2
    for point in route[1:-1]:
        assert math.hypot(point[0], point[2]) >= obstacle["radius"]


def test_dense_approach_routes_remain_clear_without_all_pairs_visibility_search():
    obstacles = [{"cx": float(x), "cz": 0.0, "radius": 0.45}
                 for x in range(-3, 4)]
    route = _clear_route_around_discs(
        [-5.0, 0.0, 0.0], [5.0, 0.0, 0.0], obstacles,
        lambda points: all(abs(point[2]) <= 4.0 for point in points))
    assert route is not None
    assert _route_clear_of_discs(route, obstacles,
                                 lambda points: all(abs(point[2]) <= 4.0 for point in points))


def test_interaction_search_budget_reports_infeasible_approach():
    surface = {"height_m": 0.0, "polygon_xz": [[-8, -8], [8, -8], [8, 8], [-8, 8]]}
    actors = {"first": {"position": [-4.0, 0.0, 0.0]},
              "second": {"position": [4.0, 0.0, 0.0]}}
    beats = [Beat("pair_first", "turn_to_face", "first",
                  {"target": "second", "group_id": "pair", "participants": ["first", "second"],
                   "spacing": "combat"}),
             Beat("pair_second", "turn_to_face", "second",
                  {"target": "first", "group_id": "pair", "participants": ["first", "second"],
                   "spacing": "combat"})]
    _, error = schedule_prompt_interactions(
        ShotPlan(beats=beats), actors, TimelineResolver(), SimpleRoutes(),
        {"support_surfaces": [surface]}, max_route_attempts=0)
    assert error is not None and error.error_code == "INTERACTION_SEARCH_LIMIT"


def test_pair_searches_another_supported_meeting_spot_when_center_is_occupied():
    surface = {"prim_path": "/World/Floor", "height_m": 0.0,
               "polygon_xz": [[-8.0, -8.0], [8.0, -8.0],
                              [8.0, 8.0], [-8.0, 8.0]]}
    scene = {"objects": [], "support_surfaces": [surface],
             "standing_spots": [{"name": "open_spot", "position": [0.0, 0.0, 4.0]}]}
    actors = {"left": {"position": [-5.0, 0.0, 0.0]},
              "right": {"position": [5.0, 0.0, 0.0]},
              "bystander": {"position": [0.0, 0.0, 0.0]}}
    plan = ShotPlan(beats=[
        Beat("pair_left", "turn_to_face", "left",
             {"target": "right", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "combat"}),
        Beat("pair_right", "turn_to_face", "right",
             {"target": "left", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "combat"}),
    ])
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(), scene)
    assert error is None
    staged = next(beat for beat in scheduled.beats if beat.beat_id == "pair_left")
    assert all(math.hypot(point[0], point[2]) >= 1.5
               for point in staged.params["interaction_positions"].values())


def test_pair_waits_for_blocker_to_leave_meeting_area():
    surface = {"prim_path": "/World/Floor", "height_m": 0.0,
               "polygon_xz": [[-8.0, -2.0], [8.0, -2.0],
                              [8.0, 2.0], [-8.0, 2.0]]}
    actors = {"left": {"position": [-4.0, 0.0, 0.0]},
              "right": {"position": [4.0, 0.0, 0.0]},
              "blocker": {"position": [0.0, 0.0, 0.0]}}
    plan = ShotPlan(beats=[
        Beat("blocker_leaves", "walk_to", "blocker",
             {"brief_path": {"points": [[0.0, 0.0, 0.0], [0.0, 0.0, 5.0]]},
              "action": "walk_to"},
             TimingCondition("at", 0.0), TimingCondition("for_duration", 2.0)),
        Beat("pair_left", "turn_to_face", "left",
             {"target": "right", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "combat"}),
        Beat("pair_right", "turn_to_face", "right",
             {"target": "left", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "combat"}),
    ])
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(),
        {"objects": [], "support_surfaces": [surface]})
    assert error is None
    approaches = [beat for beat in scheduled.beats
                  if beat.params.get("auto_interaction_approach")]
    assert len(approaches) == 2
    assert all(beat.start_condition.value >= 2.0 for beat in approaches)


def test_multiple_pairs_use_separate_space_in_a_crowd():
    surface = {"prim_path": "/World/Floor", "height_m": 0.0,
               "polygon_xz": [[-10.0, -10.0], [10.0, -10.0],
                              [10.0, 10.0], [-10.0, 10.0]]}
    actors = {"left_a": {"position": [-6.0, 0.0, -2.0]},
              "right_a": {"position": [6.0, 0.0, -2.0]},
              "left_b": {"position": [-6.0, 0.0, 2.0]},
              "right_b": {"position": [6.0, 0.0, 2.0]},
              "observer_a": {"position": [0.0, 0.0, -2.0]},
              "observer_b": {"position": [0.0, 0.0, 2.0]}}
    beats = []
    for group, pair in (("pair_a", ("left_a", "right_a")),
                        ("pair_b", ("left_b", "right_b"))):
        for actor, target in (pair, pair[::-1]):
            beats.append(Beat(group + "_" + actor, "turn_to_face", actor,
                              {"target": target, "group_id": group,
                               "participants": list(pair), "spacing": "combat"}))
    scene = {"objects": [], "support_surfaces": [surface],
             "standing_spots": [{"position": [0.0, 0.0, -5.0]},
                                {"position": [0.0, 0.0, 5.0]}]}
    scheduled, error = schedule_prompt_interactions(
        ShotPlan(beats=beats), actors, TimelineResolver(), SimpleRoutes(), scene)
    assert error is None
    formations = [beat.params["interaction_positions"] for beat in scheduled.beats
                  if beat.beat_id in ("pair_a_left_a", "pair_b_left_b")]
    assert len(formations) == 2
    assert all(math.hypot(a[0] - b[0], a[2] - b[2]) >= 1.5
               for a in formations[0].values() for b in formations[1].values())


def test_stage_pair_uses_stair_arrivals_and_restages_for_kiss():
    catalog = FunctionCatalog({"assets": [{"id": "character.male"}, {"id": "character.female"}],
                               "clips": {"left_hook": {}, "elbow_punch": {}}})
    _, man = catalog.place("character.male", label="man")
    _, woman = catalog.place("character.female", label="woman")
    man_id, woman_id = man["actor_id"], woman["actor_id"]
    catalog.climb_stairs(man_id, "left", direction="up", pace="run")
    catalog.climb_stairs(woman_id, "right", direction="up", pace="run")
    catalog.plan.beats[0].params["stair_exit_distance_m"] = 0.8
    ok, staged = catalog.stage_pair(["man", "woman"], spacing="combat")
    assert ok and staged["ready_event"].endswith(".finished")
    catalog.attack(man_id, "woman", after=staged["ready_event"])
    catalog.hug_or_kiss(man_id, "woman", style="kiss")
    assert catalog.get_plan().beats[-1].params["target"] == woman_id

    initial = {
        man_id: {"position": [0.0, 0.0, 4.0], "label": "man", "asset_id": "character.male"},
        woman_id: {"position": [2.0, 0.0, 4.0], "label": "woman", "asset_id": "character.female"},
    }
    plan, error = schedule_prompt_interactions(
        catalog.get_plan(), initial, TimelineResolver(), SimpleRoutes(), {})
    assert error is None
    combat = [beat for beat in plan.beats
              if beat.function == "turn_to_face" and beat.params.get("spacing") == "combat"]
    assert len(combat) == 2
    first_positions = combat[0].params["interaction_positions"]
    assert all(abs(first_positions[actor][1] - 2.0) < 0.01 for actor in (man_id, woman_id))
    assert math.dist(first_positions[man_id], first_positions[woman_id]) < 2.0
    attack = next(beat for beat in plan.beats if beat.function == "attack")
    assert attack.params["paired_target_position"] == first_positions[woman_id]
    assert len([beat for beat in plan.beats if beat.params.get("auto_interaction_approach")]) >= 2
    expected_arrival = stair_route_geometry(SimpleRoutes()._resolve_stair("left")["data"],
                                            exit_distance_m=0.8)[2]
    approach = next(beat for beat in plan.beats
                    if beat.params.get("auto_interaction_approach") and beat.actor_id == man_id)
    assert approach.params["brief_path"]["points"][0] == expected_arrival
    recompilation, error = schedule_prompt_interactions(
        plan, initial, TimelineResolver(), SimpleRoutes(), {})
    assert error is None
    assert len([beat for beat in recompilation.beats
                if beat.params.get("auto_interaction_approach")]) == len([
                    beat for beat in plan.beats if beat.params.get("auto_interaction_approach")])
    kiss = next(beat for beat in plan.beats if beat.function == "hug_or_kiss")
    assert kiss.params["participants"] == [man_id, woman_id]
    assert math.dist(*kiss.params["interaction_positions"].values()) <= 1.2


@pytest.mark.parametrize("later_condition", [None, TimingCondition("at", 4.0)])
def test_timed_beat_cannot_overtake_earlier_action_for_same_actor(later_condition):
    plan = ShotPlan(beats=[
        Beat("starter", "wait", "human_1", {},
             TimingCondition("at", 0.0), TimingCondition("for_duration", 2.0)),
        Beat("first", "wait", "human_1", {}, None,
             TimingCondition("for_duration", 2.0)),
        Beat("second", "react", "human_1", {}, later_condition,
             TimingCondition("for_duration", 2.0)),
    ])
    timeline, error = TimelineResolver().resolve(plan, {"human_1": {"position": [0, 0, 0]}})
    assert error is None
    by_id = {beat.beat_id: beat for beat in timeline.beats}
    assert by_id["first"].start_time >= by_id["starter"].end_time
    assert by_id["second"].start_time >= by_id["first"].end_time


def test_second_pair_staging_stays_after_both_actors_fight():
    catalog = FunctionCatalog({"assets": [{"id": "character.male"}, {"id": "character.female"}],
                               "clips": {"left_hook": {}, "punching": {}}})
    _, man = catalog.place("character.male", label="man")
    _, woman = catalog.place("character.female", label="woman")
    man_id, woman_id = man["actor_id"], woman["actor_id"]
    catalog.climb_stairs(man_id, "left", direction="up", pace="run")
    catalog.climb_stairs(woman_id, "right", direction="up", pace="run")
    catalog.stage_pair([man_id, woman_id], spacing="combat")
    catalog.attack(man_id, woman_id, moves=["left_hook"])
    catalog.defend_or_block(woman_id, man_id)
    catalog.attack(woman_id, man_id, moves=["punching"])
    catalog.defend_or_block(man_id, woman_id)
    catalog.react(man_id, mood="tired")
    catalog.react(woman_id, mood="tired")
    catalog.stage_pair([man_id, woman_id], spacing="contact")
    catalog.hug_or_kiss(man_id, woman_id, style="kiss")
    initial = {
        man_id: {"position": [0, 0, 4], "label": "man", "asset_id": "character.male"},
        woman_id: {"position": [2, 0, 4], "label": "woman", "asset_id": "character.female"},
    }
    resolver = TimelineResolver()
    plan, error = schedule_prompt_interactions(catalog.get_plan(), initial, resolver, SimpleRoutes(), {})
    assert error is None
    timeline, error = resolver.resolve(plan, initial)
    assert error is None
    combat_end = max(beat.end_time for beat in timeline.beats
                     if beat.function in ("attack", "defend_or_block", "react"))
    contact_start = min(beat.start_time for beat in timeline.beats
                        if beat.params.get("spacing") == "contact")
    kiss_beats = [beat for beat in timeline.beats if beat.function == "hug_or_kiss"]
    kiss_start = kiss_beats[0].start_time
    assert contact_start >= combat_end
    assert kiss_start >= contact_start
    assert len(kiss_beats) == 2
    assert kiss_beats[0].start_time == kiss_beats[1].start_time
    attacks = [beat for beat in timeline.beats if beat.function == "attack"]
    defenses = [beat for beat in timeline.beats if beat.function == "defend_or_block"]
    assert len(attacks) == len(defenses) == 2
    for attack in attacks:
        defense = next(beat for beat in defenses if beat.actor_id == attack.params["target"])
        assert attack.start_time == defense.start_time


def test_explicitly_ordered_attacks_remain_sequential():
    actors = {"human_1": {"position": [-1, 0, 0]},
              "human_2": {"position": [1, 0, 0]}}
    plan = ShotPlan(beats=[
        Beat("strike_first", "attack", "human_1", {"target": "human_2"}),
        Beat("strike_second", "attack", "human_2", {"target": "human_1"},
             TimingCondition("after", "strike_first.finished")),
    ])
    linked = link_prompt_interactions(plan, actors)
    first = next(beat for beat in linked.beats if beat.beat_id == "strike_first")
    second = next(beat for beat in linked.beats if beat.beat_id == "strike_second")
    assert first.params["group_id"] != second.params["group_id"]
    timeline, error = TimelineResolver().resolve(linked, actors)
    assert error is None
    resolved = {beat.beat_id: beat for beat in timeline.beats}
    assert resolved["strike_second"].start_time >= resolved["strike_first"].end_time


def test_reciprocal_attacks_share_time_and_group_without_duplicate_reactions():
    actors = {"human_1": {"position": [-1, 0, 0]},
              "human_2": {"position": [1, 0, 0]}}
    plan = ShotPlan(beats=[
        Beat("first", "attack", "human_1", {"target": "human_2"}),
        Beat("second", "attack", "human_2", {"target": "human_1"}),
    ])
    linked = link_prompt_interactions(plan, actors)
    assert len(linked.beats) == 2
    assert linked.beats[0].params["group_id"] == linked.beats[1].params["group_id"]
    timeline, error = TimelineResolver().resolve(linked, actors)
    assert error is None
    attacks = [beat for beat in timeline.beats if beat.function == "attack"]
    assert attacks[0].start_time == attacks[1].start_time


def test_reciprocal_fight_actions_with_same_ready_event_share_time():
    actors = {"human_1": {"position": [-1, 0, 0]},
              "human_2": {"position": [1, 0, 0]}}
    ready = TimingCondition("after", "ready.finished")
    plan = ShotPlan(beats=[
        Beat("ready", "wait", "human_1", {}, TimingCondition("at", 0),
             TimingCondition("for_duration", 1)),
        Beat("strike", "attack", "human_1", {"target": "human_2"}, ready),
        Beat("guard", "defend_or_block", "human_2", {"target": "human_1"}, ready),
    ])
    linked = link_prompt_interactions(plan, actors)
    assert len(linked.beats) == 3
    assert linked.beats[1].params["group_id"] == linked.beats[2].params["group_id"]
    timeline, error = TimelineResolver().resolve(linked, actors)
    assert error is None
    by_id = {beat.beat_id: beat for beat in timeline.beats}
    assert by_id["strike"].start_time == by_id["guard"].start_time


@pytest.mark.parametrize("target_key", ["attacker", "threat", "target"])
@pytest.mark.parametrize("use_label", [False, True])
def test_defense_target_forms_one_shared_exchange(target_key, use_label):
    from crowd.conformance import check_conformance

    catalog = FunctionCatalog({"assets": [{"id": "character.human"}],
                               "clips": {name: {} for name in (
                                   "left_hook", "elbow_punch", "walk", "idle", "boxing_block")}})
    _, striker = catalog.place("character.human", label="striker")
    _, guard = catalog.place("character.human", label="guard")
    a, b = striker["actor_id"], guard["actor_id"]
    catalog.attack(a, b)
    ok, result = catalog.defend_or_block(
        b, **{target_key: "striker" if use_label else a})
    assert ok, result
    assert catalog.plan.beats[-1].params["target"] == a
    actors = {a: {"position": [-2, 0, 0]}, b: {"position": [2, 0, 0]}}
    resolver = TimelineResolver()
    # Each animated core needs more room than the combat pair's separation;
    # only their intentional shared exchange permits this close spacing.
    env = ActorEnvelopes({a: .3, b: .3},
                         {beat.beat_id: .6 for beat in catalog.plan.beats})
    plan, error = schedule_prompt_interactions(
        catalog.plan, actors, resolver, SimpleRoutes(), {}, envelopes=env)
    assert error is None
    exchanges = [beat for beat in plan.beats if beat.params.get("auto_interaction")]
    assert len(exchanges) == 2
    assert {beat.function for beat in exchanges} == {"attack", "defend_or_block"}
    assert len({beat.params["group_id"] for beat in exchanges}) == 1
    timeline, error = resolver.resolve(plan, actors)
    assert error is None
    performances = {}
    synthesizer = MotionSynthesizer(catalog.manifest)
    for actor in actors:
        performance, error = synthesizer.synthesize_actor(
            actor, actors[actor], timeline.actor_schedules[actor], {}, performances,
            timeline.total_duration_s, all_initial_states=actors)
        assert error is None
        performances[actor] = performance
    assert not check_conformance(plan, timeline, performances, env, timeline.fps)


def test_defense_target_aliases_accept_same_actor_and_reject_conflicts():
    catalog = FunctionCatalog({"assets": [{"id": "character.human"}]})
    _, first = catalog.place("character.human", label="first")
    _, second = catalog.place("character.human", label="second")
    a, b = first["actor_id"], second["actor_id"]
    ok, result = catalog.defend_or_block(b, threat=a, attacker="first", target=a)
    assert ok, result
    assert catalog.plan.beats[-1].params["target"] == a
    before = len(catalog.plan.beats)
    ok, result = catalog.defend_or_block(b, threat=a, attacker=b)
    assert not ok and result["error_code"] == "DEFENSE_TARGET"
    assert len(catalog.plan.beats) == before
    ok, result = catalog.defend_or_block(b)
    assert ok, result
    assert catalog.plan.beats[-1].params["target"] is None


def test_one_sided_interactions_generate_unique_partner_ids():
    actors = {"human_1": {"position": [-1, 0, 0]},
              "human_2": {"position": [1, 0, 0]}}
    plan = ShotPlan(beats=[
        Beat("strike", "attack", "human_1", {"target": "human_2"}),
        Beat("strike_reaction", "react", "human_2", {"mood": "tired"}),
        Beat("kiss", "hug_or_kiss", "human_1", {"target": "human_2", "style": "kiss"}),
        Beat("kiss_partner", "wait", "human_2", {}),
    ])
    linked = link_prompt_interactions(plan, actors)
    assert len({beat.beat_id for beat in linked.beats}) == len(linked.beats)
    strike_index = next(i for i, beat in enumerate(linked.beats) if beat.beat_id == "strike")
    reaction = linked.beats[strike_index + 1]
    assert reaction.function == "hit_reaction"
    assert reaction.actor_id == "human_2"
    assert linked.beats[strike_index].params["group_id"] == reaction.params["group_id"]
    kisses = [beat for beat in linked.beats if beat.function == "hug_or_kiss"]
    assert len(kisses) == 2
    assert kisses[0].params["group_id"] == kisses[1].params["group_id"]


def test_heuristic_fight_plans_attack_and_reaction_together():
    from llm_planner import LLMShotPlanner
    catalog = FunctionCatalog({"assets": [{"id": "character.human"}],
                               "clips": {"left_hook": {}, "elbow_punch": {},
                                         "kick_to_the_groin": {}}})
    catalog.place("character.human", label="first")
    catalog.place("character.human", label="second")
    planner = LLMShotPlanner(catalog)
    planner._synthesize_clauses(["they fight"], "they fight", [],
                                ["human_1", "human_2"], {})
    linked = link_prompt_interactions(catalog.get_plan(), {
        "human_1": {"position": [-1, 0, 0]},
        "human_2": {"position": [1, 0, 0]},
    })
    fight = [beat for beat in linked.beats
             if beat.function in ("attack", "hit_reaction")]
    assert len(fight) == 2
    assert fight[0].params["group_id"] == fight[1].params["group_id"]
    assert fight[0].start_condition == fight[1].start_condition


def test_fight_then_contact_compiles_without_actor_collision(tmp_path):
    from compiler import ShotCompiler
    plan = ShotPlan(
        setup=[
            SetupAction("place", "human_1", "character.male", position=[-2, 0, 0]),
            SetupAction("place", "human_2", "character.female", position=[2, 0, 0]),
        ],
        beats=[
            Beat("pair_combat_1", "turn_to_face", "human_1",
                 {"target": "human_2", "participants": ["human_1", "human_2"],
                  "group_id": "pair_combat", "spacing": "combat"}),
            Beat("pair_combat_2", "turn_to_face", "human_2",
                 {"target": "human_1", "participants": ["human_1", "human_2"],
                  "group_id": "pair_combat", "spacing": "combat"}),
            Beat("strike_1", "attack", "human_1", {"target": "human_2", "moves": ["left_hook"]}),
            Beat("guard_2", "defend_or_block", "human_2", {"target": "human_1", "style": "block"}),
            Beat("strike_2", "attack", "human_2", {"target": "human_1", "moves": ["punching"]}),
            Beat("guard_1", "defend_or_block", "human_1", {"target": "human_2", "style": "block"}),
            Beat("pair_contact_1", "turn_to_face", "human_1",
                 {"target": "human_2", "participants": ["human_1", "human_2"],
                  "group_id": "pair_contact", "spacing": "contact"}),
            Beat("pair_contact_2", "turn_to_face", "human_2",
                 {"target": "human_1", "participants": ["human_1", "human_2"],
                  "group_id": "pair_contact", "spacing": "contact"}),
            Beat("kiss", "hug_or_kiss", "human_1", {"target": "human_2", "style": "kiss"}),
        ],
    )
    ok, report = ShotCompiler(ROOT).compile(plan, tmp_path / "shot.usda", validate=True)
    assert ok, report


def test_elevated_meeting_uses_supported_platform_instead_of_ground_zone():
    from semantic_references import ReferenceResolver
    surface = {"prim_path": "/World/Platform", "height_m": 2.0,
               "polygon_xz": [[-3, -6], [3, -6], [3, 2], [-3, 2]]}
    scene = {"objects": [], "zones": [], "support_surfaces": [surface]}
    actors = {"human_1": {"position": [0, 2, -5]},
              "human_2": {"position": [0, 2, 1]}}
    plan = ShotPlan(beats=[
        Beat("pair_a", "turn_to_face", "human_1",
             {"target": "human_2", "group_id": "pair", "participants": list(actors),
              "spacing": "combat", "center": "plaza.center"}),
        Beat("pair_b", "turn_to_face", "human_2",
             {"target": "human_1", "group_id": "pair", "participants": list(actors),
              "spacing": "combat", "center": "plaza.center"}),
    ])
    resolver = TimelineResolver(ref_resolver=ReferenceResolver({}, scene))
    result, error = schedule_prompt_interactions(plan, actors, resolver, SimpleRoutes(), scene)
    assert error is None
    positions = result.beats[-1].params["interaction_positions"]
    assert all(contains(surface, position, 0.35) for position in positions.values())
    assert all(position[2] < 0 for position in positions.values())
    assert all(supports_route(surface, beat.params["brief_path"]["points"])
               for beat in result.beats if beat.params.get("auto_interaction_approach"))


def test_elevated_meeting_rejects_platform_too_narrow_for_formation():
    surface = {"prim_path": "/World/Narrow", "height_m": 2.0,
               "polygon_xz": [[-0.2, -6], [0.2, -6], [0.2, 2], [-0.2, 2]]}
    scene = {"objects": [], "support_surfaces": [surface]}
    actors = {"human_1": {"position": [0, 2, -5]},
              "human_2": {"position": [0, 2, 1]}}
    plan = ShotPlan(beats=[
        Beat("pair_a", "turn_to_face", "human_1",
             {"target": "human_2", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
        Beat("pair_b", "turn_to_face", "human_2",
             {"target": "human_1", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
    ])
    result, error = schedule_prompt_interactions(plan, actors, TimelineResolver(), SimpleRoutes(), scene)
    assert result is None
    assert error.error_code == "INTERACTION_NO_SHARED_SUPPORT"
    assert error.beat_id == "pair_a"
    assert error.details["reason"] == "no_supported_formation"
    assert error.details["participants"] == ["human_1", "human_2"]
    assert error.details["level_support_count"] == 1


def test_meeting_support_error_reports_vertical_gap_and_source_reference():
    surface = {"prim_path": "/World/Club/MainFloor", "height_m": 0.24,
               "polygon_xz": [[-10, -10], [10, -10], [10, 10], [-10, 10]]}
    scene = {"objects": [], "support_surfaces": [surface]}
    actors = {"human_1": {"position": [0, 0, 0], "at_reference": "plaza.center"},
              "human_2": {"position": [4, 0, 0], "at_reference": "right_of(human_1, far)"}}
    plan = ShotPlan(beats=[
        Beat("pair_a", "turn_to_face", "human_1",
             {"target": "human_2", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
        Beat("pair_b", "turn_to_face", "human_2",
             {"target": "human_1", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
    ])
    result, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(), scene)
    assert result is None
    assert error.error_code == "INTERACTION_NO_SHARED_SUPPORT"
    assert error.details["reason"] == "actor_without_level_support"
    assert error.details["level_support_count"] == 0
    assert error.details["actors"][0]["at_reference"] == "plaza.center"
    nearest = error.details["actors"][0]["nearest_surfaces"][0]
    assert nearest["prim_path"] == "/World/Club/MainFloor"
    assert nearest["covers_xz"] is True
    assert nearest["vertical_gap_m"] == pytest.approx(0.24)


def test_elevated_meeting_can_cross_adjacent_supported_slabs():
    surfaces = [
        {"prim_path": "/World/SlabA", "height_m": 2.0,
         "polygon_xz": [[-2, -4], [2, -4], [2, 0], [-2, 0]]},
        {"prim_path": "/World/SlabB", "height_m": 2.0,
         "polygon_xz": [[-2, 0], [2, 0], [2, 4], [-2, 4]]},
    ]
    scene = {"objects": [], "support_surfaces": surfaces}
    actors = {"human_1": {"position": [0, 2, -3]},
              "human_2": {"position": [0, 2, 3]}}
    plan = ShotPlan(beats=[
        Beat("pair_a", "turn_to_face", "human_1",
             {"target": "human_2", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
        Beat("pair_b", "turn_to_face", "human_2",
             {"target": "human_1", "group_id": "pair", "participants": list(actors),
              "spacing": "combat"}),
    ])
    result, error = schedule_prompt_interactions(plan, actors, TimelineResolver(), SimpleRoutes(), scene)
    assert error is None
    assert all(supports_route(surfaces, beat.params["brief_path"]["points"])
               for beat in result.beats if beat.params.get("auto_interaction_approach"))


def _corridor_pair_scene():
    """A narrow corridor with a bystander standing between the pair and their meeting point."""
    surface = {"prim_path": "/World/Corridor", "height_m": 0.0,
               "polygon_xz": [[-8.0, -1.3], [8.0, -1.3], [8.0, 1.3], [-8.0, 1.3]]}
    actors = {"left": {"position": [-6.0, 0.0, 0.0]},
              "right": {"position": [6.0, 0.0, 0.0]},
              "bystander": {"position": [-3.0, 0.0, 0.0]}}
    plan = ShotPlan(beats=[
        Beat("pair_left", "turn_to_face", "left",
             {"target": "right", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "conversational"}),
        Beat("pair_right", "turn_to_face", "right",
             {"target": "left", "group_id": "pair", "participants": ["left", "right"],
              "spacing": "conversational"}),
    ])
    return plan, actors, {"objects": [], "support_surfaces": [surface]}


def test_measured_radii_replace_grid_pitch_as_route_clearance():
    plan, actors, scene = _corridor_pair_scene()
    _, grid_error = schedule_prompt_interactions(plan, actors, TimelineResolver(), SimpleRoutes(), scene)
    assert grid_error is not None, "the 1.5 m grid-pitch fallback should block this corridor"

    slim = ActorEnvelopes({actor: 0.3 for actor in actors}, {}, buffer_m=0.1)
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(), scene, envelopes=slim,
        crowd_config=CrowdSolverConfig(buffer_m=0.1))
    assert error is None
    for beat in scheduled.beats:
        if beat.params.get("auto_interaction_approach"):
            for point in beat.params["brief_path"]["points"]:
                assert math.hypot(point[0] + 3.0, point[2]) >= 0.3 + 0.3 + 0.1 - 1e-6


def test_pair_waits_for_wide_dancer_to_finish_in_narrow_corridor():
    plan, actors, scene = _corridor_pair_scene()
    plan.beats.insert(0, Beat("bystander_dance", "dance", "bystander", {},
                              duration_condition=TimingCondition("for_duration", 60.0)))
    wide = ActorEnvelopes({actor: 0.3 for actor in actors}, {"bystander_dance": 1.2})
    scheduled, error = schedule_prompt_interactions(
        plan, actors, TimelineResolver(), SimpleRoutes(), scene, envelopes=wide,
        crowd_config=CrowdSolverConfig(buffer_m=0.12))
    assert error is None
    meeting = next(beat for beat in scheduled.beats if beat.beat_id == "pair_left")
    assert meeting.start_condition.value >= 60.0 - 1e-6


def test_dancer_blocks_space_only_while_dancing():
    plan, actors, scene = _corridor_pair_scene()
    plan.beats.insert(0, Beat("bystander_dance", "dance", "bystander", {},
                              duration_condition=TimingCondition("for_duration", 1.0)))
    envelopes = ActorEnvelopes({actor: 0.3 for actor in actors}, {"bystander_dance": 1.2})
    beat = type("B", (), {"actor_id": "bystander", "beat_id": "bystander_dance",
                          "start_time": 0.0, "end_time": 1.0})()
    assert envelopes.radius_at("bystander", 0.5, [beat]) == 1.2
    assert envelopes.radius_at("bystander", 1.5, [beat]) == 0.3
    assert envelopes.clearance("left", "bystander", 0.5, [beat]) == pytest.approx(0.3 + 1.2 + 0.3)
    assert envelopes.clearance("left", "bystander", 1.5, [beat]) == pytest.approx(0.3 + 0.3 + 0.3)
def test_free_form_approach_is_routed_by_following_contact_scheduler():
    from director_solver import schedule_prompt_interactions
    from types import SimpleNamespace

    states = {'a': {'position': [-3., 0., 0.]},
              'b': {'position': [3., 0., 0.]}}
    plan = ShotPlan(beats=[
        Beat('move', 'approach', 'a', {'target': 'b'}),
        Beat('strike', 'attack', 'a', {'target': 'b'}),
    ])
    original = plan.to_dict()
    scheduled, error = schedule_prompt_interactions(
        plan, states, TimelineResolver(), SimpleRoutes(), {})
    assert error is None, error
    assert plan.to_dict() == original
    assert not any(beat.beat_id == 'move' for beat in scheduled.beats)
    assert any(beat.params.get('auto_interaction_approach') for beat in scheduled.beats)


@pytest.mark.parametrize('approach_kwargs', [
    {'start_condition': TimingCondition('at', 1)},
    {'duration_condition': TimingCondition('for_duration', 3)},
    {'params': {'target': 'b', 'brief_path': {'points': [[-3., 0., 0.], [1.7, 0., 0.]]}}},
])
def test_authored_approach_before_contact_is_preserved(approach_kwargs):
    from director_solver import schedule_prompt_interactions

    states = {'a': {'position': [-3., 0., 0.]},
              'b': {'position': [3., 0., 0.]}}
    options = {'beat_id': 'move', 'function': 'approach', 'actor_id': 'a',
               'params': {'target': 'b'}}
    options.update(approach_kwargs)
    plan = ShotPlan(beats=[Beat(**options),
                           Beat('strike', 'attack', 'a', {'target': 'b'})])
    scheduled, error = schedule_prompt_interactions(
        plan, states, TimelineResolver(), SimpleRoutes(), {})
    assert error is None, error
    assert any(beat.beat_id == 'move' for beat in scheduled.beats)

