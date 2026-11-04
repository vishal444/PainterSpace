"""Measured flat scene geometry is usable without building-specific metadata."""
import sys
import json
import pytest
from pathlib import Path

from pxr import Gf, Usd, UsdGeom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "shot_library"))

from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from process_agent_queue import _anchor_support_references
from semantic_references import ReferenceResolver
from plan_model import ShotPlan
from support_surfaces import derive_surface_spots, supports_route


@pytest.mark.parametrize('height,heading', [(0., 0.), (1.5, 90.)])
def test_landscape_mesh_floor_precedes_decorative_ledge_markers(tmp_path, height, heading):
    scene_path = tmp_path / 'landscape.usda'
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, '/World')
    landscape = UsdGeom.Xform.Define(stage, '/World/Landscape')
    landscape.AddTranslateOp().Set(Gf.Vec3d(0, height, 0))
    landscape.AddRotateYOp().Set(heading)
    mesh = UsdGeom.Mesh.Define(stage, '/World/Landscape/Ground')
    mesh.CreatePointsAttr([Gf.Vec3f(-8, 0, -6), Gf.Vec3f(8, 0, -6),
                           Gf.Vec3f(8, 0, 6), Gf.Vec3f(-8, 0, 6)])
    mesh.CreateFaceVertexCountsAttr([3, 3])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 0, 2, 1])
    ledge = UsdGeom.Cube.Define(stage, '/World/Landscape/Ledge')
    ledge.AddTranslateOp().Set(Gf.Vec3d(5, 2, 0))
    ledge.AddScaleOp().Set(Gf.Vec3f(1, .05, 4))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    assert not scene.get('read_error')
    spots = [spot for spot in scene['standing_spots'] if spot['owner'] == 'Landscape']
    assert len(spots) > 12
    assert all(abs(spot['position'][1] - height) < 1e-6 for spot in spots)
    floor_faces = [face for face in scene['support_surfaces']
                   if face['prim_path'].endswith('/Ground')]
    assert len(floor_faces) == 2
    assert supports_route(floor_faces, [[0, height, -3], [0, height, 3]])


def test_compiler_saves_floor_generated_initial_positions(tmp_path, monkeypatch):
    scene_path = tmp_path / 'scene.usda'
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, '/World')
    UsdGeom.Xform.Define(stage, '/World/Platform')
    slab = UsdGeom.Cube.Define(stage, '/World/Platform/Slab')
    slab.AddTranslateOp().Set(Gf.Vec3d(0, -.1, 0))
    slab.AddScaleOp().Set(Gf.Vec3f(10, .1, 6))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    read_scene = compiler._read_scene_context

    def sparse_markers(*args, **kwargs):
        scene = read_scene(*args, **kwargs)
        # The reusable marker is too close to the edge for the character.
        scene['standing_spots'] = [{'owner': 'Platform', 'name': 'standing_left_01',
                                    'position': [-9.95, 0, 0]}]
        return scene

    monkeypatch.setattr(compiler, '_read_scene_context', sparse_markers)
    catalog = FunctionCatalog(compiler.manifest)
    for _ in range(2):
        ok, actor = catalog.place('character.human', at_reference='Platform:standing_left_01')
        assert ok
        ok, _ = catalog.dance(actor['actor_id'], style='groove', duration='short')
        assert ok
    output = tmp_path / 'shot' / 'shot.usda'
    success, report = compiler.compile(catalog.plan, output, scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:3000]
    saved = json.loads((output.parent / 'shot_plan.json').read_text())
    assert all(setup.get('position') is not None and not setup.get('at_reference')
               for setup in saved['setup'])
    assert report['validation']['collision_passed']
    replay_output = tmp_path / 'replay' / 'shot.usda'
    success, report = compiler.compile(ShotPlan.from_dict(saved), replay_output,
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:3000]
    replay = json.loads((replay_output.parent / 'shot_plan.json').read_text())
    assert replay['setup'] == saved['setup']
    assert report['validation']['collision_passed']


def test_adjacent_small_triangles_form_a_standable_surface():
    faces = [
        {"owner": "Platform", "height_m": 2,
         "polygon_xz": [[0, 0], [1.1, 0], [1.1, 1.1]]},
        {"owner": "Platform", "height_m": 2,
         "polygon_xz": [[0, 0], [1.1, 1.1], [0, 1.1]]},
    ]
    spots = derive_surface_spots(faces, [], {"Platform": 0})
    assert spots
    assert spots[0]["name"].startswith("surface_")
    assert supports_route(faces, [[0.4, 2, 0.4], [0.7, 2, 0.7]])
    scene = {"objects": [{"name": "Platform", "asset": "Platform",
                          "position": [0, 0, 0], "heading_deg": 0}],
             "support_surfaces": faces, "surface_spots": spots,
             "unprotected_scene_objects": []}
    classified = _anchor_support_references(ShotCompiler(project_root=ROOT).manifest, scene)
    assert f'Platform:{spots[0]["name"]}' in classified["supported_anchors"]


def test_surface_spots_require_room_and_head_clearance():
    tiny = {"owner": "Platform", "height_m": 2,
            "polygon_xz": [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]]}
    broad = {"owner": "Platform", "height_m": 2,
             "polygon_xz": [[-2, -2], [2, -2], [2, 2], [-2, 2]]}
    overhead = {"owner": "Ceiling", "min": [-2, 2.5, -2], "max": [2, 3, 2]}
    assert derive_surface_spots([tiny], [], {"Platform": 0}) == []
    assert derive_surface_spots([broad], [overhead], {"Platform": 0}) == []


def test_nonbuilding_platform_exposes_resolvable_surface_spot(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Platform")
    slab = UsdGeom.Cube.Define(stage, "/World/Platform/Slab")
    slab.AddTranslateOp().Set(Gf.Vec3d(0, 2, 0))
    slab.AddScaleOp().Set(Gf.Vec3f(2, 0.1, 2))
    stage.GetRootLayer().Save()

    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    assert not scene.get("read_error")
    spots = [spot for spot in scene["surface_spots"] if spot["owner"] == "Platform"]
    assert spots
    reference = f'Platform:{spots[0]["name"]}'
    target, error = ReferenceResolver(compiler.manifest, scene).resolve(reference, {})
    assert not error
    assert target.position[1] > 2
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene)
    catalog.place("character.human", at_reference=reference)
    success, report = compiler.compile(catalog.plan, tmp_path / "shot" / "shot.usda",
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:2500]


def test_nonbuilding_mesh_exposes_flat_surface_spot(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Platform")
    mesh = UsdGeom.Mesh.Define(stage, "/World/Platform/Top")
    mesh.CreatePointsAttr([Gf.Vec3f(-1, 3, -1), Gf.Vec3f(1, 3, -1),
                           Gf.Vec3f(1, 3, 1), Gf.Vec3f(-1, 3, 1)])
    mesh.CreateFaceVertexCountsAttr([3, 3])
    mesh.CreateFaceVertexIndicesAttr([0, 3, 2, 0, 2, 1])
    stage.GetRootLayer().Save()

    scene = ShotCompiler(project_root=ROOT)._read_scene_context(scene_path)
    assert not scene.get("read_error")
    assert any(spot["owner"] == "Platform" and spot["position"][1] == 3
               for spot in scene["surface_spots"])


def test_character_walks_across_adjacent_elevated_platforms(tmp_path):
    scene_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(scene_path))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Platform")
    for name, x in (("Left", -4), ("Right", 4)):
        slab = UsdGeom.Cube.Define(stage, f"/World/Platform/{name}")
        slab.AddTranslateOp().Set(Gf.Vec3d(x, 2, 0))
        slab.AddScaleOp().Set(Gf.Vec3f(4, 0.1, 4))
    stage.GetRootLayer().Save()
    compiler = ShotCompiler(project_root=ROOT)
    scene = compiler._read_scene_context(scene_path)
    spots = [spot for spot in scene["surface_spots"] if spot["owner"] == "Platform"]
    assert len(spots) >= 2
    catalog = FunctionCatalog(compiler.manifest, scene_data=scene)
    catalog.place("character.human", at_reference=f'Platform:{spots[0]["name"]}')
    catalog.walk_to("human_1", target=f'Platform:{spots[1]["name"]}')
    success, report = compiler.compile(catalog.plan, tmp_path / "shot" / "shot.usda",
                                       scene_usda_path=scene_path, validate=True)
    assert success, repr(report)[:2500]
