"""Reproduce component comparisons using an explicit baseline module snapshot."""
import importlib.util,sys,time,statistics,math,json
from pathlib import Path
import argparse
parser=argparse.ArgumentParser(description='Compare crowd component timings against a saved module snapshot.')
parser.add_argument('--baseline-dir', type=Path, required=True, help='Directory with baseline st_planner.py, joint.py, camera_controller.py and director_brief.py')
parser.add_argument('--output', type=Path)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'tools/shot_library'))
from crowd import st_planner,joint
import camera_controller,director_brief
oldroot=args.baseline_dir
def module(name,file):
 spec=importlib.util.spec_from_file_location(name,oldroot/file);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
old_st=module('crowd._performance_before_st','st_planner.py')
old_joint=module('crowd._performance_before_joint','joint.py');old_joint.blocked_intervals=old_st.blocked_intervals
old_camera=module('_performance_before_camera','camera_controller.py')
old_paths=module('_performance_before_paths','director_brief.py')
from crowd.config import CrowdSolverConfig
cfg=CrowdSolverConfig();rows=[]
def compare(label,old,new,loops):
 times=[[],[]]
 for run in range(7):
  for i in ((0,1) if run%2==0 else (1,0)):
   f=(old,new)[i];start=time.perf_counter()
   for _ in range(loops):f()
   times[i].append((time.perf_counter()-start)/loops)
 a,b=[statistics.median(t)for t in times]
 rows.append({'case':label,'baseline_ms':a*1000,'updated_ms':b*1000,'speedup':a/b,'repeats':7,'iterations_per_repeat':loops})
for count in (2,4,20,40):
 def make(m):return[m.Reservation(str(i),[(0,i*4.,0,0)],.3)for i in range(count)]
 a,b=make(old_st),make(st_planner)
 assert old_joint.first_conflict(a,0,70,cfg)==joint.first_conflict(b,0,70,cfg)
 compare(f'{count}_stationary_conflicts',lambda:old_joint.first_conflict(a,0,70,cfg),lambda:joint.first_conflict(b,0,70,cfg),100)
points=[(t/10,math.sin(t),0,math.cos(t))for t in range(100)]
windows=[(i,i+2,.3+i*.01)for i in range(10)]
a=old_st.Reservation('a',points,.3,windows);b=st_planner.Reservation('a',points,.3,windows)
compare('short_interval_queries_100_keys',lambda:list(a.intervals(3.1,3.4)),lambda:list(b.intervals(3.1,3.4)),2000)
for count in (2,4,20):
 keys=[camera_controller.CameraKeyframe(i/24,[0,2,30],[0,0,0])for i in range(1680)]
 positions={str(i):[(0,[i*.1,0,0]),(70,[i*.1,0,0])]for i in range(count)}
 compare(f'{count}_camera_subjects',lambda:old_camera.CameraController().validate_frustum(keys,positions,list(positions)),lambda:camera_controller.CameraController().validate_frustum(keys,positions,list(positions)),50)
for count in (2,40):
 p={'points':[[i,0,math.sin(i)]for i in range(count)]}
 a=old_paths.resolve_path_for_actor(p,(0,20));b=director_brief.resolve_path_for_actor(p,(0,20));assert [(t,p)for t,p,h in a]==[(t,p)for t,p,h in b]
 compare(f'{count}_point_path_sampling',lambda:old_paths.resolve_path_for_actor(p,(0,20)),lambda:director_brief.resolve_path_for_actor(p,(0,20)),20)
result={'method':'Interleaved seven-repeat medians on the same process; component timings, not rendering benchmarks.', 'components':rows}
if args.output:
 args.output.write_text(json.dumps(result,indent=2)+'\n')
else:
 print(json.dumps(result,indent=2))
