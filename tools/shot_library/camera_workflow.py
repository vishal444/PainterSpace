"""Camera-only baking: no planner imports, actor baking, or actor artifact writes."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


def fingerprints(shot_dir, prompt=None):
    directory = Path(shot_dir)
    meta_path = directory / 'shot.json'
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    actor = hashlib.sha256()
    actor.update((meta.get('prompt', '') if prompt is None else prompt).encode())
    for path in (directory / 'director_brief.json', directory / 'shot_plan.json',
                 directory.parent / 'scene.usda'):
        actor.update(path.name.encode())
        actor.update(path.read_bytes() if path.exists() else b'<missing>')
    rig = directory / 'camera_rig.json'
    edits = directory / 'user_edits.usda'
    return {'actor': actor.hexdigest(),
            'camera': hashlib.sha256(rig.read_bytes() if rig.exists() else b'').hexdigest(),
            'edits': hashlib.sha256(edits.read_bytes() if edits.exists() else b'').hexdigest()}


def save_fingerprints(shot_dir):
    (Path(shot_dir) / 'generation_fingerprints.json').write_text(
        json.dumps(fingerprints(shot_dir), sort_keys=True, indent=2)+'\n')


def change_status(shot_dir, prompt=None):
    path = Path(shot_dir) / 'generation_fingerprints.json'
    if not path.exists():
        return 'generate'
    previous = json.loads(path.read_text())
    current = fingerprints(shot_dir, prompt)
    if previous.get('actor') != current['actor']:
        return 'generate'
    if previous != current:
        return 'camera_only'
    return 'up_to_date'


def resolve_render_camera(stage, shot_dir):
    rig_path = Path(shot_dir) / 'camera_rig.json'
    if rig_path.exists():
        data = json.loads(rig_path.read_text())
        candidate = '/World/Cameras/'+data['active_camera']
        if stage.GetPrimAtPath(candidate):
            return candidate
    if stage.GetPrimAtPath('/World/ShotCam'):
        return '/World/ShotCam'
    meta = json.loads((Path(shot_dir) / 'shot.json').read_text())
    return meta.get('camera_prim', '/World/ShotCam')


def load_camera_document(path):
    """Reject invalid rigs before writing any derived layer."""
    data = json.loads(Path(path).read_text())
    if data.get('version') != 1 or not data.get('cameras') or data.get('active_camera') not in data['cameras']:
        raise ValueError('Invalid camera rig version or active camera')
    def finite(value):
        if isinstance(value, dict):
            for v in value.values(): finite(v)
        elif isinstance(value, list):
            for v in value: finite(v)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Camera values must be finite')
    finite(data)
    for cid, rig in data['cameras'].items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', cid): raise ValueError('Invalid camera ID')
        if not rig.get('path', {}).get('waypoints'): raise ValueError('A camera requires a waypoint')
        for w in rig['path']['waypoints']:
            if len(w.get('p', [])) != 3: raise ValueError('Camera waypoints require three coordinates')
            if w.get('dwell_s', 0) < 0: raise ValueError('Camera dwell must be non-negative')
        segments = sorted(rig.get('aim', {}).get('segments', []), key=lambda x: x['t'][0])
        end = -math.inf
        for seg in segments:
            if seg['t'][0] < end or seg['t'][1] <= seg['t'][0]: raise ValueError('Invalid focus windows')
            end = seg['t'][1]
            if seg['mode'] not in ('look_at', 'along_path', 'lock_direction', 'free'): raise ValueError('Unknown camera aim mode')
        keys = rig['path'].get('speed', {}).get('keys', [])
        if keys and (keys[0]['t'] != 0 or keys[-1]['t'] != 1 or keys[0]['distance'] != 0 or keys[-1]['distance'] != 1):
            raise ValueError('Speed keys must span normalized time and distance')
        if any(b['t'] <= a['t'] or b['distance'] < a['distance'] for a, b in zip(keys, keys[1:])):
            raise ValueError('Speed keys must be monotonic')
    return data


def check_frustum(camera, targets, tc, target_specs):
    """Project target bounds through the actual baked camera; warnings never gate actors."""
    from pxr import Gf, UsdGeom
    transform = UsdGeom.Xformable(camera).ComputeLocalToWorldTransform(tc)
    inv = transform.GetInverse()
    focal = camera.GetFocalLengthAttr().Get(tc)
    aperture_x = camera.GetHorizontalApertureAttr().Get(tc)
    aperture_y = camera.GetVerticalApertureAttr().Get(tc)
    issues = []
    for spec in target_specs:
        prim = targets.prim(spec['actor'])
        if not prim: continue
        if spec.get('joint'):
            try: positions = [targets.sample(spec, tc)]
            except ValueError: continue
        else:
            bound = UsdGeom.Imageable(prim).ComputeWorldBound(tc, 'default', 'render').ComputeAlignedRange()
            positions = [bound.GetCorner(i) for i in range(8)] if not bound.IsEmpty() else [targets.sample(spec, tc)]
        outside = []
        for p in positions:
            v = inv.Transform(p)
            depth = -v[2]
            outside.append(depth <= .1 or abs(v[0])*focal > depth*aperture_x/2 or abs(v[1])*focal > depth*aperture_y/2)
        if any(outside):
            issues.append({'code': 'OUTSIDE_FRAME' if all(outside) else 'CROPPED_TARGET', 'actor': spec['actor']})
    return issues


def _group_warnings(warnings):
    """Coalesce contiguous 48 Hz diagnostics into click-to-seek time ranges."""
    grouped = {}
    for warning in warnings:
        item = dict(warning)
        t = item.pop('time_s', 0.)
        key = json.dumps(item, sort_keys=True)
        spans = grouped.setdefault(key, [])
        if spans and t <= spans[-1]['time_range'][1]+1/48+1e-6:
            spans[-1]['time_range'][1] = t
        else:
            spans.append({**item, 'time_range': [t, t]})
    return [w for key in sorted(grouped) for w in grouped[key]]


def bake_manual_cameras(shot_dir, prompt=None, require_current=True, record_fingerprints=True):
    from pxr import Gf, Sdf, Usd, UsdGeom, Vt
    from camera_rig import look_at_rotation_xyz, path_tangent_rotation_xyz, evaluate_seeded_shake
    from camera_sampling import CameraPathSampler, StageTargets, quaternion_xyz, interpolate_keys

    directory = Path(shot_dir).resolve()
    if require_current and change_status(directory, prompt) == 'generate':
        raise ValueError('Actor inputs changed; generate the shot before re-rendering its camera')
    data = load_camera_document(directory / 'camera_rig.json')
    meta = json.loads((directory / 'shot.json').read_text())
    fps, duration = float(meta.get('fps', 24)), float(meta['duration_s'])
    # Validate every timing schedule before authoring any output.
    samplers = {cid: CameraPathSampler(rig['path'], duration) for cid, rig in data['cameras'].items()}
    composed = Usd.Stage.CreateInMemory()
    layers = [str(directory / 'shot.usda')]
    if (directory / 'user_edits.usda').exists(): layers.insert(0, str(directory / 'user_edits.usda'))
    composed.GetRootLayer().subLayerPaths = layers
    targets = StageTargets(composed)
    solids = []
    for prim in composed.Traverse():
        if not prim.IsA(UsdGeom.Boundable): continue
        if UsdGeom.Imageable(prim).ComputePurpose() == 'guide': continue
        bounds = UsdGeom.Imageable(prim).ComputeUntransformedBound(0, 'default', 'render').ComputeAlignedRange()
        if not bounds.IsEmpty(): solids.append((prim, bounds))
    warnings, outputs = [], []
    def warn(code, cid, t, **details): warnings.append({'code': code, 'camera': cid, 'time_s': t, **details})
    for cid, rig in sorted(data['cameras'].items()):
        sampler = samplers[cid]
        layer = Usd.Stage.CreateInMemory()
        layer.SetTimeCodesPerSecond(fps); layer.SetFramesPerSecond(fps)
        layer.SetStartTimeCode(0); layer.SetEndTimeCode(duration*fps)
        camera = UsdGeom.Camera.Define(layer, '/World/Cameras/'+cid)
        lens, aim = rig.get('lens', {}), rig.get('aim', {})
        camera.CreateHorizontalApertureAttr(lens.get('horizontal_aperture_mm', 36.))
        camera.CreateVerticalApertureAttr(lens.get('vertical_aperture_mm', 20.25))
        camera.CreateClippingRangeAttr(Gf.Vec2f(*lens.get('clipping_range', [.1, 10000])))
        xf = UsdGeom.Xformable(camera)
        tr, orient = xf.AddTranslateOp(), xf.AddOrientOp(UsdGeom.XformOp.PrecisionDouble)
        curve = UsdGeom.BasisCurves.Define(layer, '/World/CameraPaths/'+cid)
        curve.CreateTypeAttr('linear'); curve.CreatePurposeAttr('guide')
        guide = [sampler.sample(sampler.start+(sampler.end-sampler.start)*i/128)[0] for i in range(129)]
        curve.CreateCurveVertexCountsAttr([len(guide)])
        curve.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in guide]))
        lens_keys = [{'t': 0., 'focal_mm': lens.get('focal_mm', 35.)}, *(lens.get('keys') or [])]
        for waypoint in rig['path']['waypoints']:
            if 'focal_mm' in waypoint and 't' in waypoint: lens_keys.append({'t': waypoint['t'], 'focal_mm': waypoint['focal_mm']})
        previous_q, previous_position, previous_velocity = None, None, None
        smoothed, velocity = None, Gf.Vec3d(0)
        initial_subject_distance = None
        segments = sorted(aim.get('segments', []), key=lambda s: s['t'][0])
        for i in range(math.ceil(duration*48)+1):
            t, tc = min(duration, i/48), Usd.TimeCode(min(duration, i/48)*fps)
            position, tangent = sampler.sample(t)
            eligible = [s for s in segments if s['t'][0] <= t]
            segment = eligible[-1] if eligible else (segments[0] if segments else {'mode': 'along_path'})
            target_specs = segment.get('targets', [])
            points = []
            for spec in target_specs:
                try:
                    point = targets.sample(spec, tc)
                    lead = float(aim.get('lead_room_s', 0))
                    if lead: point += (targets.sample(spec, Usd.TimeCode((t+1/48)*fps))-point)*48*lead
                    points.append(point)
                except ValueError as exc: warn('MISSING_FOCUS_TARGET', cid, t, message=str(exc))
            if 'point' in segment: points.append(Gf.Vec3d(*segment['point']))
            roll = float(aim.get('roll_deg', 0))
            mode = segment.get('mode', 'look_at')
            if mode == 'look_at' and points:
                target_pos = sum(points, Gf.Vec3d(0))/len(points)
                smoothing = float(aim.get('smoothing_s', .35))
                if smoothed is None or smoothing <= 0: smoothed = target_pos
                else:
                    omega = 2/max(.001, smoothing)
                    delta = smoothed-target_pos; temp = (velocity+omega*delta)/48
                    decay = math.exp(-omega/48)
                    smoothed = target_pos+(delta+temp)*decay; velocity = (velocity-omega*temp)*decay
                rotation = look_at_rotation_xyz(position, list(smoothed), roll)
                if (smoothed-target_pos).GetLength() > max(.25, (target_pos-Gf.Vec3d(*position)).GetLength()*.1): warn('AIM_LAG', cid, t)
                q = quaternion_xyz(rotation)
                camera.GetFocusDistanceAttr().Set((target_pos-Gf.Vec3d(*position)).GetLength(), tc)
            elif mode == 'along_path':
                rotation = path_tangent_rotation_xyz(tangent, roll); q = quaternion_xyz(rotation)
            else:
                rotation = segment.get('rotation_deg', [segment.get('pitch_deg', 0), segment.get('heading_deg', 0), roll])
                q = interpolate_keys(segment.get('keys', []), t, 'rotation_deg', rotation, quaternion=True)
            shake = rig.get('shake', {})
            dp, dr = evaluate_seeded_shake(t, seed=int(shake.get('seed', 7)), amp=float(shake.get('amount', 0)), freq=float(shake.get('frequency_hz', 2)))
            position = [a+b for a, b in zip(position, dp)]
            q = (Gf.Rotation(q)*Gf.Rotation(quaternion_xyz(dr))).GetQuat()
            if previous_q is not None and Gf.Dot(previous_q, q) < 0: q = -q
            previous_q = q
            tr.Set(Gf.Vec3d(*position), tc); orient.Set(q, tc)
            focal = interpolate_keys(lens_keys, t, 'focal_mm', float(lens.get('focal_mm', 35)))
            if lens.get('dolly_zoom') and points:
                distance = ((sum(points, Gf.Vec3d(0))/len(points))-Gf.Vec3d(*position)).GetLength()
                if initial_subject_distance is None: initial_subject_distance = max(.01, distance)
                focal *= distance/initial_subject_distance
            camera.GetFocalLengthAttr().Set(focal, tc)
            camera.GetFStopAttr().Set(float(lens.get('fstop', 0)) if lens.get('dof_enabled') else 0., tc)
            if not 10 <= focal <= 300: warn('FOCAL_RANGE', cid, t)
            if abs(rotation[0]) > 89 or abs(roll) > 45: warn('ORIENTATION_LIMIT', cid, t)
            for issue in check_frustum(camera, targets, tc, target_specs):
                warn(issue.pop('code'), cid, t, **issue)
            p = Gf.Vec3d(*position)
            near = float(camera.GetClippingRangeAttr().Get()[0])
            for solid, bounds in solids:
                world = UsdGeom.Xformable(solid).ComputeLocalToWorldTransform(tc)
                local = world.GetInverse().Transform(p)
                nearest = Gf.Vec3d(*(max(bounds.GetMin()[j], min(bounds.GetMax()[j], local[j])) for j in range(3)))
                if (p-world.Transform(nearest)).GetLength() < near: warn('CAMERA_SOLID', cid, t, path=str(solid.GetPath()))
            if previous_position is not None:
                v = (p-previous_position)*48
                if v.GetLength() > 15: warn('CAMERA_SPEED', cid, t)
                if previous_velocity is not None and (v-previous_velocity).GetLength()*48 > 20: warn('CAMERA_ACCELERATION', cid, t)
                previous_velocity = v
            previous_position = p
        output = directory / ('cam_'+cid+'.usda')
        output.write_text(layer.GetRootLayer().ExportToString())
        outputs.append(output.name)
    render = Sdf.Layer.CreateAnonymous()
    render.subLayerPaths = outputs+[Path(p).name for p in layers]
    render.timeCodesPerSecond = fps; render.framesPerSecond = fps
    render.startTimeCode = 0; render.endTimeCode = duration*fps
    render.customLayerData = {'active_camera': '/World/Cameras/'+data['active_camera']}
    render.Export(str(directory / 'render_stage.usda'))
    warnings = _group_warnings(warnings)
    (directory / 'camera_warnings.json').write_text(json.dumps(warnings, sort_keys=True, indent=2)+'\n')
    if record_fingerprints: save_fingerprints(directory)
    return {'stage': str(directory / 'render_stage.usda'), 'camera': '/World/Cameras/'+data['active_camera'], 'warnings': warnings,
            'takes': [{'id': cid, 'camera': '/World/Cameras/'+cid, 'frames': str(directory/'takes'/cid/'frame.###.png')} for cid in sorted(data['cameras'])]}


def _ancestors(prim):
    parent = prim.GetParent()
    while parent and not parent.IsPseudoRoot():
        yield parent
        parent = parent.GetParent()
