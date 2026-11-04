"""A placed actor's manual layer must not freeze its compiled route."""

import sys
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from compiler import ShotCompiler


def test_generated_motion_releases_static_placement_and_duplicate_reference(tmp_path):
    asset = Usd.Stage.CreateNew(str(tmp_path / "vehicle.usdc"))
    source = UsdGeom.Xform.Define(asset, "/Vehicle")
    asset.SetDefaultPrim(source.GetPrim())
    UsdGeom.Cube.Define(asset, "/Vehicle/Body")
    asset.GetRootLayer().Save()

    wrapper = Usd.Stage.CreateNew(str(tmp_path / "vehicle.usda"))
    world = UsdGeom.Xform.Define(wrapper, "/World")
    wrapper.SetDefaultPrim(world.GetPrim())
    UsdGeom.Xform.Define(wrapper, "/World/Vehicle").GetPrim().GetReferences().AddReference("./vehicle.usdc")
    wrapper.GetRootLayer().Save()

    shot_path = tmp_path / "shot.usda"
    shot = Usd.Stage.CreateNew(str(shot_path))
    UsdGeom.Xform.Define(shot, "/World")
    generated = UsdGeom.Xform.Define(shot, "/World/Actor")
    generated.GetPrim().GetReferences().SetReferences([Sdf.Reference("./vehicle.usdc", "/Vehicle")])
    move = generated.AddTranslateOp()
    move.Set(Gf.Vec3d(0, 0, 0), 0)
    move.Set(Gf.Vec3d(10, 0, 0), 24)
    shot.GetRootLayer().Save()

    manual = Usd.Stage.CreateNew(str(tmp_path / "user_edits.usda"))
    UsdGeom.Xform.Define(manual, "/World")
    placed = UsdGeom.Xform.Define(manual, "/World/Actor")
    placed.GetPrim().GetReferences().AddReference("./vehicle.usda")
    placed.AddTranslateOp().Set(Gf.Vec3d(2, 0, 0))
    placed.AddRotateXYZOp().Set(Gf.Vec3f(0, 0, 0))
    unrelated = UsdGeom.Xform.Define(manual, "/World/Prop")
    unrelated.AddTranslateOp().Set(Gf.Vec3d(3, 0, 0))
    manual.GetRootLayer().Save()

    ShotCompiler._release_animated_actor_overrides(shot_path)
    root = Sdf.Layer.FindOrOpen(str(shot_path))
    edits = Sdf.Layer.FindOrOpen(str(tmp_path / "user_edits.usda"))
    composed = Usd.Stage.Open(root, edits)
    actor = composed.GetPrimAtPath("/World/Actor")
    assert len([p for p in Usd.PrimRange(actor) if p.IsA(UsdGeom.Cube)]) == 1
    assert not composed.GetPrimAtPath("/World/Actor/Vehicle").IsValid()
    assert UsdGeom.Xformable(actor).GetLocalTransformation(24).ExtractTranslation()[0] == 10
    assert UsdGeom.Xformable(composed.GetPrimAtPath("/World/Prop")) \
        .GetLocalTransformation().ExtractTranslation()[0] == 3
