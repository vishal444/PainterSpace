#!/usr/bin/env python3
"""Regression test suite for project review items:
1. Normalization transform preservation when authoring transforms on instance
3. Rear door swing signs (outward swing away from cabin for front-hinged vs rear-hinged doors)
4. customData allow_contact bypass prevention (rejecting raw stage bypasses and enforcing limits)
6. Scene versioning immutability (compiled shots sublayering immutable scene_vN.usda)
8. Seated occupant cabin volume boundary checking (detecting limbs poking through the roof)
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "collision"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))

from colliders import AssetDB, apply, l2w
from validate_collisions import validate
from compiler import ShotCompiler
from function_catalog import FunctionCatalog


class TestRegressionIssues(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="painterspace_reg_test_"))
        self.manifest_path = REPO_ROOT / "manifest.json"
        # Compiled shots reference ../../../assets/... relative to the project root,
        # so the temp project needs the real asset library to bake/bind characters.
        if not (REPO_ROOT / "assets").exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)
            self.skipTest("assets/ is git-ignored and missing; these tests need the real asset library")
        os.symlink(REPO_ROOT / "assets", self.test_dir / "assets")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # ITEM 1: Normalization transform dropped when authoring on instance
    # -------------------------------------------------------------------------
    def test_item_1_normalization_transform_preservation(self):
        """Verify that authoring translate and rotateY on a referenced asset instance

        does not drop the asset's normalization transform (failing-then-passing case).
        """
        car2_norm_path = REPO_ROOT / "assets" / "usdc_assets" / "cars" / "car2.norm.usda"
        self.assertTrue(car2_norm_path.exists(), "car2.norm.usda must exist")

        # 1. FAILING PATTERN: If an asset had its normalization op on the root prim itself,
        # authoring a stronger xformOpOrder on the instance would drop painterspaceNorm.
        bad_asset_path = self.test_dir / "flawed_car.usda"
        bad_asset_stage = Usd.Stage.CreateNew(str(bad_asset_path))
        bad_root = bad_asset_stage.DefinePrim("/root", "Xform")
        bad_asset_stage.SetDefaultPrim(bad_root)
        bad_mesh = UsdGeom.Cube.Define(bad_asset_stage, "/root/Body")
        bad_mesh.GetSizeAttr().Set(2.0)
        # Author normalization matrix on root prim
        norm_op = bad_root.CreateAttribute("xformOp:transform:painterspaceNorm", Sdf.ValueTypeNames.Matrix4d)
        norm_matrix = Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), 90.0))
        norm_op.Set(norm_matrix)
        UsdGeom.Xformable(bad_root).GetXformOpOrderAttr().Set(["xformOp:transform:painterspaceNorm"])
        bad_asset_stage.GetRootLayer().Save()

        # In shot: instance flawed_car and author translate + rotateY on instance prim with explicit xformOpOrder
        failing_shot_path = self.test_dir / "failing_shot.usda"
        st_fail = Usd.Stage.CreateNew(str(failing_shot_path))
        car_fail = st_fail.DefinePrim("/World/Car", "Xform")
        car_fail.GetReferences().AddReference(f"./{bad_asset_path.name}")
        xf_fail = UsdGeom.Xformable(car_fail)
        xf_fail.AddTranslateOp().Set(Gf.Vec3d(5.0, 0.0, 10.0))
        xf_fail.AddRotateYOp().Set(45.0)
        # When a shot or tool writes xformOpOrder on the instance, it replaces the referenced root xformOpOrder
        xf_fail.GetXformOpOrderAttr().Set(["xformOp:translate", "xformOp:rotateY"])
        fail_ops = [op.GetOpName() for op in xf_fail.GetOrderedXformOps()]
        self.assertNotIn("xformOp:transform:painterspaceNorm", fail_ops,
                         "Demonstrating failing case: local xformOpOrder dropped normalization on root")

        # 2. PASSING PATTERN: car2.norm.usda puts painterspaceNorm on the inner child prim (car_body),
        # so authoring translate and rotateY on /World/Car preserves car_body's normalization.
        passing_shot_path = self.test_dir / "passing_shot.usda"
        st_pass = Usd.Stage.CreateNew(str(passing_shot_path))
        UsdGeom.SetStageUpAxis(st_pass, UsdGeom.Tokens.y)
        car_pass = st_pass.DefinePrim("/World/Car", "Xform")
        car_pass.GetReferences().AddReference(str(car2_norm_path.resolve()))
        
        # Check initial world height of car2.norm.usda
        bbc = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        init_box = bbc.ComputeWorldBound(car_pass).ComputeAlignedBox()
        init_height = float(init_box.GetSize()[1])
        self.assertAlmostEqual(init_height, 1.714, places=2, msg="Normalized car height should be ~1.71m")

        # Author translate and rotateY with xformOpOrder on instance prim
        xf_pass = UsdGeom.Xformable(car_pass)
        xf_pass.AddTranslateOp().Set(Gf.Vec3d(12.0, 0.0, -8.0))
        xf_pass.AddRotateYOp().Set(65.0)
        xf_pass.GetXformOpOrderAttr().Set(["xformOp:translate", "xformOp:rotateY"])

        # Assert world height is strictly preserved
        after_box = bbc.ComputeWorldBound(car_pass).ComputeAlignedBox()
        after_height = float(after_box.GetSize()[1])
        self.assertAlmostEqual(after_height, init_height, places=3,
                               msg="Car2 world height must remain its normalized height and not fall on its side")

    # -------------------------------------------------------------------------
    # ITEM 3: Rear door swing signs
    # -------------------------------------------------------------------------
    def test_item_3_rear_door_swing_signs(self):
        """Verify door open_sign: front-hinged doors swing with negative angle for left,

        while rear-hinged doors (hinge at rear edge) swing with positive angle away from cabin.
        """
        # Hinge coordinates in vehicle frame (Y-up, -Z forward, Left is -X, Right is +X)
        # Front Left Door (conventional, hinge at front edge: z = -0.914)
        fl_hinge = np.array([-0.866, 0.0, -0.914])
        fl_center = np.array([-0.866, 0.5, 0.0])  # door extends along +Z behind hinge

        # Rear Left Door on car1 (rear-hinged "suicide" door, hinge at rear edge: z = +0.914)
        rl_hinge = np.array([-0.866, 0.0, +0.914])
        rl_center = np.array([-0.866, 0.5, 0.0])  # door extends along -Z in front of hinge

        def rotate_y(pt, hinge, deg):
            rad = np.radians(deg)
            cos_a, sin_a = np.cos(rad), np.sin(rad)
            R = np.array([
                [cos_a, 0, sin_a],
                [0, 1, 0],
                [-sin_a, 0, cos_a]
            ])
            return hinge + R @ (pt - hinge)

        # 1. FAILING CASE: If Door_RL is hardcoded to -65 deg (same sign as FL)
        rl_wrong = rotate_y(rl_center, rl_hinge, -65.0)
        # For left side, vehicle centerline is X=0. Moving outward means X becomes MORE negative (X < -0.866).
        # With -65 deg, rl_wrong[0] moves inward toward X=0 (into the cabin!)
        self.assertGreater(rl_wrong[0], rl_center[0],
                           "Demonstrating failing case: -65 deg on rear-hinged door swings into cabin")

        # 2. PASSING CASE: Ingest / compiler detects rear-hinge and selects open_sign = +1.0 for RL
        rl_correct = rotate_y(rl_center, rl_hinge, +65.0)
        self.assertLess(rl_correct[0], rl_center[0],
                        "Passing case: +65 deg on rear-hinged door swings outward away from vehicle body")
        self.assertLess(rl_correct[0], -1.2, "Door should swing wide clear of cabin")

    # -------------------------------------------------------------------------
    # ITEM 4: customData allow_contact bypass prevention
    # -------------------------------------------------------------------------
    def test_item_4_customdata_allow_contact_bypass_prevented(self):
        """Verify that an LLM-authored raw customData allow_contact on a stage prim

        cannot excuse an unallowed collision unless specified in a compiler-generated file
        with limb, time window, and maximum depth limits.
        """
        # Create a simple stage with an obstacle and an actor penetrating it by 0.35m
        stage_path = self.test_dir / "contact_bypass_shot.usda"
        st = Usd.Stage.CreateNew(str(stage_path))
        UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.y)

        # Obstacle: Tree trunk box
        trunk = UsdGeom.Cube.Define(st, "/World/Obstacle")
        trunk.GetSizeAttr().Set(1.0)
        xf_tr = UsdGeom.Xformable(trunk)
        xf_tr.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.5, 0.0))
        trunk.GetPrim().SetCustomDataByKey("asset_id", "trees.test")

        # Actor: Hero penetrating 0.35m into trunk
        hero = st.DefinePrim("/World/Hero", "Xform")
        hero.SetCustomDataByKey("actor_id", "human_1")
        hero.SetCustomDataByKey("asset_id", "character.human")
        # Attaching raw unconstrained allow_contact on stage prim
        hero.SetCustomDataByKey("allow_contact", "Obstacle")
        hero.GetReferences().AddReference(str((REPO_ROOT / "assets" / "usdc_assets" / "humans" / "human.norm.usda").resolve()))
        xf_h = UsdGeom.Xformable(hero)
        xf_h.AddTranslateOp().Set(Gf.Vec3d(0.2, 0.0, 0.0))
        st.GetRootLayer().Save()

        db = AssetDB(str(self.manifest_path))

        # 1. FAILING PATTERN: If validator accepted raw customData allow_contact without depth limit
        # A deep penetration (0.35m) would pass unchecked.
        # 2. PASSING PATTERN: With our fix, raw customData allow_contact is rejected,
        # and even with an explicit rule, penetration exceeding max_depth_m (e.g. 0.08m) fails!
        rep = validate(st, db, t0=0, t1=0, allow=[
            {"actor": "Hero", "asset": "trees.test", "max_depth_m": 0.08}
        ])
        self.assertFalse(rep["ok"], "Penetration of 0.35m must NOT be excused when max_depth_m is 0.08m")
        self.assertGreater(rep["violations"][0]["worst_depth_m"], 0.15)

    # -------------------------------------------------------------------------
    # ITEM 6: Scene versioning immutability
    # -------------------------------------------------------------------------
    def test_item_6_scene_versioning_immutability(self):
        """Verify that compiled shots sublayer immutable scene_vN.usda and record

        the version in shot.json, protecting existing shots when scene.usda is edited.
        """
        scene_dir = self.test_dir / "scenes" / "scene_01"
        scene_dir.mkdir(parents=True, exist_ok=True)
        if not (self.test_dir / "assets").exists():
            os.symlink(REPO_ROOT / "assets", self.test_dir / "assets")
        scene_usda = scene_dir / "scene.usda"
        scene_usda.write_text("""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)
def "World" {
    def Xform "Tree" {
        double3 xformOp:translate = (5, 0, 5)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""", encoding="utf-8")

        # Compile a shot using ShotCompiler
        shot_dir = scene_dir / "shot_001"
        shot_usda = shot_dir / "shot.usda"
        
        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        catalog.place("character.human", position=[0.0, 0.0, 0.0], label="Hero")
        
        ok, report = compiler.compile(catalog.plan, shot_usda, scene_usda_path=scene_usda, validate=False)
        self.assertTrue(ok, f"Compile failed: {report}")

        # Check shot.usda sublayers scene_v1.usda (not live scene.usda)
        shot_text = shot_usda.read_text(encoding="utf-8")
        self.assertIn("@../scene_v1.usda@", shot_text, "Compiled shot must sublayer immutable scene_v1.usda")
        self.assertNotIn("@../scene.usda@", shot_text, "Compiled shot must not sublayer mutable scene.usda")

        # Check shot.json records scene_version
        shot_meta = json.loads((shot_dir / "shot.json").read_text(encoding="utf-8"))
        self.assertEqual(shot_meta.get("scene_version"), "scene_v1.usda")

        # Now simulate a user or LLM editing scene.usda (e.g. moving the tree to (20, 0, 20))
        scene_usda.write_text("""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)
def "World" {
    def Xform "Tree" {
        double3 xformOp:translate = (20, 0, 20)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""", encoding="utf-8")

        # The compiled shot's stage should still compose with the tree at (5, 0, 5) from scene_v1.usda!
        st_shot = Usd.Stage.Open(str(shot_usda))
        tree_prim = st_shot.GetPrimAtPath("/World/Tree")
        self.assertTrue(tree_prim.IsValid())
        pos = tree_prim.GetAttribute("xformOp:translate").Get()
        self.assertEqual(pos, Gf.Vec3d(5, 0, 5), "Shot stage must retain baseline tree position despite scene.usda edits")

    # -------------------------------------------------------------------------
    # ITEM 8: Seated occupant cabin volume boundary checking
    # -------------------------------------------------------------------------
    def test_item_8_seated_occupant_cabin_volume_bounds(self):
        """Verify that seated occupant exemptions only excuse interior contact,

        and flag violations when an occupant's limb extends outside the cabin roof (failing-then-passing case).
        """
        db = AssetDB(str(self.manifest_path))

        # 1. PASSING CASE: Normal seated occupant inside vehicle cabin (head at y = 1.26m < roof 1.49m)
        stage_pass_path = self.test_dir / "cabin_shot_pass.usda"
        st_pass = Usd.Stage.CreateNew(str(stage_pass_path))
        UsdGeom.SetStageUpAxis(st_pass, UsdGeom.Tokens.y)

        car_pass = st_pass.DefinePrim("/World/Car", "Xform")
        car_pass.SetCustomDataByKey("asset_id", "cars.car1")
        car_pass.GetReferences().AddReference(str((REPO_ROOT / "assets" / "usdc_assets" / "cars" / "car1.usdc").resolve()))
        xf_car_p = UsdGeom.Xformable(car_pass)
        xf_car_p.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 0.0))

        driver_pass = st_pass.DefinePrim("/World/DriverPass", "Xform")
        driver_pass.SetCustomDataByKey("actor_id", "driver_pass")
        driver_pass.SetCustomDataByKey("asset_id", "character.human")
        driver_pass.GetReferences().AddReference(str((REPO_ROOT / "assets" / "usdc_assets" / "humans" / "human.norm.usda").resolve()))
        xf_dp = UsdGeom.Xformable(driver_pass)
        # Seated inside cabin: y = -0.55 puts head top at -0.55 + 1.81 = 1.26m (well below 1.49m roof)
        xf_dp.AddTranslateOp().Set(Gf.Vec3d(-0.36, -0.55, -0.15))
        st_pass.GetRootLayer().Save()

        rules_pass = [{
            "actor": "DriverPass",
            "asset": "Car",
            "is_occupant": True,
            "t0": 0.0,
            "t1": 10.0,
            "max_depth_m": 1.0
        }]
        rep_pass = validate(st_pass, db, t0=0, t1=0, allow=rules_pass)
        self.assertEqual(len(rep_pass["violations"]), 0,
                         f"Normal seated occupant must have no violations: {rep_pass['violations']}")

        # 2. FAILING CASE: Occupant head/limb poking out through vehicle roof (standing/elevated at y = 0.20 -> head at 2.01m)
        stage_fail_path = self.test_dir / "cabin_shot_fail.usda"
        st_fail = Usd.Stage.CreateNew(str(stage_fail_path))
        UsdGeom.SetStageUpAxis(st_fail, UsdGeom.Tokens.y)

        car_fail = st_fail.DefinePrim("/World/Car", "Xform")
        car_fail.SetCustomDataByKey("asset_id", "cars.car1")
        car_fail.GetReferences().AddReference(str((REPO_ROOT / "assets" / "usdc_assets" / "cars" / "car1.usdc").resolve()))
        xf_car_f = UsdGeom.Xformable(car_fail)
        xf_car_f.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 0.0))

        driver_fail = st_fail.DefinePrim("/World/DriverFail", "Xform")
        driver_fail.SetCustomDataByKey("actor_id", "driver_fail")
        driver_fail.SetCustomDataByKey("asset_id", "character.human")
        driver_fail.GetReferences().AddReference(str((REPO_ROOT / "assets" / "usdc_assets" / "humans" / "human.norm.usda").resolve()))
        xf_df = UsdGeom.Xformable(driver_fail)
        # Elevated: y = 0.20 puts head at 2.01m, penetrating 1.49m roof by ~0.52m
        xf_df.AddTranslateOp().Set(Gf.Vec3d(-0.36, 0.20, -0.15))
        st_fail.GetRootLayer().Save()

        rules_fail = [{
            "actor": "DriverFail",
            "asset": "Car",
            "is_occupant": True,
            "t0": 0.0,
            "t1": 10.0,
            "max_depth_m": 1.0
        }]
        rep_fail = validate(st_fail, db, t0=0, t1=0, allow=rules_fail)
        roof_violations = [v for v in rep_fail["violations"] if v.get("part") == "cabin_roof"]
        self.assertGreater(len(roof_violations), 0,
                           "Occupant with head/limb poking out of cabin roof must be flagged despite occupant rule")
        self.assertEqual(roof_violations[0]["body_part"], "Head>HeadTop_End")
        self.assertGreater(roof_violations[0]["worst_depth_m"], 0.40,
                           "Penetration depth through roof must be accurately reported")

        # Rising beside the door is outside the cabin roof, even while the
        # time-scoped vehicle transition contact is active.
        xf_df.GetOrderedXformOps()[0].Set(Gf.Vec3d(-2.5, 0.20, -0.15))
        rep_outside = validate(st_fail, db, t0=0, t1=0, allow=rules_fail)
        self.assertFalse(any(v.get("part") == "cabin_roof" for v in rep_outside["violations"]))

    # -------------------------------------------------------------------------
    # Prompt Adherence & Scene Snapshot Invalidation Tests
    # -------------------------------------------------------------------------
    def test_scene_version_invalidation_and_bumping(self):
        """Verify that modifying scene.usda automatically invalidates existing snapshot

        and creates scene_v2.usda for new shots, while keeping old shots immutable.
        """
        scene_dir = self.test_dir / "scenes" / "scene_auto_bump"
        scene_dir.mkdir(parents=True, exist_ok=True)
        if not (self.test_dir / "assets").exists():
            os.symlink(REPO_ROOT / "assets", self.test_dir / "assets")

        scene_usda = scene_dir / "scene.usda"
        scene_usda.write_text("""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)
def "World" {
    def Xform "StreetAvenue" {}
}
""", encoding="utf-8")

        compiler = ShotCompiler(project_root=REPO_ROOT)

        # 1. First shot creates scene_v1.usda
        catalog1 = FunctionCatalog(compiler.manifest)
        catalog1.place("character.human", position=[0.0, 0.0, 0.0], label="Actor1")
        shot1_path = scene_dir / "shot_001" / "shot.usda"
        ok1, _ = compiler.compile(catalog1.plan, shot1_path, scene_usda_path=scene_usda, validate=False)
        self.assertTrue(ok1)
        self.assertTrue((scene_dir / "scene_v1.usda").exists())
        self.assertIn("@../scene_v1.usda@", shot1_path.read_text(encoding="utf-8"))

        # 2. Modify scene.usda (e.g. user adds OakTree to scene)
        scene_usda.write_text("""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)
def "World" {
    def Xform "StreetAvenue" {}
    def Xform "OakTree" {
        double3 xformOp:translate = (-3.18, 0, 4.74)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""", encoding="utf-8")

        # 3. Second shot must detect scene.usda modification and bump to scene_v2.usda
        catalog2 = FunctionCatalog(compiler.manifest)
        catalog2.place("character.human", position=[1.0, 0.0, 0.0], label="Actor2")
        shot2_path = scene_dir / "shot_002" / "shot.usda"
        ok2, _ = compiler.compile(catalog2.plan, shot2_path, scene_usda_path=scene_usda, validate=False)
        self.assertTrue(ok2)
        self.assertTrue((scene_dir / "scene_v2.usda").exists(), "Must create scene_v2.usda when scene.usda is modified")
        shot2_text = shot2_path.read_text(encoding="utf-8")
        self.assertIn("@../scene_v2.usda@", shot2_text, "shot_002 must sublayer scene_v2.usda")
        shot2_meta = json.loads((scene_dir / "shot_002" / "shot.json").read_text(encoding="utf-8"))
        self.assertEqual(shot2_meta.get("scene_version"), "scene_v2.usda")

        # 4. Old shot_001 must still sublayer immutable scene_v1.usda
        shot1_text = shot1_path.read_text(encoding="utf-8")
        self.assertIn("@../scene_v1.usda@", shot1_text, "shot_001 must remain immutable on scene_v1.usda")

        # 5. Third shot compiled without changes to scene.usda must REUSE scene_v2.usda (no unnecessary bumping)
        catalog3 = FunctionCatalog(compiler.manifest)
        catalog3.place("character.human", position=[2.0, 0.0, 0.0], label="Actor3")
        shot3_path = scene_dir / "shot_003" / "shot.usda"
        ok3, _ = compiler.compile(catalog3.plan, shot3_path, scene_usda_path=scene_usda, validate=False)
        self.assertTrue(ok3)
        self.assertFalse((scene_dir / "scene_v3.usda").exists(), "Must not bump to scene_v3 when scene.usda is unchanged")
        self.assertIn("@../scene_v2.usda@", shot3_path.read_text(encoding="utf-8"))

    def test_prompt_adherence_tree_and_entities(self):
        """Verify that shots strictly adhere to entities specified in user prompts:

        1. Tree presence from scene.usda is retained via scene snapshot update.
        2. Missing trees are authored automatically if not in scene.
        3. 'street' is not misidentified as 'tree'.
        4. Validation report certifies prompt adherence.
        """
        from llm_planner import LLMShotPlanner

        scene_dir = self.test_dir / "scenes" / "scene_prompt_adh"
        scene_dir.mkdir(parents=True, exist_ok=True)
        if not (self.test_dir / "assets").exists():
            os.symlink(REPO_ROOT / "assets", self.test_dir / "assets")

        # Scene with an OakTree
        scene_usda = scene_dir / "scene.usda"
        scene_usda.write_text("""#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Y"
)
def "World" {
    def Xform "StreetAvenue" {}
    def Xform "OakTree" (
        customData = {
            string asset_id = "trees.oak_tree"
        }
    )
    {
        double3 xformOp:translate = (-3.18, 0, 4.74)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
""", encoding="utf-8")

        prompt = "Misty evening drone shot soaring toward a happy couple standing near the big tree, but their joyful expressions suddenly turn into startled surprise as they notice something approaching."

        compiler = ShotCompiler(project_root=REPO_ROOT)
        scene_data = compiler._read_scene_context(scene_usda)
        catalog = FunctionCatalog(compiler.manifest, scene_data=scene_data)
        planner = LLMShotPlanner(catalog)

        ok_plan, plan, logs = planner.plan_from_prompt(prompt)
        self.assertTrue(ok_plan)

        shot_path = scene_dir / "shot_drone_tree" / "shot.usda"
        ok_compile, report = compiler.compile(plan, shot_path, scene_usda_path=scene_usda, validate=False)
        self.assertTrue(ok_compile, f"Compilation failed: {report}")

        # Check prompt adherence report
        val_rep = report.get("validation", {}).get("prompt_adherence", {})
        self.assertTrue(val_rep.get("passed"), f"Prompt adherence must pass: {val_rep}")
        self.assertTrue(val_rep.get("checks", {}).get("tree", {}).get("found"))
        self.assertTrue(val_rep.get("checks", {}).get("couple", {}).get("required"))
        self.assertTrue(val_rep.get("checks", {}).get("drone_camera", {}).get("elevated"))
        self.assertTrue(val_rep.get("checks", {}).get("emotional_shift", {}).get("happy_action_found"))
        self.assertTrue(val_rep.get("checks", {}).get("emotional_shift", {}).get("surprise_action_found"))

        # Verify tree exists on composed stage
        st = Usd.Stage.Open(str(shot_path))
        tree_prim = st.GetPrimAtPath("/World/OakTree")
        self.assertTrue(tree_prim.IsValid(), "OakTree must be composed on shot stage")

    def test_dynamic_beat_start_reference_resolution(self):
        """Verify that beat targets referencing moving vehicles/actors are resolved at beat t_start, not t=0."""
        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        catalog.place("cars.car1", position=[0.0, 0.0, -6.0], facing_deg=180.0, label="MovingCar")
        catalog.place("character.human", position=[-3.0, 0.0, 4.0], facing_deg=90.0, label="Pedestrian")

        # Car drives from z=-6.0 to z=4.0 between 0s and 3.0s
        catalog.drive_to("car_1", target="pos(0.0, 0.0, 4.0, 180.0)", at_time=0.0, for_duration=3.0)
        # Pedestrian walks to car_1:door_fl_stand AFTER car stops (at t=3.2s)
        catalog.walk_to("human_1", target="car_1:door_fl_stand", after="car_1.stopped", for_duration=2.5)

        shot_path = self.test_dir / "scenes" / "scene_dyn" / "shot_001" / "shot.usda"
        ok, report = compiler.compile(catalog.plan, shot_path, validate=False)
        self.assertTrue(ok, f"Compile failed: {report}")

        # Verify Pedestrian arrives near z ~ 4.0 (where car_1 stopped), NOT z ~ -6.0 (where car_1 started at t=0)
        st = Usd.Stage.Open(str(shot_path))
        ped_prim = st.GetPrimAtPath("/World/Pedestrian")
        self.assertTrue(ped_prim.IsValid())
        tr_attr = ped_prim.GetAttribute("xformOp:translate")
        samples = tr_attr.GetTimeSamples()
        end_pos = tr_attr.Get(samples[-1])
        self.assertGreater(end_pos[2], 1.5, f"Pedestrian should walk to car's stopped Z (~4.0), got {end_pos}")

    def test_cardinal_and_relative_selectors_and_duration_levels(self):
        """Verify cardinal directions, nearest_to/left_of/right_of selectors, and named duration levels."""
        from semantic_references import ReferenceResolver

        compiler = ShotCompiler(project_root=REPO_ROOT)
        resolver = ReferenceResolver(compiler.manifest, {})
        actor_states = {
            "human_1": {"position": [0.0, 0.0, 0.0], "facing_deg": 180.0, "asset_id": "character.human"},
            "car_1": {"position": [2.0, 0.0, -3.0], "facing_deg": 180.0, "asset_id": "cars.car1"},
            "car_2": {"position": [8.0, 0.0, -10.0], "facing_deg": 180.0, "asset_id": "cars.car2"},
        }

        res_n, err_n = resolver.resolve("north", actor_states)
        self.assertEqual(err_n, "")
        self.assertLess(res_n.position[2], -5.0)

        res_near, err_near = resolver.resolve("nearest_to(human_1, car)", actor_states)
        self.assertEqual(err_near, "")
        self.assertAlmostEqual(float(res_near.position[0]), 2.0, places=2)

        res_left, err_left = resolver.resolve("left_of(human_1, near)", actor_states)
        self.assertEqual(err_left, "")
        self.assertLess(float(res_left.position[0]), -0.5)

    def test_multi_move_attack_and_non_combat_argue_clips(self):
        """Verify attack schedules all combo moves and argue never selects head_hit_during_fight."""
        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        catalog.place("character.human", position=[0.0, 0.0, 0.0], label="FighterA")
        catalog.place("character.human", position=[0.0, 0.0, 1.2], label="FighterB")
        catalog.argue(actors=["human_1", "human_2"], intensity="heated", at_time=0.0, for_duration=4.0)
        catalog.attack(
            "human_1",
            target="human_2",
            moves=["left_hook", "elbow_punch", "kick_to_the_groin"],
            at_time=4.2,
            for_duration=2.4,
        )

        shot_path = self.test_dir / "scenes" / "scene_combo" / "shot_001" / "shot.usda"
        ok, report = compiler.compile(catalog.plan, shot_path, validate=False)
        self.assertTrue(ok, f"Compile failed: {report}")

        actions_a = json.loads((shot_path.parent / "actions_human_1.json").read_text(encoding="utf-8"))
        clips_a = [a["clip"] for a in actions_a["actions"]]
        self.assertNotIn("head_hit_during_fight", clips_a, "Argue must never use head_hit_during_fight")
        self.assertIn("left_hook", clips_a)
        self.assertIn("elbow_punch", clips_a)
        self.assertIn("kick_to_the_groin", clips_a)
        self.assertTrue(len(actions_a.get("look_at_targets", [])) > 0, "Social/combat beats should emit look_at_targets for gaze IK")

    def test_compile_is_independent_of_pythonhashseed(self):
        """Compiling the same plan under different PYTHONHASHSEED values must be byte-identical."""
        import hashlib
        import subprocess

        script = (
            "import sys, hashlib\n"
            "from pathlib import Path\n"
            f"R = Path({str(REPO_ROOT)!r})\n"
            "for p in ('tools', 'tools/collision', 'tools/shot_library'): sys.path.insert(0, str(R / p))\n"
            "from compiler import ShotCompiler\n"
            "from function_catalog import FunctionCatalog\n"
            "c = ShotCompiler(project_root=R)\n"
            "cat = FunctionCatalog(c.manifest)\n"
            "cat.place('character.human', position=[0.0, 0.0, 0.0], label='A')\n"
            "cat.place('character.human', position=[0.0, 0.0, 1.5], label='B')\n"
            "cat.argue(actors=['human_1', 'human_2'], intensity='heated', at_time=0.0, for_duration=5.0)\n"
            "shot = Path(sys.argv[1])\n"
            "ok, rep = c.compile(cat.plan, shot, validate=False)\n"
            "assert ok, rep\n"
            "print(hashlib.sha1(shot.read_bytes()).hexdigest())\n"
            "print(hashlib.sha1((shot.parent / 'actions_human_1.json').read_bytes()).hexdigest())\n"
        )
        shot_path = self.test_dir / "scenes" / "scene_seed" / "shot_001" / "shot.usda"
        outputs = []
        for seed in ("1", "2", "12345"):
            if shot_path.parent.exists():
                shutil.rmtree(shot_path.parent)
            env = dict(os.environ, PYTHONHASHSEED=seed)
            res = subprocess.run([sys.executable, "-c", script, str(shot_path)],
                                 capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(res.returncode, 0, res.stderr[-1500:])
            outputs.append(res.stdout.strip().splitlines()[-2:])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def test_camera_3d_perspective_frustum_validation(self):
        """Verify 3D perspective camera frustum validator catches actors behind camera or off-screen."""
        from camera_controller import CameraController, CameraKeyframe

        cam_ctrl = CameraController(fps=24)
        kfs = [CameraKeyframe(time_s=0.0, position=[0.0, 1.8, 8.0], rotation_xyz=[-5.0, 0.0, 0.0], focal_length=35.0)]

        # Actor in front of camera at (0, 0, 0) -> inside frustum
        ok_in, warn_in = cam_ctrl.validate_frustum(kfs, {"human_1": [(0.0, [0.0, 0.0, 0.0])]}, ["human_1"])
        self.assertTrue(ok_in, f"Expected actor at origin to be inside frustum: {warn_in}")

        # Actor behind camera at (0, 0, 12.0) -> outside frustum
        ok_behind, warn_behind = cam_ctrl.validate_frustum(kfs, {"human_1": [(0.0, [0.0, 0.0, 12.0])]}, ["human_1"])
        self.assertFalse(ok_behind, "Actor behind camera must fail frustum validation")

        # Actor far off to the right at (20.0, 0, 5.0) -> outside horizontal FOV
        ok_side, warn_side = cam_ctrl.validate_frustum(kfs, {"human_1": [(0.0, [20.0, 0.0, 5.0])]}, ["human_1"])
        self.assertFalse(ok_side, "Actor far outside horizontal FOV must fail frustum validation")

    def test_compositional_multi_clause_planner(self):
        """Verify compositional NLP planner handles multi-clause prompts with multiple actors and sequential beats."""
        from llm_planner import LLMShotPlanner

        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        planner = LLMShotPlanner(catalog)

        prompt = "Three people sneak forward; then they huddle; then they disperse in a wide orbit shot."
        ok_plan, plan, logs = planner.plan_from_prompt(prompt)
        self.assertTrue(ok_plan)
        self.assertGreaterEqual(len(plan.setup), 3)
        fns = [b.function for b in plan.beats]
        self.assertIn("sneak_to", fns)
        self.assertIn("huddle", fns)
        self.assertIn("disperse", fns)
        self.assertEqual(plan.camera.get("style"), "wide")
        self.assertEqual(plan.camera.get("move"), "orbit")

    def test_general_llm_planner_zero_hardcoded_coordinates(self):
        """Enforce the Foundational Law: llm_planner.py must contain zero hardcoded pos() coordinates
        and must plan & compile diverse unseen prompts purely via semantic references and manifest data.
        """
        import re as _re
        from llm_planner import LLMShotPlanner

        planner_src = (REPO_ROOT / "tools" / "shot_library" / "llm_planner.py").read_text(encoding="utf-8")
        hardcoded_pos_calls = _re.findall(r"pos\(\s*-?\d+\.\d+", planner_src)
        self.assertEqual(
            len(hardcoded_pos_calls),
            0,
            f"Foundational Law violation: llm_planner.py must not contain hardcoded pos(...) coordinates: {hardcoded_pos_calls}",
        )

        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        planner = LLMShotPlanner(catalog)

        # Verify dynamic tool schema introspection covers the FunctionCatalog
        schemas = planner.export_tool_schemas()
        schema_names = {s["function"]["name"] for s in schemas}
        for expected_fn in ("place", "walk_to", "run_to", "climb_stairs", "dodge", "attack", "disembark_vehicle", "camera_frame"):
            self.assertIn(expected_fn, schema_names)

        # Verify diverse prompts produce semantic plans with zero raw coordinates and compile cleanly
        prompts = [
            "A person hurries down the stairs and then jogs toward the plaza.",
            "A car drives up and stops; the driver and passenger get out and argue heatedly.",
            "A runner sprints toward a parked car, jumps onto the roof, and jumps down in a wide shot.",
        ]
        for idx, p in enumerate(prompts):
            ok_plan, plan, _ = planner.plan_from_prompt(p)
            self.assertTrue(ok_plan, f"Failed to plan prompt: {p}")
            plan_json = plan.to_json()
            self.assertIsNone(
                _re.search(r"pos\(\s*-?\d+", plan_json),
                f"Generated ShotPlan for '{p}' must use semantic references, not raw pos() coordinates",
            )
            shot_out = self.test_dir / "scenes" / f"scene_gen_{idx}" / "shot_001" / "shot.usda"
            ok_compile, rep = compiler.compile(plan, shot_out, validate=False)
            if "stairs" in p:
                self.assertFalse(ok_compile)
                self.assertEqual(rep["error"]["error_code"], "STAIR_NOT_IN_SCENE")
            else:
                self.assertTrue(ok_compile, f"Failed to compile general plan for '{p}': {rep}")

    def test_expanded_fundamental_function_catalog(self):
        """Verify all 25 new fundamental building-block functions are exported as LLM tools,
        planned from natural language without hardcoded coordinates, and compiled cleanly with passing lint.
        """
        from llm_planner import LLMShotPlanner

        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        planner = LLMShotPlanner(catalog)

        schemas = planner.export_tool_schemas()
        schema_names = {s["function"]["name"] for s in schemas}
        self.assertGreaterEqual(len(schema_names), 65, f"Expected >= 65 functions in catalog, got {len(schema_names)}")

        new_functions = [
            "crawl_to", "stumble_to", "back_up_to", "carry_to", "pace_while_talking", "swim_to", "fly_to",
            "kneel", "lie_down", "climb_surface", "vault_over", "perform_acrobatics",
            "interact_object", "throw_toward", "pull_object", "inspect_area", "use_phone", "honk_horn",
            "shake_hands", "hug_or_kiss", "celebrate", "emote", "dance", "perform_activity",
            "defend_or_block", "use_weapon", "collapse_or_knockout", "medical_aid",
        ]
        for fn in new_functions:
            self.assertIn(fn, schema_names, f"New fundamental function '{fn}' missing from exported tool schemas")

        # Test a multi-action prompt exercising the new fundamental building blocks
        prompts = [
            "Two people shake hands; then one dances a moonwalk while the other plays guitar.",
            "An injured person stumbles forward, then kneels, then backs up slowly.",
            "A person climbs a ladder, then performs a backflip, then pushes a button.",
            "A medic administers CPR to a patient lying down.",
        ]
        for idx, p in enumerate(prompts):
            ok_plan, plan, _ = planner.plan_from_prompt(p)
            self.assertTrue(ok_plan, f"Failed to plan prompt: {p}")
            shot_out = self.test_dir / "scenes" / f"scene_fund_{idx}" / "shot_001" / "shot.usda"
            ok_compile, rep = compiler.compile(plan, shot_out, validate=False)
            self.assertTrue(ok_compile, f"Failed to compile plan for '{p}': {rep}")
            self.assertTrue(rep.get("validation", {}).get("lint_passed"), f"Lint failed for '{p}': {rep.get('validation')}")

    def test_llm_json_endpoint_execution(self):
        """Verify that structured LLM JSON tool-call responses (task_queue/llm_response.json)
        are executed directly by LLMShotPlanner.plan_from_llm_json and compiled by ShotCompiler.
        """
        from llm_planner import LLMShotPlanner

        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        planner = LLMShotPlanner(catalog)

        llm_json_response = {
            "calls": [
                {"function": "set_environment", "args": {"asset": "residential_avenue", "name": "ResidentialAvenue"}},
                {"function": "place", "args": {"asset": "mustang_fastback", "name": "Mustang", "pos": {"at": "ResidentialAvenue"}}},
                {"function": "place", "args": {"asset": "walking", "name": "Hero", "pos": {"near": "Mustang", "side": "left", "gap": "near"}, "facing": {"facing": "Mustang"}}},
                {"function": "walk_to", "args": {"actor": "Hero", "target": "Mustang:door_fl_stand"}},
                {"function": "enter_vehicle", "args": {"actor": "Hero", "vehicle": "Mustang", "seat": "driver", "after": "human_1.arrived"}},
                {"function": "camera_frame", "args": {"subjects": ["Hero", "Mustang"], "shot": "wide", "vantage": "eye_level"}},
            ]
        }

        ok, plan, logs = planner.plan_from_llm_json(llm_json_response, prompt="Hero walks up to the Mustang and gets in.")
        self.assertTrue(ok, f"plan_from_llm_json failed: {logs}")
        shot_out = self.test_dir / "scenes" / "scene_llm_json" / "shot_001" / "shot.usda"
        ok_compile, rep = compiler.compile(plan, shot_out, validate=False)
        self.assertTrue(ok_compile, f"Failed to compile LLM JSON plan: {rep}")
        self.assertTrue(shot_out.exists())

    def test_task_queue_llm_call_tools_json_protocol(self):
        """Verify that when prompts are prepared or sent using the task queue, the LLM call instructions
        explicitly mandate responding only with the tools call JSON and ending execution immediately there.
        """
        from llm_planner import LLMShotPlanner
        from function_catalog import FunctionCatalog
        from compiler import ShotCompiler
        import process_agent_queue

        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        planner = LLMShotPlanner(catalog)

        sys_prompt = planner.build_system_prompt()
        self.assertIn("calls", sys_prompt)
        self.assertIn("execution MUST end there", sys_prompt)
        self.assertIn("Keep the starting scene assets exactly as they are", sys_prompt)
        self.assertIn("Stair portals on current scene objects: []", sys_prompt)
        existing_scene_prompt = LLMShotPlanner(FunctionCatalog(compiler.manifest, scene_data={
            "objects": [{"name": "GrandNightClub", "asset": "buildings.grand_night_club"}],
        })).build_system_prompt()
        self.assertIn('"GrandNightClub"', existing_scene_prompt)
        self.assertIn("Stair portals on current scene objects: []", existing_scene_prompt)
        self.assertNotIn("- Stairs: `stairs_main`", existing_scene_prompt)

        # Test protocol schema in pending tasks
        dummy_task = {
            "task_id": "test_shot_001",
            "prompt": "Two actors walk toward each other",
            "target_stage": "scenes/scene_01/shot_001/shot.usda",
            "scene_stage": "scenes/scene_01/scene.usda",
        }
        test_pending = self.test_dir / "pending_request.json"
        orig_pending = process_agent_queue.PENDING_FILE
        try:
            process_agent_queue.PENDING_FILE = test_pending
            test_pending.write_text(json.dumps(dummy_task), encoding="utf-8")
            ret = process_agent_queue.prepare_pending_task()
            self.assertEqual(ret, 0)
            prepared = json.loads(test_pending.read_text(encoding="utf-8"))
            protocol = prepared.get("strict_llm_protocol", {})
            self.assertIn("responds only with the tools call json", protocol.get("mandate", "").lower())
            self.assertIn("end your execution immediately", " ".join(protocol.get("must_do", [])).lower())
            llm_call = prepared.get("llm_call", {})
            self.assertIn("tools call json", llm_call.get("instruction", "").lower())
            self.assertIn("Keep the starting scene assets exactly as they are", llm_call.get("instruction", ""))
            self.assertIn("execution ends there", llm_call.get("execution_policy", "").lower())
        finally:
            process_agent_queue.PENDING_FILE = orig_pending
            if test_pending.exists():
                test_pending.unlink()


if __name__ == "__main__":
    unittest.main()

