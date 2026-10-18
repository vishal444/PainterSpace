#!/usr/bin/env python3
"""
Agent Queue Processor for PainterSpace.
Inspects and manages tasks queued by PainterSpace for the Gemini Coding Agent.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = REPO_ROOT / "task_queue"
PENDING_FILE = QUEUE_DIR / "pending_request.json"


def get_pending_task():
    if not PENDING_FILE.exists():
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Error reading pending task: {exc}", file=sys.stderr)
        return None


def show_status():
    task = get_pending_task()
    if not task:
        print("No pending task in task_queue/pending_request.json")
        return 0

    print("==================================================")
    print("PAINTERSPACE PENDING TASK FOR GEMINI AGENT")
    print("==================================================")
    print(f"Task ID:      {task.get('task_id')}")
    print(f"Status:       {task.get('status')}")
    print(f"Type:         {task.get('stage_type', 'shot')}")
    print(f"Target Stage: {task.get('target_stage')}")
    print(f"Prompt:       {task.get('prompt')}")
    if task.get("scene_prompt"):
        print(f"Scene Prompt: {task.get('scene_prompt')}")
    print("--------------------------------------------------")
    print("Full context available in task_queue/pending_request.json")
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

    # Clean up pending
    if PENDING_FILE.exists():
        PENDING_FILE.unlink()

    print(f"Task {task.get('task_id')} marked completed and archived to {completed_file.name}")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("status", "show", "inspect"):
        return show_status()
    elif sys.argv[1] in ("complete", "done"):
        out_path = sys.argv[2] if len(sys.argv) > 2 else None
        return mark_completed(out_path)
    else:
        print("Usage: process_agent_queue.py [status|complete <stage_path>]")
        return 1


if __name__ == "__main__":
    sys.exit(main())

