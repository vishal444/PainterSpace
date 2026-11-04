"""A new placement must survive inactive opinions from older USD layers."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from compiler import ShotCompiler
from actor_registry import ActorRegistry
from camera_controller import CameraKeyframe
from motion_synthesis import ActorPerformance, Keyframe
from timeline_resolver import ResolvedTimeline
from pxr import Sdf, Usd, UsdGeom, UsdSkel


class ReactivatedShotActorsTests(unittest.TestCase):
    def test_generated_actor_overrides_inactive_scene_opinions(self):
        with tempfile.TemporaryDirectory() as directory:
            compiler = ShotCompiler(project_root=Path(directory))
            registry = ActorRegistry()
            actor = registry.register("character.human", actor_id="human_1", label="New Person")
            performance = ActorPerformance(actor_id=actor.actor_id)
            performance.keyframes.append(Keyframe(0, [0, 0, 0], 0))
            camera = CameraKeyframe(0, [0, 2, 5], [0, 0, 0])

            text = compiler._generate_shot_usda(
                ResolvedTimeline(total_duration_s=1), registry,
                {actor.actor_id: performance}, [camera],
            )
            layer = Sdf.Layer.CreateAnonymous("shot.usda")
            self.assertTrue(layer.ImportFromString(text))
            spec = layer.GetPrimAtPath("/World/New_Person")
            self.assertTrue(spec.active)
            self.assertEqual(spec.properties["visibility"].default, "inherited")

    def test_new_placement_reactivates_scene_actor_and_clears_session_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            asset_path = root_dir / "character.usda"
            asset = Usd.Stage.CreateNew(str(asset_path))
            UsdGeom.Xform.Define(asset, "/root")
            UsdSkel.Skeleton.Define(asset, "/root/Armature/Skeleton")
            asset.GetRootLayer().Save()

            scene_path = root_dir / "scene.usda"
            scene = Usd.Stage.CreateNew(str(scene_path))
            old_actor = UsdGeom.Xform.Define(scene, "/World/Man")
            old_actor.GetPrim().SetActive(False)
            old_actor.GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
            scene.GetRootLayer().Save()

            shot_path = root_dir / "shot.usda"
            shot = Usd.Stage.CreateNew(str(shot_path))
            shot.GetRootLayer().subLayerPaths.append("scene.usda")
            placed = UsdGeom.Xform.Define(shot, "/World/Man")
            placed.GetPrim().SetActive(True)
            placed.GetPrim().SetCustomDataByKey("actor_id", "human_1")
            placed.GetPrim().SetCustomDataByKey("asset_id", "character.human")
            placed.GetPrim().GetReferences().AddReference("character.usda", "/root")
            placed.GetVisibilityAttr().Set(UsdGeom.Tokens.inherited)
            shot.GetRootLayer().Save()

            edits_path = root_dir / "user_edits.usda"
            edits = Usd.Stage.CreateNew(str(edits_path))
            deleted = UsdGeom.Xform.Define(edits, "/World/Man")
            deleted.GetPrim().SetActive(False)
            deleted.GetVisibilityAttr().Set(UsdGeom.Tokens.invisible)
            unrelated = UsdGeom.Xform.Define(edits, "/World/Other")
            unrelated.GetPrim().SetActive(False)
            edits.GetRootLayer().Save()

            self.assertEqual(ShotCompiler._read_existing_actors(shot_path, {"human_1"}, {}), {})
            ShotCompiler._restore_placed_actor_visibility(shot_path, {"/World/Man"})

            root_layer = Sdf.Layer.FindOrOpen(str(shot_path))
            edits_layer = Sdf.Layer.FindOrOpen(str(edits_path))
            composed = Usd.Stage.Open(root_layer, edits_layer)
            actor = composed.GetPrimAtPath("/World/Man")
            self.assertTrue(actor.IsActive())
            self.assertEqual(UsdGeom.Imageable(actor).ComputeVisibility(), UsdGeom.Tokens.inherited)
            self.assertEqual(sum(prim.IsA(UsdSkel.Skeleton) for prim in Usd.PrimRange(actor)), 1)
            self.assertFalse(composed.GetPrimAtPath("/World/Other").IsActive())
            self.assertEqual(ShotCompiler._read_existing_actors(shot_path, {"human_1"}, {})["human_1"]["label"], "Man")

    def test_active_existing_actor_keeps_its_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            shot_path = Path(directory) / "shot.usda"
            shot = Usd.Stage.CreateNew(str(shot_path))
            actor = UsdGeom.Xform.Define(shot, "/World/ExistingName")
            actor.GetPrim().SetCustomDataByKey("actor_id", "human_1")
            actor.GetPrim().SetCustomDataByKey("asset_id", "character.human")
            shot.GetRootLayer().Save()

            found = ShotCompiler._read_existing_actors(shot_path, {"human_1"}, {})
            self.assertEqual(found["human_1"]["label"], "ExistingName")


if __name__ == "__main__":
    unittest.main()
