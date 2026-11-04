"""Rooftop references come from measured faces and long falls use drop distance."""
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))
sys.path.insert(0, str(ROOT / "tools" / "collision"))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from motion_synthesis import MotionSynthesizer
from process_agent_queue import _anchor_support_references
from validate_collisions import check_level_integrity
from semantic_references import ReferenceResolver
from timeline_resolver import JUMP_TAKEOFF_S, TimelineResolver


def test_scene_reader_exposes_mesh_roof_and_resolves_reference(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    building = UsdGeom.Xform.Define(stage, "/World/Building")
    building.GetPrim().SetCustomDataByKey("asset_id", "environment.downtown_tower")
    mesh = UsdGeom.Mesh.Define(stage, "/World/Building/Roof")
    mesh.CreatePointsAttr([Gf.Vec3f(-4, 12, -4), Gf.Vec3f(4, 12, -4),
                           Gf.Vec3f(4, 12, 4), Gf.Vec3f(-4, 12, 4)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 1])
    stage.GetRootLayer().Save()

    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    assert not scene.get("read_error")
    assert len(scene["rooftop_spots"]) == 1
    target, error = ReferenceResolver(compiler.manifest, scene).resolve("Building:roof_01", {})
    assert not error
    assert list(target.position) == [0, 12, 0]


def test_narrow_building_top_is_not_advertised_as_standable(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    building = UsdGeom.Xform.Define(stage, "/World/Building")
    building.GetPrim().SetCustomDataByKey("asset_id", "environment.downtown_tower")
    mesh = UsdGeom.Mesh.Define(stage, "/World/Building/Top")
    mesh.CreatePointsAttr([Gf.Vec3f(0, 12, 0), Gf.Vec3f(0, 12, 0.2),
                           Gf.Vec3f(0.2, 12, 0.2), Gf.Vec3f(0.2, 12, 0)])
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    stage.GetRootLayer().Save()
    scene = ShotCompiler(project_root=ROOT)._read_scene_context(scene_path)
    assert not scene["rooftop_spots"]


@pytest.mark.parametrize("asset_id", ["environment.downtown_tower", "buildings.grand_night_club"])
def test_building_asset_gets_roof_and_external_ground_landing(tmp_path, asset_id):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    ground = UsdGeom.Cube.Define(stage, "/World/Ground")
    ground.AddScaleOp().Set(Gf.Vec3f(50, 0.05, 50))
    building = UsdGeom.Xform.Define(stage, "/World/Building")
    building.GetPrim().SetCustomDataByKey("asset_id", asset_id)
    asset = next(asset for asset in ShotCompiler(project_root=ROOT).manifest["assets"]
                 if asset["id"] == asset_id)
    building.GetPrim().GetReferences().AddReference(str(ROOT / asset["path"]))
    stage.GetRootLayer().Save()

    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    assert not scene.get("read_error")
    roof = next(spot for spot in scene["rooftop_spots"] if spot["owner"] == "Building")
    assert roof["position"][1] > 2
    assert roof["landing_reference"].startswith("Ground:standing_")
    landing = next(spot for spot in scene["standing_spots"]
                   if f'{spot["owner"]}:{spot["name"]}' == roof["landing_reference"])
    assert landing["position"][1] < roof["position"][1]
    support = _anchor_support_references(compiler.manifest, scene)
    assert "Building:roof_01" in support["supported_anchors"]
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene)
    catalog.place("character.human", at_reference="Building:roof_01")
    catalog.jump_down("human_1", target=roof["landing_reference"])
    success, report = compiler.compile(catalog.plan, tmp_path / "shot" / "shot.usda",
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:6000]


def test_jump_down_timeline_uses_measured_drop():
    scene = {"objects": [{"name": "Building", "asset": "environment.downtown_tower",
                          "position": [0, 0, 0], "heading_deg": 0},
                         {"name": "Ground", "asset": "Ground",
                          "position": [0, 0, 0], "heading_deg": 0}],
             "rooftop_spots": [{"owner": "Building", "name": "roof_01",
                                 "position": [0, 25, 0]}],
             "standing_spots": [{"owner": "Ground", "name": "standing_left_01",
                                  "position": [5, 0, 0]}]}
    catalog = FunctionCatalog(ShotCompiler(project_root=ROOT).manifest, scene_data=scene)
    catalog.place("character.human", at_reference="Building:roof_01")
    catalog.jump_down("human_1", target="Ground:standing_left_01")
    resolver = ReferenceResolver(catalog.manifest, scene)
    timeline, error = TimelineResolver(ref_resolver=resolver).resolve(
        catalog.plan, {"human_1": {"position": [0, 25, 0]}})
    assert not error
    assert timeline.beats[0].duration >= JUMP_TAKEOFF_S + math.sqrt(50 / 9.81)
    landing, error = resolver.resolve("Ground:standing_left_01", {})
    assert not error
    performance, error = MotionSynthesizer(catalog.manifest, ref_resolver=resolver,
                                           scene_data=scene).synthesize_actor(
        "human_1", {"position": [0, 25, 0], "asset_id": "character.human"},
        timeline.actor_schedules["human_1"],
        {timeline.beats[0].beat_id: landing}, {}, timeline.total_duration_s)
    assert not error
    assert [action["clip"] for action in performance.actions[:2]] == [
        "jumping_down", "falling_in_air_idle"]
    assert performance.keyframes[-1].position == [5, 0, 0]


def test_semantic_planner_uses_measured_roof_and_landing():
    scene = {"objects": [{"name": "Building", "asset": "environment.downtown_tower",
                          "position": [0, 0, 0], "heading_deg": 0},
                         {"name": "Ground", "asset": "Ground",
                          "position": [0, 0, 0], "heading_deg": 0}],
             "zones": [],
             "rooftop_spots": [{"owner": "Building", "name": "roof_01",
                                 "position": [0, 12, 0],
                                 "landing_reference": "Ground:standing_left_01"}],
             "standing_spots": [{"owner": "Ground", "name": "standing_left_01",
                                  "position": [5, 0, 0], "clearance_m": 3}]}
    catalog = FunctionCatalog(ShotCompiler(project_root=ROOT).manifest, scene_data=scene)
    planner = LLMShotPlanner(catalog)
    planner._plan_general_semantic("A person leaps from the top of the building down to the ground.")
    assert catalog.plan.setup[0].at_reference == "Building:roof_01"
    jumps = [beat for beat in catalog.plan.beats if beat.function == "jump_down"]
    assert len(jumps) == 1
    assert jumps[0].params["target"] == "Ground:standing_left_01"
    assert jumps[0].duration_condition is None


def test_airborne_allow_interval_does_not_hide_later_teleport(tmp_path):
    stage = Usd.Stage.CreateNew(str(tmp_path / "actor.usda"))
    actor = UsdGeom.Xform.Define(stage, "/Actor")
    translate = actor.AddTranslateOp()
    for frame, height in ((0, 0), (24, 10), (48, 20)):
        translate.Set(Gf.Vec3d(0, height, 0), frame)
    stage.GetRootLayer().Save()
    actors = [SimpleNamespace(name="Actor", prim=actor.GetPrim())]
    first_jump = [{"actor": "Actor", "vertical_transition": True, "t0": 0, "t1": 1}]
    assert check_level_integrity(stage, None, actors, [0, 24, 48],
                                 stairs=[], allow=first_jump)
    both_jumps = first_jump + [{"actor": "Actor", "vertical_transition": True,
                                "t0": 1, "t1": 2}]
    assert check_level_integrity(stage, None, actors, [0, 24, 48],
                                 stairs=[], allow=both_jumps) == []
