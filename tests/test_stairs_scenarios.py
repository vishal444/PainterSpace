"""Scenarios K, L, M, N, O for Stairs & Levels Verification.

- Scenario K: Walk up straight flight (PASS)
- Scenario L: Walk through stair side handrail/wall (FAIL)
- Scenario M: Walk down straight flight (PASS)
- Scenario N: Cadence matching & structured error handling (PASS)
- Scenario O: Low ceiling headroom collision (FAIL)
"""
import unittest
import os
import sys
import shutil
import tempfile
import json
import numpy as np
from pxr import Gf, Usd, UsdGeom

sys.path.insert(0, os.path.abspath("tools"))
sys.path.insert(0, os.path.abspath("tools/collision"))
sys.path.insert(0, os.path.abspath("tools/shot_library"))

from colliders import AssetDB
from validate_collisions import validate
from plan_path import author_root_motion, plan
from function_catalog import FunctionCatalog
from motion_synthesis import MotionSynthesizer
from compiler import ShotCompiler


class TestStairsScenarios(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="painterspace_stair_scenarios_")
        self.db = AssetDB("manifest.json")
        with open("manifest.json", "r") as f:
            self.manifest = json.load(f)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_stair_stage(self, stage_name="stage.usda"):
        """Helper to create a stage with test_building_stairs."""
        path = os.path.join(self.temp_dir, stage_name)
        stage = Usd.Stage.CreateNew(path)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        world = stage.DefinePrim("/World", "Xform")
        stage.SetDefaultPrim(world)

        # Place stair building
        bld = stage.DefinePrim("/World/Building", "Xform")
        bld.SetCustomDataByKey("asset_id", "buildings.test_stair_building")
        bld_ref_path = os.path.abspath("assets/usdc_assets/buildings/test_building_stairs.norm.usda")
        bld.GetReferences().AddReference(bld_ref_path)

        # Place character
        hero = stage.DefinePrim("/World/Hero", "Xform")
        hero.SetCustomDataByKey("asset_id", "character.human")
        hero_ref_path = os.path.abspath("assets/usdc_assets/humans/human.norm.usda")
        hero.GetReferences().AddReference(hero_ref_path, "/root")

        stage.SetStartTimeCode(0.0)
        stage.SetEndTimeCode(48.0)
        stage.GetRootLayer().Save()
        return stage, path

    def test_scenario_k_walk_up_straight_flight_pass(self):
        """Scenario K: Character walks straight up flight from L0 to L1 along centerline (PASS)."""
        stage, stage_path = self._create_stair_stage("scenario_k.usda")
        start = (0.0, 0.2, 5.0)
        goal = (0.0, 3.2, -2.0)
        wp = plan(stage, self.db, start, goal, [0.0], radius=0.35)
        self.assertIsNotNone(wp)

        # Author root motion
        author_root_motion(stage, "/World/Hero", wp, speed=0.7, t0=0, fps=24)
        stage.GetRootLayer().Save()

        # Stair climb allows minor hand brush on railing (within 10cm)
        allow_rules = [
            {"actor": "Hero", "asset": "Building", "part": "body", "max_depth_m": 0.10, "t0": 0.0, "t1": 3.0}
        ]

        # Validate collisions
        res = validate(stage, self.db, t0=0, t1=48, allow=allow_rules)
        self.assertTrue(res["ok"], f"Expected clean pass for Scenario K, got violations: {res['violations']}")
        self.assertEqual(len(res.get("level_violations", [])), 0)

    def test_scenario_l_walk_through_handrail_fail(self):
        """Scenario L: Character walks laterally across stair boundary through handrail (FAIL)."""
        stage, stage_path = self._create_stair_stage("scenario_l.usda")
        # Path cut straight across X from -1.5 to +1.5 at z = 1.5, y = 1.7 (crosses through both handrails)
        wp_cut = [(-1.5, 1.7, 1.5), (1.5, 1.7, 1.5)]
        author_root_motion(stage, "/World/Hero", wp_cut, speed=0.8, t0=0, fps=24)
        stage.GetRootLayer().Save()

        res = validate(stage, self.db, t0=0, t1=48)
        self.assertFalse(res["ok"], "Expected collision failure when walking through handrail")
        self.assertGreater(len(res["violations"]), 0)
        hit_parts = [v.get("asset") for v in res["violations"]]
        self.assertTrue(any("test_stair_building" in str(p) for p in hit_parts))

    def test_scenario_m_walk_down_straight_flight_pass(self):
        """Scenario M: Character walks down straight flight from L1 to L0 (PASS)."""
        stage, stage_path = self._create_stair_stage("scenario_m.usda")
        start = (0.0, 3.2, -2.0)
        goal = (0.0, 0.2, 5.0)
        wp = plan(stage, self.db, start, goal, [0.0], radius=0.35)
        self.assertIsNotNone(wp)

        # Author root motion descending
        author_root_motion(stage, "/World/Hero", wp, speed=0.7, t0=0, fps=24)
        stage.GetRootLayer().Save()

        res = validate(stage, self.db, t0=0, t1=48)
        self.assertTrue(res["ok"], f"Expected clean pass for Scenario M, got violations: {res['violations']}")
        self.assertEqual(len(res.get("level_violations", [])), 0)

    def test_scenario_n_cadence_and_structured_errors_pass(self):
        """Scenario N: Climb stairs cadence calculation, invalid pace, and speed limit structured errors."""
        catalog = FunctionCatalog(manifest=self.manifest)
        # Invalid pace error
        ok, err = catalog.climb_stairs("human_1", stairs="test_stair_building:stairs_main", pace="sprint_stairs")
        self.assertFalse(ok)
        self.assertIn("Invalid pace", str(err))

        # Unsafe speed structured error in MotionSynthesizer
        synth = MotionSynthesizer(self.manifest, fps=24)
        from tools.shot_library.plan_model import Beat
        from tools.shot_library.timeline_resolver import ResolvedBeat

        bad_speed_beat = ResolvedBeat(
            beat_id="beat_climb_fast",
            actor_id="human_1",
            function="climb_stairs",
            start_time=0.0,
            end_time=2.0,
            duration=2.0,
            params={"stairs": "buildings.test_stair_building:stairs_main", "direction": "down", "speed_mps": 3.5},
            events={},
        )
        perf, s_err = synth.synthesize_actor(
            "human_1",
            {"position": [0.0, 3.2, -1.0], "facing_deg": 180.0},
            [bad_speed_beat],
            {},
            {},
            total_duration_s=2.0,
        )
        self.assertIsNone(perf)
        self.assertIsNotNone(s_err)
        self.assertEqual(s_err.error_code, "UNSAFE_STAIR_SPEED")

        # Valid pace cadence matching
        good_beat = ResolvedBeat(
            beat_id="beat_climb_good",
            actor_id="human_1",
            function="climb_stairs",
            start_time=0.0,
            end_time=5.0,
            duration=5.0,
            params={"stairs": "buildings.test_stair_building:stairs_main", "direction": "up", "pace": "walk"},
            events={},
        )
        perf_good, s_err_none = synth.synthesize_actor(
            "human_1",
            {"position": [0.0, 0.2, 4.0], "facing_deg": 180.0},
            [good_beat],
            {},
            {},
            total_duration_s=5.0,
        )
        self.assertIsNone(s_err_none)
        self.assertIsNotNone(perf_good)
        self.assertEqual(len(perf_good.actions), 1)
        action = perf_good.actions[0]
        self.assertEqual(action["clip"], "walking_up_the_stairs")
        # Step frequency f = 0.65 / 0.333 = 1.95 Hz. Clip freq = 1.714 Hz. Ratio ~ 1.14
        self.assertAlmostEqual(action["speed"], 1.14, delta=0.15)

    def test_scenario_o_low_ceiling_headroom_fail(self):
        """Scenario O: Low ceiling obstruction above stair landing hits character head (FAIL)."""
        stage, stage_path = self._create_stair_stage("scenario_o.usda")

        # Add a low ceiling obstacle at z = 1.5, y = 2.0 (stair is at y = 1.7, clearance = 0.3m < 2.1m)
        ceil_prim = stage.DefinePrim("/World/LowCeiling", "Cube")
        UsdGeom.XformCommonAPI(ceil_prim).SetTranslate(Gf.Vec3d(0.0, 2.0, 1.5))
        UsdGeom.XformCommonAPI(ceil_prim).SetScale(Gf.Vec3f(2.0, 0.2, 2.0))

        start = (0.0, 0.2, 5.0)
        goal = (0.0, 3.2, -2.0)
        wp = [(0.0, 0.2, 5.0), (0.0, 1.7, 1.5), (0.0, 3.2, -2.0)]
        author_root_motion(stage, "/World/Hero", wp, speed=0.7, t0=0, fps=24)
        stage.GetRootLayer().Save()

        res = validate(stage, self.db, t0=0, t1=48)
        self.assertFalse(res["ok"], "Expected low ceiling collision failure")


if __name__ == "__main__":
    unittest.main()
