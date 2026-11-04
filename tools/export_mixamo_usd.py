#!/usr/bin/env python3
"""Export one skinned Mixamo character and skinless FBX actions to OpenUSD.

Run through Blender, not Python::

  blender --background --python tools/export_mixamo_usd.py -- \
    --character "assets/usdc_assets/humans/Idle with skin.fbx" \
    --all-clips --manifest manifest.json

``--character`` must be the one FBX downloaded *With Skin*.  ``--all-clips``
selects every ``*without skin*.fbx`` beside it; alternatively supply one or
more ``--clip`` paths.  Each clip is imported into a fresh scene and exported
with armatures and animation, but without meshes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import bpy
from mathutils import Vector


LOOPING_CLIPS = {"walk", "run", "idle"}


def blender_args() -> list[str]:
    """Return only options following Blender's required ``--`` separator."""
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in list(bpy.data.collections):
        if collection.users == 0:
            bpy.data.collections.remove(collection)


def import_fbx(path: Path) -> tuple[list[bpy.types.Object], list[bpy.types.Object]]:
    clear_scene()
    bpy.ops.import_scene.fbx(filepath=str(path), use_anim=True)
    objects = list(bpy.context.scene.objects)
    armatures = [obj for obj in objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if len(armatures) != 1:
        raise RuntimeError(f"{path.name}: expected exactly one armature, found {len(armatures)}")
    return armatures, meshes


def select(objects: list[bpy.types.Object]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def operator_kwargs(**requested: object) -> dict[str, object]:
    """Only use exporter options present in this Blender version."""
    properties = bpy.ops.wm.usd_export.get_rna_type().properties
    names = {prop.identifier for prop in properties}
    return {name: value for name, value in requested.items() if name in names}


def export_usdc(path: Path, objects: list[bpy.types.Object], *, animation: bool, meshes: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    select(objects)
    options = operator_kwargs(
        filepath=str(path),
        selected_objects_only=True,
        visible_objects_only=True,
        export_animation=animation,
        export_armatures=True,
        export_meshes=meshes,
        export_materials=meshes,
        export_uvmaps=meshes,
        export_normals=meshes,
        export_hair=False,
        export_lights=False,
        export_cameras=False,
    )
    result = bpy.ops.wm.usd_export(**options)
    if "FINISHED" not in result or not path.is_file():
        raise RuntimeError(f"USD export failed: {path}")


def action_range(armature: bpy.types.Object) -> tuple[int, int]:
    action = armature.animation_data.action if armature.animation_data else None
    if not action:
        raise RuntimeError(f"{armature.name}: no imported animation action")
    start, end = action.frame_range
    return int(start // 1), int(-(-end // 1))  # floor / ceil without float rounding surprises


def character_height(meshes: list[bpy.types.Object]) -> float | None:
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    return max(point.z for point in points) - min(point.z for point in points) if points else None


def skelroot_proxy(armature: bpy.types.Object) -> bpy.types.Object:
    """Make Blender emit a valid SkelRoot for an otherwise mesh-free action.

    Blender 5.x applies SkelBindingAPI to an armature-only USD export but does
    not create a SkelRoot, which ``usdchecker`` rejects.  A microscopic,
    skinned triangle causes the exporter to author the proper hierarchy.  The
    baker ignores meshes, so this has no effect on the resulting performance.
    """
    mesh = bpy.data.meshes.new("__MixamoAnimationProxyMesh")
    mesh.from_pydata([(0.0, 0.0, 0.0), (0.0001, 0.0, 0.0), (0.0, 0.0001, 0.0)], [], [(0, 1, 2)])
    proxy = bpy.data.objects.new("__MixamoAnimationProxyMesh", mesh)
    bpy.context.scene.collection.objects.link(proxy)
    root_bone = armature.data.bones[0].name
    group = proxy.vertex_groups.new(name=root_bone)
    group.add([0, 1, 2], 1.0, "REPLACE")
    modifier = proxy.modifiers.new("MixamoArmature", "ARMATURE")
    modifier.object = armature
    return proxy


def clip_name(source: Path) -> str:
    stem = re.sub(r"\b(?:with|without|wihtout)\s+skin\b", "", source.stem, flags=re.I)
    stem = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    aliases = {"standard_walk": "walk", "walking": "walk", "running": "run"}
    return aliases.get(stem, stem)


LOOPING_CLIPS = {
    "walk", "run", "idle", "crawling", "swimming", "slow_jog_backwards",
    "jogging_with_box", "floating", "silly_dancing", "playing_drums",
    "praying", "cheering", "clapping", "sitting", "sitting_clap", "singing", "arm_stretching"
}

LOCOMOTION_CLIPS = {
    "walk": 1.4,
    "run": 4.2,
    "slow_jog_backwards": 1.8,
    "jogging_with_box": 2.2,
    "crawling": 0.8,
    "swimming": 1.0,
    "climbing": 0.6,
    "sad_walk_arc_right": 1.2,
    "start_walking": 1.2,
    "jump": 2.4,
}


def is_looping_clip(name: str) -> bool:
    if name in LOOPING_CLIPS:
        return True
    lower = name.lower()
    return any(k in lower for k in [
        "idle", "walk", "run", "jog", "crawl", "swim", "dance", "dancing",
        "sitting", "meeting", "listen", "float", "pray", "cheer", "clap",
        "stretch", "sing", "bored", "sleep", "typing", "gaming", "smoking"
    ])


def get_locomotion_stride(name: str) -> float | None:
    if name in LOCOMOTION_CLIPS:
        return LOCOMOTION_CLIPS[name]
    lower = name.lower()
    if "fast_run" in lower or "sprint" in lower:
        return 5.5
    if "run" in lower:
        return 4.0
    if "jog" in lower:
        return 2.5
    if "sneak" in lower:
        return 1.0
    if "crawl" in lower:
        return 0.8
    if "climb" in lower:
        return 0.6
    if "stairs" in lower:
        return 0.9
    if "walk" in lower:
        return 1.4
    return None


def is_skinless_fbx(path: Path) -> bool:
    if path.suffix.lower() != ".fbx":
        return False
    return not bool(re.search(r"\bwith\s+skin\b", path.name, re.I))


def update_manifest(manifest_path: Path, output_dir: Path, clips: dict[str, Path], scale: float) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    library = manifest.setdefault("clips", {})
    project_root = manifest_path.parent.resolve()
    for name, output in clips.items():
        rel_file = output.resolve().relative_to(project_root).as_posix()
        existing = library.get(name, {})
        entry: dict[str, object] = dict(existing)
        entry["file"] = rel_file
        entry["loop"] = is_looping_clip(name)
        if scale != 1.0:
            entry["translation_scale"] = scale
        stride = get_locomotion_stride(name)
        if stride is not None:
            entry.setdefault("strip_root_motion", True)
            entry.setdefault("strip_root_axes", [0, 2])
            entry.setdefault("stride_speed_mps", stride)
        else:
            entry.setdefault("stride_speed_mps", 0.0)
        library[name] = entry
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--character", required=False, type=Path, help="the one Mixamo FBX downloaded With Skin")
    parser.add_argument("--clip", action="append", type=Path, default=[], help="a Mixamo FBX downloaded Without Skin")
    parser.add_argument("--all-clips", action="store_true", help="export every FBX clip in output directory")
    parser.add_argument("--skip-existing", action="store_true", help="skip export if destination .usdc already exists")
    parser.add_argument("--output-dir", type=Path, default=Path("assets/usdc_assets/human_actions"), help="default: assets/usdc_assets/human_actions")
    parser.add_argument("--manifest", type=Path, help="update its top-level clips object")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--translation-scale", type=float, default=1.0,
                        help="use 0.01 only after confirming clip translations are centimetres")
    args = parser.parse_args(blender_args())
    if args.fps <= 0:
        parser.error("--fps must be positive")
    output_dir = args.output_dir.resolve()
    clips = list(args.clip)
    if args.all_clips:
        clips.extend(sorted(path for path in output_dir.iterdir() if is_skinless_fbx(path)))
    clip_sources = list(dict.fromkeys(path.resolve() for path in clips))
    if not clip_sources:
        parser.error("provide --clip or --all-clips")
    if any(not path.is_file() for path in clip_sources):
        parser.error("one or more clip FBX files do not exist")

    character_output = (output_dir.parent / "humans" / "human.usdc") if output_dir.name == "human_actions" else (output_dir / "human.usdc")
    height = None
    if args.character:
        character = args.character.resolve()
        if not character.is_file():
            parser.error(f"character FBX not found: {character}")
        scene = bpy.context.scene
        scene.render.fps = args.fps
        armatures, meshes = import_fbx(character)
        height = character_height(meshes)
        if height is None:
            raise RuntimeError("character FBX contains no mesh; it must be the With Skin download")
        if not 1.2 <= height <= 2.2:
            print(f"warning: character is {height:.3f} Blender units tall (expected about 1.7)", file=sys.stderr)
        export_usdc(character_output, armatures + meshes, animation=False, meshes=True)
    elif character_output.is_file():
        print(f"Using existing character: {character_output}")
    else:
        parser.error("Provide --character or ensure human.usdc exists in output-dir")

    scene = bpy.context.scene
    exported: dict[str, Path] = {}
    report: dict[str, object] = {
        "fps": args.fps,
        "character": {"source": str(args.character) if args.character else "existing", "output": str(character_output), "height": height},
        "clips": {}
    }
    for source in clip_sources:
        name = clip_name(source)
        if not name:
            print(f"Warning: Could not derive clip name from {source.name}", file=sys.stderr)
            continue
        output = output_dir / f"{name}.usdc"
        if args.skip_existing and output.is_file() and output.stat().st_size > 0:
            print(f"Skipping already exported clip: {name}")
            exported[name] = output
            continue

        try:
            armatures, meshes = import_fbx(source)
            if not armatures:
                print(f"Warning: {source.name} contains no armature; skipping", file=sys.stderr)
                continue
            if meshes:
                print(f"warning: {source.name} contains meshes; excluding them from clip export", file=sys.stderr)
            start, end = action_range(armatures[0])
            scene.render.fps = args.fps
            scene.frame_start, scene.frame_end = start, end
            # See skelroot_proxy: this is needed only because current Blender
            # versions otherwise export invalid armature-only UsdSkel bindings.
            proxy = skelroot_proxy(armatures[0])
            export_usdc(output, armatures + [proxy], animation=True, meshes=True)
            exported[name] = output
            report["clips"][name] = {"source": str(source), "output": str(output), "frame_range": [start, end]}
            print(f"Exported clip {name}: {output}")
        except Exception as err:
            print(f"Error exporting {source.name}: {err}", file=sys.stderr)

    if args.manifest:
        update_manifest(args.manifest.resolve(), output_dir, exported, args.translation_scale)
    (output_dir / "mixamo_export_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Exported character: {character_output}")
    print(f"Total clips processed: {len(exported)}")
    print("Validate with: usdcat <file>.usdc | grep -E 'SkelAnimation|Skeleton|joints|timeSamples'")


if __name__ == "__main__":
    main()
