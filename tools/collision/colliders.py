"""
colliders.py - shared helpers for the PainterSpace collision toolkit.

  AssetDB              reads manifest.json and finds which prims in a stage are asset instances
  scene_objects()      every object on a stage that can block a character: manifest assets,
                       inline objects the LLM built from primitives, and the actors themselves
  world_groups()       the collision boxes of one object in world space at a given time.
                       A moving part (door, hood...) is placed with its REAL transform at that
                       time, read from USD, so any rotate op, variant or animation is honoured.
  CharacterModel       capsules along the bones of a UsdSkel skeleton, posed at a given time
  BoxGroup.sd()        signed distance from points to boxes (negative = inside), vectorised

Conventions: meters, Y up, heading 0 = -Z (north), 90 = +X (east), rotateY = -heading.
Needs: usd-core, numpy.
"""
from __future__ import annotations

import json
import math
import os
import re

import numpy as np
from pxr import Usd, UsdGeom, UsdSkel

STEP_HEIGHT_M = 0.15        # anything whose top is lower can be stepped over (ground, curbs)
REACH_HEIGHT_M = 2.1        # nothing whose bottom is higher can touch a standing character
INLINE_MAX_FOOTPRINT_M2 = 40.0   # bigger inline gprims are ground / environment, not obstacles
HIDDEN_HELPER_NAMES = {"Camera", "Cube", "Light", "env_light"}   # Blender export leftovers
VISIBLE_PURPOSES = (UsdGeom.Tokens.default_, UsdGeom.Tokens.render)


# ----------------------------------------------------------------- small math
def m2np(m):
    """Gf.Matrix4d (row-vector convention) -> 4x4 numpy matrix (column-vector convention)."""
    return np.array(m, dtype=float).T


def np2gf_rows(M):
    """4x4 column-vector numpy matrix -> nested rows for Gf.Matrix4d (row-vector convention)."""
    return [[float(v) for v in row] for row in np.asarray(M, float).T]


def T4(v):
    M = np.eye(4)
    M[:3, 3] = v
    return M


def R4(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    R = {"X": [[1, 0, 0], [0, c, -s], [0, s, c]],
         "Y": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
         "Z": [[c, -s, 0], [s, c, 0], [0, 0, 1]]}[axis.upper()]
    M = np.eye(4)
    M[:3, :3] = R
    return M


def R4_axis(v, deg):
    """rotation about an arbitrary unit axis v (Rodrigues)."""
    v = np.asarray(v, float)
    v = v / max(np.linalg.norm(v), 1e-12)
    a = math.radians(deg)
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    M = np.eye(4)
    M[:3, :3] = np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * K @ K
    return M


def apply(M, P):
    return np.asarray(P, float) @ M[:3, :3].T + M[:3, 3]


def heading_deg(dx, dz):
    """0 = -Z (north), 90 = +X (east)."""
    return math.degrees(math.atan2(dx, -dz)) % 360.0


def mat_to_list(M):
    return [round(float(v), 6) for v in np.asarray(M, float).ravel()]


def list_to_mat(L):
    return np.array(L, float).reshape(4, 4)


def min_scale(M):
    """smallest axis scale of a transform: distances scaled by it are never overestimated."""
    return float(np.linalg.svd(M[:3, :3], compute_uv=False).min())


def l2w(xc, prim):
    return m2np(xc.GetLocalToWorldTransform(prim))


# ----------------------------------------------------------------- manifest
def load_manifest(path):
    with open(path) as f:
        return json.load(f)


class AssetDB:
    def __init__(self, manifest_path):
        self.path = os.path.abspath(manifest_path)
        self.data = load_manifest(manifest_path)
        self.entries = self.data["assets"] if isinstance(self.data, dict) else self.data
        self.by_id = {}
        self.by_file = {}
        for e in self.entries:
            self.by_id[e["id"]] = e
            for alias in e.get("aliases", []) or []:
                self.by_id.setdefault(alias, e)
            for key in ("path", "source_path"):
                if e.get(key):
                    self.by_file.setdefault(os.path.basename(e[key]), e)

    def lookup_id(self, aid):
        if not aid:
            return None
        if aid in self.by_id:
            return self.by_id[aid]
        # "character.human.poseable" -> "character.human"
        best = None
        for key, e in self.by_id.items():
            if aid.startswith(key + ".") and (best is None or len(key) > len(best[0])):
                best = (key, e)
        return best[1] if best else None

    def from_prim(self, prim):
        e = self.lookup_id(prim.GetCustomDataByKey("asset_id"))
        if e:
            return e
        for spec in prim.GetPrimStack():
            if not spec.hasReferences:
                continue
            ops = spec.referenceList
            for ref in list(ops.explicitItems) + list(ops.prependedItems) + list(ops.appendedItems) + list(ops.addedItems):
                e = self.by_file.get(os.path.basename(ref.assetPath))
                if e:
                    return e
        return None


# ----------------------------------------------------------------- scene objects
class SceneObject:
    """One thing on the stage: an actor (skinned character), a manifest asset, or an inline object."""

    def __init__(self, prim, entry, kind):
        self.prim, self.entry, self.kind = prim, entry, kind
        self.name = prim.GetName()
        self.path = prim.GetPath()

    @property
    def is_actor(self):
        return self.kind == "actor"

    @property
    def asset_id(self):
        return self.entry["id"] if self.entry else f"inline:{self.name}"

    @property
    def protected(self):
        """True when this object has colliders (manifest shapes, or inline geometry)."""
        if self.kind == "inline":
            return True
        return bool(self.entry and (self.entry.get("collision") or {}).get("shapes"))

    def __repr__(self):
        return f"<{self.kind} {self.path}>"


def _role(prim):
    return str(prim.GetCustomDataByKey("world_state_role") or "").lower()


def _has_skel(prim):
    return any(p.IsA(UsdSkel.Root) for p in Usd.PrimRange(prim))


def _is_actor_entry(entry):
    tags = [t.lower() for t in (entry or {}).get("tags", [])]
    return "character" in tags or "human" in tags


def scene_objects(stage, db):
    """All manifest instances (pruned at the first match) plus inline objects under the default prim."""
    out = []
    it = iter(Usd.PrimRange(stage.GetPseudoRoot()))
    for prim in it:
        if prim.IsPseudoRoot():
            continue
        e = db.from_prim(prim)
        if e:
            kind = "actor" if (_is_actor_entry(e) or _has_skel(prim)) else "asset"
            if _role(prim) not in ("ignore", "zone"):
                out.append(SceneObject(prim, e, kind))
            it.PruneChildren()
    claimed = {str(o.path) for o in out}
    world = stage.GetDefaultPrim() or stage.GetPrimAtPath("/World")
    if world and world.IsValid():
        for child in world.GetChildren():
            sp = str(child.GetPath())
            if sp in claimed or any(sp.startswith(c + "/") for c in claimed):
                continue
            if any(c.startswith(sp + "/") for c in claimed):
                continue                       # a grouping prim that holds asset instances
            role = _role(child)
            if role in ("ignore", "zone", "environment") or child.GetName() in ("Zones", "Paint", "Lights"):
                continue
            if child.IsA(UsdGeom.Camera) or not child.IsA(UsdGeom.Imageable):
                continue
            if _has_skel(child):
                out.append(SceneObject(child, None, "actor"))
            elif any(p.IsA(UsdGeom.Gprim) for p in Usd.PrimRange(child)):
                out.append(SceneObject(child, None, "inline"))
    return out


def is_visible(prim, tc):
    return UsdGeom.Imageable(prim).ComputeVisibility(tc) != UsdGeom.Tokens.invisible


# ----------------------------------------------------------------- box groups
class BoxGroup:
    """Boxes that move together. centers/halves are in the group's local frame, T maps local -> world."""

    def __init__(self, obj, part, T, ids, centers, halves):
        self.obj, self.part = obj, part
        self.T = np.asarray(T, float)
        self.Tinv = np.linalg.inv(self.T)
        self.ids = list(ids)
        self.c = np.asarray(centers, float).reshape(-1, 3)
        self.h = np.asarray(halves, float).reshape(-1, 3)
        self.scale = min_scale(self.T)

    def __len__(self):
        return len(self.ids)

    def local(self, P):
        return apply(self.Tinv, P)

    def candidates(self, P_local, reach):
        """indices of boxes whose local AABB comes within `reach` (world m) of the points' local AABB."""
        r = reach / max(self.scale, 1e-9)
        lo, hi = P_local.min(0) - r, P_local.max(0) + r
        ok = np.all((self.c + self.h >= lo) & (self.c - self.h <= hi), axis=1)
        return np.where(ok)[0]

    def sd(self, P_local, idx=None):
        """signed distance (world meters) of local points to boxes: shape (points, boxes)."""
        c = self.c if idx is None else self.c[idx]
        h = self.h if idx is None else self.h[idx]
        q = np.abs(P_local[:, None, :] - c[None]) - h[None]
        out = np.linalg.norm(np.maximum(q, 0), axis=2) + np.minimum(q.max(axis=2), 0)
        return out * self.scale

    def push_dir(self, p_local, i):
        """world unit direction that moves a point out of box i fastest."""
        q = p_local - self.c[i]
        pen = self.h[i] - np.abs(q)
        if np.all(pen > 0):                   # inside: leave through the nearest face
            ax = int(np.argmin(pen))
            d = np.zeros(3)
            d[ax] = 1.0 if q[ax] >= 0 else -1.0
        else:
            d = np.where(np.abs(q) > self.h[i], np.sign(q), 0.0)
        w = self.T[:3, :3] @ d
        return w / max(np.linalg.norm(w), 1e-12)

    def corners_world(self, idx=None):
        c = self.c if idx is None else self.c[idx]
        h = self.h if idx is None else self.h[idx]
        signs = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float)
        P = (c[:, None, :] + signs[None] * h[:, None, :]).reshape(-1, 3)
        return apply(self.T, P).reshape(len(c), 8, 3)


def _shape_arrays(shapes):
    ids, cs, hs = [], [], []
    for sh in shapes:
        if sh.get("type", "box") == "box":
            ids.append(sh["id"])
            cs.append(sh["center"])
            hs.append(np.asarray(sh["size"], float) / 2)
        elif sh.get("type") == "cylinder":            # stored as its bounding box: conservative
            r = float(sh["radius"])
            c = sh["center"]
            y0, y1 = float(sh["y0"]), float(sh["y1"])
            ids.append(sh["id"])
            cs.append([c[0], (y0 + y1) / 2, c[2]])
            hs.append([r, (y1 - y0) / 2, r])
    return ids, cs, hs


def part_prim(stage, obj, part):
    rel = part.get("prim", "")
    if not rel:
        return None
    p = stage.GetPrimAtPath(obj.path.AppendPath(rel))
    return p if p and p.IsValid() else None


def _part_transform(stage, obj, part, tc, xc, M):
    """local(asset frame at rest) -> world, for a moving part at time tc."""
    pp = part_prim(stage, obj, part)
    if pp is None:
        return M, False
    if part.get("collider_frame"):
        return l2w(xc, pp) @ list_to_mat(part["collider_frame"]), is_visible(pp, tc)
    # legacy manual entry: hinge + axis + rotate op value
    ang = 0.0
    for op in UsdGeom.Xformable(pp).GetOrderedXformOps():
        name = op.GetOpName()
        if name.startswith("xformOp:rotate" + part.get("axis", "Y").upper()):
            v = op.Get(tc)
            ang = float(v) if v is not None else 0.0
            break
    h = np.array(part.get("hinge", [0, 0, 0]), float)
    return M @ T4(h) @ R4(part.get("axis", "Y"), ang) @ T4(-h), is_visible(pp, tc)


def world_groups(stage, obj, tc, xc, bbc=None):
    """collision boxes of one object in world space at time tc, as a list of BoxGroup."""
    if not is_visible(obj.prim, tc):
        return []
    if obj.kind == "inline":
        return inline_groups(obj, tc, xc, bbc)
    col = (obj.entry or {}).get("collision") or {}
    shapes = col.get("shapes") or []
    if not shapes:
        return []
    M = l2w(xc, obj.prim)
    parts = (obj.entry or {}).get("parts") or {}
    by_part = {}
    for sh in shapes:
        by_part.setdefault(sh.get("part"), []).append(sh)
    out = []
    for pname, lst in by_part.items():
        T, vis = M, True
        if pname and pname in parts:
            T, vis = _part_transform(stage, obj, parts[pname], tc, xc, M)
        if not vis:
            continue
        ids, cs, hs = _shape_arrays(lst)
        if ids:
            out.append(BoxGroup(obj, pname, T, ids, cs, hs))
    return out


def inline_groups(obj, tc, xc, bbc=None):
    """one oriented box per visible gprim of an inline object (the LLM built it from primitives)."""
    bbc = bbc or UsdGeom.BBoxCache(tc, list(VISIBLE_PURPOSES))
    out = []
    for p in Usd.PrimRange(obj.prim):
        if not p.IsA(UsdGeom.Gprim) or not is_visible(p, tc):
            continue
        if UsdGeom.Imageable(p).ComputePurpose() not in VISIBLE_PURPOSES:
            continue
        box = bbc.ComputeWorldBound(p)
        rng = box.GetRange()
        if rng.IsEmpty():
            continue
        T = m2np(box.GetMatrix())
        lo, hi = np.array(rng.GetMin()), np.array(rng.GetMax())
        corners = apply(T, np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]))
        wlo, whi = corners.min(0), corners.max(0)
        if whi[1] < STEP_HEIGHT_M or wlo[1] > REACH_HEIGHT_M:
            continue
        if (whi[0] - wlo[0]) * (whi[2] - wlo[2]) > INLINE_MAX_FOOTPRINT_M2:
            continue                                   # ground plane or backdrop
        half = np.maximum((hi - lo) / 2, 0.01)
        out.append(BoxGroup(obj, None, T, [p.GetName()], [(lo + hi) / 2], [half]))
    return out


# ----------------------------------------------------------------- character body model
_FINGER = re.compile(r"(thumb|index|middle|ring|pinky)", re.I)


def joint_leaf(path):
    return re.sub(r"^mixamorig\d*[:_]", "", str(path).split("/")[-1])


def is_limb_segment(label):
    """Classify arm/leg bones; unknown labels stay protected as main body.

    Either endpoint makes a segment a limb, including hip-to-thigh and
    spine-to-shoulder connections. Head, neck, spine and pelvis stay protected.
    """
    endings = ('shoulder', 'clavicle', 'arm', 'forearm', 'hand', 'wrist', 'elbow',
               'upleg', 'upperleg', 'lowerleg', 'leg', 'thigh', 'calf', 'knee',
               'ankle', 'foot', 'toe', 'toebase', 'thumb', 'index', 'middle', 'ring', 'pinky')
    for endpoint in str(label).split('>'):
        name = re.sub('[^a-z]', '', joint_leaf(endpoint).lower())
        if name.endswith('end'):
            name = name[:-3]
        if (name.endswith(endings) or
                (name.endswith(('l', 'r')) and name[:-1].endswith(endings))):
            return True
    return False


def segment_radius(parent_leaf, child_leaf):
    p, c = parent_leaf.lower(), child_leaf.lower()
    if _FINGER.search(p) or _FINGER.search(c):
        return None
    if c.endswith("_end") and p.startswith("head"):
        return .10
    if "shoulder" in c:
        return .05
    if p.startswith("hips") and "upleg" in c:
        return .10
    if p.startswith("head"):
        return .10
    if p.startswith("neck"):
        return .055
    if p.startswith("hips"):
        return .13
    if "spine" in p:
        return .15
    if "shoulder" in p:
        return .05
    if "forearm" in p:
        return .045
    if "upleg" in p:
        return .09
    if p.endswith("leg"):
        return .065
    if "foot" in p:
        return .055
    if "toe" in p:
        return .04
    if "hand" in p:
        return None
    if p.endswith("arm"):
        return .055
    return .04


TORSO_KEYS = ("hips", "spine", "neck", "head")


class CharacterModel:
    """Capsules along the bones of a skinned character, posed at any time (baked animation included)."""

    def __init__(self, stage, obj):
        root = next((p for p in Usd.PrimRange(obj.prim) if p.IsA(UsdSkel.Root)), None)
        if root is None:
            raise ValueError(f"{obj.path} has no SkelRoot")
        self.cache = UsdSkel.Cache()
        self.cache.Populate(UsdSkel.Root(root), Usd.PrimDefaultPredicate)
        skels = [p for p in Usd.PrimRange(root) if p.IsA(UsdSkel.Skeleton)]
        if not skels:
            raise ValueError(f"{obj.path} has no Skeleton")
        self.skel = skels[0]
        self.q = self.cache.GetSkelQuery(UsdSkel.Skeleton(self.skel))
        self.joints = [str(j) for j in self.q.GetJointOrder()]
        topo = self.q.GetTopology()
        self.segs, self.spheres = [], []
        for i, j in enumerate(self.joints):
            pi = topo.GetParent(i)
            leaf = joint_leaf(j)
            if "hand" in leaf.lower() and not _FINGER.search(leaf):
                self.spheres.append((i, .06, leaf))
            if pi < 0:
                continue
            r = segment_radius(joint_leaf(self.joints[pi]), leaf)
            if r:
                self.segs.append((pi, i, r, f"{joint_leaf(self.joints[pi])}>{leaf}"))
        self.torso = [s for s in self.segs if s[3].split(">")[0].lower().startswith(TORSO_KEYS)]

    def joint_world(self, tc, xc):
        xf = self.q.ComputeJointSkelTransforms(tc)
        P = np.array([[*m.ExtractTranslation()] for m in xf], float)
        return apply(l2w(xc, self.skel), P)

    def samples(self, tc, xc, step=0.06, joints=None):
        """points along all capsules: (points, radii, labels)."""
        J = self.joint_world(tc, xc) if joints is None else joints
        pts, rad, lab = [], [], []
        for pi, ci, r, label in self.segs:
            a, b = J[pi], J[ci]
            n = max(2, int(math.ceil(np.linalg.norm(b - a) / step)) + 1)
            fractions = np.linspace(0.0, 1.0, n)[:, None]
            pts.append(a[None, :] + (b - a)[None, :] * fractions)
            rad.append(np.full(n, r, dtype=float))
            lab.extend([label] * n)
        for ji, r, label in self.spheres:
            pts.append(J[ji:ji + 1])
            rad.append(np.array([r], dtype=float))
            lab.append(label)
        if not pts:
            return np.empty((0, 3), dtype=float), np.empty(0, dtype=float), lab
        return np.concatenate(pts), np.concatenate(rad), lab

    def torso_capsules(self, tc, xc):
        J = self.joint_world(tc, xc)
        return [(J[a], J[b], r, label) for a, b, r, label in self.torso]

    def body_capsules(self, tc, xc, joints=None):
        J = self.joint_world(tc, xc) if joints is None else joints
        return [(J[a], J[b], r, label) for a, b, r, label in self.segs]


def segment_distance(p1, q1, p2, q2):
    """closest distance between segments p1q1 and p2q2."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = d1 @ d1, d2 @ d2, d2 @ r
    if a <= 1e-12 and e <= 1e-12:
        return float(np.linalg.norm(r))
    if a <= 1e-12:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = d1 @ r
        if e <= 1e-12:
            t, s = 0.0, min(max(-c / a, 0.0), 1.0)
        else:
            b = d1 @ d2
            den = a * e - b * b
            s = min(max((b * f - c * e) / den, 0.0), 1.0) if den > 1e-12 else 0.0
            t = (b * s + f) / e
            if t < 0:
                t, s = 0.0, min(max(-c / a, 0.0), 1.0)
            elif t > 1:
                t, s = 1.0, min(max((b - c) / a, 0.0), 1.0)
    return float(np.linalg.norm((p1 + d1 * s) - (p2 + d2 * t)))

