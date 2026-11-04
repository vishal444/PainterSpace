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
import math
import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom
from pathlib import Path

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
            end_time=5.0 / 0.65,
            duration=5.0 / 0.65,
            params={"stairs": "buildings.test_stair_building:stairs_main", "direction": "up", "pace": "walk"},
            events={},
        )
        perf_good, s_err_none = synth.synthesize_actor(
            "human_1",
            {"position": [0.0, 0.2, 4.0], "facing_deg": 180.0},
            [good_beat],
            {},
            {},
            total_duration_s=5.0 / 0.65,
        )
        self.assertIsNone(s_err_none)
        self.assertIsNotNone(perf_good)
        self.assertEqual(len(perf_good.actions), 1)
        action = perf_good.actions[0]
        self.assertEqual(action["clip"], "walking_up_the_stairs")
        # Step frequency f = 0.65 / 0.333 = 1.95 Hz. Clip freq = 1.714 Hz. Ratio ~ 1.14
        self.assertAlmostEqual(action["speed"], 1.14, delta=0.15)

    def test_opposite_side_climbers_use_separate_treads_and_timed_approaches(self):
        from plan_model import Beat, ShotPlan
        from timeline_resolver import TimelineResolver

        starts = {
            "left": {"position": [-1.7, 0.0, 1.333], "facing_deg": 180.0},
            "right": {"position": [1.72, 0.0, 1.333], "facing_deg": 180.0},
        }
        plan = ShotPlan(beats=[
            Beat(f"climb_{actor}", "climb_stairs", actor,
                 {"stairs": "buildings.test_stair_building:stairs_main", "direction": "up", "pace": "run"})
            for actor in starts
        ])
        synth = MotionSynthesizer(self.manifest, fps=24)
        compiler = object.__new__(ShotCompiler)
        self.assertIsNone(compiler._prepare_stair_climbs(plan, starts, synth))
        timeline, error = TimelineResolver(fps=24).resolve(plan, starts)
        self.assertIsNone(error)
        self.assertGreater(timeline.total_duration_s, 3.5)
        performances = {}
        for actor, state in starts.items():
            perf, error = synth.synthesize_actor(actor, state, timeline.actor_schedules[actor],
                                                 {}, {}, timeline.total_duration_s)
            self.assertIsNone(error)
            performances[actor] = perf
            moving_actions = [action for action in perf.actions if action["clip"] in
                              ("run", "running_up_stairs")]
            self.assertEqual([action["clip"] for action in moving_actions[:2]],
                             ["run", "running_up_stairs"])
            stair_action = moving_actions[1]
            stair_duration = stair_action["end_s"] - stair_action["start_s"]
            expected_speed = (5.0 / stair_duration / 0.333) / 3.43
            self.assertAlmostEqual(stair_action["speed"], expected_speed, delta=0.02)
            top, _ = perf.sample(stair_action["end_s"])
            self.assertAlmostEqual(top[1], 3.2, places=3)
        for i in range(25):
            t = timeline.total_duration_s * i / 24
            left, _ = performances["left"].sample(t)
            right, _ = performances["right"].sample(t)
            self.assertGreaterEqual(math.hypot(left[0] - right[0], left[2] - right[2]), 0.79)

        stage, _ = self._create_stair_stage("opposite_climbers.usda")
        right_prim = stage.DefinePrim("/World/OtherHero", "Xform")
        right_prim.SetCustomDataByKey("asset_id", "character.human")
        right_prim.GetReferences().AddReference(
            os.path.abspath("assets/usdc_assets/humans/human.norm.usda"), "/root")
        for actor, prim_name in (("left", "Hero"), ("right", "OtherHero")):
            xform = UsdGeom.Xformable(stage.GetPrimAtPath(f"/World/{prim_name}"))
            translate = xform.AddTranslateOp()
            for frame in range(math.ceil(timeline.total_duration_s * 24) + 1):
                position, _ = performances[actor].sample(frame / 24)
                translate.Set(Gf.Vec3d(*position), frame)
        stage.SetEndTimeCode(math.ceil(timeline.total_duration_s * 24))
        stage.GetRootLayer().Save()
        stair_contact = [{"actor": actor, "asset": "Building", "part": "body",
                          "max_depth_m": 0.1, "t0": 0.0, "t1": timeline.total_duration_s}
                         for actor in ("Hero", "OtherHero")]
        result = validate(stage, self.db, t0=0, t1=stage.GetEndTimeCode(),
                          allow=stair_contact)
        self.assertTrue(result["ok"], result.get("violations"))

    def test_stair_geometry_uses_placed_instance_transform(self):
        from plan_model import Beat, ShotPlan

        matrix = [[2.0, 0.0, 0.0, 0.0],
                  [0.0, 1.5, 0.0, 0.0],
                  [0.0, 0.0, 2.0, 0.0],
                  [8.0, 0.0, -5.0, 1.0]]
        synth = MotionSynthesizer(self.manifest, scene_data={"objects": [
            {"name": "ExistingStairs", "asset": "buildings.test_stair_building",
             "world_transform": matrix}]})
        stair = synth._resolve_stair("ExistingStairs:stairs_main")
        for actual, expected in zip(stair["data"]["centerline"][0], [8.0, 0.3, 3.0]):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(stair["data"]["centerline"][-1], [8.0, 4.8, -7.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(stair["data"]["width_m"], 2.8, places=2)
        self.assertAlmostEqual(stair["data"]["treads"][0]["tread_m"], 0.666, places=3)
        self.assertIsNone(synth._resolve_stair("OtherStairs:stairs_main"))

        compiler = object.__new__(ShotCompiler)
        plan = ShotPlan(beats=[Beat("climb", "climb_stairs", "hero", {"stairs": "stairs_main"})])
        empty_scene = MotionSynthesizer(self.manifest, scene_data={"objects": [
            {"name": "Club", "asset": "buildings.grand_night_club"}]})
        error = compiler._prepare_stair_climbs(plan, {"hero": {"position": [0, 0, 0]}}, empty_scene)
        self.assertEqual(error.error_code, "STAIR_NOT_IN_SCENE")
        self.assertIsNone(MotionSynthesizer(self.manifest, scene_data={"objects": []})
                          ._resolve_stair("stairs_main"))

    def test_manual_city_square_replaces_club_in_stair_context(self):
        scene_path = Path(self.temp_dir) / "scene.usda"
        edits_path = Path(self.temp_dir) / "user_edits.usda"
        shot_dir = Path(self.temp_dir)
        scene = Usd.Stage.CreateNew(str(scene_path))
        world = scene.DefinePrim("/World", "Xform")
        scene.SetDefaultPrim(world)
        club = scene.DefinePrim("/World/GrandNightClub", "Xform")
        club.SetCustomDataByKey("asset_id", "buildings.grand_night_club")
        scene.GetRootLayer().Save()
        edits_path.write_text(
            '#usda 1.0\nover "World" {\n'
            '    over "GrandNightClub" (active = false) {}\n'
            '    def Xform "CitySquare" (\n'
            '        customData = { string asset_id = "buildings.city_square" }\n'
            f'        prepend references = @{os.path.abspath("assets/usdc_assets/buildings/city_square.usdc")}@\n'
            '    ) {}\n}\n', encoding="utf-8")
        compiler = ShotCompiler()
        context = compiler._read_scene_context(scene_path, shot_dir=shot_dir)
        names = {obj["name"] for obj in context["objects"]}
        self.assertIn("CitySquare", names)
        self.assertNotIn("GrandNightClub", names)
        stairs = next(obj["stairs"] for obj in context["objects"] if obj["name"] == "CitySquare")
        self.assertEqual(set(stairs), {"ascending_steps", "descending_steps"})
        self.assertAlmostEqual(stairs["ascending_steps"]["width_m"], 6.0, places=2)
        self.assertAlmostEqual(stairs["descending_steps"]["centerline"][-1][1], 1.7, places=2)

        from llm_planner import LLMShotPlanner
        prompt = LLMShotPlanner(FunctionCatalog(manifest=compiler.manifest,
                                                scene_data=context)).build_system_prompt()
        self.assertIn("CitySquare:ascending_steps", prompt)
        self.assertIn("CitySquare:descending_steps", prompt)
        self.assertNotIn("GrandNightClub", prompt)

        from semantic_references import ReferenceResolver
        resolver = ReferenceResolver(compiler.manifest, context)
        bottom, error = resolver.resolve("CitySquare:descending_steps_bottom", {})
        self.assertEqual(error, "")
        self.assertAlmostEqual(bottom.position[2], 3.04, places=2)
        synth = MotionSynthesizer(compiler.manifest, scene_data=context)
        self.assertIsNotNone(synth._resolve_stair("CitySquare:ascending_steps"))

        composed = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(scene_path)),
                                  Sdf.Layer.FindOrOpen(str(edits_path)))
        from plan_path import scene_stairs
        discovered = scene_stairs(composed, self.db)
        self.assertEqual({stair["stair_name"] for stair in discovered},
                         {"ascending_steps", "descending_steps"})

        import process_agent_queue as queue
        pending = shot_dir / "pending_request.json"
        pending.write_text(json.dumps({"scene_stage": str(scene_path),
                                       "target_stage": str(shot_dir / "shot.usda"),
                                       "prompt": "Two people climb the stairs from opposite sides.",
                                       "world_state": {"objects": [{"id": "CitySquare", "asset": "buildings.city_square",
                                                                    "position": [0, 0, 0]},
                                                                   {"id": "Hero", "asset": "character.human",
                                                                    "position": [1, 0, 2]}]}}),
                           encoding="utf-8")
        original_pending = queue.PENDING_FILE
        try:
            queue.PENDING_FILE = pending
            self.assertEqual(queue.prepare_pending_task(), 0)
        finally:
            queue.PENDING_FILE = original_pending
        request = json.loads(pending.read_text(encoding="utf-8"))
        current = {obj["name"]: obj for obj in request["scene_context"]["objects"]}
        self.assertIn("CitySquare", current)
        self.assertNotIn("GrandNightClub", current)
        self.assertEqual(set(current["CitySquare"]["stairs"]),
                         {"ascending_steps", "descending_steps"})
        self.assertIn("CitySquare:ascending_steps", request["planner_rules"])
        self.assertEqual(request["current_shot_state"]["objects"][1]["position"], [1, 0, 2])

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
