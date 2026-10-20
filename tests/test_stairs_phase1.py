import unittest
from pathlib import Path
import sys
import numpy as np
from pxr import Usd, UsdGeom

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "collision"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))

from manifest_schema import enrich_manifest, CLIP_CATEGORIES
from ingest_asset import extract_stairs_and_levels
from colliders import l2w


class TestStairsPhase1(unittest.TestCase):
    def setUp(self):
        self.building_usda = REPO_ROOT / "assets" / "usdc_assets" / "buildings" / "test_building_stairs.usda"
        self.assertTrue(self.building_usda.exists(), "test_building_stairs.usda must exist")

    def test_stair_clips_manifest_enrichment(self):
        import json
        raw_manifest = json.loads((REPO_ROOT / "manifest.json").read_text())
        m = enrich_manifest(raw_manifest)
        clips = m.get("clips", {})
        
        # Check walking up stairs clip metadata
        self.assertIn("walking_up_the_stairs", clips)
        c_walk = clips["walking_up_the_stairs"]
        self.assertEqual(c_walk["category"], "stair_locomotion")
        self.assertEqual(c_walk["stair_suitability"], "ascent")
        self.assertAlmostEqual(c_walk["f_clip"], 1.71, places=1)

        # Check running up stairs clip metadata
        self.assertIn("running_up_stairs", clips)
        c_run = clips["running_up_stairs"]
        self.assertEqual(c_run["stair_suitability"], "ascent")
        self.assertAlmostEqual(c_run["f_clip"], 3.43, places=1)

        # Check descending stairs clip metadata
        self.assertIn("descending_stairs_fast", clips)
        c_down = clips["descending_stairs_fast"]
        self.assertEqual(c_down["stair_suitability"], "descent")
        self.assertAlmostEqual(c_down["f_clip"], 4.80, places=1)

    def test_extract_stairs_and_levels_from_markers(self):
        stage = Usd.Stage.Open(str(self.building_usda))
        dprim = stage.GetDefaultPrim()
        xc = UsdGeom.XformCache()
        Wn = np.linalg.inv(l2w(xc, dprim))

        stairs, levels = extract_stairs_and_levels(dprim, xc, Wn)
        
        # Verify stair sequence extraction
        self.assertIn("stairs_main", stairs)
        s = stairs["stairs_main"]
        self.assertEqual(s["from_level"], "L0")
        self.assertEqual(s["to_level"], "L1")
        self.assertAlmostEqual(s["width_m"], 1.4, places=1)
        
        # Verify 3D centerline
        self.assertEqual(len(s["centerline"]), 2)
        self.assertAlmostEqual(s["centerline"][0][1], 0.0, places=2)
        self.assertAlmostEqual(s["centerline"][1][1], 3.0, places=2)

        # Verify tread table
        treads = s["treads"]
        self.assertEqual(len(treads), 15)
        self.assertAlmostEqual(treads[0]["rise_m"], 0.2, places=2)
        self.assertAlmostEqual(treads[0]["tread_m"], 0.333, places=2)

        # Verify derived levels
        self.assertEqual(len(levels), 2)
        level_ids = [lvl["id"] for lvl in levels]
        self.assertIn("L0", level_ids)
        self.assertIn("L1", level_ids)


if __name__ == "__main__":
    unittest.main()
