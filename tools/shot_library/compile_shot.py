"""CLI tool for compiling shots using the PainterSpace Shot Function Library.

Usage:
  python tools/shot_library/compile_shot.py --prompt "..." --out scenes/scene_01/shot_005/shot.usda
  python tools/shot_library/compile_shot.py --plan shot_plan.json --out scenes/scene_01/shot_005/shot.usda
  python tools/shot_library/compile_shot.py --execute-llm-response task_queue/llm_response.json
  python tools/shot_library/compile_shot.py --process-queue
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "collision"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))
venv_site = REPO_ROOT / ".venv" / "lib" / "python3.12" / "site-packages"
if venv_site.exists() and str(venv_site) not in sys.path:
    sys.path.insert(0, str(venv_site))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from plan_model import ShotPlan


def _finalize_and_thumbnail(out_path: Path) -> None:
    try:
        from world_state import finalize_shot
        finalize_shot(out_path, images=True, max_objects=12)
        print("  [WorldState] Generated state_end.json and topdown map")
    except (Exception, SystemExit) as exc:
        print(f"  [WorldState] finalize_shot warning: {exc}")

    thumb_path = out_path.parent / "thumbnail.jpg"
    usdrecord_bin = shutil.which("usdrecord") or "/usr/bin/usdrecord"
    if Path(usdrecord_bin).exists():
        try:
            subprocess.run(
                [usdrecord_bin, str(out_path), str(thumb_path), "--frames", "0", "--cam", "/World/ShotCam"],
                capture_output=True,
                timeout=60,
            )
            if thumb_path.exists():
                print(f"  [Render] Generated thumbnail {thumb_path}")
        except Exception as exc:
            print(f"  [Render] Thumbnail warning: {exc}")


def _write_validation_feedback(queue_file: Path, response_file: Path, error_payload: dict) -> None:
    val_err_file = REPO_ROOT / "task_queue" / "validation_error.json"
    val_err_file.parent.mkdir(parents=True, exist_ok=True)
    val_err_file.write_text(json.dumps(error_payload, indent=2), encoding="utf-8")

    if queue_file.exists():
        try:
            task = json.loads(queue_file.read_text(encoding="utf-8"))
            task["status"] = "needs_revision"
            task["validation_error"] = error_payload
            queue_file.write_text(json.dumps(task, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Warning: could not update pending_request.json: {exc}", file=sys.stderr)

    if response_file.exists():
        try:
            response_file.unlink()
        except Exception:
            pass


def execute_llm_response(response_path: Path, out_override: Path | None = None) -> int:
    """Execute structured LLM JSON function calls from task_queue/llm_response.json."""
    queue_file = REPO_ROOT / "task_queue" / "pending_request.json"
    response_file = response_path.resolve()
    if not response_file.exists():
        print(f"No LLM response file at {response_file}", file=sys.stderr)
        return 1

    try:
        llm_data = json.loads(response_file.read_text(encoding="utf-8"))
    except Exception as exc:
        err = {
            "stage": "json_parse",
            "message": f"Invalid JSON in {response_file.name}: {exc}",
            "instruction": "Rewrite task_queue/llm_response.json with valid JSON matching {\"calls\": [{\"function\": \"...\", \"args\": {...}}]}.",
        }
        _write_validation_feedback(queue_file, response_file, err)
        print(err["message"], file=sys.stderr)
        return 2

    task = {}
    if queue_file.exists():
        try:
            task = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            task = {}

    prompt = task.get("prompt", llm_data.get("prompt", ""))
    target_stage = str(out_override) if out_override else task.get("target_stage", llm_data.get("target_stage", ""))
    if not target_stage:
        print("Error: target_stage not found in pending_request.json or --out", file=sys.stderr)
        return 1

    out_path = (REPO_ROOT / target_stage).resolve() if not Path(target_stage).is_absolute() else Path(target_stage).resolve()
    is_scene_stage = out_path.name == "scene.usda"
    scene_usda = out_path if is_scene_stage else (out_path.parent.parent / "scene.usda")

    print(f"Executing LLM JSON response from {response_file.name} -> {out_path}")
    compiler = ShotCompiler(project_root=REPO_ROOT)
    scene_data = compiler._read_scene_context(scene_usda) if (not is_scene_stage and scene_usda.exists()) else {}
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
    planner = LLMShotPlanner(catalog)

    ok, plan, logs = planner.plan_from_llm_json(llm_data, prompt=prompt)
    for log in logs:
        print(f"  [FunctionCatalog] {log}")

    if not ok:
        err = {
            "stage": "function_catalog_execution",
            "message": "One or more LLM function calls failed execution.",
            "logs": logs,
            "instruction": "Fix the failing function call arguments in task_queue/llm_response.json and write ONLY the updated JSON.",
        }
        _write_validation_feedback(queue_file, response_file, err)
        print(f"LLM function call execution failed: {logs}", file=sys.stderr)
        return 2

    success, report = compiler.compile(
        plan,
        out_path,
        scene_usda_path=scene_usda if (not is_scene_stage and scene_usda.exists()) else None,
        validate=not is_scene_stage,
    )
    if not success:
        err = {
            "stage": "shot_compiler_validation",
            "message": report.get("error", "Compilation or collision validation failed."),
            "validation": report.get("validation", {}),
            "logs": logs,
            "instruction": "Adjust semantic offsets/distances or action timing in task_queue/llm_response.json to resolve the validation error, and write ONLY the updated JSON.",
        }
        _write_validation_feedback(queue_file, response_file, err)
        print(f"Compilation or validation failed: {json.dumps(err, indent=2)}", file=sys.stderr)
        return 2

    print(f"Successfully executed LLM function calls and compiled {out_path}!")
    if not is_scene_stage:
        _finalize_and_thumbnail(out_path)

    from process_agent_queue import mark_completed
    mark_completed(str(out_path))
    if response_file.exists():
        try:
            response_file.unlink()
        except Exception:
            pass
    return 0


def process_queue() -> int:
    response_file = REPO_ROOT / "task_queue" / "llm_response.json"
    if response_file.exists():
        return execute_llm_response(response_file)

    queue_file = REPO_ROOT / "task_queue" / "pending_request.json"
    if not queue_file.exists():
        print("No pending request in task_queue/pending_request.json")
        return 0

    task = json.loads(queue_file.read_text(encoding="utf-8"))
    prompt = task.get("prompt", "")
    target_stage = task.get("target_stage")
    if not target_stage:
        print("Task missing target_stage", file=sys.stderr)
        return 1

    out_path = (REPO_ROOT / target_stage).resolve()
    if out_path.name == "scene.usda":
        out_path = out_path.parent / "shot_001" / "shot.usda"
    print(f"Processing task {task.get('task_id')}...")
    print(f"Prompt: {prompt}")
    print(f"Target: {out_path}")

    scene_usda = out_path.parent.parent / "scene.usda"
    compiler = ShotCompiler(project_root=REPO_ROOT)
    scene_data = compiler._read_scene_context(scene_usda) if scene_usda.exists() else {}
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
    planner = LLMShotPlanner(catalog)

    ok, plan, logs = planner.plan_from_prompt(prompt)
    for log in logs:
        print(f"  [Planner] {log}")

    success, report = compiler.compile(plan, out_path, scene_usda_path=scene_usda if scene_usda.exists() else None, validate=True)
    if not success:
        print(f"Compilation or validation failed: {report}", file=sys.stderr)
        return 1

    print(f"Successfully compiled shot to {out_path}!")
    print(f"Artifacts generated:")
    for k, v in report.get("artifacts", {}).items():
        print(f"  - {k}: {v}")

    _finalize_and_thumbnail(out_path)

    # Mark completed in agent queue
    from process_agent_queue import mark_completed
    mark_completed(str(out_path))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Natural language storyboard prompt")
    parser.add_argument("--plan", type=Path, help="Path to shot_plan.json")
    parser.add_argument("--execute-llm-response", type=Path, dest="llm_response", help="Path to task_queue/llm_response.json containing LLM tool calls")
    parser.add_argument("--out", "--output", dest="out", type=Path, help="Target shot.usda path")
    parser.add_argument("--scene", type=Path, default=None, help="Optional explicit path to scene.usda")
    parser.add_argument("--process-queue", action="store_true", help="Process pending request from task_queue")

    args = parser.parse_args()

    if args.llm_response:
        return execute_llm_response(args.llm_response, out_override=args.out)

    if args.process_queue:
        return process_queue()

    if not args.out:
        print("Error: --out is required unless --process-queue or --execute-llm-response is used", file=sys.stderr)
        return 1

    out_path = args.out.resolve()
    scene_usda = args.scene.resolve() if args.scene else (out_path.parent.parent / "scene.usda")
    compiler = ShotCompiler(project_root=REPO_ROOT)
    scene_data = compiler._read_scene_context(scene_usda) if scene_usda.exists() else {}

    if args.plan:
        plan = ShotPlan.from_json(args.plan.read_text(encoding="utf-8"))
        success, report = compiler.compile(plan, out_path, scene_usda_path=scene_usda if scene_usda.exists() else None, validate=True)
    elif args.prompt:
        catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
        planner = LLMShotPlanner(catalog)
        success, report, logs = planner.plan_and_compile_with_retry(
            args.prompt,
            compiler,
            out_path,
            scene_usda_path=scene_usda if scene_usda.exists() else None,
            validate=True,
        )
        for log in logs:
            print(f"[Planner] {log}")
    else:
        print("Error: either --prompt, --plan, --execute-llm-response, or --process-queue must be specified", file=sys.stderr)
        return 1

    if not success:
        if out_path.exists():
            # Attempt automatic collision path repair if stage was authored
            val_script = REPO_ROOT / "tools" / "collision" / "validate_collisions.py"
            allow_path = out_path.parent / "allow_collisions.json"
            if val_script.exists():
                cmd = [
                    sys.executable,
                    str(val_script),
                    str(out_path),
                    "--manifest", str(compiler.manifest_path),
                    "--repair",
                ]
                if allow_path.exists():
                    cmd.extend(["--allow", str(allow_path)])
                subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
            print(f"Compiled {args.out} (with post-compile repair)")
            return 0
        print(f"Compilation failed: {report}", file=sys.stderr)
        return 1

    print(f"Compiled and validated {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
