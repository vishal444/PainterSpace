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


def write_task_json(path, task):
    """Keep queue payload compact while preserving every JSON value."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def _anchor_support_references(manifest, scene_data):
    """Classify scene-object anchors against measured standing faces."""
    from semantic_references import ReferenceResolver
    from support_surfaces import ACTOR_SUPPORT_MARGIN_M, contains, supports_disc

    assets = {asset.get("id"): asset for asset in manifest.get("assets", [])}
    resolver = ReferenceResolver(manifest, scene_data)
    supported, unsupported, unverified = [], [], []
    surfaces = scene_data.get("support_surfaces", [])
    incomplete_geometry = bool(scene_data.get("unprotected_scene_objects"))
    for obj in scene_data.get("objects", []):
        name = obj.get("name") or obj.get("id")
        asset = assets.get(obj.get("asset") or obj.get("asset_id"), {})
        for anchor in asset.get("anchors", {}):
            reference = f"{name}:{anchor}"
            target, error = resolver.resolve(reference, {})
            if error or target is None:
                continue
            on_support = (any(contains(surface, target.position, ACTOR_SUPPORT_MARGIN_M)
                              for surface in surfaces)
                          or supports_disc(surfaces, target.position))
            bucket = unverified if on_support and incomplete_geometry else (supported if on_support else unsupported)
            bucket.append(reference)
    for spot in (scene_data.get("standing_spots", [])
                 + scene_data.get("surface_spots", [])
                 + scene_data.get("rooftop_spots", [])):
        reference = f'{spot["owner"]}:{spot["name"]}'
        if (any(contains(surface, spot["position"], ACTOR_SUPPORT_MARGIN_M)
                for surface in surfaces)
                or supports_disc(surfaces, spot["position"])):
            (unverified if incomplete_geometry else supported).append(reference)
    return {"supported_anchors": supported, "unsupported_anchors": unsupported,
            "unverified_anchors": unverified}


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
        shot_dir = (REPO_ROOT / task.get("target_stage", "")).resolve().parent
        scene_data = compiler._read_scene_context(scene_usda, shot_dir=shot_dir) if (scene_usda and scene_usda.exists()) else {}
        if scene_data.get("read_error"):
            raise RuntimeError(f"Scene spatial context unavailable: {scene_data['read_error']}")
        assets_by_id = {asset.get("id"): asset for asset in compiler.manifest.get("assets", [])}
        uncovered = set(scene_data.get("unprotected_scene_objects", []))
        task["scene_context"] = {
            "objects": [
                {
                    "name": obj.get("name") or obj.get("id"),
                    "asset_id": obj.get("asset") or obj.get("asset_id"),
                    "position": obj.get("position"),
                    "heading_deg": obj.get("heading_deg"),
                    "bounds_m": assets_by_id.get(obj.get("asset") or obj.get("asset_id"), {}).get("bounds_m"),
                    "pivot": assets_by_id.get(obj.get("asset") or obj.get("asset_id"), {}).get("pivot"),
                    "obstacle_bounds_xz": obj.get("obstacle_bounds_xz"),
                    "obstacle_box_count": obj.get("obstacle_box_count", 0),
                    "collision_covered": (obj.get("name") or obj.get("id")) not in uncovered,
                    "stairs": {
                        stair_id: {
                            "width_m": stair.get("width_m"),
                            "rise_m": abs(stair["centerline"][-1][1] - stair["centerline"][0][1]),
                            "tread_count": len(stair.get("treads", [])),
                        }
                        for stair_id, stair in obj.get("stairs", {}).items()
                        if len(stair.get("centerline", [])) >= 2
                    },
                }
                for obj in scene_data.get("objects", [])
            ],
            "authored_zones": [
                {key: zone.get(key) for key in ("id", "min", "max", "footprint_xz",
                                               "heading_deg", "reference_markers")}
                for zone in scene_data.get("zones", [])
            ],
            "standing_spots": [
                {"reference": f'{spot["owner"]}:{spot["name"]}',
                 "position": spot.get("position"),
                 "facing_deg": next((obj.get("heading_deg", 0.0) for obj in scene_data.get("objects", [])
                                     if obj.get("name") == spot["owner"]), 0.0),
                 "clearance_m": spot.get("clearance_m"),
                 "collision_verified": not uncovered}
                for spot in scene_data.get("standing_spots", [])
            ],
            "rooftop_spots": [
                {"reference": f'{spot["owner"]}:{spot["name"]}',
                 "position": spot["position"],
                 "landing_reference": spot.get("landing_reference"),
                 "collision_verified": not uncovered}
                for spot in scene_data.get("rooftop_spots", [])
            ],
            "surface_spots": [
                {"reference": f'{spot["owner"]}:{spot["name"]}',
                 "position": spot["position"],
                 "collision_verified": not uncovered}
                for spot in scene_data.get("surface_spots", [])
            ],
            "unprotected_scene_objects": sorted(uncovered),
        }
        task["scene_context"]["anchor_support"] = _anchor_support_references(
            compiler.manifest, scene_data)
        from spatial_regions import region_descriptors
        task['scene_context']['spatial_regions'] = region_descriptors(scene_data.get('spatial_regions', []))
        task['scene_context']['existing_actors'] = scene_data.get('existing_actors', [])
        world_state = task.get("world_state") or {}
        if world_state:
            task["current_shot_state"] = {
                "objects": [
                    {key: obj.get(key) for key in ("id", "asset", "category", "position", "heading_deg")}
                    for obj in world_state.get("objects", [])
                ],
                "camera": world_state.get("camera"),
            }
        catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
        planner = LLMShotPlanner(catalog)
        from director_brief import load_brief, build_digest, enrich_brief_choices
        planner.brief = load_brief(shot_dir / "director_brief.json")
        planner.camera_mode = "manual" if task.get("camera_locked") or (shot_dir / "camera_rig.json").exists() else "auto"
        task["director_brief_digest"] = build_digest(enrich_brief_choices(planner.brief, compiler.manifest), scene_data)
        # The queue has separate structured scene and brief fields. Keep the
        # behavioral rules, but do not embed those large inventories twice.
        task["planner_rules"] = planner.build_system_prompt(
            include_scene_inventory=False, include_brief_digest=False)
        task["planner_rules"] += (
            "\nScene inventory, standing spots, stairs, and authored zones are in scene_context. "
            "Vehicle anchors and surfaces are in manifest_summary. "
            "Use director_brief_digest for Director constraints.\n"
        )
        task["planner_rules"] += (
            "Use scene_context.surface_spots for placement or walking on measured "
            "flat elevated surfaces. These references are derived from scene geometry; "
            "do not invent surface names or coordinates.\n"
        )
        task["planner_rules"] += (
            "\nFor standing-character placements at scene-object anchors, use only references listed in "
            "scene_context.anchor_support.supported_anchors when measured support faces are present. "
            "Do not use unsupported_anchors for standing placements or paired interactions. "
            "Other reference types are not verified by this anchor list; do not assume they provide standing support. "
            "A different meeting center cannot repair an unsupported starting placement. "
            "Scene-derived standing references (standing_left_* and standing_right_*) are measured "
            "on connected floor; only collision-verified spots have clearance from all scene objects. "
            "Use separate references for crowd members. "
            "If no supported standing reference is available, do not invent ground or move scene assets.\n"
        )
        task["planner_rules"] += (
            "Use scene_context.rooftop_spots for prompts that start on top of a building. "
            "Place the actor at a listed rooftop reference, then use jump_down with "
            "its landing_reference as the target when one is provided. Never infer a roof "
            "position from bounds or invent a rooftop reference.\n"
        )
        task["planner_rules"] += (
            "Use scene_context object dimensions, headings, standing-spot positions, and authored zones "
            "to judge spatial relationships. Use only scene-authored zone markers; generic manifest "
            "lane names do not establish a road in this scene. "
            "When scene_context.unprotected_scene_objects is nonempty, the compiler cannot certify "
            "actor or vehicle placement near those assets; do not claim a safe route through them.\n"
        )
        schemas = planner.export_tool_schemas()
    except Exception as exc:
        print(f"Warning: could not export function catalog schemas: {exc}", file=sys.stderr)
        return 1

    task["llm_endpoint_mode"] = True
    task["response_file"] = "task_queue/llm_response.json"
    task["strict_llm_protocol"] = {
        "role": "You are acting STRICTLY as the LLM API endpoint for PainterSpace.",
        "mandate": "When sending prompt using the task queue, always make sure the LLM responds only with the tools call json, and the execution should end there. make this explicit in the LLM call.",
        "must_do": [
            "1. Read this file (task_queue/pending_request.json).",
            "2. Use the prompt when present, combine it with relevant drawn routes and explicit timeline segments, and use timeline segments alone when the prompt is empty. An empty timeline adds no constraint. Choose only symbolic catalog calls.",
            "3. Write ONLY the tools call JSON response to task_queue/llm_response.json with schema: {\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}.",
            "4. Output that JSON block in chat and END YOUR EXECUTION IMMEDIATELY THERE.",
        ],
        "must_never_do": [
            "DO NOT run the queue processor, planner, compiler, simulator, animation bake, validator, renderer, or ANY Python/shell commands while handling this pending request.",
            "DO NOT write or edit .usda files directly.",
            "DO NOT perform the program's execution or compilation work yourself — the PainterSpace C++ desktop app watches task_queue/llm_response.json and automatically executes the function calls, compiles shot.usda, and validates collisions itself.",
        ],
    }
    task["llm_call"] = {
        "instruction": "Keep the starting scene assets exactly as they are. Use existing scene objects; do not place, replace, remove, or change buildings or other environment assets, even if a prompt mentions a missing feature. Respond ONLY with the tools call JSON matching schema: {\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}. Write this JSON to task_queue/llm_response.json and end execution immediately there.",
        "execution_policy": "LLM responds ONLY with tools call JSON; execution ends there.",
    }
    zone_examples = [f"{zone['id']}.{marker}"
                     for zone in task.get("scene_context", {}).get("authored_zones", [])
                     for marker in zone.get("reference_markers", ["center"])]
    spot_examples = [spot["reference"] for spot in task.get("scene_context", {}).get("standing_spots", [])]
    object_names = [obj["name"] for obj in task.get("scene_context", {}).get("objects", []) if obj.get("name")]
    centered_objects = [obj["name"] for obj in task.get("scene_context", {}).get("objects", [])
                        if obj.get("name") and "center" in str(obj.get("pivot") or "").lower()]
    task["semantic_reference_grammar"] = {
        "rule": "Use exact scene object names, authored zone markers, listed anchors or spots, and world directions. A bare 'center' is invalid unless that exact reference is listed. To target an object's center, use its name only when its pivot identifies the center. Never emit numeric geometry, timing or angles.",
        "scene_object_names": object_names,
        "examples": ([{"at": zone_examples[0]}] if zone_examples else [])
                    + ([{"at": spot_examples[0]}] if spot_examples else [])
                    + ([{"at": centered_objects[0]}] if centered_objects else []),
    }
    # Camera affects the available schema, never the planner context.
    task.pop("camera_locked", None)
    for raw_field in ("existing_scene_usda", "existing_shot_usda", "world_state", "world_state_images"):
        task.pop(raw_field, None)
    if task.get("validation_error"):
        from director_brief import symbolic_feedback
        task["validation_error"] = symbolic_feedback(task["validation_error"])
    task["manifest_summary"] = {
        "defaults": compiler.manifest.get("defaults", {}),
        "assets": [{"id": a.get("id"), "tags": a.get("tags", []),
                    "anchors": list(a.get("anchors", {})), "surfaces": list(a.get("surfaces", {}))}
                   for a in compiler.manifest.get("assets", [])],
        "clips": list(compiler.manifest.get("clips", {})),
    }
    task["function_catalog_schemas"] = schemas

    write_task_json(PENDING_FILE, task)
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
    print("STRICT RULE: The LLM must respond ONLY with the tools call JSON to task_queue/llm_response.json, and execution must end there.")
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


def clear_pending():
    """Remove pending request, response, and validation files from task_queue."""
    cleared = []
    for p in (PENDING_FILE, QUEUE_DIR / "pending_request.md", LLM_RESPONSE_FILE, VALIDATION_ERROR_FILE):
        if p.exists():
            p.unlink()
            cleared.append(p.name)
    if cleared:
        print(f"Cleared pending queue files: {', '.join(cleared)}")
    else:
        print("No pending queue files to clear.")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("status", "show", "inspect"):
        return show_status()
    elif sys.argv[1] == "prepare":
        return prepare_pending_task()
    elif sys.argv[1] in ("complete", "done"):
        out_path = sys.argv[2] if len(sys.argv) > 2 else None
        return mark_completed(out_path)
    elif sys.argv[1] in ("clear", "clean", "remove", "reset"):
        return clear_pending()
    else:
        print("Usage: process_agent_queue.py [status|prepare|complete <stage_path>|clear]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
