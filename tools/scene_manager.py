#!/usr/bin/env python3
"""
Scene Manager for PainterSpace
Handles main scene editing isolation, non-destructive snapshotting, and shot inheritance.

Rule:
- Existing shots remain visually and physically frozen to the scene as it was when authored (scene_vN.usda).
- New shots always inherit the current main scene (scene.usda).
- When the user saves edits to scene.usda, pre-edit scene.usda is snapshotted to scene_v<N>.usda
  and any existing shots sublayering scene.usda are retargeted to the snapshot.
"""

import os
import sys
import glob
import re
import shutil
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

from pxr import Sdf, Usd, Tf


def find_next_snapshot_version(scene_dir: Path) -> int:
    """Finds the next unused version number scene_v<N>.usda in scene_dir."""
    existing_versions = []
    for f in scene_dir.glob("scene_v*.usda"):
        m = re.match(r"^scene_v(\d+)\.usda$", f.name)
        if m:
            existing_versions.append(int(m.group(1)))
    return max(existing_versions, default=0) + 1


def get_shot_sublayer_scene_reference(shot_usda_path: Path) -> Optional[str]:
    """
    Checks if shot.usda references scene.usda (e.g. '../scene.usda' or '@../scene.usda@').
    Returns the exact matching sublayer string if found, otherwise None.
    """
    if not shot_usda_path.exists():
        return None
    try:
        shot_layer = Sdf.Layer.FindOrOpen(str(shot_usda_path))
        if shot_layer:
            shot_layer.Reload()
            for p in shot_layer.subLayerPaths:
                clean = p.strip("@")
                if clean == "../scene.usda" or clean.endswith("/scene.usda"):
                    return p
    except Exception:
        pass

    # Fallback to text inspection
    text = shot_usda_path.read_text(encoding="utf-8")
    if "../scene.usda" in text:
        return "../scene.usda"
    return None


def retarget_shot_sublayer(shot_usda_path: Path, old_sublayer: str, new_sublayer: str) -> bool:
    """Retargets a shot's sublayer from old_sublayer to new_sublayer."""
    if not shot_usda_path.exists():
        return False
    try:
        shot_layer = Sdf.Layer.FindOrOpen(str(shot_usda_path))
        if shot_layer:
            shot_layer.Reload()
            paths = list(shot_layer.subLayerPaths)
            modified = False
            for i, p in enumerate(paths):
                clean = p.strip("@")
                if clean == "../scene.usda" or clean == old_sublayer.strip("@"):
                    shot_layer.subLayerPaths[i] = new_sublayer
                    modified = True
            if modified:
                shot_layer.Save()
                return True
    except Exception:
        pass

    # Direct textual replacement fallback
    text = shot_usda_path.read_text(encoding="utf-8")
    new_text = re.sub(
        r"@\.\./scene\.usda@",
        f"@{new_sublayer}@",
        text
    )
    if new_text != text:
        shot_usda_path.write_text(new_text, encoding="utf-8")
        return True
    return False


def snapshot_scene_for_existing_shots(scene_dir_path: str) -> Optional[Dict[str, Any]]:
    """
    Scans scene_dir for existing shots that sublayer '../scene.usda'.
    If found, snapshots the current scene.usda to scene_v<N>.usda and retargets
    those shots to sublayer the new snapshot.
    """
    scene_dir = Path(scene_dir_path).resolve()
    scene_usda = scene_dir / "scene.usda"
    if not scene_usda.exists():
        return None

    # Find shots that sublayer ../scene.usda
    shots_to_retarget = []
    for shot_dir in sorted(scene_dir.glob("shot_*")):
        if not shot_dir.is_dir():
            continue
        shot_usda = shot_dir / "shot.usda"
        ref = get_shot_sublayer_scene_reference(shot_usda)
        if ref:
            shots_to_retarget.append((shot_dir.name, shot_usda, ref))

    if not shots_to_retarget:
        return None  # No existing shots depending on current unedited scene.usda

    version_num = find_next_snapshot_version(scene_dir)
    snapshot_filename = f"scene_v{version_num}.usda"
    snapshot_path = scene_dir / snapshot_filename

    # Copy current scene.usda to snapshot
    shutil.copy2(scene_usda, snapshot_path)

    # Retarget shots to snapshot
    new_sublayer_rel = f"../{snapshot_filename}"
    retargeted_shots = []
    for shot_name, shot_usda, old_ref in shots_to_retarget:
        if retarget_shot_sublayer(shot_usda, old_ref, new_sublayer_rel):
            retargeted_shots.append(shot_name)

    return {
        "snapshot_version": version_num,
        "snapshot_filename": snapshot_filename,
        "snapshot_path": str(snapshot_path),
        "retargeted_shots": retargeted_shots
    }


def merge_user_edits_into_scene_layer(user_edits_layer: Sdf.Layer, scene_layer: Sdf.Layer) -> int:
    """
    Non-destructively merges user opinions from user_edits_layer into scene_layer.
    Preserves existing references and definitions.
    Returns number of prims merged.
    """
    merged_count = 0

    def merge_prim(src_prim: Sdf.PrimSpec):
        nonlocal merged_count
        dst_prim = scene_layer.GetPrimAtPath(src_prim.path)
        if not dst_prim:
            parent_path = src_prim.path.GetParentPath()
            if not scene_layer.GetPrimAtPath(parent_path):
                Sdf.CreatePrimInLayer(scene_layer, parent_path)
            Sdf.CreatePrimInLayer(scene_layer, src_prim.path)
            Sdf.CopySpec(user_edits_layer, src_prim.path, scene_layer, src_prim.path)
            merged_count += 1
            return

        # Prim already exists: merge properties, attributes, and customData/active metadata
        has_opinions = False
        for prop in src_prim.properties:
            Sdf.CopySpec(user_edits_layer, prop.path, scene_layer, prop.path)
            has_opinions = True

        for key in src_prim.ListInfoKeys():
            if key in ["customData", "active"]:
                dst_prim.SetInfo(key, src_prim.GetInfo(key))
                has_opinions = True

        if has_opinions:
            merged_count += 1

        for child in src_prim.nameChildren:
            merge_prim(child)

    world = user_edits_layer.GetPrimAtPath("/World")
    if world:
        for child in world.nameChildren:
            merge_prim(child)

    return merged_count


def save_scene_edits(scene_dir_path: str, rebuild_world_state: bool = True) -> Dict[str, Any]:
    """
    Saves and commits user_edits.usda into scene.usda in scene_dir_path.
    1. Snapshots pre-edit scene.usda if any existing shots depend on it.
    2. Merges user_edits.usda opinions into scene.usda.
    3. Resets user_edits.usda to an empty layer.
    4. Rebuilds world state and thumbnail if requested.
    """
    scene_dir = Path(scene_dir_path).resolve()
    scene_usda = scene_dir / "scene.usda"
    user_edits_usda = scene_dir / "user_edits.usda"

    if not scene_usda.exists():
        raise FileNotFoundError(f"scene.usda not found in {scene_dir}")

    user_edits_layer = Sdf.Layer.FindOrOpen(str(user_edits_usda)) if user_edits_usda.exists() else None
    if not user_edits_layer:
        return {"status": "no_edits", "message": "user_edits.usda does not exist"}
    user_edits_layer.Reload()

    # Count authored opinions in user_edits_layer
    world = user_edits_layer.GetPrimAtPath("/World")
    if not world or len(world.nameChildren) == 0:
        return {"status": "clean", "message": "No user edits to commit", "prims_merged": 0}

    # 1. Snapshot scene.usda for existing shots before modifying it
    snapshot_info = snapshot_scene_for_existing_shots(str(scene_dir))

    # 2. Open scene.usda and merge opinions
    scene_layer = Sdf.Layer.FindOrOpen(str(scene_usda))
    if not scene_layer:
        raise RuntimeError(f"Failed to open scene.usda at {scene_usda}")
    scene_layer.Reload()

    prims_merged = merge_user_edits_into_scene_layer(user_edits_layer, scene_layer)
    scene_layer.Save()

    # 3. Reset user_edits.usda
    clean_template = (
        "#usda 1.0\n"
        "(\n"
        '    doc = "PainterSpace manual user edits layer"\n'
        ")\n\n"
        'over "World"\n'
        "{\n"
        "}\n"
    )
    user_edits_usda.write_text(clean_template, encoding="utf-8")
    user_edits_layer.Reload()

    # 4. Update scene.json metadata
    scene_json_path = scene_dir / "scene.json"
    if scene_json_path.exists():
        try:
            data = json.loads(scene_json_path.read_text(encoding="utf-8"))
            if snapshot_info:
                snapshots = data.get("snapshots", [])
                snapshots.append({
                    "version": snapshot_info["snapshot_version"],
                    "file": snapshot_info["snapshot_filename"],
                    "shots": snapshot_info["retargeted_shots"]
                })
                data["snapshots"] = snapshots
            data["configured"] = True
            data["last_scene_edit_saved"] = True
            scene_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 5. Optionally rebuild world state
    if rebuild_world_state:
        world_state_tool = Path(__file__).resolve().parent / "world_state.py"
        if world_state_tool.exists():
            import subprocess
            subprocess.run(
                [sys.executable, str(world_state_tool), "build", str(scene_usda)],
                cwd=str(scene_dir.parent.parent),
                capture_output=True,
                check=False
            )

    return {
        "status": "success",
        "prims_merged": prims_merged,
        "snapshot": snapshot_info
    }


def get_scene_status(scene_dir_path: str) -> Dict[str, Any]:
    """Returns overview of scene.usda, user edits, existing shots, and snapshots."""
    scene_dir = Path(scene_dir_path).resolve()
    scene_usda = scene_dir / "scene.usda"
    user_edits_usda = scene_dir / "user_edits.usda"

    edits_count = 0
    if user_edits_usda.exists():
        layer = Sdf.Layer.FindOrOpen(str(user_edits_usda))
        if layer:
            world = layer.GetPrimAtPath("/World")
            if world:
                edits_count = len(world.nameChildren)

    snapshots = [f.name for f in sorted(scene_dir.glob("scene_v*.usda"))]

    shots = []
    for shot_dir in sorted(scene_dir.glob("shot_*")):
        shot_usda = shot_dir / "shot.usda"
        ref = get_shot_sublayer_scene_reference(shot_usda)
        shots.append({
            "shot": shot_dir.name,
            "sublayer_ref": ref or "unknown"
        })

    return {
        "scene_usda_exists": scene_usda.exists(),
        "uncommitted_edits_count": edits_count,
        "snapshots": snapshots,
        "shots": shots
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PainterSpace Scene Manager")
    parser.add_argument("action", choices=["status", "snapshot", "save"], help="Action to perform")
    parser.add_argument("scene_dir", help="Path to scene directory (e.g. scenes/scene_02)")
    parser.add_argument("--no-world-state", action="store_true", help="Skip rebuilding world state on save")
    args = parser.parse_args()

    target_dir = args.scene_dir
    if args.action == "status":
        res = get_scene_status(target_dir)
        print(json.dumps(res, indent=2))
    elif args.action == "snapshot":
        res = snapshot_scene_for_existing_shots(target_dir)
        print(json.dumps(res or {"message": "No snapshot needed"}, indent=2))
    elif args.action == "save":
        res = save_scene_edits(target_dir, rebuild_world_state=not args.no_world_state)
        print(json.dumps(res, indent=2))
