"""Actor identity regression: editor session-layer characters must be reused."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from compiler import ShotCompiler
from combat_moves import resolve_attack_moves
from actor_registry import ActorRegistry
from pxr import Gf, Sdf, Usd, UsdGeom


class ExistingShotActorsTests(unittest.TestCase):
    def test_static_editor_placement_does_not_mask_compiled_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.usda"
            shot = Usd.Stage.CreateNew(str(path))
            actor = UsdGeom.Xform.Define(shot, "/World/MaleBryan")
            actor.GetPrim().SetCustomDataByKey("asset_id", "character.male")
            translate = actor.AddTranslateOp()
            translate.Set(Gf.Vec3d(9, 0, 6), 0)
            translate.Set(Gf.Vec3d(9, 0, -6), 24)
            actor.AddRotateYOp().Set(90, 0)
            shot.GetRootLayer().Save()

            edits = Usd.Stage.CreateNew(str(path.parent / "user_edits.usda"))
            placed = UsdGeom.Xform.Define(edits, "/World/MaleBryan")
            placed.AddTranslateOp().Set(Gf.Vec3d(9, 0, 6))
            placed.AddRotateXYZOp().Set(Gf.Vec3f(0, 0, 0))
            edits.GetRootLayer().Save()

            root = Sdf.Layer.FindOrOpen(str(path))
            layer = Sdf.Layer.FindOrOpen(str(path.parent / "user_edits.usda"))
            composed = Usd.Stage.Open(root, layer)
            self.assertEqual(composed.GetPrimAtPath("/World/MaleBryan").GetAttribute("xformOp:translate").Get(24), Gf.Vec3d(9, 0, 6))

            ShotCompiler._release_animated_actor_overrides(path)
            layer.Reload()
            composed = Usd.Stage.Open(root, layer)
            actor = composed.GetPrimAtPath("/World/MaleBryan")
            self.assertEqual(actor.GetAttribute("xformOp:translate").Get(24), Gf.Vec3d(9, 0, -6))
            self.assertEqual(list(actor.GetAttribute("xformOpOrder").Get()), ["xformOp:translate", "xformOp:rotateY"])
            self.assertEqual(ShotCompiler._read_existing_actors(path, {"MaleBryan"}, {})["MaleBryan"]["position"], [9, 0, 6])

    def test_editor_characters_win_over_generated_duplicates_without_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.usda"
            root = Usd.Stage.CreateNew(str(path))
            # This is the stale duplicate produced by the previous fallback.
            duplicate = UsdGeom.Xform.Define(root, "/World/Human_MaleBryan").GetPrim()
            duplicate.SetCustomDataByKey("actor_id", "MaleBryan")
            duplicate.SetCustomDataByKey("asset_id", "character.female")
            root.GetRootLayer().Save()
            edits = Usd.Stage.CreateNew(str(path.parent / "user_edits.usda"))
            for name, asset, pos in (
                ("MaleBryan", "character.male", (9, 0, 6)),
                ("FemaleSofie", "character.female", (10, 0, -14)),
            ):
                actor = UsdGeom.Xform.Define(edits, "/World/" + name)
                actor.GetPrim().SetCustomDataByKey("asset_id", asset)
                actor.AddTranslateOp().Set(Gf.Vec3d(*pos))
                actor.AddRotateXYZOp().Set(Gf.Vec3f(0, 90, 0))
            edits.GetRootLayer().Save()
            found = ShotCompiler._read_existing_actors(path, {"MaleBryan", "FemaleSofie", "missing"}, {})
            self.assertEqual(set(found), {"MaleBryan", "FemaleSofie"})
            self.assertEqual(found["MaleBryan"]["asset_id"], "character.male")
            self.assertEqual(found["MaleBryan"]["position"], [9, 0, 6])
            self.assertAlmostEqual(found["MaleBryan"]["facing_deg"], 270)
            registry = ActorRegistry()
            for name, state in found.items():
                record = registry.register(asset_id=state["asset_id"], actor_id=name,
                                           label=state["label"], prim_path=state["prim_path"],
                                           appearance=state["appearance"])
                self.assertEqual(record.prim_path, "/World/" + name)
                self.assertEqual(record.label, name)

    def test_track_path_resolves_existing_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shot.usda"
            stage = Usd.Stage.CreateNew(str(path))
            prim = UsdGeom.Xform.Define(stage, "/World/Bryan").GetPrim()
            prim.SetCustomDataByKey("asset_id", "character.male")
            stage.GetRootLayer().Save()
            found = ShotCompiler._read_existing_actors(path, {"man"}, {"man": {"actor_path": "/World/Bryan"}})
            self.assertEqual(found["man"]["label"], "Bryan")

    def test_attack_aliases_and_unknown_clip_rejection(self):
        clips = {"punching": {}, "boxing_block": {}, "combo_punch": {}, "left_hook": {}}
        self.assertEqual(resolve_attack_moves(["punch", "block", "counterattack"], clips),
                         ["punching", "boxing_block", "combo_punch"])
        self.assertEqual(resolve_attack_moves(["left_hook"], clips), ["left_hook"])
        with self.assertRaisesRegex(ValueError, "Unknown attack clips: invented_move"):
            resolve_attack_moves(["invented_move"], clips)


if __name__ == "__main__":
    unittest.main()
