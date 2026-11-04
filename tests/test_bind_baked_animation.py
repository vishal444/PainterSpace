"""A manual placement must not pin a generated animated character."""

import sys
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bind_baked_animation import release_user_transform_overrides


def test_generated_route_overrides_static_user_placement(tmp_path):
    shot = tmp_path / "shot.usda"
    edits = tmp_path / "user_edits.usda"
    shot.write_text('''#usda 1.0
def Xform "World"
{
    def Xform "Hero"
    {
        double3 xformOp:translate.timeSamples = { 0: (0, 0, 0), 24: (0, 0, -5) }
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
''')
    edits.write_text('''#usda 1.0
over "World"
{
    over "Hero"
    {
        double3 xformOp:translate = (0, 0, 2)
        float3 xformOp:rotateXYZ = (0, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]
        custom bool previewFlag = true
    }
}
''')
    assert release_user_transform_overrides(shot, "/World/Hero")
    stage = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(shot)), Sdf.Layer.FindOrOpen(str(edits)))
    hero = stage.GetPrimAtPath("/World/Hero")
    position = UsdGeom.Xformable(hero).ComputeLocalToWorldTransform(Usd.TimeCode(24)).ExtractTranslation()
    assert tuple(position) == (0.0, 0.0, -5.0)
    assert hero.GetAttribute("previewFlag").Get() is True
    assert not release_user_transform_overrides(shot, "/World/Hero")
