import copy
import json
import math
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
for folder in ('tools','tools/shot_library','tools/collision'):
    sys.path.insert(0,str(ROOT/folder))
from director_brief import merge_brief, resolve_path_for_actor, build_digest
from director_solver import ensure_in_range
from plan_model import ShotPlan, Beat
from motion_synthesis import ActorPerformance, Keyframe
from camera_sampling import CameraPathSampler, StageTargets, interpolate_keys
from camera_workflow import bake_manual_cameras, save_fingerprints, change_status


def test_flexible_windows_are_explicit_and_solve_backwards():
    data={'version':1,'paths':{},'tracks':{'human_1':{'segments':[
        {'id':'a','t':[2,5],'action':'wait','lock':'preferred','flexible':True,'tolerance_s':2},
        {'id':'b','t':[4,6],'action':'wait','lock':'locked'}]}}}
    plan=merge_brief(data,ShotPlan())
    windows={b.params['brief_segment']:b.params['exact_window'] for b in plan.beats if b.params.get('brief_segment')}
    assert windows['a']==[1,4] and windows['b']==[4,6]
    assert data['tracks']['human_1']['segments'][0]['t']==[2,5]
    data['tracks']['human_1']['segments'][0]['flexible']=False
    with pytest.raises(ValueError):merge_brief(data,ShotPlan())


def test_implicit_gaps_can_be_filled_symbolically():
    data={'version':1,'paths':{},'duration_s':8,'tracks':{'human_1':{'segments':[{'id':'a','t':[2,4],'action':'wait','lock':'locked'}]}}}
    assert 'gap_before_a' in build_digest(data) and 'gap_after_a' in build_digest(data)
    plan=merge_brief(data,ShotPlan(beats=[Beat('choice','use_phone','human_1',{'during':'gap_before_a','action':'use_phone'})]))
    assert next(b for b in plan.beats if b.params.get('brief_segment')=='gap_before_a').function=='use_phone'


def test_path_only_intent_is_compiled_without_llm():
    data={'version':1,'tracks':{},'paths':{'p':{'owner':'human_1','points':[[0,0,0],[0,0,-7]],'speed_profile':'constant'}}}
    plan=merge_brief(data,ShotPlan())
    assert plan.beats[0].params['exact_window']==[0,5]
    assert plan.beats[0].params['brief_path']['points'][-1]==[0,0,-7]


def test_prompt_without_timeline_does_not_lock_every_drawn_path():
    data={'version':1,'tracks':{},'paths':{
        'p':{'owner':'background_human','points':[[0,0,0],[0,0,-7]],'speed_profile':'constant'}}}
    prompt_plan=ShotPlan(prompt='The main actor greets a friend', beats=[
        Beat('greet_1','greet','main_human',{'target':'friend'})])
    merged=merge_brief(data,prompt_plan)
    assert [beat.beat_id for beat in merged.beats]==['greet_1']
    digest=build_digest(data)
    assert 'Available drawn route p for background_human' in digest
    assert 'LOCKED' not in digest


def test_eased_path_never_leaves_action_speed_band():
    path={'points':[[0,0,0],[0,0,-21]],'speed_profile':'ease_in_out','speed_band':[2.2,7.]}
    samples=resolve_path_for_actor(path,[0,5])
    speeds=[math.dist(a[1],b[1])/(b[0]-a[0]) for a,b in zip(samples,samples[1:])]
    assert min(speeds)>=2.2 and max(speeds)<=7
    assert max(speeds)>min(speeds)+1


def test_contact_approach_preserves_preceding_motion_and_holds_target():
    a=ActorPerformance('a',keyframes=[Keyframe(0,[0,0,5],0),Keyframe(5,[0,0,5],0)],actions=[{'clip':'left_hook','start_s':3,'end_s':5}])
    b=ActorPerformance('b',keyframes=[Keyframe(0,[0,0,0],0),Keyframe(5,[1,0,0],0)])
    assert ensure_in_range(a,b,[3,5],available_start=1) is None
    assert a.sample(0)[0]==[0,0,5]
    for t in [3,4,5]:assert math.dist(a.sample(t)[0],b.sample(t)[0])==pytest.approx(1,abs=.01)
    assert any(x['clip']=='run' for x in a.actions)
    far=ActorPerformance('far',keyframes=[Keyframe(0,[0,0,50],0)])
    assert ensure_in_range(far,b,[0,2])['error_code']=='CONTACT_UNREACHABLE'


def test_camera_dwell_custom_pacing_and_free_quaternion():
    path={'waypoints':[{'p':[0,1,0],'dwell_s':1},{'p':[0,1,-4]}],'time_range':[0,3],'speed':{'profile':'constant'}}
    sampler=CameraPathSampler(path,3)
    assert sampler.sample(.75)[0]==pytest.approx([0,1,0])
    assert sampler.sample(2)[0]==pytest.approx([0,1,-2],abs=.01)
    path['waypoints'][0]['dwell_s']=0
    path['speed']['keys']=[{'t':0,'distance':0},{'t':.5,'distance':.25},{'t':1,'distance':1}]
    assert CameraPathSampler(path,3).sample(1.5)[0]==pytest.approx([0,1,-1],abs=.01)
    q=interpolate_keys([{'t':0,'rotation_deg':[0,179,0]},{'t':1,'rotation_deg':[0,-179,0]}],.5,'rotation_deg',[0,0,0],True)
    assert abs(q.GetReal())<.02


def stage_fixture(tmp_path):
    from pxr import Usd, UsdGeom
    stage=Usd.Stage.CreateNew(str(tmp_path/'shot.usda'))
    hero=UsdGeom.Xform.Define(stage,'/World/Hero');hero.GetPrim().SetCustomDataByKey('actor_id','human_1')
    hero.AddTranslateOp().Set((0,0,0));UsdGeom.Cube.Define(stage,'/World/Hero/Body').CreateSizeAttr(1)
    wall=UsdGeom.Cube.Define(stage,'/World/Wall');wall.AddTranslateOp().Set((0,2,10));wall.CreateSizeAttr(1)
    stage.GetRootLayer().Save()
    (tmp_path/'shot.json').write_text(json.dumps({'fps':24,'duration_s':1,'prompt':'hello'}))
    (tmp_path/'director_brief.json').write_text('{"version":1,"tracks":{},"paths":{}}')
    (tmp_path/'shot_plan.json').write_text('{}')
    rig={'version':1,'active_camera':'cam_1','cameras':{'cam_1':{'path':{'waypoints':[{'p':[0,2,10]}],'time_range':[0,1]},'aim':{'segments':[{'t':[0,1],'mode':'look_at','targets':[{'actor':'human_1'}]}]},'lens':{'focal_mm':35}}}}
    (tmp_path/'camera_rig.json').write_text(json.dumps(rig));save_fingerprints(tmp_path)
    return stage,rig


def test_camera_diagnostics_and_preview_does_not_clear_stale_actors(tmp_path):
    stage,rig=stage_fixture(tmp_path)
    before=(tmp_path/'generation_fingerprints.json').read_bytes()
    result=bake_manual_cameras(tmp_path)
    assert 'CAMERA_SOLID' in {w['code'] for w in result['warnings']}
    assert all('time_range' in w for w in result['warnings'])
    (tmp_path/'director_brief.json').write_text('{"version":1,"tracks":{},"paths":{},"duration_s":2}')
    state=(tmp_path/'generation_fingerprints.json').read_bytes()
    bake_manual_cameras(tmp_path,require_current=False,record_fingerprints=False)
    assert (tmp_path/'generation_fingerprints.json').read_bytes()==state
    assert change_status(tmp_path)=='generate'


def test_skeleton_joint_focus_uses_baked_transforms(tmp_path):
    from pxr import Gf, Usd, UsdGeom, UsdSkel, Vt
    stage=Usd.Stage.CreateInMemory()
    root=UsdSkel.Root.Define(stage,'/World/Hero');root.GetPrim().SetCustomDataByKey('actor_id','human_1')
    skel=UsdSkel.Skeleton.Define(stage,'/World/Hero/Skeleton')
    skel.CreateJointsAttr(Vt.TokenArray(['Hips','Hips/Spine2','Hips/Spine2/Head']))
    skel.CreateRestTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1),Gf.Matrix4d(1).SetTranslate((0,1,0)),Gf.Matrix4d(1).SetTranslate((0,.5,0))]))
    skel.CreateBindTransformsAttr(Vt.Matrix4dArray([Gf.Matrix4d(1)]*3))
    targets=StageTargets(stage)
    assert list(targets.sample({'actor':'human_1','joint':'Chest'},Usd.TimeCode(0)))==pytest.approx([0,1,0])
    assert list(targets.sample({'actor':'human_1','joint':'Head'},Usd.TimeCode(0)))==pytest.approx([0,1.5,0])


def test_contact_compiles_with_real_skeleton_and_exact_window(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    brief={'version':1,'paths':{},'tracks':{'human_1':{'segments':[{'id':'strike','t':[1,2],'action':'attack','target':'human_2','lock':'locked'}]}}}
    (tmp_path/'director_brief.json').write_text(json.dumps(brief))
    plan=ShotPlan(setup=[SetupAction('place','human_1','character.human',label='Hero',position=[0,0,0]),SetupAction('place','human_2','character.human',label='Other',position=[0,0,-3])])
    ok,report=ShotCompiler(ROOT).compile(plan,tmp_path/'shot.usda',validate=False)
    assert ok,json.dumps(report.get('validation',report),indent=2)
    from pxr import Usd,UsdGeom
    stage=Usd.Stage.Open(str(tmp_path/'shot.usda'))
    hero=UsdGeom.Xformable(stage.GetPrimAtPath('/World/Hero'))
    other=UsdGeom.Xformable(stage.GetPrimAtPath('/World/Other'))
    for t in [1,1.5,1.9]:
        assert math.dist(hero.ComputeLocalToWorldTransform(t*24).ExtractTranslation(),other.ComputeLocalToWorldTransform(t*24).ExtractTranslation())==pytest.approx(1,abs=.05)


def test_manual_schema_removes_every_camera_verb():
    from function_catalog import FunctionCatalog
    from llm_planner import LLMShotPlanner
    planner=LLMShotPlanner(FunctionCatalog({}));planner.camera_mode='manual'
    assert not any(t['function']['name'].startswith('camera_') for t in planner.export_tool_schemas())


def test_queue_payload_projects_geometry_and_camera_out(tmp_path,monkeypatch):
    import process_agent_queue as queue
    brief={'version':1,'tracks':{},'paths':{'p':{'owner':'human_1','points':[[0,0,0],[0,0,-7]],'speed_profile':'constant'}}}
    (tmp_path/'director_brief.json').write_text(json.dumps(brief))
    (tmp_path/'camera_rig.json').write_text('{"version":1,"active_camera":"cam_1","cameras":{}}')
    scene=tmp_path/'scene.usda';scene.write_text('#usda 1.0\ndef Xform "World" {\n def Camera "ShotCam" {}\n}\n')
    pending=tmp_path/'pending.json'
    pending.write_text(json.dumps({'target_stage':str(tmp_path/'shot.usda'),'scene_stage':str(scene),'prompt':'walk',
        'camera_locked':True,'existing_shot_usda':'point3f[] points = [(1, 2, 3)]',
        'world_state':{'camera':{'position':[1,2,3]}},'validation_error':{'message':'Gap 4.5 metres','details':{'camera':[1,2,3]}}}))
    monkeypatch.setattr(queue,'PENDING_FILE',pending)
    assert queue.prepare_pending_task()==0
    task=json.loads(pending.read_text())
    assert all(k not in task for k in ['world_state','existing_shot_usda','camera_locked','world_state_images'])
    assert not any(t['function']['name'].startswith('camera_') for t in task['function_catalog_schemas'])
    assert 'ShotCam' not in task['planner_rules']
    assert 'points' not in json.dumps(task['manifest_summary'])
    assert '4.5' not in json.dumps(task['validation_error'])


def test_repairs_are_explicit_nonmutating_and_preflight_uses_real_solids(tmp_path):
    from brief_tools import repair_brief,inspect_brief
    from compiler import ShotCompiler
    from pxr import Usd,UsdGeom
    data={'version':1,'tracks':{'human_1':{'segments':[{'id':'s','t':[0,1],'action':'walk_to','path':'p'}]}},'paths':{'p':{'owner':'human_1','points':[[0,0,0],[0,0,-10]]}}}
    original=copy.deepcopy(data)
    repaired=repair_brief(data,'stretch_window','s')
    assert repaired['tracks']['human_1']['segments'][0]['t'][1]>1
    assert data==original
    scene=Usd.Stage.CreateNew(str(tmp_path/'scene.usda'))
    obstacle=UsdGeom.Cube.Define(scene,'/World/Block');obstacle.CreateSizeAttr(2)
    obstacle.AddTranslateOp().Set((0,1,-5));scene.GetRootLayer().Save()
    (tmp_path/'director_brief.json').write_text(json.dumps(repaired))
    report=inspect_brief(tmp_path,ShotCompiler(ROOT).manifest,tmp_path/'scene.usda')
    assert not any(i['code']=='ACTION_SPEED_MISMATCH' for i in report['lint'])
    assert any(i['code']=='PATH_SOLID' for i in report['lint'])
    from colliders import AssetDB
    routed=repair_brief(repaired,'route_around',path_id='p',stage=scene,manifest_db=AssetDB(ROOT/'manifest.json'))
    assert len(routed['paths']['p']['points'])>2
    assert repaired['paths']['p']['points']==data['paths']['p']['points']


def test_replay_compilation_is_byte_identical(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    brief={'version':1,'tracks':{'human_1':{'segments':[{'id':'s','t':[0,1],'action':'walk_to','path':'p','lock':'locked'}]}},'paths':{'p':{'owner':'human_1','points':[[0,0,0],[0,0,-1.4]],'speed_profile':'ease_in_out'}}}
    (tmp_path/'director_brief.json').write_text(json.dumps(brief))
    compiler=ShotCompiler(ROOT);plan=ShotPlan(setup=[SetupAction('place','human_1','character.human',label='Hero')])
    ok,report=compiler.compile(plan,tmp_path/'shot.usda',validate=False);assert ok,json.dumps(report.get('validation',report),indent=2)
    names=['shot.usda','anim_human_1.usda','actions_human_1.json','director_brief.json']
    first={name:(tmp_path/name).read_bytes() for name in names}
    ok,report=compiler.compile(plan,tmp_path/'shot.usda',validate=False);assert ok,json.dumps(report.get('validation',report),indent=2)
    assert first=={name:(tmp_path/name).read_bytes() for name in names}


def test_regeneration_keeps_manual_camera_authoring_bytes(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    from pxr import Usd
    brief={'version':1,'tracks':{'human_1':{'segments':[{'id':'s','t':[0,1],'action':'walk_to','path':'p','lock':'locked'}]}},'paths':{'p':{'owner':'human_1','points':[[0,0,0],[0,0,-1.4]],'speed_profile':'constant'}}}
    (tmp_path/'director_brief.json').write_text(json.dumps(brief))
    rig={'version':1,'active_camera':'cam_1','cameras':{'cam_1':{'path':{'waypoints':[{'p':[0,2,5]}],'time_range':[0,1]},'aim':{'segments':[{'t':[0,1],'mode':'look_at','targets':[{'actor':'human_1','joint':'Chest'}]}]},'lens':{'focal_mm':35}}}}
    raw=json.dumps(rig,indent=4).encode();(tmp_path/'camera_rig.json').write_bytes(raw)
    plan=ShotPlan(setup=[SetupAction('place','human_1','character.human',label='Hero')])
    ok,report=ShotCompiler(ROOT).compile(plan,tmp_path/'shot.usda',validate=False);assert ok,report
    assert (tmp_path/'camera_rig.json').read_bytes()==raw
    assert (tmp_path/'cam_cam_1.usda').exists()
    stage=Usd.Stage.Open(str(tmp_path/'render_stage.usda'))
    from camera_workflow import resolve_render_camera
    assert resolve_render_camera(stage,tmp_path)=='/World/Cameras/cam_1'
    assert stage.GetPrimAtPath('/World/Cameras/cam_1')


def test_proximity_events_sample_actual_drawn_paths():
    from timeline_resolver import TimelineResolver
    from plan_model import TimingCondition
    brief={'version':1,'tracks':{'human_1':{'segments':[{'id':'s','t':[0,5],'action':'walk_to','path':'p','lock':'locked'}]}},'paths':{'p':{'owner':'human_1','points':[[0,0,10],[0,0,3]],'speed_profile':'constant'}}}
    react=Beat('react','wait','human_2',{},TimingCondition('when_closer_than',0,actor_a='human_1',actor_b='human_2',gap='far'))
    timeline,error=TimelineResolver().resolve(merge_brief(brief,ShotPlan(beats=[react])),{'human_1':{'position':[0,0,10]},'human_2':{'position':[0,0,0]}})
    assert error is None
    assert next(b.start_time for b in timeline.beats if b.beat_id=='react')>3


def test_fractional_window_boundary_has_an_exact_baked_sample(tmp_path):
    from compiler import ShotCompiler
    from plan_model import SetupAction
    from pxr import Usd
    brief={'version':1,'paths':{},'tracks':{'human_1':{'segments':[{'id':'idle','t':[0,.3],'action':'wait','lock':'locked'},{'id':'wave','t':[.3,.9],'action':'waving','lock':'locked'}]}}}
    (tmp_path/'director_brief.json').write_text(json.dumps(brief))
    ok,report=ShotCompiler(ROOT).compile(ShotPlan(setup=[SetupAction('place','human_1','character.human',label='Hero')]),tmp_path/'shot.usda',validate=False)
    assert ok,json.dumps(report.get('validation',report),indent=2)
    anim=Usd.Stage.Open(str(tmp_path/'anim_human_1.usda'))
    marker=anim.GetPrimAtPath('/BakedAnim').GetAttribute('ps:actionClip')
    assert marker.Get(.3*24)=='waving'
    assert marker.Get(.3*24-.0001)=='acknowledging'
    assert any(abs(t-.3*24)<1e-7 for t in anim.GetPrimAtPath('/BakedAnim').GetAttribute('rotations').GetTimeSamples())
