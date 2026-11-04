"""Prompt/plan mismatches are rejected before route synthesis or publication."""
import copy
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools' / 'shot_library'), str(ROOT / 'tools' / 'collision')]
from plan_model import Beat, ShotPlan, SetupAction, InteractionPlan
from plan_review import review_plan
from llm_planner import LLMShotPlanner
from function_catalog import FunctionCatalog


def group_plan(count, function, timing, prompt):
    actors = [f'person_{index}' for index in range(count)]
    beats = [Beat(f'action_{actor}', function, actor) for actor in actors]
    return ShotPlan(prompt=prompt,
                    setup=[SetupAction('place', actor, 'character.human', position=[index * 4, 0, 0])
                           for index, actor in enumerate(actors)],
                    beats=beats,
                    interactions=[InteractionPlan('shared', actors, [b.beat_id for b in beats], timing)])


@pytest.mark.parametrize('count', [2, 7, 13])
@pytest.mark.parametrize('function,words', [('dance', 'dancing'), ('celebrate', 'celebrate'),
                                           ('run_to', 'running'), ('jump_over', 'jump')])
def test_together_rejects_serialized_actions_generally(count, function, words):
    plan = group_plan(count, function, 'sequence', f'{count} people {words} together.')
    before = plan.to_json()
    report = review_plan(plan)
    assert not report['passed']
    conflict = next(v for v in report['violations'] if v['code'] == 'SIMULTANEOUS')
    assert len(conflict['beat_ids']) == count
    assert plan.to_json() == before
    plan.interactions[0].timing = 'simultaneous'
    assert review_plan(plan)['passed']


@pytest.mark.parametrize('timing', ['parallel', 'overlap'])
def test_equal_starts_need_a_contract_not_a_coincidental_provisional_timeline(timing):
    plan = group_plan(3, 'wave', timing, 'Everybody waves at the same time.')
    assert not review_plan(plan)['passed']


def test_one_by_one_is_preserved_and_negated_together_is_not_imposed():
    plan = group_plan(4, 'dance', 'sequence', 'Do not dance together; dance one by one.')
    assert review_plan(plan)['passed']
    plan.interactions[0].timing = 'simultaneous'
    report = review_plan(plan)
    assert not report['passed']
    assert any(v['code'] == 'SEQUENCE' for v in report['violations'])


def test_missing_arrival_barrier_and_valid_shared_readiness():
    plan = group_plan(3, 'dance', 'simultaneous', 'Everybody walks, then everyone dances together after everyone arrives.')
    actors = [setup.actor_id for setup in plan.setup]
    walks = [Beat(f'move_{actor}', 'walk_to', actor, {'target': [index * 4, 0, 8]})
             for index, actor in enumerate(actors)]
    # Wrong order permits performance before travel, despite shared dance timing.
    plan.beats += walks
    report = review_plan(plan)
    assert not report['passed']
    assert any(v['code'] == 'PHASE_BARRIER' for v in report['violations'])
    plan.beats = walks + plan.beats[:-len(walks)]
    # Shared readiness injected by the simultaneous declaration is a valid
    # barrier even without an explicit after array; no syntactic shortcut needed.
    assert review_plan(plan)['passed']


def test_independent_sentences_do_not_invent_phase_order():
    actors = ['Alice', 'Ben', 'Cara', 'Dev']
    plan = ShotPlan(prompt='Alice and Ben dance together; Cara and Dev dance together.',
                    setup=[SetupAction('place', actor, 'character.human', label=actor) for actor in actors],
                    beats=[Beat(actor + '_dance', 'dance', actor) for actor in actors],
                    interactions=[InteractionPlan('first', actors[:2], [a + '_dance' for a in actors[:2]], 'simultaneous'),
                                  InteractionPlan('other', actors[2:], [a + '_dance' for a in actors[2:]], 'simultaneous')])
    report = review_plan(plan)
    assert report['passed'], report
    assert not any(r['kind'] == 'phase_barrier' for r in report['requirements'])


def test_simultaneous_different_actions_with_named_subjects():
    plan = ShotPlan(prompt='Alice runs and Ben jumps at the same time.',
                    setup=[SetupAction('place', actor, 'character.human', label=actor) for actor in ('Alice', 'Ben')],
                    beats=[Beat('run', 'run_to', 'Alice'), Beat('jump', 'jump_over', 'Ben')],
                    interactions=[InteractionPlan('shared', ['Alice', 'Ben'], ['run', 'jump'], 'sequence')])
    assert not review_plan(plan)['passed']
    plan.interactions[0].timing = 'simultaneous'
    assert review_plan(plan)['passed']


def test_missing_actions_do_not_trivially_satisfy_a_shared_action_request():
    plan = group_plan(2, 'dance', 'simultaneous', 'Everybody dances together.')
    plan.beats = []
    plan.interactions = []
    assert not review_plan(plan)['passed']
    plan = ShotPlan(prompt='Alice and Ben dance together.',
                    setup=[SetupAction('place', name, 'character.human', label=name) for name in ('Alice', 'Ben')],
                    beats=[Beat('dance', 'dance', 'Alice'), Beat('wait', 'wait', 'Ben')])
    assert any(v['code'] == 'MISSING_GROUP_ACTION' for v in review_plan(plan)['violations'])


@pytest.mark.parametrize('heading,offset', [(0, [0, 0]), (90, [17, -9]), (35, [-12, 25])])
@pytest.mark.parametrize('correct_side', [False, True])
def test_source_direction_uses_scene_geometry_at_different_rotations(heading, offset, correct_side):
    angle = math.radians(heading)
    def world(x, z=0):
        return [offset[0] + x * math.cos(angle) - z * math.sin(angle), 0,
                offset[1] + x * math.sin(angle) + z * math.cos(angle)]
    scene = {'objects': [{'name': 'Depot', 'position': world(20)}]}
    plan = ShotPlan(prompt='People walk from the direction of Depot.',
                    setup=[SetupAction('place', 'walker', 'character.human', position=world(10 if correct_side else -10))],
                    beats=[Beat('move', 'walk_to', 'walker', {'target': world(0)})])
    before = copy.deepcopy((plan.to_dict(), scene))
    report = review_plan(plan, scene)
    assert report['passed'] == correct_side, report
    assert (plan.to_dict(), scene) == before


def test_compound_landscape_uses_owned_footprints_instead_of_central_pivot():
    scene = {'objects': [{'name': 'Landscape', 'asset': 'environment.area', 'position': [0, 0, 0]}],
             'solid_boxes': [{'owner': 'Landscape', 'min': [-20, 0, 10], 'max': [20, 2, 15]},
                             {'owner': 'Landscape', 'min': [-20, 0, -15], 'max': [20, 2, -10]}]}
    manifest = {'assets': [{'id': 'environment.area', 'tags': ['building']}]}
    plan = ShotPlan(prompt='People approach from the direction of the buildings.',
                    setup=[SetupAction('place', 'a', 'character.human', position=[0, 0, 8]),
                           SetupAction('place', 'b', 'character.human', position=[0, 0, -8])],
                    beats=[Beat('a_move', 'walk_to', 'a', {'target': [0, 0, 2]}),
                           Beat('b_move', 'walk_to', 'b', {'target': [0, 0, -2]})])
    assert review_plan(plan, scene, manifest)['passed']
    plan.setup[0].position = [0, 0, 0]
    assert not review_plan(plan, scene, manifest)['passed']


def test_source_names_match_the_whole_name_instead_of_a_shared_prefix():
    scene = {'objects': [{'name': 'OldDepot', 'position': [20, 0, 0]},
                         {'name': 'OldBarn', 'position': [-20, 0, 0]}]}
    plan = ShotPlan(prompt='People walk from the direction of Old Depot.',
                    setup=[SetupAction('place', 'a', 'character.human', position=[-10, 0, 0])],
                    beats=[Beat('move', 'walk_to', 'a', {'target': [0, 0, 0]})])
    assert not review_plan(plan, scene)['passed']
    plan.setup[0].position = [10, 0, 0]
    assert review_plan(plan, scene)['passed']


def test_unverified_sources_and_unresolved_explicit_timing_report_limitations():
    plan = group_plan(3, 'dance', 'simultaneous', 'Everybody approaches from the direction of UnknownPlace.')
    report = review_plan(plan)
    assert not report['passed'] and report['violations'][0]['code'] == 'SOURCE_DIRECTION'
    plan.prompt = 'Everybody does an unspecified activity together.'
    report = review_plan(plan)
    assert not report['passed'] and report['violations'][0]['code'] == 'SIMULTANEOUS'


def test_explicit_director_tracks_have_precedence_and_override_is_visible():
    plan = group_plan(3, 'dance', 'sequence', 'Everybody dances together.')
    report = review_plan(plan, locked_actors=[setup.actor_id for setup in plan.setup])
    assert report['passed'] and report['overrides']


def test_json_calls_fail_review_with_specific_feedback_before_compile():
    planner = LLMShotPlanner(FunctionCatalog({'assets': [{'id': 'character.human'}]}))
    calls = {'calls': [
        {'function': 'place', 'args': {'asset_id': 'character.human', 'label': actor}} for actor in ('Alice', 'Ben')] + [
        {'function': 'dance', 'args': {'actor': actor}} for actor in ('Alice', 'Ben')] + [
        {'function': 'declare_interaction', 'args': {'interaction_id': 'turns',
         'participants': ['Alice', 'Ben'], 'actions': ['Alice.dance', 'Ben.dance'], 'timing': 'sequence'}}]}
    ok, _, logs = planner.plan_from_llm_json(calls, 'Alice and Ben dance together.')
    assert not ok
    assert planner.last_plan_review['violations'][0]['code'] == 'SIMULTANEOUS'
    assert any('[Plan review]' in log for log in logs)
    calls['calls'][-1]['args']['timing'] = 'simultaneous'
    ok, _, _ = planner.plan_from_llm_json(calls, 'Alice and Ben dance together.')
    assert ok and planner.last_plan_review['passed']


def test_compiler_rejects_bad_intent_before_synthesis_and_preserves_current_shot(tmp_path, monkeypatch):
    from compiler import ShotCompiler
    from motion_synthesis import MotionSynthesizer
    compiler = ShotCompiler(ROOT)
    output = tmp_path / 'shot' / 'shot.usda'
    output.parent.mkdir()
    accepted = '#usda 1.0\ndef Xform "World" {}\n'
    output.write_text(accepted)
    def forbidden(*args, **kwargs):
        pytest.fail('Synthesis must not run after plan review fails.')
    monkeypatch.setattr(MotionSynthesizer, 'synthesize_actor', forbidden)
    plan = group_plan(3, 'dance', 'sequence', 'Three people dance together.')
    ok, report = compiler.compile(plan, output, validate=True)
    assert not ok and report['error']['error_code'] == 'PLAN_REVIEW'
    assert output.read_text() == accepted
    assert not list(output.parent.glob('anim_*.usda'))


def test_retry_does_not_compile_rejected_plan(tmp_path, monkeypatch):
    from compiler import ShotCompiler
    planner = LLMShotPlanner(FunctionCatalog({'assets': []}))
    def bad_plan(prompt, error_feedback=None):
        planner.last_plan_review = {'passed': False, 'violations': [{'code': 'SIMULTANEOUS'}]}
        return False, group_plan(2, 'dance', 'sequence', prompt), ['bad plan']
    monkeypatch.setattr(planner, 'plan_from_prompt', bad_plan)
    compiler = ShotCompiler(ROOT)
    def forbidden(*args, **kwargs):
        pytest.fail('Rejected plans must return to planning before compilation.')
    monkeypatch.setattr(compiler, 'compile', forbidden)
    ok, report, _ = planner.plan_and_compile_with_retry('Dance together', compiler, tmp_path / 'shot.usda', max_retries=1)
    assert not ok and report['error']['error_code'] == 'PLAN_REVIEW'


def test_retry_feedback_retains_action_ids_and_recovery_advice():
    from plan_review import review_suggestions
    from director_brief import symbolic_feedback
    plan = group_plan(3, 'dance', 'sequence', 'All three people dance together.')
    report = review_plan(plan)
    feedback = symbolic_feedback({'error_code': 'PLAN_REVIEW', 'details': report,
                                  'suggestions': review_suggestions(report)})
    assert 'action_person_0' in feedback['suggestions'][0]
    assert 'simultaneous' in feedback['suggestions'][0]


def test_response_execution_returns_review_feedback_without_compilation(tmp_path, monkeypatch):
    import compile_shot as cli
    calls = {'prompt': 'Alice and Ben dance together.', 'calls': [
        {'function': 'place', 'args': {'asset_id': 'character.human', 'label': actor}} for actor in ('Alice', 'Ben')] + [
        {'function': 'dance', 'args': {'actor': actor}} for actor in ('Alice', 'Ben')] + [
        {'function': 'declare_interaction', 'args': {'interaction_id': 'turns',
         'participants': ['Alice', 'Ben'], 'actions': ['Alice.dance', 'Ben.dance'], 'timing': 'sequence'}}]}
    response = tmp_path / 'response.json'
    response.write_text(json.dumps(calls))
    class Compiler:
        def __init__(self, *args, **kwargs):
            self.manifest = {'assets': [{'id': 'character.human'}]}
        def compile(self, *args, **kwargs):
            pytest.fail('Response rejected by review must not compile.')
    captured = []
    monkeypatch.setattr(cli, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(cli, 'ShotCompiler', Compiler)
    monkeypatch.setattr(cli, '_write_validation_feedback', lambda queue, response, payload: captured.append(payload))
    code = cli.execute_llm_response(response, out_override=tmp_path / 'shot' / 'shot.usda')
    assert code == 2 and len(captured) == 1
    assert captured[0]['stage'] == 'plan_review'
    assert captured[0]['error_code'] == 'PLAN_REVIEW'
    assert captured[0]['details']['violations'][0]['code'] == 'SIMULTANEOUS'
    assert captured[0]['suggestions']
