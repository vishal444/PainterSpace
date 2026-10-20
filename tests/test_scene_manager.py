import os
import shutil
import tempfile
import unittest
from pathlib import Path
from pxr import Sdf, Usd, UsdGeom

from tools.scene_manager import (
    snapshot_scene_for_existing_shots,
    save_scene_edits,
    get_scene_status,
)


class TestSceneManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="painterspace_test_scene_")
        self.scene_dir = Path(self.test_dir) / "scenes" / "scene_01"
        self.scene_dir.mkdir(parents=True, exist_ok=True)

        # Baseline scene.usda
        self.scene_usda = self.scene_dir / "scene.usda"
        self.scene_usda.write_text(
            """#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)

def "World"
{
    def Xform "Hero" (
        prepend references = @../../assets/humans/xbot.usdc@
        customData = {
            string actor_id = "human_1"
            string label = "Hero"
        }
    )
    {
        double3 xformOp:translate = (0, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""",
            encoding="utf-8",
        )

        # Initial user_edits.usda
        self.user_edits = self.scene_dir / "user_edits.usda"
        self.user_edits.write_text(
            """#usda 1.0
(
    doc = "PainterSpace manual user edits layer"
)

over "World"
{
}
""",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_no_snapshot_when_no_shots_exist(self):
        # With user edits but no existing shots, saving should merge into scene.usda without creating snapshot
        self.user_edits.write_text(
            """#usda 1.0
over "World"
{
    over "Hero"
    {
        double3 xformOp:translate = (10, 0, 20)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""",
            encoding="utf-8",
        )

        res = save_scene_edits(str(self.scene_dir), rebuild_world_state=False)
        self.assertEqual(res["status"], "success")
        self.assertIsNone(res["snapshot"])
        self.assertFalse((self.scene_dir / "scene_v1.usda").exists())

        # Verify scene.usda has new position
        layer = Sdf.Layer.FindOrOpen(str(self.scene_usda))
        prim = layer.GetPrimAtPath("/World/Hero")
        attr = layer.GetAttributeAtPath("/World/Hero.xformOp:translate")
        self.assertEqual(attr.default, (10, 0, 20))

    def test_snapshot_and_shot_isolation(self):
        # 1. Create an existing shot_001 that sublayers ../scene.usda
        shot_001_dir = self.scene_dir / "shot_001"
        shot_001_dir.mkdir(parents=True, exist_ok=True)
        shot_001_usda = shot_001_dir / "shot.usda"
        shot_001_usda.write_text(
            """#usda 1.0
(
    defaultPrim = "World"
    subLayers = [
        @../scene.usda@
    ]
)

over "World"
{
    over "Hero"
    {
        double3 xformOp:translate.timeSamples = {
            0: (0, 0, 0),
            10: (1, 0, 0)
        }
    }
}
""",
            encoding="utf-8",
        )

        # 2. User edits the main scene (moves Hero to (50, 0, 50))
        self.user_edits.write_text(
            """#usda 1.0
over "World"
{
    over "Hero"
    {
        double3 xformOp:translate = (50, 0, 50)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""",
            encoding="utf-8",
        )

        # 3. Save scene edits
        res = save_scene_edits(str(self.scene_dir), rebuild_world_state=False)
        self.assertEqual(res["status"], "success")
        self.assertIsNotNone(res["snapshot"])
        self.assertEqual(res["snapshot"]["snapshot_version"], 1)
        self.assertEqual(res["snapshot"]["snapshot_filename"], "scene_v1.usda")
        self.assertIn("shot_001", res["snapshot"]["retargeted_shots"])

        # 4. Verify scene_v1.usda was created with the OLD hero position (0, 0, 0)
        scene_v1_path = self.scene_dir / "scene_v1.usda"
        self.assertTrue(scene_v1_path.exists())
        v1_layer = Sdf.Layer.FindOrOpen(str(scene_v1_path))
        attr_v1 = v1_layer.GetAttributeAtPath("/World/Hero.xformOp:translate")
        self.assertEqual(attr_v1.default, (0, 0, 0))

        # 5. Verify existing shot_001 was retargeted to sublayer ../scene_v1.usda
        shot_layer = Sdf.Layer.FindOrOpen(str(shot_001_usda))
        sublayers = list(shot_layer.subLayerPaths)
        self.assertIn("../scene_v1.usda", sublayers)
        self.assertNotIn("../scene.usda", sublayers)

        # 6. Verify main scene.usda NOW has the new position (50, 0, 50)
        cur_layer = Sdf.Layer.FindOrOpen(str(self.scene_usda))
        attr_cur = cur_layer.GetAttributeAtPath("/World/Hero.xformOp:translate")
        self.assertEqual(attr_cur.default, (50, 0, 50))

        # 7. Create a NEW shot_002: it sublayers @../scene.usda@
        shot_002_dir = self.scene_dir / "shot_002"
        shot_002_dir.mkdir(parents=True, exist_ok=True)
        shot_002_usda = shot_002_dir / "shot.usda"
        shot_002_usda.write_text(
            """#usda 1.0
(
    defaultPrim = "World"
    subLayers = [
        @../scene.usda@
    ]
)

over "World" {}
""",
            encoding="utf-8",
        )

        # Verify composition: shot_001 sees Hero at (0, 0, 0), shot_002 sees Hero at (50, 0, 50)
        stage_001 = Usd.Stage.Open(str(shot_001_usda))
        hero_001 = stage_001.GetPrimAtPath("/World/Hero")
        xform_001 = UsdGeom.Xformable(hero_001)
        ops_001 = xform_001.GetOrderedXformOps()
        # At rest / default of underlying scene:
        # shot_001's sublayered stage is scene_v1.usda where translate is (0, 0, 0)
        stage_v1 = Usd.Stage.Open(str(scene_v1_path))
        hero_v1 = stage_v1.GetPrimAtPath("/World/Hero")
        val_v1 = hero_v1.GetAttribute("xformOp:translate").Get()
        self.assertEqual(val_v1, (0, 0, 0))

        stage_002 = Usd.Stage.Open(str(shot_002_usda))
        hero_002 = stage_002.GetPrimAtPath("/World/Hero")
        val_002 = hero_002.GetAttribute("xformOp:translate").Get()
        self.assertEqual(val_002, (50, 0, 50))

        # 8. Successive edits: edit scene again, make shot_003
        self.user_edits.write_text(
            """#usda 1.0
over "World"
{
    over "Hero"
    {
        double3 xformOp:translate = (100, 0, 100)
    }
}
""",
            encoding="utf-8",
        )
        res2 = save_scene_edits(str(self.scene_dir), rebuild_world_state=False)
        self.assertEqual(res2["snapshot"]["snapshot_version"], 2)
        self.assertEqual(res2["snapshot"]["snapshot_filename"], "scene_v2.usda")
        # shot_001 was already on scene_v1.usda, so ONLY shot_002 should be retargeted to scene_v2.usda
        self.assertEqual(res2["snapshot"]["retargeted_shots"], ["shot_002"])

        shot_001_layer = Sdf.Layer.FindOrOpen(str(shot_001_usda))
        shot_001_layer.Reload()
        self.assertIn("../scene_v1.usda", list(shot_001_layer.subLayerPaths))

        shot_002_layer = Sdf.Layer.FindOrOpen(str(shot_002_usda))
        shot_002_layer.Reload()
        self.assertIn("../scene_v2.usda", list(shot_002_layer.subLayerPaths))


if __name__ == "__main__":
    unittest.main()
