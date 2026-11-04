"""Dynamic targets are synthesized before actors that travel to them."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from compiler import ShotCompiler
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from semantic_references import ReferenceResolver
from timeline_resolver import ResolvedBeat


def beat(actor, function, target=None):
    params = {"target": target} if target else {}
    return ResolvedBeat(f"{actor}_{function}", actor, function, 0, 2, 2, params)


def test_moving_target_is_ready_before_pursuer():
    schedules = {
        "pursuer": [beat("pursuer", "use_weapon", "target"),
                    beat("pursuer", "run_to", "target")],
        "target": [beat("target", "exit_vehicle"),
                   beat("target", "collapse_or_knockout")],
    }
    states = {actor: {"label": actor} for actor in schedules}
    assert ShotCompiler._actor_synthesis_order(
        ["pursuer", "target"], schedules, states) == ["target", "pursuer"]


def test_stationary_target_does_not_reorder_unrelated_actors():
    schedules = {
        "first": [beat("first", "use_weapon", "second")],
        "second": [beat("second", "use_weapon", "first")],
        "third": [beat("third", "dance")],
    }
    states = {actor: {} for actor in schedules}
    assert ShotCompiler._actor_synthesis_order(
        list(schedules), schedules, states) == list(schedules)


def test_mutual_pursuit_reports_dependency_instead_of_using_stale_positions():
    schedules = {
        "first": [beat("first", "run_to", "second")],
        "second": [beat("second", "run_to", "first")],
    }
    states = {actor: {} for actor in schedules}
    try:
        ShotCompiler._actor_synthesis_order(list(schedules), schedules, states)
    except ValueError as error:
        assert "Mutual movement targets" in str(error)
    else:
        raise AssertionError("A cyclic movement dependency must be reported")


def test_run_targets_actor_position_at_arrival():
    manifest = {"assets": [{"id": "person", "tags": ["character"]}],
                "clips": {"run": {}}}
    target = ActorPerformance("target", asset_id="person")
    target.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(2, [6, 0, 0], 0)]
    source = {"asset_id": "person", "position": [-5, 0, 0], "facing_deg": 90}
    states = {"source": source, "target": {"asset_id": "person", "position": [0, 0, 0]}}
    action = beat("source", "run_to", "target")
    resolver = ReferenceResolver(manifest)
    performance, error = MotionSynthesizer(manifest, ref_resolver=resolver).synthesize_actor(
        "source", source, [action], {}, {"target": target}, 2,
        ref_resolver=resolver, all_initial_states=states)
    assert error is None
    assert performance.sample(2)[0][0] > 4
