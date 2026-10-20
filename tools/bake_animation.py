#!/usr/bin/env python3
"""Bake a declarative multi-action performance into one UsdSkelAnimation.

The input action JSON is data only::

  {"actions": [{"clip": "run", "start_s": 0, "end_s": 2}, ...]}

Clip definitions live in the manifest's top-level ``clips`` object.  This
program intentionally never evaluates input text as code; it only reads USD
and JSON and writes a USD animation layer.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt


class BakeError(ValueError):
    pass


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _leaf(name: str) -> str:
    return name.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _without_namespaces(name: str) -> str:
    return "/".join(part.rsplit(":", 1)[-1] for part in name.split("/"))


def _as_vec3(value: Any) -> Gf.Vec3f:
    return Gf.Vec3f(float(value[0]), float(value[1]), float(value[2]))


def _identity_pose(count: int) -> "Pose":
    return Pose([Gf.Quatf(1.0, Gf.Vec3f(0.0)) for _ in range(count)],
                [Gf.Vec3f(0.0) for _ in range(count)],
                [Gf.Vec3f(1.0) for _ in range(count)])


@dataclass
class Pose:
    rotations: list[Gf.Quatf]
    translations: list[Gf.Vec3f]
    scales: list[Gf.Vec3f]


@dataclass
class Clip:
    name: str
    options: dict[str, Any]
    # Keep the source stage alive.  UsdPrim/UsdSkel schema wrappers become
    # invalid when their owning Python Usd.Stage is garbage-collected.
    stage: Usd.Stage
    animation: UsdSkel.Animation
    first: float
    last: float
    tps: float
    source_for_target: list[int | None]
    root_index: int
    root_first_translation: Gf.Vec3f

    @property
    def duration(self) -> float:
        return self.last - self.first


@dataclass
class Action:
    clip: Clip
    start: float
    end: float
    speed: float
    loop: bool
    blend: float


@dataclass
class OverlayAction:
    clip: Clip
    start: float
    end: float
    speed: float
    loop: bool
    blend: float
    weight: float


UPPER_BODY_JOINT_WEIGHTS: dict[str, float] = {
    "Spine": 0.35,
    "Spine1": 0.65,
    "Spine2": 0.90,
    "Neck": 0.85,
    "Head": 0.85,
    "HeadTop_End": 0.85,
    "LeftShoulder": 1.0,
    "RightShoulder": 1.0,
    "LeftArm": 1.0,
    "RightArm": 1.0,
    "LeftForeArm": 1.0,
    "RightForeArm": 1.0,
    "LeftHand": 1.0,
    "RightHand": 1.0,
}


def _upper_body_mask_for_joints(joints: list[str]) -> list[float]:
    mask: list[float] = []
    for j in joints:
        leaf = _leaf(j)
        if leaf in UPPER_BODY_JOINT_WEIGHTS:
            mask.append(UPPER_BODY_JOINT_WEIGHTS[leaf])
        elif any(k in leaf for k in ("Thumb", "Index", "Middle", "Ring", "Pinky", "Hand", "Arm", "Shoulder")):
            mask.append(1.0)
        else:
            mask.append(0.0)
    return mask


def blend_masked(base: Pose, overlay: Pose, joint_mask: list[float], envelope_weight: float) -> Pose:
    w_env = max(0.0, min(1.0, envelope_weight))
    w_env = w_env * w_env * (3.0 - 2.0 * w_env)
    out_rot: list[Gf.Quatf] = []
    for r_b, r_o, jm in zip(base.rotations, overlay.rotations, joint_mask):
        w = w_env * jm
        if w <= 1e-4:
            out_rot.append(r_b)
        else:
            out_rot.append(Gf.Slerp(w, r_b, r_o))
    return Pose(out_rot, list(base.translations), list(base.scales))


def _quat_from_axis_angle_deg(axis: Gf.Vec3f, angle_deg: float) -> Gf.Quatf:
    rot = Gf.Rotation(Gf.Vec3d(float(axis[0]), float(axis[1]), float(axis[2])), float(angle_deg))
    q = rot.GetQuat()
    imag = q.GetImaginary()
    return Gf.Quatf(float(q.GetReal()), Gf.Vec3f(float(imag[0]), float(imag[1]), float(imag[2])))


def apply_procedural_life_and_gaze(
    pose: Pose,
    joints: list[str],
    time: float,
    clip_name: str,
    look_at_targets: list[dict[str, Any]],
) -> Pose:
    rotations = list(pose.rotations)

    # 1. Subtle deterministic breathing on stationary/social clips so waiting actors never freeze
    if clip_name in ("acknowledging", "idle", "wait"):
        breath_phase = math.sin(2.0 * math.pi * 0.28 * time)
        spine_q = _quat_from_axis_angle_deg(Gf.Vec3f(1.0, 0.0, 0.0), 0.75 * breath_phase)
        shoulder_l_q = _quat_from_axis_angle_deg(Gf.Vec3f(0.0, 0.0, 1.0), 0.35 * breath_phase)
        shoulder_r_q = _quat_from_axis_angle_deg(Gf.Vec3f(0.0, 0.0, 1.0), -0.35 * breath_phase)
        for idx, j in enumerate(joints):
            leaf = _leaf(j)
            if leaf in ("Spine1", "Spine2"):
                rotations[idx] = rotations[idx] * spine_q
            elif leaf == "LeftShoulder":
                rotations[idx] = rotations[idx] * shoulder_l_q
            elif leaf == "RightShoulder":
                rotations[idx] = rotations[idx] * shoulder_r_q

    # 2. Procedural gaze IK (Spine2 / Neck / Head yaw & pitch toward active look_at_target)
    for l_tgt in look_at_targets:
        t0 = float(l_tgt.get("start_s", 0.0))
        t1 = float(l_tgt.get("end_s", 0.0))
        if t0 <= time <= t1:
            yaw_deg = float(l_tgt.get("yaw_deg", 0.0))
            pitch_deg = float(l_tgt.get("pitch_deg", 0.0))
            weight = float(l_tgt.get("weight", 0.85))
            if abs(yaw_deg) > 0.2 or abs(pitch_deg) > 0.2:
                fade = min(1.0, (time - t0) / 0.25, (t1 - time) / 0.25) if (t1 - t0) > 0.5 else 1.0
                eff_w = max(0.0, min(1.0, fade * weight))
                clamped_yaw = max(-50.0, min(50.0, yaw_deg)) * eff_w
                clamped_pitch = max(-22.0, min(22.0, pitch_deg)) * eff_w
                for idx, j in enumerate(joints):
                    leaf = _leaf(j)
                    if leaf == "Spine2":
                        q_y = _quat_from_axis_angle_deg(Gf.Vec3f(0.0, 1.0, 0.0), clamped_yaw * 0.15)
                        rotations[idx] = rotations[idx] * q_y
                    elif leaf == "Neck":
                        q_y = _quat_from_axis_angle_deg(Gf.Vec3f(0.0, 1.0, 0.0), clamped_yaw * 0.35)
                        q_p = _quat_from_axis_angle_deg(Gf.Vec3f(1.0, 0.0, 0.0), clamped_pitch * 0.40)
                        rotations[idx] = rotations[idx] * q_y * q_p
                    elif leaf == "Head":
                        q_y = _quat_from_axis_angle_deg(Gf.Vec3f(0.0, 1.0, 0.0), clamped_yaw * 0.50)
                        q_p = _quat_from_axis_angle_deg(Gf.Vec3f(1.0, 0.0, 0.0), clamped_pitch * 0.60)
                        rotations[idx] = rotations[idx] * q_y * q_p
            break

    return Pose(rotations, list(pose.translations), list(pose.scales))


def find_one(stage: Usd.Stage, schema: type) -> Any:
    found = [schema(prim) for prim in stage.Traverse() if prim.IsA(schema)]
    if not found:
        raise BakeError(f"No {schema.__name__.split('.')[-1]} found in {stage.GetRootLayer().realPath}")
    return found[0]


def rest_pose(skeleton: UsdSkel.Skeleton, count: int) -> Pose:
    matrices = skeleton.GetRestTransformsAttr().Get() or []
    pose = _identity_pose(count)
    for i, matrix in enumerate(matrices[:count]):
        transform = Gf.Transform(matrix)
        pose.translations[i] = Gf.Vec3f(transform.GetTranslation())
        pose.rotations[i] = Gf.Quatf(transform.GetRotation().GetQuat())
        pose.scales[i] = Gf.Vec3f(transform.GetScale())
    return pose


def joint_map(target: list[str], source: list[str], clip_name: str) -> list[int | None]:
    exact = {name: i for i, name in enumerate(source)}
    stripped: dict[str, list[int]] = {}
    leaves: dict[str, list[int]] = {}
    for i, name in enumerate(source):
        stripped.setdefault(_without_namespaces(name), []).append(i)
        leaves.setdefault(_leaf(name), []).append(i)
    result: list[int | None] = []
    for name in target:
        index = exact.get(name)
        if index is None:
            candidates = stripped.get(_without_namespaces(name), [])
            if len(candidates) == 1:
                index = candidates[0]
        if index is None:
            candidates = leaves.get(_leaf(name), [])
            if len(candidates) == 1:
                index = candidates[0]
        if index is None:
            warn(f"clip '{clip_name}' has no unambiguous joint for '{name}'; using rest pose")
        result.append(index)
    return result


def attr_value(attr: Usd.Attribute, time: float, fallback: list[Any]) -> list[Any]:
    value = attr.Get(Usd.TimeCode(time))
    return list(value) if value is not None else fallback


def sample_clip(clip: Clip, output_time: float, action: Action, fallback: Pose) -> Pose:
    elapsed = max(0.0, (output_time - action.start) * action.speed)
    # USD time codes need not start at zero. A one-sample clip is held.
    if clip.duration <= 1e-8:
        source_time = clip.first
    elif action.loop:
        source_time = clip.first + math.fmod(elapsed * clip.tps, clip.duration)
    else:
        source_time = min(clip.first + elapsed * clip.tps, clip.last)

    rotations = attr_value(clip.animation.GetRotationsAttr(), source_time, [])
    translations = attr_value(clip.animation.GetTranslationsAttr(), source_time, [])
    scales = attr_value(clip.animation.GetScalesAttr(), source_time, [])
    out = Pose(list(fallback.rotations), list(fallback.translations), list(fallback.scales))
    for target_index, source_index in enumerate(clip.source_for_target):
        if source_index is None:
            continue
        if source_index < len(rotations): out.rotations[target_index] = Gf.Quatf(rotations[source_index])
        if source_index < len(translations): out.translations[target_index] = Gf.Vec3f(translations[source_index])
        if source_index < len(scales): out.scales[target_index] = Gf.Vec3f(scales[source_index])

    scale = float(clip.options.get("translation_scale", 1.0))
    out.translations = [value * scale for value in out.translations]
    if clip.root_index >= 0:
        root = out.translations[clip.root_index]
        original = clip.root_first_translation * scale
        axes = clip.options.get("strip_root_axes", []) if clip.options.get("strip_root_motion") else []
        for axis in axes:
            if axis not in (0, 1, 2):
                raise BakeError(f"clip '{clip.name}' has invalid strip_root_axes entry {axis}")
            root[axis] = original[axis]
        offset = clip.options.get("root_offset_m", [0, 0, 0])
        if len(offset) != 3:
            raise BakeError(f"clip '{clip.name}' root_offset_m must have three numbers")
        out.translations[clip.root_index] = root + _as_vec3(offset)
    return out


def blend(a: Pose, b: Pose, weight: float) -> Pose:
    # Cubic smoothstep gives zero velocity at either end of the join.
    weight = max(0.0, min(1.0, weight))
    weight = weight * weight * (3.0 - 2.0 * weight)
    return Pose([Gf.Slerp(weight, x, y) for x, y in zip(a.rotations, b.rotations)],
                [x * (1.0 - weight) + y * weight for x, y in zip(a.translations, b.translations)],
                [x * (1.0 - weight) + y * weight for x, y in zip(a.scales, b.scales)])


def action_at(actions: list[Action], time: float) -> int | None:
    # Later list entries intentionally win an overlap.
    result = None
    for i, action in enumerate(actions):
        if action.start <= time <= action.end:
            result = i
    return result


def load_clip(name: str, options: dict[str, Any], target_joints: list[str], root_index: int,
              manifest_dir: Path) -> Clip:
    path = (manifest_dir / options["file"]).resolve()
    stage = Usd.Stage.Open(str(path))
    if not stage:
        raise BakeError(f"Unable to open clip '{name}': {path}")
    animation = find_one(stage, UsdSkel.Animation)
    source_joints = [str(x) for x in (animation.GetJointsAttr().Get() or [])]
    if not source_joints:
        raise BakeError(f"Clip '{name}' has no joints")
    samples: list[float] = []
    for attr in (animation.GetRotationsAttr(), animation.GetTranslationsAttr(), animation.GetScalesAttr()):
        samples.extend(attr.GetTimeSamples())
    if not samples:
        raise BakeError(f"Clip '{name}' has no animation time samples")
    first, last = min(samples), max(samples)
    source_map = joint_map(target_joints, source_joints, name)
    root_source = source_map[root_index] if root_index >= 0 else None
    first_translations = attr_value(animation.GetTranslationsAttr(), first, [])
    root_first = (Gf.Vec3f(first_translations[root_source]) if root_source is not None and root_source < len(first_translations)
                  else Gf.Vec3f(0.0))
    return Clip(name, options, stage, animation, first, last, stage.GetTimeCodesPerSecond() or 24.0,
                source_map, root_index, root_first)


def bake(character_path: Path, manifest_path: Path, actions_path: Path, output_path: Path,
         fps: float | None, duration: float | None) -> tuple[int, float]:
    manifest = json.loads(manifest_path.read_text())
    library = manifest.get("clips")
    if not isinstance(library, dict):
        raise BakeError("Manifest needs a top-level 'clips' object")
    payload = json.loads(actions_path.read_text())
    raw_actions = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(raw_actions, list) or not raw_actions:
        raise BakeError("Action list needs a non-empty 'actions' array")

    character_stage = Usd.Stage.Open(str(character_path))
    if not character_stage:
        raise BakeError(f"Unable to open character: {character_path}")
    skeleton = find_one(character_stage, UsdSkel.Skeleton)
    joints = [str(x) for x in (skeleton.GetJointsAttr().Get() or [])]
    if not joints:
        raise BakeError("Character skeleton has no joints")
    fallback = rest_pose(skeleton, len(joints))
    root_index = 0
    clips: dict[str, Clip] = {}
    actions: list[Action] = []
    for raw in raw_actions:
        if not isinstance(raw, dict): raise BakeError("Each action must be an object")
        name = raw.get("clip")
        if name not in library or not isinstance(library[name], dict):
            raise BakeError(f"Unknown clip: {name!r}")
        start, end = raw.get("start_s"), raw.get("end_s")
        speed = raw.get("speed", 1.0)
        if not all(isinstance(v, (int, float)) for v in (start, end, speed)) or end <= start or speed <= 0:
            raise BakeError(f"Action '{name}' needs end_s > start_s and speed > 0")
        options = library[name]
        if "file" not in options: raise BakeError(f"Clip '{name}' has no file")
        if name not in clips:
            clips[name] = load_clip(name, options, joints, root_index, manifest_path.parent)
        one_shot_clips = {
            "falling_down", "knocked_out", "dying_backwards", "stand_up", "sit_to_stand",
            "sitting_down", "entering_car", "exiting_car", "jumping_down", "jump_over_obstacle",
            "backflip", "front_flip",
        }
        clip_dur_s = clips[name].duration / max(1.0, clips[name].tps)
        default_loop = bool(options.get("loop", (name not in one_shot_clips and (end - start) > clip_dur_s * 1.05)))
        loop = raw.get("loop", default_loop)
        if not isinstance(loop, bool): raise BakeError(f"Action '{name}' loop must be boolean")
        blend_s = raw.get("blend_s", 0.0)
        if not isinstance(blend_s, (int, float)) or blend_s < 0:
            raise BakeError(f"Action '{name}' blend_s must be non-negative")
        actions.append(Action(clips[name], float(start), float(end), float(speed), loop, float(blend_s)))

    for earlier, later in zip(actions, actions[1:]):
        if later.start > earlier.end: warn(f"gap from {earlier.end:g}s to {later.start:g}s; holding pose")
        if later.start < earlier.end: warn(f"overlap at {later.start:g}s; later action wins")

    raw_overlays = payload.get("overlay_actions", []) if isinstance(payload, dict) else []
    overlays: list[OverlayAction] = []
    if isinstance(raw_overlays, list):
        for raw in raw_overlays:
            if not isinstance(raw, dict):
                continue
            name = raw.get("clip")
            if name not in library or not isinstance(library[name], dict):
                continue
            start, end = float(raw.get("start_s", 0.0)), float(raw.get("end_s", 0.0))
            speed = float(raw.get("speed", 1.0))
            if end <= start or speed <= 0:
                continue
            options = library[name]
            if "file" not in options:
                continue
            if name not in clips:
                clips[name] = load_clip(name, options, joints, root_index, manifest_path.parent)
            loop = bool(raw.get("loop", options.get("loop", True)))
            blend_s = max(0.0, float(raw.get("blend_s", 0.2)))
            weight = max(0.0, min(1.0, float(raw.get("weight", 0.75))))
            overlays.append(OverlayAction(clips[name], start, end, speed, loop, blend_s, weight))

    look_at_targets = payload.get("look_at_targets", []) if isinstance(payload, dict) else []
    if not isinstance(look_at_targets, list):
        look_at_targets = []
    upper_mask = _upper_body_mask_for_joints(joints)

    fps = fps or float(payload.get("fps", character_stage.GetTimeCodesPerSecond() or 24.0))
    if fps <= 0: raise BakeError("fps must be positive")
    duration = duration if duration is not None else float(payload.get("duration_s", max(a.end for a in actions)))
    if duration <= 0: raise BakeError("duration must be positive")
    if any(a.end > duration + 1e-8 for a in actions): raise BakeError("Action extends beyond output duration")
    end_frame = int(math.ceil(duration * fps))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    stage = Usd.Stage.CreateNew(str(output_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(end_frame)
    stage.SetTimeCodesPerSecond(fps)
    stage.SetFramesPerSecond(fps)
    animation = UsdSkel.Animation.Define(stage, "/BakedAnim")
    animation.CreateJointsAttr(Vt.TokenArray(joints))
    rotation_attr, translation_attr, scale_attr = (animation.CreateRotationsAttr(), animation.CreateTranslationsAttr(), animation.CreateScalesAttr())
    held = fallback
    active_clip_name = "idle"
    for frame in range(end_frame + 1):
        time = frame / fps
        index = action_at(actions, time)
        if index is not None:
            current = actions[index]
            active_clip_name = current.clip.name
            pose = sample_clip(current.clip, time, current, fallback)
            if index and current.blend > 0 and time < current.start + current.blend:
                previous = sample_clip(actions[index - 1].clip, time, actions[index - 1], fallback)
                pose = blend(previous, pose, (time - current.start) / current.blend)
            held = pose

        out_pose = held
        # Apply upper-body masked overlay actions
        for ov in overlays:
            if ov.start <= time <= ov.end:
                ov_act = Action(ov.clip, ov.start, ov.end, ov.speed, ov.loop, ov.blend)
                ov_pose = sample_clip(ov.clip, time, ov_act, fallback)
                env = 1.0
                if ov.blend > 0:
                    env = min(1.0, (time - ov.start) / ov.blend, (ov.end - time) / ov.blend)
                out_pose = blend_masked(out_pose, ov_pose, upper_mask, env * ov.weight)

        # Apply procedural breathing micro-motion and gaze IK
        out_pose = apply_procedural_life_and_gaze(out_pose, joints, time, active_clip_name, look_at_targets)

        rotation_attr.Set(Vt.QuatfArray(out_pose.rotations), frame)
        translation_attr.Set(Vt.Vec3fArray(out_pose.translations), frame)
        scale_attr.Set(Vt.Vec3fArray(out_pose.scales), frame)
    stage.SetDefaultPrim(animation.GetPrim())
    stage.GetRootLayer().Save()
    return end_frame, fps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--actions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--duration-s", type=float)
    # Blender embeds Python but forwards its own command line too; accepting
    # arguments after ``--`` makes this tool usable in Blender's bundled USD
    # runtime as well as a normal project venv.
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None)
    try:
        frames, fps = bake(args.character, args.manifest, args.actions, args.output, args.fps, args.duration_s)
        print(json.dumps({"output": str(args.output), "frames": frames + 1, "end_frame": frames, "fps": fps}))
        return 0
    except (BakeError, OSError, json.JSONDecodeError) as exc:
        print(f"bake_animation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
