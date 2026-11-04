"""Limb contact is permitted between actors; body overlap and solids still block."""
import json
import sys
from pathlib import Path

import pytest
from pxr import Gf, Usd, UsdGeom, UsdSkel, Vt

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools' / 'collision'), str(ROOT / 'tools' / 'shot_library')]

from colliders import AssetDB, is_limb_segment
from validate_collisions import validate, validate_parallel, main
from crowd.config import CrowdSolverConfig
from crowd_spacing import actor_envelopes
from plan_model import Beat, ShotPlan


@pytest.mark.parametrize('label', [
    'Spine2>LeftShoulder', 'Hips>RightUpLeg', 'RightLeg>RightFoot',
    'LeftFoot>LeftToe_End', 'mixamorig:LeftForeArm>mixamorig:LeftHand', 'upper_arm.L',
])
def test_limb_connections_are_classified(label):
    assert is_limb_segment(label)


@pytest.mark.parametrize('label', ['Hips>Spine', 'Spine2>Neck', 'Neck>Head', 'Head>Head_End',
                                  'Armature>Hips', 'unknown_protected_bone'])
def test_core_and_unknown_connections_stay_protected(label):
    assert not is_limb_segment(label)


def _stage(tmp_path, limb='arm', core_overlap=False, obstacle=False):
    path = tmp_path / 'actors.usda'
    stage = Usd.Stage.CreateNew(str(path))
    world = UsdGeom.Xform.Define(stage, '/World')
    stage.SetDefaultPrim(world.GetPrim())
    stage.SetTimeCodesPerSecond(24)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(120)
    for name, x, sign in [('A', -1., 1), ('B', -.95 if core_overlap else 1., -1)]:
        actor = UsdSkel.Root.Define(stage, '/World/' + name)
        actor.GetPrim().SetCustomDataByKey('asset_id', 'character.synthetic')
        UsdGeom.Xformable(actor).AddTranslateOp().Set(Gf.Vec3d(x, 0, 0))
        skeleton = UsdSkel.Skeleton.Define(stage, '/World/' + name + '/Skeleton')
        if limb == 'arm':
            joints = ['Hips', 'Hips/Spine', 'Hips/Spine/LeftArm', 'Hips/Spine/LeftArm/LeftForeArm']
            positions = [(0, 1, 0), (0, .5, 0), (sign * 2, 0, 0), (0, 0, 0)]
        elif limb == 'leg':
            joints = ['Hips', 'Hips/Spine', 'Hips/LeftUpLeg', 'Hips/LeftUpLeg/LeftLeg']
            positions = [(0, 1, 0), (0, .5, 0), (sign * 2, -.5, 0), (0, -.5, 0)]
        else:
            joints = ['Hips', 'Hips/Spine', 'Hips/Spine/Head']
            positions = [(0, 1, 0), (0, .5, 0), (sign * 2, .2, 0)]
        skeleton.CreateJointsAttr(Vt.TokenArray(joints))
        skeleton.CreateRestTransformsAttr(Vt.Matrix4dArray(
            [Gf.Matrix4d(1).SetTranslate(Gf.Vec3d(*p)) for p in positions]))
        skeleton.CreateBindTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1) for _ in joints]))
    if obstacle:
        solid = UsdGeom.Cube.Define(stage, '/World/Obstacle')
        solid.CreateSizeAttr(.25)
        solid.AddTranslateOp().Set(Gf.Vec3d(0, 1.5, 0))
    stage.GetRootLayer().Save()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'assets': [{'id': 'character.synthetic', 'tags': ['character']}]}))
    return stage, AssetDB(str(manifest)), path, manifest


@pytest.mark.parametrize('limb', ['arm', 'leg'])
def test_limb_contact_allowed_and_strict_policy_still_available(tmp_path, limb):
    stage, db, _, _ = _stage(tmp_path, limb)
    relaxed = validate(stage, db, t0=0, t1=0)
    strict = validate(stage, db, t0=0, t1=0, allow_actor_limb_contacts=False)
    assert relaxed['ok'], relaxed
    assert relaxed['actor_contact_policy'] == 'core_body'
    assert not strict['ok'] and strict['violations']
    assert strict['actor_contact_policy'] == 'full_body'


@pytest.mark.parametrize('limb,overlap', [('arm', True), ('head', False)])
def test_main_body_and_head_contact_still_fail(tmp_path, limb, overlap):
    stage, db, _, _ = _stage(tmp_path, limb, core_overlap=overlap)
    report = validate(stage, db, t0=0, t1=0)
    assert not report['ok'] and report['violations']
    assert all(not is_limb_segment(v['body_part']) for v in report['violations'])


def test_limb_hits_on_scenery_are_not_exempt(tmp_path):
    stage, db, _, _ = _stage(tmp_path, obstacle=True)
    report = validate(stage, db, t0=0, t1=0)
    assert not report['ok']
    assert any(v['instance'] == 'Obstacle' and is_limb_segment(v['body_part']) for v in report['violations'])


@pytest.mark.parametrize('allow_limbs', [True, False])
def test_policy_matches_in_serial_and_parallel_validation(tmp_path, allow_limbs):
    stage, db, path, manifest = _stage(tmp_path)
    serial = validate(stage, db, allow_actor_limb_contacts=allow_limbs)
    parallel = validate_parallel(stage, db, str(path), str(manifest), workers=2,
                                 allow_actor_limb_contacts=allow_limbs)
    assert serial == parallel


@pytest.mark.parametrize('strict', [False, True])
def test_standalone_cli_records_selected_policy(tmp_path, strict):
    _, _, path, manifest = _stage(tmp_path)
    output = tmp_path / 'report.json'
    args = [str(path), '--manifest', str(manifest), '--workers', '1', '--t0', '0', '--t1', '0',
            '--json', str(output)]
    if strict:
        args.append('--strict-actor-limbs')
    assert main(args) == (1 if strict else 0)
    assert json.loads(output.read_text())['actor_contact_policy'] == ('full_body' if strict else 'core_body')


def test_planning_uses_core_for_actors_and_full_reach_for_solids(tmp_path):
    _, _, path, _ = _stage(tmp_path)
    manifest = {'assets': [{'id': 'character.synthetic', 'collision': {'character': {'radius_m': .3}}}],
                'clips': {'groving_to_music_energetically': {'file': str(path)},
                          'walk': {'file': str(path)}}}
    plan = ShotPlan(beats=[Beat('dance', 'dance', 'a', {'style': 'groove'})])
    states = {'a': {'asset_id': 'character.synthetic'}}
    relaxed = actor_envelopes(plan, states, manifest, ROOT,
                             CrowdSolverConfig(allow_actor_limb_contacts=True))
    strict = actor_envelopes(plan, states, manifest, ROOT)
    assert relaxed.beat_reach['dance'] < strict.beat_reach['dance']
    assert relaxed.solid_beat_reach == strict.solid_beat_reach
    assert relaxed.movement_radius('a', 'walk_to') < strict.movement_radius('a', 'walk_to')
    assert relaxed.solid_movement_radius('a', 'walk_to') == strict.solid_movement_radius('a', 'walk_to')
