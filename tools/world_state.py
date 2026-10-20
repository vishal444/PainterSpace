#!/usr/bin/env python3
"""Build the PainterSpace world state record: what is in the scene right now.

USD is the source of truth. This tool evaluates a composed stage (shot.usda over
scene.usda) at one frame and writes a generated, human-readable JSON summary for
the LLM: object positions, headings, sizes, world bounds, zones, the camera, and
derived facts (relations, overlaps, free space, camera visibility, groups).
Every number is computed from the real stage; nothing here is authored by hand.

Requires the OpenUSD Python bindings (`pip install usd-core`). Pillow is optional
and only needed for the top-down map image.

Commands
    build STAGE [--frame start|end|N] [--output JSON] [--topdown PNG] [--camera-image PNG]
    prepare-shot scenes/scene_XX/shot_XXX/shot.usda   -> world_state.json + state_start.json
    finalize-shot scenes/scene_XX/shot_XXX/shot.usda  -> state_end.json

Conventions (must match the USD stages): meters, Y up, north = -Z, east = +X.
heading_deg: 0 = facing north (-Z), 90 = east (+X), 180 = south, 270 = west;
increases clockwise when seen from above. Note that a positive USD rotateY turns
an object counter-clockwise from above, so heading_deg = -rotateY (mod 360).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdSkel
except ImportError:  # pragma: no cover - environment guard
    print(
        "world_state.py needs the OpenUSD Python bindings. Install them with\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(3)

SCHEMA_VERSION = "1.0"
DECIMALS = 2
DEFAULT_MAX_OBJECTS = 40
GROUP_MIN_COUNT = 4
RELATION_RADIUS_M = 15.0
MAX_RELATIONS = 40
FACING_TOLERANCE_DEG = 30.0
ENVIRONMENT_FOOTPRINT_M2 = 40.0
ENVIRONMENT_NAMES = {"ground", "floor", "terrain", "sky", "environment"}
OVERLAP_TOLERANCE_M = 0.02
GROUND_TOLERANCE_M = 0.05
MOVING_SPEED_MPS = 0.05
FREE_SPACE_MIN_AREA_M2 = 1.0
ZONES_SCOPE = "Zones"
COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
AXIS_VECTORS = {
    "-Z": Gf.Vec3d(0, 0, -1),
    "+Z": Gf.Vec3d(0, 0, 1),
    "Z": Gf.Vec3d(0, 0, 1),
    "-X": Gf.Vec3d(-1, 0, 0),
    "+X": Gf.Vec3d(1, 0, 0),
    "X": Gf.Vec3d(1, 0, 0),
}


# --------------------------------------------------------------------------- helpers


def rnd(value: float) -> float:
    return round(float(value), DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def rvec(values) -> list[float]:
    return [rnd(v) for v in values]


def compass(heading_deg: float) -> str:
    return COMPASS[int(((heading_deg % 360) + 22.5) // 45) % 8]


def heading_of(dx: float, dz: float) -> float | None:
    """Heading of a world-space XZ direction: 0 = north (-Z), clockwise from above."""
    if math.hypot(dx, dz) < 1e-6:
        return None
    return math.degrees(math.atan2(dx, -dz)) % 360


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def range_valid(rng: Gf.Range3d) -> bool:
    return not rng.IsEmpty() and all(abs(c) < 1e30 for c in (*rng.GetMin(), *rng.GetMax()))


def range_gap(a: Gf.Range3d, b: Gf.Range3d) -> float:
    gaps = [max(0.0, a.GetMin()[i] - b.GetMax()[i], b.GetMin()[i] - a.GetMax()[i]) for i in range(3)]
    return math.sqrt(sum(g * g for g in gaps))


def range_overlap(a: Gf.Range3d, b: Gf.Range3d) -> list[float]:
    return [min(a.GetMax()[i], b.GetMax()[i]) - max(a.GetMin()[i], b.GetMin()[i]) for i in range(3)]


def find_project_root(start: Path) -> Path:
    directory = start.resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / "manifest.json").is_file() or (candidate / "assets" / "manifest.json").is_file():
            return candidate
    return start.resolve()


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def shot_number(path: Path) -> int:
    match = re.search(r"(\d+)$", path.name)
    return int(match.group(1)) if match else 0


def is_light(prim: Usd.Prim) -> bool:
    return prim.HasAPI(UsdLux.LightAPI) or prim.GetTypeName().endswith("Light")


# --------------------------------------------------------------------------- manifest


class Manifest:
    """Asset library lookups: by id and by resolved file path."""

    def __init__(self, project_root: Path):
        self.by_id: dict[str, dict] = {}
        self.by_file: dict[str, dict] = {}
        try:
            from sync_manifest import sync_manifest
            sync_manifest(project_root=project_root, quiet=True)
        except Exception:  # noqa: BLE001 - continue with existing manifest if sync fails
            pass
        path = project_root / "manifest.json"
        if not path.is_file():
            path = project_root / "assets" / "manifest.json"
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        assets_root = project_root / "assets"
        for item in data.get("assets", []):
            for key in ("path", "source_path"):
                rel_p = item.get(key, "")
                if rel_p:
                    asset_path = Path(rel_p)
                    full = project_root / asset_path if asset_path.parts[:1] == ("assets",) else assets_root / asset_path
                    self.by_file[os.path.realpath(full)] = item
            if item.get("id"):
                self.by_id[item["id"]] = item
            for alias in item.get("aliases", []):
                if alias:
                    self.by_id[str(alias)] = item

    def lookup(self, prim: Usd.Prim) -> tuple[str | None, dict | None, str | None]:
        """Return (asset_id, manifest_entry, referenced_file) for an object prim."""
        explicit = prim.GetCustomDataByKey("asset_id")
        referenced = None
        for spec in prim.GetPrimStack():
            for ref in spec.referenceList.GetAddedOrExplicitItems():
                if not ref.assetPath:
                    continue
                try:
                    resolved = spec.layer.ComputeAbsolutePath(ref.assetPath)
                except Exception:  # noqa: BLE001 - fall back to a plain join
                    resolved = os.path.join(os.path.dirname(spec.layer.realPath), ref.assetPath)
                referenced = os.path.realpath(resolved)
                break
            if referenced:
                break
        if explicit:
            aid = str(explicit)
            entry = self.by_id.get(aid)
            if not entry:
                for key, cand in self.by_id.items():
                    if aid.startswith(key + "."):
                        entry = cand
                        break
            if not entry and referenced and referenced in self.by_file:
                entry = self.by_file[referenced]
            return aid, entry, referenced
        if referenced and referenced in self.by_file:
            entry = self.by_file[referenced]
            return entry.get("id"), entry, referenced
        return None, None, referenced


# --------------------------------------------------------------------------- geometry


def _axis_box(axis: str, radius: float, half_height: float) -> Gf.Range3d:
    half = [radius, radius, radius]
    half["XYZ".index(axis)] = half_height
    return Gf.Range3d(Gf.Vec3d(*[-h for h in half]), Gf.Vec3d(*half))


def gprim_extent(prim: Usd.Prim, time: Usd.TimeCode) -> Gf.Range3d | None:
    """Object-space extent of one gprim, preferring its own shape attributes."""
    def get(name: str, default: float) -> float:
        attr = prim.GetAttribute(name)
        value = attr.Get(time) if attr else None
        return float(value) if value is not None else default

    axis = str(prim.GetAttribute("axis").Get() or "Z") if prim.GetAttribute("axis") else "Z"
    if prim.IsA(UsdGeom.Cube):
        half = get("size", 2.0) / 2
        return Gf.Range3d(Gf.Vec3d(-half, -half, -half), Gf.Vec3d(half, half, half))
    if prim.IsA(UsdGeom.Sphere):
        r = get("radius", 1.0)
        return Gf.Range3d(Gf.Vec3d(-r, -r, -r), Gf.Vec3d(r, r, r))
    if prim.IsA(UsdGeom.Cylinder) or prim.IsA(UsdGeom.Cone):
        return _axis_box(axis, get("radius", 1.0), get("height", 2.0) / 2)
    if prim.IsA(UsdGeom.Capsule):
        r = get("radius", 0.5)
        return _axis_box(axis, r, get("height", 1.0) / 2 + r)
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


def skinned_boxes(
    root_prim: Usd.Prim,
    time: Usd.TimeCode,
    xf_cache: UsdGeom.XformCache,
    include_hidden: bool,
) -> list[tuple[Gf.Range3d, Gf.Matrix4d]]:
    """Posed bounds of skinned meshes under a SkelRoot (skeleton space -> world).

    The SkelRoot extent hint is padded around joints and is much larger than the
    character, so the actual skinned points are evaluated instead.
    """
    boxes = []
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
                if not include_hidden and UsdGeom.Imageable(mesh).ComputeVisibility(time) == UsdGeom.Tokens.invisible:
                    continue
                points = UsdGeom.PointBased(mesh).GetPointsAttr().Get(time)
                if not points or not target.ComputeSkinnedPoints(transforms, points, time):
                    continue
                extent = UsdGeom.PointBased.ComputeExtent(points)
                if extent and len(extent) == 2:
                    boxes.append((Gf.Range3d(Gf.Vec3d(*extent[0]), Gf.Vec3d(*extent[1])), skel_world))
    except Exception as exc:  # noqa: BLE001 - fall back to child extents
        print(f"world_state: skinning failed for {root_prim.GetPath()}: {exc}", file=sys.stderr)
        return []
    return boxes


def collect_boxes(
    prim: Usd.Prim,
    time: Usd.TimeCode,
    xf_cache: UsdGeom.XformCache,
    include_hidden: bool = False,
) -> list[tuple[Gf.Range3d, Gf.Matrix4d]]:
    """Leaf extents with their world matrices.

    UsdGeom.BBoxCache only uses authored extents, and the schema fallback for an
    unauthored `Cube` extent is (-1..1) regardless of `size`. Implicit shapes are
    therefore measured from their own attributes, skinned characters from their
    posed (skinned) points, and everything else from authored/computed extents.
    """
    purposes = {UsdGeom.Tokens.default_, UsdGeom.Tokens.render}
    if include_hidden:
        purposes |= {UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy}
    boxes: list[tuple[Gf.Range3d, Gf.Matrix4d]] = []

    def visit(current: Usd.Prim) -> None:
        if current.IsA(UsdGeom.Camera) or is_light(current):
            return
        imageable = UsdGeom.Imageable(current)
        if imageable:
            if not include_hidden and imageable.GetVisibilityAttr().Get(time) == UsdGeom.Tokens.invisible:
                return
            if imageable.GetPurposeAttr().Get() not in purposes:
                return
        if current.IsA(UsdSkel.Root):
            skinned = skinned_boxes(current, time, xf_cache, include_hidden)
            if skinned:
                boxes.extend(skinned)
                return
        elif current.IsA(UsdGeom.Gprim) or current.IsA(UsdGeom.PointInstancer):
            rng = gprim_extent(current, time)
            if rng is not None and range_valid(rng):
                boxes.append((rng, xf_cache.GetLocalToWorldTransform(current)))
            return
        for child in current.GetFilteredChildren(Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)):
            visit(child)

    if include_hidden or UsdGeom.Imageable(prim).ComputeVisibility(time) != UsdGeom.Tokens.invisible:
        visit(prim)
    return boxes


def union_world(boxes) -> Gf.Range3d:
    total = Gf.Range3d()
    for rng, matrix in boxes:
        total.UnionWith(Gf.BBox3d(rng, matrix).ComputeAlignedRange())
    return total


def union_local(boxes, to_local: Gf.Matrix4d) -> Gf.Range3d:
    total = Gf.Range3d()
    for rng, matrix in boxes:
        total.UnionWith(Gf.BBox3d(rng, matrix * to_local).ComputeAlignedRange())
    return total


def matrix_scale(matrix: Gf.Matrix4d) -> list[float]:
    return [Gf.Vec3d(matrix.GetRow3(i)).GetLength() for i in range(3)]


class Geometry:
    """Measured geometry of one object prim at one time."""

    def __init__(self, prim: Usd.Prim, time: Usd.TimeCode, xf_cache: UsdGeom.XformCache, include_hidden=False):
        self.matrix = xf_cache.GetLocalToWorldTransform(prim)
        self.boxes = collect_boxes(prim, time, xf_cache, include_hidden)
        self.world = union_world(self.boxes)
        self.valid = range_valid(self.world)
        if not self.valid:
            return
        unit = Gf.Matrix4d(self.matrix)
        scale = matrix_scale(unit)
        # Strip scale from the object frame so local sizes come out in meters.
        for i in range(3):
            if scale[i] > 1e-9:
                unit.SetRow3(i, Gf.Vec3d(unit.GetRow3(i)) / scale[i])
        self.frame = unit
        self.local = union_local(self.boxes, unit.GetInverse())
        lmin, lmax = self.local.GetMin(), self.local.GetMax()
        self.size = [lmax[i] - lmin[i] for i in range(3)]
        base_local = Gf.Vec3d((lmin[0] + lmax[0]) / 2, lmin[1], (lmin[2] + lmax[2]) / 2)
        self.position = unit.Transform(base_local)
        self.footprint = [
            unit.Transform(Gf.Vec3d(x, lmin[1], z))
            for x, z in ((lmin[0], lmin[2]), (lmax[0], lmin[2]), (lmax[0], lmax[2]), (lmin[0], lmax[2]))
        ]
        self.corners = [
            unit.Transform(Gf.Vec3d(x, y, z))
            for x in (lmin[0], lmax[0])
            for y in (lmin[1], lmax[1])
            for z in (lmin[2], lmax[2])
        ]

    def heading(self, forward_axis: str | None) -> float | None:
        axis = AXIS_VECTORS.get((forward_axis or "").upper().replace("POS", "+"))
        if axis is None:
            return None
        direction = self.frame.TransformDir(axis)
        return heading_of(direction[0], direction[2])


# --------------------------------------------------------------------------- record builder


class WorldStateBuilder:
    def __init__(self, stage_path: Path, frame_spec: str = "start", max_objects: int = DEFAULT_MAX_OBJECTS):
        self.stage_path = stage_path.resolve()
        self.project_root = find_project_root(self.stage_path.parent)
        self.manifest = Manifest(self.project_root)
        self.user_edits_path = self.stage_path.parent / "user_edits.usda"
        if self.user_edits_path.is_file() and self.user_edits_path.stat().st_size > 0:
            root_layer = Sdf.Layer.FindOrOpen(str(self.stage_path))
            user_layer = Sdf.Layer.FindOrOpen(str(self.user_edits_path))
            self.stage = (
                Usd.Stage.Open(root_layer, user_layer)
                if (root_layer and user_layer)
                else Usd.Stage.Open(str(self.stage_path))
            )
        else:
            self.stage = Usd.Stage.Open(str(self.stage_path))
        if not self.stage:
            raise RuntimeError(f"Cannot open USD stage {stage_path}")
        self.max_objects = max_objects
        self.fps = self.stage.GetTimeCodesPerSecond() or 24.0
        self.start = self.stage.GetStartTimeCode()
        self.end = self.stage.GetEndTimeCode()
        self.animated = self.stage.HasAuthoredTimeCodeRange() and self.end > self.start
        if frame_spec == "end":
            self.frame = self.end if self.animated else self.start
        elif frame_spec == "start":
            self.frame = self.start if self.animated else 0.0
        else:
            self.frame = float(frame_spec)
        self.time = Usd.TimeCode(self.frame)
        self.xf_cache = UsdGeom.XformCache(self.time)
        self.root = self.stage.GetDefaultPrim() or self.stage.GetPseudoRoot()
        meta = self.stage.GetMetadata("customLayerData") or {}
        self.ground_y = float(meta.get("ground_y", 0.0)) if isinstance(meta, dict) else 0.0
        self.warnings: list[str] = []
        self._render_extras: dict = {}

    # ------------------------------------------------------------------ objects

    def defined_in(self, prim: Usd.Prim) -> str:
        local_layers = {layer.identifier for layer in self.stage.GetLayerStack(includeSessionLayers=True)}
        root_id = self.stage.GetRootLayer().identifier
        session_layer = self.stage.GetSessionLayer()
        session_id = session_layer.identifier if session_layer else ""
        for spec in prim.GetPrimStack():
            if spec.specifier == Sdf.SpecifierDef and spec.layer.identifier in local_layers:
                if spec.layer.identifier in (root_id, session_id) and self.stage_path.name == "shot.usda":
                    return "shot"
                return "scene"
        return "scene"

    def candidate_prims(self) -> tuple[list[Usd.Prim], list[Usd.Prim]]:
        objects, zones = [], []
        for prim in self.root.GetFilteredChildren(Usd.PrimDefaultPredicate):
            role = str(prim.GetCustomDataByKey("world_state_role") or "")
            if role == "ignore" or prim.IsA(UsdGeom.Camera) or is_light(prim):
                continue
            if prim.GetName() == ZONES_SCOPE:
                zones.extend(prim.GetFilteredChildren(Usd.PrimDefaultPredicate))
                continue
            if role == "zone":
                zones.append(prim)
                continue
            if prim.GetTypeName() in ("Scope", "Material", "Shader"):
                continue
            objects.append(prim)
        return objects, zones

    def pose_of(self, prim: Usd.Prim) -> dict | None:
        cache = UsdSkel.Cache()
        for descendant in Usd.PrimRange(prim, Usd.TraverseInstanceProxies()):
            if not descendant.IsA(UsdSkel.Root):
                continue
            cache.Populate(UsdSkel.Root(descendant), Usd.TraverseInstanceProxies())
            for skel_prim in Usd.PrimRange(descendant):
                if not skel_prim.IsA(UsdSkel.Skeleton):
                    continue
                query = cache.GetSkelQuery(UsdSkel.Skeleton(skel_prim))
                anim = query.GetAnimQuery() if query else None
                try:
                    anim_prim = anim.GetPrim() if anim else None
                except Exception:  # noqa: BLE001
                    anim_prim = None
                if not anim_prim:
                    return {"clip": None, "state": "rest_pose"}
                samples = list(anim.GetJointTransformTimeSamples())
                if not samples:
                    return {"clip": anim_prim.GetName(), "state": "static_pose"}
                first, last = samples[0], samples[-1]
                if self.frame < first:
                    state = "before_clip_start"
                elif self.frame > last:
                    state = "holding_last_pose"
                else:
                    state = "playing"
                return {
                    "clip": anim_prim.GetName(),
                    "clip_frames": [rnd(first), rnd(last)],
                    "clip_seconds": rnd((last - first) / self.fps),
                    "state": state,
                }
        return None

    def parts_of(self, prim: Usd.Prim, entry: dict | None) -> tuple[dict, dict]:
        """Current state of movable parts declared in the manifest.

        Manifest form: "parts": {"door_l": {"prim": "Body/DoorL", "axis": "Y"}}.
        Only the current value lives in the record; axes and ranges stay in the manifest.
        """
        states, boxes = {}, {}
        for name, spec in ((entry or {}).get("parts") or {}).items():
            part = prim.GetPrimAtPath(spec.get("prim", "")) if spec.get("prim") else None
            if not part or not part.IsValid():
                continue
            local = UsdGeom.Xformable(part).GetLocalTransformation(self.time)
            rotation = local.ExtractRotation()
            axis_index = "XYZ".index(str(spec.get("axis", "Y")).upper()[-1])
            angle = rotation.Decompose(Gf.Vec3d.XAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.ZAxis())[axis_index]
            states[name] = {"open_deg": rnd(abs(angle))}
            part_boxes = collect_boxes(part, self.time, self.xf_cache)
            if part_boxes:
                boxes[name] = union_world(part_boxes)
        return states, boxes

    def motion_of(self, prim: Usd.Prim, geometry: Geometry) -> dict | None:
        if not self.animated:
            return None
        neighbor = self.frame - 1 if self.frame - 1 >= self.start else self.frame + 1
        other = Geometry(prim, Usd.TimeCode(neighbor), UsdGeom.XformCache(Usd.TimeCode(neighbor)))
        if not other.valid:
            return None
        sign = 1.0 if neighbor < self.frame else -1.0
        dx = (geometry.position[0] - other.position[0]) * sign
        dz = (geometry.position[2] - other.position[2]) * sign
        dy = (geometry.position[1] - other.position[1]) * sign
        speed = math.hypot(dx, dz) * self.fps
        if speed < MOVING_SPEED_MPS and abs(dy) * self.fps < MOVING_SPEED_MPS:
            return {"moving": False}
        direction = heading_of(dx, dz)
        result = {"moving": True, "speed_mps": rnd(speed), "vertical_mps": rnd(dy * self.fps)}
        if direction is not None:
            result["direction_deg"] = rnd(direction)
            result["direction"] = compass(direction)
        return result

    def build_objects(self, prims: list[Usd.Prim]) -> tuple[list[dict], list[dict]]:
        objects, environment = [], []
        for prim in prims:
            geometry = Geometry(prim, self.time, self.xf_cache)
            if not geometry.valid:
                continue
            asset_id, entry, referenced = self.manifest.lookup(prim)
            wmin, wmax = geometry.world.GetMin(), geometry.world.GetMax()
            footprint_area = (wmax[0] - wmin[0]) * (wmax[2] - wmin[2])
            role = str(prim.GetCustomDataByKey("world_state_role") or "")
            if not role:
                big = footprint_area > ENVIRONMENT_FOOTPRINT_M2 or prim.GetName().lower() in ENVIRONMENT_NAMES
                role = "environment" if big and not asset_id else "object"
            if role == "environment":
                environment.append({
                    "id": prim.GetName(),
                    "asset": asset_id,
                    "bounds_min": rvec(wmin),
                    "bounds_max": rvec(wmax),
                    "top_y": rnd(wmax[1]),
                    "_geometry": geometry,
                })
                continue

            pose = self.pose_of(prim)
            is_norm = bool(prim.GetCustomDataByKey("painterspace_normalized")) or (
                referenced and referenced.endswith(".norm.usda")
            )
            if is_norm:
                forward = "-Z"
            elif prim.GetCustomDataByKey("forward_axis"):
                forward = str(prim.GetCustomDataByKey("forward_axis"))
            else:
                forward = (entry or {}).get("forward_axis") or "-Z"
            heading = geometry.heading(forward)
            tags = (entry or {}).get("tags") or []
            is_paint = role == "paint_summary" or prim.GetName() == "Paint"
            category = (
                "paint"
                if is_paint
                else ("character" if pose is not None or "character" in tags else (tags[0] if tags else "prop"))
            )
            record = {
                "id": prim.GetName(),
                "path": str(prim.GetPath()),
                "asset": asset_id,
                "category": category,
                "defined_in": self.defined_in(prim),
                "position": rvec(geometry.position),
                "heading_deg": None if is_paint else (rnd(heading) if heading is not None else None),
                "size": rvec(geometry.size),
                "bounds_min": rvec(wmin),
                "bounds_max": rvec(wmax),
            }
            if is_paint:
                record["stroke_count"] = len(list(prim.GetFilteredChildren(Usd.PrimDefaultPredicate)))
            elif heading is None:
                record["heading_note"] = f"forward axis {'unknown' if not forward else forward!r} for this asset"
            if asset_id is None and referenced:
                record["asset_note"] = f"references {rel(Path(referenced), self.project_root)}, which is not in manifest.json"
            parts, part_boxes = self.parts_of(prim, entry)
            if parts:
                record["parts"] = parts
            if entry and entry.get("anchors"):
                world_mat = self.xf_cache.GetLocalToWorldTransform(prim)
                world_anchors = {}
                for aname, aspec in entry["anchors"].items():
                    apos = aspec.get("pos", [0, 0, 0])
                    wp = world_mat.Transform(Gf.Vec3d(float(apos[0]), float(apos[1]), float(apos[2])))
                    afacing = float(aspec.get("facing_deg", 0.0))
                    arad = math.radians(afacing)
                    v_dir = world_mat.TransformDir(Gf.Vec3d(math.sin(arad), 0.0, -math.cos(arad)))
                    w_head = heading_of(v_dir[0], v_dir[2])
                    world_anchors[aname] = {
                        "pos": rvec([wp[0], wp[1], wp[2]]),
                        "facing_deg": rnd(w_head if w_head is not None else afacing),
                    }
                if world_anchors:
                    record["anchors"] = world_anchors
            if pose is not None:
                record["pose"] = pose
            motion = self.motion_of(prim, geometry)
            if motion is not None:
                record["motion"] = motion
            record["_geometry"] = geometry
            record["_parts"] = part_boxes
            record["_entry"] = entry
            record["_prim"] = prim
            objects.append(record)
        return objects, environment

    # ------------------------------------------------------------------ zones & camera

    def build_zones(self, prims: list[Usd.Prim]) -> list[dict]:
        zones = []
        for prim in prims:
            boxes = collect_boxes(prim, self.time, self.xf_cache, include_hidden=True)
            world = union_world(boxes)
            if not range_valid(world):
                continue
            zone = {"id": prim.GetName(), "min": rvec(world.GetMin()), "max": rvec(world.GetMax())}
            description = prim.GetCustomDataByKey("description")
            if description:
                zone["description"] = str(description)
            zone["_range"] = world
            zones.append(zone)
        return zones

    def find_camera(self) -> Usd.Prim | None:
        candidates = []
        sidecar = self.stage_path.parent / "shot.json"
        if sidecar.is_file():
            try:
                candidates.append(json.loads(sidecar.read_text(encoding="utf-8")).get("camera_prim", ""))
            except (OSError, json.JSONDecodeError):
                pass
        root = str(self.root.GetPath()) if self.root.GetPath() != Sdf.Path.absoluteRootPath else ""
        candidates += [f"{root}/ShotCam", f"{root}/CameraRig/RenderCamera"]
        for path in candidates:
            if path:
                prim = self.stage.GetPrimAtPath(path)
                if prim and prim.IsA(UsdGeom.Camera):
                    return prim
        for prim in self.stage.Traverse():
            if prim.IsA(UsdGeom.Camera) and "/_" not in str(prim.GetPath()):
                if UsdGeom.Imageable(prim).ComputeVisibility(self.time) != UsdGeom.Tokens.invisible:
                    return prim
        return None

    def build_camera(self) -> dict | None:
        prim = self.find_camera()
        if not prim:
            return None
        camera = UsdGeom.Camera(prim).GetCamera(self.time)
        frustum = camera.frustum
        position = frustum.GetPosition()
        forward = frustum.ComputeViewDirection()
        look_at = position + forward * 10.0
        if forward[1] < -1e-3:
            distance = (self.ground_y - position[1]) / forward[1]
            if 0 < distance <= 60:
                look_at = position + forward * distance
        heading = heading_of(forward[0], forward[2])
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, forward[1]))))
        info = {
            "path": str(prim.GetPath()),
            "position": rvec(position),
            "look_at": rvec(look_at),
            "heading_deg": rnd(heading) if heading is not None else None,
            "pitch_deg": rnd(pitch),
            "focal_mm": rnd(camera.focalLength),
            "fov_deg": rnd(camera.GetFieldOfView(Gf.Camera.FOVHorizontal)),
            "vertical_fov_deg": rnd(camera.GetFieldOfView(Gf.Camera.FOVVertical)),
            "aspect": rnd(camera.aspectRatio),
        }
        self._camera = (camera, frustum)
        return info

    # ------------------------------------------------------------------ derived facts

    def camera_view(self, objects: list[dict]) -> list[dict]:
        if not getattr(self, "_camera", None):
            return []
        _, frustum = self._camera
        view = frustum.ComputeViewMatrix()
        projection = frustum.ComputeProjectionMatrix()
        near = frustum.GetNearFar().min
        position = frustum.GetPosition()
        results = []
        for obj in objects:
            corners = [view.Transform(c) for c in obj["_geometry"].corners]
            front = [c for c in corners if c[2] < -near]
            distance = (obj["_geometry"].world.GetMidpoint() - position).GetLength()
            if not front:
                results.append({"id": obj["id"], "state": "behind_camera", "distance_m": rnd(distance)})
                continue
            ndc = [projection.Transform(c) for c in front]
            xs, ys = [p[0] for p in ndc], [p[1] for p in ndc]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            cx0, cx1, cy0, cy1 = max(x0, -1), min(x1, 1), max(y0, -1), min(y1, 1)
            full_area = max(1e-9, (x1 - x0) * (y1 - y0))
            if cx0 >= cx1 or cy0 >= cy1:
                side = "left" if x1 < -1 else "right" if x0 > 1 else "below" if y1 < -1 else "above"
                results.append({"id": obj["id"], "state": "outside", "side": side, "distance_m": rnd(distance)})
                continue
            visible = (cx1 - cx0) * (cy1 - cy0) / full_area
            if len(front) < len(corners):
                visible = min(visible, 0.97)  # crosses the near plane
            center_x = (cx0 + cx1) / 2
            screen = "left" if center_x < -0.33 else "right" if center_x > 0.33 else "center"
            results.append({
                "id": obj["id"],
                "state": "inside" if visible >= 0.98 else "partial",
                "screen": screen,
                "coverage_pct": rnd(100 * (cx1 - cx0) * (cy1 - cy0) / 4),
                "visible_pct": rnd(100 * visible),
                "distance_m": rnd(distance),
            })
        return results

    def relations(self, objects: list[dict]) -> list[dict]:
        pairs = []
        for i, a in enumerate(objects):
            for b in objects[i + 1:]:
                pa, pb = a["_geometry"].position, b["_geometry"].position
                distance = math.hypot(pb[0] - pa[0], pb[2] - pa[2])
                if distance <= RELATION_RADIUS_M:
                    pairs.append((distance, a, b))
        pairs.sort(key=lambda item: item[0])
        results = []
        for distance, a, b in pairs[:MAX_RELATIONS]:
            # Express as "b is <distance> <direction> of a" with the smaller object as the subject.
            if b["_geometry"].world.GetSize().GetLength() > a["_geometry"].world.GetSize().GetLength():
                a, b = b, a
            pa, pb = a["_geometry"].position, b["_geometry"].position
            bearing = heading_of(pb[0] - pa[0], pb[2] - pa[2])
            gap = range_gap(a["_geometry"].world, b["_geometry"].world)
            entry = {
                "a": b["id"],
                "b": a["id"],
                "distance_m": rnd(distance),
                "gap_m": rnd(gap),
                "direction": compass(bearing) if bearing is not None else "same position",
            }
            text = f"{b['id']} is {rnd(distance)} m {entry['direction']} of {a['id']}"
            if bearing is not None:
                back = (bearing + 180) % 360
                if b.get("heading_deg") is not None:
                    entry["a_faces_b"] = angle_diff(b["heading_deg"], back) <= FACING_TOLERANCE_DEG
                    if entry["a_faces_b"]:
                        text += ", facing it"
                if a.get("heading_deg") is not None:
                    entry["b_faces_a"] = angle_diff(a["heading_deg"], bearing) <= FACING_TOLERANCE_DEG
            text += f", {rnd(gap)} m from its nearest side" if gap > 0 else ", touching or overlapping it"
            nearest_part = None
            for part_name, part_range in a.get("_parts", {}).items():
                part_gap = range_gap(b["_geometry"].world, part_range)
                if nearest_part is None or part_gap < nearest_part[1]:
                    nearest_part = (part_name, part_gap)
            if nearest_part:
                entry["nearest_part"] = {"part": nearest_part[0], "gap_m": rnd(nearest_part[1])}
                text += f", {rnd(nearest_part[1])} m from its {nearest_part[0]}"
            entry["text"] = text
            results.append(entry)
        return results

    def overlaps(self, objects: list[dict]) -> list[dict]:
        results = []
        for i, a in enumerate(objects):
            for b in objects[i + 1:]:
                amount = range_overlap(a["_geometry"].world, b["_geometry"].world)
                if all(v > OVERLAP_TOLERANCE_M for v in amount):
                    results.append({"a": a["id"], "b": b["id"], "overlap_m": rvec(amount)})
        return results

    def support_check(self, objects: list[dict], environment: list[dict]) -> None:
        surfaces = [o["_geometry"] for o in objects] + [e["_geometry"] for e in environment]
        for obj in objects:
            geometry = obj["_geometry"]
            bottom = geometry.world.GetMin()[1]
            if bottom < self.ground_y - GROUND_TOLERANCE_M:
                self.warnings.append(
                    f"{obj['id']} extends {rnd(self.ground_y - bottom)} m below the ground (ground_y {rnd(self.ground_y)})."
                )
            elif bottom > self.ground_y + GROUND_TOLERANCE_M:
                supported = False
                for other in surfaces:
                    if other is geometry:
                        continue
                    amount = range_overlap(geometry.world, other.world)
                    if amount[0] > 0 and amount[2] > 0 and abs(other.world.GetMax()[1] - bottom) <= 0.1:
                        supported = True
                        break
                if not supported:
                    self.warnings.append(f"{obj['id']} floats {rnd(bottom - self.ground_y)} m above the ground with nothing under it.")
            if obj["category"] == "character":
                width, height, depth = obj["size"]
                if height < max(width, depth):
                    self.warnings.append(
                        f"{obj['id']} is not upright: {height} m tall but {max(width, depth)} m long. "
                        "The asset is probably authored with a different up axis; check its orientation before posing it."
                    )
                motion = obj.get("motion")
                heading = obj.get("heading_deg")
                if motion and motion.get("moving") and heading is not None and "direction_deg" in motion:
                    diff = angle_diff(heading, motion["direction_deg"])
                    if diff > 100.0:
                        self.warnings.append(
                            f"{obj['id']} is moving backwards: facing {compass(heading)} ({heading:.0f}°) "
                            f"while traveling {motion['direction']} ({motion['direction_deg']:.0f}°). "
                            "If this asset has forward_axis = '+Z' (like human characters), rotateY = 180 is required to face North (-Z)."
                        )

    def free_space(self, objects: list[dict], zones: list[dict]) -> list[dict]:
        results = []
        for zone in zones:
            zr = zone["_range"]
            zmin, zmax = zr.GetMin(), zr.GetMax()
            width, depth = zmax[0] - zmin[0], zmax[2] - zmin[2]
            cell = max(0.25, max(width, depth) / 300)
            cols, rows = max(1, int(width / cell)), max(1, int(depth / cell))
            occupied = [[False] * cols for _ in range(rows)]
            for obj in objects:
                w = obj["_geometry"].world
                if w.GetMax()[1] < zmin[1] - 0.5 or w.GetMin()[1] > zmax[1] + 2.5:
                    continue
                c0 = int((w.GetMin()[0] - 0.15 - zmin[0]) / cell)
                c1 = int(math.ceil((w.GetMax()[0] + 0.15 - zmin[0]) / cell))
                r0 = int((w.GetMin()[2] - 0.15 - zmin[2]) / cell)
                r1 = int(math.ceil((w.GetMax()[2] + 0.15 - zmin[2]) / cell))
                for row in range(max(0, r0), min(rows, r1)):
                    for col in range(max(0, c0), min(cols, c1)):
                        occupied[row][col] = True
            best = largest_empty_rectangle(occupied)
            if not best:
                continue
            area, (row0, col0, row1, col1) = best
            if area * cell * cell < FREE_SPACE_MIN_AREA_M2:
                continue
            fmin = [zmin[0] + col0 * cell, zmin[1], zmin[2] + row0 * cell]
            fmax = [zmin[0] + (col1 + 1) * cell, zmax[1], zmin[2] + (row1 + 1) * cell]
            results.append({
                "zone": zone["id"],
                "min": rvec(fmin),
                "max": rvec(fmax),
                "size": [rnd(fmax[0] - fmin[0]), rnd(fmax[2] - fmin[2])],
            })
        return results

    def group(self, objects: list[dict], zones: list[dict]) -> tuple[list[dict], list[dict]]:
        by_asset: dict[str, list[dict]] = {}
        for obj in objects:
            if obj["asset"]:
                by_asset.setdefault(obj["asset"], []).append(obj)
        groups, grouped_ids = [], set()
        for asset, members in by_asset.items():
            if len(members) < GROUP_MIN_COUNT:
                continue
            total = Gf.Range3d()
            for member in members:
                total.UnionWith(member["_geometry"].world)
            spacing = []
            for member in members:
                p = member["_geometry"].position
                nearest = min(
                    math.hypot(p[0] - o["_geometry"].position[0], p[2] - o["_geometry"].position[2])
                    for o in members
                    if o is not member
                )
                spacing.append(nearest)
            center = total.GetMidpoint()
            groups.append({
                "asset": asset,
                "count": len(members),
                "bounds_min": rvec(total.GetMin()),
                "bounds_max": rvec(total.GetMax()),
                "spacing_m": rnd(sum(spacing) / len(spacing)),
                "location": self.describe_location(center, zones),
                "ids": [m["id"] for m in members[:12]] + (["..."] if len(members) > 12 else []),
                "text": f"{len(members)} × {asset}, {self.describe_location(center, zones)}, "
                f"about {rnd(sum(spacing) / len(spacing))} m apart",
            })
            grouped_ids.update(m["id"] for m in members)
        return [o for o in objects if o["id"] not in grouped_ids], groups

    def describe_location(self, point: Gf.Vec3d, zones: list[dict]) -> str:
        best = None
        for zone in zones:
            zr = zone["_range"]
            inside = zr.GetMin()[0] <= point[0] <= zr.GetMax()[0] and zr.GetMin()[2] <= point[2] <= zr.GetMax()[2]
            if inside:
                return f"in {zone['id']}"
            mid = zr.GetMidpoint()
            distance = math.hypot(point[0] - mid[0], point[2] - mid[2])
            if best is None or distance < best[0]:
                best = (distance, zone, mid)
        if best:
            bearing = heading_of(point[0] - best[2][0], point[2] - best[2][2])
            return f"{compass(bearing)} of {best[1]['id']}" if bearing is not None else f"at {best[1]['id']}"
        bearing = heading_of(point[0], point[2])
        return f"{compass(bearing)} of the world origin" if bearing is not None else "at the world origin"

    # ------------------------------------------------------------------ assemble

    def build(self, label: str = "") -> dict:
        up_axis = UsdGeom.GetStageUpAxis(self.stage)
        meters = UsdGeom.GetStageMetersPerUnit(self.stage)
        if up_axis != UsdGeom.Tokens.y:
            self.warnings.append(f"Stage upAxis is {up_axis}; PainterSpace expects Y.")
        if abs(meters - 1.0) > 1e-6:
            self.warnings.append(f"Stage metersPerUnit is {meters}; PainterSpace expects 1.")

        object_prims, zone_prims = self.candidate_prims()
        objects, environment = self.build_objects(object_prims)
        zones = self.build_zones(zone_prims)
        camera = self.build_camera()
        if camera is None and self.stage_path.name == "shot.usda":
            self.warnings.append("No camera found; expected /World/ShotCam.")

        self.support_check(objects, environment)
        overlaps = self.overlaps(objects)
        relations = self.relations(objects)
        free_space = self.free_space(objects, zones)
        view = self.camera_view(objects)
        visible_ids = {v["id"] for v in view if v["state"] in ("inside", "partial")}

        listed, groups = self.group(objects, zones)
        omitted = None
        if len(listed) > self.max_objects:
            reference = self._camera[1].GetPosition() if getattr(self, "_camera", None) else Gf.Vec3d(0, 0, 0)

            def rank(obj):
                p = obj["_geometry"].position
                return (obj["id"] not in visible_ids, math.hypot(p[0] - reference[0], p[2] - reference[2]))

            listed.sort(key=rank)
            dropped = listed[self.max_objects:]
            listed = listed[: self.max_objects]
            counts: dict[str, int] = {}
            for obj in dropped:
                key = obj["asset"] or obj["category"]
                counts[key] = counts.get(key, 0) + 1
            omitted = {
                "count": len(dropped),
                "text": f"{len(dropped)} more objects outside the camera's interest: "
                + ", ".join(f"{n} × {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])),
            }

        self._render_extras = {"objects": objects, "environment": environment, "zones": zones, "camera": camera}
        stage_rel = rel(self.stage_path, self.project_root)
        record = {
            "schema_version": SCHEMA_VERSION,
            "generated_by": "tools/world_state.py",
            "source": {
                "stage": stage_rel,
                "label": label or stage_rel,
                "frame": rnd(self.frame),
                "time_s": rnd((self.frame - self.start) / self.fps) if self.animated else 0.0,
                "fps": rnd(self.fps),
                "frame_range": [rnd(self.start), rnd(self.end)] if self.animated else None,
            },
            "world": {"units": "m", "up": "Y", "north": "-Z", "east": "+X", "ground_y": rnd(self.ground_y)},
            "objects": [strip_private(o) for o in listed],
            "groups": groups,
            "omitted": omitted,
            "environment": [strip_private(e) for e in environment],
            "zones": [strip_private(z) for z in zones],
            "camera": camera,
            "camera_view": view,
            "relations": relations,
            "overlaps": overlaps,
            "free_space": free_space,
            "warnings": self.warnings,
        }
        return record

    # ------------------------------------------------------------------ images

    def render_topdown(self, output: Path, title: str = "") -> bool:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            print("Pillow is not installed; skipping the top-down map", file=sys.stderr)
            return False
        extras = self._render_extras
        points = []
        for obj in extras["objects"]:
            points += [(p[0], p[2]) for p in obj["_geometry"].footprint]
        for env in extras["environment"]:
            w = env["_geometry"].world
            points += [(w.GetMin()[0], w.GetMin()[2]), (w.GetMax()[0], w.GetMax()[2])]
        for zone in extras["zones"]:
            zr = zone["_range"]
            points += [(zr.GetMin()[0], zr.GetMin()[2]), (zr.GetMax()[0], zr.GetMax()[2])]
        camera = extras.get("camera")
        if camera:
            points.append((camera["position"][0], camera["position"][2]))
        if not points:
            points = [(-5, -5), (5, 5)]
        xmin, xmax = min(p[0] for p in points) - 2, max(p[0] for p in points) + 2
        zmin, zmax = min(p[1] for p in points) - 2, max(p[1] for p in points) + 2
        span = max(xmax - xmin, zmax - zmin, 4.0)
        canvas, margin = 1024, 48
        scale = (canvas - 2 * margin) / span
        width = int((xmax - xmin) * scale) + 2 * margin
        height = int((zmax - zmin) * scale) + 2 * margin + 28

        def px(x: float, z: float) -> tuple[float, float]:
            # +X to the right, north (-Z) up.
            return margin + (x - xmin) * scale, margin + 28 + (z - zmin) * scale

        image = Image.new("RGB", (width, height), (246, 244, 238))
        draw = ImageDraw.Draw(image, "RGBA")
        try:
            font = ImageFont.load_default(size=14)
            small = ImageFont.load_default(size=11)
        except TypeError:
            font = small = ImageFont.load_default()

        step = next(s for s in (1, 2, 5, 10, 20, 50, 100, 200) if span / s <= 14)
        x = math.floor(xmin / step) * step
        while x <= xmax:
            draw.line([px(x, zmin), px(x, zmax)], fill=(214, 210, 200), width=1)
            draw.text((px(x, zmax)[0] + 2, px(x, zmax)[1] + 2), f"x={x:g}", fill=(150, 145, 135), font=small)
            x += step
        z = math.floor(zmin / step) * step
        while z <= zmax:
            draw.line([px(xmin, z), px(xmax, z)], fill=(214, 210, 200), width=1)
            draw.text((px(xmin, z)[0] + 2, px(xmin, z)[1] + 2), f"z={z:g}", fill=(150, 145, 135), font=small)
            z += step

        for env in extras["environment"]:
            w = env["_geometry"].world
            draw.rectangle([px(w.GetMin()[0], w.GetMin()[2]), px(w.GetMax()[0], w.GetMax()[2])],
                           fill=(120, 120, 120, 30), outline=(110, 110, 110), width=1)
            draw.text(px(w.GetMin()[0], w.GetMin()[2]), f" {env['id']}", fill=(90, 90, 90), font=small)
        for zone in extras["zones"]:
            zr = zone["_range"]
            draw.rectangle([px(zr.GetMin()[0], zr.GetMin()[2]), px(zr.GetMax()[0], zr.GetMax()[2])],
                           fill=(70, 140, 220, 38), outline=(60, 120, 200), width=2)
            corner = px(zr.GetMin()[0], zr.GetMax()[2])
            draw.text((corner[0] + 4, corner[1] - 18), zone["id"], fill=(40, 90, 170), font=small)

        if camera:
            cx, cz = camera["position"][0], camera["position"][2]
            heading = camera.get("heading_deg")
            if heading is not None:
                half = math.radians(camera["fov_deg"] / 2)
                length = span * 0.6
                for offset in (-half, half):
                    angle = math.radians(heading) + offset
                    end = (cx + math.sin(angle) * length, cz - math.cos(angle) * length)
                    draw.line([px(cx, cz), px(*end)], fill=(200, 60, 160, 160), width=2)
                draw.line([px(cx, cz), px(camera["look_at"][0], camera["look_at"][2])], fill=(200, 60, 160, 90), width=1)
            ccx, ccz = px(cx, cz)
            draw.ellipse([ccx - 7, ccz - 7, ccx + 7, ccz + 7], fill=(200, 60, 160), outline=(90, 20, 70))
            draw.text((ccx + 10, ccz - 6), f"CAMERA {camera['focal_mm']:g}mm", fill=(150, 30, 120), font=font)

        palette = [(214, 82, 60), (46, 134, 92), (52, 101, 178), (224, 160, 40), (130, 80, 170), (30, 150, 160)]
        for index, obj in enumerate(extras["objects"]):
            color = palette[index % len(palette)]
            outline = [px(p[0], p[2]) for p in obj["_geometry"].footprint]
            has_shapes = bool((obj.get("_entry") or {}).get("collision", {}).get("shapes"))
            draw.polygon(outline, fill=color + (55 if has_shapes else 120,), outline=color)
            if has_shapes and obj.get("_prim"):
                try:
                    sys.path.insert(0, str(Path(__file__).resolve().parent / "collision"))
                    from colliders import SceneObject, world_groups
                    sobj = SceneObject(obj["_prim"], obj["_entry"], "asset")
                    for grp in world_groups(self.stage, sobj, self.time, self.xf_cache):
                        for corners in grp.corners_world():
                            xs, zs = corners[:, 0], corners[:, 2]
                            draw.rectangle(
                                [px(float(xs.min()), float(zs.min())), px(float(xs.max()), float(zs.max()))],
                                fill=color + (165,),
                                outline=color,
                            )
                except Exception:  # noqa: BLE001
                    pass
            for aname, aspec in (obj.get("anchors") or {}).items():
                apos = aspec.get("pos", [0, 0, 0])
                ax, az = px(apos[0], apos[2])
                draw.ellipse([ax - 2, az - 2, ax + 2, az + 2], fill=(255, 255, 255, 210), outline=color)
            pos = obj["_geometry"].position
            center = px(pos[0], pos[2])
            heading = obj.get("heading_deg")
            if heading is not None:
                reach = max(obj["size"][0], obj["size"][2]) * 0.5 + 0.6
                tip = px(pos[0] + math.sin(math.radians(heading)) * reach, pos[2] - math.cos(math.radians(heading)) * reach)
                draw.line([center, tip], fill=color, width=3)
                draw.ellipse([tip[0] - 4, tip[1] - 4, tip[0] + 4, tip[1] + 4], fill=color)
            motion = obj.get("motion") or {}
            if motion.get("moving") and motion.get("direction_deg") is not None:
                d = math.radians(motion["direction_deg"])
                reach = max(1.0, motion["speed_mps"] * 0.5)
                tip = px(pos[0] + math.sin(d) * reach, pos[2] - math.cos(d) * reach)
                draw.line([center, tip], fill=(20, 20, 20, 180), width=2)
            draw.ellipse([center[0] - 3, center[1] - 3, center[0] + 3, center[1] + 3], fill=(20, 20, 20))
            label = obj["id"]
            draw.text((center[0] + 6, center[1] + 4), label, fill=(20, 20, 20), font=font,
                      stroke_width=3, stroke_fill=(246, 244, 238))

        draw.rectangle([0, 0, width, 26], fill=(40, 46, 54))
        header = title or "Top-down map"
        draw.text((10, 6), f"{header}   (N = -Z up, E = +X right, grid {step:g} m)", fill=(240, 240, 240), font=font)
        arrow_x, arrow_y = width - 30, 60
        draw.polygon([(arrow_x, arrow_y - 16), (arrow_x - 8, arrow_y + 4), (arrow_x + 8, arrow_y + 4)], fill=(40, 46, 54))
        draw.text((arrow_x - 4, arrow_y + 6), "N", fill=(40, 46, 54), font=font)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output)
        return True

    def render_camera(self, output: Path, width: int = 640) -> bool:
        if not getattr(self, "_camera", None):
            return False
        usdrecord = shutil.which("usdrecord")
        if not usdrecord:
            print("usdrecord is not installed; skipping the camera view image", file=sys.stderr)
            return False
        camera_path = self._render_extras["camera"]["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            # usdrecord insists on a frame placeholder; the decimal form avoids
            # scientific-notation frame names on some builds.
            pattern = Path(scratch) / "camera.####.###.png"
            result = subprocess.run(
                [usdrecord, str(self.stage_path), str(pattern), "--frames", f"{self.frame:g}",
                 "--cam", camera_path, "--imageWidth", str(width), "--enableDomeLightVisibility"],
                capture_output=True, text=True, timeout=300,
            )
            produced = sorted(Path(scratch).glob("camera.*.png"))
            if result.returncode != 0 or not produced:
                print(f"usdrecord failed: {(result.stderr or result.stdout).strip()}", file=sys.stderr)
                return False
            shutil.move(str(produced[0]), output)
        return True


def largest_empty_rectangle(grid: list[list[bool]]) -> tuple[int, tuple[int, int, int, int]] | None:
    """Maximal all-free rectangle in a boolean occupancy grid (histogram method)."""
    if not grid or not grid[0]:
        return None
    cols = len(grid[0])
    heights = [0] * cols
    best = None
    for row_index, row in enumerate(grid):
        for col in range(cols):
            heights[col] = 0 if row[col] else heights[col] + 1
        stack: list[int] = []
        for col in range(cols + 1):
            current = heights[col] if col < cols else 0
            while stack and heights[stack[-1]] >= current:
                top = stack.pop()
                h = heights[top]
                left = stack[-1] + 1 if stack else 0
                area = h * (col - left)
                if h and (best is None or area > best[0]):
                    best = (area, (row_index - h + 1, left, row_index, col - 1))
            stack.append(col)
    return best


def strip_private(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- per-shot workflow


def write_state(
    stage: Path,
    frame: str,
    output: Path,
    label: str,
    images: bool,
    max_objects: int = DEFAULT_MAX_OBJECTS,
) -> dict:
    builder = WorldStateBuilder(stage, frame, max_objects)
    record = builder.build(label)
    image_paths = {"topdown": None, "camera": None}
    if images:
        stem = output.with_suffix("")
        topdown = Path(f"{stem}_topdown.png")
        if builder.render_topdown(topdown, label):
            image_paths["topdown"] = topdown.name
        camera_image = Path(f"{stem}_camera.png")
        if builder.render_camera(camera_image):
            image_paths["camera"] = camera_image.name
    record["images"] = image_paths
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def previous_shot(shot_dir: Path) -> Path | None:
    number = shot_number(shot_dir)
    candidates = [
        d for d in shot_dir.parent.iterdir()
        if d.is_dir() and d.name.startswith("shot_") and 0 < shot_number(d) < number and (d / "shot.usda").is_file()
    ]
    return max(candidates, key=shot_number) if candidates else None


def copy_images(record: dict, source_dir: Path, source_stem: str, target_dir: Path, target_stem: str) -> dict:
    copied = {}
    for kind, name in (record.get("images") or {}).items():
        if not name:
            copied[kind] = None
            continue
        target = target_dir / f"{target_stem}_{kind}.png"
        shutil.copyfile(source_dir / name, target)
        copied[kind] = target.name
    return copied


def prepare_shot(shot_stage: Path, images: bool, max_objects: int) -> dict:
    """Rebuild the scene baseline and write state_start.json for a shot about to be (re)generated."""
    shot_dir = shot_stage.resolve().parent
    scene_dir = shot_dir.parent
    scene_stage = scene_dir / "scene.usda"
    if not scene_stage.is_file():
        raise RuntimeError(f"Missing scene layer {scene_stage}")
    baseline = write_state(scene_stage, "start", scene_dir / "world_state.json",
                           f"{scene_dir.name} baseline", images, max_objects)
    previous = previous_shot(shot_dir)
    if shot_stage.is_file():
        out = shot_dir / "state_start.json"
        record = write_state(shot_stage, "start", out, f"{shot_dir.name} start", images, max_objects)
        if previous:
            record["continuity"] = {
                "from": previous.name,
                "note": "This shot includes current 3D editor / shot.usda state continuing from the previous shot.",
            }
        else:
            record["continuity"] = {
                "from": "3d editor / current shot.usda",
                "note": "Reflects the current 3D editor layout and shot layer.",
            }
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"state": str(out), "images": {k: str(shot_dir / v) if v else None for k, v in record["images"].items()}}

    if previous:
        source = write_state(previous / "shot.usda", "end", previous / "state_end.json",
                             f"{previous.name} end", images, max_objects)
        record = dict(source)
        record["images"] = copy_images(source, previous, "state_end", shot_dir, "state_start")
        record["continuity"] = {
            "from": previous.name,
            "note": "This shot continues from the end of the previous shot. Its actors are NOT inherited "
            "through composition (each shot sublayers only scene.usda), so any actor with defined_in = "
            "\"shot\" must be re-created in this shot at its listed position, heading and pose.",
        }
    else:
        record = dict(baseline)
        record["images"] = copy_images(baseline, scene_dir, "world_state", shot_dir, "state_start")
        record["continuity"] = {"from": "scene baseline", "note": "First shot of the scene."}
    record["source"] = dict(record["source"], label=f"{shot_dir.name} start")
    out = shot_dir / "state_start.json"
    shot_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"state": str(out), "images": {k: str(shot_dir / v) if v else None for k, v in record["images"].items()}}


def finalize_shot(shot_stage: Path, images: bool, max_objects: int) -> dict:
    shot_dir = shot_stage.resolve().parent
    out = shot_dir / "state_end.json"
    record = write_state(shot_stage, "end", out, f"{shot_dir.name} end", images, max_objects)
    return {"state": str(out), "images": {k: str(shot_dir / v) if v else None for k, v in record["images"].items()},
            "warnings": record["warnings"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a record for any stage at one frame")
    build.add_argument("stage", type=Path)
    build.add_argument("--frame", default="start", help="start, end, or a time code (default: start)")
    build.add_argument("--output", type=Path, help="JSON output path (default: stdout)")
    build.add_argument("--topdown", type=Path, help="write a labelled top-down map PNG")
    build.add_argument("--camera-image", type=Path, help="render the camera view PNG with usdrecord")
    build.add_argument("--label", default="")
    build.add_argument("--max-objects", type=int, default=DEFAULT_MAX_OBJECTS)

    for name, text in (("prepare-shot", "write world_state.json and state_start.json before an LLM request"),
                       ("finalize-shot", "write state_end.json from the shot's last frame")):
        command = sub.add_parser(name, help=text)
        command.add_argument("shot_stage", type=Path, help="scenes/scene_XX/shot_XXX/shot.usda")
        command.add_argument("--no-images", action="store_true")
        command.add_argument("--max-objects", type=int, default=DEFAULT_MAX_OBJECTS)

    options = parser.parse_args()
    try:
        if options.command == "build":
            builder = WorldStateBuilder(options.stage, options.frame, options.max_objects)
            record = builder.build(options.label)
            if options.topdown:
                builder.render_topdown(options.topdown, options.label)
            if options.camera_image:
                builder.render_camera(options.camera_image)
            text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
            if options.output:
                options.output.write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
        elif options.command == "prepare-shot":
            print(json.dumps(prepare_shot(options.shot_stage, not options.no_images, options.max_objects)))
        else:
            print(json.dumps(finalize_shot(options.shot_stage, not options.no_images, options.max_objects)))
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"world_state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

