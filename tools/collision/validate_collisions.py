#!/usr/bin/env python3
"""
validate_collisions.py - fail any shot where a character's body passes through another asset.

For every time step (default 2 per frame) it poses the character's
skeleton, builds capsules along the bones, and measures the distance to every collision shape of
every other object in world space (manifest assets, inline primitive objects, and other actors).
Doors, hood, etc. are tested at their CURRENT transform in USD.
Between characters, arm/leg contacts are permitted by default while pelvis,
torso, neck and head contacts remain blocked. --strict-actor-limbs restores
full-body character-to-character checks. Character-to-object checks always
include all body parts.
These posed-body checks are sampled; they do not prove collision freedom between
samples. The compiler separately checks synthesized actor-root envelopes continuously.

  python tools/collision/validate_collisions.py shot.usda --manifest manifest.json [--allow allow.json] [--json report.json]

Exit code 0 = clean, 1 = penetration found, 2 = could not run.

allow.json lists contacts that ARE meant to happen (sit, open a door, touch):
  [ {"actor": "Person_01", "asset": "cars.car1", "shape": "door_fr", "joints": ["RightHand"],
     "t0": 2.0, "t1": 3.5} ]
Every key except "actor" is optional. "shape" matches by prefix ("door_fr" covers door_fr_0, door_fr_1...).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
import os
import sys

import numpy as np
from pxr import Usd, UsdGeom

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contact_windows import contains_time, window_error
from colliders import (
    STEP_HEIGHT_M,
    VISIBLE_PURPOSES,
    AssetDB,
    CharacterModel,
    apply,
    is_visible,
    l2w,
    scene_objects,
    segment_distance,
    is_limb_segment,
    world_groups,
)


def _allowed(rules, actor, obj, shape_id, part, joint, sec, depth=0.0):
    effective_part = part or "body"
    candidates = (rules.get(actor, []) + rules.get(None, [])) if isinstance(rules, dict) else (rules or ())
    for r in candidates:
        if r.get("actor") not in (None, actor):
            continue
        if r.get("asset") not in (None, obj.asset_id, obj.name):
            continue
        if r.get("part") and effective_part != r["part"]:
            continue
        if r.get("shape"):
            pat = r["shape"]
            if not (shape_id.startswith(pat) or (part and part.startswith(pat))):
                continue
        if r.get("joints") and not any(j.lower() in joint.lower() for j in r["joints"]):
            continue
        if not contains_time(r, sec):
            continue
        if "max_depth_m" in r and depth > float(r["max_depth_m"]):
            continue
        return True
    return False


def _invalid_contact_windows(allow, allow_actor_limb_contacts):
    invalid = [{'rule_index': index, 'actor': rule.get('actor'), 'asset': rule.get('asset'),
                'reason': reason}
               for index, rule in enumerate(allow or []) if (reason := window_error(rule))]
    if not invalid:
        return None
    return {'ok': False, 'actor_contact_policy': 'core_body' if allow_actor_limb_contacts else 'full_body',
            'time_steps': 0, 'actors': [], 'assets': [], 'unprotected_assets': [],
            'unprotected_actors': [], 'violations': [], 'level_violations': [],
            'error': {'error_code': 'CONTACT_WINDOW_INVALID',
                      'message': 'Contact timing is invalid; spatial clearance cannot repair it.',
                      'details': {'rules': invalid}}}


def validate(stage, db, t0=None, t1=None, substeps=2, tol=0.01, margin=0.0, allow=None,
             sample_times=None, check_levels=True, allow_actor_limb_contacts=True):
    invalid = _invalid_contact_windows(allow, allow_actor_limb_contacts)
    if invalid:
        return invalid
    # Contact rules are consulted for every penetrating capsule. Index once
    # instead of scanning the entire shot's rules on each limb comparison.
    indexed_allow = {}
    for rule in allow or []:
        indexed_allow.setdefault(rule.get("actor"), []).append(rule)
    objs = scene_objects(stage, db)
    actors = [o for o in objs if o.is_actor]
    props = [o for o in objs if not o.is_actor and o.protected]
    unprotected = [
        o.name
        for o in objs
        if not o.is_actor
        and not o.protected
        and not any(tag in (o.entry or {}).get("tags", []) for tag in ("environment", "camera"))
    ]

    tcps = stage.GetTimeCodesPerSecond() or 24.0
    start = stage.GetStartTimeCode() if t0 is None else float(t0)
    end = stage.GetEndTimeCode() if t1 is None else float(t1)
    if end < start:
        end = start
    times = (np.asarray(sample_times, dtype=float) if sample_times is not None
             else np.arange(start, end + 1e-9, 1.0 / max(1, substeps)))

    models = {}
    unprotected_actors = []
    for a in actors:
        try:
            model = CharacterModel(stage, a)
            if not model.segs and not model.spheres:
                raise ValueError(f"{a.path} has no usable body segments")
            models[a.name] = model
        except ValueError:
            unprotected_actors.append(a.name)
    actors = [a for a in actors if a.name in models]

    rec = {}
    for t in times:
        tc = Usd.TimeCode(float(t))
        xc = UsdGeom.XformCache(tc)
        bbc = UsdGeom.BBoxCache(tc, list(VISIBLE_PURPOSES))
        sec = float(t) / tcps
        live_props = [p for p in props if is_visible(p.prim, tc)]
        live_actors = [a for a in actors if is_visible(a.prim, tc)]
        groups = {p.name: world_groups(stage, p, tc, xc, bbc) for p in live_props}
        actor_joints = {a.name: models[a.name].joint_world(tc, xc) for a in live_actors}

        # 1. Actor vs. Asset / Inline Object
        for a in live_actors:
            pts, rad, lab = models[a.name].samples(tc, xc, joints=actor_joints[a.name])
            if len(pts) == 0:
                continue
            max_r = float(rad.max()) + margin
            for p in live_props:
                for grp in groups[p.name]:
                    P_local = grp.local(pts)
                    cand = grp.candidates(P_local, max_r)
                    if len(cand) == 0:
                        continue
                    D = grp.sd(P_local, cand) - rad[:, None] - margin
                    bad_pts, bad_cands = np.where(D < -tol)
                    if len(bad_pts) == 0:
                        continue
                    part_name = grp.part or "body"
                    worst_pair = None
                    worst_d = 0.0
                    for pi_idx, ci_idx in zip(bad_pts, bad_cands):
                        box_idx = int(cand[ci_idx])
                        sid = grp.ids[box_idx]
                        j_label = lab[int(pi_idx)]
                        depth = float(-D[pi_idx, ci_idx])
                        if _allowed(indexed_allow, a.name, p, sid, grp.part, j_label, sec, depth):
                            continue
                        if depth > worst_d:
                            worst_d = depth
                            worst_pair = (int(pi_idx), box_idx, sid, j_label, P_local[int(pi_idx)])
                    if worst_pair is None:
                        continue
                    pi_idx, box_idx, sid, j_label, p_loc = worst_pair
                    key = (a.name, p.name, part_name)
                    r = rec.setdefault(
                        key,
                        {
                            "actor": a.name,
                            "asset": p.asset_id,
                            "instance": p.name,
                            "part": part_name,
                            "first_s": sec,
                            "last_s": sec,
                            "frames": 0,
                            "worst_depth_m": 0.0,
                        },
                    )
                    r["last_s"] = sec
                    r["frames"] += 1
                    if worst_d > r["worst_depth_m"]:
                        g_dir = grp.push_dir(p_loc, box_idx)
                        r.update(
                            worst_depth_m=worst_d,
                            worst_s=sec,
                            body_part=j_label,
                            shape=sid,
                            push_out_m=[round(float(v), 3) for v in g_dir * (worst_d + tol)],
                        )

        # 1b. Seated Occupants vs Host Vehicle Cabin Volume
        for al in (allow or []):
            if not al.get("is_occupant"):
                continue
            act_name = al.get("actor")
            veh_name = al.get("asset")
            if not act_name or not veh_name or act_name not in models:
                continue
            if not contains_time(al, sec):
                continue
            veh_prop = next((p for p in live_props if p.name == veh_name or p.asset_id == veh_name), None)
            if not veh_prop:
                continue
            veh_M = l2w(xc, veh_prop.prim)
            try:
                inv_M = np.linalg.inv(veh_M)
            except np.linalg.LinAlgError:
                continue

            v_entry = db.lookup_id(veh_prop.asset_id) if hasattr(db, "lookup_id") else (db.get(veh_prop.asset_id) if hasattr(db, "get") else None)
            cabin = v_entry.get("cabin") if v_entry else None
            if cabin:
                c_min, c_max = np.array(cabin["min"], float), np.array(cabin["max"], float)
            else:
                v_bounds = v_entry.get("bounds_m", [2.0, 1.47, 4.8]) if v_entry else [2.0, 1.47, 4.8]
                c_min = np.array([-v_bounds[0] * 0.48, 0.05, -v_bounds[2] * 0.35])
                c_max = np.array([v_bounds[0] * 0.48, v_bounds[1] + 0.02, v_bounds[2] * 0.35])

            pts, _, lab = models[act_name].samples(tc, xc)
            if len(pts) == 0:
                continue
            P_veh = apply(inv_M, pts)
            # Once the actor is outside the cabin footprint, rising beside the
            # door is not a roof penetration. Points still under the roof remain
            # checked during entry and exit as well as while seated.
            inside_roof = ((P_veh[:, 0] >= c_min[0]) & (P_veh[:, 0] <= c_max[0])
                           & (P_veh[:, 2] >= c_min[2]) & (P_veh[:, 2] <= c_max[2]))
            if not np.any(inside_roof):
                continue
            roof_viol = np.where(inside_roof, P_veh[:, 1] - c_max[1], -np.inf)
            worst_idx = int(np.argmax(roof_viol))
            worst_pen = float(roof_viol[worst_idx])
            if worst_pen > tol:
                key = (act_name, veh_name, "cabin_roof")
                rec_r = rec.setdefault(key, {
                    "actor": act_name,
                    "asset": veh_prop.asset_id,
                    "instance": veh_name,
                    "part": "cabin_roof",
                    "first_s": sec,
                    "last_s": sec,
                    "frames": 0,
                    "worst_depth_m": 0.0,
                })
                rec_r["last_s"] = sec
                rec_r["frames"] += 1
                if worst_pen > rec_r["worst_depth_m"]:
                    rec_r.update(
                        worst_depth_m=worst_pen,
                        worst_s=sec,
                        body_part=lab[worst_idx],
                        shape="cabin_volume",
                        push_out_m=[0.0, round(-worst_pen, 3), 0.0],
                    )

        # 2. Actor vs. Actor (full-body capsules: torso + limbs)
        if len(live_actors) > 1:
            capsules = {
                a.name: (models[a.name].body_capsules(tc, xc, joints=actor_joints[a.name]) if hasattr(models[a.name], "body_capsules") else models[a.name].torso_capsules(tc, xc))
                for a in live_actors
            }
            if allow_actor_limb_contacts:
                # Limb contacts are permitted only between characters. The
                # actor-vs-solid pass above continues checking every body part.
                capsules = {name: [part for part in parts if not is_limb_segment(part[3])]
                            for name, parts in capsules.items()}
            segment_bounds = {
                name: [(min(p[0], q[0]), max(p[0], q[0]),
                        min(p[1], q[1]), max(p[1], q[1]),
                        min(p[2], q[2]), max(p[2], q[2]))
                       for p, q, _, _ in parts]
                for name, parts in capsules.items()
            }
            # Whole-body bounds reject distant actors before the much more
            # expensive segment-to-segment tests. Include capsule radii and
            # the requested margin so this cannot discard a real collision.
            capsule_bounds = {}
            for name, parts in capsules.items():
                if not parts:
                    continue
                low = np.min([np.minimum(p, q) - (radius + margin + tol)
                              for p, q, radius, _ in parts], axis=0)
                high = np.max([np.maximum(p, q) + (radius + margin + tol)
                               for p, q, radius, _ in parts], axis=0)
                capsule_bounds[name] = (low, high)
            for i in range(len(live_actors)):
                for j in range(i + 1, len(live_actors)):
                    a1, a2 = live_actors[i], live_actors[j]
                    bounds1 = capsule_bounds.get(a1.name)
                    bounds2 = capsule_bounds.get(a2.name)
                    if bounds1 is None or bounds2 is None or np.any(bounds1[1] < bounds2[0]) or np.any(bounds2[1] < bounds1[0]):
                        continue
                    worst_d = 0.0
                    worst_info = None
                    for index1, (p1, q1, r1, l1) in enumerate(capsules[a1.name]):
                        b1 = segment_bounds[a1.name][index1]
                        for index2, (p2, q2, r2, l2) in enumerate(capsules[a2.name]):
                            b2 = segment_bounds[a2.name][index2]
                            reach = r1 + r2 + margin + tol
                            if (b1[0] > b2[1] + reach or b2[0] > b1[1] + reach or
                                    b1[2] > b2[3] + reach or b2[2] > b1[3] + reach or
                                    b1[4] > b2[5] + reach or b2[4] > b1[5] + reach):
                                continue
                            dist = segment_distance(p1, q1, p2, q2) - (r1 + r2 + margin)
                            if dist < -tol:
                                depth = float(-dist)
                                if _allowed(indexed_allow, a1.name, a2, "body", "body", l1, sec, depth) or _allowed(indexed_allow, a2.name, a1, "body", "body", l2, sec, depth):
                                    continue
                                if depth > worst_d:
                                    worst_d = depth
                                    diff = ((p1 + q1) - (p2 + q2)) * 0.5
                                    diff[1] = 0.0
                                    norm = float(np.linalg.norm(diff))
                                    g_dir = diff / norm if norm > 1e-6 else np.array([1.0, 0.0, 0.0])
                                    worst_info = (l1, g_dir)
                    if worst_info is not None:
                        l1, g_dir = worst_info
                        key = (a1.name, a2.name, "body")
                        r = rec.setdefault(
                            key,
                            {
                                "actor": a1.name,
                                "asset": a2.asset_id,
                                "instance": a2.name,
                                "part": "body",
                                "first_s": sec,
                                "last_s": sec,
                                "frames": 0,
                                "worst_depth_m": 0.0,
                            },
                        )
                        r["last_s"] = sec
                        r["frames"] += 1
                        if worst_d > r["worst_depth_m"]:
                            r.update(
                                worst_depth_m=worst_d,
                                worst_s=sec,
                                body_part=l1,
                                shape="body_capsule",
                                push_out_m=[round(float(v), 3) for v in g_dir * (worst_d + tol)],
                            )

        # 3. Any animated rigid asset (vehicle, animal, or prop) vs. other solids.
        # Skeletal actors are checked above using their posed body capsules.
        moving_solids = [
            p for p in live_props
            if any(op.GetNumTimeSamples() > 1 for op in UsdGeom.Xformable(p.prim).GetOrderedXformOps())
        ]
        for v_obj in moving_solids:
            v_grps = groups.get(v_obj.name, [])
            if not v_grps:
                continue
            v_pts_list = []
            for vg in v_grps:
                if vg.part is None:  # main collision boxes, excluding moving attachments
                    corners = vg.corners_world().reshape(-1, 3)
                    # Centers catch full containment, where every corner lies
                    # on or outside the other box's surface.
                    points = np.vstack((corners, apply(vg.T, vg.c)))
                    # Ground contact and step-height curbs are support surfaces.
                    high_pts = points[points[:, 1] > STEP_HEIGHT_M]
                    if len(high_pts) > 0:
                        v_pts_list.append(high_pts)
            if not v_pts_list:
                continue
            v_pts = np.vstack(v_pts_list)
            for p_other in live_props:
                if p_other.name == v_obj.name or (p_other in moving_solids and p_other.name < v_obj.name):
                    continue
                for grp in groups.get(p_other.name, []):
                    P_local = grp.local(v_pts)
                    cand = grp.candidates(P_local, 0.05)
                    if len(cand) == 0:
                        continue
                    D = grp.sd(P_local, cand)
                    bad_pts, bad_cands = np.where(D < -tol)
                    if len(bad_pts) == 0:
                        continue
                    worst_d = 0.0
                    worst_pair = None
                    for pi_idx, ci_idx in zip(bad_pts, bad_cands):
                        box_idx = int(cand[ci_idx])
                        sid = grp.ids[box_idx]
                        depth = float(-D[pi_idx, ci_idx])
                        if _allowed(indexed_allow, v_obj.name, p_other, sid, grp.part, "body", sec, depth):
                            continue
                        if depth > worst_d:
                            worst_d = depth
                            worst_pair = (box_idx, sid, P_local[int(pi_idx)])
                    if worst_pair is not None:
                        box_idx, sid, p_loc = worst_pair
                        key = (v_obj.name, p_other.name, grp.part or "body")
                        r = rec.setdefault(
                            key,
                            {
                                "actor": v_obj.name,
                                "asset": p_other.asset_id,
                                "instance": p_other.name,
                                "part": grp.part or "body",
                                "first_s": sec,
                                "last_s": sec,
                                "frames": 0,
                                "worst_depth_m": 0.0,
                            },
                        )
                        r["last_s"] = sec
                        r["frames"] += 1
                        if worst_d > r["worst_depth_m"]:
                            g_dir = grp.push_dir(p_loc, box_idx)
                            r.update(
                                worst_depth_m=worst_d,
                                worst_s=sec,
                            body_part="body",
                                shape=sid,
                                push_out_m=[round(float(v), 3) for v in g_dir * (worst_d + tol)],
                            )

    level_viol = _level_violations(stage, db, actors, times, allow=allow) if check_levels else []

    viol = sorted(rec.values(), key=lambda r: -r["worst_depth_m"])
    for r in viol:
        _finalize_violation(r)
    return {
        "ok": (not viol) and (not level_viol) and (not unprotected) and (not unprotected_actors),
        "actor_contact_policy": "core_body" if allow_actor_limb_contacts else "full_body",
        "time_steps": len(times),
        "actors": [a.name for a in actors],
        "assets": [p.name for p in props],
        "unprotected_assets": unprotected,
        "unprotected_actors": unprotected_actors,
        "violations": viol,
        "level_violations": level_viol,
    }


def _finalize_violation(r):
    r["first_s"], r["last_s"], r["worst_s"] = (round(r[k], 3) for k in ("first_s", "last_s", "worst_s"))
    r["worst_depth_m"] = round(r["worst_depth_m"], 3)
    r["advice"] = (
        f"{r['actor']} passes through {r['instance']} ({r['asset']}, part '{r['part']}') "
        f"from {r['first_s']} s to {r['last_s']} s, up to {r['worst_depth_m']} m deep ({r['body_part']}). "
        f"Move {r['actor']} by {r['push_out_m']} m at {r['worst_s']} s, or re-plan the route (plan_path.py) / use an anchor."
    )


def _level_violations(stage, db, actors, times, allow=None):
    stairs = None
    try:
        import plan_path as pp
        stairs = pp.scene_stairs(stage, db, t=float(times[0]))
    except Exception:
        pass
    return check_level_integrity(stage, db, actors, times, stairs=stairs, allow=allow)


def _open_stage(stage_path, user_edits=None):
    if user_edits:
        from pxr import Sdf
        root = Sdf.Layer.FindOrOpen(stage_path)
        edits = Sdf.Layer.FindOrOpen(user_edits)
        return Usd.Stage.Open(root, edits) if root and edits else None
    return Usd.Stage.Open(stage_path)


def _validate_chunk(args):
    stage_path, manifest_path, user_edits, times, substeps, tol, margin, allow, allow_actor_limb_contacts = args
    stage = _open_stage(stage_path, user_edits)
    if not stage:
        raise ValueError(f"cannot open {stage_path}")
    return validate(stage, AssetDB(manifest_path), substeps=substeps, tol=tol, margin=margin,
                    allow=allow, sample_times=times, check_levels=False,
                    allow_actor_limb_contacts=allow_actor_limb_contacts)


def validate_parallel(stage, db, stage_path, manifest_path, user_edits=None, t0=None, t1=None,
                      substeps=2, tol=0.01, margin=0.0, allow=None, workers=1,
                      allow_actor_limb_contacts=True):
    """Validate disjoint time samples in separate USD processes, then merge exact checks."""
    invalid = _invalid_contact_windows(allow, allow_actor_limb_contacts)
    if invalid:
        return invalid
    start = stage.GetStartTimeCode() if t0 is None else float(t0)
    end = max(start, stage.GetEndTimeCode() if t1 is None else float(t1))
    times = np.arange(start, end + 1e-9, 1.0 / max(1, substeps))
    worker_count = min(max(1, workers), len(times))
    # One second of samples per process amortizes USD stage loading.
    if worker_count == 1 or len(times) < worker_count * max(1, int((stage.GetTimeCodesPerSecond() or 24) * substeps)):
        return validate(stage, db, t0, t1, substeps, tol, margin, allow,
                        allow_actor_limb_contacts=allow_actor_limb_contacts)

    chunks = [part.tolist() for part in np.array_split(times, worker_count) if len(part)]
    tasks = [(stage_path, manifest_path, user_edits, part, substeps, tol, margin, allow, allow_actor_limb_contacts)
             for part in chunks]
    with ProcessPoolExecutor(max_workers=len(tasks)) as pool:
        reports = list(pool.map(_validate_chunk, tasks))

    merged = reports[0].copy()
    merged["time_steps"] = sum(report["time_steps"] for report in reports)
    merged["unprotected_assets"] = sorted(set().union(*(report["unprotected_assets"] for report in reports)))
    merged["unprotected_actors"] = sorted(set().union(*(report["unprotected_actors"] for report in reports)))
    violations = {}
    for report in reports:
        for item in report["violations"]:
            key = (item["actor"], item["instance"], item["part"])
            if key not in violations:
                violations[key] = item.copy()
                continue
            current = violations[key]
            current["first_s"] = min(current["first_s"], item["first_s"])
            current["last_s"] = max(current["last_s"], item["last_s"])
            current["frames"] += item["frames"]
            if item["worst_depth_m"] > current["worst_depth_m"]:
                for field in ("worst_depth_m", "worst_s", "body_part", "shape", "push_out_m"):
                    current[field] = item[field]
    merged["violations"] = sorted(violations.values(), key=lambda r: -r["worst_depth_m"])
    for item in merged["violations"]:
        _finalize_violation(item)
    actors = [obj for obj in scene_objects(stage, db) if obj.is_actor and obj.name in merged["actors"]]
    merged["level_violations"] = _level_violations(stage, db, actors, times, allow=allow)
    merged["ok"] = not (merged["violations"] or merged["level_violations"] or
                        merged["unprotected_assets"] or merged["unprotected_actors"])
    return merged


def check_level_integrity(stage, db, actors, times, stairs=None, allow=None):
    """Require a stair portal or a scheduled airborne interval for level changes."""
    violations = []
    tcps = stage.GetTimeCodesPerSecond() or 24.0
    for a in actors:
        positions = []
        for t in times:
            tc = Usd.TimeCode(float(t))
            pos = UsdGeom.XformCache(tc).GetLocalToWorldTransform(a.prim).ExtractTranslation()
            positions.append((float(t) / tcps, np.array([pos[0], pos[1], pos[2]], float)))

        y_vals = [p[1][1] for p in positions]
        if max(y_vals) - min(y_vals) > 1.8:
            airborne = [rule for rule in (allow or [])
                        if rule.get("vertical_transition") and rule.get("actor") == a.name]
            uncovered_change = sum(
                abs(curr[1][1] - prev[1][1])
                for prev, curr in zip(positions, positions[1:])
                if not any(contains_time(rule, prev[0]) and contains_time(rule, curr[0])
                           for rule in airborne))
            if uncovered_change <= 1.8:
                continue
            passed_stair = False
            for sec, pos in positions:
                for s in (stairs or []):
                    cline = np.array(s.get("centerline_world", []), float)
                    w = float(s.get("width_m", 1.2))
                    if len(cline) >= 2:
                        for i in range(len(cline) - 1):
                            p0, p1 = cline[i], cline[i + 1]
                            v = p1 - p0
                            L2 = v[0]**2 + v[2]**2
                            if L2 > 1e-4:
                                u = ((pos[0] - p0[0]) * v[0] + (pos[2] - p0[2]) * v[2]) / L2
                                if -0.1 <= u <= 1.1:
                                    proj_x = p0[0] + np.clip(u, 0, 1) * v[0]
                                    proj_z = p0[2] + np.clip(u, 0, 1) * v[2]
                                    if np.hypot(pos[0] - proj_x, pos[2] - proj_z) <= (w / 2.0 + 0.35):
                                        passed_stair = True
                                        break
                    if passed_stair:
                        break
                if passed_stair:
                    break
            if not passed_stair:
                violations.append({
                    "actor": a.name,
                    "elevation_change_m": round(float(max(y_vals) - min(y_vals)), 2),
                    "reason": "Actor changed vertical level without passing through any recognized stair portal.",
                })
    return violations


def collect_stage_allow_rules(stage, db):
    """Extract contact whitelist rules declared via customData allow_contact on actor prims."""
    rules = []
    for obj in scene_objects(stage, db):
        if not obj.is_actor:
            continue
        raw = obj.prim.GetCustomDataByKey("allow_contact")
        if not raw:
            continue
        items = [x.strip() for x in raw.split(",") if x.strip()] if isinstance(raw, str) else list(raw)
        for item in items:
            s = str(item).strip()
            if not s:
                continue
            if ":" in s:
                asset_ref, part_ref = s.split(":", 1)
                rules.append({"actor": obj.name, "asset": asset_ref.strip(), "shape": part_ref.strip()})
            else:
                rules.append({"actor": obj.name, "asset": s})
    return rules


def _nudge_to_free_cell(stage, db, pt_xz, times, skip, radius=0.45, cell=0.1):
    """If pt_xz is inside an obstacle keep-out zone, move it to the nearest unblocked grid cell."""
    import plan_path as pp

    obs = pp.obstacles(stage, db, times, skip=skip)
    if not obs:
        return pt_xz
    pts_list = [pt_xz]
    for o in obs:
        if o[0] == "poly":
            pts_list.extend(o[1])
        else:
            pts_list.append(o[1])
    pts = np.asarray(pts_list, float)
    margin = 6.0
    bounds = (pts[:, 0].min() - margin, pts[:, 1].min() - margin, pts[:, 0].max() + margin, pts[:, 1].max() + margin)
    blocked, (x0, z0, c) = pp.rasterize(obs, bounds, cell, radius)
    ix, iz = int((pt_xz[0] - x0) / c), int((pt_xz[1] - z0) / c)
    nx, nz = blocked.shape
    if 0 <= ix < nx and 0 <= iz < nz and not blocked[ix, iz]:
        return pt_xz
    best = None
    for r in range(1, 50):
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                if abs(dx) != r and abs(dz) != r:
                    continue
                jx, jz = ix + dx, iz + dz
                if 0 <= jx < nx and 0 <= jz < nz and not blocked[jx, jz]:
                    wx, wz = x0 + (jx + 0.5) * c, z0 + (jz + 0.5) * c
                    dist = (wx - pt_xz[0]) ** 2 + (wz - pt_xz[1]) ** 2
                    if best is None or dist < best[0]:
                        best = (dist, np.array([wx, wz], float))
        if best is not None:
            return best[1]
    return pt_xz


def apply_stage_intents(stage, db):
    """Apply customData go_to intents on actors using plan_path."""
    import plan_path as pp

    tcps = stage.GetTimeCodesPerSecond() or 24.0
    t0 = stage.GetStartTimeCode()
    t1 = max(t0, stage.GetEndTimeCode())
    times = np.arange(t0, t1 + 1e-9, max(1.0, tcps / 2))
    modified = False
    for obj in scene_objects(stage, db):
        if not obj.is_actor:
            continue
        go_to = obj.prim.GetCustomDataByKey("go_to")
        if not go_to:
            continue
        speed = float(obj.prim.GetCustomDataByKey("speed_mps") or 1.4)
        start = pp.actor_xz(stage, obj.prim, t0)
        end_facing = None
        if isinstance(go_to, str) and ":" in go_to:
            goal, end_facing = pp.anchor_world(stage, db, go_to.strip(), t0)
        elif isinstance(go_to, (list, tuple)) and len(go_to) >= 2:
            goal = np.array([float(go_to[0]), float(go_to[1])], float)
        else:
            parts = [float(x) for x in str(go_to).replace(",", " ").split() if x]
            if len(parts) < 2:
                continue
            goal = np.array(parts[:2], float)
        start = _nudge_to_free_cell(stage, db, start, times, skip=(obj.name,))
        goal = _nudge_to_free_cell(stage, db, goal, times, skip=(obj.name,))
        stage.SetEditTarget(stage.GetRootLayer())
        wp = pp.plan(stage, db, start, goal, times, skip=(obj.name,))
        pp.author_root_motion(stage, str(obj.path), wp, speed, t0, tcps, end_facing_deg=end_facing)
        modified = True
    if modified:
        stage.GetRootLayer().Save()
    return modified


def repair_violations(stage, db, rep):
    """Automatically re-plan colliding actor routes or apply beat-local push-out offsets."""
    import plan_path as pp
    from pxr import Gf

    if rep.get("ok"):
        return False
    tcps = stage.GetTimeCodesPerSecond() or 24.0
    t0 = stage.GetStartTimeCode()
    t1 = max(t0, stage.GetEndTimeCode())
    times = np.arange(t0, t1 + 1e-9, max(1.0, tcps / 2))
    objs_by_name = {o.name: o for o in scene_objects(stage, db) if o.is_actor}
    by_actor = {}
    for v in rep.get("violations", []):
        by_actor.setdefault(v["actor"], []).append(v)

    repaired = False
    stage.SetEditTarget(stage.GetRootLayer())
    for actor_name, vlist in by_actor.items():
        actor = objs_by_name.get(actor_name)
        if actor is None:
            continue
        xform = UsdGeom.Xformable(actor.prim)
        ops = {o.GetOpType(): o for o in xform.GetOrderedXformOps()}
        tr = ops.get(UsdGeom.XformOp.TypeTranslate) or xform.AddTranslateOp()
        ts = tr.GetTimeSamples()

        start_xz = pp.actor_xz(stage, actor.prim, t0)
        end_xz = pp.actor_xz(stage, actor.prim, t1)
        travel = float(np.linalg.norm(end_xz - start_xz))
        dur_s = max((t1 - t0) / tcps, 1.0 / tcps)

        # Only replace full root motion with a single A* path if the actor is doing a single segment move (<= 3 samples)
        if travel > 0.15 and len(ts) <= 3:
            s_free = _nudge_to_free_cell(stage, db, start_xz, times, skip=(actor.name,))
            g_free = _nudge_to_free_cell(stage, db, end_xz, times, skip=(actor.name,))
            try:
                wp = pp.plan(stage, db, s_free, g_free, times, skip=(actor.name,))
                path_len = float(sum(np.linalg.norm(np.diff(np.array(wp, float), axis=0), axis=1)))
                speed = max(0.8, min(4.5, path_len / dur_s))
                pp.author_root_motion(stage, str(actor.path), wp, speed, t0, tcps)
                repaired = True
                continue
            except ValueError:
                pass

        # Beat-local push-out over the colliding time window [first_s, last_s] (preserving non-colliding beats)
        worst = vlist[0]
        push = np.array(worst.get("push_out_m", [0.0, 0.0, 0.0]), float)
        push[1] = 0.0
        norm = float(np.linalg.norm(push))
        if norm > 1e-6:
            push = push + (push / norm) * 0.08

        w_first_s = min(float(v.get("first_s", 0.0)) for v in vlist)
        w_last_s = max(float(v.get("last_s", dur_s)) for v in vlist)
        fade_s = 0.45

        if ts:
            for t_sample in ts:
                sec_sample = float(t_sample) / tcps
                if w_first_s - fade_s <= sec_sample <= w_last_s + fade_s:
                    if sec_sample < w_first_s:
                        w = max(0.0, 1.0 - (w_first_s - sec_sample) / fade_s)
                    elif sec_sample > w_last_s:
                        w = max(0.0, 1.0 - (sec_sample - w_last_s) / fade_s)
                    else:
                        w = 1.0
                    # Smoothstep weight
                    w = w * w * (3.0 - 2.0 * w)
                    cur = tr.Get(t_sample)
                    tr.Set(Gf.Vec3d(float(cur[0] + push[0] * w), float(cur[1]), float(cur[2] + push[2] * w)), t_sample)
        else:
            cur = tr.Get() or Gf.Vec3d(float(start_xz[0]), 0.0, float(start_xz[1]))
            tr.Set(Gf.Vec3d(float(cur[0] + push[0]), float(cur[1]), float(cur[2] + push[2])))
        repaired = True

    if repaired:
        stage.GetRootLayer().Save()
    return repaired


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", help="USD stage path")
    ap.add_argument("--stage", dest="stage_opt", help="USD stage path (optional flag alias)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--user-edits", help="manual scene edits layer to compose over the stage")
    ap.add_argument("--allow")
    ap.add_argument("--json", help="write JSON report to file path (or '-' for stdout)")
    ap.add_argument("--substeps", type=int, default=2)
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                    help="parallel USD validation processes (1 disables parallelism)")
    ap.add_argument("--tol", type=float, default=0.01, help="penetration allowed before it counts (m)")
    ap.add_argument("--margin", type=float, default=0.0, help="extra clearance to demand around every body part (m)")
    ap.add_argument('--strict-actor-limbs', action='store_false', dest='allow_actor_limb_contacts',
                    help='also reject arm/leg contacts between characters (default: protect core bodies only)')
    ap.add_argument("--t0", type=float)
    ap.add_argument("--t1", type=float)
    ap.add_argument("--apply-intents", action="store_true", help="route any actor with customData go_to before checking")
    ap.add_argument("--repair", action="store_true", help="automatically re-plan or push out colliding actors")
    a = ap.parse_args(argv)

    stage_path = a.stage_opt or a.stage
    if not stage_path:
        ap.error("stage path is required")
    stage = _open_stage(stage_path, a.user_edits)
    if not stage:
        print("cannot open", stage_path, file=sys.stderr)
        return 2
    db = AssetDB(a.manifest)
    if a.apply_intents:
        try:
            apply_stage_intents(stage, db)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: intent routing failed: {e}", file=sys.stderr)

    rules = []
    if a.allow and os.path.isfile(a.allow):
        rules.extend(json.load(open(a.allow)))
    else:
        stage_dir = os.path.dirname(os.path.abspath(stage_path))
        for cand in ("allow_collisions.json", "allow.json"):
            default_allow = os.path.join(stage_dir, cand)
            if os.path.isfile(default_allow):
                try:
                    rules.extend(json.load(open(default_allow)))
                    break
                except Exception:  # noqa: BLE001
                    pass
    # Do not accept unconstrained customData allow_contact on stage prims;
    # allow rules are only accepted from compiler-generated files (allow_collisions.json / --allow).

    if a.workers < 1:
        ap.error("--workers must be at least 1")
    def run_validation():
        return validate_parallel(stage, db, stage_path, a.manifest, a.user_edits,
                                 a.t0, a.t1, a.substeps, a.tol, a.margin, rules or None, a.workers,
                                 allow_actor_limb_contacts=a.allow_actor_limb_contacts)

    rep = run_validation()
    if not rep["ok"] and a.repair:
        if repair_violations(stage, db, rep):
            rep = run_validation()
            rep["repaired"] = True

    if a.json == "-":
        print(json.dumps(rep))
        return 0 if rep["ok"] else 1
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=2)
            f.write("\n")
    if rep.get("unprotected_assets"):
        print("FAIL: assets without collision shapes:", ", ".join(rep["unprotected_assets"]), file=sys.stderr)
    if rep.get("unprotected_actors"):
        print("FAIL: actors without usable skeletons:", ", ".join(rep["unprotected_actors"]), file=sys.stderr)
    if rep["ok"]:
        tag = "PASS (auto-repaired)" if rep.get("repaired") else "PASS"
        print(f"{tag}  {len(rep['actors'])} actor(s) vs {len(rep['assets'])} asset(s), {rep['time_steps']} time steps")
        return 0
    print(
        f"FAIL  {len(rep['violations'])} penetration(s), "
        f"{len(rep['unprotected_assets'])} solid asset(s) without colliders, "
        f"{len(rep['unprotected_actors'])} actor(s) without usable skeletons:"
    )
    for v in rep["violations"]:
        print("  -", v["advice"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

