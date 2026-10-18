#!/usr/bin/env python3
"""Automatically scan assets/usdc_assets/ and synchronize manifest.json.

Inspects every OpenUSD asset (.usdc, .usda, .usd) under assets/usdc_assets/:
- Distinguishes skinless Mixamo animation clip files (which contain a
  UsdSkel.Animation and only __MixamoAnimationProxyMesh / no real meshes) and
  registers them in manifest["clips"].
- Computes 3D asset metadata for placeable assets and updates manifest["assets"]:
  id, path, default_prim, source_format, usd_reference_ready, tags, units,
  up_axis, forward_axis, bounds_m, pivot, parts, and variants.
- Preserves existing user-authored overrides (custom ids, notes, parts, etc.)
  and prunes entries whose files no longer exist on disk.
- Writes manifest.json only when changes are detected (idempotent).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pxr import Gf, Usd, UsdGeom, UsdLux, UsdSkel

HIDDEN_HELPER_NAMES = {"Camera", "Cube", "Light", "env_light"}
PROXY_MESH_PREFIX = "__MixamoAnimationProxyMesh"

LOOPING_CLIPS = {
    "walk", "run", "idle", "crawling", "swimming", "slow_jog_backwards",
    "jogging_with_box", "floating", "silly_dancing", "playing_drums",
    "praying", "cheering", "clapping", "sitting", "sitting_clap", "singing",
    "arm_stretching",
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

CATEGORY_BASE_TAGS: dict[str, list[str]] = {
    "environments": ["environment"],
    "cars": ["vehicle", "car"],
    "vehicles": ["vehicle"],
    "trees": ["tree", "nature", "foliage", "prop"],
    "humans": ["character", "human"],
    "characters": ["character"],
    "props": ["prop"],
    "buildings": ["building", "environment"],
}


def rnd(val: float, digits: int = 2) -> float:
    return round(float(val), digits) + 0.0


def is_light(prim: Usd.Prim) -> bool:
    return prim.HasAPI(UsdLux.LightAPI) or prim.GetTypeName().endswith("Light")


def is_demo_or_test_file(stem: str, ext: str) -> bool:
    lower = stem.lower()
    if ext == ".usda" and ("_demo" in lower or lower.endswith("demo")):
        return True
    if lower.startswith("anim_") or lower.endswith("_test"):
        return True
    return False


def is_skinless_animation_clip(stage: Usd.Stage) -> bool:
    """Return True if the stage is an animation-only clip (no real character mesh)."""
    skel_anims = [p for p in stage.Traverse() if p.IsA(UsdSkel.Animation)]
    if not skel_anims:
        return False
    real_meshes = [
        p for p in stage.Traverse()
        if p.IsA(UsdGeom.Mesh) and not p.GetName().startswith(PROXY_MESH_PREFIX)
    ]
    return len(real_meshes) == 0


def _axis_box(axis: str, radius: float, half_height: float) -> Gf.Range3d:
    half = [radius, radius, radius]
    idx = "XYZ".index(axis.upper()) if axis.upper() in "XYZ" else 2
    half[idx] = half_height
    return Gf.Range3d(Gf.Vec3d(*[-h for h in half]), Gf.Vec3d(*half))


def _gprim_extent(prim: Usd.Prim, time: Usd.TimeCode) -> Gf.Range3d | None:
    def get(name: str, default: float) -> float:
        attr = prim.GetAttribute(name)
        value = attr.Get(time) if attr else None
        return float(value) if value is not None else default

    axis = str(prim.GetAttribute("axis").Get() or "Z") if prim.GetAttribute("axis") else "Z"
    if prim.IsA(UsdGeom.Cube):
        half = get("size", 2.0) / 2.0
        return Gf.Range3d(Gf.Vec3d(-half, -half, -half), Gf.Vec3d(half, half, half))
    if prim.IsA(UsdGeom.Sphere):
        r = get("radius", 1.0)
        return Gf.Range3d(Gf.Vec3d(-r, -r, -r), Gf.Vec3d(r, r, r))
    if prim.IsA(UsdGeom.Cylinder) or prim.IsA(UsdGeom.Cone):
        return _axis_box(axis, get("radius", 1.0), get("height", 2.0) / 2.0)
    if prim.IsA(UsdGeom.Capsule):
        r = get("radius", 0.5)
        return _axis_box(axis, r, get("height", 1.0) / 2.0 + r)
    attr = UsdGeom.Boundable(prim).GetExtentAttr()
    extent = attr.Get(time) if attr.HasAuthoredValue() else None
    if not extent:
        extent = UsdGeom.Boundable(prim).ComputeExtent(time)
    if not extent and prim.IsA(UsdGeom.PointBased):
        points = UsdGeom.PointBased(prim).GetPointsAttr().Get(time)
        extent = UsdGeom.PointBased.ComputeExtent(points) if points else None
    if extent and len(extent) == 2:
        return Gf.Range3d(Gf.Vec3d(*extent[0]), Gf.Vec3d(*extent[1]))
    return None


def _skinned_boxes(
    root_prim: Usd.Prim,
    time: Usd.TimeCode,
    xf_cache: UsdGeom.XformCache,
) -> list[tuple[Gf.Range3d, Gf.Matrix4d]]:
    boxes: list[tuple[Gf.Range3d, Gf.Matrix4d]] = []
    try:
        cache = UsdSkel.Cache()
        root = UsdSkel.Root(root_prim)
        cache.Populate(root, Usd.TraverseInstanceProxies())
        for binding in cache.ComputeSkelBindings(root, Usd.TraverseInstanceProxies()):
            skel_query = cache.GetSkelQuery(binding.GetSkeleton())
            transforms = skel_query.ComputeSkinningTransforms(time)
            skel_world = xf_cache.GetLocalToWorldTransform(binding.GetSkeleton().GetPrim())
            for target in binding.GetSkinningTargets():
                mesh = target.GetPrim()
                if mesh.GetName().startswith(PROXY_MESH_PREFIX):
                    continue
                if UsdGeom.Imageable(mesh).ComputeVisibility(time) == UsdGeom.Tokens.invisible:
                    continue
                points = UsdGeom.PointBased(mesh).GetPointsAttr().Get(time)
                if not points or not target.ComputeSkinnedPoints(transforms, points, time):
                    continue
                extent = UsdGeom.PointBased.ComputeExtent(points)
                if extent and len(extent) == 2:
                    boxes.append((Gf.Range3d(Gf.Vec3d(*extent[0]), Gf.Vec3d(*extent[1])), skel_world))
    except Exception:  # noqa: BLE001
        return []
    return boxes


def compute_stage_bounds(stage: Usd.Stage, is_human: bool = False) -> Gf.Range3d:
    """Compute world-space aligned bounding box in stage units, ignoring helper prims."""
    time = Usd.TimeCode(1.0) if is_human else Usd.TimeCode.Default()
    xf_cache = UsdGeom.XformCache(time)
    purposes = {UsdGeom.Tokens.default_, UsdGeom.Tokens.render}
    boxes: list[tuple[Gf.Range3d, Gf.Matrix4d]] = []

    def visit(prim: Usd.Prim, depth: int = 0) -> None:
        if depth <= 2 and prim.GetName() in HIDDEN_HELPER_NAMES:
            return
        if prim.IsA(UsdGeom.Camera) or is_light(prim):
            return
        if prim.GetName().startswith(PROXY_MESH_PREFIX):
            return
        imageable = UsdGeom.Imageable(prim)
        if imageable:
            if imageable.GetVisibilityAttr().Get(time) == UsdGeom.Tokens.invisible:
                return
            purpose = imageable.GetPurposeAttr().Get()
            if purpose and purpose not in purposes:
                return
        if prim.IsA(UsdSkel.Root):
            skinned = _skinned_boxes(prim, time, xf_cache)
            if skinned:
                boxes.extend(skinned)
                return
        elif prim.IsA(UsdGeom.Gprim) or prim.IsA(UsdGeom.PointInstancer):
            rng = _gprim_extent(prim, time)
            if rng is not None and not rng.IsEmpty():
                boxes.append((rng, xf_cache.GetLocalToWorldTransform(prim)))
            return
        for child in prim.GetFilteredChildren(Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)):
            visit(child, depth + 1)

    for root_child in stage.GetPseudoRoot().GetFilteredChildren(Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)):
        visit(root_child, 1)

    total = Gf.Range3d()
    for rng, matrix in boxes:
        total.UnionWith(Gf.BBox3d(rng, matrix).ComputeAlignedRange())
    return total


def has_unresolved_local_references(stage: Usd.Stage, file_path: Path) -> bool:
    """Check if the root layer references a missing local file (e.g. broken .usda wrapper)."""
    root_layer = stage.GetRootLayer()
    base_dir = file_path.parent
    for ref in root_layer.externalReferences:
        if not ref:
            continue
        candidate = (base_dir / ref).resolve()
        if not candidate.exists():
            return True
    return False


def detect_parts(stage: Usd.Stage, up_axis: str) -> dict[str, dict[str, str]]:
    """Discover movable Xform parts (doors, hood, trunk, steering wheel) relative to defaultPrim."""
    default_prim = stage.GetDefaultPrim()
    if not default_prim or not default_prim.IsValid():
        children = stage.GetPseudoRoot().GetChildren()
        default_prim = children[0] if children else None
    if not default_prim:
        return {}

    root_path = default_prim.GetPath()
    door_axis = "Z" if up_axis == "Z" else "Y"
    lid_axis = "X"
    parts: dict[str, dict[str, str]] = {}

    for prim in Usd.PrimRange(default_prim):
        if prim == default_prim or not prim.IsA(UsdGeom.Xform):
            continue
        name = prim.GetName()
        lower = name.lower()
        rel_path = prim.GetPath().MakeRelativePath(root_path).pathString
        if not rel_path or "/" in rel_path and rel_path.count("/") > 2:
            continue

        # Match doors: Door_FL, Door_FR, Door_RL, Door_RR, door_l, door_r, DoorL, DoorR
        if re.match(r"^door_?(fl|fr|rl|rr|bl|br|l|r)$", lower):
            key = re.sub(r"^door_?", "door_", lower)
            parts[key] = {"prim": rel_path, "axis": door_axis}
        elif lower == "hood" or lower == "bonnet":
            parts["hood"] = {"prim": rel_path, "axis": lid_axis}
        elif lower == "trunk" or lower == "boot" or lower == "tailgate":
            parts["trunk"] = {"prim": rel_path, "axis": lid_axis}
        elif lower in ("steeringwheel", "steering_wheel"):
            parts["steering_wheel"] = {"prim": rel_path, "axis": "Z" if up_axis == "Y" else "Y"}

    return parts


def detect_variants(stage: Usd.Stage) -> dict[str, list[str]]:
    variants: dict[str, list[str]] = {}
    for prim in stage.Traverse():
        if prim.GetPath().pathElementCount > 2:
            continue
        vsets = prim.GetVariantSets()
        for name in vsets.GetNames():
            names = vsets.GetVariantSet(name).GetVariantNames()
            if names:
                variants[name] = list(names)
    return variants


def detect_forward_axis(stage: Usd.Stage, subdir: str, up_axis: str) -> str:
    default_prim = stage.GetDefaultPrim()
    if default_prim and default_prim.IsValid():
        custom = default_prim.GetCustomDataByKey("forward_axis")
        if custom and str(custom).strip():
            return str(custom).strip()

    if subdir in ("humans", "characters"):
        return "+Z"

    if subdir in ("cars", "vehicles"):
        # Check front vs rear wheel or hood vs trunk positions to determine whether front is -Z or +Z
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        front_z: list[float] = []
        rear_z: list[float] = []
        for prim in stage.Traverse():
            if not prim.IsA(UsdGeom.Xformable):
                continue
            low = prim.GetName().lower()
            if low in ("hood", "wheel_fl", "wheel_fr", "front_bumper", "headlight_l", "headlight_r"):
                rng = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
                if not rng.IsEmpty():
                    front_z.append(rng.GetMidpoint()[2 if up_axis == "Y" else 1])
            elif low in ("trunk", "wheel_rl", "wheel_rr", "wheel_bl", "wheel_br", "rear_bumper"):
                rng = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
                if not rng.IsEmpty():
                    rear_z.append(rng.GetMidpoint()[2 if up_axis == "Y" else 1])
        if front_z and rear_z and up_axis == "Y":
            avg_front = sum(front_z) / len(front_z)
            avg_rear = sum(rear_z) / len(rear_z)
            return "-Z" if avg_front < avg_rear else "+Z"
        return "+Z"

    return "-Z"


def build_tags(subdir: str, stem: str, stage: Usd.Stage) -> list[str]:
    tags: list[str] = []
    for t in CATEGORY_BASE_TAGS.get(subdir.lower(), [subdir.lower().rstrip("s") or subdir.lower()]):
        if t not in tags:
            tags.append(t)

    tokens = [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", stem) if w]
    stop_words = {"usdc", "usda", "usd", "asset", "model"}
    for tok in tokens:
        if tok not in stop_words and tok not in tags:
            tags.append(tok)

    has_skel = any(p.IsA(UsdSkel.Root) or p.IsA(UsdSkel.Skeleton) for p in stage.Traverse())
    has_anim = any(p.IsA(UsdSkel.Animation) for p in stage.Traverse())
    if has_skel and "rigged" not in tags:
        tags.append("rigged")
    if has_anim and "animated" not in tags:
        tags.append("animated")
    return tags


def describe_pivot(bounds: Gf.Range3d, up_axis: str, subdir: str, has_skel: bool) -> str:
    if has_skel and subdir in ("humans", "characters"):
        return "Armature root at origin; in Y-up stages with forward=-Z, rotateY = 180 to face North"
    if bounds.IsEmpty():
        return "Origin (0, 0, 0)"
    bmin = bounds.GetMin()
    up_idx = 2 if up_axis == "Z" else 1
    up_name = "z" if up_axis == "Z" else "y"
    height = max(1e-4, bounds.GetSize()[up_idx])
    if abs(bmin[up_idx]) <= 0.08 * height:
        return f"Base center at origin ({up_name}=0)"
    return f"Origin at {up_name}={rnd(bmin[up_idx])}m relative to base"


def inspect_asset_file(
    file_path: Path,
    subdir: str,
    project_root: Path,
    existing_entry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Open an asset stage and return its manifest dictionary (or None if not a valid 3D asset)."""
    stage = Usd.Stage.Open(str(file_path))
    if not stage:
        return None
    if has_unresolved_local_references(stage, file_path):
        return None
    if is_skinless_animation_clip(stage):
        return None

    rel_path = file_path.resolve().relative_to(project_root.resolve()).as_posix()
    stem = file_path.stem
    canonical_id = f"{subdir}.{stem}"

    up_axis = str(UsdGeom.GetStageUpAxis(stage) or "Y").upper()
    mpu = float(UsdGeom.GetStageMetersPerUnit(stage) or 1.0)
    is_human = subdir in ("humans", "characters")
    bounds = compute_stage_bounds(stage, is_human=is_human)
    if bounds.IsEmpty():
        return None

    raw_size = bounds.GetSize()
    # For Z-up rigged human characters where Armature is rotated 90 deg on X,
    # keep the Y-up stage standing bounds if already measured, or map Z-up height to Y.
    if up_axis == "Z" and is_human:
        bounds_m = [
            rnd(min(raw_size[0], raw_size[2]) * mpu),
            rnd(max(raw_size[1], raw_size[2]) * mpu),
            rnd(min(raw_size[1], raw_size[2]) * mpu),
        ]
    else:
        bounds_m = [rnd(raw_size[0] * mpu), rnd(raw_size[1] * mpu), rnd(raw_size[2] * mpu)]

    default_prim = stage.GetDefaultPrim()
    default_prim_name = default_prim.GetName() if (default_prim and default_prim.IsValid()) else None
    has_skel = any(p.IsA(UsdSkel.Root) or p.IsA(UsdSkel.Skeleton) for p in stage.Traverse())

    entry: dict[str, Any] = {}
    if existing_entry:
        entry.update(existing_entry)

    entry["id"] = existing_entry.get("id", canonical_id) if existing_entry else canonical_id
    if entry["id"] != canonical_id:
        aliases = list(entry.get("aliases", []))
        if canonical_id not in aliases:
            aliases.append(canonical_id)
        entry["aliases"] = aliases

    entry["path"] = rel_path
    if default_prim_name:
        entry["default_prim"] = default_prim_name
    entry["source_format"] = file_path.suffix.lstrip(".").upper()
    entry["usd_reference_ready"] = True
    entry["tags"] = existing_entry.get("tags") if (existing_entry and existing_entry.get("tags")) else build_tags(subdir, stem, stage)
    entry["units"] = "meters"
    entry["up_axis"] = up_axis
    entry["forward_axis"] = (
        existing_entry.get("forward_axis")
        if (existing_entry and existing_entry.get("forward_axis") not in (None, "", "unknown"))
        else detect_forward_axis(stage, subdir, up_axis)
    )
    entry["bounds_m"] = (
        existing_entry["bounds_m"]
        if (existing_entry and existing_entry.get("bounds_m") and is_human)
        else bounds_m
    )
    entry["pivot"] = (
        existing_entry["pivot"]
        if (existing_entry and existing_entry.get("pivot") and "unknown" not in str(existing_entry.get("pivot")).lower())
        else describe_pivot(bounds, up_axis, subdir, has_skel)
    )

    detected_parts = detect_parts(stage, up_axis)
    if detected_parts or (existing_entry and "parts" in existing_entry):
        merged_parts = dict(detected_parts)
        if existing_entry and isinstance(existing_entry.get("parts"), dict):
            merged_parts.update(existing_entry["parts"])
        if merged_parts:
            entry["parts"] = merged_parts

    detected_variants = detect_variants(stage)
    if existing_entry and isinstance(existing_entry.get("variants"), dict):
        merged_variants = dict(detected_variants)
        merged_variants.update(existing_entry["variants"])
        entry["variants"] = merged_variants
    else:
        entry["variants"] = detected_variants

    return entry


def sync_manifest(project_root: Path | None = None, manifest_path: Path | None = None, quiet: bool = True) -> dict[str, Any]:
    """Scan assets/usdc_assets/ and synchronize manifest.json in-place."""
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    project_root = project_root.resolve()

    if manifest_path is None:
        manifest_path = project_root / "manifest.json"
        if not manifest_path.is_file() and (project_root / "assets" / "manifest.json").is_file():
            manifest_path = project_root / "assets" / "manifest.json"

    original_text = ""
    if manifest_path.is_file():
        try:
            original_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(original_text)
        except (OSError, json.JSONDecodeError):
            manifest = {}
    else:
        manifest = {}

    manifest.setdefault("schema_version", "1.0")
    manifest.setdefault("units", "meters")
    manifest.setdefault("up_axis", "Y")
    manifest.setdefault("forward_axis", "-Z")
    clips: dict[str, Any] = manifest.setdefault("clips", {})
    existing_assets: list[dict[str, Any]] = manifest.get("assets", [])

    by_rel_path: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for item in existing_assets:
        p = item.get("path", "")
        if p:
            norm = p if p.startswith("assets/") else f"assets/{p}"
            by_rel_path[norm] = item
        if item.get("id"):
            by_id[item["id"]] = item

    # Remove stale clip entries whose files no longer exist on disk
    for clip_key in list(clips.keys()):
        clip_file = clips[clip_key].get("file", "") if isinstance(clips[clip_key], dict) else ""
        if clip_file and not (project_root / clip_file).is_file():
            del clips[clip_key]

    usdc_base = project_root / "assets" / "usdc_assets"
    synced_assets: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    if usdc_base.is_dir():
        subdirs = sorted(d for d in usdc_base.iterdir() if d.is_dir() and not d.name.startswith("."))
        # Put environments first, then humans, cars, trees, and any other folders for clean ordering
        priority = {"environments": 0, "humans": 1, "cars": 2, "vehicles": 3, "trees": 4}
        subdirs.sort(key=lambda d: (priority.get(d.name.lower(), 10), d.name.lower()))

        for cat_dir in subdirs:
            subdir = cat_dir.name
            usd_files = sorted(
                f for f in cat_dir.iterdir()
                if f.is_file() and f.suffix.lower() in {".usdc", ".usda", ".usd"}
            )
            # Prefer .usdc when both .usdc and .usda exist for the same stem
            usdc_stems = {f.stem for f in usd_files if f.suffix.lower() == ".usdc"}

            for f in usd_files:
                ext = f.suffix.lower()
                stem = f.stem
                if is_demo_or_test_file(stem, ext):
                    continue
                if ext == ".usda" and stem in usdc_stems:
                    continue

                rel_path = f.resolve().relative_to(project_root).as_posix()
                stage = Usd.Stage.Open(str(f))
                if not stage:
                    continue

                if is_skinless_animation_clip(stage):
                    clip_entry = dict(clips.get(stem, {}))
                    clip_entry["file"] = rel_path
                    clip_entry.setdefault("loop", stem in LOOPING_CLIPS)
                    if stem in LOCOMOTION_CLIPS:
                        clip_entry.setdefault("strip_root_motion", True)
                        clip_entry.setdefault("strip_root_axes", [0, 2])
                        clip_entry.setdefault("stride_speed_mps", LOCOMOTION_CLIPS[stem])
                    else:
                        clip_entry.setdefault("stride_speed_mps", 0.0)
                    clips[stem] = clip_entry
                    continue

                existing = by_rel_path.get(rel_path) or by_id.get(f"{subdir}.{stem}")
                entry = inspect_asset_file(f, subdir, project_root, existing)
                if entry is not None:
                    synced_assets.append(entry)
                    seen_paths.add(rel_path)

    manifest["assets"] = synced_assets
    new_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if new_text != original_text:
        manifest_path.write_text(new_text, encoding="utf-8")
        if not quiet:
            print(f"Updated {manifest_path} ({len(synced_assets)} assets, {len(clips)} clips)")
    elif not quiet:
        print(f"Manifest up to date: {manifest_path} ({len(synced_assets)} assets, {len(clips)} clips)")

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize manifest.json with assets/usdc_assets/")
    parser.add_argument("--project-root", type=Path, default=None, help="PainterSpace project root directory")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to manifest.json")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout status messages")
    args = parser.parse_args()
    sync_manifest(args.project_root, args.manifest, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())

