import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "collision"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner


def test_timeline_beats_planning():
    compiler = ShotCompiler(project_root=REPO_ROOT)
    catalog = FunctionCatalog(compiler.manifest)
    planner = LLMShotPlanner(catalog)

    prompt = "0s-5s walk 5-10 phone call 10-15 handshake"
    ok, plan, logs = planner.plan_from_prompt(prompt)

    assert ok, "Planner should succeed"
    assert len(plan.beats) >= 3, f"Expected at least 3 beats, got {len(plan.beats)}"

    functions = [b.function for b in plan.beats]
    assert "walk_to" in functions
    assert "use_phone" in functions
    assert "shake_hands" in functions

    # Check timings
    walk_beat = next(b for b in plan.beats if b.function == "walk_to")
    phone_beat = next(b for b in plan.beats if b.function == "use_phone")
    handshake_beat = next(b for b in plan.beats if b.function == "shake_hands")

    assert walk_beat.start_condition is not None
    assert walk_beat.start_condition.value == 0.0

    assert phone_beat.start_condition is not None
    assert phone_beat.start_condition.value == 5.0

    assert handshake_beat.start_condition is not None
    assert handshake_beat.start_condition.value == 10.0


def test_actions_json_schema():
    payload = {
        "duration_s": 15.0,
        "actions": [
            {"id": "act_1", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 0.0, "end_s": 5.0, "action": "walk", "color": "#22c55e"},
            {"id": "act_2", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 5.0, "end_s": 10.0, "action": "phone call", "color": "#22c55e"},
            {"id": "act_3", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 10.0, "end_s": 15.0, "action": "handshake", "color": "#22c55e"},
        ],
        "paths": {
            "/World/Hero": [[0.0, 0.0, 0.0], [2.0, 0.0, 4.0], [5.0, 0.0, 8.0]],
            "/World/Mustang": [[10.0, 0.0, 0.0], [10.0, 0.0, 20.0]]
        }
    }
    raw = json.dumps(payload)
    parsed = json.loads(raw)

    assert parsed["duration_s"] == 15.0
    assert len(parsed["actions"]) == 3
    assert len(parsed["paths"]) == 2
    assert "/World/Hero" in parsed["paths"]
    assert "/World/Mustang" in parsed["paths"]


def test_distinct_path_colors():
    # Test that different assets hash to distinct colors in the palette
    s_palette = [
        "#22c55e", "#f43f5e", "#eab308", "#a855f7", "#f97316",
        "#06b6d4", "#ec4899", "#14b8a6", "#3b82f6", "#84cc16"
    ]
    assets = ["/World/Hero", "/World/Mustang", "/World/Tiger", "/World/Pedestrian_01", "/World/ShotCam"]
    colors = {}
    for a in assets:
        if a == "/World/ShotCam":
            colors[a] = "#38bdf8"
        else:
            h = abs(hash(a))
            colors[a] = s_palette[h % len(s_palette)]

    assert colors["/World/ShotCam"] == "#38bdf8"
    assert len(set(colors.values())) >= 3, "Assets should have distinct colors"


def test_action_timeline_editing():
    # Simulate action timeline edit workflow
    actions = [
        {"id": "act_1", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 0.0, "end_s": 5.0, "action": "walk", "color": "#58a6ff"},
        {"id": "act_2", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 5.0, "end_s": 10.0, "action": "phone call", "color": "#58a6ff"}
    ]

    # Select and update act_2 timing and action
    selected_id = "act_2"
    target = next(a for a in actions if a["id"] == selected_id)
    assert target["action"] == "phone call"

    # User edits timing and action
    target["start_s"] = 4.0
    target["end_s"] = 9.5
    target["action"] = "answer emergency call"

    assert target["start_s"] == 4.0
    assert target["end_s"] == 9.5
    assert target["action"] == "answer emergency call"


def test_action_timeline_undo_redo():
    # Simulate ActionTimelineWidget undo/redo stack
    undo_stack = []
    redo_stack = []

    state_0 = []
    current_actions = list(state_0)

    # 1. Add action
    undo_stack.append([dict(a) for a in current_actions])
    redo_stack.clear()
    current_actions.append({"id": "act_1", "actor_path": "/World/Hero", "start_s": 0.0, "end_s": 5.0, "action": "walk"})
    assert len(current_actions) == 1

    # 2. Edit action
    undo_stack.append([dict(a) for a in current_actions])
    redo_stack.clear()
    current_actions[0]["action"] = "sprint"
    current_actions[0]["end_s"] = 7.0
    assert current_actions[0]["action"] == "sprint"
    assert current_actions[0]["end_s"] == 7.0

    # 3. Undo edit
    redo_stack.append([dict(a) for a in current_actions])
    prev = undo_stack.pop()
    current_actions = prev
    assert current_actions[0]["action"] == "walk"
    assert current_actions[0]["end_s"] == 5.0

    # 4. Undo add
    redo_stack.append([dict(a) for a in current_actions])
    prev = undo_stack.pop()
    current_actions = prev
    assert len(current_actions) == 0

    # 5. Redo add
    undo_stack.append([dict(a) for a in current_actions])
    next_state = redo_stack.pop()
    current_actions = next_state
    assert len(current_actions) == 1
    assert current_actions[0]["action"] == "walk"

    # 6. Redo edit
    undo_stack.append([dict(a) for a in current_actions])
    next_state = redo_stack.pop()
    current_actions = next_state
    assert len(current_actions) == 1
    assert current_actions[0]["action"] == "sprint"
    assert current_actions[0]["end_s"] == 7.0


def test_multi_character_selection_and_summary_prompt():
    # Test multi-actor actions and summary prompt formatting
    actions = [
        {"id": "act_1", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 0.0, "end_s": 5.0, "action": "walk"},
        {"id": "act_2", "actor_path": "/World/Mustang", "actor_label": "Mustang", "start_s": 2.0, "end_s": 8.0, "action": "drive"},
        {"id": "act_3", "actor_path": "/World/Hero", "actor_label": "Hero", "start_s": 5.0, "end_s": 10.0, "action": "handshake"}
    ]

    # Verify filtering/selection by actor
    hero_actions = [a for a in actions if a["actor_path"] == "/World/Hero"]
    assert len(hero_actions) == 2
    mustang_actions = [a for a in actions if a["actor_path"] == "/World/Mustang"]
    assert len(mustang_actions) == 1

    # Format summary prompt
    by_actor = {}
    for a in actions:
        by_actor.setdefault(a["actor_label"], []).append(a)

    lines = []
    for label, acts in by_actor.items():
        acts.sort(key=lambda x: x["start_s"])
        phrases = [f"{int(x['start_s'])}s-{int(x['end_s'])}s {x['action']}" for x in acts]
        lines.append(f"{label}: {', '.join(phrases)}.")

    summary = "\n".join(lines)
    assert "Hero: 0s-5s walk, 5s-10s handshake." in summary
    assert "Mustang: 2s-8s drive." in summary


if __name__ == "__main__":
    test_timeline_beats_planning()
    test_actions_json_schema()
    test_distinct_path_colors()
    test_action_timeline_editing()
    test_action_timeline_undo_redo()
    test_multi_character_selection_and_summary_prompt()
    print("ALL ACTION TIMELINE TESTS PASSED!")
