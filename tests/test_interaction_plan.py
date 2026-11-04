"""Declared interaction intent constrains and verifies the shot timeline."""
import json
from pathlib import Path
import sys
import copy
import math
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from function_catalog import FunctionCatalog
from compiler import ShotCompiler
from interaction_plan import apply_interactions, validate_interactions
from llm_planner import LLMShotPlanner
from plan_model import Beat, InteractionPlan, SetupAction, ShotPlan, TimingCondition
from timeline_resolver import TimelineResolver


def test_sequence_declared_in_calls_schedules_cross_actor_dependency():
    plan = ShotPlan(
        beats=[Beat("leave", "exit_vehicle", "a"), Beat("arrive", "run_to", "b", {"target": "a"})],
        interactions=[InteractionPlan("meeting", ["a", "b"],
                                      ["a.exit_vehicle", "b.run_to"], "sequence", "meet after exit")])
    states = {"a": {"position": [0, 0, 0]}, "b": {"position": [4, 0, 0]}}
    assert apply_interactions(plan, states) is None
    assert plan.beats[1].params["wait_for"] == ["leave"]
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None
    beats = {beat.beat_id: beat for beat in timeline.beats}
    assert beats["arrive"].start_time >= beats["leave"].end_time
    assert ShotPlan.from_json(plan.to_json()).interactions[0].intent == "meet after exit"


def test_overlap_declaration_aligns_simultaneous_actions():
    plan = ShotPlan(
        beats=[Beat("a_attack", "attack", "a"), Beat("b_block", "defend_or_block", "b")],
        interactions=[InteractionPlan("exchange", ["a", "b"],
                                      ["a.attack", "b.defend_or_block"], "overlap")])
    states = {"a": {"position": [0, 0, 0]}, "b": {"position": [2, 0, 0]}}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None
    beats = {beat.beat_id: beat for beat in timeline.beats}
    assert beats["a_attack"].start_time == beats["b_block"].start_time


def test_ambiguous_or_missing_action_is_reported():
    plan = ShotPlan(
        beats=[Beat("first", "run_to", "a"), Beat("second", "run_to", "a"),
               Beat("wait", "wait", "b")],
        interactions=[InteractionPlan("pursuit", ["a", "b"], ["a.run_to", "b.wait"])])
    states = {"a": {}, "b": {}}
    error = apply_interactions(plan, states)
    assert error and error.error_code == "INTERACTION_ACTION_REFERENCE"
    plan.interactions[0].actions = ["a.run_to#2", "b.wait"]
    assert apply_interactions(plan, states) is None


def test_queue_tool_catalog_exposes_interaction_declaration():
    planner = LLMShotPlanner(FunctionCatalog({"assets": []}))
    schemas = {tool["function"]["name"]: tool["function"]
               for tool in planner.export_tool_schemas()}
    assert "declare_interaction" in schemas
    assert schemas["declare_interaction"]["parameters"]["required"] == [
        "interaction_id", "participants", "actions"]
    assert "declare_interaction" in planner.build_system_prompt(
        include_scene_inventory=False, include_brief_digest=False)


def test_existing_calls_json_carries_interaction_into_shot_plan():
    planner = LLMShotPlanner(FunctionCatalog({"assets": [
        {"id": "character.human", "tags": ["character"]}]}))
    calls = {"calls": [
        {"function": "place", "args": {"asset_id": "character.human", "label": "A"}},
        {"function": "place", "args": {"asset_id": "character.human", "label": "B"}},
        {"function": "wait", "args": {"actor": "A"}},
        {"function": "run_to", "args": {"actor": "B", "target": "A"}},
        {"function": "declare_interaction", "args": {
            "interaction_id": "arrival", "participants": ["A", "B"],
            "actions": ["A.wait", "B.run_to"], "timing": "sequence",
            "intent": "B arrives after A waits"}},
    ]}
    ok, plan, _ = planner.plan_from_llm_json(calls, "B runs to A")
    assert ok
    assert plan.interactions[0].interaction_id == "arrival"
    assert len(plan.beats) == 2


def test_compiler_applies_declared_sequence_to_final_timeline(tmp_path):
    root = Path(__file__).resolve().parents[1]
    plan = ShotPlan(
        setup=[SetupAction("place", "a", "character.human", label="A", position=[-3, 0, 0]),
               SetupAction("place", "b", "character.human", label="B", position=[3, 0, 0])],
        beats=[Beat("a_wait", "wait", "a"), Beat("b_wait", "wait", "b")],
        interactions=[InteractionPlan("turns", ["A", "B"],
                                      ["A.wait", "B.wait"], "sequence")])
    ok, report = ShotCompiler(root).compile(plan, tmp_path / "shot.usda", validate=False)
    assert ok, report
    timeline = json.loads((tmp_path / "resolved_timeline.json").read_text())
    beats = {beat["beat_id"]: beat for beat in timeline["beats"]}
    assert beats["b_wait"]["start_time"] >= beats["a_wait"]["end_time"]
    stored = json.loads((tmp_path / "shot_plan.json").read_text())
    assert stored["interactions"][0]["actions"] == ["a_wait", "b_wait"]


@pytest.mark.parametrize('count', [2, 5, 15])
@pytest.mark.parametrize('function', ['dance', 'perform_activity', 'celebrate'])
def test_terminal_performances_do_not_overrun_declared_sequence(count, function):
    actors = [f'actor_{index}' for index in range(count)]
    plan = ShotPlan(beats=[Beat(f'action_{actor}', function, actor) for actor in actors],
                    interactions=[InteractionPlan('turns', actors,
                                                   [f'{actor}.{function}' for actor in actors])])
    states = {actor: {'position': [index * 4, 0, 0]} for index, actor in enumerate(actors)}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None
    scheduled = {beat.beat_id: beat for beat in timeline.beats}
    for first, second in zip(plan.beats, plan.beats[1:]):
        assert scheduled[first.beat_id].end_time <= scheduled[second.beat_id].start_time
        assert timeline.events[f'{first.beat_id}.finished'] == scheduled[first.beat_id].end_time


@pytest.mark.parametrize('bounded', [False, True])
def test_terminal_extension_respects_duration_and_updates_finish_events(bounded):
    plan = ShotPlan(beats=[Beat('dance', 'dance', 'a',
                                duration_condition=TimingCondition('for_duration', 1) if bounded else None),
                          Beat('long_wait', 'wait', 'b', duration_condition=TimingCondition('for_duration', 20))])
    timeline, error = TimelineResolver().resolve(plan, {'a': {'position': [0, 0, 0]},
                                                       'b': {'position': [4, 0, 0]}})
    assert error is None
    dance = next(beat for beat in timeline.beats if beat.beat_id == 'dance')
    assert dance.end_time == (1 if bounded else timeline.total_duration_s)
    assert timeline.events['dance.finished'] == dance.end_time
    assert timeline.events['a.finished'] == dance.end_time
    assert dance.events['dance.finished'] == dance.end_time


@pytest.mark.parametrize('count', [1, 3, 15])
@pytest.mark.parametrize('simultaneous', [False, True])
def test_tool_calls_express_parallel_arrivals_then_shared_performance(count, simultaneous):
    planner = LLMShotPlanner(FunctionCatalog({'assets': [{'id': 'character.human'}]}))
    actors = [f'person_{index}' for index in range(count)]
    calls = [{'function': 'place', 'args': {'asset_id': 'character.human', 'label': actor}}
             for actor in actors]
    calls += [{'function': 'walk_to', 'args': {'actor': actor, 'target': f'Floor:standing_left_{index + 1:02d}'}}
              for index, actor in enumerate(actors)]
    calls += [{'function': 'dance', 'args': {'actor': actor}} for actor in actors]
    # Forward references are permitted; declaration order does not set causality.
    calls += [{'function': 'declare_interaction', 'args': {
        'interaction_id': 'performance', 'participants': actors,
        'actions': [f'{actor}.dance' for actor in actors],
        'timing': 'simultaneous' if simultaneous else 'overlap', 'after': ['travel']}},
        {'function': 'declare_interaction', 'args': {
            'interaction_id': 'travel', 'participants': actors,
            'actions': [f'{actor}.walk_to' for actor in actors],
            'timing': 'simultaneous' if simultaneous else 'parallel'}}]
    ok, plan, logs = planner.plan_from_llm_json({'calls': calls})
    assert ok, logs
    states = {setup.actor_id: {'position': [index * 4, 0, 0], 'label': setup.label}
              for index, setup in enumerate(plan.setup)}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None
    moves = [beat for beat in timeline.beats if beat.function == 'walk_to']
    dances = [beat for beat in timeline.beats if beat.function == 'dance']
    assert len({beat.start_time for beat in moves}) == 1
    assert len({beat.start_time for beat in dances}) == 1
    assert min(beat.start_time for beat in dances) >= max(beat.end_time for beat in moves)
    restored = ShotPlan.from_json(plan.to_json())
    assert restored.interactions[0].after == ['travel']
    before = restored.to_dict()
    assert apply_interactions(restored, states) is None
    assert restored.to_dict() == before
    schemas = {tool['function']['name']: tool['function'] for tool in planner.export_tool_schemas()}
    assert schemas['declare_interaction']['parameters']['properties']['after']['type'] == 'array'
    assert set(schemas['declare_interaction']['parameters']['properties']['timing']['enum']) == {
        'sequence', 'parallel', 'overlap', 'simultaneous'}
    prompt = planner.build_system_prompt(include_scene_inventory=False, include_brief_digest=False)
    assert 'simultaneous departures' in prompt
    assert 'alone do not guarantee equal start times' in prompt


def test_interaction_timing_error_names_conflicting_actions_and_times():
    plan = ShotPlan(beats=[Beat('first', 'wait', 'a'), Beat('second', 'wait', 'b')],
                    interactions=[InteractionPlan('turns', ['a', 'b'], ['first', 'second'])])
    states = {'a': {'position': [0, 0, 0]}, 'b': {'position': [4, 0, 0]}}
    # Deliberately validate a schedule that was not constrained by apply_interactions.
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    error = validate_interactions(plan, timeline, states)
    assert error.error_code == 'INTERACTION_TIMING'
    assert error.beat_id == 'second' and error.actor_id == 'b'
    assert error.details['interaction_id'] == 'turns'
    assert error.details['predecessor']['beat_id'] == 'first'
    assert error.details['following']['start_time_s'] == 0
    assert error.details['overlap_s'] > 0


@pytest.mark.parametrize('parent', ['missing', 'phase'])
def test_invalid_phase_dependencies_are_reported(parent):
    plan = ShotPlan(beats=[Beat('action', 'wait', 'a')],
                    interactions=[InteractionPlan('phase', ['a'], ['action'], after=[parent])])
    error = apply_interactions(plan, {'a': {}})
    assert error.error_code == 'INTERACTION_DEPENDENCY'
    assert error.details == {'interaction_id': 'phase', 'predecessor': parent}


def test_phase_validation_reports_late_arrival_and_early_performance():
    plan = ShotPlan(beats=[Beat('move', 'wait', 'a'), Beat('dance', 'dance', 'b')],
                    interactions=[InteractionPlan('travel', ['a'], ['move'], 'parallel'),
                                  InteractionPlan('performance', ['b'], ['dance'], 'overlap', after=['travel'])])
    states = {'a': {'position': [0, 0, 0]}, 'b': {'position': [4, 0, 0]}}
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    error = validate_interactions(plan, timeline, states)
    assert error.error_code == 'INTERACTION_TIMING'
    assert error.details['after'] == 'travel'
    assert error.details['predecessor']['beat_id'] == 'move'
    assert error.details['following']['beat_id'] == 'dance'


@pytest.mark.parametrize('count', [2, 5])
@pytest.mark.parametrize('phases', [False, True, 'simultaneous'])
def test_routed_waits_and_segments_keep_real_arrival_dependencies(count, phases, monkeypatch):
    from crowd.movements import route_movements
    from crowd.st_planner import RouteResult
    from crowd_spacing import ActorEnvelopes

    class StraightRoutes:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]

    def route_with_waits(source, goal, start, speed, *args, **kwargs):
        # Exercise both source and intermediate waits in a planner result.
        # Spatial collision checking is covered by crowd planner tests; this
        # regression checks the conversion into beats and public arrival events.
        travel = math.dist(source, goal) / speed
        middle = [(a + b) / 2 for a, b in zip(source, goal)]
        return RouteResult([(start, *source), (start + .5, *source),
                            (start + .5 + travel / 2, *middle),
                            (start + 1 + travel / 2, *middle),
                            (start + 1 + travel, *goal)], 'ok')

    monkeypatch.setattr('crowd.movements.plan_route', route_with_waits)
    actors = [f'actor_{index}' for index in range(count)]
    states = {actor: {'position': [0, 0, index * 4]} for index, actor in enumerate(actors)}
    walks = [Beat(f'move_{actor}', 'walk_to', actor, {'target': [4, 0, index * 4]},
                  duration_condition=TimingCondition('for_duration', 2 + index)) for index, actor in enumerate(actors)]
    dances = [Beat(f'dance_{actor}', 'dance', actor) for actor in actors]
    if phases:
        interactions = [InteractionPlan('travel', actors, [b.beat_id for b in walks],
                                        'simultaneous' if phases == 'simultaneous' else 'parallel'),
                        InteractionPlan('performance', actors, [b.beat_id for b in dances],
                                        'simultaneous' if phases == 'simultaneous' else 'overlap', after=['travel'])]
    else:
        interactions = [InteractionPlan('turns', actors, [b.beat_id for b in walks + dances])]
    plan = ShotPlan(beats=walks + dances, interactions=interactions)
    assert apply_interactions(plan, states) is None
    original = copy.deepcopy(plan.to_dict())
    resolver = TimelineResolver()
    envelopes = ActorEnvelopes({actor: .3 for actor in actors}, {})
    for _ in range(2):
        routed, error = route_movements(plan, states, resolver, StraightRoutes(), {}, envelopes)
        assert error is None, error
        timeline, error = resolver.resolve(routed, states)
        assert error is None
        assert validate_interactions(routed, timeline, states) is None
        scheduled = {b.beat_id: b for b in timeline.beats}
        for walk in walks:
            segments = [b for b in timeline.beats if b.params.get('auto_crowd_movement') == walk.beat_id]
            assert len(segments) == 4
            assert sum(b.function == 'wait' for b in segments) == 2
            assert timeline.events[f'{walk.beat_id}.finished'] == max(b.end_time for b in segments)
            assert scheduled[walk.beat_id].end_time == timeline.events[f'{walk.beat_id}.finished']
        if phases:
            assert min(scheduled[b.beat_id].start_time for b in dances) >= max(
                scheduled[b.beat_id].end_time for b in walks)
            assert len({scheduled[b.beat_id].start_time for b in dances}) == 1
        else:
            for first, second in zip(walks + dances, (walks + dances)[1:]):
                assert scheduled[first.beat_id].end_time <= scheduled[second.beat_id].start_time
        assert plan.to_dict() == original
        plan = ShotPlan.from_json(routed.to_json())
        original = copy.deepcopy(plan.to_dict())


@pytest.mark.parametrize('phases', [False, True, 'simultaneous'])
def test_validated_compile_preserves_group_timing_and_replays(tmp_path, phases):
    from pxr import Gf, Usd, UsdGeom
    stage = Usd.Stage.CreateNew(str(tmp_path / 'scene.usda'))
    UsdGeom.Xform.Define(stage, '/World')
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(20, .1, 20))
    stage.GetRootLayer().Save()
    root = Path(__file__).resolve().parents[1]
    compiler = ShotCompiler(root)
    catalog = FunctionCatalog(compiler.manifest)
    actors = []
    for index in range(3):
        ok, actor = catalog.place('character.human', position=[(index - 1) * 6, 0, -4])
        assert ok
        actors.append(actor['actor_id'])
    if phases == 'simultaneous':
        assert catalog.wait(actors[-1], duration='short')[0]
    for index, actor in enumerate(actors):
        ok, _ = catalog.walk_to(actor, f'point({(index - 1) * 6}, 0, 4)')
        assert ok
    for actor in actors:
        ok, _ = catalog.dance(actor, style='groove', duration='short')
        assert ok
    if phases:
        assert catalog.declare_interaction('travel', actors, [f'{a}.walk_to' for a in actors],
                                           timing='simultaneous' if phases == 'simultaneous' else 'parallel')[0]
        assert catalog.declare_interaction('performance', actors, [f'{a}.dance' for a in actors],
                                           timing='simultaneous' if phases == 'simultaneous' else 'overlap', after=['travel'])[0]
    else:
        assert catalog.declare_interaction('turns', actors,
                                           [f'{a}.walk_to' for a in actors] +
                                           [f'{a}.dance' for a in actors])[0]
    plan = catalog.plan
    if phases == 'simultaneous':
        plan.prompt = 'People walk simultaneously, then dance together after everyone arrives.'
    for name in ('first', 'replay'):
        output = tmp_path / name / 'shot.usda'
        ok, report = compiler.compile(plan, output, scene_usda_path=tmp_path / 'scene.usda', validate=True)
        assert ok, repr(report)[:3500]
        assert report['validation']['collision_passed']
        if phases == 'simultaneous':
            assert report['validation']['plan_review']['passed']
            assert report['validation']['plan_review']['requirements']
        stored = json.loads((output.parent / 'shot_plan.json').read_text())
        timeline = json.loads((output.parent / 'resolved_timeline.json').read_text())
        beats = {beat['beat_id']: beat for beat in timeline['beats']}
        if phases:
            moves = [beats[bid] for bid in stored['interactions'][0]['actions']]
            dances = [beats[bid] for bid in stored['interactions'][1]['actions']]
            assert min(b['start_time'] for b in dances) >= max(b['end_time'] for b in moves)
            assert len({b['start_time'] for b in dances}) == 1
            if phases == 'simultaneous':
                from interaction_plan import _action_interval
                from timeline_resolver import ResolvedBeat
                resolved = [ResolvedBeat(**beat) for beat in timeline['beats']]
                scheduled = {beat.beat_id: beat for beat in resolved}
                intervals = [_action_interval(action, scheduled, resolved) for action in plan.beats
                             if action.beat_id in stored['interactions'][0]['actions']]
                assert len({beat.start_time for beat in intervals}) == 1
        else:
            sequence = [beats[bid] for bid in stored['interactions'][0]['actions']]
            assert all(a['end_time'] <= b['start_time'] for a, b in zip(sequence, sequence[1:]))
        plan = ShotPlan.from_dict(stored)


def test_multiple_phase_predecessors_and_cycles():
    states = {actor: {'position': [index * 4, 0, 0]} for index, actor in enumerate(('a', 'b', 'c'))}
    plan = ShotPlan(beats=[Beat('a_wait', 'wait', 'a'), Beat('b_wait', 'wait', 'b'),
                          Beat('c_dance', 'dance', 'c')],
                    interactions=[InteractionPlan('first', ['a'], ['a_wait'], 'parallel'),
                                  InteractionPlan('second', ['b'], ['b_wait'], 'parallel'),
                                  InteractionPlan('last', ['c'], ['c_dance'], 'parallel',
                                                  after=['first', 'second'])])
    assert apply_interactions(plan, states) is None
    assert set(plan.beats[2].params['wait_for']) == {'a_wait', 'b_wait'}
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None and validate_interactions(plan, timeline, states) is None
    plan.interactions[0].after = ['last']
    assert apply_interactions(plan, states) is None
    _, error = TimelineResolver().resolve(plan, states)
    assert error is not None


@pytest.mark.parametrize('after', ['', 'earlier', [None], ['']])
def test_malformed_phase_predecessors_are_rejected(after):
    catalog = FunctionCatalog({'assets': []})
    ok, error = catalog.declare_interaction('phase', ['a'], ['a.wait'], after=after)
    assert not ok and error.error_code == 'INTERACTION_SCHEMA'
    with pytest.raises(ValueError):
        InteractionPlan.from_dict({'after': after})


@pytest.mark.parametrize('function', ['walk_to', 'dance', 'celebrate'])
def test_simultaneous_phase_waits_for_every_actors_prior_action(function):
    plan = ShotPlan(beats=[Beat('prior_b', 'wait', 'b',
                                duration_condition=TimingCondition('for_duration', 3)),
                          Beat('start_a', function, 'a'), Beat('start_b', function, 'b')],
                    interactions=[InteractionPlan('shared', ['a', 'b'], ['start_a', 'start_b'], 'simultaneous')])
    states = {'a': {'position': [-4, 0, 0]}, 'b': {'position': [4, 0, 0]}}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    scheduled = {beat.beat_id: beat for beat in timeline.beats}
    assert scheduled['start_a'].start_time == scheduled['start_b'].start_time == scheduled['prior_b'].end_time
    assert validate_interactions(plan, timeline, states) is None


def test_simultaneous_reports_conflicting_authored_windows():
    plan = ShotPlan(beats=[Beat('a_wait', 'wait', 'a', {'exact_window': [0, 4]}, TimingCondition('at', 0)),
                          Beat('b_wait', 'wait', 'b', {'exact_window': [1, 4]}, TimingCondition('at', 1))],
                    interactions=[InteractionPlan('shared', ['a', 'b'], ['a_wait', 'b_wait'], 'simultaneous')])
    states = {'a': {'position': [-4, 0, 0]}, 'b': {'position': [4, 0, 0]}}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    error = validate_interactions(plan, timeline, states)
    assert error.error_code == 'INTERACTION_SIMULTANEOUS'
    assert error.details['start_spread_s'] == 1
    assert [action['beat_id'] for action in error.details['actions']] == ['a_wait', 'b_wait']


@pytest.mark.parametrize('same_departure', [False, True])
def test_simultaneous_checks_first_motion_instead_of_final_route_segment(same_departure):
    from timeline_resolver import ResolvedBeat, ResolvedTimeline
    plan = ShotPlan(beats=[Beat('a_move', 'walk_to', 'a'), Beat('b_move', 'walk_to', 'b')],
                    interactions=[InteractionPlan('departure', ['a', 'b'], ['a_move', 'b_move'], 'simultaneous')])
    parts = [ResolvedBeat('a_first', 'a', 'walk_to', 1, 2, 1, {'auto_crowd_movement': 'a_move'}),
             ResolvedBeat('a_move', 'a', 'walk_to', 5, 6, 1, {'auto_crowd_movement': 'a_move'}),
             ResolvedBeat('b_first', 'b', 'walk_to', 1 if same_departure else 2, 3,
                          2 if same_departure else 1, {'auto_crowd_movement': 'b_move'}),
             ResolvedBeat('b_move', 'b', 'walk_to', 4 if same_departure else 5, 7,
                          3 if same_departure else 2, {'auto_crowd_movement': 'b_move'})]
    error = validate_interactions(plan, ResolvedTimeline(7, beats=parts), {'a': {}, 'b': {}})
    if same_departure:
        assert error is None  # Final segments start at different times.
    else:
        assert error.error_code == 'INTERACTION_SIMULTANEOUS'  # Final segments start together.


@pytest.mark.parametrize('permanent_delay', [False, True])
def test_routing_negotiates_common_departure_or_reports_bounded_failure(permanent_delay, monkeypatch):
    from crowd.movements import route_movements
    from crowd.config import CrowdSolverConfig
    from crowd.st_planner import RouteResult
    from crowd_spacing import ActorEnvelopes

    class StraightRoutes:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]

    calls = []

    def delayed_route(source, goal, start, speed, *args, **kwargs):
        calls.append((source[2], start))
        delay = (1 if permanent_delay else max(0, 2 - start)) if source[2] > 0 else 0
        points = [(start, *source)]
        if delay:
            points.append((start + delay, *source))
        points.append((start + delay + math.dist(source, goal) / speed, *goal))
        return RouteResult(points, 'ok')

    monkeypatch.setattr('crowd.movements.plan_route', delayed_route)
    plan = ShotPlan(beats=[Beat('a_move', 'walk_to', 'a', {'target': [4, 0, -4]}),
                          Beat('b_move', 'walk_to', 'b', {'target': [4, 0, 4]}),
                          Beat('a_dance', 'dance', 'a'), Beat('b_dance', 'dance', 'b')],
                    interactions=[InteractionPlan('travel', ['a', 'b'], ['a_move', 'b_move'], 'simultaneous'),
                                  InteractionPlan('performance', ['a', 'b'], ['a_dance', 'b_dance'],
                                                  'simultaneous', after=['travel'])])
    states = {'a': {'position': [-4, 0, -4]}, 'b': {'position': [-4, 0, 4]}}
    assert apply_interactions(plan, states) is None
    original = copy.deepcopy(plan.to_dict())
    config = CrowdSolverConfig(max_delay_s=2, max_joint_nodes=4)
    resolver = TimelineResolver()
    routed, error = route_movements(plan, states, resolver, StraightRoutes(), {},
                                    ActorEnvelopes({'a': .3, 'b': .3}, {}), config)
    assert plan.to_dict() == original
    if permanent_delay:
        assert routed is None and error.error_code == 'INTERACTION_SIMULTANEOUS'
        assert error.details['added_departure_delay_s'] > config.max_delay_s
        assert error.details['synchronization_attempts'] <= config.max_joint_nodes
    else:
        assert error is None, error
        timeline, error = resolver.resolve(routed, states)
        assert error is None and validate_interactions(routed, timeline, states) is None
        assert any(z < 0 and start == 2 for z, start in calls)
        moves = [beat for beat in timeline.beats if beat.function == 'walk_to']
        assert {beat.start_time for beat in moves} == {2}
        dances = [beat for beat in timeline.beats if beat.function == 'dance']
        assert len({beat.start_time for beat in dances}) == 1
        assert min(beat.start_time for beat in dances) >= max(beat.end_time for beat in moves)


def test_real_routing_waits_for_crossing_actor_before_shared_departure():
    from crowd.movements import route_movements
    from crowd.config import CrowdSolverConfig
    from crowd_spacing import ActorEnvelopes
    from crowd.trajectories import planning_tracks
    from crowd.st_planner import Reservation
    from crowd.joint import first_conflict

    class StraightRoutes:
        def _collect_obstacles_at_time(self, *args): return []
        def _plan_smooth_path(self, source, goal, obstacles): return [source, goal]

    states = {'a': {'position': [-4, 0, -4]}, 'b': {'position': [-4, 0, 2]},
              'crossing': {'position': [0, 0, 2]}}
    plan = ShotPlan(beats=[
        Beat('crossing_wait', 'wait', 'crossing', duration_condition=TimingCondition('for_duration', 2)),
        Beat('crossing_move', 'walk_to', 'crossing',
             {'brief_path': {'points': [[0, 0, 2], [0, 0, 8]]}},
             TimingCondition('at', 2), TimingCondition('for_duration', 2)),
        Beat('a_move', 'walk_to', 'a', {'target': [4, 0, -4]},
             duration_condition=TimingCondition('for_duration', 3)),
        Beat('b_move', 'walk_to', 'b', {'target': [4, 0, 2]},
             duration_condition=TimingCondition('for_duration', 3))],
        interactions=[InteractionPlan('depart', ['a', 'b'], ['a_move', 'b_move'], 'simultaneous')])
    assert apply_interactions(plan, states) is None
    config = CrowdSolverConfig()
    resolver = TimelineResolver()
    synthesizer = StraightRoutes()
    routed, error = route_movements(plan, states, resolver, synthesizer, {},
                                    ActorEnvelopes({actor: .3 for actor in states}, {}), config)
    assert error is None, error
    timeline, error = resolver.resolve(routed, states)
    assert error is None and validate_interactions(routed, timeline, states) is None
    departures = [beat.start_time for beat in timeline.beats
                  if beat.actor_id in ('a', 'b') and beat.function == 'walk_to']
    assert len(set(departures)) == 1 and departures[0] > 0
    tracks, _, error = planning_tracks(timeline, states, resolver, synthesizer, {})
    assert error is None
    reservations = [Reservation(actor, track, .3) for actor, track in tracks.items()]
    assert first_conflict(reservations, 0, timeline.total_duration_s, config) is None


@pytest.mark.parametrize('references,expected', [
    (['A.run_to', 'A.run_to#2'], ['first', 'second']),
    (['A.run_to#2', 'a.run_to'], ['second', 'first']),
    (['A.run_to', 'a.run_to'], ['first', 'second']),
    (['A.run_to', 'second'], ['first', 'second']),
    (['first', 'A.run_to'], ['first', 'second']),
    (['A.run_to#1', 'a.run_to#2'], ['first', 'second']),
])
def test_repeated_action_references_resolve_without_guessing(references, expected):
    plan = ShotPlan(beats=[Beat('first', 'run_to', 'actor'), Beat('second', 'run_to', 'actor')],
                    interactions=[InteractionPlan('travel', ['A'], references, 'parallel')])
    states = {'actor': {'label': 'A'}}
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == expected
    # Bindings remain stable after serialization and generated action insertion.
    restored = ShotPlan.from_json(plan.to_json())
    restored.beats.insert(0, Beat('generated', 'run_to', 'actor'))
    assert apply_interactions(restored, states) is None
    assert restored.interactions[0].actions == expected


@pytest.mark.parametrize('count', [2, 3, 7])
def test_unnumbered_repeated_actions_follow_authored_order_per_actor(count):
    beats = [Beat(f'{actor}_{index}', 'wait', actor) for index in range(count) for actor in ('a', 'b')]
    refs = [f'{actor}.wait' for _ in range(count) for actor in ('a', 'b')]
    plan = ShotPlan(beats=beats, interactions=[InteractionPlan('turns', ['a', 'b'], refs, 'sequence')])
    states = {'a': {}, 'b': {}}
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == [beat.beat_id for beat in beats]
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None


@pytest.mark.parametrize('references', [
    ['a.wait'], ['a.wait', 'a.wait#2'], ['a.wait', 'a.wait'],
    ['a.wait#4'], ['a.wait#1', 'a.wait#1'], ['a.wait', 'first', 'first'],
])
def test_partial_or_duplicate_occurrences_still_fail(references):
    plan = ShotPlan(beats=[Beat(name, 'wait', 'a') for name in ('first', 'second', 'third')],
                    interactions=[InteractionPlan('turns', ['a'], references, 'sequence')])
    error = apply_interactions(plan, {'a': {}})
    assert error and error.error_code == 'INTERACTION_ACTION_REFERENCE'


def test_calls_with_first_drive_unnumbered_pass_plan_review():
    scene = {'zones': [{'id': 'first_stop', 'min': [3, 0, -1], 'max': [5, 0, 1]},
                       {'id': 'second_stop', 'min': [3, 0, 7], 'max': [5, 0, 9]}]}
    catalog = FunctionCatalog({'assets': [{'id': 'vehicle', 'tags': ['vehicle']}]}, scene)
    planner = LLMShotPlanner(catalog)
    calls = [{'function': 'place', 'args': {'asset_id': 'vehicle', 'label': 'Transport'}},
             {'function': 'declare_interaction', 'args': {
                 'interaction_id': 'travel', 'participants': ['Transport'],
                 'actions': ['Transport.drive_to', 'Transport.drive_to#2', 'Transport.brake_to_stop'],
                 'timing': 'sequence'}},
             {'function': 'drive_to', 'args': {'vehicle': 'Transport', 'target': 'first_stop'}},
             {'function': 'drive_to', 'args': {'vehicle': 'Transport', 'target': 'second_stop'}},
             {'function': 'brake_to_stop', 'args': {'vehicle': 'Transport'}}]
    ok, plan, logs = planner.plan_from_llm_json({'calls': calls})
    assert ok, logs
    actor = plan.setup[0].actor_id
    states = {actor: {'label': 'Transport', 'position': [0, 0, 0], 'is_vehicle': True}}
    from plan_review import review_plan
    assert review_plan(plan, scene, catalog.manifest, states)['passed']
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == [beat.beat_id for beat in plan.beats]
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    assert validate_interactions(plan, timeline, states) is None


@pytest.mark.parametrize('action', ['jump', 'clapping', 'reaction'])
@pytest.mark.parametrize('reference_function', ['perform', 'action'])
def test_parameterized_tool_references_bind_to_the_created_action(action, reference_function):
    catalog = FunctionCatalog({'assets': [{'id': 'person', 'tags': ['character']}]})
    catalog.place('person', label='Performer')
    assert catalog.perform('Performer', action)[0]
    assert catalog.celebrate('Performer', style='cheer')[0]
    reference = action if reference_function == 'action' else reference_function
    assert catalog.declare_interaction('performance', ['Performer'],
                                        [f'Performer.{reference}', 'Performer.celebrate'])[0]
    states = {catalog.plan.setup[0].actor_id: {'label': 'Performer', 'position': [0, 0, 0]}}
    restored = ShotPlan.from_json(catalog.plan.to_json())
    assert restored.beats[0].function == action
    assert restored.beats[0].source_function == 'perform'
    assert apply_interactions(restored, states) is None
    assert restored.interactions[0].actions == [beat.beat_id for beat in restored.beats]
    timeline, error = TimelineResolver().resolve(restored, states)
    assert error is None
    assert validate_interactions(restored, timeline, states) is None


def test_perform_aliases_and_repeated_occurrences_pass_queue_plan_review():
    planner = LLMShotPlanner(FunctionCatalog({'assets': [{'id': 'person', 'tags': ['character']}]}))
    calls = [{'function': 'place', 'args': {'asset_id': 'person', 'label': 'Performer'}},
             {'function': 'declare_interaction', 'args': {
                 'interaction_id': 'performance', 'participants': ['Performer'],
                 'actions': ['Performer.perform', 'Performer.perform#2', 'Performer.celebrate'],
                 'timing': 'sequence'}},
             {'function': 'perform', 'args': {'actor': 'Performer', 'action': 'jump'}},
             {'function': 'perform', 'args': {'actor': 'Performer', 'action': 'clapping'}},
             {'function': 'celebrate', 'args': {'actor': 'Performer', 'style': 'cheer'}}]
    ok, plan, logs = planner.plan_from_llm_json({'calls': calls})
    assert ok, logs
    assert [beat.source_function for beat in plan.beats] == ['perform', 'perform', 'celebrate']
    states = {plan.setup[0].actor_id: {'label': 'Performer', 'position': [0, 0, 0]}}
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == [beat.beat_id for beat in plan.beats]
    # Scheduling-generated beats cannot redirect already bound tool references.
    plan.beats.insert(0, Beat('generated', 'reaction', plan.setup[0].actor_id, source_function='perform'))
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == [beat.beat_id for beat in plan.beats[1:]]


def test_tool_alias_is_not_invented_for_a_differently_created_action():
    plan = ShotPlan(beats=[Beat('jump', 'jump', 'a')],
                    interactions=[InteractionPlan('performance', ['a'], ['a.perform'])])
    error = apply_interactions(plan, {'a': {}})
    assert error.error_code == 'INTERACTION_ACTION_REFERENCE'
    assert 'absent' in error.message
    assert 'source_function' not in plan.beats[0].to_dict()


def test_tool_provenance_works_for_other_parameterized_catalog_methods():
    class ParameterizedCatalog(FunctionCatalog):
        def choose_action(self, actor, action):
            return self._create_beat(action, actor, {})
    planner = LLMShotPlanner(ParameterizedCatalog({'assets': [{'id': 'person', 'tags': ['character']}]}))
    ok, plan, logs = planner.plan_from_llm_json({'calls': [
        {'function': 'place', 'args': {'asset_id': 'person', 'label': 'Performer'}},
        {'function': 'choose_action', 'args': {'actor': 'Performer', 'action': 'clapping'}},
        {'function': 'declare_interaction', 'args': {'interaction_id': 'performance',
         'participants': ['Performer'], 'actions': ['Performer.choose_action']}}]})
    assert ok, logs
    assert plan.beats[0].source_function == 'choose_action'
    states = {plan.setup[0].actor_id: {'label': 'Performer'}}
    assert apply_interactions(plan, states) is None
    assert plan.interactions[0].actions == [plan.beats[0].beat_id]


@pytest.mark.parametrize('references', [['a.perform'], ['a.perform#3'], ['a.perform#1', 'a.jump']])
def test_tool_alias_ambiguity_and_duplicate_selection_remain_errors(references):
    plan = ShotPlan(beats=[Beat('first', 'jump', 'a', source_function='perform'),
                           Beat('second', 'clapping', 'a', source_function='perform')],
                    interactions=[InteractionPlan('performance', ['a'], references)])
    error = apply_interactions(plan, {'a': {}})
    assert error and error.error_code == 'INTERACTION_ACTION_REFERENCE'


@pytest.mark.parametrize('timing', ['overlap', 'simultaneous'])
@pytest.mark.parametrize('actions', [
    ['performer.wait', 'partner.wait', 'performer.dance'],
    ['Performer.wait#1', 'performer.wait#2'],
])
def test_conflicting_forward_interaction_is_rejected_without_mutating_catalog(timing, actions):
    catalog = FunctionCatalog({'assets': []})
    before = copy.deepcopy(catalog.plan.to_dict())
    ok, error = catalog.declare_interaction('exchange', ['performer', 'partner'], actions, timing)
    assert not ok and error.error_code == 'INTERACTION_OVERLAP'
    assert catalog.plan.to_dict() == before
    assert error.actor_id == 'performer'
    assert error.details['interaction_id'] == 'exchange'
    assert len(error.details['conflicting_actions']) == 2
    assert all(item['reference'] in error.message for item in error.details['conflicting_actions'])
    assert 'after' in error.suggestions[0]


@pytest.mark.parametrize('timing', ['overlap', 'simultaneous'])
@pytest.mark.parametrize('references', ['aliases', 'beat_ids', 'mixed'])
def test_catalog_guard_resolves_existing_actor_aliases_and_exact_beats(timing, references):
    catalog = FunctionCatalog({'assets': [{'id':'character.human'}]})
    catalog.place('character.human', label='Performer')
    actor = catalog.plan.setup[0].actor_id
    catalog.wait('Performer')
    catalog.dance('Performer')
    first, second = catalog.plan.beats
    actions = {'aliases': ['Performer.wait', f'{actor}.dance'],
               'beat_ids': [first.beat_id, second.beat_id],
               'mixed': [first.beat_id, f'{actor}.dance']}[references]
    before = copy.deepcopy(catalog.plan.to_dict())
    ok, error = catalog.declare_interaction('exchange', ['Performer'], actions, timing)
    assert not ok and error.actor_id == actor
    assert catalog.plan.to_dict() == before


def test_unresolved_beat_ids_get_canonical_conflict_details_at_complete_plan_check():
    catalog = FunctionCatalog({'assets': []})
    assert catalog.declare_interaction('exchange', ['a'], ['first', 'second'], 'overlap')[0]
    catalog.plan.beats = [Beat('first', 'wait', 'a'), Beat('second', 'dance', 'a')]
    error = apply_interactions(catalog.plan, {'a': {}})
    assert error.error_code == 'INTERACTION_OVERLAP'
    assert error.actor_id == 'a' and error.beat_id == 'second'
    assert [a['beat_id'] for a in error.details['conflicting_actions']] == ['first','second']
    from plan_review import review_suggestions
    suggestions = review_suggestions({'violations': [{'code':'INVALID_INTERACTION', 'error':error.to_dict()}]})
    assert 'first' in suggestions[0] and 'second' in suggestions[0]
    assert any('after' in item for item in suggestions)
    assert 'Actions: .' not in ' '.join(suggestions)


@pytest.mark.parametrize('timing', ['sequence','parallel'])
def test_sequential_actor_actions_are_valid_when_shared_overlap_is_not_requested(timing):
    catalog = FunctionCatalog({'assets': []})
    assert catalog.declare_interaction('phase', ['a'], ['a.wait', 'a.dance'], timing)[0]
    catalog.plan.beats = [Beat('first','wait','a'), Beat('second','dance','a')]
    assert apply_interactions(catalog.plan, {'a': {}}) is None


def test_invalid_generated_phase_stops_before_forward_action_calls():
    planner = LLMShotPlanner(FunctionCatalog({'assets':[{'id':'character.human'}]}))
    ok, plan, logs = planner.plan_from_llm_json({'calls': [
        {'function':'declare_interaction', 'args':{'interaction_id':'phase', 'participants':['a'],
            'actions':['a.wait','a.dance'], 'timing':'overlap'}},
        {'function':'place', 'args':{'asset_id':'character.human','label':'a'}},
        {'function':'wait', 'args':{'actor':'a'}},
        {'function':'dance', 'args':{'actor':'a'}},
    ]})
    assert not ok and not plan.setup and not plan.beats and not plan.interactions
    error = planner.last_plan_review['violations'][0]['error']
    assert error['error_code'] == 'INTERACTION_OVERLAP'
    assert error['details']['interaction_id'] == 'phase'


def test_api_contract_failure_is_returned_to_retry_loop_instead_of_semantic_fallback(monkeypatch):
    planner = LLMShotPlanner(FunctionCatalog({'assets':[]}))
    monkeypatch.setenv('PAINTERSPACE_LLM_URL', 'unused-test-endpoint')
    def generated(*args, **kwargs):
        return planner._invoke_catalog_call('declare_interaction', {
            'interaction_id':'phase', 'participants':['a'],
            'actions':['a.wait','a.dance'], 'timing':'simultaneous'})
    monkeypatch.setattr(planner, '_plan_with_llm_api', generated)
    monkeypatch.setattr(planner, '_plan_general_semantic', lambda *a, **kw: pytest.fail('invalid plan was hidden by fallback'))
    ok, plan, logs = planner.plan_from_prompt('A performs successive actions')
    assert not ok and planner.last_plan_review['violations'][0]['error']['error_code'] == 'INTERACTION_OVERLAP'


def test_generator_contract_and_split_phases_preserve_concurrency_then_causality():
    planner = LLMShotPlanner(FunctionCatalog({'assets':[{'id':'character.human'}]}))
    schema = next(tool['function'] for tool in planner.export_tool_schemas()
                  if tool['function']['name'] == 'declare_interaction')
    assert 'at most one full-body action per actor' in schema['description']
    assert 'at most one full-body action per actor' in planner.build_system_prompt(False, False, False)
    calls = [
        {'function':'declare_interaction','args':{'interaction_id':'shared','participants':['a','b'],
            'actions':['a.wait#1','b.wait'],'timing':'simultaneous'}},
        {'function':'declare_interaction','args':{'interaction_id':'followup','participants':['a'],
            'actions':['a.wait#2'],'timing':'sequence','after':['shared']}},
        *[{'function':'place','args':{'asset_id':'character.human','label':actor}} for actor in ('a','b')],
        *[{'function':'wait','args':{'actor':actor}} for actor in ('a','b','a')],
    ]
    ok, plan, logs = planner.plan_from_llm_json({'calls':calls})
    assert ok, logs
    states = {s.actor_id:{'label':s.label,'position':[0,0,0]} for s in plan.setup}
    assert apply_interactions(plan, states) is None
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None and validate_interactions(plan,timeline,states) is None
    beats = {b.beat_id:b for b in timeline.beats}
    first, partner = [beats[key] for key in plan.interactions[0].actions]
    following = beats[plan.interactions[1].actions[0]]
    assert first.start_time == partner.start_time
    assert following.start_time >= max(first.end_time, partner.end_time)


def test_generation_retry_receives_conflicts_and_only_compiles_corrected_phases(tmp_path, monkeypatch):
    from types import SimpleNamespace
    planner = LLMShotPlanner(FunctionCatalog({'assets':[{'id':'character.human'}]}))
    monkeypatch.setenv('PAINTERSPACE_LLM_URL', 'unused-test-endpoint')
    feedbacks = []
    def generated(prompt, api_key, llm_url, error_feedback=None):
        feedbacks.append(error_feedback)
        if len(feedbacks) == 1:
            return planner._invoke_catalog_call('declare_interaction', {
                'interaction_id':'shared', 'participants':['performer'],
                'actions':['performer.wait#1','performer.wait#2'], 'timing':'overlap'})
        calls = [
            ('place', {'asset_id':'character.human','label':'performer'}),
            ('wait', {'actor':'performer'}), ('wait', {'actor':'performer'}),
            ('declare_interaction', {'interaction_id':'first','participants':['performer'],
                'actions':['performer.wait#1'],'timing':'overlap'}),
            ('declare_interaction', {'interaction_id':'later','participants':['performer'],
                'actions':['performer.wait#2'],'timing':'sequence','after':['first']}),
        ]
        return all(planner._invoke_catalog_call(function, args) for function, args in calls)
    monkeypatch.setattr(planner, '_plan_with_llm_api', generated)
    compiled = []
    def compile_plan(plan, *args, **kwargs):
        assert len(feedbacks) == 2
        assert plan.interactions[1].after == ['first']
        compiled.append(plan)
        return True, {'success':True}
    ok, report, logs = planner.plan_and_compile_with_retry(
        '', SimpleNamespace(compile=compile_plan), tmp_path/'shot.usda', max_retries=1)
    assert ok and len(compiled) == 1
    assert feedbacks[0] is None
    assert all(value in feedbacks[1] for value in ('shared','performer.wait#1','performer.wait#2','after'))
