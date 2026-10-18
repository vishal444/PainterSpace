#!/usr/bin/env python3
"""Synthetic regression test for the multi-action USD baker.

Run with ``.venv/bin/python tools/test_bake_animation.py`` after installing
``tools/requirements.txt``.  It needs no production character assets.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from pxr import Gf, Usd, UsdSkel, Vt

sys.path.insert(0, str(Path(__file__).parent))
from bake_animation import bake  # noqa: E402


def qz(degrees: float) -> Gf.Quatf:
    return Gf.Quatf(Gf.Rotation(Gf.Vec3d(0, 0, 1), degrees).GetQuat())


def make_character(path: Path) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    skeleton = UsdSkel.Skeleton.Define(stage, "/Rig/Skeleton")
    skeleton.CreateJointsAttr(Vt.TokenArray(["Hips", "Spine"]))
    skeleton.CreateRestTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1), Gf.Matrix4d(1)]))
    stage.GetRootLayer().Save()


def make_clip(path: Path, joints: list[str], start_deg: float, end_deg: float, root_x: float) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetTimeCodesPerSecond(24)
    anim = UsdSkel.Animation.Define(stage, "/Clip")
    anim.CreateJointsAttr(Vt.TokenArray(joints))
    for frame, angle in ((0, start_deg), (24, end_deg)):
        anim.CreateRotationsAttr().Set(Vt.QuatfArray([qz(angle), qz(angle)]), frame)
        anim.CreateTranslationsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(root_x * frame / 24, 0, 0), Gf.Vec3f(0)]), frame)
        anim.CreateScalesAttr().Set(Vt.Vec3fArray([Gf.Vec3f(1), Gf.Vec3f(1)]), frame)
    stage.GetRootLayer().Save()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_character(root / "character.usda")
        # Namespace matching is deliberate: character Hips maps to mixamorig:Hips.
        make_clip(root / "run.usda", ["mixamorig:Hips", "mixamorig:Spine"], 0, 60, 2)
        make_clip(root / "walk.usda", ["Hips", "Spine"], 60, 90, 1)
        (root / "manifest.json").write_text(json.dumps({"clips": {
            "run": {"file": "run.usda", "loop": True, "strip_root_motion": True, "strip_root_axes": [0]},
            "walk": {"file": "walk.usda", "loop": True, "root_offset_m": [0, -0.45, 0]},
        }}))
        (root / "actions.json").write_text(json.dumps({"fps": 24, "actions": [
            {"clip": "run", "start_s": 0, "end_s": 1},
            {"clip": "walk", "start_s": 1, "end_s": 2, "blend_s": 0.25},
        ]}))
        end_frame, fps = bake(root / "character.usda", root / "manifest.json", root / "actions.json",
                              root / "baked.usda", fps=None, duration=None)
        assert (end_frame, fps) == (48, 24)
        stage = Usd.Stage.Open(str(root / "baked.usda"))
        anim = UsdSkel.Animation(stage.GetDefaultPrim())
        assert [str(x) for x in anim.GetJointsAttr().Get()] == ["Hips", "Spine"]
        assert anim.GetRotationsAttr().GetNumTimeSamples() == 49
        # Root translation is stripped during run and the sit/walk-style offset is applied.
        assert abs(anim.GetTranslationsAttr().Get(0)[0][0]) < 1e-5
        assert abs(anim.GetTranslationsAttr().Get(48)[0][1] + 0.45) < 1e-5
        # At the join, a crossfade produces a pose between the two clip poses.
        joined = anim.GetRotationsAttr().Get(25)[0].GetImaginary()[2]
        assert 0.0 < joined < 1.0
    print("test_bake_animation: PASS")


if __name__ == "__main__":
    main()
