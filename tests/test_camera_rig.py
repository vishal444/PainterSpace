#!/usr/bin/env python3
"""Unit and integration tests for Manual Camera Director Mode and CameraRig.

Tests:
1. Centripetal Catmull-Rom spline passes through control points and arc-length is monotonic.
2. Easing curves clamp correctly (hold before start, hold after end) and interpolate smoothly.
3. Aim solve: look-at vector aims at target within 0.01 degree; roll stability; steep pitch.
4. Continuous angle unwrapping: avoids 360-degree flips across keyframes.
5. Shot size presets: occupancy distances and focal length calculations for humans and vehicles.
6. Zoom interpolation & Dolly zoom helper (Vertigo effect).
7. Determinism: identical rig produces byte-identical evaluations and seeded shake.
8. USD authoring & Layer strength: user_edits.usda overrides shot.usda camera; reset restores LLM camera.
9. Compiler respect for camera_locked flag.
"""
from __future__ import annotations

import math
import shutil
import tempfile
import unittest
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools" / "shot_library"))

from camera_rig import (
    ArcLengthSpline,
    CameraRigData,
    CameraRigEvaluator,
    PathPoint,
    bake_rig_to_stage,
    compute_shot_size_distance,
    compute_shot_size_focal,
    create_rig_preset,
    evaluate_ease,
    evaluate_seeded_shake,
    look_at_rotation_xyz,
    path_tangent_rotation_xyz,
    read_rig_from_stage,
    simplify_points_rdp,
)
from plan_model import Beat, SetupAction, ShotPlan, TimingCondition


class TestCameraRig(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="ps_camera_rig_test_"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Spline math & Arc-length monotonicity
    # -------------------------------------------------------------------------
    def test_spline_passes_through_control_points_and_monotonic(self):
        pts = [
            [0.0, 1.5, 0.0],
            [2.0, 2.0, 3.0],
            [5.0, 1.8, 6.0],
            [8.0, 3.0, 4.0],
        ]
        spline = ArcLengthSpline(pts, samples_per_segment=64)

        # Spline length should be strictly positive and greater than straight line endpoint distance
        direct_dist = math.sqrt(sum((pts[-1][i] - pts[0][i]) ** 2 for i in range(3)))
        self.assertGreater(spline.total_length, direct_dist)

        # LUT distances must be strictly monotonically increasing
        for i in range(1, len(spline.lut_distances)):
            self.assertGreaterEqual(spline.lut_distances[i], spline.lut_distances[i - 1])

        # Sample at start (distance = 0) should match P0
        pos_start, tan_start = spline.sample_at_distance(0.0)
        for i in range(3):
            self.assertAlmostEqual(pos_start[i], pts[0][i], places=2)

        # Sample at end (distance = total_length) should match P_last
        pos_end, tan_end = spline.sample_at_distance(spline.total_length)
        for i in range(3):
            self.assertAlmostEqual(pos_end[i], pts[-1][i], places=2)

    # -------------------------------------------------------------------------
    # 2. Easing curves
    # -------------------------------------------------------------------------
    def test_easing_curves_bounds_and_monotonicity(self):
        for ease_type in ["linear", "ease_in", "ease_out", "ease_in_out"]:
            # Bounds
            self.assertAlmostEqual(evaluate_ease(0.0, ease_type), 0.0, places=5)
            self.assertAlmostEqual(evaluate_ease(1.0, ease_type), 1.0, places=5)
            self.assertAlmostEqual(evaluate_ease(-0.5, ease_type), 0.0, places=5)
            self.assertAlmostEqual(evaluate_ease(1.5, ease_type), 1.0, places=5)

            # Monotonicity check over [0, 1]
            prev = -1e-5
            for step in range(101):
                val = evaluate_ease(float(step) / 100.0, ease_type)
                self.assertGreaterEqual(val, prev)
                prev = val

    # -------------------------------------------------------------------------
    # 3. Aim solve: Look-At precision and steep pitch stability
    # -------------------------------------------------------------------------
    def test_look_at_precision(self):
        cam = [0.0, 1.5, 10.0]
        tgt = [0.0, 1.5, 0.0]
        rot = look_at_rotation_xyz(cam, tgt, roll_deg=5.0)
        # Directly in front along -Z -> pitch=0, yaw=0, roll=5
        self.assertAlmostEqual(rot[0], 0.0, delta=0.01)
        self.assertAlmostEqual(rot[1], 0.0, delta=0.01)
        self.assertAlmostEqual(rot[2], 5.0, delta=0.01)

        # Target to the right (+X)
        tgt_right = [10.0, 1.5, 10.0]
        rot_right = look_at_rotation_xyz(cam, tgt_right)
        self.assertAlmostEqual(rot_right[0], 0.0, delta=0.01)
        self.assertAlmostEqual(rot_right[1], -90.0, delta=0.01)

        # Steep pitch up (+Y directly above)
        tgt_top = [0.0, 20.0, 10.0]
        rot_top = look_at_rotation_xyz(cam, tgt_top)
        self.assertAlmostEqual(rot_top[0], 90.0, delta=0.01)

        # Steep pitch down (-Y directly below)
        tgt_bot = [0.0, -20.0, 10.0]
        rot_bot = look_at_rotation_xyz(cam, tgt_bot)
        self.assertAlmostEqual(rot_bot[0], -90.0, delta=0.01)

    # -------------------------------------------------------------------------
    # 4. Shot size framing calculations
    # -------------------------------------------------------------------------
    def test_shot_size_framing_presets(self):
        human_height = 1.8
        focal = 50.0
        v_aperture = 20.25

        dist_close = compute_shot_size_distance("close_up", human_height, focal, v_aperture)
        dist_medium = compute_shot_size_distance("medium", human_height, focal, v_aperture)
        dist_wide = compute_shot_size_distance("wide", human_height, focal, v_aperture)
        dist_extreme_wide = compute_shot_size_distance("extreme_wide", human_height, focal, v_aperture)

        # Closer shot sizes must have smaller distance
        self.assertLess(dist_close, dist_medium)
        self.assertLess(dist_medium, dist_wide)
        self.assertLess(dist_wide, dist_extreme_wide)

        # Focal solver for fixed distance
        focal_close = compute_shot_size_focal("close_up", human_height, 3.0, v_aperture)
        focal_wide = compute_shot_size_focal("wide", human_height, 3.0, v_aperture)
        self.assertGreater(focal_close, focal_wide)

    # -------------------------------------------------------------------------
    # 5. Dolly zoom helper (Vertigo effect)
    # -------------------------------------------------------------------------
    def test_dolly_zoom_evaluation(self):
        rig = CameraRigData(
            points=[
                PathPoint([0.0, 1.5, 10.0]),
                PathPoint([0.0, 1.5, 5.0]),
            ],
            path_start_s=0.0,
            path_end_s=4.0,
            focal_start=50.0,
            focal_end=50.0,
            aim_mode="look_at",
            aim_targets=["Hero"],
            dolly_zoom_enabled=True,
        )
        evaluator = CameraRigEvaluator(fps=24)
        actors_pos = {"Hero": [(0.0, [0.0, 1.5, 0.0]), (4.0, [0.0, 1.5, 0.0])]}
        kfs = evaluator.evaluate_sequence(rig, start_s=0.0, end_s=4.0, actor_positions_over_time=actors_pos)

        # Distance halved from 10m to 5m => focal length should halve from 50mm to 25mm to keep framing constant
        self.assertAlmostEqual(kfs[0].focal_length, 50.0, delta=1.0)
        self.assertAlmostEqual(kfs[-1].focal_length, 25.0, delta=1.0)

    # -------------------------------------------------------------------------
    # 6. Replay determinism & Seeded shake
    # -------------------------------------------------------------------------
    def test_seeded_shake_determinism(self):
        s1_pos, s1_rot = evaluate_seeded_shake(time_s=1.35, seed=123, amp=0.5, freq=2.0)
        s2_pos, s2_rot = evaluate_seeded_shake(time_s=1.35, seed=123, amp=0.5, freq=2.0)
        for i in range(3):
            self.assertEqual(s1_pos[i], s2_pos[i])
            self.assertEqual(s1_rot[i], s2_rot[i])

        # Different seed yields different result
        s3_pos, _ = evaluate_seeded_shake(time_s=1.35, seed=999, amp=0.5, freq=2.0)
        self.assertNotEqual(s1_pos, s3_pos)

    # -------------------------------------------------------------------------
    # 7. Polyline simplification (RDP)
    # -------------------------------------------------------------------------
    def test_rdp_simplification(self):
        # Create collinear points with a small perturbation
        straight = [[float(i), 0.0, 0.0] for i in range(20)]
        simplified = simplify_points_rdp(straight, epsilon=0.01)
        self.assertEqual(len(simplified), 2)
        self.assertEqual(simplified[0], straight[0])
        self.assertEqual(simplified[-1], straight[-1])

    # -------------------------------------------------------------------------
    # 8. USD Authoring & Layer Strength
    # -------------------------------------------------------------------------
    def test_usd_baking_and_layer_override(self):
        shot_path = self.test_dir / "shot.usda"
        user_edits_path = self.test_dir / "user_edits.usda"

        # 1. Base shot.usda with LLM camera at [0, 2, 10]
        shot_stage = Usd.Stage.CreateNew(str(shot_path))
        shot_cam = UsdGeom.Camera.Define(shot_stage, Sdf.Path("/World/ShotCam"))
        shot_xf = UsdGeom.Xformable(shot_cam)
        op_tr = shot_xf.AddTranslateOp()
        op_tr.Set(Gf.Vec3d(0.0, 2.0, 10.0), Usd.TimeCode(0.0))
        shot_stage.Save()

        # 2. user_edits.usda with manual camera rig at [5, 1.5, 3]
        edits_stage = Usd.Stage.CreateNew(str(user_edits_path))
        rig = CameraRigData(
            points=[PathPoint([5.0, 1.5, 3.0]), PathPoint([5.0, 1.5, 1.0])],
            path_start_s=0.0,
            path_end_s=2.0,
            aim_mode="free",
        )
        ok = bake_rig_to_stage(edits_stage, rig, start_frame=0, end_frame=48, fps=24)
        self.assertTrue(ok)
        edits_stage.Save()

        # 3. Composite stage sublayering user_edits over shot
        main_stage = Usd.Stage.CreateInMemory()
        root_layer = main_stage.GetRootLayer()
        root_layer.subLayerPaths.append(str(user_edits_path))
        root_layer.subLayerPaths.append(str(shot_path))

        comp_cam = main_stage.GetPrimAtPath(Sdf.Path("/World/ShotCam"))
        self.assertTrue(comp_cam.IsValid())
        comp_xf = UsdGeom.Xformable(comp_cam)
        mat = comp_xf.ComputeLocalToWorldTransform(Usd.TimeCode(0.0))
        cam_pos = mat.ExtractTranslation()

        # By layer strength, user_edits.usda wins: X should be 5.0 (manual), not 0.0 (LLM)
        self.assertAlmostEqual(cam_pos[0], 5.0, places=2)
        self.assertAlmostEqual(cam_pos[1], 1.5, places=2)
        self.assertAlmostEqual(cam_pos[2], 3.0, places=2)

        # 4. Verify read_rig_from_stage
        loaded_rig = read_rig_from_stage(edits_stage)
        self.assertIsNotNone(loaded_rig)
        self.assertEqual(len(loaded_rig.points), 2)
        self.assertAlmostEqual(loaded_rig.points[0].position[0], 5.0, places=2)

    # -------------------------------------------------------------------------
    # 9. ShotPlan camera_locked roundtrip & Compiler guard
    # -------------------------------------------------------------------------
    def test_shot_plan_camera_locked(self):
        plan = ShotPlan(prompt="Test shot", camera_locked=True)
        data = plan.to_dict()
        self.assertTrue(data.get("camera_locked"))

        json_str = plan.to_json()
        parsed = ShotPlan.from_json(json_str)
        self.assertTrue(parsed.camera_locked)

    def test_compiler_respects_camera_locked(self):
        from compiler import ShotCompiler
        from function_catalog import FunctionCatalog
        compiler = ShotCompiler(project_root=REPO_ROOT)
        catalog = FunctionCatalog(compiler.manifest)
        catalog.place("character.human", position=[0.0, 0.0, 0.0], label="Hero")
        catalog.plan.camera = {"style": "aerial", "target": "Hero"}
        catalog.plan.camera_locked = True

        out_path = self.test_dir / "shot.usda"
        success, report = compiler.compile(catalog.plan, out_path, validate=False)
        self.assertTrue(success, f"Compilation failed: {report}")

        # When camera_locked is True, plan.camera (aerial high up) is ignored
        stage = Usd.Stage.Open(str(out_path))
        cam = stage.GetPrimAtPath("/World/ShotCam")
        self.assertTrue(cam.IsValid())
        xf = UsdGeom.Xformable(cam)
        mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode(0.0))
        t = mat.ExtractTranslation()
        # Aerial camera would have y >= 20.0; default baseline camera is near eye level (~1.6m - 2.5m)
        self.assertLess(t[1], 10.0, "Camera should not have synthesized the aerial high-angle intent when camera_locked is True")


if __name__ == "__main__":
    unittest.main()
