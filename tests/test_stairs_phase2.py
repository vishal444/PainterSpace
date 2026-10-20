"""Unit tests for Phase 2: Stair Collision Carve-Out, Per-Level Bands, and Headroom Checks.
"""
import unittest
import json
import numpy as np
from tools.collision.ingest_asset import check_stair_headroom, boxes_from_tris


class TestStairsPhase2(unittest.TestCase):

    def setUp(self):
        with open("manifest.json", "r") as f:
            manifest = json.load(f)
        self.entry = next((a for a in manifest["assets"] if a["id"] == "buildings.test_stair_building"), None)
        self.assertIsNotNone(self.entry, "buildings.test_stair_building not found in manifest")

    def test_stair_steps_carved_out(self):
        """Verify that stair steps are excluded from collision boxes, but side rails are kept."""
        shapes = self.entry.get("collision", {}).get("shapes", [])
        self.assertGreater(len(shapes), 0, "Expected collision shapes for building")

        # Check that no shape sits directly in the walking corridor center (|x| < 0.4) along the stair incline
        for s in shapes:
            cx, cy, cz = s["center"]
            sx, sy, sz = s["size"]
            # Stair is roughly z in [-1.5, 4.0], y in [0.0, 3.0], corridor width = 1.2 (|x| <= 0.6)
            if -1.2 <= cz <= 3.8 and 0.1 <= cy <= 2.9:
                # If it's inside the stair Z and Y range, it must be outside the walk corridor (|cx| > 0.5)
                self.assertGreater(abs(cx), 0.5, f"Step box found inside walking corridor: {s}")

    def test_handrails_preserved(self):
        """Verify handrails on both left and right (around x = +/- 0.75) are preserved."""
        shapes = self.entry.get("collision", {}).get("shapes", [])
        left_rails = [s for s in shapes if s["center"][0] < -0.5]
        right_rails = [s for s in shapes if s["center"][0] > 0.5]
        self.assertGreater(len(left_rails), 0, "Left handrail shapes missing")
        self.assertGreater(len(right_rails), 0, "Right handrail shapes missing")

    def test_headroom_check_clean(self):
        """The test building has open headroom; check_stair_headroom should report 0 violations."""
        stairs_data = self.entry.get("stairs", {})
        # Create mock tris representing clear space (or far above)
        # 10m high ceiling triangle
        mock_tris = np.array([
            [[-1.0, 5.0, 0.0], [1.0, 5.0, 0.0], [0.0, 5.0, 2.0]]
        ], dtype=float)
        violations = check_stair_headroom(mock_tris, stairs_data, min_headroom_m=2.10)
        self.assertEqual(len(violations), 0)

    def test_headroom_check_low_ceiling_detected(self):
        """Low ceiling triangle at y = 1.50 over a stair point at y = 0.50 (clearance 1.0m < 2.10m)."""
        stairs_data = self.entry.get("stairs", {})
        cline = stairs_data["stairs_main"]["centerline"]
        mid_point = [(cline[0][i] + cline[1][i]) / 2.0 for i in range(3)]
        stair_y = mid_point[1]

        # Put a ceiling triangle 1.2m above the stair point
        ceil_y = stair_y + 1.20
        cz = mid_point[2]
        low_ceiling_tri = np.array([
            [[-0.3, ceil_y, cz - 0.2], [0.3, ceil_y, cz - 0.2], [0.0, ceil_y, cz + 0.2]]
        ], dtype=float)

        violations = check_stair_headroom(low_ceiling_tri, stairs_data, min_headroom_m=2.10)
        self.assertGreater(len(violations), 0, "Expected headroom violation for low ceiling")
        min_clear = min(v["clearance_m"] for v in violations)
        self.assertLess(min_clear, 2.10)
        self.assertTrue(1.0 <= min_clear <= 1.4, f"Expected clearance around 1.2m, got {min_clear}")


if __name__ == "__main__":
    unittest.main()
