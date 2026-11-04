"""Collision validation must fail when a moving solid intersects another solid or lacks coverage."""

import json
import sys
from pathlib import Path

from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "collision"))

from colliders import AssetDB
from validate_collisions import validate, validate_parallel


def stage_with_cubes(path, moving_positions, obstacle_position):
    stage = Usd.Stage.CreateNew(str(path))
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(1)
    moving = UsdGeom.Cube.Define(stage, "/World/Moving")
    moving.CreateSizeAttr(1.0)
    move_op = moving.AddTranslateOp()
    for frame, position in enumerate(moving_positions):
        move_op.Set(Gf.Vec3d(*position), frame)
    obstacle = UsdGeom.Cube.Define(stage, "/World/Obstacle")
    obstacle.CreateSizeAttr(1.0)
    obstacle.AddTranslateOp().Set(Gf.Vec3d(*obstacle_position))
    stage.GetRootLayer().Save()
    return stage


def test_moving_solid_collision_is_detected_without_vehicle_name(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": []}))
    db = AssetDB(str(manifest))

    clear = stage_with_cubes(tmp_path / "clear.usda", [(-3, 1, 0), (-2, 1, 0)], (0, 1, 0))
    crossing = stage_with_cubes(tmp_path / "crossing.usda", [(-3, 1, 0), (0, 1, 0)], (0, 1, 0))

    assert validate(clear, db)["ok"]
    report = validate(crossing, db)
    assert not report["ok"]
    assert any(v["actor"] == "Moving" and v["instance"] == "Obstacle" for v in report["violations"])


def test_two_moving_solid_assets_are_checked_against_each_other(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": []}))
    db = AssetDB(str(manifest))
    stage = Usd.Stage.CreateNew(str(tmp_path / "moving_pair.usda"))
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(2)
    for name, start, end in (("A", -2., 2.), ("B", 2., -2.)):
        cube = UsdGeom.Cube.Define(stage, f"/World/{name}")
        cube.CreateSizeAttr(1.)
        op = cube.AddTranslateOp()
        op.Set(Gf.Vec3d(start, 1., 0.), 0)
        op.Set(Gf.Vec3d(end, 1., 0.), 2)
    stage.GetRootLayer().Save()
    report = validate(stage, db, substeps=4)
    assert not report["ok"]
    assert any({v["actor"], v["instance"]} == {"A", "B"}
               for v in report["violations"])


def test_unprotected_solid_cannot_pass_validation(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": [{"id": "props.block", "tags": ["prop"]}]}))
    db = AssetDB(str(manifest))
    stage = stage_with_cubes(tmp_path / "unprotected.usda", [(-3, 1, 0), (-2, 1, 0)], (0, 1, 0))
    stage.GetPrimAtPath("/World/Obstacle").SetCustomDataByKey("asset_id", "props.block")

    report = validate(stage, db)
    assert not report["ok"]
    assert report["unprotected_assets"] == ["Obstacle"]


def test_actor_without_usable_skeleton_cannot_pass_validation(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": [{"id": "character.unknown", "tags": ["character"]}]}))
    db = AssetDB(str(manifest))
    stage = stage_with_cubes(tmp_path / "actor.usda", [(-3, 1, 0), (-2, 1, 0)], (0, 1, 0))
    actor = UsdGeom.Xform.Define(stage, "/World/Actor").GetPrim()
    actor.SetCustomDataByKey("asset_id", "character.unknown")

    report = validate(stage, db)
    assert not report["ok"]
    assert report["unprotected_actors"] == ["Actor"]


def test_parallel_validation_matches_serial_for_clear_and_colliding_shots(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": []}))
    db = AssetDB(str(manifest))
    for collides, endpoint in ((False, (-2, 1, 0)), (True, (0, 1, 0))):
        path = tmp_path / ("collision.usda" if collides else "clear.usda")
        stage = stage_with_cubes(path, [(-3, 1, 0), endpoint], (0, 1, 0))
        stage.SetEndTimeCode(120)
        stage.GetRootLayer().Save()
        serial = validate(stage, db, substeps=2)
        parallel = validate_parallel(stage, db, str(path), str(manifest),
                                     substeps=2, workers=2)
        assert parallel == serial
