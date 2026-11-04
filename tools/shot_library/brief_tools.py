"""Editor-facing director lint and explicit repairs. No planner or camera payloads."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
from director_brief import validate_brief, build_digest, path_metrics, ACTION_SPEEDS, ALIASES, check_turning_radius, route_around


def inspect_brief(shot_dir, manifest, scene_path=None, scene_context=None):
    from function_catalog import FunctionCatalog
    directory = Path(shot_dir)
    path = directory/'director_brief.json'
    brief = json.loads(path.read_text()) if path.exists() else {'version': 1, 'tracks': {}, 'paths': {}}
    catalog = FunctionCatalog(manifest)
    excluded = {'reset', 'get_plan', 'place', 'remove', 'set_appearance', 'set_variant', 'set_environment', 'follow_path', 'perform'}
    actions = sorted(name for name in dir(catalog) if not name.startswith('_') and not name.startswith('list_') and not name.startswith('camera_') and callable(getattr(catalog, name)) and name not in excluded and name not in {'describe_actor', 'find_actor', 'where_is', 'distance_between', 'time_of', 'can_reach', 'check_plan', 'camera_frame'})
    actions += sorted(set(manifest.get('clips', {}))-set(actions))
    issues = validate_brief(brief, actions=actions)
    stage = None
    if scene_path and Path(scene_path).exists():
        from pxr import Usd
        from pxr import Sdf
        edits = Path(scene_path).parent/'user_edits.usda'
        stage = Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(scene_path)), Sdf.Layer.FindOrOpen(str(edits))) if edits.exists() else Usd.Stage.Open(str(scene_path))
    if stage:
        from camera_sampling import StageTargets
        known = StageTargets(stage)
        available = set(known.actors) | {str(p.GetName()) for p in stage.Traverse()}
        available |= set(brief.get('tracks', {}))
        for actor, track in brief.get('tracks', {}).items():
            for seg in track.get('segments', []):
                target = seg.get('target')
                if target and target.split(':')[0] not in available:
                    issues.append({'code': 'MISSING_TARGET', 'actor': actor, 'segment': seg['id'], 'time_s': seg['t'][0]})
        from colliders import AssetDB
        from plan_path import obstacles, rasterize
        import numpy as np
        db = AssetDB(Path(__file__).resolve().parents[2]/'manifest.json')
        assigned_paths = {seg.get('path') for track in brief.get('tracks', {}).values()
                          for seg in track.get('segments', []) if isinstance(seg, dict) and seg.get('path')}
        for pid, path in brief.get('paths', {}).items():
            # Unassigned drawn routes are optional visual guides. Their geometry
            # constrains the shot only when a timeline segment names the route.
            if pid not in assigned_paths: continue
            if any(i.get('path') == pid for i in issues): continue
            owner = path.get('owner')
            entry = next((a for a in manifest.get('assets', []) if a.get('id') == brief.get('tracks', {}).get(owner, {}).get('asset_id')), {})
            if any(token in str(entry.get('id', owner)).lower() for token in ('car', 'vehicle')):
                issues.extend({**w, 'path': pid, 'fixes': ['widen_corner']} for w in check_turning_radius(path, entry))
            # Use the production inflated footprint rasterizer, including long edges.
            pts = path['points']
            floor = min(p[1] for p in pts)
            prim = known.prim(owner)
            skip = (owner, str(prim.GetName()), str(prim.GetPath())) if prim else (owner,)
            obs = obstacles(stage, db, [0], skip=skip, floor_y=floor)
            if not obs: continue
            xs, zs = [p[0] for p in pts], [p[2] for p in pts]
            bounds = [min(xs)-1, min(zs)-1, max(xs)+1, max(zs)+1]
            grid, _ = rasterize(obs, bounds, .1, .45)
            # Check continuous edge samples against the same occupancy grid as A*.
            for a, b in zip(pts, pts[1:]):
                count = max(1, math.ceil(math.dist(a,b)/.05))
                blocked = False
                for i in range(count+1):
                    x = a[0]+(b[0]-a[0])*i/count; z = a[2]+(b[2]-a[2])*i/count
                    ix, iz = int((x-bounds[0])/.1), int((z-bounds[1])/.1)
                    if 0 <= ix < grid.shape[0] and 0 <= iz < grid.shape[1] and grid[ix, iz]: blocked = True; break
                if blocked:
                    issues.append({'code': 'PATH_SOLID', 'path': pid, 'fixes': ['route_around']}); break
    from director_brief import enrich_brief_choices
    return {'brief': brief, 'digest': build_digest(enrich_brief_choices(brief, manifest), scene_context), 'lint': issues, 'actions': actions}


def repair_brief(brief, operation, segment_id=None, path_id=None, stage=None, manifest_db=None):
    result = copy.deepcopy(brief)
    seg = next((s for tr in result.get('tracks', {}).values() for s in tr.get('segments', []) if s['id'] == segment_id), None)
    if operation == 'route_around':
        if stage is None: raise ValueError('Routing needs a scene stage')
        result['paths'][path_id] = route_around(result['paths'][path_id], stage, manifest_db)
    elif operation in ('stretch_window', 'shorten_window', 'switch_action', 'lengthen_path', 'shorten_path'):
        if not seg or not seg.get('path'): raise ValueError('Select a segment with a path')
        path = result['paths'][seg['path']]
        length = path_metrics(path)['length_m']; start, end = seg['t']
        low, high, _ = ACTION_SPEEDS[ALIASES.get(seg['action'], seg['action'])]
        if operation in ('stretch_window', 'shorten_window'): seg['t'] = [start, start+length/((low+high)/2)]
        elif operation in ('lengthen_path', 'shorten_path'):
            desired = (end-start)*(low+high)/2
            origin = path['points'][0]
            if length <= 1e-9: raise ValueError('Draw a direction before lengthening a path')
            path['points'] = [[origin[j]+(p[j]-origin[j])*desired/length for j in range(3)] for p in path['points']]
        else:
            speed = length/(end-start)
            seg['action'] = next((action for action in ('walk_to','jog_to','run_to','sprint_to') if ACTION_SPEEDS[action][0] <= speed <= ACTION_SPEEDS[action][1]), seg['action'])
    elif operation == 'widen_corner':
        from director_brief import check_turning_radius
        path = result['paths'][path_id]
        warnings = check_turning_radius(path, {})
        if warnings:
            scale = max(w['minimum_radius_m']/max(.01, w['radius_m']) for w in warnings)*1.05
            origin = path['points'][0]
            path['points'] = [[origin[j]+(p[j]-origin[j])*scale for j in range(3)] for p in path['points']]
    elif operation == 'trim':
        for track in result.get('tracks', {}).values():
            end = 0.
            for s in sorted(track.get('segments', []), key=lambda s:s['t'][0]):
                if s['t'][0] < end:
                    if s['t'][1] <= end: raise ValueError('A fully overlapped segment must be deleted or moved')
                    s['t'][0] = end
                end = s['t'][1]
    else: raise ValueError('Unknown brief repair')
    return result
