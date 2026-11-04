#!/usr/bin/env python3
"""Replay prompts with/without a brief and score actual baked constraint satisfaction.

Default runs the compositional planner offline. --live-llm explicitly enables the
configured endpoint. Collision validation remains enabled in either mode.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
for folder in ('tools','tools/shot_library','tools/collision'):
    sys.path.insert(0,str(ROOT/folder))
site=ROOT/'.venv/lib/python3.12/site-packages'
if site.exists():sys.path.insert(0,str(site))
from compiler import ShotCompiler
from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from director_brief import resolve_path_for_actor, ACTION_SPEEDS


def score(stage_path, brief, fps=24):
    from pxr import Usd, UsdGeom
    if not stage_path.exists():return 0.
    stage=Usd.Stage.Open(str(stage_path))
    actors={str(p.GetCustomDataByKey('actor_id')):p for p in stage.Traverse() if p.GetCustomDataByKey('actor_id')}
    passed=total=0
    for actor,track in brief.get('tracks',{}).items():
        for seg in track.get('segments',[]):
            if not seg.get('path'):continue
            path=dict(brief['paths'][seg['path']])
            if seg['action'] in ACTION_SPEEDS:path['speed_band']=ACTION_SPEEDS[seg['action']][:2]
            for t,expected,_ in resolve_path_for_actor(path,seg['t']):
                total+=1
                prim=actors.get(actor)
                if prim and math.dist(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(t*fps).ExtractTranslation(),expected)<=.05:passed+=1
    return passed/total if total else 1.


def evaluate(cases, output, live=False):
    results=[]
    compiler=ShotCompiler(ROOT)
    with tempfile.TemporaryDirectory(prefix='painterspace_director_eval_') as temporary:
        for index,case in enumerate(cases):
            row={'id':case['id']}
            scene=(ROOT/case['scene']).resolve() if case.get('scene') else Path(temporary)/str(index)/'scene.usda'
            if not case.get('scene'):
                scene.parent.mkdir(parents=True,exist_ok=True)
                scene.write_text('#usda 1.0\ndef Xform "World" {}\n')
            for use_brief in (False,True):
                directory=Path(temporary)/str(index)/('brief' if use_brief else 'auto');directory.mkdir(parents=True)
                if use_brief:(directory/'director_brief.json').write_text(json.dumps(case['brief']))
                catalog=FunctionCatalog(compiler.manifest,compiler._read_scene_context(scene) if scene else {})
                planner=LLMShotPlanner(catalog)
                planner.brief=case['brief'] if use_brief else {'version':1,'tracks':{},'paths':{}}
                if live:
                    ok,report,logs=planner.plan_and_compile_with_retry(case['prompt'],compiler,directory/'shot.usda',scene,validate=True)
                else:
                    catalog.reset(prompt=case['prompt']);planner._plan_general_semantic(case['prompt'])
                    ok,report=compiler.compile(catalog.plan,directory/'shot.usda',scene_usda_path=scene,validate=True)
                    logs=[]
                row['brief' if use_brief else 'auto']={
                    'constraint_satisfaction':score(directory/'shot.usda',case['brief']),
                    'collision_free_first_pass':bool(report.get('validation',{}).get('collision_passed',False)) and not any('[Retry' in log for log in logs),
                    'retries':sum('[Retry' in log for log in logs),'success':ok}
            results.append(row)
    result={'mode':'live' if live else 'offline','cases':results}
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=ROOT/'tests/fixtures/director_eval.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live-llm',action='store_true')
    args=parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.cases.read_text()),args.output,args.live_llm),indent=2))
