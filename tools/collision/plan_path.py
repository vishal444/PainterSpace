#!/usr/bin/env python3
"""
plan_path.py - route a character around assets, then write the root motion.

The LLM states INTENT ("go to Car:door_fl_stand"); this tool finds a route that cannot touch
anything, so the shot is safe by construction. validate_collisions.py is still run afterwards.

  python tools/collision/plan_path.py shot.usda --manifest manifest.json --actor Person_01 \
        --to-anchor Car:door_fl_stand --speed 1.4 --out path_person_01.usda
  python tools/collision/plan_path.py shot.usda --manifest manifest.json --actor Person_01 --to 4 -3 --in-place

Start = where the actor stands at --t0. The obstacle map is the union of every asset over
--t0..--t1 (so a door that opens during the walk is avoided). --radius is the clear space kept
around the body center (0.45 m covers the torso plus swinging arms).
The written layer sets xformOp:translate and xformOp:rotateY on the actor (heading 0 = -Z,
rotateY = -heading for normalized -Z assets, or 180 - heading for legacy +Z assets).
"""
from __future__ import annotations

import argparse
import heapq
import math
import os
import sys

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from colliders import (
    REACH_HEIGHT_M,
    STEP_HEIGHT_M,
    VISIBLE_PURPOSES,
    AssetDB,
    apply,
    heading_deg,
    l2w,
    scene_objects,
    world_groups,
)


def hull(P):
    """2D monotone-chain convex hull (CCW) of an array of (x, z) points."""
    pts = sorted(set(map(tuple, np.round(np.asarray(P, float), 6))))
    if len(pts) <= 2:
        return np.array(pts, float)
    cr = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cr(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cr(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return np.array(lo[:-1] + up[:-1], float)


def actor_xz(stage, prim, t):
    return np.array(
        UsdGeom.XformCache(Usd.TimeCode(float(t))).GetLocalToWorldTransform(prim).ExtractTranslation(),
        float,
    )[[0, 2]]


def actor_y(stage, prim, t):
    return float(
        UsdGeom.XformCache(Usd.TimeCode(float(t))).GetLocalToWorldTransform(prim).ExtractTranslation()[1]
    )


def obstacles(stage, db, times, skip=(), skip_parts=(), floor_y=0.0):
    """Footprints (convex polygons in XZ, or discs) of everything a character can touch at floor_y."""
    all_objs = scene_objects(stage, db)
    skip_set = set(skip or ())
    skip_parts_set = set(skip_parts or ())
    out = []
    y_min = floor_y + STEP_HEIGHT_M
    y_max = floor_y + REACH_HEIGHT_M
    for t in times:
        tc = Usd.TimeCode(float(t))
        xc = UsdGeom.XformCache(tc)
        bbc = UsdGeom.BBoxCache(tc, list(VISIBLE_PURPOSES))
        for obj in all_objs:
            if obj.name in skip_set or obj.asset_id in skip_set or str(obj.path) in skip_set:
                continue
            if obj.is_actor:
                # Other actors not in `skip` block space as a disc if within vertical reach
                pos_3d = UsdGeom.XformCache(tc).GetLocalToWorldTransform(obj.prim).ExtractTranslation()
                if abs(pos_3d[1] - floor_y) > 1.8:
                    continue  # Actor on another floor
                r = float(((obj.entry or {}).get("collision") or {}).get("character", {}).get("radius_m", 0.3))
                out.append(("disc", np.array([pos_3d[0], pos_3d[2]], float), r))
                continue
            for grp in world_groups(stage, obj, tc, xc, bbc):
                if grp.part and (
                    grp.part in skip_parts_set
                    or (obj.name, grp.part) in skip_parts_set
                    or (obj.asset_id, grp.part) in skip_parts_set
                ):
                    continue
                C = grp.corners_world()
                for corners in C:
                    if corners[:, 1].max() < y_min or corners[:, 1].min() > y_max:
                        continue
                    poly = hull(corners[:, [0, 2]])
                    if len(poly) >= 3:
                        out.append(("poly", poly))
    return out


def scene_levels(stage, db):
    """Collect all level definitions across scene objects with their floor_y."""
    levels = {}
    for obj in scene_objects(stage, db):
        for lvl in (obj.entry or {}).get("levels", []):
            lid = lvl.get("id")
            if lid and lid not in levels:
                levels[lid] = float(lvl.get("floor_y", 0.0))
    if not levels:
        levels = {"L0": 0.0, "L1": 3.0}
    return levels


def scene_stairs(stage, db, t=0.0):
    """Find all stair portals in the scene, transformed to world space."""
    tc = Usd.TimeCode(float(t))
    xc = UsdGeom.XformCache(tc)
    stairs = []
    for obj in scene_objects(stage, db):
        entry_stairs = (obj.entry or {}).get("stairs") or {}
        if not entry_stairs:
            continue
        M = l2w(xc, obj.prim)
        for s_id, s_data in entry_stairs.items():
            cline_local = np.array(s_data.get("centerline", []), float)
            if len(cline_local) < 2:
                continue
            cline_world = apply(M, cline_local)
            stairs.append({
                "id": f"{obj.name}:{s_id}",
                "obj_name": obj.name,
                "stair_name": s_id,
                "from_level": s_data.get("from_level", "L0"),
                "to_level": s_data.get("to_level", "L1"),
                "width_m": float(s_data.get("width_m", 1.2)),
                "centerline_world": cline_world,
                "bottom_world": cline_world[0],
                "top_world": cline_world[-1],
                "treads": s_data.get("treads", []),
                "anchors": s_data.get("anchors", {}),
            })
    return stairs


def generate_stair_waypoints(centerline, y_start, y_end, ease_dist=0.30, step_dist=0.20):
    """Generate 3D waypoints along a stair centerline with smoothed nosing line elevation."""
    cline = np.asarray(centerline, float)
    if len(cline) < 2:
        return [(float(cline[0][0]), float(y_start), float(cline[0][2]))]
    seg = np.hypot(np.diff(cline[:, 0]), np.diff(cline[:, 2]))
    L = np.concatenate([[0.0], np.cumsum(seg)])
    total_L = L[-1]
    if total_L < 1e-4:
        return [(float(cline[0][0]), float(y_start), float(cline[0][2]))]

    n_samples = max(2, int(math.ceil(total_L / step_dist)) + 1)
    d = np.linspace(0.0, total_L, n_samples)
    x = np.interp(d, L, cline[:, 0])
    z = np.interp(d, L, cline[:, 2])

    d_ease = min(ease_dist, total_L / 3.0)
    dy_total = y_end - y_start
    m = dy_total / max(1e-4, total_L - d_ease)

    y = np.zeros_like(d)
    for k, s in enumerate(d):
        if s <= 0:
            y[k] = y_start
        elif s < d_ease:
            y[k] = y_start + 0.5 * (m / d_ease) * (s ** 2)
        elif s <= total_L - d_ease:
            y[k] = y_start + 0.5 * m * d_ease + m * (s - d_ease)
        elif s < total_L:
            ds = total_L - s
            y[k] = y_end - 0.5 * (m / d_ease) * (ds ** 2)
        else:
            y[k] = y_end

    return [(round(float(x[i]), 3), round(float(y[i]), 3), round(float(z[i]), 3)) for i in range(len(d))]


def rasterize(obs, bounds, cell, radius):
    x0, z0, x1, z1 = bounds
    nx = max(1, int(math.ceil((x1 - x0) / cell)))
    nz = max(1, int(math.ceil((z1 - z0) / cell)))
    gx, gz = np.meshgrid(
        x0 + (np.arange(nx) + 0.5) * cell,
        z0 + (np.arange(nz) + 0.5) * cell,
        indexing="ij",
    )
    g = np.zeros((nx, nz), bool)
    for o in obs:
        if o[0] == "disc":
            g |= (gx - o[1][0]) ** 2 + (gz - o[1][1]) ** 2 <= o[2] ** 2
            continue
        P = o[1]
        if len(P) < 3:
            continue
        inside = np.ones_like(g)
        for i in range(len(P)):
            a, b = P[i], P[(i + 1) % len(P)]
            inside &= ((b[0] - a[0]) * (gz - a[1]) - (b[1] - a[1]) * (gx - a[0])) >= -1e-9
        g |= inside
    k = int(math.ceil(radius / cell))
    inf = np.zeros_like(g)
    offs = [(i, j) for i in range(-k, k + 1) for j in range(-k, k + 1) if i * i + j * j <= k * k]
    for i, j in offs:
        s = np.zeros_like(g)
        xs, xe = max(0, i), nx + min(0, i)
        zs, ze = max(0, j), nz + min(0, j)
        s[xs:xe, zs:ze] = g[xs - i : xe - i, zs - j : ze - j]
        inf |= s
    return inf, (x0, z0, cell)


def astar(blocked, s, g):
    nx, nz = blocked.shape
    D = [
        (1, 0, 1.0),
        (-1, 0, 1.0),
        (0, 1, 1.0),
        (0, -1, 1.0),
        (1, 1, 1.4142),
        (1, -1, 1.4142),
        (-1, 1, 1.4142),
        (-1, -1, 1.4142),
    ]
    h = lambda a: max(abs(a[0] - g[0]), abs(a[1] - g[1])) + 0.4142 * min(abs(a[0] - g[0]), abs(a[1] - g[1]))
    pq = [(h(s), 0.0, s)]
    came = {s: None}
    cost = {s: 0.0}
    while pq:
        _, c, u = heapq.heappop(pq)
        if u == g:
            break
        if c > cost[u]:
            continue
        for dx, dz, w in D:
            v = (u[0] + dx, u[1] + dz)
            if not (0 <= v[0] < nx and 0 <= v[1] < nz) or blocked[v]:
                continue
            if dx and dz and (blocked[u[0] + dx, u[1]] or blocked[u[0], u[1] + dz]):
                continue
            nc = c + w
            if nc < cost.get(v, 1e18):
                cost[v] = nc
                came[v] = u
                heapq.heappush(pq, (nc + h(v), nc, v))
    if g not in came:
        return None
    path = []
    u = g
    while u is not None:
        path.append(u)
        u = came[u]
    return path[::-1]


def line_free(blocked, a, b):
    n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) * 3) + 1
    for f in np.linspace(0.0, 1.0, n + 1):
        ix = int(round(a[0] + (b[0] - a[0]) * f))
        iz = int(round(a[1] + (b[1] - a[1]) * f))
        if blocked[ix, iz]:
            return False
    return True


def _plan_single_level(start, goal, obs, radius, cell, margin):
    """2D A* navigation around obstacles with line-of-sight shortcutting."""
    start = np.asarray(start, float)
    goal = np.asarray(goal, float)
    pts_list = [start, goal]
    for o in obs:
        if o[0] == "poly":
            pts_list.extend(o[1])
        else:
            pts_list.append(o[1])
    pts = np.asarray(pts_list, float)
    bounds = (
        pts[:, 0].min() - margin,
        pts[:, 1].min() - margin,
        pts[:, 0].max() + margin,
        pts[:, 1].max() + margin,
    )
    blocked, (x0, z0, c) = rasterize(obs, bounds, cell, radius)
    cellof = lambda p: (int((p[0] - x0) / c), int((p[1] - z0) / c))
    s, g = cellof(start), cellof(goal)
    if not (0 <= s[0] < blocked.shape[0] and 0 <= s[1] < blocked.shape[1]) or blocked[s]:
        raise ValueError(f"start {tuple(np.round(start, 3))} is inside the keep-out zone of an asset (radius {radius} m)")
    if not (0 <= g[0] < blocked.shape[0] and 0 <= g[1] < blocked.shape[1]) or blocked[g]:
        raise ValueError(
            f"goal {tuple(np.round(goal, 3))} is inside the keep-out zone of an asset; use an anchor or move the goal"
        )
    raw = astar(blocked, s, g)
    if raw is None:
        raise ValueError("no route exists between start and goal")
    out = [raw[0]]
    i = 0
    while i < len(raw) - 1:
        j = len(raw) - 1
        while j > i + 1 and not line_free(blocked, raw[i], raw[j]):
            j -= 1
        out.append(raw[j])
        i = j
    wp = [tuple(start)] + [(x0 + (p[0] + 0.5) * c, z0 + (p[1] + 0.5) * c) for p in out[1:-1]] + [tuple(goal)]
    return wp


def plan(
    stage,
    db,
    start,
    goal,
    times,
    radius=0.45,
    cell=0.1,
    margin=6.0,
    skip=(),
    skip_parts=(),
    from_level=None,
    to_level=None,
):
    """Plan safe walking route between start and goal across one or more levels connected by stairs."""
    start = np.asarray(start, float)
    goal = np.asarray(goal, float)
    is_3d_input = (len(start) == 3 or len(goal) == 3)
    y_start = float(start[1]) if len(start) == 3 else 0.0
    y_goal = float(goal[1]) if len(goal) == 3 else 0.0
    start_xz = start[[0, 2]] if len(start) == 3 else start
    goal_xz = goal[[0, 2]] if len(goal) == 3 else goal

    levels = scene_levels(stage, db)
    if from_level is None:
        from_level = min(levels.keys(), key=lambda lid: abs(levels[lid] - y_start)) if levels else "L0"
    if to_level is None:
        to_level = min(levels.keys(), key=lambda lid: abs(levels[lid] - y_goal)) if levels else "L0"

    all_stairs = scene_stairs(stage, db, times[0] if len(times) else 0.0)

    # Single-level plan
    if from_level == to_level or not all_stairs or abs(y_start - y_goal) < 0.4:
        floor_y = levels.get(from_level, y_start)
        obs = obstacles(stage, db, times, skip=skip, skip_parts=skip_parts, floor_y=floor_y)
        wp_xz = _plan_single_level(start_xz, goal_xz, obs, radius, cell, margin)
        if is_3d_input:
            return [(round(float(p[0]), 3), round(float(floor_y), 3), round(float(p[1]), 3)) for p in wp_xz]
        return wp_xz

    # Multi-level plan via stair portal
    stair = next(
        (s for s in all_stairs if s["from_level"] == from_level and s["to_level"] == to_level),
        None,
    )
    is_descending = False
    if stair is None:
        stair = next(
            (s for s in all_stairs if s["from_level"] == to_level and s["to_level"] == from_level),
            None,
        )
        if stair is not None:
            is_descending = True

    if stair is None:
        raise ValueError(f"no stair connects level '{from_level}' and '{to_level}'")

    cline = np.asarray(stair["centerline_world"], float)
    if is_descending:
        stair_entry = cline[-1]
        stair_exit = cline[0]
        stair_cline = cline[::-1]
        y_bot = float(cline[0][1])
        y_top = float(cline[-1][1])
        floor_y_start = levels.get(from_level, y_top)
        floor_y_goal = levels.get(to_level, y_bot)
    else:
        stair_entry = cline[0]
        stair_exit = cline[-1]
        stair_cline = cline
        y_bot = float(cline[0][1])
        y_top = float(cline[-1][1])
        floor_y_start = levels.get(from_level, y_bot)
        floor_y_goal = levels.get(to_level, y_top)

    # 1. Start level path to stair entrance
    obs_start = obstacles(stage, db, times, skip=skip, skip_parts=skip_parts, floor_y=floor_y_start)
    wp1_xz = _plan_single_level(start_xz, stair_entry[[0, 2]], obs_start, radius, cell, margin)
    wp1 = [(round(float(p[0]), 3), round(float(floor_y_start if idx > 0 else y_start), 3), round(float(p[1]), 3)) for idx, p in enumerate(wp1_xz)]

    # 2. Stair traversal with continuous eased nosing elevation
    wp_stair = generate_stair_waypoints(stair_cline, floor_y_start, floor_y_goal)

    # 3. Goal level path from stair exit
    obs_goal = obstacles(stage, db, times, skip=skip, skip_parts=skip_parts, floor_y=floor_y_goal)
    wp3_xz = _plan_single_level(stair_exit[[0, 2]], goal_xz, obs_goal, radius, cell, margin)
    n3 = len(wp3_xz)
    wp3 = [(round(float(p[0]), 3), round(float(floor_y_goal if idx < n3 - 1 else y_goal), 3), round(float(p[1]), 3)) for idx, p in enumerate(wp3_xz)]

    combined = wp1[:-1] + wp_stair + wp3[1:]
    return combined


def sample_path(wp, speed, fps, max_turn_dps=270.0, end_facing_deg=None, return_pitch=False):
    """Positions and headings at every frame (constant speed, limited turn rate).
    Supports both 2D (x, z) and 3D (x, y, z) waypoints with continuous torso pitch evaluation.
    """
    P = np.asarray(wp, float)
    is_3d = (P.shape[1] == 3)
    if len(P) < 2 or np.linalg.norm(P[-1] - P[0]) < 1e-6:
        pos = np.repeat(P[:1], 2, axis=0)
        h0 = float(end_facing_deg) if end_facing_deg is not None else 0.0
        pitch = np.zeros(2, float)
        if return_pitch:
            return pos, np.array([h0, h0], float), pitch
        return pos, np.array([h0, h0], float)

    if is_3d:
        seg = np.hypot(np.diff(P[:, 0]), np.diff(P[:, 2]))
    else:
        seg = np.linalg.norm(np.diff(P, axis=0), axis=1)

    L = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(2, int(math.ceil(L[-1] / max(speed, 1e-6) * fps)) + 1)
    d = np.minimum(np.arange(n) * speed / fps, L[-1])

    if is_3d:
        pos_x = np.interp(d, L, P[:, 0])
        pos_y = np.interp(d, L, P[:, 1])
        pos_z = np.interp(d, L, P[:, 2])
        pos = np.stack([pos_x, pos_y, pos_z], 1)
        si = np.clip(np.searchsorted(L, d, side="right") - 1, 0, len(seg) - 1)
        tgt = np.array([heading_deg(P[i + 1, 0] - P[i, 0], P[i + 1, 2] - P[i, 2]) for i in si], float)

        # Compute pitch based on elevation slope along segment
        dy = P[:, 1][1:] - P[:, 1][:-1]
        slope = np.zeros(len(seg), float)
        mask = seg > 1e-4
        slope[mask] = dy[mask] / seg[mask]

        raw_pitch = np.zeros(n, float)
        for k in range(n):
            idx = si[k]
            sl = slope[idx]
            if sl > 0.15:
                raw_pitch[k] = 7.5   # Forward lean when ascending
            elif sl < -0.15:
                raw_pitch[k] = -3.0  # Slight lean back when descending
            else:
                raw_pitch[k] = 0.0
        window = max(3, int(fps * 0.25))
        pitch = np.convolve(raw_pitch, np.ones(window) / window, mode="same")
    else:
        pos = np.stack([np.interp(d, L, P[:, 0]), np.interp(d, L, P[:, 1])], 1)
        si = np.clip(np.searchsorted(L, d, side="right") - 1, 0, len(seg) - 1)
        tgt = np.array([heading_deg(*(P[i + 1] - P[i])) for i in si], float)
        pitch = np.zeros(n, float)

    if end_facing_deg is not None:
        tgt[-1] = float(end_facing_deg)
    head = np.zeros(n, float)
    head[0] = tgt[0]
    for k in range(1, n):
        diff = (tgt[k] - head[k - 1] + 180.0) % 360.0 - 180.0
        m = max_turn_dps / fps
        head[k] = head[k - 1] + max(-m, min(m, diff))
    if end_facing_deg is not None:
        head[-1] = float(end_facing_deg)

    if return_pitch:
        return pos, head, pitch
    return pos, head


def _actor_needs_pos_z_flip(prim):
    """True only for legacy un-normalized +Z characters."""
    for p in Usd.PrimRange(prim):
        if p.GetCustomDataByKey("painterspace_normalized"):
            return False
    fwd = str(prim.GetCustomDataByKey("forward_axis") or "").strip().upper()
    return fwd in ("+Z", "Z")


def author_root_motion(stage, prim_path, wp, speed, t0, fps, y=None, end_facing_deg=None, pitch=True):
    """Author character root translation and orientation time samples on stage."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        raise ValueError(f"Actor prim not found: {prim_path}")
    if y is None:
        y0 = actor_y(stage, prim, t0)
        y = 0.0 if abs(y0) < 0.05 else y0
    x = UsdGeom.Xformable(prim)
    ops = {o.GetOpType(): o for o in x.GetOrderedXformOps()}
    tr = ops.get(UsdGeom.XformOp.TypeTranslate) or x.AddTranslateOp()
    ry = ops.get(UsdGeom.XformOp.TypeRotateY) or x.AddRotateYOp()
    rx = ops.get(UsdGeom.XformOp.TypeRotateX)

    tr.GetAttr().Clear()
    ry.GetAttr().Clear()

    pos, head, pitches = sample_path(wp, speed, fps, end_facing_deg=end_facing_deg, return_pitch=True)
    flip = _actor_needs_pos_z_flip(prim)
    has_elevation = (pos.shape[1] == 3)
    has_pitch = pitch and any(abs(p) > 0.1 for p in pitches)

    if has_pitch and rx is None:
        rx = x.AddRotateXOp()
    if rx is not None:
        rx.GetAttr().Clear()

    for k in range(len(pos)):
        t = t0 + k
        yk = pos[k, 1] if has_elevation else y
        zk = pos[k, 2] if has_elevation else pos[k, 1]
        tr.Set(Gf.Vec3d(float(pos[k, 0]), float(yk), float(zk)), t)
        deg = float(180.0 - head[k]) if flip else float(-head[k])
        ry.Set(deg, t)
        if rx is not None:
            rx.Set(float(-pitches[k]) if has_pitch else 0.0, t)
    return t0 + len(pos) - 1


def anchor_world_3d(stage, db, ref, t=0.0):
    """Return (world_xyz, world_facing_deg) for 'InstanceName:anchor_name' at time t."""
    name, anchor = ref.split(":", 1)
    objs = scene_objects(stage, db)
    inst = next((o for o in objs if o.name == name or o.asset_id == name), None)
    if inst is None:
        raise ValueError(f"Instance '{name}' not found on stage")
    anchors = (inst.entry or {}).get("anchors") or {}
    if anchor not in anchors:
        raise ValueError(f"Anchor '{anchor}' not found on {name} (available: {list(anchors)})")
    a = anchors[anchor]
    M = l2w(UsdGeom.XformCache(Usd.TimeCode(float(t))), inst.prim)
    p = apply(M, np.array([a["pos"]], float))[0]
    rad = math.radians(float(a.get("facing_deg", 0.0)))
    v_local = np.array([math.sin(rad), 0.0, -math.cos(rad)], float)
    v_world = M[:3, :3] @ v_local
    w_facing = heading_deg(v_world[0], v_world[2])
    return p, w_facing


def anchor_world(stage, db, ref, t=0.0):
    """Return (world_xz, world_facing_deg) for 'InstanceName:anchor_name' at time t."""
    p, w_facing = anchor_world_3d(stage, db, ref, t)
    return p[[0, 2]], w_facing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--actor", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--to", nargs="+", type=float, metavar="COORD", help="Destination (X Z or X Y Z)")
    g.add_argument("--to-anchor", metavar="Instance:anchor")
    ap.add_argument("--from", dest="frm", nargs="+", type=float, help="Start position (X Z or X Y Z)")
    ap.add_argument("--from-level", help="Source level (e.g. L0)")
    ap.add_argument("--to-level", help="Destination level (e.g. L1)")
    ap.add_argument("--speed", type=float, default=1.4)
    ap.add_argument("--radius", type=float, default=0.45)
    ap.add_argument("--cell", type=float, default=0.1)
    ap.add_argument("--t0", type=float, default=0.0)
    ap.add_argument("--t1", type=float)
    ap.add_argument("--skip-part", action="append", default=[], help="part to ignore during walk, e.g. Car:door_fl")
    out_group = ap.add_mutually_exclusive_group(required=True)
    out_group.add_argument("--out", help="layer file to write (or same as stage for in-place)")
    out_group.add_argument("--in-place", action="store_true", help="write directly to the stage's root layer")
    a = ap.parse_args(argv)

    stage = Usd.Stage.Open(a.stage)
    if not stage:
        print(f"PLAN FAILED: cannot open {a.stage}", file=sys.stderr)
        return 2
    db = AssetDB(a.manifest)
    tcps = stage.GetTimeCodesPerSecond() or 24.0
    objs = scene_objects(stage, db)
    actor = next((o for o in objs if o.name == a.actor or str(o.path) == a.actor), None)
    if actor is None:
        print(f"PLAN FAILED: actor '{a.actor}' not found on stage", file=sys.stderr)
        return 2

    start = np.array(a.frm, float) if a.frm else actor_xz(stage, actor.prim, a.t0)
    end_facing = None
    if a.to:
        goal = np.array(a.to, float)
    else:
        goal_3d, end_facing = anchor_world_3d(stage, db, a.to_anchor, a.t0)
        goal = goal_3d

    t1 = a.t1 if a.t1 is not None else stage.GetEndTimeCode()
    times = np.arange(a.t0, max(a.t0, t1) + 1e-9, max(1.0, tcps / 2))
    skip_parts = []
    for sp in a.skip_part:
        if ":" in sp:
            skip_parts.append(tuple(sp.split(":", 1)))
        else:
            skip_parts.append(sp)

    try:
        wp = plan(
            stage,
            db,
            start,
            goal,
            times,
            a.radius,
            a.cell,
            skip=(actor.name,),
            skip_parts=skip_parts,
            from_level=a.from_level,
            to_level=a.to_level,
        )
    except ValueError as e:
        print("PLAN FAILED:", e, file=sys.stderr)
        return 1

    in_place = a.in_place or (a.out and os.path.abspath(a.out) == os.path.abspath(a.stage))
    if in_place:
        stage.SetEditTarget(stage.GetRootLayer())
        t_end = author_root_motion(
            stage, str(actor.prim.GetPath()), wp, a.speed, a.t0, tcps, end_facing_deg=end_facing
        )
        stage.GetRootLayer().Save()
        out_target = a.stage
    else:
        if os.path.exists(a.out):
            os.remove(a.out)
        layer = Sdf.Layer.CreateNew(a.out)
        stage.SetEditTarget(layer)
        t_end = author_root_motion(
            stage, str(actor.prim.GetPath()), wp, a.speed, a.t0, tcps, end_facing_deg=end_facing
        )
        layer.Save()
        out_target = a.out

    dist = float(sum(np.linalg.norm(np.diff(np.array(wp, float), axis=0), axis=1)))
    print(f"route: {len(wp)} waypoints, {dist:.2f} m, ends at frame {t_end:g}")
    print("waypoints:", [(round(float(x), 2), round(float(z), 2)) for x, z in wp])
    print("written:", out_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
