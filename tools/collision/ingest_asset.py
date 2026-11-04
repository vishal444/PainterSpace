#!/usr/bin/env python3
"""
ingest_asset.py - make an asset safe to collide with.

For one manifest entry (or a USD file) it:
  1. NORMALIZES it: meters, Y up, forward = -Z, pivot at base centre. When anything must change it
     writes <name>.norm.usda next to the source. The wrapper references the WHOLE source, so every
     prim path stays the same (Armature/Armature, Doors/Door_FL ...); the correction is added as an
     extra transform op on the top-level children, and Blender helpers (Camera, Cube, Light,
     env_light) are deactivated. The original file is never touched.
  2. MEASURES it from the real mesh points: bounds_min / bounds_max / bounds_m.
  3. BUILDS COLLIDERS: compound boxes covering everything a standing character can touch
     (from STEP_HEIGHT to --reach metres). Columns are split where there is a vertical gap
     (--gap) so a character can walk under a canopy, branch or table. Moving parts get their own
     boxes plus collider_frame, so the validator places them with the part's real USD transform.
  4. ADDS ANCHORS: safe standing spots around the asset, and next to each door clear of its swing.
  5. For characters: detects facing from the feet and records body height and planning radius.

Usage:
  python tools/collision/ingest_asset.py --manifest manifest.json --id cars.car1 --write
  python tools/collision/ingest_asset.py --manifest manifest.json --id cars.car2 --forward=-Y --write
  python tools/collision/ingest_asset.py --manifest manifest.json --all --write

--forward is given in the ORIGINAL asset's own axes (for example -Y for a Z-up car whose nose points -Y).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade, UsdSkel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shot_library"))
from colliders import (HIDDEN_HELPER_NAMES, REACH_HEIGHT_M, STEP_HEIGHT_M, VISIBLE_PURPOSES, R4, R4_axis, T4,
                       apply, heading_deg, joint_leaf, l2w, mat_to_list, np2gf_rows)
from support_surfaces import (MAX_SUPPORT_FACE_HEIGHT_VARIATION_M,
                              ACTOR_SUPPORT_MARGIN_M, derive_surface_spots)

AXES = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1)}
NORM_OP = "xformOp:transform:painterspaceNorm"
SKIP_CATEGORIES = ()                        # all asset categories are eligible for collision ingestion
# Skin, hair, and fabric are dielectrics. Some character exports give every
# surface a fractional metallic value and connect glossiness directly to
# roughness, producing a silvery film over the diffuse texture. Keep the
# source diffuse color unchanged while correcting that reflective finish.
CHARACTER_FINISH_ROUGHNESS = 0.65
CHARACTER_FINISH_SPECULAR = 0.25
CHARACTER_TEXTURE_MAX_SIZE = 2048


class IngestError(Exception):
    pass


# ------------------------------------------------------------------ helpers / traversal
def is_helper(prim, root):
    if prim.GetParent() != root:
        return False
    if prim.GetName() in HIDDEN_HELPER_NAMES:
        return True
    return any(p.IsA(UsdGeom.Camera) or p.HasAPI(UsdLux.LightAPI) for p in Usd.PrimRange(prim)) and \
        not any(p.IsA(UsdGeom.Gprim) for p in Usd.PrimRange(prim))


def visible_meshes(root, skip_paths=()):
    it = iter(Usd.PrimRange(root))
    for p in it:
        sp = str(p.GetPath())
        if any(sp == s or sp.startswith(s + "/") for s in skip_paths):
            it.PruneChildren()
            continue
        if p != root and p.GetParent() == root and is_helper(p, root):
            it.PruneChildren()
            continue
        if p.IsA(UsdGeom.Imageable):
            if UsdGeom.Imageable(p).ComputeVisibility() == UsdGeom.Tokens.invisible:
                it.PruneChildren()
                continue
            if UsdGeom.Imageable(p).ComputePurpose() not in VISIBLE_PURPOSES:
                it.PruneChildren()
                continue
        if p.IsA(UsdGeom.Gprim):
            yield p


def cube_tris(prim, M):
    size_attr = prim.GetAttribute("size")
    size = float(size_attr.Get() or 2.0) if size_attr else 2.0
    h = size / 2.0
    pts = np.array([
        [-h, -h, -h], [h, -h, -h], [h, h, -h], [-h, h, -h],
        [-h, -h,  h], [h, -h,  h], [h, h,  h], [-h, h,  h]
    ], float)
    pts_w = apply(M, pts)
    faces = [
        [0, 1, 2], [0, 2, 3],  # -Z
        [4, 6, 5], [4, 7, 6],  # +Z
        [0, 4, 5], [0, 5, 1],  # -Y
        [2, 6, 7], [2, 7, 3],  # +Y
        [0, 3, 7], [0, 7, 4],  # -X
        [1, 5, 6], [1, 6, 2],  # +X
    ]
    return pts_w[faces]


def cylinder_tris(prim, M, num_segments=16):
    r_attr = prim.GetAttribute("radius")
    r = float(r_attr.Get() or 1.0) if r_attr else 1.0
    h_attr = prim.GetAttribute("height")
    h = float(h_attr.Get() or 2.0) if h_attr else 2.0
    axis = str(prim.GetAttribute("axis").Get() or "Z").upper() if prim.GetAttribute("axis") else "Z"
    angles = np.linspace(0, 2 * np.pi, num_segments, endpoint=False)
    cos_a = np.cos(angles) * r
    sin_a = np.sin(angles) * r
    half_h = h / 2.0

    pts_top = np.zeros((num_segments, 3))
    pts_bot = np.zeros((num_segments, 3))
    center_top = np.zeros(3)
    center_bot = np.zeros(3)

    if axis == "Y":
        pts_top[:, 0] = cos_a; pts_top[:, 1] = half_h; pts_top[:, 2] = sin_a
        pts_bot[:, 0] = cos_a; pts_bot[:, 1] = -half_h; pts_bot[:, 2] = sin_a
        center_top[1] = half_h; center_bot[1] = -half_h
    elif axis == "X":
        pts_top[:, 0] = half_h; pts_top[:, 1] = cos_a; pts_top[:, 2] = sin_a
        pts_bot[:, 0] = -half_h; pts_bot[:, 1] = cos_a; pts_bot[:, 2] = sin_a
        center_top[0] = half_h; center_bot[0] = -half_h
    else:  # Z
        pts_top[:, 0] = cos_a; pts_top[:, 1] = sin_a; pts_top[:, 2] = half_h
        pts_bot[:, 0] = cos_a; pts_bot[:, 1] = sin_a; pts_bot[:, 2] = -half_h
        center_top[2] = half_h; center_bot[2] = -half_h

    tris = []
    w_top = apply(M, pts_top)
    w_bot = apply(M, pts_bot)
    c_top = apply(M, center_top.reshape(1, 3))[0]
    c_bot = apply(M, center_bot.reshape(1, 3))[0]
    for i in range(num_segments):
        nxt = (i + 1) % num_segments
        tris.append([c_top, w_top[i], w_top[nxt]])
        tris.append([c_bot, w_bot[nxt], w_bot[i]])
        tris.append([w_bot[i], w_top[i], w_top[nxt]])
        tris.append([w_bot[i], w_top[nxt], w_bot[nxt]])
    return np.array(tris)


def sphere_tris(prim, M, segments=12, rings=6):
    r_attr = prim.GetAttribute("radius")
    r = float(r_attr.Get() or 1.0) if r_attr else 1.0
    theta = np.linspace(0, np.pi, rings + 1)
    phi = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    pts = []
    for t in theta:
        st = np.sin(t) * r
        ct = np.cos(t) * r
        for p in phi:
            pts.append([st * np.cos(p), ct, st * np.sin(p)])
    pts = apply(M, np.array(pts))
    tris = []
    for ring in range(rings):
        r0 = ring * segments
        r1 = (ring + 1) * segments
        for seg in range(segments):
            nxt = (seg + 1) % segments
            tris.append([pts[r0 + seg], pts[r1 + seg], pts[r1 + nxt]])
            tris.append([pts[r0 + seg], pts[r1 + nxt], pts[r0 + nxt]])
    return np.array(tris)


def mesh_points(mesh_prim, M):
    if mesh_prim.IsA(UsdGeom.Mesh):
        pts = UsdGeom.Mesh(mesh_prim).GetPointsAttr().Get()
        if not pts:
            return np.zeros((0, 3))
        return apply(M, np.array(pts, float))
    tris = mesh_tris(mesh_prim, M)
    return tris.reshape(-1, 3) if len(tris) else np.zeros((0, 3))


def mesh_tris(mesh_prim, M):
    if mesh_prim.IsA(UsdGeom.Mesh):
        mesh = UsdGeom.Mesh(mesh_prim)
        pts = mesh.GetPointsAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        idx = mesh.GetFaceVertexIndicesAttr().Get()
        if not pts or not counts or not idx:
            return np.zeros((0, 3, 3))
        pts = apply(M, np.array(pts, float))
        counts, idx = np.array(counts, int), np.array(idx, int)
        starts = np.cumsum(counts) - counts
        out = []
        for k in range(1, counts.max() - 1):
            m = counts > k + 1
            a, b, c = idx[starts[m]], idx[starts[m] + k], idx[starts[m] + k + 1]
            out.append(np.stack([pts[a], pts[b], pts[c]], 1))
        return np.concatenate(out) if out else np.zeros((0, 3, 3))
    if mesh_prim.IsA(UsdGeom.Cube):
        return cube_tris(mesh_prim, M)
    if mesh_prim.IsA(UsdGeom.Cylinder):
        return cylinder_tris(mesh_prim, M)
    if mesh_prim.IsA(UsdGeom.Sphere):
        return sphere_tris(mesh_prim, M)
    return np.zeros((0, 3, 3))


def gather(root, xc, skip_paths=(), M_pre=None, tris=True):
    out = []
    for p in visible_meshes(root, skip_paths):
        M = l2w(xc, p)
        if M_pre is not None:
            M = M_pre @ M
        out.append(mesh_tris(p, M) if tris else mesh_points(p, M))
    if not out:
        return np.zeros((0, 3, 3)) if tris else np.zeros((0, 3))
    return np.concatenate(out)


def measured_asset_surfaces(triangles, collision_shapes, floor_height, tags=()):
    """Derive placeable top references from normalized asset mesh geometry."""
    triangles = np.asarray(triangles, float)
    if triangles.size == 0:
        return {}
    normals = np.cross(triangles[:, 1] - triangles[:, 0],
                       triangles[:, 2] - triangles[:, 0])
    flat = np.ptp(triangles[:, :, 1], axis=1) <= MAX_SUPPORT_FACE_HEIGHT_VARIATION_M
    upward = normals[:, 1] > 1e-8
    faces = [{"owner": "asset", "height_m": float(np.mean(triangles[i, :, 1])),
              "polygon_xz": triangles[i][:, [0, 2]].tolist()}
             for i in np.flatnonzero(flat & upward)]
    solids = []
    for shape in collision_shapes:
        if shape.get("part") or shape.get("type", "box") != "box":
            continue
        center = shape.get("center", [0, 0, 0])
        size = shape.get("size", [0, 0, 0])
        solids.append({"owner": "asset",
                       "min": [float(center[i]) - float(size[i]) / 2 for i in range(3)],
                       "max": [float(center[i]) + float(size[i]) / 2 for i in range(3)]})
        x0, z0 = solids[-1]["min"][0], solids[-1]["min"][2]
        x1, z1 = solids[-1]["max"][0], solids[-1]["max"][2]
        faces.append({"owner": "asset", "height_m": solids[-1]["max"][1],
                      "polygon_xz": [[x0, z0], [x1, z0], [x1, z1], [x0, z1]]})
    spots = derive_surface_spots(faces, solids, {"asset": float(floor_height)},
                                 spatial_cell_m=ACTOR_SUPPORT_MARGIN_M)
    surfaces = {}
    for spot in spots:
        surfaces[spot["name"]] = {
            "center": [round(float(value), 3) for value in spot["position"]],
            "height_m": round(float(spot["position"][1]), 3),
            "usable_for": ["place", "stand_at", "jump_on", "sit_on"],
        }
    if surfaces and {"vehicle", "car", "truck"}.intersection(tags):
        highest = max(surfaces.values(), key=lambda surface: surface["height_m"])
        surfaces["roof"] = dict(highest)
    return surfaces


def subdivide(T, maxlen, limit=4_000_000):
    done = []
    while len(T):
        e = np.stack([np.linalg.norm(T[:, 0] - T[:, 1], axis=1), np.linalg.norm(T[:, 1] - T[:, 2], axis=1),
                      np.linalg.norm(T[:, 2] - T[:, 0], axis=1)], 1).max(1)
        big = e > maxlen
        done.append(T[~big])
        if not big.any():
            break
        if sum(len(d) for d in done) + 4 * big.sum() > limit:
            done.append(T[big])
            break
        B = T[big]
        a, b, c = B[:, 0], B[:, 1], B[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        T = np.concatenate([np.stack(x, 1) for x in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
    return np.concatenate(done) if done else np.zeros((0, 3, 3))


# ------------------------------------------------------------------ colliders from geometry
def _decompose_single_band(tris, voxel, step, reach, gap, max_boxes):
    keep = (tris[:, :, 1].min(1) <= reach) & (tris[:, :, 1].max(1) >= step)
    tris_band = tris[keep]
    if len(tris_band) == 0:
        return []
    span_xz = max(
        float(tris_band[:, :, 0].max() - tris_band[:, :, 0].min()),
        float(tris_band[:, :, 2].max() - tris_band[:, :, 2].min()),
        float(voxel),
    )
    max_voxel = max(0.5, span_xz / max(8.0, math.sqrt(max(1, max_boxes))))
    curr_voxel = voxel
    while True:
        T = subdivide(tris_band, curr_voxel * 0.5)
        P = np.vstack([T.reshape(-1, 3), T.mean(1)])
        P = P[(P[:, 1] <= reach) & (P[:, 1] >= step - 0.05)]
        P[:, 1] = np.maximum(P[:, 1], step - 0.05)
        if len(P) == 0:
            return []
        o = np.array([P[:, 0].min() - curr_voxel, P[:, 1].min() - curr_voxel, P[:, 2].min() - curr_voxel])
        ij = np.floor((P - o) / curr_voxel).astype(int)
        order = np.lexsort((ij[:, 1], ij[:, 2], ij[:, 0]))
        P, ij = P[order], ij[order]
        newcol = np.ones(len(ij), bool)
        newcol[1:] = (ij[1:, 0] != ij[:-1, 0]) | (ij[1:, 2] != ij[:-1, 2])
        jump = np.ones(len(ij), bool)
        jump[1:] = (ij[1:, 1] - ij[:-1, 1]) * curr_voxel > gap
        run = np.cumsum(newcol | jump) - 1
        n = run[-1] + 1

        def agg(vals, fn, init):
            a = np.full(n, init, float)
            fn.at(a, run, vals)
            return a

        lo = np.stack([agg(P[:, k], np.minimum, 1e9) for k in range(3)], 1)
        hi = np.stack([agg(P[:, k], np.maximum, -1e9) for k in range(3)], 1)
        rx = agg(ij[:, 0], np.minimum, 1e9).astype(int)
        rz = agg(ij[:, 2], np.minimum, 1e9).astype(int)
        ry0 = np.floor((lo[:, 1] - o[1]) / curr_voxel).astype(int)
        ry1 = np.floor((hi[:, 1] - o[1]) / curr_voxel).astype(int)
        alive = hi[:, 1] >= step
        boxes = []
        keys = {}
        for r in np.where(alive)[0]:
            keys.setdefault((ry0[r], ry1[r]), []).append(r)
        nx, nz = ij[:, 0].max() + 1, ij[:, 2].max() + 1
        for (_, _), runs in keys.items():
            runs = np.array(runs)
            grid = -np.ones((nx, nz), int)
            grid[rx[runs], rz[runs]] = runs
            used = np.zeros((nx, nz), bool)
            xs, zs = np.where(grid >= 0)
            for ix, iz in zip(xs, zs):
                if used[ix, iz]:
                    continue
                jz = iz
                while jz + 1 < nz and grid[ix, jz + 1] >= 0 and not used[ix, jz + 1]:
                    jz += 1
                jx = ix
                while jx + 1 < nx and (grid[jx + 1, iz:jz + 1] >= 0).all() and not used[jx + 1, iz:jz + 1].any():
                    jx += 1
                used[ix:jx + 1, iz:jz + 1] = True
                c = grid[ix:jx + 1, iz:jz + 1].ravel()
                c = c[c >= 0]
                blo, bhi = lo[c].min(0), hi[c].max(0)
                bhi = np.maximum(bhi, blo + 0.02)
                boxes.append((blo, bhi))
        if len(boxes) <= max_boxes or curr_voxel > max_voxel:
            return boxes
        curr_voxel *= 1.4


def boxes_from_tris(tris, voxel, reach, gap, max_boxes, step=STEP_HEIGHT_M, levels=None, stairs=None):
    """compound of boxes covering geometry between `step` and `reach` metres high.
    Carves out stair clearance envelopes and generates per-level height slices when levels are defined.
    """
    if len(tris) == 0:
        return []

    # 1. Stair Carve-out: exclude geometry inside the walkable stair clearance envelope
    if stairs:
        corridor_mask = np.zeros(len(tris), bool)
        tri_centers = tris.mean(1)
        for s_id, s_data in stairs.items():
            cline = s_data.get("centerline", [])
            w = float(s_data.get("width_m", 1.2))
            hw = w / 2.0
            for i in range(len(cline) - 1):
                p0 = np.array(cline[i], float)
                p1 = np.array(cline[i + 1], float)
                v = p1 - p0
                L2 = v[0]**2 + v[2]**2
                if L2 < 1e-6:
                    continue
                u = ((tri_centers[:, 0] - p0[0]) * v[0] + (tri_centers[:, 2] - p0[2]) * v[2]) / L2
                in_seg = (-0.08 <= u) & (u <= 1.08)
                u_c = np.clip(u, 0.0, 1.0)
                seg_x = p0[0] + u_c * v[0]
                seg_y = p0[1] + u_c * v[1]
                seg_z = p0[2] + u_c * v[2]
                d_xz = np.hypot(tri_centers[:, 0] - seg_x, tri_centers[:, 2] - seg_z)
                in_corridor = in_seg & (d_xz <= hw) & (tri_centers[:, 1] >= seg_y - 0.25) & (tri_centers[:, 1] <= seg_y + 2.15)
                corridor_mask |= in_corridor
        tris = tris[~corridor_mask]
        if len(tris) == 0:
            return []

    # 2. Multi-level bands or default single band
    if levels and len(levels) > 1:
        all_boxes = []
        per_lvl_max = max(12, max_boxes // len(levels))
        for lvl in levels:
            fy = float(lvl.get("floor_y", 0.0))
            lvl_step = fy + step
            lvl_reach = fy + reach
            lvl_boxes = _decompose_single_band(tris, voxel, lvl_step, lvl_reach, gap, per_lvl_max)
            for blo, bhi in lvl_boxes:
                all_boxes.append((blo, bhi, lvl.get("id")))
        if all_boxes:
            return all_boxes

    default_boxes = _decompose_single_band(tris, voxel, step, reach, gap, max_boxes)
    return [(blo, bhi, None) for blo, bhi in default_boxes]


def check_stair_headroom(tris, stairs_data, min_headroom_m=2.10):
    """Verify that overhead geometry along the stair centerline maintains >= min_headroom_m clearance."""
    violations = []
    if not stairs_data or len(tris) == 0:
        return violations
    for s_id, s_data in stairs_data.items():
        cline = s_data.get("centerline", [])
        w = float(s_data.get("width_m", 1.2))
        hw_walk = max(0.2, (w / 2.0) - 0.15)
        for i in range(len(cline) - 1):
            p0 = np.array(cline[i], float)
            p1 = np.array(cline[i + 1], float)
            dist = np.linalg.norm(p1 - p0)
            n_samples = max(2, int(math.ceil(dist / 0.2)))
            for u in np.linspace(0.0, 1.0, n_samples):
                pt = p0 + u * (p1 - p0)
                in_col = (
                    (tris[:, :, 0].max(1) >= pt[0] - hw_walk) & (tris[:, :, 0].min(1) <= pt[0] + hw_walk) &
                    (tris[:, :, 2].max(1) >= pt[2] - 0.15) & (tris[:, :, 2].min(1) <= pt[2] + 0.15)
                )
                col_tris = tris[in_col]
                if len(col_tris) > 0:
                    overhead = col_tris[:, :, 1].min(1)
                    above = overhead[overhead > pt[1] + 0.50]
                    if len(above) > 0:
                        min_y = above.min()
                        clearance = min_y - pt[1]
                        if clearance < min_headroom_m:
                            violations.append({
                                "stair_id": s_id,
                                "point": [round(float(v), 2) for v in pt],
                                "overhead_y": round(float(min_y), 2),
                                "clearance_m": round(float(clearance), 2),
                                "min_required_m": min_headroom_m,
                            })
    return violations


def box_entry(sid, lo, hi, part=None, level=None):
    e = {"id": sid, "type": "box", "center": [round(float(v), 4) for v in (lo + hi) / 2],
         "size": [round(float(v), 4) for v in hi - lo], "blocks": ["character", "vehicle"]}
    if part:
        e["part"] = part
    if level:
        e["level"] = level
    return e


def footprint(shapes):
    if not shapes:
        return None
    lo = np.min([np.array(s["center"]) - np.array(s["size"]) / 2 for s in shapes], 0)
    hi = np.max([np.array(s["center"]) + np.array(s["size"]) / 2 for s in shapes], 0)
    return lo, hi


# ------------------------------------------------------------------ characters
def detect_character_forward(root, up):
    """facing from foot -> toe in the asset's own world axes, snapped to a horizontal axis."""
    skelroot = next((p for p in Usd.PrimRange(root) if p.IsA(UsdSkel.Root)), None)
    if skelroot is None:
        return None
    cache = UsdSkel.Cache()
    cache.Populate(UsdSkel.Root(skelroot), Usd.PrimDefaultPredicate)
    skel = next((p for p in Usd.PrimRange(skelroot) if p.IsA(UsdSkel.Skeleton)), None)
    if skel is None:
        return None
    q = cache.GetSkelQuery(UsdSkel.Skeleton(skel))
    names = [joint_leaf(j) for j in q.GetJointOrder()]
    xf = q.ComputeJointSkelTransforms(Usd.TimeCode.Default())
    P = apply(l2w(UsdGeom.XformCache(), skel), np.array([[*m.ExtractTranslation()] for m in xf]))
    d = np.zeros(3)
    got = 0
    for side in ("Left", "Right"):
        if f"{side}Foot" in names and f"{side}ToeBase" in names:
            d += P[names.index(f"{side}ToeBase")] - P[names.index(f"{side}Foot")]
            got += 1
    if not got:
        return None
    d[1 if up == "Y" else 2] = 0
    cands = [a for a, v in AXES.items() if v[1 if up == "Y" else 2] == 0]
    return max(cands, key=lambda a: float(np.dot(d, AXES[a])))


# ------------------------------------------------------------------ wrapper
def correction_targets(prim, root):
    """the highest xformable prims below root along each branch (Scopes are descended)."""
    out = []
    for child in prim.GetChildren():
        if child.GetParent() == root and is_helper(child, root):
            continue
        if child.IsA(UsdGeom.Xformable):
            out.append(child)
        elif any(p.IsA(UsdGeom.Gprim) for p in Usd.PrimRange(child)):
            out.extend(correction_targets(child, root))
    return out


def author_character_finish(src, dprim, wrapper, src_path, norm_path,
                            texture_max_size=CHARACTER_TEXTURE_MAX_SIZE):
    """Correct fractional-metal character shaders and cap their texture cost."""
    texture_dir = Path(norm_path).parent / ".painterspace_textures"
    for prim in Usd.PrimRange(dprim):
        shader = UsdShade.Shader(prim)
        if not shader or shader.GetIdAttr().Get() != "UsdPreviewSurface":
            continue
        metallic = shader.GetInput("metallic")
        metallic_value = metallic.Get() if metallic else None
        # A constant fractional metallic value on a character surface is an
        # export artifact. Fully metallic parts and metallic maps are preserved.
        if metallic_value is None or not 0.0 < metallic_value < 1.0:
            continue
        if metallic.GetAttr().GetConnections():
            continue
        override = wrapper.OverridePrim(prim.GetPath())
        for name, value in (
            ("metallic", 0.0),
            ("roughness", CHARACTER_FINISH_ROUGHNESS),
            ("specular", CHARACTER_FINISH_SPECULAR),
        ):
            attr = override.CreateAttribute("inputs:" + name, Sdf.ValueTypeNames.Float)
            attr.SetConnections([])
            attr.Set(value)

        for input_name in ("diffuseColor", "normal"):
            surface_input = shader.GetInput(input_name)
            if not surface_input:
                continue
            for connection in surface_input.GetAttr().GetConnections():
                texture_prim = src.GetPrimAtPath(connection.GetPrimPath())
                texture = UsdShade.Shader(texture_prim)
                if not texture or texture.GetIdAttr().Get() != "UsdUVTexture":
                    continue
                texture_override = wrapper.OverridePrim(texture_prim.GetPath())
                file_input = texture.GetInput("file")
                asset = file_input.Get() if file_input else None
                if not asset or texture_max_size <= 0:
                    continue
                source_texture = Path(asset.resolvedPath or
                                      (Path(src_path).parent / asset.path))
                if not source_texture.is_file():
                    continue
                with Image.open(source_texture) as image:
                    if max(image.size) <= texture_max_size:
                        continue
                    texture_dir.mkdir(parents=True, exist_ok=True)
                    # A source path hash prevents collisions between textures
                    # with the same basename in different source directories.
                    source_key = hashlib.sha256(str(source_texture.resolve()).encode()).hexdigest()[:12]
                    preview = texture_dir / f"{texture_max_size}_{source_key}_{source_texture.name}"
                    if not preview.exists() or preview.stat().st_mtime < source_texture.stat().st_mtime:
                        image.thumbnail((texture_max_size, texture_max_size), Image.Resampling.LANCZOS)
                        image.save(preview)
                relative_preview = os.path.relpath(preview, Path(norm_path).parent).replace(os.sep, "/")
                texture_override.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
                    Sdf.AssetPath("./" + relative_preview))


def write_wrapper(src, dprim, src_path, norm_path, C, is_character=False,
                  texture_max_size=CHARACTER_TEXTURE_MAX_SIZE):
    """reference the whole source (paths unchanged) and prepend the correction C on top-level xformables."""
    dname = dprim.GetName()
    w = Usd.Stage.CreateNew(norm_path)
    UsdGeom.SetStageUpAxis(w, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(w, 1.0)
    w.SetStartTimeCode(src.GetStartTimeCode())
    w.SetEndTimeCode(src.GetEndTimeCode())
    if src.HasAuthoredTimeCodeRange() or src.GetTimeCodesPerSecond():
        w.SetTimeCodesPerSecond(src.GetTimeCodesPerSecond())
    root = w.DefinePrim("/" + dname, dprim.GetTypeName() or "Xform")
    w.SetDefaultPrim(root)
    rel = os.path.relpath(src_path, os.path.dirname(os.path.abspath(norm_path))).replace(os.sep, "/")
    root.GetReferences().AddReference(rel if rel.startswith(".") else "./" + rel, "/" + dname)
    root.SetCustomDataByKey("painterspace_normalized", True)
    if dprim.IsA(UsdGeom.Xformable):
        UsdGeom.Xformable(root).GetXformOpOrderAttr().Set([])     # folded into C
    warnings = []
    for child in dprim.GetChildren():
        if is_helper(child, dprim):
            w.OverridePrim(child.GetPath()).SetActive(False)
    gm = Gf.Matrix4d(np2gf_rows(C))
    for tgt in correction_targets(dprim, dprim):
        order = list(UsdGeom.Xformable(tgt).GetXformOpOrderAttr().Get() or [])
        if order and order[0] == "!resetXformStack!":
            warnings.append(f"{tgt.GetPath()} resets the transform stack; left uncorrected")
            continue
        o = w.OverridePrim(tgt.GetPath())
        o.CreateAttribute(NORM_OP, Usd.Stage.CreateInMemory().GetPseudoRoot().GetAttribute("x").GetTypeName()
                          if False else __import__("pxr").Sdf.ValueTypeNames.Matrix4d, False).Set(gm)
        UsdGeom.Xformable(o).GetXformOpOrderAttr().Set([NORM_OP] + order)
    if is_character:
        author_character_finish(src, dprim, w, src_path, norm_path, texture_max_size)
    w.GetRootLayer().Save()
    return warnings


# ------------------------------------------------------------------ parts
def part_axis_world(stage, pp, axis_letter, xc):
    parent = pp.GetParent()
    R = l2w(xc, parent)[:3, :3] if parent and parent.IsValid() else np.eye(3)
    v = R @ np.array(AXES["+" + axis_letter.upper()], float)
    v = v / max(np.linalg.norm(v), 1e-12)
    return [round(float(x), 4) for x in v]


def swing_corners(shapes, part, n=8):
    """corners (asset frame) of a part's boxes over its full swing."""
    lo_d, hi_d = float(part.get("min_deg", 0.0)), float(part.get("max_deg", 70.0))
    signs = [part["open_sign"]] if part.get("open_sign") in (1, -1, 1.0, -1.0) else [1, -1]
    h = np.array(part["hinge"], float)
    out = []
    for s in shapes:
        c, hs = np.array(s["center"]), np.array(s["size"]) / 2
        C = np.array([c + hs * np.array([a, b, d]) for a in (-1, 1) for b in (-1, 1) for d in (-1, 1)])
        for sg in signs:
            for deg in np.linspace(lo_d, hi_d, n):
                M = T4(h) @ R4_axis(part["axis_vec"], sg * deg) @ T4(-h)
                out.append(apply(M, C))
    return np.concatenate(out) if out else np.zeros((0, 3))


# ------------------------------------------------------------------ stairs & levels
def extract_stairs_and_levels(nroot, xc, Wn, Pa=None):
    """Scan root prim hierarchy for authored stair markers (customData.painterspace_role == 'stairs').
    Extracts centerline, width, treads table, and derived levels in asset frame.
    """
    stairs = {}
    levels_dict = {}

    for prim in Usd.PrimRange(nroot):
        role = prim.GetCustomDataByKey("painterspace_role") or prim.GetCustomDataByKey("role")
        is_stair_role = str(role).lower() == "stairs" or "stairs" in prim.GetName().lower()
        if not is_stair_role:
            continue

        stair_id = prim.GetName().lower()
        from_lvl = str(prim.GetCustomDataByKey("from_level") or "L0")
        to_lvl = str(prim.GetCustomDataByKey("to_level") or "L1")
        width_m = float(prim.GetCustomDataByKey("width_m") or 1.2)

        # Look for Bottom, Landing_*, Top markers among children
        children = {c.GetName().lower(): c for c in prim.GetChildren()}
        bottom_prim = next((c for k, c in children.items() if "bottom" in k or "start" in k), None)
        top_prim = next((c for k, c in children.items() if "top" in k or "end" in k), None)
        landings = [c for k, c in sorted(children.items()) if "landing" in k]

        centerline = []
        if bottom_prim and top_prim:
            M_bot = Wn @ l2w(xc, bottom_prim)
            p_bot = apply(M_bot, np.zeros((1, 3)))[0]
            centerline.append([round(float(v), 3) for v in p_bot])

            for l_prim in landings:
                M_land = Wn @ l2w(xc, l_prim)
                p_land = apply(M_land, np.zeros((1, 3)))[0]
                centerline.append([round(float(v), 3) for v in p_land])

            M_top = Wn @ l2w(xc, top_prim)
            p_top = apply(M_top, np.zeros((1, 3)))[0]
            centerline.append([round(float(v), 3) for v in p_top])

        # Step prims under this stair prim
        step_prims = [c for k, c in sorted(children.items()) if "step" in k]
        treads = []
        for sp in step_prims:
            M_step = Wn @ l2w(xc, sp)
            sp_pos = apply(M_step, np.zeros((1, 3)))[0]
            rise_m = 0.18
            tread_m = 0.28
            if sp.IsA(UsdGeom.Cube):
                cube = UsdGeom.Cube(sp)
                sz = cube.GetSizeAttr().Get() or 1.0
                xf = UsdGeom.Xformable(sp)
                sc = [1.0, 1.0, 1.0]
                for op in xf.GetOrderedXformOps():
                    if "scale" in op.GetName().lower():
                        sc = list(op.Get())
                rise_m = round(float(sz * sc[1]), 3)
                tread_m = round(float(sz * sc[2]), 3)
            treads.append({
                "center": [round(float(v), 3) for v in sp_pos],
                "rise_m": rise_m,
                "tread_m": tread_m,
            })

        if not centerline and treads:
            centerline = [
                treads[0]["center"],
                treads[-1]["center"]
            ]

        if centerline:
            b_anch = f"{stair_id}_bottom"
            t_anch = f"{stair_id}_top"
            stairs[stair_id] = {
                "from_level": from_lvl,
                "to_level": to_lvl,
                "centerline": centerline,
                "width_m": width_m,
                "treads": treads,
                "anchors": {"bottom": b_anch, "top": t_anch},
            }
            levels_dict[from_lvl] = round(float(centerline[0][1]), 2)
            levels_dict[to_lvl] = round(float(centerline[-1][1]), 2)

    levels = [{"id": lvl_id, "floor_y": fy} for lvl_id, fy in sorted(levels_dict.items(), key=lambda x: x[1])]
    return stairs, levels


def suggest_stairs(nroot, xc, Wn):
    """Analyze asset geometry for candidate step clusters (0.10 - 0.25m rise)."""
    steps = []
    for prim in Usd.PrimRange(nroot):
        if prim.IsA(UsdGeom.Cube):
            M = Wn @ l2w(xc, prim)
            pos = apply(M, np.zeros((1, 3)))[0]
            xf = UsdGeom.Xformable(prim)
            scale = [1.0, 1.0, 1.0]
            for op in xf.GetOrderedXformOps():
                if "scale" in op.GetName().lower():
                    scale = list(op.Get())
            if 0.08 <= scale[1] <= 0.30 and (scale[0] >= 0.8 or scale[2] >= 0.8):
                steps.append((prim.GetName(), pos, scale))

    chains = []
    visited = set()
    for i, s1 in enumerate(steps):
        if i in visited:
            continue
        for direction in (1, -1):
            chain = [s1]
            chain_indices = [i]
            while True:
                last = chain[-1]
                next_step = None
                next_idx = None
                for j, s2 in enumerate(steps):
                    if j in visited or j in chain_indices:
                        continue
                    dy = (s2[1][1] - last[1][1]) * direction
                    dxz = math.hypot(s2[1][0] - last[1][0], s2[1][2] - last[1][2])
                    if 0.10 <= dy <= 0.25 and 0.20 <= dxz <= 0.60:
                        next_step = s2
                        next_idx = j
                        break
                if next_step:
                    chain.append(next_step)
                    chain_indices.append(next_idx)
                else:
                    break
            if len(chain) >= 3:
                for idx in chain_indices:
                    visited.add(idx)
                if chain[0][1][1] > chain[-1][1][1]:
                    chain.reverse()
                chains.append(chain)
                break

    if chains:
        print(f"\n[--suggest-stairs] Found {len(chains)} candidate stair sequence(s):")
        for idx, c in enumerate(chains):
            bot = c[0][1]
            top = c[-1][1]
            w = round(float(c[0][2][0] if abs(top[2] - bot[2]) > abs(top[0] - bot[0]) else c[0][2][2]), 2)
            print(f"  Candidate #{idx+1}: {len(c)} steps ({c[0][0]} -> {c[-1][0]})")
            print(f"    Elevation: y={bot[1]:.2f}m to y={top[1]:.2f}m")
            print(f"    Suggested USDA Marker Prim snippet:")
            print(f'      def Xform "Stairs_Candidate_{idx+1}" (')
            print('          customData = {')
            print('              string painterspace_role = "stairs"')
            print('              string from_level = "L0"')
            print('              string to_level = "L1"')
            print(f'              float width_m = {w}')
            print('          }')
            print('      ) {')
            print(f'          def Xform "Bottom" {{ double3 xformOp:translate = ({bot[0]:.3f}, {bot[1]:.3f}, {bot[2]:.3f}) uniform token[] xformOpOrder = ["xformOp:translate"] }}')
            print(f'          def Xform "Top" {{ double3 xformOp:translate = ({top[0]:.3f}, {top[1]:.3f}, {top[2]:.3f}) uniform token[] xformOpOrder = ["xformOp:translate"] }}')
            print('      }')
    else:
        print("\n[--suggest-stairs] No un-marked candidate stair sequences detected.")


# ------------------------------------------------------------------ main logic
def ingest(entry, src_path, out_dir, args):
    src = Usd.Stage.Open(src_path)
    if not src:
        raise IngestError(f"cannot open {src_path}")
    dname = entry.get("default_prim") or (src.GetDefaultPrim().GetName() if src.GetDefaultPrim() else None)
    if not dname:
        raise IngestError("asset has no default prim; set default_prim in the manifest entry")
    dprim = src.GetPrimAtPath("/" + dname)
    if not dprim:
        raise IngestError(f"default prim /{dname} not found in {src_path}")
    up = UsdGeom.GetStageUpAxis(src)
    mpu = UsdGeom.GetStageMetersPerUnit(src) or 1.0
    is_char = any(p.IsA(UsdSkel.Root) for p in Usd.PrimRange(dprim))
    notes = []
    xc0 = UsdGeom.XformCache()
    W0 = l2w(xc0, dprim)
    P0 = gather(dprim, xc0, tris=False, M_pre=np.linalg.inv(W0) @ W0)   # source root's parent frame
    if len(P0) == 0:
        raise IngestError("asset has no measurable mesh geometry")
    lo0, hi0 = P0.min(0), P0.max(0)

    # ---- forward axis, in the original frame
    fwd = args.forward
    if not fwd and is_char:
        fwd = detect_character_forward(dprim, up)
        if fwd:
            notes.append(f"character facing detected from the feet: {fwd} (original axes)")
    if not fwd and entry.get("normalization"):
        fwd = entry["normalization"].get("forward_was")
    if not fwd and entry.get("forward_axis") and not entry.get("normalization"):
        cand = entry["forward_axis"]
        if cand in AXES and AXES[cand][1 if up == "Y" else 2] == 0:
            fwd = cand
    if not fwd:
        fwd = "-Z" if up == "Y" else "-Y"
        notes.append(f"WARNING: forward axis not known; assumed {fwd}. Check it and pass --forward.")
    if fwd not in AXES:
        raise IngestError(f"forward axis '{fwd}' must be one of {list(AXES)}")
    if AXES[fwd][1 if up == "Y" else 2] != 0:
        ext = {a: round(float((hi0 - lo0)[i] * mpu), 2) for i, a in enumerate("XYZ")}
        raise IngestError(f"forward axis {fwd} is the up axis ({up}). Extents in the asset's own axes (m): {ext}. "
                          f"Pass --forward with a horizontal axis.")

    # ---- correction: scale -> up fix -> yaw -> recentre (applied in the source root's frame)
    Rup = R4("X", -90) if up == "Z" else np.eye(4)
    f1 = Rup[:3, :3] @ np.array(AXES[fwd], float)
    yaw = round(math.degrees(math.atan2(f1[0], -f1[2])), 6)
    L = R4("Y", yaw) @ Rup @ np.diag([mpu, mpu, mpu, 1.0])
    R0 = l2w(xc0, dprim) if dprim.IsA(UsdGeom.Xformable) else np.eye(4)
    if dprim.GetParent() and not dprim.GetParent().IsPseudoRoot():
        R0 = np.linalg.inv(l2w(xc0, dprim.GetParent())) @ R0
    Pn = apply(L @ R0, P0) if False else apply(L, P0)      # P0 already includes the root's own ops
    lo, hi = Pn.min(0), Pn.max(0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -lo[1], -(lo[2] + hi[2]) / 2])
    if args.keep_pivot or getattr(args, "collision_only", False):
        shift = np.zeros(3)
    C = T4(shift) @ L @ R0
    helpers = [c.GetName() for c in dprim.GetChildren() if is_helper(c, dprim)]
    wrap = not np.allclose(C, np.eye(4), atol=1e-6) or bool(helpers) or is_char
    if getattr(args, "collision_only", False) and not entry.get("normalization") and np.allclose(L, np.eye(4), atol=1e-6):
        wrap = False
    if np.allclose(L, np.eye(4), atol=1e-6) and np.allclose(shift, 0, atol=0.02) and not helpers \
            and np.allclose(R0, np.eye(4), atol=1e-6) and not is_char:
        wrap = False
    norm_path = src_path
    if wrap:
        os.makedirs(out_dir, exist_ok=True)
        norm_path = os.path.join(out_dir, os.path.splitext(os.path.basename(src_path))[0] + ".norm.usda")
        if os.path.exists(norm_path):
            os.remove(norm_path)
        notes += write_wrapper(src, dprim, src_path, norm_path, C, is_character=is_char,
                               texture_max_size=args.character_texture_max_size)
        if helpers:
            notes.append("deactivated Blender helpers: " + ", ".join(helpers))
    ns = Usd.Stage.Open(norm_path)
    nroot = ns.GetPrimAtPath("/" + dname)
    xc = UsdGeom.XformCache()
    Wn = np.linalg.inv(l2w(xc, nroot))            # world -> asset frame (identity for wrappers)
    Pa = gather(nroot, xc, tris=False, M_pre=Wn)
    bmin, bmax = Pa.min(0), Pa.max(0)

    out = dict(entry)
    for k in ("collision", "anchors", "notes"):
        out.pop(k, None)
    out.update({"id": entry["id"], "default_prim": dname, "units": "meters", "up_axis": "Y", "forward_axis": "-Z",
                "pivot": "Base center at origin (y=0)", "path": args.path_for(norm_path),
                "bounds_min": [round(float(v), 3) for v in bmin], "bounds_max": [round(float(v), 3) for v in bmax],
                "bounds_m": [round(float(v), 3) for v in bmax - bmin], "usd_reference_ready": True,
                "file_hash": hashlib.sha256(Path(src_path).read_bytes()).hexdigest()[:16]})
    if wrap:
        out["source_path"] = entry.get("source_path") or args.path_for(src_path)
        out["normalization"] = {"meters_per_unit_was": mpu, "up_axis_was": up, "forward_was": fwd,
                                "yaw_deg": yaw, "shift_m": [round(float(v), 3) for v in shift],
                                "wrapper_op": NORM_OP, "prim_paths_unchanged": True}
    else:
        out.pop("source_path", None)
        out.pop("normalization", None)

    if is_char:
        height = float(bmax[1] - bmin[1])
        out["collision"] = {"character": {"height_m": round(height, 2), "radius_m": args.char_radius,
                                          "body": "capsules along the skeleton bones (validate_collisions.py)"}}
        tags = list(out.get("tags", []))
        for t in ("character", "human"):
            if t not in tags:
                tags.append(t)
        out["tags"] = tags
        out["pivot"] = "Feet at origin (y=0), facing -Z"
        if height < 1.0 or height > 2.5:
            notes.append(f"WARNING: character height {height:.2f} m looks wrong; check units/up axis")
        out["notes"] = notes
        return out, wrap

    # ---- parts (doors, hood...)
    parts = {k: dict(v) for k, v in (entry.get("parts") or {}).items()}
    part_prims = {}
    for name, p in parts.items():
        for stale in ("hinge", "axis_vec", "collider_frame", "limits_unverified"):
            p.pop(stale, None)
        pp = ns.GetPrimAtPath(f"/{dname}/{p['prim']}")
        if not pp:
            notes.append(f"WARNING: part '{name}' prim {p['prim']} not found; skipped")
            continue
        part_prims[name] = pp
        meta = dict(pp.GetCustomDataByKey("part") or {})
        Mp = Wn @ l2w(xc, pp)
        p["hinge"] = [round(float(v), 4) for v in Mp[:3, 3]]
        p["axis"] = (p.get("axis") or meta.get("axis") or "Y").upper()
        p["axis_vec"] = part_axis_world(ns, pp, p["axis"], xc) if not wrap else \
            [round(float(v), 4) for v in (Wn[:3, :3] @ np.array(part_axis_world(ns, pp, p["axis"], xc)))]
        p["collider_frame"] = mat_to_list(np.linalg.inv(l2w(xc, pp)))
        for k in ("min_deg", "max_deg", "open_sign", "kind"):
            if k in meta:
                p[k] = float(meta[k]) if k != "kind" else str(meta[k])
        if "open_sign" not in p:
            # Test both directions around hinge to pick the one moving away from body
            pts_part = gather(pp, xc, M_pre=Wn)
            if len(pts_part) > 0:
                h = np.array(p["hinge"], float)
                ax = np.array(p["axis_vec"], float)
                P0 = pts_part[:, :3]
                Mp = T4(h) @ R4_axis(ax, 30.0) @ T4(-h)
                Mm = T4(h) @ R4_axis(ax, -30.0) @ T4(-h)
                Pp = apply(Mp, P0)
                Pm = apply(Mm, P0)
                if "door" in name.lower() or p.get("kind") == "door":
                    d_pos = float(np.mean(np.abs(Pp[:, 0])))
                    d_neg = float(np.mean(np.abs(Pm[:, 0])))
                else:
                    d_pos = float(np.mean(Pp[:, 1]))
                    d_neg = float(np.mean(Pm[:, 1]))
                p["open_sign"] = 1.0 if d_pos > d_neg else -1.0
        if "max_deg" not in p:
            p.update({"min_deg": 0.0, "max_deg": 70.0, "limits_unverified": True})
            notes.append(f"part '{name}': no limits in the asset; assumed 0..70 deg, both directions")
    stairs_data, levels_data = extract_stairs_and_levels(nroot, xc, Wn, Pa)
    if stairs_data:
        out["stairs"] = stairs_data
        notes.append(f"ingested {len(stairs_data)} stair sequence(s): {', '.join(stairs_data.keys())}")
    if levels_data:
        out["levels"] = levels_data
        notes.append(f"ingested {len(levels_data)} level(s): {', '.join(l['id'] for l in levels_data)}")

    shapes = []
    skip = [str(pp.GetPath()) for pp in part_prims.values()]
    all_body_tris = gather(nroot, xc, skip, M_pre=Wn)

    # Check headroom along stair centerline
    if stairs_data:
        headroom_violations = check_stair_headroom(all_body_tris, stairs_data)
        if headroom_violations:
            for v in headroom_violations:
                notes.append(f"WARNING: stair '{v['stair_id']}' headroom violation at {v['point']}: "
                             f"{v['clearance_m']} m clearance (required >= {v['min_required_m']} m)")

    body = boxes_from_tris(all_body_tris, args.voxel, args.reach, args.gap, args.max_boxes,
                           levels=levels_data, stairs=stairs_data)
    for i, item in enumerate(body):
        l, h = item[0], item[1]
        lvl = item[2] if len(item) > 2 else None
        shapes.append(box_entry(f"body_{i}", l, h, level=lvl))
    for name, pp in part_prims.items():
        if name in args.no_collide or parts[name].get("collide") is False:
            parts[name]["collide"] = False
            continue
        bx = boxes_from_tris(gather(pp, xc, M_pre=Wn), args.voxel, args.reach, args.gap, args.max_part_boxes)
        for i, item in enumerate(bx):
            l, h = item[0], item[1]
            lvl = item[2] if len(item) > 2 else None
            shapes.append(box_entry(f"{name}_{i}", l, h, part=name, level=lvl))
    if parts:
        out["parts"] = parts
    if not shapes:
        notes.append("no geometry between %.2f and %.2f m: nothing to collide with" % (STEP_HEIGHT_M, args.reach))
    out["collision"] = {"frame": "asset", "voxel_m": args.voxel, "reach_height_m": args.reach,
                        "step_height_m": STEP_HEIGHT_M, "gap_m": args.gap, "shapes": shapes}
    measured = measured_asset_surfaces(all_body_tris, shapes, bmin[1], out.get("tags", ()))
    if measured:
        out["surfaces"] = {**out.get("surfaces", {}), **measured}
        notes.append(f"measured {len(measured)} placeable top surface reference(s)")

    # Add stair bottom/top standing spots to anchors
    stair_anchors = {}
    for sid, sdata in stairs_data.items():
        if "centerline" in sdata and len(sdata["centerline"]) >= 2:
            p_bot = sdata["centerline"][0]
            p_top = sdata["centerline"][-1]
            b_name = sdata.get("anchors", {}).get("bottom", f"{sid}_bottom")
            t_name = sdata.get("anchors", {}).get("top", f"{sid}_top")
            stair_anchors[b_name] = {"pos": [round(float(v), 3) for v in p_bot], "facing_deg": 180.0, "note": f"bottom entry for {sid}"}
            stair_anchors[t_name] = {"pos": [round(float(v), 3) for v in p_top], "facing_deg": 180.0, "note": f"top exit for {sid}"}

    out["anchors"] = make_anchors(shapes, parts, args)
    out["anchors"].update(stair_anchors)

    if getattr(args, "suggest_stairs", False):
        suggest_stairs(nroot, xc, Wn)

    out["notes"] = notes
    return out, wrap


def make_anchors(shapes, parts, args):
    """standing spots: on each side of the collision footprint, and beside each door clear of its swing."""
    anchors = {}
    fp = footprint(shapes)
    if fp is None:
        return anchors
    lo, hi = fp
    clear = args.anchor_clearance
    ctr = (lo + hi) / 2
    for nm, (dx, dz) in {"front": (0, -1), "back": (0, 1), "left": (-1, 0), "right": (1, 0)}.items():
        pos = np.array([ctr[0] + dx * ((hi[0] - lo[0]) / 2 + clear), 0.0, ctr[2] + dz * ((hi[2] - lo[2]) / 2 + clear)])
        anchors[f"stand_{nm}"] = {"pos": [round(float(v), 3) for v in pos],
                                  "facing_deg": round(heading_deg(ctr[0] - pos[0], ctr[2] - pos[2]), 1)}
    static = [s for s in shapes if not s.get("part")]
    slo, shi = footprint(static) if static else fp
    for name, p in parts.items():
        ps = [s for s in shapes if s.get("part") == name]
        if not ps or not name.startswith("door") or "hinge" not in p:
            continue
        sw = swing_corners(ps, p)
        c = np.mean([s["center"] for s in ps], axis=0)
        side = 1.0 if c[0] > ctr[0] else -1.0
        x = (shi[0] if side > 0 else slo[0]) + side * (args.char_radius + 0.30)
        closed_lo, closed_hi = footprint(ps)
        best = None
        # beside the opening, out of the door's swept area, nearest the door's middle
        for z in np.linspace(lo[2] - 1.5, hi[2] + 1.5, 181):
            q = np.array([x, z])
            d_sw = np.min(np.linalg.norm(sw[:, [0, 2]] - q, axis=1)) if len(sw) else 9.0
            in_swing = _point_in_hull(q, sw[:, [0, 2]]) if len(sw) >= 3 else False
            if in_swing or d_sw < (args.char_radius + 0.25):
                continue
            score = abs(z - c[2])
            if best is None or score < best[0]:
                best = (score, z)
        if best is None:
            continue
        pos = np.array([x, 0.0, best[1]])
        anchors[f"{name}_stand"] = {"pos": [round(float(v), 3) for v in pos],
                                    "facing_deg": round(heading_deg(-side, 0.0), 1), "part": name,
                                    "note": "beside the door, clear of its full swing"}
    return anchors


def _point_in_hull(q, P):
    from plan_path import hull                      # local import: avoid a cycle at module load
    H = hull(P)
    if len(H) < 3:
        return False
    for i in range(len(H)):
        a, b = H[i], H[(i + 1) % len(H)]
        if (b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (q[0] - a[0]) < 0:
            return False
    return True


# ------------------------------------------------------------------ CLI
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--id")
    g.add_argument("--all", action="store_true", help="every non-environment asset in the manifest")
    ap.add_argument("--asset", help="USD file to ingest instead of the manifest path")
    ap.add_argument("--root", help="project root that manifest paths are relative to (default: manifest dir)")
    ap.add_argument("--out-dir", help="where to write wrappers (default: next to the source)")
    ap.add_argument("--forward", help="forward axis in the ORIGINAL asset's axes, e.g. -Y")
    ap.add_argument("--keep-pivot", action="store_true")
    ap.add_argument("--voxel", type=float, default=0.08)
    ap.add_argument("--reach", type=float, default=REACH_HEIGHT_M)
    ap.add_argument("--gap", type=float, default=0.5, help="vertical empty gap (m) that splits a column")
    ap.add_argument("--max-boxes", type=int, default=160)
    ap.add_argument("--max-part-boxes", type=int, default=12)
    ap.add_argument("--anchor-clearance", type=float, default=0.9)
    ap.add_argument("--char-radius", type=float, default=0.3)
    ap.add_argument("--character-texture-max-size", type=int, default=CHARACTER_TEXTURE_MAX_SIZE,
                    help="maximum dimension of generated character texture proxies; 0 keeps source resolution")
    ap.add_argument("--no-collide", default="steering_wheel", help="comma list of parts that get no collider")
    ap.add_argument("--suggest-stairs", action="store_true", help="scan asset geometry for candidate stair clusters and print suggested marker prims")
    ap.add_argument("--write", action="store_true", help="update the manifest in place (a .bak copy is kept)")
    ap.add_argument("--collision-only", action="store_true",
                    help="update only collision shapes; requires the source to match the manifest asset frame")
    return ap


def collision_only_entry(entry, generated):
    """Keep authored asset metadata while replacing geometry-derived colliders."""
    if entry.get("path") != generated.get("path"):
        raise IngestError("collision-only update requires the generated and manifest asset paths to match")
    if entry.get("file_hash") and entry["file_hash"] != generated.get("file_hash"):
        raise IngestError("collision-only update requires the manifest asset hash to match the source")
    collision = generated.get("collision") or {}
    if not collision.get("shapes"):
        raise IngestError("collision-only update produced no collision shapes")
    updated = dict(entry)
    updated["collision"] = collision
    updated["file_hash"] = generated["file_hash"]
    return updated


def main(argv=None):
    a = build_parser().parse_args(argv)
    a.no_collide = [x for x in a.no_collide.split(",") if x]
    data, lst = None, None
    if a.manifest:
        data = json.load(open(a.manifest))
        lst = data["assets"] if isinstance(data, dict) else data
    root = a.root or (os.path.dirname(os.path.abspath(a.manifest)) if a.manifest else ".")
    a.path_for = lambda p: os.path.relpath(os.path.abspath(p), os.path.abspath(root)).replace(os.sep, "/")
    if a.all:
        if lst is None:
            print("ERROR: --all needs --manifest", file=sys.stderr)
            return 2
        ids = [e["id"] for e in lst if not e["id"].startswith(SKIP_CATEGORIES)]
    else:
        ids = [a.id]
    failed = 0
    for aid in ids:
        entry = {"id": aid}
        if lst is not None:
            entry = next((e for e in lst if e["id"] == aid), None)
            if entry is None:
                print(f"ERROR: id {aid} not in {a.manifest}", file=sys.stderr)
                return 2
        src = a.asset or os.path.join(root, entry.get("source_path") or entry["path"])
        if not os.path.exists(src):
            print(f"ERROR {aid}: asset file not found: {src}", file=sys.stderr)
            failed += 1
            continue
        out_dir = a.out_dir or os.path.dirname(os.path.abspath(src))
        try:
            out, wrapped = ingest(entry, os.path.abspath(src), out_dir, a)
        except IngestError as e:
            print(f"ERROR {aid}: {e}", file=sys.stderr)
            failed += 1
            continue
        nb = len((out.get("collision") or {}).get("shapes", []))
        print(f"{aid}: bounds {out['bounds_m']} m, {nb} collision boxes, {len(out.get('anchors', {}))} anchors, "
              f"{'wrapped -> ' + out['path'] if wrapped else 'no wrapper needed'}")
        for n in out.get("notes", []):
            print("  -", n)
        if lst is not None:
            if a.collision_only:
                try:
                    out = collision_only_entry(entry, out)
                except IngestError as e:
                    print(f"ERROR {aid}: {e}", file=sys.stderr)
                    failed += 1
                    continue
            lst[[e["id"] for e in lst].index(aid)] = out
        else:
            print(json.dumps(out, indent=2))
    if a.write and data is not None:
        shutil.copy(a.manifest, a.manifest + ".bak")
        with open(a.manifest, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print("manifest updated:", a.manifest)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

