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
from asset_matcher import ManifestAssetMatcher, tokenize
from manifest_schema import enrich_manifest
from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from plan_model import Beat, SetupAction, ShotPlan, StructuredError
from semantic_references import ReferenceResolver, ResolvedTarget
from shot_linter import ShotLinter
from timeline_resolver import ResolvedBeat, ResolvedTimeline, TimelineResolver


class ShotCompiler:
    def __init__(self, project_root: Optional[Path] = None, fps: int = 24) -> None:
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
        """Compile a ShotPlan into a complete shot directory.

        Returns (success: bool, report: dict).
        """
        output_shot_usda_path = output_shot_usda_path.resolve()
        shot_dir = output_shot_usda_path.parent
        shot_dir.mkdir(parents=True, exist_ok=True)

        if not scene_usda_path:
            scene_usda_path = shot_dir.parent / "scene.usda"

        # Resolve immutable scene snapshot version
        scene_dir = shot_dir.parent
        scene_version_file = None
        if scene_usda_path and scene_usda_path.exists():
            v_files = []
            for f in scene_dir.glob("scene_v*.usda"):
                m = re.match(r"^scene_v(\d+)\.usda$", f.name)
                if m:
                    v_files.append((int(m.group(1)), f.name, f))
            if v_files:
                v_files.sort(key=lambda x: x[0])
                latest_num, latest_name, latest_file = v_files[-1]
                scene_hash = hashlib.sha256(scene_usda_path.read_bytes()).hexdigest()
                latest_hash = hashlib.sha256(latest_file.read_bytes()).hexdigest()
                if scene_hash == latest_hash:
                    scene_version_file = latest_name
                else:
                    new_num = latest_num + 1
                    new_name = f"scene_v{new_num}.usda"
                    new_path = scene_dir / new_name
                    try:
                        shutil.copy2(scene_usda_path, new_path)
                        scene_version_file = new_name
                        self._update_scene_json_snapshots(scene_dir, new_name, new_num)
                    except Exception:
                        scene_version_file = latest_name
            else:
                v1_path = scene_dir / "scene_v1.usda"
                try:
                    shutil.copy2(scene_usda_path, v1_path)
                    scene_version_file = "scene_v1.usda"
                    self._update_scene_json_snapshots(scene_dir, "scene_v1.usda", 1)
                except Exception:
                    pass

        # 1. Parse scene context (zones, static objects)
        scene_data = self._read_scene_context(scene_usda_path)
        ref_resolver = ReferenceResolver(self.manifest, scene_data)

        # 2. Setup actors and initialize registry
        registry = ActorRegistry()
        actor_initial_states: Dict[str, Dict[str, Any]] = {}

        for setup_act in plan.setup:
            if setup_act.action_type == "place":
                asset_id = setup_act.asset_id or "character.human"
                app = None
                if setup_act.appearance:
                    app = ActorAppearance.from_dict(setup_act.appearance)

                rec = registry.register(
                    asset_id=asset_id,
                    label=setup_act.label,
                    actor_id=setup_act.actor_id,
                    appearance=app,
                )

                # Resolve initial position and facing
                init_pos = [0.0, 0.0, 0.0]
                init_facing = 0.0
                if setup_act.at_reference:
                    target_res, err = ref_resolver.resolve(
                        setup_act.at_reference, actor_initial_states, 0.0
                    )
                    if target_res:
                        init_pos = list(target_res.position)
                        init_facing = target_res.facing_deg
                elif setup_act.position:
                    init_pos = list(setup_act.position)
                    if setup_act.facing_deg is not None:
                        init_facing = float(setup_act.facing_deg)

                if setup_act.facing_reference:
                    face_res, _ = ref_resolver.resolve(
                        setup_act.facing_reference, actor_initial_states, 0.0
                    )
                    if face_res:
                        dx = face_res.position[0] - init_pos[0]
                        dz = face_res.position[2] - init_pos[2]
                        if math.hypot(dx, dz) > 1e-3:
                            init_facing = math.degrees(math.atan2(dx, -dz)) % 360.0

                is_veh = "car" in rec.asset_id or "vehicle" in rec.asset_id
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
        if not actor_initial_states and plan.beats:
            for b in plan.beats:
                if b.actor_id not in registry._actors:
                    default_car = next((a.get("id") for a in self.manifest.get("assets", []) if "car" in a.get("id", "") or "car" in a.get("tags", [])), "cars.car1")
                    default_human = next((a.get("id") for a in self.manifest.get("assets", []) if "character" in a.get("id", "") or "human" in a.get("id", "")), "character.human")
                    asset_id = default_car if "car" in b.actor_id else default_human
                    rec = registry.register(asset_id=asset_id, actor_id=b.actor_id)
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

        # 3. Resolve timeline
        timeline_resolver = TimelineResolver(fps=self.fps, ref_resolver=ref_resolver)
        timeline, err = timeline_resolver.resolve(plan, actor_initial_states)
        if err or not timeline:
            return False, {
                "success": False,
                "error": err.to_dict() if err else {"message": "Timeline resolution failed"},
            }

        # 4. Resolve semantic targets for each beat (initial pass; dynamic re-resolution occurs at beat t_start in MotionSynthesizer)
        resolved_targets: Dict[str, ResolvedTarget] = {}
        actor_states_at_start = copy.deepcopy(actor_initial_states)

        for b in plan.beats:
            target_ref = b.params.get("target") or b.params.get("to")
            if not target_ref and b.function == "climb_stairs":
                target_ref = b.params.get("stairs")
            if target_ref:
                t_res, ref_err = ref_resolver.resolve(
                    target_ref, actor_states_at_start, 0.0, source_actor_id=b.actor_id
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
                            suggestions=["Check valid actor IDs, zone markers, or surfaces in manifest."],
                        ).to_dict(),
                    }

        # 5. Motion synthesis
        synthesizer = MotionSynthesizer(
            self.manifest,
            fps=self.fps,
            ref_resolver=ref_resolver,
            scene_data=scene_data,
        )
        actor_performances: Dict[str, ActorPerformance] = {}

        # Synthesize in sequence: vehicles first (threats), then humans (reactions/actions)
        vehicle_ids = [aid for aid, s in actor_initial_states.items() if s["is_vehicle"]]
        human_ids = [aid for aid, s in actor_initial_states.items() if not s["is_vehicle"]]

        for aid in vehicle_ids + human_ids:
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

        # 6. Camera synthesis
        cam_controller = CameraController(fps=self.fps)
        actor_positions_by_id = {
            aid: [(kf.time_s, list(kf.position)) for kf in p.keyframes]
            for aid, p in actor_performances.items()
        }
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
            scene_usda_rel_path=f"../{scene_version_file}" if scene_version_file else "../scene.usda",
            required_props=required_props,
        )
        output_shot_usda_path.write_text(shot_usda_text, encoding="utf-8")

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

        # 9. Write sidecars: shot_plan.json, resolved_timeline.json, allow_collisions.json, shot.json
        plan_path = shot_dir / "shot_plan.json"
        plan_path.write_text(plan.to_json(), encoding="utf-8")

        timeline_path = shot_dir / "resolved_timeline.json"
        timeline_path.write_text(json.dumps(timeline.to_dict(), indent=2), encoding="utf-8")

        # Aggregate allowed contacts across all actors
        allowed_contacts = []
        scene_building_assets = [
            obj.get("asset") or obj.get("name")
            for obj in scene_data.get("objects", [])
            if str(obj.get("asset", "")).startswith("buildings.") or "club" in str(obj.get("name", "")).lower()
        ]
        for aid, perf in actor_performances.items():
            rec = registry.get(aid)
            label = rec.label.replace(" ", "_") if rec else aid
            actor_names = [aid]
            if label not in actor_names:
                actor_names.append(label)

            # Allow foot/sole contact with building floor meshes in the scene
            for b_asset in scene_building_assets:
                for a_name in actor_names:
                    allowed_contacts.append({
                        "actor": a_name,
                        "asset": b_asset,
                        "joints": ["Foot", "Toe", "Leg"],
                        "max_depth_m": 0.18,
                    })

            for c in perf.allowed_contacts:
                target_asset = c.get("asset")
                target_rec = registry.get(target_asset) if target_asset else None
                possible_assets = [target_asset]
                if target_rec:
                    possible_assets.extend([target_rec.actor_id, target_rec.label, target_rec.label.replace(" ", "_"), target_rec.asset_id])

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
            "success": len(character_bake_errors) == 0,
            "lint_passed": True,
            "collision_passed": True,
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
        cam_ok, cam_warnings = cam_controller.validate_frustum(
            cam_keyframes, actor_positions_by_id, active_actor_ids
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
        if not prompt_res["passed"]:
            validation_report["success"] = False

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
            f"    subLayers = [ @{scene_usda_rel_path}@ ]",
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
            f = int(round(kf.time_s * self.fps))
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
                f = int(round(kf.time_s * self.fps))
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

    def _read_scene_context(self, scene_usda_path: Path) -> Dict[str, Any]:
        """Extract zones and static objects from scene.usda."""
        data = {"zones": [], "objects": []}
        if not scene_usda_path.exists():
            return data
        try:
            from pxr import Usd, UsdGeom
            stage = Usd.Stage.Open(str(scene_usda_path))
            if not stage:
                return data
            for prim in stage.Traverse():
                p_path = str(prim.GetPath())
                if "Zones" in p_path and prim.IsA(UsdGeom.Cube):
                    xform = UsdGeom.Xformable(prim)
                    pos = [0.0, 0.0, 0.0]
                    scale = [1.0, 1.0, 1.0]
                    for op in xform.GetOrderedXformOps():
                        if "translate" in op.GetOpName():
                            t = op.Get()
                            if t:
                                pos = [float(t[0]), float(t[1]), float(t[2])]
                        elif "scale" in op.GetOpName():
                            s = op.Get()
                            if s:
                                scale = [float(s[0]), float(s[1]), float(s[2])]
                    bmin = [pos[0] - scale[0] * 0.5, pos[1], pos[2] - scale[2] * 0.5]
                    bmax = [pos[0] + scale[0] * 0.5, pos[1] + scale[1], pos[2] + scale[2] * 0.5]
                    data["zones"].append({"id": prim.GetName(), "min": bmin, "max": bmax})
                elif prim.GetParent() and prim.GetParent().GetName() == "World" and prim.GetName() != "Zones" and not prim.GetTypeName().endswith("Light"):
                    xform = UsdGeom.Xformable(prim)
                    pos = [0.0, 0.0, 0.0]
                    rot_y = 0.0
                    for op in xform.GetOrderedXformOps():
                        if "translate" in op.GetOpName():
                            t = op.Get()
                            if t:
                                pos = [float(t[0]), float(t[1]), float(t[2])]
                        elif "rotateXYZ" in op.GetOpName():
                            r = op.Get()
                            if r:
                                rot_y = float(r[1])
                        elif "rotateY" in op.GetOpName():
                            r = op.Get()
                            if r is not None:
                                rot_y = float(r)
                    asset_id = prim.GetCustomDataByKey("asset_id") if prim.HasCustomDataKey("asset_id") else prim.GetName()
                    data["objects"].append({
                        "id": prim.GetName(),
                        "name": prim.GetName(),
                        "asset": asset_id,
                        "position": pos,
                        "heading_deg": rot_y,
                    })
        except Exception:
            pass
        return data

    def _run_collision_validator(self, shot_path: Path, allow_path: Path) -> Tuple[bool, Dict[str, Any]]:
        """Run validate_collisions.py against the compiled shot."""
        val_script = self.project_root / "tools" / "collision" / "validate_collisions.py"
        if not val_script.exists():
            return True, {}
        python_bin = self.project_root / ".venv" / "bin" / "python"
        if not python_bin.exists():
            python_bin = Path(sys.executable)

        report_json = shot_path.parent / "collision_report.json"
        cmd = [
            str(python_bin),
            str(val_script),
            str(shot_path),
            "--manifest", str(self.manifest_path),
            "--allow", str(allow_path),
            "--json", str(report_json),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                repair_cmd = cmd + ["--repair"]
                subprocess.run(repair_cmd, capture_output=True, text=True, timeout=120)
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if report_json.exists():
                col_data = json.loads(report_json.read_text(encoding="utf-8"))
            else:
                col_data = {"raw_output": res.stdout, "raw_error": res.stderr}
            return res.returncode == 0, col_data
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



