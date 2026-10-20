#!/usr/bin/env python3
"""Generate a data-only OpenUSD shot layer (shot.usda) and metadata sidecar (shot.json).

The model is never asked to write or execute Python. The generated shot layer
sublayers ../scene.usda, uses `over "World"` for shot-specific actors/motion/camera,
is checked for unsafe asset paths, validated by usdcat/usdchecker, and saved
alongside a metadata-only shot.json file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which

MAX_STAGE_BYTES = 1_000_000
MAX_PRIMS = 2_000
MAX_FRAMES = 1_440
RETRIES = 3
PROMPT_VERSION = "2.0"

DEFAULT_SCENE_USDA = """#usda 1.0
(
    defaultPrim = "World"
    upAxis = "Y"
    metersPerUnit = 1
)

def Xform "World"
{
    def DomeLight "BaseAmbient"
    {
        color3f inputs:color = (0.35, 0.42, 0.55)
        float inputs:intensity = 0.4
    }
    def DistantLight "BaseKeyLight"
    {
        color3f inputs:color = (0.95, 0.88, 0.78)
        float inputs:intensity = 1.6
        float angle = 0.53
        float3 xformOp:rotateXYZ = (-35, -25, 0)
        uniform token[] xformOpOrder = ["xformOp:rotateXYZ"]
    }
    def Xform "Ground"
    {
        double3 xformOp:translate = (0, -0.04, 0)
        float3 xformOp:scale = (24, 0.08, 24)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
        def Cube "Floor"
        {
            float size = 1
            color3f[] primvars:displayColor = [(0.16, 0.20, 0.24)] (
                interpolation = "constant"
            )
        }
    }
}
"""


def manifest_prompt(path: Path, asset_root: Path) -> str:
    resolved_asset_root = asset_root.resolve()
    project_root = resolved_asset_root.parent
    try:
        from sync_manifest import sync_manifest
        sync_manifest(project_root=project_root, quiet=True)
    except Exception:  # noqa: BLE001
        pass
    if not path.is_file():
        fallback = asset_root / "manifest.json"
        if fallback.is_file():
            path = fallback
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read asset manifest: {exc}") from exc
    allowed = []
    for item in manifest.get("assets", []):
        rel = item.get("path", "")
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts or "://" in rel:
            raise ValueError(f"Unsafe path in manifest: {rel}")
        full = (project_root / candidate).resolve() if candidate.parts and candidate.parts[0] == "assets" else (resolved_asset_root / candidate).resolve()
        if resolved_asset_root not in full.parents and full != resolved_asset_root:
            raise ValueError(f"Asset escapes asset directory: {rel}")
        if candidate.suffix.lower() not in {".usd", ".usda", ".usdc"}:
            continue
        rel_to_assets = full.relative_to(resolved_asset_root).as_posix()
        item_copy = {k: v for k, v in item.items() if k not in ("collision", "normalization")}
        if isinstance(item_copy.get("parts"), dict):
            compact_parts = {}
            for p_name, p_meta in item_copy["parts"].items():
                if isinstance(p_meta, dict):
                    compact_parts[p_name] = {pk: pv for pk, pv in p_meta.items() if pk != "collider_frame"}
            item_copy["parts"] = compact_parts
        item_copy["shot_reference_path"] = f"../../../assets/{rel_to_assets}"
        allowed.append(item_copy)
    return json.dumps({"assets": allowed}, ensure_ascii=False, indent=2)


def extract_text(response: dict) -> str:
    if response.get("output_text"):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
    return "\n".join(chunks)


WORLD_STATE_INSTRUCTIONS = (
    "A WORLD STATE RECORD (JSON computed by code from the real USD stage and the user's 3D editor layout) describes "
    "what is in the scene right now; trust its numbers instead of estimating geometry. Meters, Y up, north = -Z, "
    "east = +X. position is the centre of an object's base. heading_deg: 0 = north (-Z), 90 = east (+X), clockwise from above. "
    "All normalized assets in the manifest (forward_axis = '-Z', including *.norm.usda wrappers for characters, cars, and trees) "
    "face -Z with base at y = 0; author xformOp:rotateY = -heading_deg. Only if a legacy un-normalized asset has forward_axis = '+Z', "
    "author xformOp:rotateY = 180 - heading_deg. Always ensure characters traveling along a path face their direction of motion. "
    "SOLID OBJECTS & COLLISION PREVENTION: Characters, cars, doors, and tree trunks are solid 3D colliders validated at every frame. "
    "Each object in state_start.json includes world-space `anchors` (e.g. `door_fl_stand`, `stand_front`, `stand_left`, `stand_right`) "
    "that are guaranteed clear of solid colliders. To make a character walk/run to an object or position around obstacles automatically, "
    "set navigation intent in the character prim's customData, e.g. `customData = { string asset_id = \"character.human\", string go_to = \"Car:door_fl_stand\" }` "
    "(or `double2 go_to = (x, z)`), and if the shot requires touching a door or object add `string allow_contact = \"Car:door_fl\"`. "
    "Keep all existing objects with defined_in = \"shot\" (and the current ShotCam camera framing) from state_start.json / CURRENT SHOT LAYER "
    "at their listed positions, headings, scales, and asset references unless the prompt asks to move, animate, or replace them. "
    "On every prim that references a manifest asset add customData = { string asset_id = \"<id>\" }. "
)


def world_state_python() -> str:
    configured = os.environ.get("PAINTERSPACE_PYTHON")
    if configured:
        return configured
    venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3"
    return str(venv) if venv.is_file() else sys.executable


def run_world_state(command: str, stage_path: Path, images: bool = True) -> dict | None:
    """Run tools/world_state.py; returns its JSON result or None (generation continues without it)."""
    script = Path(__file__).resolve().parent / "world_state.py"
    args = [world_state_python(), str(script), command, str(stage_path)] + ([] if images else ["--no-images"])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"World state unavailable: {exc}", file=sys.stderr)
        return None
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return None


def image_data_url(path: str | None) -> str | None:
    if not path or not Path(path).is_file():
        return None
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def call_model(
    prompt: str,
    manifest: str,
    scene_layer: str,
    existing_shot: str,
    feedback: str,
    model: str,
    api_key: str,
    fps: int,
    frames: int,
    world_state: str = "",
    images: list[str] | None = None,
) -> str:
    input_parts = [f"PROMPT:\n{prompt}"]
    if world_state:
        input_parts.append(f"WORLD STATE RECORD (state_start.json):\n{world_state}")
    if existing_shot:
        input_parts.append(f"CURRENT SHOT LAYER (shot.usda):\n{existing_shot}")
    input_parts.append(f"SHARED SCENE LAYER (../scene.usda):\n{scene_layer}")
    input_parts.append(f"USD ASSET MANIFEST:\n{manifest}")
    if feedback:
        input_parts.append(f"VALIDATION FEEDBACK:\n{feedback}")

    text_input = "\n\n".join(input_parts)
    request_input: str | list = text_input
    image_urls = [url for url in (image_data_url(p) for p in (images or [])) if url]
    if image_urls:
        content = [{"type": "input_text", "text": text_input}]
        content += [{"type": "input_image", "image_url": url} for url in image_urls]
        request_input = [{"role": "user", "content": content}]

    body = {
        "model": model,
        "instructions": (
            "You are a cinematic director authoring one OpenUSD ASCII SHOT LAYER (shot.usda). "
            "Return only a complete OpenUSD ASCII layer beginning with #usda 1.0. "
            "Never return scripts, markdown, URLs, or payloads. "
            "Set defaultPrim = \"World\", upAxis = \"Y\", metersPerUnit = 1, "
            f"startTimeCode = 0, endTimeCode = {frames - 1}, timeCodesPerSecond = {fps}, "
            "and subLayers = [ @../scene.usda@ ]. "
            "Use `over \"World\"` to add or override shot-specific actors, movement, and a Camera named \"ShotCam\" (at /World/ShotCam). "
            "Do not redefine the whole world from scene.usda. "
            "Reference USD assets from the manifest using their `shot_reference_path` starting with ../../../assets/. "
            "Keep total duration at or below 60 seconds and the stage below 2000 prims. "
            + (WORLD_STATE_INSTRUCTIONS if world_state else "")
        ),
        "input": request_input,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenAI Responses request failed: {exc}") from exc
    text = extract_text(payload).strip()
    text = re.sub(r"^```(?:usda)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return text.strip()


def local_checks(stage: str, stage_dir: Path, asset_root: Path, start: int, end: int) -> str:
    errors = []
    raw = stage.encode("utf-8")
    if len(raw) > MAX_STAGE_BYTES:
        errors.append(f"stage exceeds {MAX_STAGE_BYTES} bytes")
    if not stage.startswith("#usda 1.0"):
        errors.append("output must start with #usda 1.0")
    if re.search(r"\bpayload\s*=|#include|@[^@]*://", stage):
        errors.append("payloads, includes, and URL assets are prohibited")
    if len(re.findall(r"\b(?:def|over|class)\s+(?:\w+\s+)?\"", stage)) > MAX_PRIMS:
        errors.append(f"stage exceeds {MAX_PRIMS} prims")
    if end < start or end - start + 1 > MAX_FRAMES:
        errors.append(f"frame range must contain 1 to {MAX_FRAMES} frames")
    resolved_assets = asset_root.resolve()
    resolved_stage_dir = stage_dir.resolve()
    scene_dir = resolved_stage_dir.parent
    for match in re.finditer(r"@([^@]+)@", stage):
        ref = match.group(1)
        candidate = Path(ref)
        if candidate.is_absolute() or "://" in ref:
            errors.append(f"unsafe asset reference: {ref}")
            continue
        resolved = (stage_dir / candidate).resolve()
        in_assets = resolved_assets in resolved.parents or resolved == resolved_assets
        in_scene = resolved == (scene_dir / "scene.usda").resolve() or (
            scene_dir in resolved.parents and resolved.name == "shot.usda"
        )
        in_shot_dir = resolved.parent == resolved_stage_dir and candidate.suffix.lower() in {".usd", ".usda", ".usdc"}
        if not in_assets and not in_scene and not in_shot_dir:
            errors.append(f"reference escapes project assets and scene layer: {ref}")
        elif candidate.suffix.lower() not in {".usd", ".usda", ".usdc"}:
            errors.append(f"only USD references are allowed: {ref}")
        elif not resolved.is_file():
            errors.append(f"referenced USD file does not exist: {ref}")
    if not re.search(r"def\s+Camera\s+\"(?:ShotCam|RenderCamera)\"", stage):
        errors.append('stage must define a Camera named "ShotCam" or "RenderCamera"')
    return "; ".join(errors)


def inspect_stage_values(stage: str, default_fps: int, default_end_frame: int) -> tuple[int, int, int, str]:
    fps = default_fps
    start_frame = 0
    end_frame = default_end_frame
    rate_match = re.search(r"(?:timeCodesPerSecond|framesPerSecond)\s*=\s*(\d+(?:\.\d+)?)", stage)
    start_match = re.search(r"startTimeCode\s*=\s*(\d+(?:\.\d+)?)", stage)
    end_match = re.search(r"endTimeCode\s*=\s*(\d+(?:\.\d+)?)", stage)
    if rate_match:
        fps = max(1, min(120, round(float(rate_match.group(1)))))
    if start_match:
        start_frame = round(float(start_match.group(1)))
    if end_match:
        end_frame = round(float(end_match.group(1)))
    camera_prim = "/World/ShotCam"
    if re.search(r"def\s+Camera\s+\"RenderCamera\"", stage):
        camera_prim = "/World/CameraRig/RenderCamera"
    return fps, start_frame, end_frame, camera_prim


def run_validator(tool: str, stage_path: Path, args: list[str]) -> str:
    executable = which(tool)
    if not executable:
        return f"{tool} is not installed"
    result = subprocess.run([executable, *args, str(stage_path)], capture_output=True, text=True, timeout=60)
    return "" if result.returncode == 0 else (result.stderr or result.stdout or f"{tool} exited {result.returncode}").strip()


def run_collision_validator(
    stage_path: Path, manifest_path: Path, apply_intents: bool = True, repair: bool = False
) -> tuple[bool, str, dict]:
    """Run tools/collision/validate_collisions.py on stage_path.

    Returns (ok, error_message, report_dict).
    """
    script = Path(__file__).resolve().parent / "collision" / "validate_collisions.py"
    if not script.is_file():
        return True, "", {}
    resolved_manifest = manifest_path if manifest_path.is_file() else (Path(__file__).resolve().parent.parent / "manifest.json")
    cmd = [world_state_python(), str(script), str(stage_path), "--manifest", str(resolved_manifest), "--json", "-"]
    if apply_intents:
        cmd.append("--apply-intents")
    if repair:
        cmd.append("--repair")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"collision validator failed to run: {exc}", {}
    report: dict = {}
    if res.stdout.strip():
        try:
            report = json.loads(res.stdout)
        except json.JSONDecodeError:
            pass
    if res.returncode == 0 and report.get("ok", True):
        return True, "", report
    violations = report.get("violations") or []
    unprotected = report.get("unprotected_assets") or []
    parts: list[str] = []
    if unprotected:
        parts.append("Unprotected assets missing collision shapes: " + ", ".join(str(u) for u in unprotected))
    for v in violations:
        parts.append(
            f"COLLISION ({v.get('actor')} vs {v.get('object')} [{v.get('part')}] frames {v.get('frames')}, "
            f"worst frame {v.get('worst_frame')}, depth {v.get('max_penetration_m')}m, "
            f"push_out_m={v.get('push_out_m')}): {v.get('advice')}"
        )
    if not parts:
        parts.append((res.stderr or res.stdout or f"validate_collisions exited {res.returncode}").strip())
    return False, " | ".join(parts), report


def parse_shot_order(shot_dir_name: str) -> int:
    match = re.search(r"(\d+)$", shot_dir_name)
    return int(match.group(1)) if match else 1


def write_shot_metadata(
    metadata_path: Path,
    stage_path: Path,
    stage_text: str,
    prompt: str,
    default_fps: int,
    default_frames: int,
    model: str,
    validation_passed: bool,
) -> None:
    original_prompt = prompt
    revisions: list[str] = []
    if metadata_path.is_file():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            prev_prompt = str(existing.get("prompt", "")).strip()
            prev_revisions = existing.get("revisions", [])
            if isinstance(prev_revisions, list):
                revisions = [str(r) for r in prev_revisions]
            if prev_prompt:
                original_prompt = prev_prompt
                if prompt.strip() != prev_prompt:
                    revisions.append(prompt.strip())
        except (OSError, json.JSONDecodeError):
            pass

    fps, start_frame, end_frame, camera_prim = inspect_stage_values(stage_text, default_fps, default_frames - 1)
    shot_id = stage_path.parent.name
    scene_id = stage_path.parent.parent.name
    metadata_path.write_text(
        json.dumps(
            {
                "shot_id": shot_id,
                "scene_id": scene_id,
                "scene_order": parse_shot_order(shot_id),
                "stage": stage_path.name,
                "prompt": original_prompt,
                "revisions": revisions,
                "fps": fps,
                "frame_range": [start_frame, end_frame],
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration": (end_frame - start_frame) / fps if fps > 0 else 0.0,
                "camera_prim": camera_prim,
                "llm_model": model,
                "prompt_version": PROMPT_VERSION,
                "validation_passed": validation_passed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def compile_via_shot_library(prompt: str, output_path: Path, scene_layer_path: Path, fps: int = 24) -> tuple[bool, dict]:
    """Compile the shot deterministically via the Shot Function Library (LLMShotPlanner + ShotCompiler)."""
    project_root = Path(__file__).resolve().parent.parent
    shot_lib_dir = project_root / "tools" / "shot_library"
    if str(shot_lib_dir) not in sys.path:
        sys.path.insert(0, str(shot_lib_dir))
    try:
        from compiler import ShotCompiler
        from function_catalog import FunctionCatalog
        from llm_planner import LLMShotPlanner

        compiler = ShotCompiler(project_root=project_root, fps=fps)
        scene_data = compiler._read_scene_context(scene_layer_path)
        catalog = FunctionCatalog(compiler.manifest, scene_data)
        planner = LLMShotPlanner(catalog)
        ok_plan, plan, _ = planner.plan_from_prompt(prompt)
        if not ok_plan:
            return False, {"error": "Planner failed to construct ShotPlan"}
        ok_comp, report = compiler.compile(plan, output_path, scene_usda_path=scene_layer_path, validate=True)
        return ok_comp, report
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="plain-language cinematic moment or shot revision")
    parser.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    parser.add_argument("--asset-root", type=Path, default=Path("assets"))
    parser.add_argument("--output", type=Path, default=Path("scenes/scene_01/shot_001/shot.usda"))
    parser.add_argument("--metadata", type=Path, help="metadata JSON output (default: shot.json beside stage)")
    parser.add_argument("--model", default=os.environ.get("PAINTERSPACE_OPENAI_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--frames", type=int, default=97)
    parser.add_argument("--render-review-frame", action="store_true")
    parser.add_argument("--no-world-state-images", action="store_true",
                        help="send the world state record without the top-down map and camera images")
    parser.add_argument("--legacy-raw-llm", action="store_true",
                        help="bypass Shot Function Library compiler and request raw USDA directly from OpenAI")
    options = parser.parse_args()

    if options.fps < 1 or options.fps > 120 or options.frames < 1 or options.frames > MAX_FRAMES:
        parser.error("fps must be 1–120 and frames must be 1–1440")
    if options.output.name != "shot.usda":
        parser.error("output must be named shot.usda inside scenes/scene_XX/shot_XXX/")
    try:
        manifest = manifest_prompt(options.manifest, options.asset_root)
    except ValueError as exc:
        parser.error(str(exc))

    options.output.parent.mkdir(parents=True, exist_ok=True)
    scene_layer_path = options.output.parent.parent / "scene.usda"
    if not scene_layer_path.is_file():
        scene_layer_path.write_text(DEFAULT_SCENE_USDA, encoding="utf-8")

    # Primary Path: Deterministic Shot Function Library Compiler
    if not options.legacy_raw_llm:
        ok_lib, lib_report = compile_via_shot_library(
            options.prompt, options.output, scene_layer_path, fps=options.fps
        )
        if ok_lib and options.output.is_file():
            stage_text = options.output.read_text(encoding="utf-8")
            metadata_path = options.metadata or (options.output.parent / "shot.json")
            write_shot_metadata(
                metadata_path,
                options.output,
                stage_text,
                options.prompt,
                options.fps,
                options.frames,
                options.model,
                True,
            )
            print(f"Generated and validated {options.output} via Shot Function Library Compiler")
            print(f"Wrote metadata {metadata_path}")
            finalized = run_world_state("finalize-shot", options.output, images=not options.no_world_state_images)
            if finalized:
                print(f"Wrote world state {finalized['state']}")
            return 0

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        parser.error("Set OPENAI_API_KEY in the environment; credentials are never read from project files")
    scene_layer_text = scene_layer_path.read_text(encoding="utf-8")
    existing_shot_text = options.output.read_text(encoding="utf-8") if options.output.is_file() else ""

    # Rebuild the world state record from the real stage before the request.
    world_state_text, world_state_images = "", []
    prepared = run_world_state("prepare-shot", options.output, images=not options.no_world_state_images)
    if prepared:
        world_state_text = Path(prepared["state"]).read_text(encoding="utf-8")
        world_state_images = [p for p in (prepared.get("images") or {}).values() if p]
    else:
        print("World state record unavailable; generating without it", file=sys.stderr)

    feedback = ""
    stage = ""
    validation_passed = False
    metadata_path = options.metadata or (options.output.parent / "shot.json")

    for attempt in range(RETRIES + 1):
        try:
            stage = call_model(
                options.prompt,
                manifest,
                scene_layer_text,
                existing_shot_text,
                feedback,
                options.model,
                api_key,
                options.fps,
                options.frames,
                world_state_text,
                world_state_images,
            )
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        error = local_checks(stage, options.output.parent.resolve(), options.asset_root, 0, options.frames - 1)
        if not error:
            options.output.write_text(stage.rstrip() + "\n", encoding="utf-8")
            error = run_validator("usdcat", options.output, ["--loadOnly"])
        if not error:
            checker = which("usdchecker")
            if checker:
                error = run_validator("usdchecker", options.output, [])
        if not error:
            ok_coll, coll_err, _ = run_collision_validator(
                options.output, options.manifest, apply_intents=True, repair=(attempt == RETRIES)
            )
            if options.output.is_file():
                stage = options.output.read_text(encoding="utf-8")
            if not ok_coll:
                error = coll_err
        if not error:
            validation_passed = True
            break
        feedback = "Previous stage failed validation: " + error
        if attempt == RETRIES:
            write_shot_metadata(
                metadata_path,
                options.output,
                stage,
                options.prompt,
                options.fps,
                options.frames,
                options.model,
                False,
            )
            print(feedback, file=sys.stderr)
            return 1

    write_shot_metadata(
        metadata_path,
        options.output,
        stage,
        options.prompt,
        options.fps,
        options.frames,
        options.model,
        validation_passed,
    )
    print(f"Generated and validated {options.output}")
    print(f"Wrote metadata {metadata_path}")
    finalized = run_world_state("finalize-shot", options.output, images=not options.no_world_state_images)
    if finalized:
        print(f"Wrote world state {finalized['state']}")
        for warning in finalized.get("warnings", []):
            print(f"World state warning: {warning}", file=sys.stderr)

    if options.render_review_frame:
        usdrecord = which("usdrecord")
        if not usdrecord:
            print("usdrecord is not installed; review frame skipped", file=sys.stderr)
            return 1
        review = options.output.parent / "thumbnail.jpg"
        _, _, _, cam_prim = inspect_stage_values(stage, options.fps, options.frames - 1)
        result = subprocess.run(
            [usdrecord, str(options.output), str(review), "--frames", "0", "--cam", cam_prim],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            print((result.stderr or result.stdout).strip(), file=sys.stderr)
            return 1
        print(f"Rendered thumbnail {review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
