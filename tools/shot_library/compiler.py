"""Compiler Pipeline for PainterSpace Shot Function Library.

Compiles a shot_plan.json into:
1. resolved_timeline.json
2. actions_<actor>.json & anim_<actor>.usda (baked and bound UsdSkel animations)
3. shot.usda (OpenUSD layer sublayering ../scene.usda with actor xforms, prim customData, camera, and vehicle motion)
4. allow_collisions.json
5. validation_report.json (collision validation & physical shot linting)
6. shot.json (metadata sidecar)
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Add tools paths
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "collision"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))

from actor_registry import ActorAppearance, ActorRecord, ActorRegistry
from camera_controller import CameraController
from crowd_spacing import actor_envelopes, space_standing_actors
from asset_matcher import ManifestAssetMatcher, tokenize
from manifest_schema import enrich_manifest
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from plan_model import Beat, SetupAction, ShotPlan, StructuredError, TimingCondition
from semantic_references import ReferenceResolver, ResolvedTarget
from shot_linter import ShotLinter
from timeline_resolver import ResolvedBeat, ResolvedTimeline, TimelineResolver


def missing_local_sublayers(scene_usda_path: Path) -> List[Dict[str, str]]:
    """Find broken local dependencies throughout a scene's layer tree."""
    from pxr import Sdf

    missing: List[Dict[str, str]] = []
    visited = set()

    def visit(path: Path) -> None:
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        layer = Sdf.Layer.FindOrOpen(str(path))
        if layer is None:
            return
        for authored in layer.subLayerPaths:
            resolved = Sdf.ComputeAssetPathRelativeToLayer(layer, authored)
            if not resolved or "://" in resolved:
                continue
            child = Path(resolved)
            if not child.is_absolute():
                child = path.parent / child
            child = child.resolve()
            if not child.exists():
                missing.append({
                    "source_layer": str(path),
                    "authored_path": authored,
                    "resolved_path": str(child),
                })
            else:
                visit(child)

    visit(scene_usda_path)
    return missing


class ShotCompiler:
    @staticmethod
    def _actor_synthesis_order(actor_ids, actor_schedules, initial_states):
        """Resolve a moving target's trajectory before an actor travels to it."""
        moving = {
            "exit_vehicle", "enter_vehicle", "walk_to", "run_to", "jog_to",
            "sneak_to", "approach", "flee_from", "follow", "disperse",
            "crawl_to", "stumble_to", "back_up_to", "carry_to", "swim_to",
            "fly_to", "take_cover", "jump_on", "jump_down", "jump_over",
            "climb_stairs", "climb_surface", "vault_over", "stage_pair",
        }
        aliases = {actor_id.lower(): actor_id for actor_id in actor_ids}
        aliases.update({str(initial_states[actor_id].get("label")).lower(): actor_id
                        for actor_id in actor_ids if initial_states[actor_id].get("label")})
        dependencies = {actor_id: set() for actor_id in actor_ids}
        for actor_id in actor_ids:
            for beat in actor_schedules.get(actor_id, []):
                if beat.function not in moving:
                    continue
                target_id = aliases.get(str(beat.params.get("target") or "").lower())
                if target_id and target_id != actor_id and any(
                        candidate.function in moving
                        for candidate in actor_schedules.get(target_id, [])):
                    dependencies[actor_id].add(target_id)
        ordered = []
        remaining = list(actor_ids)
        while remaining:
            ready = next((actor_id for actor_id in remaining
                          if dependencies[actor_id].issubset(ordered)), None)
            if ready is None:
                raise ValueError(
                    "Mutual movement targets require a joint trajectory: "
                    + ", ".join(remaining))
            ordered.append(ready)
            remaining.remove(ready)
        return ordered

    def _prepare_stair_climbs(self, plan: ShotPlan, initial_states: Dict[str, Dict[str, Any]],
                              synthesizer: MotionSynthesizer) -> Optional[StructuredError]:
        """Fit prompt stair actions to the real flight and assign opposite tread lanes."""
        stair_groups: Dict[Tuple[str, str], List[Beat]] = {}
        for beat in plan.beats:
            if beat.function != "climb_stairs":
                continue
            info = synthesizer._resolve_stair(str(beat.params.get("stairs", "")))
            if not info:
                return StructuredError(
                    "STAIR_NOT_IN_SCENE", "The requested stair is not present in the starting scene.",
                    beat_id=beat.beat_id, actor_id=beat.actor_id,
                    suggestions=["Use a stair portal on an existing scene asset."])
            stair_groups.setdefault((info["asset_id"], info["stair_id"]), []).append(beat)

        for key, beats in stair_groups.items():
            info = synthesizer._resolve_stair(beats[0].params["stairs"])
            stair = info["data"]
            width = float(stair.get("width_m", 0.0))
            if len({b.actor_id for b in beats}) == 2 and len(beats) == 2:
                if width < 1.2:
                    return StructuredError("STAIR_TOO_NARROW", "Two climbers need separate usable stair lanes.",
                                           beat_id=beats[0].beat_id)
                centerline = stair.get("centerline", [])
                if len(centerline) >= 2:
                    p0, p1 = centerline[0], centerline[-1]
                    dx, dz = p1[0] - p0[0], p1[2] - p0[2]
                    length = max(1e-6, math.hypot(dx, dz))
                    lateral = (-dz / length, dx / length)
                    beats.sort(key=lambda b: (
                        (initial_states.get(b.actor_id, {}).get("position", p0)[0] - p0[0]) * lateral[0]
                        + (initial_states.get(b.actor_id, {}).get("position", p0)[2] - p0[2]) * lateral[1]))
                    lane = min(0.4, width * 0.5 - 0.25)
                    beats[0].params["stair_lane_offset_m"] = -lane
                    beats[1].params["stair_lane_offset_m"] = lane
                    beats[0].params["stair_exit_distance_m"] = 0.8
                    if beats[1].start_condition is None:
                        beats[1].start_condition = TimingCondition("at", 1.2)
                    later = next((b for b in plan.beats[plan.beats.index(beats[0]) + 1:]
                                  if b.actor_id == beats[0].actor_id and b.function != "climb_stairs"), None)
                    if later is not None and later.start_condition is None:
                        later.start_condition = TimingCondition("after", f"{beats[1].beat_id}.finished")

            for beat in beats:
                if beat.params.get("exact_window") or beat.duration_condition is not None:
                    continue
                pts = [list(p) for p in stair.get("centerline", [])]
                if len(pts) < 2:
                    continue
                if beat.params.get("direction", "up") == "down":
                    pts.reverse()
                offset = float(beat.params.get("stair_lane_offset_m", 0.0))
                dx, dz = pts[-1][0] - pts[0][0], pts[-1][2] - pts[0][2]
                length = max(1e-6, math.hypot(dx, dz))
                entry = [pts[0][0] - dz / length * offset, pts[0][2] + dx / length * offset]
                source = initial_states.get(beat.actor_id, {}).get("position", pts[0])
                approach = math.hypot(source[0] - entry[0], source[2] - entry[1])
                if approach > 0.5:
                    back_x, back_z = -dx / length * 0.7, -dz / length * 0.7
                    approach_pts = [list(source),
                                    [source[0] + back_x, source[1], source[2] + back_z],
                                    [entry[0] + back_x, pts[0][1], entry[1] + back_z],
                                    [entry[0], pts[0][1], entry[1]]]
                    approach = sum(math.dist(a, b) for a, b in zip(approach_pts, approach_pts[1:]))
                flight = sum(math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(pts, pts[1:]))
                run = beat.params.get("pace", "walk") == "run"
                climb_speed = float(beat.params.get("speed_mps") or (2.0 if run else 0.65))
                max_speed = 1.0 if beat.params.get("direction") == "down" else (2.5 if run else 0.8)
                climb_speed = min(climb_speed, max_speed)
                needed = (approach / (3.0 if run else 1.25) if approach > 0.15 else 0.0) + flight / max(0.1, climb_speed)
                needed += float(beat.params.get("stair_exit_distance_m", 0.0)) / (3.0 if run else 1.25)
                default = 2.5 if run else 6.0
                beat.duration_condition = TimingCondition("for_duration", max(default, needed))
        return None

    @staticmethod
    def _release_animated_actor_overrides(shot_path: Path) -> None:
        """Let generated motion show through static editor placement opinions.

        The editor stores initial placement in its stronger session layer.  Once
        that placement has been consumed by the compiler, its default xform
        values would mask every time sample in shot.usda.
        """
        from pxr import Sdf, Usd

        edits_path = shot_path.parent / "user_edits.usda"
        if not edits_path.exists():
            return
        shot = Usd.Stage.Open(str(shot_path))
        shot_layer = Sdf.Layer.FindOrOpen(str(shot_path))
        edits = Sdf.Layer.FindOrOpen(str(edits_path))
        if not shot or not shot_layer or not edits:
            return
        edits.Reload()
        changed = False
        for prim in shot.Traverse():
            motion = prim.GetAttribute("xformOp:translate")
            if not motion or not motion.GetNumTimeSamples():
                continue
            spec = edits.GetPrimAtPath(prim.GetPath())
            if not spec:
                continue
            generated_spec = shot_layer.GetPrimAtPath(prim.GetPath())
            generated_refs = (generated_spec.referenceList.GetAddedOrExplicitItems()
                              if generated_spec else [])
            manual_refs = spec.referenceList.prependedItems
            if generated_refs and manual_refs and all(
                    any(Path(manual.assetPath).with_suffix("") ==
                        Path(generated.assetPath).with_suffix("")
                        for generated in generated_refs)
                    for manual in manual_refs):
                spec.referenceList.ClearEdits()
                changed = True
            edited_motion = spec.properties.get("xformOp:translate")
            if edited_motion and edited_motion.ListTimeSamples():
                continue  # An explicit animated edit remains authoritative.
            for name in ("xformOp:translate", "xformOp:rotateXYZ",
                         "xformOp:rotateY", "xformOp:rotateX"):
                prop = spec.properties.get(name)
                if prop and not prop.ListTimeSamples():
                    spec.RemoveProperty(prop)
                    changed = True
            order = spec.properties.get("xformOpOrder")
            if order:
                # Keep unrelated manual scale/transform ops alongside shot motion.
                shot_order = list(prim.GetAttribute("xformOpOrder").Get() or [])
                extras = [op for op in (order.default or [])
                          if op not in shot_order and op in spec.properties]
                if extras:
                    order.default = shot_order + extras
                else:
                    spec.RemoveProperty(order)
                changed = True
        if changed:
            edits.Save()

    @staticmethod
    def _restore_placed_actor_visibility(shot_path: Path, actor_paths: set[str]) -> None:
        """Discard stale editor deletions when a plan explicitly places an actor again.

        Scene snapshots are weaker than the generated shot, but the editor's
        session layer is stronger. Keep all unrelated manual edits intact.
        """
        from pxr import Sdf

        edits_path = shot_path.parent / "user_edits.usda"
        if not actor_paths or not edits_path.exists():
            return
        edits = Sdf.Layer.FindOrOpen(str(edits_path))
        if not edits:
            return
        edits.Reload()
        changed = False
        for path in actor_paths:
            spec = edits.GetPrimAtPath(path)
            if not spec:
                continue
            if spec.HasInfo("active") and spec.GetInfo("active") is False:
                spec.ClearInfo("active")
                changed = True
            visibility = spec.properties.get("visibility")
            if (visibility and visibility.default == "invisible"
                    and not visibility.ListTimeSamples()):
                spec.RemoveProperty(visibility)
                changed = True
        if changed:
            edits.Save()

    @staticmethod
    def _read_existing_actors(shot_path: Path, actors: Optional[set], tracks: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Resolve action actors against the same root/session composition as the editor."""
        from pxr import Gf, Sdf, Usd, UsdGeom

        if not shot_path.exists():
            return {}
        root = Sdf.Layer.FindOrOpen(str(shot_path))
        edits_path = shot_path.parent / "user_edits.usda"
        edits = Sdf.Layer.FindOrOpen(str(edits_path)) if edits_path.exists() else None
        stage = Usd.Stage.Open(root, edits) if edits else Usd.Stage.Open(root)
        if actors is None:
            actors = {str(p.GetCustomDataByKey("actor_id") or p.GetName())
                      for p in stage.Traverse() if p.GetCustomDataByKey("asset_id")
                      and UsdGeom.Xformable(p)}
        result = {}
        for actor in actors:
            path = tracks.get(actor, {}).get("actor_path") or "/World/" + actor
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsActive():
                prim = next((p for p in stage.Traverse()
                             if p.GetCustomDataByKey("actor_id") == actor), None)
            if not prim or not prim.IsActive():
                continue
            asset_id = prim.GetCustomDataByKey("asset_id")
            if not asset_id or not UsdGeom.Xformable(prim):
                continue
            transform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode(0))
            forward = transform.TransformDir(Gf.Vec3d(0, 0, -1))
            appearance = ActorAppearance(
                color_variant=str(prim.GetCustomDataByKey("appearance_variant") or "default"),
                height_scale=float(prim.GetCustomDataByKey("height_scale") or 1.0),
                tint_rgb=list(prim.GetCustomDataByKey("tint_rgb") or [1.0, 1.0, 1.0]),
            )
            result[actor] = {
                "actor_id": actor, "label": prim.GetName(), "prim_path": str(prim.GetPath()),
                "asset_id": str(asset_id), "position": list(transform.ExtractTranslation()),
                "facing_deg": math.degrees(math.atan2(forward[0], -forward[2])) % 360.0,
                "is_vehicle": "car" in asset_id or "vehicle" in asset_id,
                "appearance": appearance,
            }
        return result

    def __init__(self, project_root: Optional[Path] = None, fps: int = 24, crowd_config=None) -> None:
        from crowd.config import CrowdSolverConfig
        self.crowd_config = crowd_config or CrowdSolverConfig()
        self.project_root = project_root or REPO_ROOT
        self.fps = fps
        self.manifest_path = self.project_root / "manifest.json"
        raw_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists() else {}
        self.manifest = enrich_manifest(raw_manifest, self.project_root)
        self.matcher = ManifestAssetMatcher(self.manifest)

    def compile(
        self,
        plan: ShotPlan,
        output_shot_usda_path: Path,
        scene_usda_path: Optional[Path] = None,
        validate: bool = True,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Validate private candidates and promote only a passing shot.

        ``validate=False`` remains an explicit development/preview bypass; it
        does not run repair or certify collision freedom. Production queue
        callers always use the validated path.
        """
        if validate and Path(output_shot_usda_path).name == 'scene.usda' and plan.beats:
            return False, {'success': False, 'error': StructuredError(
                'SCENE_ACTIONS_REQUIRE_SHOT',
                'Action beats belong in a shot layer so later shots inherit only the shared scene.'
            ).to_dict()}
        if not validate:
            return self._compile_once(copy.deepcopy(plan), output_shot_usda_path,
                                      scene_usda_path, validate=False)
        from dataclasses import replace
        from compile_transaction import CompileTransaction, relocate_report
        from crowd.repair import clearance_deficit, next_clearance

        output = Path(output_shot_usda_path).resolve()
        scene = Path(scene_usda_path).resolve() if scene_usda_path is not None else None
        if output.name == 'scene.usda' and scene is not None and scene != output:
            return False, {'success': False, 'error': StructuredError(
                'SCENE_STAGE_SOURCE_MISMATCH',
                'A scene-stage compile must use its output scene.usda as the source scene.').to_dict()}
        history = []
        clearance = self.crowd_config.buffer_m
        for attempt in range(self.crowd_config.max_repair_attempts + 1):
            with CompileTransaction(output) as transaction:
                candidate = copy.copy(self)
                candidate.crowd_config = replace(self.crowd_config, buffer_m=clearance)
                candidate._spatial_layout_variant = attempt
                staged_output = transaction.stage / output.name
                staged_scene = (staged_output if output.name == 'scene.usda'
                                else scene or output.parent.parent / 'scene.usda')
                ok, report = candidate._compile_once(
                    copy.deepcopy(plan), staged_output, staged_scene, validate=True)
                report = relocate_report(report, transaction.stage, output.parent)
                history.append({'attempt': attempt, 'planning_buffer_m': clearance,
                                'success': ok, 'error': report.get('error'),
                                'collision_report': report.get('validation', {}).get('collision_report')})
                if plan.spatial_intents:
                    history[-1]['spatial_layout_variant'] = candidate._spatial_layout_variant
                if ok:
                    report['crowd_repair'] = {'retries': attempt, 'attempts': history}
                    validation_path = transaction.stage / 'validation_report.json'
                    if validation_path.exists():
                        validation = json.loads(validation_path.read_text())
                        validation['crowd_repair'] = report['crowd_repair']
                        validation_path.write_text(json.dumps(validation, indent=2) + '\n')
                        report['validation'] = validation
                    try:
                        transaction.publish()
                    except (OSError, RuntimeError) as exc:
                        return False, {'success': False, 'error': StructuredError(
                            'SHOT_PUBLICATION_FAILED', str(exc)).to_dict(), 'crowd_repair': report['crowd_repair']}
                    return True, report
                following = next_clearance(self.crowd_config, clearance, report)
                # Region contracts authorize placement search. A failed route
                # can try another checked grid layout without changing intent,
                # authored input, or a previously published shot.
                spatial_retry = (bool(plan.spatial_intents) and (report.get('error') or {}).get('error_code') in
                                 {'SPATIAL_INTENT_LIMIT', 'SPATIAL_INTENT_CONFORMANCE', 'CROWD_ROUTE_LIMIT',
                                  'SPATIAL_ROUTE_REGION', 'SPATIAL_ROUTE_CONFORMANCE', 'SPATIAL_ORIGIN_CONFORMANCE',
                                  'CROWD_DESTINATION_LIMIT', 'INTERACTION_SIMULTANEOUS'})
                if following is None and spatial_retry and attempt < self.crowd_config.max_repair_attempts:
                    following = clearance
                if following is None or attempt == self.crowd_config.max_repair_attempts:
                    stopped = ('spatial_layout_attempt_limit' if spatial_retry else
                               'unrepairable_failure' if clearance_deficit(report) is None
                               else 'clearance_limit' if following is None else 'attempt_limit')
                    report['crowd_repair'] = {'retries': attempt, 'attempts': history,
                                             'stopped': stopped}
                    if not report.get('error') and not report.get('validation', {}).get('collision_passed', True):
                        report['error'] = StructuredError(
                            'COLLISION_VALIDATION_FAILED',
                            'Final animation failed collision validation; no candidate was published.',
                            details={'repair_stop': stopped}).to_dict()
                    # These paths belong to discarded candidates, not accepted output.
                    report.pop('artifacts', None)
                    return False, report
                clearance = following

    def _compile_once(
        self,
        plan: ShotPlan,
        output_shot_usda_path: Path,
        scene_usda_path: Optional[Path] = None,
        validate: bool = True,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Compile a ShotPlan into a complete shot directory.

        Returns (success: bool, report: dict).
        """
        output_shot_usda_path = output_shot_usda_path.resolve()
        shot_dir = output_shot_usda_path.parent
        shot_dir.mkdir(parents=True, exist_ok=True)

        # The output location defines a scene-stage compile. Callers may omit
        # --scene, but that must never turn scene.usda into a shot layer that
        # sublayers the nonexistent ../scene.usda.
        is_scene_stage = output_shot_usda_path.name == "scene.usda"
        if is_scene_stage:
            if scene_usda_path is not None and scene_usda_path.resolve() != output_shot_usda_path:
                return False, {"success": False, "error": StructuredError(
                    "SCENE_STAGE_SOURCE_MISMATCH",
                    "A scene-stage compile must use its output scene.usda as the source scene.",
                ).to_dict()}
            scene_usda_path = output_shot_usda_path
        elif scene_usda_path is None:
            scene_usda_path = shot_dir.parent / "scene.usda"

        if scene_usda_path.exists():
            missing_sublayers = missing_local_sublayers(scene_usda_path)
            if missing_sublayers:
                return False, {"success": False, "error": StructuredError(
                    "SCENE_SUBLAYER_MISSING",
                    "The source scene has missing local sublayers; repair its layer references before compiling a shot.",
                    details={"missing_sublayers": missing_sublayers},
                ).to_dict()}

        from director_brief import load_brief, merge_brief, check_conformance
        try:
            brief = load_brief(shot_dir / "director_brief.json")
            from director_brief import validate_brief
            from function_catalog import FunctionCatalog
            allowed = {name for name in dir(FunctionCatalog) if not name.startswith('_')} | set(self.manifest.get('clips', {})) | {'sprint_to'}
            lint = validate_brief(brief, actions=allowed)
            if lint:
                raise ValueError(json.dumps(lint))
            from director_brief import enrich_brief_choices
            brief = enrich_brief_choices(brief, self.manifest)
            plan = merge_brief(brief, plan)
            from combat_moves import resolve_attack_moves
            for beat in plan.beats:
                if beat.function == "attack":
                    try:
                        beat.params["moves"] = resolve_attack_moves(beat.params.get("moves"), self.manifest.get("clips", {}))
                    except ValueError as exc:
                        return False, {"success": False, "error": StructuredError(
                            "ATTACK_CLIP", str(exc), beat_id=beat.beat_id,
                        ).to_dict()}
        except (ValueError, KeyError, TypeError) as exc:
            return False, {"success": False, "error": {"error_code": "DIRECTOR_BRIEF", "message": str(exc)}}

        # Resolve immutable scene snapshot version
        # A scene-stage request writes back to scene.usda. Snapshot that source
        # beside the output so the generated layer can keep the original scene
        # without referring to itself.
        scene_dir = shot_dir if is_scene_stage else shot_dir.parent
        scene_version_file = None
        scene_edits_version_file = None
        scene_edits_path = shot_dir / "user_edits.usda" if is_scene_stage else None
        if scene_usda_path and scene_usda_path.exists():
            v_files = []
            for f in scene_dir.glob("scene_v*.usda"):
                m = re.match(r"^scene_v(\d+)\.usda$", f.name)
                if m:
                    v_files.append((int(m.group(1)), f.name, f))
            latest_num = 0
            if v_files:
                v_files.sort(key=lambda x: x[0])
                latest_num, latest_name, latest_file = v_files[-1]
                scene_hash = hashlib.sha256(scene_usda_path.read_bytes()).hexdigest()
                latest_hash = hashlib.sha256(latest_file.read_bytes()).hexdigest()
                edits_match = True
                if is_scene_stage:
                    latest_edits = scene_dir / f"scene_edits_v{latest_num}.usda"
                    edits_match = (scene_edits_path.exists() == latest_edits.exists())
                    if edits_match and scene_edits_path.exists():
                        edits_match = (hashlib.sha256(scene_edits_path.read_bytes()).digest()
                                       == hashlib.sha256(latest_edits.read_bytes()).digest())
                if scene_hash == latest_hash and edits_match:
                    scene_version_file = latest_name
                    if is_scene_stage and scene_edits_path.exists():
                        scene_edits_version_file = f"scene_edits_v{latest_num}.usda"
            if scene_version_file is None:
                new_num = latest_num + 1
                new_name = f"scene_v{new_num}.usda"
                new_path = scene_dir / new_name
                try:
                    shutil.copy2(scene_usda_path, new_path)
                    if is_scene_stage and scene_edits_path.exists():
                        scene_edits_version_file = f"scene_edits_v{new_num}.usda"
                        shutil.copy2(scene_edits_path, scene_dir / scene_edits_version_file)
                    scene_version_file = new_name
                    self._update_scene_json_snapshots(scene_dir, new_name, new_num)
                except Exception:
                    if not is_scene_stage and v_files:
                        scene_version_file = latest_name
        if is_scene_stage and not scene_version_file:
            return False, {"success": False, "error": {
                "error_code": "SCENE_SNAPSHOT_FAILED",
                "message": "Could not snapshot the starting scene before compiling into scene.usda.",
            }}

        from director_brief import expand_open_windows
        # Mirror the paths selected by merge_brief. A saved prompt string alone
        # cannot suppress a route, and unrelated unassigned paths stay guides.
        synthetic_paths = {
            pid for pid, path in brief.get("paths", {}).items()
            if any(b.beat_id == f"{path['owner']}.route_{pid}" for b in plan.beats)
        }
        brief = expand_open_windows(brief, synthesize_path_only=True,
                                    path_ids=synthetic_paths)

        # 1. Parse scene context (zones, static objects)
        scene_data = self._read_scene_context(scene_usda_path, shot_dir=shot_dir)
        if validate and scene_data.get("read_error"):
            return False, {"success": False, "error": StructuredError(
                "SCENE_CONTEXT_UNAVAILABLE",
                "The scene geometry could not be read for spatial planning and collision checks.",
                details={"reason": scene_data["read_error"]},
            ).to_dict()}
        uncovered = scene_data.get("unprotected_scene_objects", [])
        if validate and uncovered and (plan.beats or plan.setup):
            return False, {"success": False, "error": StructuredError(
                "SCENE_COLLISION_COVERAGE",
                "Scene geometry has no collision coverage, so actor and vehicle placements cannot be certified.",
                details={"objects": uncovered},
            ).to_dict()}
        ref_resolver = ReferenceResolver(self.manifest, scene_data, self.crowd_config)

        # 2. Setup actors and initialize registry
        registry = ActorRegistry()
        actor_initial_states: Dict[str, Dict[str, Any]] = {}
        existing_actors = self._read_existing_actors(
            output_shot_usda_path,
            {b.actor_id for b in plan.beats} | {s.actor_id for s in plan.setup if s.actor_id},
            brief.get("tracks", {}),
        )

        for setup_act in plan.setup:
            if setup_act.action_type == "place":
                asset_id = setup_act.asset_id or "character.human"
                app = None
                if setup_act.appearance:
                    app = ActorAppearance.from_dict(setup_act.appearance)
                elif setup_act.actor_id in existing_actors:
                    app = existing_actors[setup_act.actor_id]["appearance"]

                rec = registry.register(
                    asset_id=existing_actors.get(setup_act.actor_id, {}).get("asset_id", asset_id),
                    label=existing_actors.get(setup_act.actor_id, {}).get("label", setup_act.label),
                    actor_id=setup_act.actor_id,
                    appearance=app,
                    prim_path=existing_actors.get(setup_act.actor_id, {}).get("prim_path"),
                )

                # Resolve initial position and facing
                existing = existing_actors.get(setup_act.actor_id, {})
                init_pos = list(existing.get("position", [0.0, 0.0, 0.0]))
                init_facing = existing.get("facing_deg", 0.0)
                is_veh = "car" in rec.asset_id or "vehicle" in rec.asset_id
                placement_states = dict(actor_initial_states)
                placement_states[rec.actor_id] = dict(existing, asset_id=rec.asset_id,
                                                       is_vehicle=is_veh, appearance=rec.appearance)
                if setup_act.at_reference:
                    target_res, err = ref_resolver.resolve(
                        setup_act.at_reference, placement_states, 0.0, source_actor_id=rec.actor_id
                    )
                    if not target_res:
                        return False, {'success': False, 'error': StructuredError(
                            'UNRESOLVED_REFERENCE',
                            f"Could not resolve starting location '{setup_act.at_reference}': {err}",
                            actor_id=rec.actor_id,
                            details={'reference': setup_act.at_reference, 'role': 'starting_location'}).to_dict()}
                    init_pos = list(target_res.position)
                    init_facing = target_res.facing_deg
                elif setup_act.position:
                    init_pos = list(setup_act.position)
                    if setup_act.facing_deg is not None:
                        init_facing = float(setup_act.facing_deg)

                from semantic_references import direction_heading
                compass_heading = direction_heading(setup_act.facing_reference)
                if compass_heading is not None:
                    init_facing = compass_heading
                elif setup_act.facing_reference:
                    placement_states[rec.actor_id]['position'] = init_pos
                    face_res, _ = ref_resolver.resolve(
                        setup_act.facing_reference, placement_states, 0.0, source_actor_id=rec.actor_id
                    )
                    if face_res:
                        dx = face_res.position[0] - init_pos[0]
                        dz = face_res.position[2] - init_pos[2]
                        if math.hypot(dx, dz) > 1e-3:
                            init_facing = math.degrees(math.atan2(dx, -dz)) % 360.0

                seated_veh = None
                seat_anch = None
                if setup_act.at_reference and ":" in setup_act.at_reference:
                    ref_obj, ref_sub = setup_act.at_reference.split(":", 1)
                    if "seat" in ref_sub.lower():
                        seated_veh = ref_obj
                        seat_anch = ref_sub

                actor_initial_states[rec.actor_id] = {
                    "actor_id": rec.actor_id,
                    "label": rec.label,
                    "asset_id": rec.asset_id,
                    "position": init_pos,
                    "facing_deg": init_facing,
                    "is_vehicle": is_veh,
                    "appearance": rec.appearance,
                    "at_reference": setup_act.at_reference,
                    "seated_vehicle": seated_veh,
                    "seat_anchor": seat_anch,
                }
            elif setup_act.action_type == "set_appearance":
                rec = registry.get(setup_act.actor_id)
                if rec:
                    for k, v in setup_act.appearance.items():
                        if hasattr(rec.appearance, k):
                            setattr(rec.appearance, k, v)
                    if rec.actor_id in actor_initial_states:
                        actor_initial_states[rec.actor_id]["appearance"] = rec.appearance

        # If no setup declared, deduce from beats or pending world state
        if plan.beats:
            for b in plan.beats:
                if b.actor_id not in registry._actors:
                    existing = existing_actors.get(b.actor_id)
                    if existing:
                        rec = registry.register(
                            asset_id=existing["asset_id"], actor_id=b.actor_id,
                            label=existing["label"], prim_path=existing["prim_path"],
                            appearance=existing["appearance"],
                        )
                        actor_initial_states[b.actor_id] = dict(existing)
                        continue
                    default_car = next((a.get("id") for a in self.manifest.get("assets", []) if "car" in a.get("id", "") or "car" in a.get("tags", [])), "cars.car1")
                    default_human = next((a.get("id") for a in self.manifest.get("assets", []) if "character" in a.get("id", "") or "human" in a.get("id", "")), "character.human")
                    track = brief.get('tracks', {}).get(b.actor_id, {})
                    asset_id = track.get('asset_id') or (default_car if "car" in b.actor_id else default_human)
                    rec = registry.register(asset_id=asset_id, actor_id=b.actor_id, label=track.get('label'))
                    is_veh = "car" in asset_id
                    actor_initial_states[rec.actor_id] = {
                        "actor_id": rec.actor_id,
                        "label": rec.label,
                        "asset_id": rec.asset_id,
                        "position": [0.0, 0.0, 5.0] if is_veh else [0.0, 0.0, -2.0],
                        "facing_deg": 0.0,
                        "is_vehicle": is_veh,
                        "appearance": rec.appearance,
                    }

        from interaction_plan import apply_interactions, validate_interactions
        # Phase intent applies to complete authored actions, not the last
        # segment of a saved generated route. Rebuild before binding timing.
        from crowd.movements import movement_source_plan
        plan = movement_source_plan(plan)
        from route_placement import place_unpositioned_movers
        placement_error = place_unpositioned_movers(
            plan, actor_initial_states, scene_data, self.manifest, self.project_root,
            self.crowd_config, locked_actors=set(brief.get('tracks', {})) | set(existing_actors))
        if placement_error:
            return False, {'success': False, 'error': placement_error.to_dict()}
        from plan_review import review_plan, review_suggestions
        from spatial_intents import bind_spatial_intents
        spatial_error = bind_spatial_intents(
            plan, actor_initial_states, scene_data, self.manifest, self.project_root,
            self.crowd_config, locked_actors=brief.get('tracks', {}),
            variant=getattr(self, '_spatial_layout_variant', 0))
        if spatial_error:
            return False, {'success': False, 'error': spatial_error.to_dict()}
        plan_review = review_plan(plan, scene_data, self.manifest, actor_initial_states,
                                  locked_actors=brief.get('tracks', {}))
        if not plan_review['passed']:
            return False, {'success': False, 'error': StructuredError(
                'PLAN_REVIEW', 'The generated plan conflicts with explicit prompt requirements or cannot verify them.',
                details=plan_review,
                suggestions=review_suggestions(plan_review)
            ).to_dict(), 'plan_review': plan_review}
        declared_error = apply_interactions(plan, actor_initial_states)
        if declared_error:
            return False, {"success": False, "error": declared_error.to_dict()}

        # Expose prompt-created contact pairs before spacing and arena layout.
        # The later interaction scheduler can link again; linked beats retain
        # their group IDs and are therefore not duplicated.
        from director_solver import link_prompt_interactions
        plan = link_prompt_interactions(plan, actor_initial_states)
        placement_timeline, placement_error = TimelineResolver(
            fps=self.fps, ref_resolver=ref_resolver).resolve(plan, actor_initial_states)
        if placement_error:
            return False, {'success': False, 'error': placement_error.to_dict()}
        spacing_error = space_standing_actors(
            plan, actor_initial_states, scene_data, self.manifest, self.project_root,
            locked_actors=set(brief.get("tracks", {})),
            crowd_config=self.crowd_config,
            durations={b.beat_id: b.duration for b in placement_timeline.beats},
            timeline_beats=placement_timeline.beats,
        )
        if spacing_error:
            return False, {"success": False, "error": spacing_error.to_dict()}

        from director_solver import fit_contact_windows
        try:
            plan = fit_contact_windows(plan, brief, actor_initial_states, self.manifest)
        except ValueError as exc:
            return False, {"success": False, "error": {"error_code": "BRIEF_TIMING", "message": str(exc)}}

        # 3. Route and time prompt-only meetings before the final timeline pass.
        timeline_resolver = TimelineResolver(fps=self.fps, ref_resolver=ref_resolver)
        from director_solver import schedule_prompt_interactions
        route_synthesizer = MotionSynthesizer(self.manifest, fps=self.fps,
                                             ref_resolver=ref_resolver, scene_data=scene_data)
        stair_error = self._prepare_stair_climbs(plan, actor_initial_states, route_synthesizer)
        if stair_error:
            return False, {"success": False, "error": stair_error.to_dict()}
        envelopes = actor_envelopes(plan, actor_initial_states, self.manifest, self.project_root,
                                    self.crowd_config)
        route_synthesizer.solid_envelopes = envelopes
        from crowd.movements import movement_source_plan, route_movements
        plan = movement_source_plan(plan)
        from crowd_spacing import GROUND_TRAVEL_CLIPS
        movement_index = next((i for i, b in enumerate(plan.beats)
                               if b.function in GROUND_TRAVEL_CLIPS and not b.params.get('brief_path')
                               and not b.params.get('exact_window')), len(plan.beats))
        prefix = copy.deepcopy(plan)
        prefix.beats = prefix.beats[:movement_index]
        prefix, interaction_error = schedule_prompt_interactions(
            prefix, actor_initial_states, timeline_resolver, route_synthesizer, scene_data,
            envelopes=envelopes, crowd_config=self.crowd_config,
            continuation_beats=plan.beats[movement_index:])
        if interaction_error:
            return False, {"success": False, "error": interaction_error.to_dict()}
        plan.beats = prefix.beats + plan.beats[movement_index:]
        # Initial meeting approaches must exist before ordinary movements can
        # know when their participants vacate their standing positions. Then
        # rebuild meetings against the newly reserved ground movements.
        plan, movement_error = route_movements(plan, actor_initial_states, timeline_resolver,
                                               route_synthesizer, scene_data, envelopes, self.crowd_config,
                                               preserve_approaches=True)
        if movement_error:
            return False, {'success': False, 'error': movement_error.to_dict()}
        # Later meetings need scheduling even if every preceding movement is
        # authored/locked and therefore produced no automatic crowd route.
        plan, interaction_error = schedule_prompt_interactions(
            plan, actor_initial_states, timeline_resolver, route_synthesizer, scene_data,
            envelopes=envelopes, crowd_config=self.crowd_config)
        if interaction_error:
            return False, {'success': False, 'error': interaction_error.to_dict()}
        # Linking a one-sided interaction can introduce a reaction partner.
        # Include those generated clips in the final occupancy check too.
        envelopes = actor_envelopes(plan, actor_initial_states, self.manifest, self.project_root,
                                    self.crowd_config)
        timeline, err = timeline_resolver.resolve(plan, actor_initial_states)
        if err or not timeline:
            return False, {
                "success": False,
                "error": err.to_dict() if err else {"message": "Timeline resolution failed"},
            }
        declared_error = validate_interactions(plan, timeline, actor_initial_states)
        if declared_error:
            return False, {"success": False, "error": declared_error.to_dict()}

        # 4. Resolve semantic targets for each beat (initial pass; dynamic re-resolution occurs at beat t_start in MotionSynthesizer)
        resolved_targets: Dict[str, ResolvedTarget] = {}
        actor_states_at_start = copy.deepcopy(actor_initial_states)

        for b in plan.beats:
            target_ref = b.params.get("target") or b.params.get("to")
            if not target_ref and b.function == "climb_stairs":
                target_ref = b.params.get("stairs")
            if target_ref:
                resolve_target = (ref_resolver.resolve_facing if b.function in ('turn_to_face', 'look_at')
                                  else ref_resolver.resolve)
                t_res, ref_err = resolve_target(
                    target_ref, actor_states_at_start, 0.0, source_actor_id=b.actor_id,
                    clearance_radius_m=(envelopes.solid_movement_radius(b.actor_id, b.function, b.beat_id)
                                        if envelopes.knows(b.actor_id) else None)
                )
                if t_res:
                    resolved_targets[b.beat_id] = t_res
                elif b.function not in (
                    "climb_stairs", "climb_surface", "vault_over", "perform_acrobatics",
                    "interact_object", "pull_object", "inspect_area", "kneel",
                    "emote", "perform_activity", "defend_or_block", "take_cover",
                ):
                    return False, {
                        "success": False,
                        "error": StructuredError(
                            error_code="UNRESOLVED_REFERENCE",
                            message=f"Could not resolve target '{target_ref}' for beat '{b.beat_id}': {ref_err}",
                            beat_id=b.beat_id,
                            actor_id=b.actor_id,
                            suggestions=["Use current scene region IDs, valid actors, zone markers, anchors or surfaces."],
                        ).to_dict(),
                    }

        # 5. Motion synthesis
        synthesizer = MotionSynthesizer(
            self.manifest,
            fps=self.fps,
            ref_resolver=ref_resolver,
            scene_data=scene_data,
            solid_envelopes=envelopes,
        )
        actor_performances: Dict[str, ActorPerformance] = {}

        # Synthesize in sequence: vehicles first (threats), then humans (reactions/actions)
        vehicle_ids = [aid for aid, s in actor_initial_states.items() if s["is_vehicle"]]
        human_ids = [aid for aid, s in actor_initial_states.items() if not s["is_vehicle"]]
        try:
            human_ids = self._actor_synthesis_order(
                human_ids, timeline.actor_schedules, actor_initial_states)
        except ValueError as exc:
            return False, {"success": False, "error": StructuredError(
                "ACTOR_TRAJECTORY_DEPENDENCY", str(exc),
                suggestions=["Sequence the movements or use a joint interaction plan."]
            ).to_dict()}

        for aid in vehicle_ids + human_ids:
            if aid == (human_ids[0] if human_ids else None) and vehicle_ids:
                placed_support = self._read_placed_vehicle_support(
                    actor_performances, actor_initial_states, scene_data)
                scene_data["support_surfaces"].extend(placed_support["support_surfaces"])
                scene_data["solid_boxes"].extend(placed_support["solid_boxes"])
            a_beats = timeline.actor_schedules.get(aid, [])
            perf, synth_err = synthesizer.synthesize_actor(
                actor_id=aid,
                initial_state=actor_initial_states[aid],
                beats=a_beats,
                resolved_targets=resolved_targets,
                other_performances=actor_performances,
                total_duration_s=timeline.total_duration_s,
                seed=plan.seed,
                ref_resolver=ref_resolver,
                scene_data=scene_data,
                all_initial_states=actor_initial_states,
            )
            if synth_err or not perf:
                return False, {
                    "success": False,
                    "error": synth_err.to_dict() if synth_err else {"message": f"Synthesis failed for {aid}"},
                }
            actor_performances[aid] = perf

        from director_solver import effective_brief, solve_contacts, solve_shared_actions
        solved_brief = effective_brief(brief, timeline)
        group_errors = solve_shared_actions(solved_brief, actor_performances, synthesizer, scene_data, timeline.fps)
        if group_errors:
            return False, {"success": False, "error": {"error_code": "BRIEF_SHARED_ACTION", "details": group_errors}}
        contact_errors = solve_contacts(solved_brief, timeline, actor_performances, self.manifest, synthesizer, scene_data, actor_initial_states, ref_resolver)
        if contact_errors:
            return False, {"success": False, "error": {"error_code": "BRIEF_CONTACT", "details": contact_errors}}
        conformance_errors = check_conformance(solved_brief, actor_performances)
        if conformance_errors:
            return False, {"success": False, "error": {"error_code": "BRIEF_CONFORMANCE", "details": conformance_errors}}
        from crowd.conformance import check_conformance as check_crowd_conformance
        crowd_errors = (check_crowd_conformance(plan, timeline, actor_performances, envelopes, self.fps,
                                               self.crowd_config) if validate else [])
        if crowd_errors:
            return False, {"success": False, "error": StructuredError(
                'CROWD_CONFORMANCE', 'Synthesized actor roots overlap the planned crowd envelopes.',
                details={'conflicts': crowd_errors}).to_dict()}

        from vehicle_routes import check_vehicle_roots
        vehicle_errors = check_vehicle_roots(actor_performances, synthesizer, timeline) if validate else []
        if vehicle_errors:
            return False, {'success': False, 'error': StructuredError(
                'VEHICLE_ROUTE_CONFORMANCE',
                'Synthesized vehicle motion overlaps another vehicle or leaves supported clear space.',
                details={'conflicts': vehicle_errors}).to_dict()}

        from spatial_intents import validate_spatial_intents
        spatial_error = validate_spatial_intents(plan, timeline, actor_performances, scene_data,
                                                 self.manifest, self.crowd_config)
        if spatial_error:
            return False, {'success': False, 'error': spatial_error.to_dict()}

        # 6. Camera synthesis
        cam_controller = CameraController(fps=self.fps)
        actor_positions_by_id = {
            aid: [(kf.time_s, list(kf.position)) for kf in p.keyframes]
            for aid, p in actor_performances.items()
        }
        is_camera_locked = getattr(plan, "camera_locked", False)
        if is_camera_locked:
            # Camera is locked by user; skip camera synthesis from LLM prompt
            cam_keyframes = cam_controller.synthesize_camera(
                None, actor_positions_by_id, timeline.total_duration_s
            )
        else:
            cam_keyframes = cam_controller.synthesize_camera(
                plan.camera, actor_positions_by_id, timeline.total_duration_s
            )

        # 7. Collect prompt-mandated props & author OpenUSD shot.usda layer
        prompt_str = getattr(plan, "prompt", "") or ""
        required_props = self._collect_required_prompt_props(prompt_str, scene_data, registry)

        shot_usda_text = self._generate_shot_usda(
            timeline=timeline,
            registry=registry,
            actor_performances=actor_performances,
            cam_keyframes=cam_keyframes,
            scene_usda_rel_path=(scene_version_file or "scene.usda") if is_scene_stage
                                else (f"../{scene_version_file}" if scene_version_file else "../scene.usda"),
            scene_edits_rel_path=scene_edits_version_file,
            required_props=required_props,
        )
        # Asset references must also resolve when compiling outside scenes/scene/shot.
        asset_root_rel = os.path.relpath(self.project_root, shot_dir).replace(os.sep, "/")
        shot_usda_text = shot_usda_text.replace("@../../../", "@" + asset_root_rel + "/")
        output_shot_usda_path.write_text(shot_usda_text, encoding="utf-8")
        # An in-process regeneration may still hold the prior composed root layer.
        from pxr import Sdf
        cached_shot = Sdf.Layer.Find(str(output_shot_usda_path))
        if cached_shot:
            cached_shot.Reload()
        placed_actor_paths = {
            f"/World/{registry.get(action.actor_id).label.replace(' ', '_')}"
            for action in plan.setup
            if action.action_type == "place" and registry.get(action.actor_id)
        }
        self._restore_placed_actor_visibility(output_shot_usda_path, placed_actor_paths)

        # 8. Multi-clip baking & automatic skeleton binding for rigged human characters
        character_bake_errors = []
        for aid, perf in actor_performances.items():
            rec = registry.get(aid)
            is_human_char = rec is not None and any(k in rec.asset_id.lower() for k in ("human", "character"))
            if is_human_char and not perf.is_vehicle and perf.actions:
                asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") in (rec.asset_id, "character.human", "humans.xbot")), None)
                char_file = asset_entry.get("path") if asset_entry else "assets/usdc_assets/humans/human.norm.usda"
                char_asset_full = (self.project_root / char_file).resolve()
                if not char_asset_full.exists():
                    char_asset_full = self.project_root / "assets/usdc_assets/humans/human.usdc"

                # Compute relative yaw/pitch for look_at_targets so bake_animation can apply gaze IK
                enriched_look_ats: List[Dict[str, Any]] = []
                for l_tgt in getattr(perf, "look_at_targets", []):
                    t_mid = 0.5 * (float(l_tgt.get("start_s", 0.0)) + float(l_tgt.get("end_s", 0.0)))
                    a_pos, a_head = perf.sample(t_mid)
                    t_pos = l_tgt.get("target_pos", [a_pos[0], a_pos[1], a_pos[2]])
                    dx = float(t_pos[0]) - float(a_pos[0])
                    dy = float(t_pos[1]) - float(a_pos[1])
                    dz = float(t_pos[2]) - float(a_pos[2])
                    horiz_d = math.hypot(dx, dz)
                    if horiz_d > 0.05:
                        world_yaw = math.degrees(math.atan2(dx, -dz)) % 360.0
                        rel_yaw = (world_yaw - a_head + 180.0) % 360.0 - 180.0
                        rel_pitch = math.degrees(math.atan2(dy, horiz_d))
                    else:
                        rel_yaw = 0.0
                        rel_pitch = 0.0
                    entry_copy = dict(l_tgt)
                    entry_copy["yaw_deg"] = round(rel_yaw, 2)
                    entry_copy["pitch_deg"] = round(rel_pitch, 2)
                    enriched_look_ats.append(entry_copy)

                # Save actions_<aid>.json
                actions_payload: Dict[str, Any] = {
                    "fps": self.fps,
                    "actions": perf.actions,
                    "exact_windows": bool(brief.get("tracks", {}).get(aid)),
                }
                if getattr(perf, "overlay_actions", None):
                    actions_payload["overlay_actions"] = perf.overlay_actions
                if enriched_look_ats:
                    actions_payload["look_at_targets"] = enriched_look_ats

                actions_path = shot_dir / f"actions_{aid}.json"
                actions_path.write_text(json.dumps(actions_payload, indent=2) + "\n", encoding="utf-8")
                anim_output = shot_dir / f"anim_{aid}.usda"

                # Run bake and bind
                try:
                    from bake_animation import bake
                    from bind_baked_animation import bind
                    bake(char_asset_full, self.manifest_path, actions_path, anim_output, self.fps, None)
                    prim_name = rec.label.replace(" ", "_")
                    bind(output_shot_usda_path, f"/World/{prim_name}", anim_output)
                except Exception as exc:
                    character_bake_errors.append(f"Failed to bake/bind character {aid}: {exc}")

        if brief.get("paths"):
            from director_brief import author_path_guides
            from pxr import Usd
            guide_stage = Usd.Stage.Open(str(output_shot_usda_path))
            author_path_guides(guide_stage, brief)
            guide_stage.GetRootLayer().Save()

        # Validate the same editor composition that will be published.
        # Removing stale overrides after validation could expose unchecked motion.
        if not character_bake_errors:
            self._release_animated_actor_overrides(output_shot_usda_path)

        if not character_bake_errors and brief.get("tracks"):
            from director_solver import check_baked_conformance
            baked_errors = check_baked_conformance(solved_brief, output_shot_usda_path, actor_performances, self.fps)
            if baked_errors:
                return False, {"success": False, "error": {"error_code": "BAKED_CONFORMANCE", "details": baked_errors}}

        # 9. Write sidecars: shot_plan.json, resolved_timeline.json, allow_collisions.json, shot.json
        plan_path = shot_dir / "shot_plan.json"
        plan_path.write_text(plan.to_json(), encoding="utf-8")

        timeline_path = shot_dir / "resolved_timeline.json"
        timeline_path.write_text(json.dumps(timeline.to_dict(), indent=2), encoding="utf-8")

        # Aggregate allowed contacts across all actors
        allowed_contacts = []
        scene_building_assets = []
        support_owners = {str(surface.get("owner", "")) for surface in scene_data.get("support_surfaces", [])
                          if surface.get("owner")}
        for obj in scene_data.get("objects", []):
            asset_str = str(obj.get("asset", ""))
            name_str = str(obj.get("name", ""))
            if (name_str in support_owners
                    or asset_str.startswith(("buildings.", "environment."))
                    or "club" in name_str.lower()
                    or "building" in asset_str.lower()):
                for ref_key in (obj.get("asset"), obj.get("id"), obj.get("name")):
                    if ref_key and ref_key not in scene_building_assets:
                        scene_building_assets.append(ref_key)
        for support_owner in support_owners:
            if support_owner in actor_initial_states:
                for ref_key in (support_owner, actor_initial_states[support_owner].get("asset_id")):
                    if ref_key and ref_key not in scene_building_assets:
                        scene_building_assets.append(ref_key)

        for aid, perf in actor_performances.items():
            rec = registry.get(aid)
            label = rec.label.replace(" ", "_") if rec else aid
            actor_names = set([aid, label, label.replace("Human_", "")])
            for name in list(actor_names):
                actor_names.add(f"Human_{name}")

            # Level integrity accepts an authored airborne action only during
            # its scheduled interval; unrelated vertical motion still fails.
            for beat in timeline.actor_schedules.get(aid, []):
                if beat.function in {"jump_down", "fly_to", "climb_surface"}:
                    for a_name in actor_names:
                        allowed_contacts.append({"actor": a_name,
                                                 "vertical_transition": True,
                                                 "t0": beat.start_time,
                                                 "t1": beat.end_time})

            # Allow foot/sole contact with building floor meshes in the scene
            for b_asset in scene_building_assets:
                for a_name in actor_names:
                    allowed_contacts.append({
                        "actor": a_name,
                        "asset": b_asset,
                        "joints": ["Foot", "Toe", "Leg"],
                        "max_depth_m": 0.25,
                    })

            for c in perf.allowed_contacts:
                target_asset = c.get("asset")
                target_rec = registry.get(target_asset) if target_asset else None
                possible_assets = [target_asset]
                if target_rec:
                    # Instance aliases identify the intended counterpart. An asset
                    # ID would exempt every other copy of that character too.
                    possible_assets.extend([target_rec.actor_id, target_rec.label, target_rec.label.replace(" ", "_")])

                for a_name in actor_names:
                    seen_targets = set()
                    for tgt in possible_assets:
                        if tgt is not None and tgt in seen_targets:
                            continue
                        if tgt is not None:
                            seen_targets.add(tgt)
                        rule = dict(c)
                        rule["actor"] = a_name
                        if tgt is not None:
                            rule["asset"] = tgt
                        allowed_contacts.append(rule)

                        # Mutual contact between characters in close combat
                        if target_rec and tgt in (target_rec.actor_id, target_rec.label, target_rec.label.replace(" ", "_")):
                            for mutual_actor in [target_rec.actor_id, target_rec.label.replace(" ", "_")]:
                                mutual_rule = dict(c)
                                mutual_rule["actor"] = mutual_actor
                                mutual_rule["asset"] = a_name
                                allowed_contacts.append(mutual_rule)
        allow_path = shot_dir / "allow_collisions.json"
        allow_path.write_text(json.dumps(allowed_contacts, indent=2), encoding="utf-8")

        shot_meta = {
            "prompt": plan.prompt,
            "version": "1.0",
            "scene_version": scene_version_file or "scene.usda",
            "fps": self.fps,
            "duration_s": timeline.total_duration_s,
            "end_frame": int(round(timeline.total_duration_s * self.fps)),
            "camera_prim": "/World/ShotCam",
            "plan_hash": hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()[:16],
            "actors": registry.list_actors(),
        }
        (shot_dir / "shot.json").write_text(json.dumps(shot_meta, indent=2), encoding="utf-8")

        # 10. Run validators: physical lint and collision validator
        validation_report = {
            'plan_review': plan_review,
            'spatial_intents': [intent.to_dict() for intent in plan.spatial_intents],
            "success": len(character_bake_errors) == 0,
            "lint_passed": True,
            "collision_passed": None,
            "collision_skipped": not validate,
            "camera_passed": True,
            "bake_errors": character_bake_errors,
            "lint_warnings": [],
            "collision_report": {},
            "camera_warnings": [],
        }

        # 10a. Shot lint
        linter = ShotLinter()
        gait_stride_speeds = {
            "walk": 1.4,
            "run": 4.2,
            "crouched_walking": 0.9,
            "crawling": 0.55,
            "crawling_backwards": 0.5,
            "injured_walking": 0.85,
            "drunk_walk": 0.9,
            "sad_walk_arc_right": 1.0,
            "backward_walking_turn": 1.0,
            "slow_jog_backwards": 1.6,
            "run_backward": 2.6,
            "walking_with_shopping_bag": 1.25,
            "jogging_with_box": 2.0,
            "talking_phone_pacing": 1.1,
            "swimming": 1.2,
            "superhero_flying": 5.5,
            "floating": 2.0,
            "walking_up_the_stairs": 0.65,
            "running_up_stairs": 2.0,
            "descending_stairs": 0.65,
            "descending_stairs_fast": 0.9,
        }
        active_actor_ids = [
            aid for aid in actor_performances.keys()
            if registry.get(aid) and any(k in registry.get(aid).asset_id.lower() for k in ("human", "character", "car", "vehicle"))
        ]
        for aid in active_actor_ids:
            perf = actor_performances[aid]
            ok, warnings = linter.lint_performance(perf, gait_stride_speeds)
            if not ok:
                validation_report["lint_warnings"].extend(warnings)
                validation_report["lint_passed"] = False

        # 10b. Frustum check
        eval_cam_keyframes = cam_keyframes
        user_edits_path = shot_dir / "user_edits.usda"
        if user_edits_path.exists():
            try:
                from pxr import Usd, Sdf
                edits_stage = Usd.Stage.Open(str(user_edits_path))
                if edits_stage and edits_stage.GetPrimAtPath(Sdf.Path("/World/ShotCam")).IsValid():
                    from camera_rig import read_rig_from_stage, CameraRigEvaluator
                    rig = read_rig_from_stage(edits_stage)
                    if rig:
                        rig_eval = CameraRigEvaluator(fps=self.fps)
                        actor_positions_by_t = {
                            aid: [(kf.time_s, list(kf.position)) for kf in p.keyframes]
                            for aid, p in actor_performances.items()
                        }
                        eval_cam_keyframes = rig_eval.evaluate_sequence(
                            rig=rig,
                            start_s=0.0,
                            end_s=timeline.total_duration_s,
                            actor_positions_over_time=actor_positions_by_t,
                        )
            except Exception:
                pass

        cam_ok, cam_warnings = cam_controller.validate_frustum(
            eval_cam_keyframes, actor_positions_by_id, active_actor_ids
        )
        validation_report["camera_passed"] = cam_ok
        validation_report["camera_warnings"] = cam_warnings

        # 10c. Collision validator (validate_collisions.py)
        if validate:
            col_ok, col_data = self._run_collision_validator(output_shot_usda_path, allow_path)
            validation_report["collision_passed"] = col_ok
            validation_report["collision_report"] = col_data
            if not col_ok:
                validation_report["success"] = False

        # 10d. Prompt adherence check
        prompt_res = self._verify_prompt_adherence(
            plan.prompt, output_shot_usda_path, registry, timeline, cam_keyframes
        )
        validation_report["prompt_adherence"] = prompt_res
        if not prompt_res["passed"] and not brief.get("tracks"):
            validation_report["success"] = False

        (shot_dir / "validation_report.json").write_text(json.dumps(validation_report, indent=2), encoding="utf-8")

        if validation_report["success"]:
            from camera_workflow import bake_manual_cameras, save_fingerprints
            if (shot_dir / "camera_rig.json").exists():
                try:
                    bake_manual_cameras(shot_dir, require_current=False)
                except (ValueError, KeyError, TypeError) as exc:
                    # Camera lint never invalidates body motion.
                    validation_report["camera_warnings"].append(str(exc))
            save_fingerprints(shot_dir)

        (shot_dir / "validation_report.json").write_text(json.dumps(validation_report, indent=2), encoding="utf-8")
        return validation_report["success"], {
            "success": validation_report["success"],
            "artifacts": {
                "shot_usda": str(output_shot_usda_path),
                "shot_plan": str(plan_path),
                "resolved_timeline": str(timeline_path),
                "shot_json": str(shot_dir / "shot.json"),
                "validation_report": str(shot_dir / "validation_report.json"),
            },
            "validation": validation_report,
            "timeline": timeline.to_dict(),
        }

    def _generate_shot_usda(
        self,
        timeline: ResolvedTimeline,
        registry: ActorRegistry,
        actor_performances: Dict[str, ActorPerformance],
        cam_keyframes: List[CameraKeyframe],
        scene_usda_rel_path: str = "../scene.usda",
        scene_edits_rel_path: Optional[str] = None,
        required_props: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        end_frame = int(round(timeline.total_duration_s * self.fps))
        lines = [
            "#usda 1.0",
            "(",
            '    defaultPrim = "World"',
            '    upAxis = "Y"',
            "    metersPerUnit = 1",
            f"    timeCodesPerSecond = {self.fps}",
            "    startTimeCode = 0",
            f"    endTimeCode = {end_frame}",
            "    subLayers = [ " + ", ".join(
                f"@{path}@" for path in ([scene_edits_rel_path] if scene_edits_rel_path else [])
                + [scene_usda_rel_path]) + " ]",
            ")",
            "",
            'def "World"',
            "{",
        ]

        # Authored Camera prim
        lines.extend([
            '    def Camera "ShotCam"',
            "    {",
            "        double3 xformOp:translate.timeSamples = {",
        ])
        t_samples = []
        r_samples = []
        for kf in cam_keyframes:
            f = round(kf.time_s * self.fps, 6)
            t_samples.append(f"            {f}: ({kf.position[0]:.3f}, {kf.position[1]:.3f}, {kf.position[2]:.3f})")
            r_samples.append(f"            {f}: ({kf.rotation_xyz[0]:.2f}, {kf.rotation_xyz[1]:.2f}, {kf.rotation_xyz[2]:.2f})")
        lines.append(",\n".join(t_samples))
        lines.extend([
            "        }",
            "        float3 xformOp:rotateXYZ.timeSamples = {",
        ])
        lines.append(",\n".join(r_samples))
        lines.extend([
            "        }",
            '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]',
            f"        float focalLength = {cam_keyframes[0].focal_length if cam_keyframes else 35.0}",
            "        float horizontalAperture = 36",
            "        float verticalAperture = 20.25",
            "        float2 clippingRange = (0.1, 1000)",
            "    }",
            "",
        ])

        # Collect door beats
        door_beats = [b for b in timeline.beats if b.function in ("open_part", "close_part", "open_door", "close_door", "exit_vehicle") or "door" in b.params.get("part", "")]
        handled_door_beats = set()

        # Authored Actors
        for aid, perf in actor_performances.items():
            rec = registry.get(aid)
            if not rec:
                continue

            # Determine reference asset path dynamically from manifest
            asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") == rec.asset_id), None)
            if not asset_entry:
                asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") in (rec.asset_id, "character.human", "humans.xbot", "cars.car1")), None)

            if asset_entry and asset_entry.get("path"):
                ref_path = f"../../../{asset_entry['path']}"
                prim_target = asset_entry.get("default_prim", "Car" if perf.is_vehicle else "root")
            elif perf.is_vehicle:
                ref_path = "../../../assets/usdc_assets/cars/car1.usdc"
                prim_target = "Car"
            else:
                ref_path = "../../../assets/usdc_assets/humans/human.norm.usda"
                prim_target = "root"

            prim_name = rec.label.replace(" ", "_")
            cdata = registry.to_stage_custom_data(aid)

            lines.extend([
                f'    def Xform "{prim_name}" (',
                "        active = true",
                "        customData = {",
                f'            string actor_id = "{rec.actor_id}"',
                f'            string label = "{rec.label}"',
                f'            string asset_id = "{rec.asset_id}"',
                f'            string appearance_variant = "{rec.appearance.color_variant}"',
                f"            float height_scale = {rec.appearance.height_scale:.3f}",
                f"            float3 tint_rgb = ({rec.appearance.tint_rgb[0]:.3f}, {rec.appearance.tint_rgb[1]:.3f}, {rec.appearance.tint_rgb[2]:.3f})",
                f'            string forward_axis = "-Z"',
                "        }",
                f"        references = @{ref_path}@</{prim_target}>",
                "    )",
                "    {",
                "        double3 xformOp:translate.timeSamples = {",
            ])

            trans_lines = []
            rot_lines = []
            pitch_lines = []
            has_pitch = any(abs(kf.pitch_deg) > 0.1 for kf in perf.keyframes)
            for kf in perf.keyframes:
                f = round(kf.time_s * self.fps, 6)
                trans_lines.append(f"            {f}: ({kf.position[0]:.3f}, {kf.position[1]:.3f}, {kf.position[2]:.3f})")
                rot_y = (-kf.heading_deg) % 360.0
                rot_lines.append(f"            {f}: {rot_y:.2f}")
                if has_pitch:
                    pitch_lines.append(f"            {f}: {-kf.pitch_deg:.2f}")

            lines.append(",\n".join(trans_lines))
            lines.extend([
                "        }",
                "        float xformOp:rotateY.timeSamples = {",
            ])
            lines.append(",\n".join(rot_lines))
            lines.append("        }")

            if has_pitch:
                lines.extend([
                    "        float xformOp:rotateX.timeSamples = {",
                ])
                lines.append(",\n".join(pitch_lines))
                lines.append("        }")

            ops = ["xformOp:translate", "xformOp:rotateY"]
            if has_pitch:
                ops.append("xformOp:rotateX")

            # Apply height scale if variant specifies different height
            if abs(rec.appearance.height_scale - 1.0) > 1e-3:
                hs = rec.appearance.height_scale
                ops.append("xformOp:scale")
                lines.append(f"        float3 xformOp:scale = ({hs:.3f}, {hs:.3f}, {hs:.3f})")

            ops_tokens = ", ".join(f'"{op}"' for op in ops)
            lines.append(f"        uniform token[] xformOpOrder = [{ops_tokens}]")

            # Tint character clothing or vehicle displayColor
            lines.extend([
                '        token visibility = "inherited"',
                f"        color3f[] primvars:displayColor = [({rec.appearance.tint_rgb[0]:.3f}, {rec.appearance.tint_rgb[1]:.3f}, {rec.appearance.tint_rgb[2]:.3f})] (",
                '            interpolation = "constant"',
                "        )",
            ])

            # Check for authored car door / part animations for this actor
            car_door_beats = []
            for db in door_beats:
                part_str = db.params.get("part") or db.params.get("part_reference", "Car1:door_fl")
                car_obj = part_str.split(":")[0] if ":" in part_str else "Car1"
                if car_obj in (aid, rec.label, prim_name):
                    handled_door_beats.add(db.beat_id)
                    car_door_beats.append((part_str, db))
            if car_door_beats:
                lines.extend(self._build_grouped_part_animations_usda(rec, car_door_beats, end_frame, indent=2))

            lines.extend([
                "    }",
                "",
            ])

        # Check for authored car door animations for scene-level objects (not defined in this layer)
        unhandled_by_car: Dict[str, List[Tuple[str, ResolvedBeat]]] = {}
        for db in door_beats:
            if db.beat_id in handled_door_beats:
                continue
            part_str = db.params.get("part") or db.params.get("part_reference", "Car1:door_fl")
            car_obj = part_str.split(":")[0] if ":" in part_str else "Car1"
            unhandled_by_car.setdefault(car_obj, []).append((part_str, db))

        for car_obj, p_list in unhandled_by_car.items():
            rec = registry.get(car_obj)
            prim_car_name = rec.label.replace(" ", "_") if rec else car_obj
            lines.extend([
                f'    over "{prim_car_name}"',
                "    {",
            ])
            lines.extend(self._build_grouped_part_animations_usda(rec, p_list, end_frame, indent=2))
            lines.extend([
                "    }",
                "",
            ])

        # Authored prompt-mandated props (if missing from sublayers and not already authored as actors)
        if required_props:
            existing_prim_names = {rec.label.replace(" ", "_") for rec in registry._actors.values()}
            for prop in required_props:
                p_name = prop["name"]
                if p_name in existing_prim_names:
                    continue
                ref_p = prop["ref_path"]
                tgt = f"</{prop['target_prim']}>" if prop.get("target_prim") else ""
                pos = prop.get("position", [0.0, 0.0, 0.0])
                rot = prop.get("rotation", [0.0, 0.0, 0.0])
                lines.extend([
                    f'    def Xform "{p_name}" (',
                    "        customData = {",
                    f'            string asset_id = "{prop["asset_id"]}"',
                    '            string forward_axis = "-Z"',
                    '            string world_state_role = "object"',
                    "        }",
                    f"        prepend references = @{ref_p}@{tgt}",
                    "    )",
                    "    {",
                    f"        float3 xformOp:rotateXYZ = ({rot[0]}, {rot[1]}, {rot[2]})",
                    f"        double3 xformOp:translate = ({pos[0]}, {pos[1]}, {pos[2]})",
                    '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]',
                    "    }",
                    "",
                ])

        lines.append("}\n")
        return "\n".join(lines)

    def _build_grouped_part_animations_usda(
        self,
        car_actor_rec: Optional[ActorRecord],
        part_beats: List[Tuple[str, ResolvedBeat]],
        end_frame: int,
        indent: int = 2,
    ) -> List[str]:
        """Group multiple vehicle part animations by parent prim token to prevent duplicate `over "Doors"` blocks."""
        grouped: Dict[str, List[Tuple[List[str], str, float, int, int]]] = {}
        asset_entry = None
        if car_actor_rec:
            asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") == car_actor_rec.asset_id), None)
        if not asset_entry:
            asset_entry = next((a for a in self.manifest.get("assets", []) if "car" in a.get("tags", [])), None)

        seen_parts = set()
        for part_str, db in part_beats:
            part_name = part_str.split(":")[1] if ":" in part_str else "door_fl"
            if part_name in seen_parts:
                continue
            seen_parts.add(part_name)
            p_info = None
            if asset_entry and "parts" in asset_entry:
                p_info = asset_entry["parts"].get(part_name)
                if not p_info:
                    for k, v in asset_entry["parts"].items():
                        if part_name in k or k in part_name:
                            p_info = v
                            break
            prim_path = p_info.get("prim", "Doors/Door_FL") if p_info else ("Doors/Door_FL" if "fl" in part_name.lower() else "Doors/Door_FR")
            axis = p_info.get("axis", "Y") if p_info else "Y"
            max_deg = float(p_info.get("max_deg", 65.0)) if p_info else 65.0
            if p_info and "open_sign" in p_info:
                open_sign = float(p_info["open_sign"])
            else:
                is_left = any(s in part_name.lower() for s in ("fl", "rl", "left", "_l"))
                is_rear_hinged = bool(p_info and p_info.get("hinge") and p_info["hinge"][2] > 0.0)
                open_sign = (1.0 if is_rear_hinged else -1.0) if is_left else (-1.0 if is_rear_hinged else 1.0)

            start_f = int(round(db.start_time * self.fps))
            end_f = int(round(db.end_time * self.fps))
            is_open = ("open" in db.function or "exit" in db.function)
            swing_angle = (open_sign * max_deg) if is_open else 0.0

            tokens = [t for t in prim_path.split("/") if t] or ["Doors", "Door_FL"]
            top_tok = tokens[0]
            grouped.setdefault(top_tok, []).append((tokens[1:], axis, swing_angle, start_f, end_f))

        out: List[str] = []
        base_ind = "    " * indent
        for top_tok, items in grouped.items():
            out.append(f'{base_ind}over "{top_tok}"')
            out.append(f"{base_ind}{{")
            for sub_tokens, axis, swing_angle, start_f, end_f in items:
                for i, tok in enumerate(sub_tokens):
                    c_ind = "    " * (indent + 1 + i)
                    out.append(f'{c_ind}over "{tok}"')
                    out.append(f"{c_ind}{{")
                leaf_ind = "    " * (indent + 1 + len(sub_tokens))
                out.extend([
                    f"{leaf_ind}float xformOp:rotate{axis}.timeSamples = {{",
                    f"{leaf_ind}    0: 0.0,",
                    f"{leaf_ind}    {start_f}: 0.0,",
                    f"{leaf_ind}    {end_f}: {swing_angle:.1f},",
                    f"{leaf_ind}    {end_frame}: {swing_angle:.1f}",
                    f"{leaf_ind}}}",
                ])
                for i in range(len(sub_tokens) - 1, -1, -1):
                    c_ind = "    " * (indent + 1 + i)
                    out.append(f"{c_ind}}}")
            out.append(f"{base_ind}}}")
        return out

    def _build_part_animation_usda(
        self,
        car_actor_rec: Optional[ActorRecord],
        part_str: str,
        db: ResolvedBeat,
        end_frame: int,
        indent: int = 2,
    ) -> List[str]:
        """Generates nested USDA over blocks and rotation timeSamples for any part on any vehicle."""
        part_name = part_str.split(":")[1] if ":" in part_str else "door_fl"
        asset_entry = None
        if car_actor_rec:
            asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") == car_actor_rec.asset_id), None)
        if not asset_entry:
            asset_entry = next((a for a in self.manifest.get("assets", []) if "car" in a.get("tags", [])), None)

        p_info = None
        if asset_entry and "parts" in asset_entry:
            p_info = asset_entry["parts"].get(part_name)
            if not p_info:
                for k, v in asset_entry["parts"].items():
                    if part_name in k or k in part_name:
                        p_info = v
                        break

        prim_path = p_info.get("prim", "Doors/Door_FL") if p_info else ("Doors/Door_FL" if "fl" in part_name.lower() else "Doors/Door_FR")
        axis = p_info.get("axis", "Y") if p_info else "Y"
        max_deg = float(p_info.get("max_deg", 65.0)) if p_info else 65.0
        if p_info and "open_sign" in p_info:
            open_sign = float(p_info["open_sign"])
        else:
            is_left = any(s in part_name.lower() for s in ("fl", "rl", "left", "_l"))
            is_rear_hinged = bool(p_info and p_info.get("hinge") and p_info["hinge"][2] > 0.0)
            if is_left:
                open_sign = 1.0 if is_rear_hinged else -1.0
            else:
                open_sign = -1.0 if is_rear_hinged else 1.0

        start_f = int(round(db.start_time * self.fps))
        end_f = int(round(db.end_time * self.fps))
        is_open = ("open" in db.function or "exit" in db.function)
        swing_angle = (open_sign * max_deg) if is_open else 0.0

        tokens = [t for t in prim_path.split("/") if t]
        if not tokens:
            tokens = ["Doors", "Door_FL"]

        out = []
        for i, token in enumerate(tokens):
            cur_indent = "    " * (indent + i)
            out.append(f'{cur_indent}over "{token}"')
            out.append(f"{cur_indent}{{")

        leaf_indent = "    " * (indent + len(tokens))
        out.extend([
            f"{leaf_indent}float xformOp:rotate{axis}.timeSamples = {{",
            f"{leaf_indent}    0: 0.0,",
            f"{leaf_indent}    {start_f}: 0.0,",
            f"{leaf_indent}    {end_f}: {swing_angle:.1f},",
            f"{leaf_indent}    {end_frame}: {swing_angle:.1f}",
            f"{leaf_indent}}}",
        ])

        for i in range(len(tokens) - 1, -1, -1):
            cur_indent = "    " * (indent + i)
            out.append(f"{cur_indent}}}")

        return out

    def _read_scene_context(self, scene_usda_path: Optional[Path], shot_dir: Optional[Path] = None,
                            stage_override=None) -> Dict[str, Any]:
        """Extract the scene as seen in the editor, including shot-level manual edits."""
        data = {"zones": [], "objects": [], "support_surfaces": [], "standing_spots": [],
                "surface_spots": [], "rooftop_spots": [],
                "solid_boxes": [], "unprotected_scene_objects": []}
        if stage_override is None and (scene_usda_path is None or not scene_usda_path.exists()):
            return data
        try:
            try:
                from pxr import Gf, Usd, UsdGeom
            except ImportError:
                venv_site = self.project_root / ".venv" / "lib" / "python3.12" / "site-packages"
                if venv_site.exists() and str(venv_site) not in sys.path:
                    sys.path.insert(0, str(venv_site))
                from pxr import Gf, Usd, UsdGeom
            edits_path = shot_dir / "user_edits.usda" if shot_dir else None
            if stage_override is not None:
                stage = stage_override
            elif edits_path and edits_path.exists():
                from pxr import Sdf
                root_layer = Sdf.Layer.FindOrOpen(str(scene_usda_path))
                edits_layer = Sdf.Layer.FindOrOpen(str(edits_path))
                stage = Usd.Stage.Open(root_layer, edits_layer) if root_layer and edits_layer else None
            else:
                stage = Usd.Stage.Open(str(scene_usda_path))
            if not stage:
                return data
            from plan_path import discover_step_stairs
            from support_surfaces import (ACTOR_SUPPORT_MARGIN_M,
                                          MAX_SUPPORT_FACE_HEIGHT_VARIATION_M,
                                          STANDING_BODY_HEIGHT_M, STANDING_MAX_STEP_M,
                                          derive_standing_spots, derive_surface_spots,
                                          polygon_area)
            xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
            solid_boxes = []
            meshed_owners = set()
            object_prims = {}
            for prim in stage.Traverse():
                p_path = str(prim.GetPath())
                if prim.IsA(UsdGeom.Camera) or any(p_path == root or p_path.startswith(root+"/") for root in ("/World/Cameras", "/World/CameraRig", "/World/CameraPaths", "/World/Paths", "/World/Paint")):
                    continue
                if "Zones" in p_path and prim.IsA(UsdGeom.Cube):
                    half = float(UsdGeom.Cube(prim).GetSizeAttr().Get() or 2.0) * 0.5
                    matrix = xform_cache.GetLocalToWorldTransform(prim)
                    corners = [matrix.Transform(Gf.Vec3d(x, y, z))
                               for x in (-half, half) for y in (-half, half) for z in (-half, half)]
                    footprint = [matrix.Transform(Gf.Vec3d(x, 0, z))
                                 for x, z in ((-half, -half), (half, -half),
                                              (half, half), (-half, half))]
                    forward = matrix.TransformDir(Gf.Vec3d(0, 0, -1))
                    span_x = max(float(point[0]) for point in corners) - min(float(point[0]) for point in corners)
                    span_z = max(float(point[2]) for point in corners) - min(float(point[2]) for point in corners)
                    markers = ["center"]
                    if span_z >= span_x:
                        markers.extend(("north", "south"))
                    if span_x >= span_z:
                        markers.extend(("east", "west"))
                    data["zones"].append({
                        "id": prim.GetName(),
                        "min": [min(float(point[axis]) for point in corners) for axis in range(3)],
                        "max": [max(float(point[axis]) for point in corners) for axis in range(3)],
                        "footprint_xz": [[float(point[0]), float(point[2])] for point in footprint],
                        "reference_markers": markers,
                        "heading_deg": math.degrees(math.atan2(forward[0], -forward[2])) % 360.0,
                    })
                elif prim.GetParent() and prim.GetParent().GetName() == "World" and prim.GetName() != "Zones" and not prim.GetTypeName().endswith("Light"):
                    world_matrix = xform_cache.GetLocalToWorldTransform(prim)
                    origin = world_matrix.Transform(Gf.Vec3d(0, 0, 0))
                    forward = world_matrix.TransformDir(Gf.Vec3d(0, 0, -1))
                    pos = [float(origin[axis]) for axis in range(3)]
                    rot_y = math.degrees(math.atan2(forward[0], -forward[2])) % 360.0
                    asset_id = prim.GetCustomDataByKey("asset_id") if prim.HasCustomDataKey("asset_id") else prim.GetName()
                    data["objects"].append({
                        "id": prim.GetName(),
                        "name": prim.GetName(),
                        "asset": asset_id,
                        "position": pos,
                        "heading_deg": rot_y,
                        "world_transform": [[float(world_matrix[row][col]) for col in range(4)]
                                            for row in range(4)],
                        "stairs": discover_step_stairs(prim),
                    })
                    object_prims[prim.GetName()] = prim
                    # Convex top faces of scene boxes are exact standing support
                    # candidates. Keep their world geometry so elevated meetings
                    # can be checked without guessing from a zone's X/Z position.
                    for child in Usd.PrimRange(prim):
                        if child.IsA(UsdGeom.Mesh):
                            meshed_owners.add(prim.GetName())
                        if not child.IsA(UsdGeom.Cube):
                            continue
                        half = float(UsdGeom.Cube(child).GetSizeAttr().Get() or 2.0) * 0.5
                        matrix = xform_cache.GetLocalToWorldTransform(child)
                        box_corners = [matrix.Transform(Gf.Vec3d(x, y, z))
                                       for x in (-half, half) for y in (-half, half)
                                       for z in (-half, half)]
                        solid_boxes.append({
                            "owner": prim.GetName(),
                            "min": [min(float(point[axis]) for point in box_corners) for axis in range(3)],
                            "max": [max(float(point[axis]) for point in box_corners) for axis in range(3)],
                        })
                        corners = [matrix.Transform(Gf.Vec3d(x, half, z))
                                   for x, z in ((-half, -half), (half, -half),
                                                (half, half), (-half, half))]
                        heights = [float(point[1]) for point in corners]
                        if max(heights) - min(heights) > MAX_SUPPORT_FACE_HEIGHT_VARIATION_M:
                            continue
                        polygon = [[float(point[0]), float(point[2])] for point in corners]
                        if polygon_area(polygon) < 4 * ACTOR_SUPPORT_MARGIN_M ** 2:
                            continue
                        data["support_surfaces"].append({
                            "owner": prim.GetName(),
                            "prim_path": str(child.GetPath()),
                            "height_m": sum(heights) / len(heights),
                            "polygon_xz": polygon,
                        })
            data['solid_boxes'] = solid_boxes
            from colliders import SceneObject, apply, world_groups
            assets = {asset.get("id"): asset for asset in self.manifest.get("assets", [])}
            boxed_owners = {box["owner"] for box in solid_boxes}
            for obj in data["objects"]:
                asset = assets.get(obj.get("asset"), {})
                if ((obj["name"] in meshed_owners or obj["name"] not in boxed_owners)
                        and not asset.get("collision", {}).get("shapes")):
                    data["unprotected_scene_objects"].append(obj["name"])
                # Use the validator's transforms and shape interpretation. A
                # decorative cube is not complete coverage of a mixed asset,
                # and articulated parts remain obstacles in their live pose.
                prim = object_prims[obj["name"]]
                collider_object = SceneObject(prim, asset, "asset")
                groups = world_groups(stage, collider_object, Usd.TimeCode.Default(), xform_cache)
                box_shapes = {(shape.get("part"), shape["id"])
                              for shape in asset.get("collision", {}).get("shapes", [])
                              if shape.get("type", "box") == "box"}
                for group in groups:
                    for index, corners in enumerate(group.corners_world()):
                        solid_boxes.append({
                            "owner": obj["name"],
                            "prim_path": str(prim.GetPath()),
                            "shape_id": group.ids[index],
                            "part": group.part,
                            "min": corners.min(axis=0).tolist(),
                            "max": corners.max(axis=0).tolist(),
                        })
                        # Thin collider tops can supply support even without a
                        # mesh. Use the same live part transform as the solid.
                        lo = group.c[index] - group.h[index]
                        hi = group.c[index] + group.h[index]
                        if ((group.part, group.ids[index]) not in box_shapes
                                or hi[1] - lo[1] > STANDING_MAX_STEP_M):
                            continue
                        top = apply(group.T, [[x, hi[1], z]
                            for x, z in ((lo[0], lo[2]), (hi[0], lo[2]),
                                         (hi[0], hi[2]), (lo[0], hi[2]))])
                        heights = [float(point[1]) for point in top]
                        polygon = [[float(point[0]), float(point[2])] for point in top]
                        if (max(heights) - min(heights) <= MAX_SUPPORT_FACE_HEIGHT_VARIATION_M
                                and polygon_area(polygon) >= 4 * ACTOR_SUPPORT_MARGIN_M ** 2):
                            data["support_surfaces"].append({
                                "owner": obj["name"],
                                "prim_path": f"{prim.GetPath()}/{group.ids[index]}",
                                "height_m": sum(heights) / len(heights),
                                "polygon_xz": polygon,
                            })
            for obj in data["objects"]:
                tall_boxes = [
                    box for box in solid_boxes
                    if box.get("owner") == obj["name"]
                    and (float(box["max"][1]) - float(box["min"][1])) > STANDING_MAX_STEP_M
                ]
                if tall_boxes:
                    obj["obstacle_bounds_xz"] = [
                        round(min(float(box["min"][0]) for box in tall_boxes), 3),
                        round(min(float(box["min"][2]) for box in tall_boxes), 3),
                        round(max(float(box["max"][0]) for box in tall_boxes), 3),
                        round(max(float(box["max"][2]) for box in tall_boxes), 3),
                    ]
                    obj["obstacle_box_count"] = len(tall_boxes)
                else:
                    obj["obstacle_bounds_xz"] = None
                    obj["obstacle_box_count"] = 0
            mesh_faces = []
            for owner, prim in object_prims.items():
                asset_id = next((obj.get("asset") for obj in data["objects"]
                                 if obj["name"] == owner), None)
                if {"character", "human"}.intersection(assets.get(asset_id, {}).get("tags", [])):
                    continue
                for child in Usd.PrimRange(prim):
                    if not child.IsA(UsdGeom.Mesh):
                        continue
                    mesh = UsdGeom.Mesh(child)
                    points = mesh.GetPointsAttr().Get() or []
                    counts = mesh.GetFaceVertexCountsAttr().Get() or []
                    indices = mesh.GetFaceVertexIndicesAttr().Get() or []
                    if not points or not counts or not indices:
                        continue
                    matrix = xform_cache.GetLocalToWorldTransform(child)
                    world_points = [matrix.Transform(point) for point in points]
                    winding = -1.0 if mesh.GetOrientationAttr().Get() == UsdGeom.Tokens.leftHanded else 1.0
                    offset = 0
                    for count in counts:
                        face_indices = indices[offset:offset + count]
                        offset += count
                        if count < 3:
                            continue
                        corners = [world_points[index] for index in face_indices]
                        normal = Gf.Cross(corners[1] - corners[0], corners[2] - corners[0])
                        if winding * float(normal[1]) <= 1e-8:
                            continue
                        heights = [float(point[1]) for point in corners]
                        if max(heights) - min(heights) > MAX_SUPPORT_FACE_HEIGHT_VARIATION_M:
                            continue
                        polygon = [[float(point[0]), float(point[2])] for point in corners]
                        if polygon_area(polygon) <= 1e-6:
                            continue
                        mesh_faces.append({"owner": owner, "prim_path": str(child.GetPath()),
                                           "height_m": sum(heights) / len(heights),
                                           "polygon_xz": polygon})
            primitive_supports = list(data["support_surfaces"])
            data["support_surfaces"].extend(mesh_faces)
            # Initial layout must see measured mesh floors, not only cubes and
            # thin collider tops. Otherwise a landscape's decorative ledges can
            # become its advertised main floor while the street is discarded.
            data["standing_spots"] = derive_standing_spots(
                data["support_surfaces"], solid_boxes,
                {obj["name"]: obj["heading_deg"] for obj in data["objects"]})
            floor_heights = {obj["name"]: float(obj["position"][1])
                             for obj in data["objects"]}
            floor_levels = {}
            for spot in data["standing_spots"]:
                owner = spot["owner"]
                floor_heights[owner] = min(floor_heights[owner], float(spot["position"][1]))
                floor_levels.setdefault(owner, set()).add(float(spot['position'][1]))
            data["surface_spots"] = derive_surface_spots(
                data["support_surfaces"], solid_boxes, floor_heights)
            # Every measured flat face remains eligible support. Standing and
            # surface markers are bounded placement suggestions, not a walkable
            # mask. Route checks still require continuous body support and
            # clearance from actual solids at the actor's support level.
            data["support_surfaces"] = primitive_supports + mesh_faces
            building_owners = {obj["name"] for obj in data["objects"]
                               if "building" in assets.get(obj.get("asset"), {}).get("tags", [])}
            for owner in building_owners:
                candidates = [spot for spot in data["surface_spots"]
                              if spot["owner"] == owner
                              and spot["position"][1] - floor_heights[owner] >= STANDING_BODY_HEIGHT_M]
                if candidates:
                    source = max(candidates, key=lambda spot: spot["position"][1])
                    data["rooftop_spots"].append({"owner": owner, "name": "roof_01",
                                                  "position": source["position"],
                                                  "surface": source["surface"]})
            data["support_surfaces"].extend(spot["surface"] for spot in data["rooftop_spots"]
                                            if spot["surface"] not in data["support_surfaces"])
            for roof in data["rooftop_spots"]:
                building = next(obj for obj in data["objects"] if obj["name"] == roof["owner"])
                bounds = building.get("obstacle_bounds_xz")
                candidates = []
                for spot in data["standing_spots"]:
                    if spot["owner"] == roof["owner"]:
                        continue
                    point = spot["position"]
                    if point[1] >= roof["position"][1] - STANDING_MAX_STEP_M:
                        continue
                    if bounds and (bounds[0] - ACTOR_SUPPORT_MARGIN_M <= point[0]
                                   <= bounds[2] + ACTOR_SUPPORT_MARGIN_M
                                   and bounds[1] - ACTOR_SUPPORT_MARGIN_M <= point[2]
                                   <= bounds[3] + ACTOR_SUPPORT_MARGIN_M):
                        continue
                    candidates.append(spot)
                if candidates:
                    landing = min(candidates, key=lambda spot: (
                        float(spot["position"][1]),
                        math.hypot(spot["position"][0] - roof["position"][0],
                                   spot["position"][2] - roof["position"][2])))
                    roof["landing_reference"] = f'{landing["owner"]}:{landing["name"]}'
            from spatial_regions import derive_spatial_regions
            data['spatial_regions'] = derive_spatial_regions(data, self.manifest, self.crowd_config)
            if shot_dir is not None and stage_override is None:
                existing = self._read_existing_actors(shot_dir / 'shot.usda', None, {})
                data['existing_actors'] = [dict(actor, appearance=actor['appearance'].to_dict())
                                           for actor in existing.values()]
        except Exception as exc:
            data["read_error"] = str(exc)
        return data

    def _read_placed_vehicle_support(self, performances, initial_states, scene_data):
        """Measure stationary placed vehicles using the same scene geometry reader.

        Moving vehicle tops require time-dependent rider attachment and are not
        advertised as static path support.
        """
        from pxr import Gf, Usd, UsdGeom

        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/World")
        known = {obj["name"] for obj in scene_data.get("objects", [])}
        assets = {asset.get("id"): asset for asset in self.manifest.get("assets", [])}
        added = False
        for actor_id, perf in performances.items():
            if not perf.is_vehicle or actor_id in known:
                continue
            state = initial_states.get(actor_id, {})
            asset = assets.get(state.get("asset_id"), {})
            asset_path = self.project_root / str(asset.get("path", ""))
            if not asset.get("path") or not asset_path.is_file():
                continue
            position = state.get("position", [0, 0, 0])
            heading = float(state.get("facing_deg", 0))
            if any(math.dist(key.position, position) > 0.05
                   or abs((key.heading_deg - heading + 180) % 360 - 180) > 1.0
                   for key in perf.keyframes):
                continue
            prim = UsdGeom.Xform.Define(stage, f"/World/{actor_id}")
            prim.AddTranslateOp().Set(Gf.Vec3d(*position))
            prim.AddRotateYOp().Set(-heading)
            prim.GetPrim().GetReferences().AddReference(str(asset_path))
            prim.GetPrim().SetCustomDataByKey("asset_id", str(asset["id"]))
            added = True
        if not added:
            return {"support_surfaces": [], "solid_boxes": []}
        measured = self._read_scene_context(None, stage_override=stage)
        return {"support_surfaces": measured.get("support_surfaces", []),
                "solid_boxes": measured.get("solid_boxes", [])}

    def _run_collision_validator(self, shot_path: Path, allow_path: Path) -> Tuple[bool, Dict[str, Any]]:
        """Run validate_collisions.py against the compiled shot."""
        val_script = self.project_root / "tools" / "collision" / "validate_collisions.py"
        if not val_script.exists():
            return False, {"error": "Collision validator is missing", "path": str(val_script)}
        python_bin = self.project_root / ".venv" / "bin" / "python"
        if not python_bin.exists():
            python_bin = Path(sys.executable)

        report_json = shot_path.parent / "collision_report.json"
        # A copied/stale passing report must never stand in for this attempt.
        report_json.unlink(missing_ok=True)
        cmd = [
            str(python_bin),
            str(val_script),
            str(shot_path),
            "--manifest", str(self.manifest_path),
            "--allow", str(allow_path),
            "--json", str(report_json),
        ]
        if not self.crowd_config.allow_actor_limb_contacts:
            cmd.append('--strict-actor-limbs')
        edits_path = shot_path.parent / "user_edits.usda"
        if edits_path.exists():
            cmd.extend(["--user-edits", str(edits_path)])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if report_json.exists():
                col_data = json.loads(report_json.read_text(encoding="utf-8"))
            else:
                col_data = {"raw_output": res.stdout, "raw_error": res.stderr}
            clean = (res.returncode == 0 and col_data.get('ok') is True
                     and not any(col_data.get(key) for key in (
                         'violations', 'level_violations', 'unprotected_assets', 'unprotected_actors')))
            return clean, col_data
        except Exception as exc:
            return False, {"error": str(exc)}

    def _update_scene_json_snapshots(self, scene_dir: Path, v_name: str, v_num: int) -> None:
        """Keep scene.json snapshots registry synchronized with authored frozen snapshots."""
        scene_json_path = scene_dir / "scene.json"
        if not scene_json_path.exists():
            return
        try:
            data = json.loads(scene_json_path.read_text(encoding="utf-8"))
            snapshots = data.get("snapshots", [])
            if not any(s.get("file") == v_name for s in snapshots):
                snapshots.append({"file": v_name, "version": v_num})
                data["snapshots"] = snapshots
                scene_json_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _collect_required_prompt_props(
        self,
        prompt: str,
        scene_data: Dict[str, Any],
        registry: ActorRegistry,
    ) -> List[Dict[str, Any]]:
        """Identify props explicitly demanded by prompt that are missing from both

        the sublayered scene and the registered shot actors, returning authored fallback props.
        """
        if not prompt:
            return []
        props: List[Dict[str, Any]] = []
        demanded = self.matcher.find_prompt_props(prompt)
        existing_actors = registry.list_actors()

        for asset in demanded:
            aid = asset.get("id", "")
            dprim = asset.get("default_prim", "Prop")
            concept = asset.get("concept") or aid.split(".")[-1]
            if self.matcher.is_concept_in_scene(concept, scene_data) or self.matcher.is_concept_in_scene(aid, scene_data):
                continue
            if self.matcher.is_concept_in_actors(concept, existing_actors) or self.matcher.is_concept_in_actors(aid, existing_actors):
                continue

            ref_path = f"../../../{asset.get('path', '')}"
            props.append({
                "name": dprim if dprim != "root" else concept.title().replace("_", ""),
                "asset_id": aid,
                "ref_path": ref_path,
                "target_prim": dprim if dprim != "root" else None,
                "position": [-3.18, 0.0, 4.74] if "tree" in aid else [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0] if "tree" in aid else [0.0, 180.0, 0.0],
            })

        return props

    def _verify_prompt_adherence(
        self,
        prompt: str,
        output_shot_usda_path: Path,
        registry: ActorRegistry,
        timeline: ResolvedTimeline,
        cam_keyframes: List[CameraKeyframe],
    ) -> Dict[str, Any]:
        """Verify that composed stage and timeline strictly satisfy all entities and directives in prompt."""
        if not prompt:
            return {"passed": True, "violations": [], "checks": {}}

        p = prompt.lower()
        violations: List[str] = []
        checks: Dict[str, Any] = {}

        # 1. Inspect composed USD stage
        composed_prims = []
        try:
            try:
                from pxr import Usd
            except ImportError:
                venv_site = self.project_root / ".venv" / "lib" / "python3.12" / "site-packages"
                if venv_site.exists() and str(venv_site) not in sys.path:
                    sys.path.insert(0, str(venv_site))
                from pxr import Usd

            stage = Usd.Stage.Open(str(output_shot_usda_path))
            if stage:
                for prim in stage.TraverseAll():
                    cdata_asset = prim.GetCustomDataByKey("asset_id") if prim.HasCustomDataKey("asset_id") else ""
                    composed_prims.append({
                        "path": str(prim.GetPath()),
                        "name": prim.GetName(),
                        "asset_id": str(cdata_asset) if cdata_asset else "",
                    })
        except Exception as exc:
            violations.append(f"Could not open composed stage for verification: {exc}")

        # 2. Generic Manifest-driven Prop Verification
        demanded = self.matcher.find_prompt_props(prompt)
        for asset in demanded:
            aid = asset.get("id", "")
            concept = asset.get("concept") or aid.split(".")[-1]
            found = any(
                self.matcher.prim_has_concept(cp["name"], cp["asset_id"], concept)
                or self.matcher.prim_has_concept(cp["name"], cp["asset_id"], aid)
                for cp in composed_prims
            )
            checks[concept] = {"required": True, "found": found, "asset_id": aid}
            if not found:
                violations.append(f"Prompt mandates '{concept}' ({aid}), but no matching prim exists on the composed stage.")

        # Check Couple / Character count
        human_count = sum(
            1 for a in registry.list_actors()
            if "human" in a.get("asset_id", "")
            or "character" in a.get("asset_id", "")
            or "hero" in a.get("actor_id", "").lower()
            or "partner" in a.get("actor_id", "").lower()
        )
        if re.search(r"\b(couple|two people|both people|pair|two men|two women|two characters)\b", p):
            checks["couple"] = {"required": True, "human_count": human_count}
            if human_count < 2:
                violations.append(f"Prompt mandates a couple (2 people), but only {human_count} character(s) registered.")
        elif re.search(r"\b(three people|3 people|three men|three characters|trio)\b", p):
            checks["three_people"] = {"required": True, "human_count": human_count}
            if human_count < 3:
                violations.append(f"Prompt mandates 3 people, but only {human_count} character(s) registered.")

        # Check Drone / Aerial camera
        if re.search(r"\b(drone|aerial|soaring|flyover)\b", p):
            elevated = bool(cam_keyframes and cam_keyframes[0].position[1] >= 2.5)
            checks["drone_camera"] = {"required": True, "start_height_m": cam_keyframes[0].position[1] if cam_keyframes else 0.0, "elevated": elevated}
            if not elevated:
                violations.append("Prompt mandates a drone/aerial camera, but camera start elevation is below 2.5m.")

        # Check Emotional shift (e.g. happy to surprise/startled)
        if re.search(r"\b(happy|joy|joyful|cheering)\b", p) and re.search(r"\b(surprise|surprised|startled|spooked)\b", p):
            has_happy = False
            has_surprise = False
            for beat in timeline.beats:
                fn = beat.function
                move = beat.params.get("move", "")
                mood = beat.params.get("mood", "")
                style = beat.params.get("style", "")
                if move in ("cheering", "clapping", "greeting") or style in ("happy", "cheer", "excited") or fn in ("converse", "greet", "gesture"):
                    has_happy = True
                if mood in ("spooked", "reaction", "flinch") or style in ("startled", "spooked", "fear", "panic") or fn in ("dodge", "fall", "react", "spooked"):
                    has_surprise = True
            checks["emotional_shift"] = {"required": True, "happy_action_found": has_happy, "surprise_action_found": has_surprise}
            if not (has_happy and has_surprise):
                violations.append("Prompt mandates transition from joyful/happy to startled/surprised expressions, but timeline is missing required gestures/reactions.")

        return {
            "passed": len(violations) == 0,
            "violations": violations,
            "checks": checks,
        }


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent.parent
    app_bin = repo_root / "build" / "PainterSpace"
    env = dict(os.environ)
    qt_lib = "/Users/vishaljose/Qt/6.11.1/macos/lib"
    env["DYLD_FRAMEWORK_PATH"] = f"{qt_lib}:{env.get('DYLD_FRAMEWORK_PATH', '')}".strip(":")
    subprocess.run(["cmake", "--build", str(repo_root / "build")], cwd=str(repo_root), check=False)
    os.execve(str(app_bin), [str(app_bin)], env)



