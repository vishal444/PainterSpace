"""Unit tests for Phase 3: Multi-Level Path Planning and Elevation Synthesis.
"""
import unittest
import numpy as np
from pxr import Gf, Usd, UsdGeom
import os
import shutil
import tempfile

from tools.collision.colliders import AssetDB
from tools.collision.plan_path import (
    generate_stair_waypoints,
    sample_path,
    scene_levels,
    scene_stairs,
    plan,
)


class TestStairsPhase3(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="painterspace_test_phase3_")
        self.stage_path = os.path.join(self.temp_dir, "test_stair_stage.usda")
        self.db = AssetDB("manifest.json")

        # Create a test stage referencing buildings.test_stair_building
        stage = Usd.Stage.CreateNew(self.stage_path)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        world = stage.DefinePrim("/World", "Xform")
        stage.SetDefaultPrim(world)

        # Place the building instance
        bld = stage.DefinePrim("/World/Building", "Xform")
        bld.SetCustomDataByKey("asset_id", "buildings.test_stair_building")
        bld_ref_path = os.path.abspath("assets/usdc_assets/buildings/test_building_stairs.norm.usda")
        bld.GetReferences().AddReference(bld_ref_path)

        stage.GetRootLayer().Save()
        self.stage = stage

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stair_waypoints_easing(self):
        """Test continuous nosing elevation with smoothstep easing."""
        centerline = [[0.0, 0.0, 4.0], [0.0, 3.0, -1.0]]
        wp = generate_stair_waypoints(centerline, y_start=0.0, y_end=3.0, ease_dist=0.30)
        self.assertGreater(len(wp), 10)

        # Check endpoints
        self.assertAlmostEqual(wp[0][1], 0.0, places=2)
        self.assertAlmostEqual(wp[-1][1], 3.0, places=2)

        # Verify monotonic ascent
        ys = [p[1] for p in wp]
        for i in range(len(ys) - 1):
            self.assertGreaterEqual(ys[i + 1], ys[i], f"Non-monotonic elevation at step {i}: {ys[i]} -> {ys[i+1]}")

        # Midpoint should be approximately 1.5m
        mid_idx = len(wp) // 2
        self.assertAlmostEqual(ys[mid_idx], 1.5, delta=0.15)

    def test_sample_path_torso_pitch(self):
        """Verify forward torso pitch is generated during stair ascent and descent."""
        # Ascending path
        wp_asc = [(0.0, 0.0, 5.0), (0.0, 0.0, 4.0), (0.0, 1.5, 1.5), (0.0, 3.0, -1.0), (0.0, 3.0, -2.0)]
        pos, head, pitch_asc = sample_path(wp_asc, speed=1.0, fps=24, return_pitch=True)
        self.assertEqual(pos.shape[1], 3)
        # Should have positive pitch (forward lean) during stair climbing frames
        max_pitch = np.max(pitch_asc)
        self.assertGreater(max_pitch, 4.0, f"Expected forward pitch > 4 deg, got {max_pitch}")

        # Descending path
        wp_desc = [(0.0, 3.0, -2.0), (0.0, 3.0, -1.0), (0.0, 1.5, 1.5), (0.0, 0.0, 4.0), (0.0, 0.0, 5.0)]
        pos, head, pitch_desc = sample_path(wp_desc, speed=1.0, fps=24, return_pitch=True)
        min_pitch = np.min(pitch_desc)
        self.assertLess(min_pitch, -1.0, f"Expected negative pitch (lean back) < -1 deg, got {min_pitch}")

    def test_multi_level_path_planning(self):
        """Verify routing from L0 to L1 traverses the stair portal with smooth 3D waypoints."""
        start = (0.0, 0.0, 6.0)
        goal = (0.0, 3.0, -3.0)
        times = [0.0]

        wp = plan(self.stage, self.db, start, goal, times, radius=0.40)
        self.assertIsNotNone(wp)
        self.assertGreater(len(wp), 5)

        # First waypoint at L0, last waypoint at L1
        self.assertAlmostEqual(wp[0][1], 0.0, places=2)
        self.assertAlmostEqual(wp[-1][1], 3.0, places=2)

        # Verify that waypoints traverse through the stair centerline region
        z_coords = [p[2] for p in wp]
        self.assertTrue(any(-1.0 <= z <= 4.0 for z in z_coords))

    def test_multi_level_path_descending(self):
        """Verify routing descending from L1 to L0."""
        start = (0.0, 3.0, -3.0)
        goal = (0.0, 0.0, 6.0)
        times = [0.0]

        wp = plan(self.stage, self.db, start, goal, times, radius=0.40)
        self.assertIsNotNone(wp)
        self.assertGreater(len(wp), 5)

        self.assertAlmostEqual(wp[0][1], 3.0, places=2)
        self.assertAlmostEqual(wp[-1][1], 0.0, places=2)

        # Elevation should descend
        y_coords = [p[1] for p in wp]
        self.assertGreater(y_coords[0], y_coords[-1])


if __name__ == "__main__":
    unittest.main()
