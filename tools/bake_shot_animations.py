#!/usr/bin/env python3
"""Bake, bind, and orientation-align character performances for OpenUSD stages.

The LLM-facing part of the plan is just each ``actions`` array (and optional
trajectory intent). The app infrastructure adds ``character_path`` and
``character_file``, calculates speed-matched footwork trajectories, aligns the
character's rotation to face its direction of travel (preventing moonwalking /
backwards motion for +Z characters), and deterministically bakes and binds the
performance into the composed shot stage.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

from bake_animation import BakeError, bake
from bind_baked_animation import bind


def heading_of(dx: float, dz: float) -> float:
    """World-space XZ heading: 0 = north (-Z), 90 = east (+X), clockwise from above."""
    if math.hypot(dx, dz) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(dx, -dz)) % 360.0


def resolve_forward_axis(prim: Usd.Prim, character_file: str, manifest: dict) -> str:
    """Resolve character asset forward axis (+Z for Mixamo/humanoid, -Z for standard props)."""
    file_stem = Path(character_file).stem.lower()
    for asset in manifest.get("assets", []):
        asset_stem = Path(asset.get("path", "")).stem.lower()
        if asset_stem == file_stem or asset.get("id", "") in (f"character.{file_stem}", "character.human"):
            fwd = asset.get("forward_axis")
            if fwd and str(fwd).strip():
                return str(fwd).strip()

    # Mixamo and standard rigged human assets in PainterSpace natively face +Z
    if "human" in file_stem or "walk" in file_stem or "run" in file_stem:
        return "+Z"

    custom_axis = prim.GetCustomDataByKey("forward_axis") if prim and prim.IsValid() else None
    if custom_axis and str(custom_axis).strip():
        return str(custom_axis).strip()

    return manifest.get("forward_axis", "-Z")


def align_character_heading_to_motion(
    shot_path: Path, character_path: str, forward_axis: str
) -> float | None:
    """Ensure a moving character faces forward along its trajectory.

    For an asset with forward_axis = "+Z", moving in heading theta requires:
        rotateY = (180.0 - theta) % 360.0
    For an asset with forward_axis = "-Z", moving in heading theta requires:
        rotateY = (-theta) % 360.0
    """
    stage = Usd.Stage.Open(str(shot_path.resolve()))
    if not stage:
        return None
    prim = stage.GetPrimAtPath(character_path)
    if not prim or not prim.IsValid():
        return None

    xformable = UsdGeom.Xformable(prim)
    translate_op = None
    rotate_y_op = None
    rotate_xyz_op = None

    for op in xformable.GetOrderedXformOps():
        name = op.GetOpName()
        if "translate" in name:
            translate_op = op
        elif "rotateY" in name:
            rotate_y_op = op
        elif "rotateXYZ" in name:
            rotate_xyz_op = op

    if not translate_op:
        return None

    times = translate_op.GetTimeSamples()
    if len(times) < 2:
        return None

    p_start = translate_op.Get(times[0])
    p_end = translate_op.Get(times[-1])
    if p_start is None or p_end is None:
        return None

    dx = float(p_end[0] - p_start[0])
    dz = float(p_end[2] - p_start[2])
    if math.hypot(dx, dz) < 0.05:
        return None

    travel_heading = heading_of(dx, dz)
    is_pos_z = forward_axis.upper().replace("POS", "+") in ("+Z", "Z")
    target_ry = (180.0 - travel_heading) % 360.0 if is_pos_z else (-travel_heading) % 360.0

    needs_update = False
    if rotate_y_op:
        curr = rotate_y_op.Get()
        if curr is None:
            needs_update = True
        else:
            deg = float(curr) % 360.0
            facing = (180.0 - deg) % 360.0 if is_pos_z else (-deg) % 360.0
            diff = abs((facing - travel_heading + 180.0) % 360.0 - 180.0)
            if diff > 90.0:  # Moving backwards!
                needs_update = True
    elif rotate_xyz_op:
        curr = rotate_xyz_op.Get()
        if curr is not None:
            deg = float(curr[1]) % 360.0
            facing = (180.0 - deg) % 360.0 if is_pos_z else (-deg) % 360.0
            diff = abs((facing - travel_heading + 180.0) % 360.0 - 180.0)
            if diff > 90.0:  # Moving backwards!
                needs_update = True
    else:
        needs_update = True

    if needs_update:
        stage.SetEditTarget(stage.GetRootLayer())
        prim.SetCustomDataByKey("forward_axis", forward_axis)
        if rotate_y_op:
            rotate_y_op.Set(float(target_ry))
        elif rotate_xyz_op and len(rotate_xyz_op.GetTimeSamples()) == 0:
            curr = rotate_xyz_op.Get() or Gf.Vec3f(0, 0, 0)
            rotate_xyz_op.Set(Gf.Vec3f(curr[0], float(target_ry), curr[2]))
        else:
            new_op = xformable.AddRotateYOp()
            new_op.Set(float(target_ry))
        stage.GetRootLayer().Save()
        return target_ry

    return None


def synthesize_stride_trajectory(
    shot_path: Path,
    character_path: str,
    trajectory_spec: dict,
    actions: list[dict],
    manifest: dict,
    fps: float,
) -> None:
    """Generate continuous, speed-matched translation keyframes matching clip strides."""
    stage = Usd.Stage.Open(str(shot_path.resolve()))
    if not stage:
        return
    prim = stage.GetPrimAtPath(character_path)
    if not prim or not prim.IsValid():
        return

    start_pos = trajectory_spec.get("start")
    if not start_pos or len(start_pos) != 3:
        return

    heading_deg = float(trajectory_spec.get("heading_deg", 0.0))
    rad = math.radians(heading_deg)
    # 0 = North (-Z), 90 = East (+X)
    dir_x = math.sin(rad)
    dir_z = -math.cos(rad)

    clips = manifest.get("clips", {})
    xformable = UsdGeom.Xformable(prim)
    translate_op = None
    for op in xformable.GetOrderedXformOps():
        if "translate" in op.GetOpName():
            translate_op = op
            break
    if not translate_op:
        translate_op = xformable.AddTranslateOp()

    current_pos = [float(start_pos[0]), float(start_pos[1]), float(start_pos[2])]
    stage.SetEditTarget(stage.GetRootLayer())
    translate_op.Set(Gf.Vec3d(*current_pos), 0)

    for action in actions:
        clip_name = action.get("clip")
        start_s = float(action.get("start_s", 0.0))
        end_s = float(action.get("end_s", start_s))
        dur_s = max(0.0, end_s - start_s)
        clip_info = clips.get(clip_name, {})
        speed = float(clip_info.get("stride_speed_mps", 1.4 if clip_info.get("strip_root_motion", False) else 0.0))
        dist = dur_s * speed

        start_frame = int(round(start_s * fps))
        end_frame = int(round(end_s * fps))
        translate_op.Set(Gf.Vec3d(*current_pos), start_frame)

        current_pos[0] += dir_x * dist
        current_pos[2] += dir_z * dist
        translate_op.Set(Gf.Vec3d(*current_pos), end_frame)

    stage.GetRootLayer().Save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shot", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--plan",
        required=True,
        type=Path,
        help='{"characters":[{"id","character_path","character_file","actions":[],"trajectory":{}}]}',
    )
    parser.add_argument("--fps", type=float, default=24)
    parser.add_argument(
        "--no-auto-orient",
        dest="auto_orient",
        action="store_false",
        default=True,
        help="Disable automatic orientation alignment to trajectory",
    )
    args = parser.parse_args()

    try:
        manifest_data = json.loads(args.manifest.read_text())
        plan = json.loads(args.plan.read_text())
        entries = plan["characters"]
        if not isinstance(entries, list):
            raise BakeError("characters must be an array")

        for entry in entries:
            identifier = entry.get("id")
            character_path = entry.get("character_path")
            character_file = entry.get("character_file")
            actions = entry.get("actions")
            traj_spec = entry.get("trajectory")

            if not all(isinstance(val, str) and val for val in (identifier, character_path, character_file)):
                raise BakeError("Each character needs non-empty id, character_path, and character_file")
            if not isinstance(actions, list):
                raise BakeError(f"Character {identifier} needs actions array")

            # 1. Synthesize trajectory if declared in plan
            if traj_spec and isinstance(traj_spec, dict):
                synthesize_stride_trajectory(args.shot, character_path, traj_spec, actions, manifest_data, args.fps)

            # 2. Persist actions_<id>.json
            actions_path = args.shot.parent / f"actions_{identifier}.json"
            actions_path.write_text(json.dumps({"fps": args.fps, "actions": actions}, indent=2) + "\n")
            output = args.shot.parent / f"anim_{identifier}.usda"

            character_asset = Path(character_file)
            if not character_asset.is_absolute():
                character_asset = args.manifest.parent / character_asset

            # 3. Bake multi-action SkelAnimation
            bake(character_asset, args.manifest, actions_path, output, args.fps, None)

            # 4. Bind animation into shot layer
            bind(args.shot, character_path, output)
            print(f"baked and bound {identifier}: {output}")

            # 5. Ensure character orientation faces its motion trajectory
            if args.auto_orient:
                st = Usd.Stage.Open(str(args.shot.resolve()))
                prim = st.GetPrimAtPath(character_path) if st else None
                fwd_axis = resolve_forward_axis(prim, character_file, manifest_data)
                aligned_deg = align_character_heading_to_motion(args.shot, character_path, fwd_axis)
                if aligned_deg is not None:
                    print(
                        f"aligned orientation: {character_path} (asset forward {fwd_axis}) "
                        f"rotated to rotateY = {aligned_deg:.1f}° to match forward motion trajectory"
                    )

    except (BakeError, ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"bake_shot_animations: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
