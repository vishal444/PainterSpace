"""Validation feedback, bounded retries, and publication of checked artifacts."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'shot_library'))

from compiler import ShotCompiler
from compile_transaction import CompileTransaction, signatures
from crowd.config import CrowdSolverConfig
from crowd.conformance import check_conformance
from crowd.repair import next_clearance
from crowd_spacing import ActorEnvelopes
from motion_synthesis import ActorPerformance, Keyframe
from motion_synthesis import MotionSynthesizer
from plan_model import Beat, SetupAction, ShotPlan, TimingCondition
from timeline_resolver import TimelineResolver


def root_failure(depth):
    return {'success': False, 'error': {'error_code': 'CROWD_CONFORMANCE',
            'details': {'conflicts': [{'actor': 'a', 'other_actor': 'b', 'time_s': 1,
                                      'distance_m': .5, 'required_separation_m': .5 + depth}]}}}


def posed_failure(depth):
    return {'success': False, 'validation': {'success': False, 'collision_passed': False,
            'collision_report': {'ok': False, 'violations': [{'worst_depth_m': depth}],
                                 'level_violations': [], 'unprotected_assets': [],
                                 'unprotected_actors': []}}}


@pytest.mark.parametrize('failure', [root_failure, posed_failure])
def test_clearance_feedback_is_measured_and_capped(failure):
    config = CrowdSolverConfig(buffer_m=.15, cell_size_m=.4, max_repair_clearance_m=.5)
    assert next_clearance(config, .15, failure(.3)) == pytest.approx(.45)
    assert next_clearance(config, .45, failure(.01)) == pytest.approx(.65)
    assert next_clearance(config, .65, failure(.3)) is None


@pytest.mark.parametrize('issue', ['level_violations', 'unprotected_assets', 'unprotected_actors', 'error'])
def test_unverifiable_failures_do_not_trigger_spatial_repair(issue):
    report = posed_failure(.2)
    report['validation']['collision_report'][issue] = ['cannot certify']
    config = CrowdSolverConfig()
    assert next_clearance(config, config.buffer_m, report) is None


@pytest.mark.parametrize('config', [{'max_repair_attempts': -1}, {'max_repair_attempts': 1.5},
                                   {'max_repair_clearance_m': -1}, {'max_repair_clearance_m': float('inf')}])
def test_invalid_repair_budgets_are_rejected(config):
    with pytest.raises(ValueError):
        CrowdSolverConfig(**config)


@pytest.mark.parametrize('failure', [root_failure, posed_failure])
def test_retries_rebuild_original_plan_and_publish_only_passing_candidate(tmp_path, monkeypatch, failure):
    output = tmp_path / 'shot' / 'shot.usda'
    output.parent.mkdir()
    output.write_text('accepted shot')
    edits = output.parent / 'user_edits.usda'
    edits.write_text('authored edits')
    plan = ShotPlan(beats=[Beat('locked', 'wait', 'a', {'exact_window': [1, 2]},
                               TimingCondition('at', 1), TimingCondition('for_duration', 1))])
    original = copy.deepcopy(plan.to_dict())
    seen = []

    def attempt(self, source, target, scene, validate):
        assert validate
        assert source.to_dict() == original
        assert target.parent != output.parent
        assert output.read_text() == 'accepted shot'
        assert edits.read_text() == 'authored edits'
        seen.append(self.crowd_config.buffer_m)
        source.beats[0].params['attempt_specific'] = True
        target.write_text('candidate')
        (target.parent / 'anim_a.usda').write_text('candidate animation')
        (target.parent / 'user_edits.usda').write_text('checked edits')
        if len(seen) == 1:
            return False, failure(.35)
        validation = {'success': True, 'collision_passed': True, 'collision_report': {'ok': True}}
        (target.parent / 'validation_report.json').write_text(json.dumps(validation))
        return True, {'success': True, 'validation': validation,
                      'artifacts': {'shot_usda': str(target)}}

    monkeypatch.setattr(ShotCompiler, '_compile_once', attempt)
    compiler = ShotCompiler(ROOT)
    ok, report = compiler.compile(plan, output)
    assert ok, report
    assert len(seen) == 2 and seen[1] - seen[0] == pytest.approx(.35)
    assert compiler.crowd_config.buffer_m == seen[0]
    assert plan.to_dict() == original
    assert output.read_text() == 'candidate'
    assert edits.read_text() == 'checked edits'
    assert report['artifacts']['shot_usda'] == str(output)
    assert report['crowd_repair']['retries'] == 1
    saved = json.loads((output.parent / 'validation_report.json').read_text())
    assert saved['crowd_repair']['retries'] == 1
    assert not list(tmp_path.glob('.shot-compile-*'))


@pytest.mark.parametrize('existing', [False, True])
def test_exhaustion_keeps_accepted_artifacts_and_edits(tmp_path, monkeypatch, existing):
    output = tmp_path / 'shot' / 'shot.usda'
    output.parent.mkdir()
    if existing:
        output.write_text('accepted')
        (output.parent / 'anim_a.usda').write_text('accepted animation')
        (output.parent / 'validation_report.json').write_text('{"success": true}')
    (output.parent / 'user_edits.usda').write_text('authored edits')
    before = signatures(output.parent)
    attempts = []

    def fail(self, plan, target, scene, validate):
        attempts.append(self.crowd_config.buffer_m)
        target.write_text('failed')
        (target.parent / 'anim_a.usda').write_text('failed animation')
        (target.parent / 'user_edits.usda').write_text('candidate edits')
        return False, posed_failure(.1)

    monkeypatch.setattr(ShotCompiler, '_compile_once', fail)
    config = CrowdSolverConfig(max_repair_attempts=2)
    ok, report = ShotCompiler(ROOT, crowd_config=config).compile(ShotPlan(), output)
    assert not ok
    assert len(attempts) == 3
    assert report['crowd_repair']['stopped'] == 'attempt_limit'
    assert 'artifacts' not in report
    assert signatures(output.parent) == before


def test_transaction_rejects_concurrent_edit_and_rolls_back_promotion(tmp_path, monkeypatch):
    import compile_transaction

    output = tmp_path / 'shot' / 'shot.usda'
    output.parent.mkdir()
    output.write_text('accepted')
    with CompileTransaction(output) as transaction:
        (transaction.stage / output.name).write_text('candidate')
        output.write_text('new user edit')
        with pytest.raises(RuntimeError, match='changed during compilation'):
            transaction.publish()
    assert output.read_text() == 'new user edit'
    original_replace = compile_transaction.os.replace

    def interrupted(source, destination):
        if Path(source).name == output.name and 'promotion-backup' not in str(source):
            raise OSError('disk failure')
        return original_replace(source, destination)

    with CompileTransaction(output) as transaction:
        (transaction.stage / 'anim_a.usda').write_text('candidate animation')
        (transaction.stage / output.name).write_text('candidate')
        monkeypatch.setattr(compile_transaction.os, 'replace', interrupted)
        with pytest.raises(OSError, match='disk failure'):
            transaction.publish()
    assert output.read_text() == 'new user edit'
    assert not (output.parent / 'anim_a.usda').exists()


def test_missing_validator_and_stale_report_fail_closed(tmp_path, monkeypatch):
    compiler = ShotCompiler(tmp_path)
    output = tmp_path / 'shot.usda'
    report = tmp_path / 'collision_report.json'
    ok, result = compiler._run_collision_validator(output, tmp_path / 'allow.json')
    assert not ok and 'missing' in result['error']
    validator = tmp_path / 'tools' / 'collision' / 'validate_collisions.py'
    validator.parent.mkdir(parents=True)
    validator.write_text('')
    report.write_text('{"ok": true}')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr('compiler.subprocess.run', run)
    ok, _ = compiler._run_collision_validator(output, tmp_path / 'allow.json')
    assert not ok
    assert len(commands) == 1 and '--repair' not in commands[0]
    assert not report.exists()


def test_authored_roots_are_checked_without_generated_routes():
    plan = ShotPlan(beats=[Beat('a_idle', 'wait', 'a'), Beat('b_idle', 'wait', 'b')])
    states = {a: {'position': [0, 0, 0]} for a in ('a', 'b')}
    timeline, error = TimelineResolver().resolve(plan, states)
    assert error is None
    performances = {a: ActorPerformance(a, keyframes=[Keyframe(0, state['position'], 0)])
                    for a, state in states.items()}
    assert check_conformance(plan, timeline, performances, ActorEnvelopes({'a': .3, 'b': .3}, {}), 24)


@pytest.mark.parametrize('function', ['attack', 'hit_reaction', 'defend_or_block', 'hug_or_kiss'])
@pytest.mark.parametrize('target_synthesized', [False, True])
def test_contact_exemptions_name_one_counterpart_and_exact_window(function, target_synthesized):
    from validate_collisions import _allowed

    actors = {a: {'position': [x, 0, 0], 'asset_id': 'character.human'}
              for a, x in [('a', 0), ('b', 1), ('bystander', 3)]}
    plan = ShotPlan(beats=[Beat('contact', function, 'a', {'target': 'b'},
                               TimingCondition('at', 1), TimingCondition('for_duration', 1))])
    timeline, error = TimelineResolver().resolve(plan, actors)
    assert error is None
    other = ({'b': ActorPerformance('b', asset_id='character.human',
                                   keyframes=[Keyframe(0, actors['b']['position'], 0)])}
             if target_synthesized else {})
    perf, error = MotionSynthesizer({'clips': {}}).synthesize_actor(
        'a', actors['a'], timeline.actor_schedules['a'], {}, other,
        timeline.total_duration_s, all_initial_states=actors)
    assert error is None
    assert perf.allowed_contacts
    intended = SimpleNamespace(name='b', asset_id='character.human')
    unrelated = SimpleNamespace(name='bystander', asset_id='character.human')
    for target, when, expected in [(intended, 1.5, True), (unrelated, 1.5, False),
                                   (intended, .99, False), (intended, 2.01, False)]:
        assert _allowed(perf.allowed_contacts, 'a', target, 'body_capsule', None,
                        'Spine', when, .1) is expected


@pytest.mark.parametrize('function', ['react', 'defend_or_block', 'collapse_or_knockout'])
def test_untargeted_actions_do_not_disable_collision_checks(function):
    states = {'a': {'position': [0, 0, 0]}}
    timeline, error = TimelineResolver().resolve(ShotPlan(beats=[Beat('action', function, 'a')]), states)
    assert error is None
    perf, error = MotionSynthesizer({'clips': {}}).synthesize_actor(
        'a', states['a'], timeline.actor_schedules['a'], {}, {},
        timeline.total_duration_s, all_initial_states=states)
    assert error is None
    assert not perf.allowed_contacts


def test_final_collision_feedback_rebuilds_and_revalidates_real_animation(tmp_path, monkeypatch):
    """Inject a validator failure, then require a real clean full-pipeline retry."""
    from pxr import Gf, Usd, UsdGeom

    scene = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene))
    world = UsdGeom.Xform.Define(stage, '/World')
    stage.SetDefaultPrim(world.GetPrim())
    floor = UsdGeom.Cube.Define(stage, '/World/Floor')
    floor.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    floor.AddScaleOp().Set(Gf.Vec3f(10, .1, 10))
    stage.GetRootLayer().Save()
    output = tmp_path / 'shot' / 'shot.usda'
    initial = output.read_bytes() if output.exists() else None
    plan = ShotPlan(
        setup=[SetupAction('place', actor, 'character.human', position=[x, 0, 0])
               for actor, x in [('a', -3), ('b', 3)]],
        beats=[Beat('move_' + actor, 'walk_to', actor, {'target': [0, 0, 0]},
                    TimingCondition('at', 0), TimingCondition('for_duration', 3))
               for actor in ('a', 'b')])
    original_validator = ShotCompiler._run_collision_validator
    buffers = []

    def validator(self, candidate, allow):
        assert candidate != output
        assert (output.read_bytes() if output.exists() else None) == initial
        assert all((candidate.parent / ('anim_' + actor + '.usda')).exists() for actor in ('a', 'b'))
        buffers.append(self.crowd_config.buffer_m)
        if len(buffers) == 1:
            return False, posed_failure(.15)['validation']['collision_report']
        return original_validator(self, candidate, allow)

    monkeypatch.setattr(ShotCompiler, '_run_collision_validator', validator)
    ok, report = ShotCompiler(ROOT).compile(plan, output, scene_usda_path=scene)
    assert ok, report
    assert len(buffers) == 2 and buffers[1] > buffers[0]
    assert report['validation']['collision_report']['ok']
    assert report['crowd_repair']['retries'] == 1
    assert output.exists()
    # Validate again at the published location, proving relative references survive promotion.
    clean, collision = original_validator(ShotCompiler(ROOT), output, output.parent / 'allow_collisions.json')
    assert clean, collision
