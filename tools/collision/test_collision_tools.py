#!/usr/bin/env python3
"""
End-to-end test of the collision toolkit.  Run:  .venv/bin/python3 tools/collision/test_collision_tools.py
Uses the real repository assets in assets/usdc_assets/ and tools/bake_animation.py.

Scenes (a character walks 'walk' with its root motion locked, so travel comes from the path):
  A  straight line THROUGH the car                      -> must FAIL (body)
  B  planner route from the same start to the same goal -> must PASS
  C  walk along the car's left side, doors CLOSED       -> must PASS
  D  same walk, front-left door OPEN                    -> must FAIL (door_fl)
  E  planner route with the door open                   -> must PASS
  F  scene A again with that contact explicitly allowed -> PASS (whitelist works)
  G  tree: walking under the canopy passes, into the trunk fails, into the low branch fails
  H  a Z-up, centimeter, +X-facing asset is normalized to Y up, meters, facing -Z, pivot at base center
  I  plan route to Car:door_fl_stand anchor (with door open/closed) -> must PASS
  J  character-vs-character collision detection         -> walking through another actor FAILS
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS_DIR)

from pathlib import Path
import plan_path as pp
from bind_baked_animation import bind
from colliders import AssetDB
from validate_collisions import validate

W = tempfile.mkdtemp(prefix="coltest_")
os.makedirs(f"{W}/assets/cars", exist_ok=True)
os.makedirs(f"{W}/assets/humans", exist_ok=True)
py = sys.executable


def run(*a):
    r = subprocess.run([py, *a], capture_output=True, text=True)
    if r.returncode:
        print(r.stdout, r.stderr)
        raise SystemExit("command failed: " + " ".join(a))
    return r.stdout


# ---------------------------------------------------------------- build helper meshes
def box_mesh(stage, path, lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    V = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    F = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]]
    m = UsdGeom.Mesh.Define(stage, path)
    m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*v) for v in V]))
    m.CreateFaceVertexCountsAttr([4] * 6)
    m.CreateFaceVertexIndicesAttr([i for f in F for i in f])


def prism_mesh(stage, path, r, y0, y1, n=12):
    V = [(r * math.cos(2 * math.pi * i / n), y, r * math.sin(2 * math.pi * i / n)) for y in (y0, y1) for i in range(n)]
    F = [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)] + [
        list(range(n))[::-1],
        [n + i for i in range(n)],
    ]
    m = UsdGeom.Mesh.Define(stage, path)
    m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*v) for v in V]))
    m.CreateFaceVertexCountsAttr([len(f) for f in F])
    m.CreateFaceVertexIndicesAttr([i for f in F for i in f])


# ---------------------------------------------------------------- assets
shutil.copy(f"{REPO_ROOT}/assets/usdc_assets/cars/detailed_car.usdc", f"{W}/assets/cars/car1.usdc")
shutil.copy(f"{REPO_ROOT}/assets/usdc_assets/humans/human.usdc", f"{W}/assets/humans/human.usdc")
shutil.copy(f"{REPO_ROOT}/assets/usdc_assets/humans/walk.usdc", f"{W}/assets/humans/walk.usdc")

# tree: trunk + one low branch + a big canopy far above head height
t = Usd.Stage.CreateNew(f"{W}/assets/tree.usdc")
UsdGeom.SetStageUpAxis(t, "Y")
UsdGeom.SetStageMetersPerUnit(t, 1)
UsdGeom.Xform.Define(t, "/Tree")
t.SetDefaultPrim(t.GetPrimAtPath("/Tree"))
prism_mesh(t, "/Tree/Trunk", 0.18, 0, 3.2)
box_mesh(t, "/Tree/LowBranch", (0.1, 1.2, -0.05), (1.8, 1.3, 0.05))
box_mesh(t, "/Tree/Canopy", (-2, 3.0, -2), (2, 6.5, 2))
t.GetRootLayer().Save()

# a Z-up, centimeter asset that faces +X, with its pivot at a corner. The nose marks the front.
z = Usd.Stage.CreateNew(f"{W}/assets/fake.usdc")
UsdGeom.SetStageUpAxis(z, "Z")
UsdGeom.SetStageMetersPerUnit(z, 0.01)
UsdGeom.Xform.Define(z, "/root")
z.SetDefaultPrim(z.GetPrimAtPath("/root"))
box_mesh(z, "/root/Body", (0, 0, 0), (450, 180, 140))
box_mesh(z, "/root/Nose", (450, 60, 30), (500, 120, 70))
z.GetRootLayer().Save()

manifest = {
    "clips": {
        "walk": {
            "file": "assets/humans/walk.usdc",
            "loop": True,
            "strip_root_motion": True,
            "strip_root_axes": [0, 2],
            "stride_speed_mps": 1.4,
        }
    },
    "assets": [
        {
            "id": "cars.car1",
            "path": "assets/cars/car1.usdc",
            "default_prim": "Car",
            "tags": ["vehicle", "car"],
            "parts": {
                "hood": {"prim": "Hood", "axis": "X"},
                "trunk": {"prim": "Trunk", "axis": "X"},
                "door_fl": {"prim": "Doors/Door_FL", "axis": "Y"},
                "door_fr": {"prim": "Doors/Door_FR", "axis": "Y"},
                "door_rl": {"prim": "Doors/Door_RL", "axis": "Y"},
                "door_rr": {"prim": "Doors/Door_RR", "axis": "Y"},
                "steering_wheel": {"prim": "Interior/SteeringWheel/Rotor", "axis": "Z"},
            },
        },
        {"id": "humans.xbot", "path": "assets/humans/human.usdc", "default_prim": "root", "tags": ["character", "human"]},
        {"id": "trees.test", "path": "assets/tree.usdc", "default_prim": "Tree", "tags": ["tree"]},
        {"id": "props.fake", "path": "assets/fake.usdc", "default_prim": "root", "tags": ["prop"]},
    ],
}
MAN = f"{W}/manifest.json"
with open(MAN, "w") as f:
    json.dump(manifest, f, indent=2)

ing = os.path.join(HERE, "ingest_asset.py")
print(run(ing, "--manifest", MAN, "--id", "cars.car1", "--root", W, "--forward=-Z", "--write"))
print(run(ing, "--manifest", MAN, "--id", "humans.xbot", "--root", W, "--write"))
print(run(ing, "--manifest", MAN, "--id", "trees.test", "--root", W, "--forward=-Z", "--write"))
print(run(ing, "--manifest", MAN, "--id", "props.fake", "--root", W, "--forward=+X", "--write"))
M = {e["id"]: e for e in json.load(open(MAN))["assets"]}

# ---------------------------------------------------------------- H: normalization
f_entry = M["props.fake"]
bmn, bmx = np.array(f_entry["bounds_min"]), np.array(f_entry["bounds_max"])
assert f_entry["path"].endswith(".norm.usda") and f_entry["normalization"]["up_axis_was"] == "Z"
assert np.allclose((bmn + bmx)[[0, 2]] / 2, 0, atol=0.02) and abs(bmn[1]) < 0.02, "pivot must be base center"
size = bmx - bmn
assert abs(size[2] - 5.0) < 0.05 and abs(size[1] - 1.4) < 0.05 and abs(size[0] - 1.8) < 0.05, size
nose = [s for s in f_entry["collision"]["shapes"] if s["size"][2] < 0.6 and s["size"][1] < 0.5]
assert nose and nose[0]["center"][2] < -2.0, "nose (the front) must end up at -Z: " + str(nose)
print("H  normalization OK: Z-up cm +X-facing asset -> Y-up m, nose at z =", nose[0]["center"][2])

# ---------------------------------------------------------------- tree colliders only cover what a body can touch
tr = M["trees.test"]
tops = max(s["center"][1] + s["size"][1] / 2 for s in tr["collision"]["shapes"])
assert tops <= 2.11 and not any(s["center"][1] - s["size"][1] / 2 > 2.2 for s in tr["collision"]["shapes"]), "canopy must not collide"
print("   tree colliders: %d boxes, highest at %.2f m (canopy ignored)" % (len(tr["collision"]["shapes"]), tops))

# ---------------------------------------------------------------- baked walking animation for the character
acts = f"{W}/acts.json"
with open(acts, "w") as f:
    json.dump({"actions": [{"clip": "walk", "start_s": 0, "end_s": 10}]}, f)
run(
    f"{TOOLS_DIR}/bake_animation.py",
    "--character",
    f"{W}/assets/humans/human.usdc",
    "--manifest",
    MAN,
    "--actions",
    acts,
    "--output",
    f"{W}/walk_baked.usda",
    "--fps",
    "24",
)


def build_scene(name, doors_open=False, tree=False, second_actor=False):
    p = f"{W}/{name}.usda"
    s = Usd.Stage.CreateNew(p)
    UsdGeom.SetStageUpAxis(s, "Y")
    UsdGeom.SetStageMetersPerUnit(s, 1)
    s.SetTimeCodesPerSecond(24)
    s.SetStartTimeCode(0)
    s.SetEndTimeCode(24 * 10)
    UsdGeom.Xform.Define(s, "/World")
    s.SetDefaultPrim(s.GetPrimAtPath("/World"))

    car = UsdGeom.Xform.Define(s, "/World/Car")
    car.GetPrim().GetReferences().AddReference("./" + M["cars.car1"]["path"])
    if doors_open:
        car.GetPrim().GetVariantSets().GetVariantSet("doors").SetVariantSelection("open")

    if tree:
        tp = UsdGeom.Xform.Define(s, "/World/Tree")
        tp.GetPrim().GetReferences().AddReference("./" + M["trees.test"]["path"])
        tp.AddTranslateOp().Set(Gf.Vec3d(7, 0, 0))

    hp = UsdGeom.Xform.Define(s, "/World/Person_01")
    hp.GetPrim().GetReferences().AddReference("./" + M["humans.xbot"]["path"])
    s.GetRootLayer().Save()
    bind(Path(p), "/World/Person_01", Path(f"{W}/walk_baked.usda"))

    if second_actor:
        s = Usd.Stage.Open(p)
        hp2 = UsdGeom.Xform.Define(s, "/World/Person_02")
        hp2.GetPrim().GetReferences().AddReference("./" + M["humans.xbot"]["path"])
        hp2.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 4.0))
        s.GetRootLayer().Save()
        bind(Path(p), "/World/Person_02", Path(f"{W}/walk_baked.usda"))
    return p


def set_path(stage_path, wp, speed=1.4, end_facing_deg=None):
    s = Usd.Stage.Open(stage_path)
    end = pp.author_root_motion(s, "/World/Person_01", wp, speed, 0, 24, end_facing_deg=end_facing_deg)
    s.SetEndTimeCode(end)
    s.GetRootLayer().Save()
    return stage_path


def check(path, **kw):
    return validate(Usd.Stage.Open(path), AssetDB(MAN), **kw)


def show(tag, r):
    head = "PASS" if r["ok"] else "FAIL"
    detail = "; ".join(
        f"{v['asset']}/{v['part']} {v['first_s']}-{v['last_s']}s {v['worst_depth_m']}m by {v['body_part']}"
        for v in r["violations"][:3]
    )
    print(f"{tag}  {head}  {detail}")


# ---------------------------------------------------------------- A: straight through the car -> FAIL
A = set_path(build_scene("sceneA"), [(-6.0, 0.0), (6.0, 0.0)])
rA = check(A)
show("A  through the car      ", rA)
assert not rA["ok"] and any(v["part"] == "body" for v in rA["violations"])
assert rA["violations"][0]["worst_depth_m"] > 0.10, "penetration should be clearly measurable"

# ---------------------------------------------------------------- B: planner, same start and goal -> PASS
sB = build_scene("sceneB")
stg = Usd.Stage.Open(sB)
db = AssetDB(MAN)
wp = pp.plan(stg, db, np.array([-6.0, 0.0]), np.array([6.0, 0.0]), np.array([0.0]), skip=("Person_01",))
set_path(sB, wp)
rB = check(sB)
show("B  planned route         ", rB)
print("   route:", [(round(x, 1), round(z, 1)) for x, z in wp])
assert rB["ok"] and len(wp) > 2

# ---------------------------------------------------------------- C / D / E: the door
C = set_path(build_scene("sceneC"), [(-1.55, -4.5), (-1.55, 4.5)])
rC = check(C)
show("C  past car, doors closed", rC)
assert rC["ok"]

D = set_path(build_scene("sceneD", doors_open=True), [(-1.55, -4.5), (-1.55, 4.5)])
rD = check(D)
show("D  past car, door OPEN   ", rD)
assert not rD["ok"] and any(v["part"].startswith("door") for v in rD["violations"])

sE = build_scene("sceneE", doors_open=True)
stg = Usd.Stage.Open(sE)
wpE = pp.plan(stg, db, np.array([-1.55, -4.5]), np.array([-1.55, 4.5]), np.array([0.0]), skip=("Person_01",))
set_path(sE, wpE)
rE = check(sE)
show("E  planned around door   ", rE)
print("   route:", [(round(x, 1), round(z, 1)) for x, z in wpE])
assert rE["ok"] and max(abs(x) for x, z in wpE) > 2.0, "route must swing wide of the open door"

# ---------------------------------------------------------------- F: whitelist
rules = [{"actor": "Person_01", "asset": "cars.car1", "t0": 0, "t1": 100}]
rF = validate(Usd.Stage.Open(A), AssetDB(MAN), allow=rules)
show("F  contact allowed       ", rF)
assert rF["ok"]
rF2 = validate(Usd.Stage.Open(A), AssetDB(MAN), allow=[{"actor": "Person_01", "asset": "cars.car1", "joints": ["LeftHand"]}])
assert not rF2["ok"], "an allowance for one hand must not excuse the whole body"


# ---------------------------------------------------------------- G: tree
def tree_test(wp):
    p = set_path(build_scene("sceneG", tree=True), wp)
    return check(p)


rG1 = tree_test([(7.0, -5.0), (7.0, 5.0)])
show("G1 into the trunk         ", rG1)
assert not rG1["ok"] and rG1["violations"][0]["asset"] == "trees.test"

rG2 = tree_test([(4.2, -5.0), (4.2, 5.0)])
show("G2 clear of the branch     ", rG2)
assert rG2["ok"]

rG3 = tree_test([(8.5, -5.0), (8.5, 5.0)])
show("G3 through the low branch  ", rG3)
assert not rG3["ok"]

# walking beneath the canopy (x between trunk and the 2 m canopy edge but outside trunk and branch) is fine
rG4 = tree_test([(5.4, -5.0), (5.4, 5.0)])
show("G4 beneath the canopy      ", rG4)
assert rG4["ok"]

# ---------------------------------------------------------------- I: anchor planning (door_fl_stand)
sI = build_scene("sceneI", doors_open=True)
stgI = Usd.Stage.Open(sI)
goalI, facingI = pp.anchor_world(stgI, db, "Car:door_fl_stand", 0.0)
wpI = pp.plan(stgI, db, np.array([3.5, 4.0]), goalI, np.array([0.0]), skip=("Person_01",))
set_path(sI, wpI, end_facing_deg=facingI)
rI = check(sI)
show("I  route to door_fl_stand ", rI)
assert rI["ok"], f"route to door_fl_stand must pass: {rI}"

# ---------------------------------------------------------------- J: actor-vs-actor collision
sJ = set_path(build_scene("sceneJ", second_actor=True), [(-3.0, 4.0), (3.0, 4.0)])
rJ = check(sJ)
show("J  actor into actor       ", rJ)
assert not rJ["ok"] and any(v["instance"] == "Person_02" for v in rJ["violations"])

print("\nALL COLLISION TESTS PASSED   (workdir:", W + ")")
