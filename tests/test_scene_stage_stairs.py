"""Scene-stage queue requests must keep the composed scene's stair portals."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pxr import Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tools" / "collision"),
                str(ROOT / "tools" / "shot_library")]

import compile_shot
from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from plan_model import Beat, ShotPlan
from support_surfaces import contains


class SceneStageStairTests(unittest.TestCase):
    def test_next_shot_stage_preserves_existing_shots(self):
        with tempfile.TemporaryDirectory() as temporary:
            scene_dir = Path(temporary)
            first = scene_dir / "shot_001"
            first.mkdir()
            (first / "shot.usda").write_text("#usda 1.0\n")
            second = scene_dir / "shot_002"
            second.mkdir()
            (second / "shot.json").write_text("{}")
            self.assertEqual(compile_shot._next_shot_stage(scene_dir),
                             scene_dir / "shot_003" / "shot.usda")

    def make_scene(self, folder: Path) -> Path:
        scene_path = folder / "scene.usda"
        stage = Usd.Stage.CreateNew(str(scene_path))
        world = stage.DefinePrim("/World", "Xform")
        stage.SetDefaultPrim(world)
        old = stage.DefinePrim("/World/TestBuilding", "Xform")
        old.SetCustomDataByKey("asset_id", "buildings.test_stair_building")
        stage.GetRootLayer().Save()

        city_asset = ROOT / "assets/usdc_assets/buildings/city_square.usdc"
        (folder / "user_edits.usda").write_text(
            '#usda 1.0\nover "World" {\n'
            '    over "TestBuilding" (active = false) {}\n'
            '    def Xform "CitySquare" (\n'
            '        customData = { string asset_id = "buildings.city_square" }\n'
            f'        prepend references = @{city_asset}@\n'
            '    ) {}\n}\n', encoding="utf-8")
        return scene_path

    def test_scene_output_without_explicit_source_uses_local_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)

            success, report = ShotCompiler(project_root=ROOT).compile(
                ShotPlan(), scene_path, validate=False)

            self.assertTrue(success, report)
            self.assertTrue(report["validation"]["collision_skipped"])
            self.assertIsNone(report["validation"]["collision_passed"])
            source = scene_path.read_text(encoding="utf-8")
            self.assertIn("@scene_v1.usda@", source)
            self.assertNotIn("@../scene.usda@", source)
            self.assertTrue((folder / "scene_v1.usda").exists())

    def test_scene_output_rejects_different_source_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            other_scene = folder / "other.usda"
            other_scene.write_text('#usda 1.0\ndef Xform "World" {}\n')

            success, report = ShotCompiler(project_root=ROOT).compile(
                ShotPlan(), scene_path, scene_usda_path=other_scene, validate=False)

            self.assertFalse(success)
            self.assertEqual(report["error"]["error_code"], "SCENE_STAGE_SOURCE_MISMATCH")
            self.assertNotIn("@../scene.usda@", scene_path.read_text(encoding="utf-8"))

    def test_queue_routes_action_plan_to_new_shot(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            compiler = ShotCompiler(project_root=ROOT)
            context = compiler._read_scene_context(scene_path, shot_dir=folder)
            self.assertEqual({obj["name"] for obj in context["objects"]}, {"CitySquare"})
            self.assertIn("ascending_steps", context["objects"][0]["stairs"])

            queue = folder / "task_queue"
            queue.mkdir()
            (queue / "pending_request.json").write_text(json.dumps({
                "prompt": "A man runs up the Town Square stairs.",
                "target_stage": "scene.usda",
            }), encoding="utf-8")
            response = queue / "llm_response.json"
            response.write_text(json.dumps({"calls": [
                {"function": "place", "args": {"asset_id": "character.male", "label": "human_1"}},
                {"function": "climb_stairs", "args": {"actor": "human_1",
                                                       "stairs": "CitySquare:ascending_steps", "pace": "run"}},
            ]}), encoding="utf-8")

            seen = []
            def capture_compile(plan, output, scene_usda_path=None, validate=True):
                seen.append((output, scene_usda_path, validate))
                return False, {"error": {"error_code": "TEST_STOP"}}

            with mock.patch.object(compile_shot, "REPO_ROOT", folder), \
                 mock.patch.object(compile_shot, "ShotCompiler", return_value=compiler), \
                 mock.patch.object(compiler, "compile", side_effect=capture_compile), \
                 mock.patch.object(compile_shot, "_write_validation_feedback"):
                self.assertEqual(compile_shot.execute_llm_response(response), 2)

            self.assertEqual(seen, [((folder / "shot_001" / "shot.usda").resolve(),
                                     scene_path.resolve(), True)])

    def test_validated_compiler_rejects_action_beats_in_shared_scene(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            original = scene_path.read_bytes()
            plan = ShotPlan(beats=[Beat("walk", "walk_to", "human_1", {"target": "CitySquare"})])
            success, report = ShotCompiler(project_root=ROOT).compile(
                plan, scene_path, scene_usda_path=scene_path, validate=True)
            self.assertFalse(success)
            self.assertEqual(report["error"]["error_code"], "SCENE_ACTIONS_REQUIRE_SHOT")
            self.assertEqual(scene_path.read_bytes(), original)

    def test_failed_scene_collision_keeps_original_scene(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            original = scene_path.read_bytes()
            collision = {"ok": False, "violations": [{"actor": "vehicle_a",
                                                         "instance": "vehicle_b",
                                                         "part": "body",
                                                         "worst_depth_m": 0.1}]}
            with mock.patch.object(ShotCompiler, "_run_collision_validator",
                                   return_value=(False, collision)) as validator:
                success, report = ShotCompiler(project_root=ROOT).compile(
                    ShotPlan(), scene_path, scene_usda_path=scene_path, validate=True)
            self.assertFalse(success)
            self.assertTrue(validator.called)
            self.assertEqual(scene_path.read_bytes(), original)
            self.assertFalse(report["validation"]["collision_passed"])

    def test_clear_scene_stage_publishes_after_real_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            success, report = ShotCompiler(project_root=ROOT).compile(
                ShotPlan(), scene_path, scene_usda_path=scene_path, validate=True)
            self.assertTrue(success, report.get("error"))
            self.assertTrue(report["validation"]["collision_passed"])
            self.assertIn("@scene_v1.usda@", scene_path.read_text())

    def test_context_exposes_shared_support_at_stair_tops(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            context = ShotCompiler(project_root=ROOT)._read_scene_context(scene_path, shot_dir=folder)
            stairs = context["objects"][0]["stairs"]
            arrivals = [stairs[name]["centerline"][-1]
                        for name in ("ascending_steps", "descending_steps")]
            shared = [surface for surface in context["support_surfaces"]
                      if all(contains(surface, arrival) for arrival in arrivals)]
            self.assertTrue(shared)
            midpoint = [sum(point[axis] for point in arrivals) / len(arrivals)
                        for axis in range(3)]
            self.assertTrue(any(contains(surface, midpoint, 0.35) for surface in shared))

    def test_scene_compile_resolves_portal_before_motion(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            compiler = ShotCompiler(project_root=ROOT)
            plan = ShotPlan(beats=[Beat("climb_stairs_1", "climb_stairs", "human_1",
                                        {"stairs": "CitySquare:ascending_steps", "direction": "up",
                                         "pace": "run"})])

            class ReachedStairPreflight(Exception):
                pass

            def inspect_stair(_plan, _states, synthesizer):
                self.assertIsNotNone(synthesizer._resolve_stair("CitySquare:ascending_steps"))
                self.assertIsNotNone(synthesizer._resolve_stair("CitySquare:descending_steps"))
                raise ReachedStairPreflight

            with mock.patch.object(compiler, "_prepare_stair_climbs", side_effect=inspect_stair):
                with self.assertRaises(ReachedStairPreflight):
                    compiler.compile(plan, scene_path, scene_usda_path=scene_path, validate=False)

            self.assertTrue((folder / "scene_v1.usda").exists())
            self.assertTrue((folder / "scene_edits_v1.usda").exists())

    def test_compiled_scene_keeps_edited_town_square(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            scene_path = self.make_scene(folder)
            compiler = ShotCompiler(project_root=ROOT)
            context = compiler._read_scene_context(scene_path, shot_dir=folder)
            catalog = FunctionCatalog(compiler.manifest, scene_data=context)
            placed, result = catalog.place("character.male",
                                           at_reference="CitySquare:ascending_steps_bottom")
            self.assertTrue(placed)
            climbed, _ = catalog.climb_stairs(result["actor_id"],
                                               "CitySquare:ascending_steps", pace="run")
            self.assertTrue(climbed)

            success, report = compiler.compile(catalog.plan, scene_path,
                                               scene_usda_path=scene_path, validate=False)
            self.assertTrue(success, report)
            composed = Usd.Stage.Open(str(scene_path))
            self.assertTrue(composed.GetPrimAtPath("/World/CitySquare").IsValid())
            self.assertFalse(composed.GetPrimAtPath("/World/TestBuilding").IsActive())
            actor = next(prim for prim in composed.Traverse()
                         if prim.GetCustomDataByKey("actor_id") == result["actor_id"])
            translate = next(op for op in UsdGeom.Xformable(actor).GetOrderedXformOps()
                             if op.GetOpName() == "xformOp:translate")
            heights = [translate.Get(time)[1] for time in translate.GetTimeSamples()]
            self.assertGreater(max(heights) - min(heights), 1.0)
            rebuilt = compiler._read_scene_context(scene_path)
            square = next(obj for obj in rebuilt["objects"] if obj["name"] == "CitySquare")
            self.assertIn("ascending_steps", square["stairs"])


if __name__ == "__main__":
    unittest.main()
