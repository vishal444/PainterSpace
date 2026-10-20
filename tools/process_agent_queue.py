#!/usr/bin/env python3
"""
Agent Queue Processor for PainterSpace.
Inspects, prepares, and manages tasks queued by PainterSpace for the IDE LLM Agent.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = REPO_ROOT / "task_queue"
PENDING_FILE = QUEUE_DIR / "pending_request.json"
LLM_RESPONSE_FILE = QUEUE_DIR / "llm_response.json"
VALIDATION_ERROR_FILE = QUEUE_DIR / "validation_error.json"

SEMANTIC_GRAMMAR_DOC = {
    "rule": "NEVER pass raw [x, y, z] coordinates in 'pos' or 'target'. Always pass semantic reference dicts resolved by the C++/Python engine.",
    "examples": [
        {"at": "ResidentialAvenue"},
        {"near": "Hero", "side": "right", "dist": 1.8},
        {"near": "Car", "side": "left", "dist": 1.4},
        {"near": "Building", "side": "front", "dist": 3.0},
        {"inside": "Car", "seat": "driver"},
        {"between": ["Hero", "Friend"], "bias": 0.5},
        {"facing": "Hero"},
    ],
}


def get_pending_task():
    if not PENDING_FILE.exists():
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Error reading pending task: {exc}", file=sys.stderr)
        return None


def prepare_pending_task():
    """Enrich task_queue/pending_request.json with the FunctionCatalog tool schemas and strict LLM endpoint instructions."""
    task = get_pending_task()
    if not task:
        return 1

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))
    try:
        from compiler import ShotCompiler
        from function_catalog import FunctionCatalog
        from llm_planner import LLMShotPlanner

        compiler = ShotCompiler(project_root=REPO_ROOT)
        scene_rel = task.get("scene_stage", "")
        scene_usda = (REPO_ROOT / scene_rel).resolve() if scene_rel else None
        scene_data = compiler._read_scene_context(scene_usda) if (scene_usda and scene_usda.exists()) else {}
        catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
        planner = LLMShotPlanner(catalog)
        schemas = planner.export_tool_schemas()
    except Exception as exc:
        print(f"Warning: could not export function catalog schemas: {exc}", file=sys.stderr)
        schemas = []

    task["llm_endpoint_mode"] = True
    task["response_file"] = "task_queue/llm_response.json"
    task["strict_llm_protocol"] = {
        "role": "You are acting STRICTLY as the LLM API endpoint for PainterSpace.",
        "must_do": [
            "1. Read this file (task_queue/pending_request.json).",
            "2. Plan the shot by selecting function calls from function_catalog_schemas using semantic references (zero hardcoded coordinates).",
            "3. Write ONLY the JSON response to task_queue/llm_response.json with schema: {\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}.",
            "4. Output that JSON block in chat and END YOUR RESPONSE IMMEDIATELY.",
        ],
        "must_never_do": [
            "DO NOT run compile_shot.py, bake_animation.py, validate_collisions.py, or ANY Python/shell commands.",
            "DO NOT write or edit .usda files directly.",
            "DO NOT perform the program's execution or compilation work yourself — the PainterSpace C++ desktop app watches task_queue/llm_response.json and automatically executes the function calls, compiles shot.usda, and validates collisions itself.",
        ],
    }
    task["semantic_reference_grammar"] = SEMANTIC_GRAMMAR_DOC
    task["function_catalog_schemas"] = schemas

    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2)
    return 0


def show_status():
    task = get_pending_task()
    if not task:
        print("No pending task in task_queue/pending_request.json")
        return 0

    print("==================================================")
    print("PAINTERSPACE PENDING TASK (LLM ENDPOINT QUEUE)")
    print("==================================================")
    print(f"Task ID:       {task.get('task_id')}")
    print(f"Status:        {task.get('status')}")
    print(f"Type:          {task.get('stage_type', 'shot')}")
    print(f"Target Stage:  {task.get('target_stage')}")
    print(f"Response File: {task.get('response_file', 'task_queue/llm_response.json')}")
    print(f"Prompt:        {task.get('prompt')}")
    if task.get("scene_prompt"):
        print(f"Scene Prompt:  {task.get('scene_prompt')}")
    if task.get("validation_error"):
        print(f"Validation Err:{json.dumps(task.get('validation_error'))}")
    print("--------------------------------------------------")
    print("STRICT RULE: Write ONLY JSON tool calls to task_queue/llm_response.json and stop.")
    print("==================================================")
    return 0


def mark_completed(output_stage_path=None):
    task = get_pending_task()
    if not task:
        print("No pending task to complete.")
        return 0

    task["status"] = "completed"
    if output_stage_path:
        task["completed_stage"] = str(output_stage_path)

    completed_file = QUEUE_DIR / f"completed_{task.get('task_id', 'task')}.json"
    with open(completed_file, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2)

    # Clean up pending and transient response files
    for p in (PENDING_FILE, QUEUE_DIR / "pending_request.md", LLM_RESPONSE_FILE, VALIDATION_ERROR_FILE):
        if p.exists():
            p.unlink()

    print(f"Task {task.get('task_id')} marked completed and archived to {completed_file.name}")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("status", "show", "inspect"):
        return show_status()
    elif sys.argv[1] == "prepare":
        return prepare_pending_task()
    elif sys.argv[1] in ("complete", "done"):
        out_path = sys.argv[2] if len(sys.argv) > 2 else None
        return mark_completed(out_path)
    else:
        print("Usage: process_agent_queue.py [status|prepare|complete <stage_path>]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
