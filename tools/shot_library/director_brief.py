"""Authoritative director intent. Geometry and seconds never cross the planner boundary."""
from __future__ import annotations

import copy
import json
import math
import re
from bisect import bisect_left
from itertools import accumulate
from pathlib import Path

from plan_model import Beat, TimingCondition

# Broad feasibility bands; the animation manifest still supplies the actual clips.
ACTION_SPEEDS = {
    'walk_to': (0.5, 2.2, 'walk'), 'run_to': (2.2, 7.0, 'run'),
    'jog_to': (1.8, 4.5, 'jog'), 'sprint_to': (4.0, 10.0, 'sprint'),
    'flee_from': (2.2, 7.0, 'run'), 'drive_to': (0.1, 45.0, 'drive'),
}
ALIASES = {'walk': 'walk_to', 'run': 'run_to', 'sprint': 'sprint_to',
           'jog': 'jog_to', 'drive': 'drive_to', 'fight': 'attack',
           'hide': 'take_cover', 'phone call': 'use_phone', 'handshake': 'shake_hands'}


def load_brief(path):
    path = Path(path)
    brief = json.loads(path.read_text()) if path.exists() else {'version': 1, 'tracks': {}, 'paths': {}}
    errors = validate_brief(brief)
    if errors:
        raise ValueError(json.dumps(errors))
    return brief


def path_metrics(path):
    pts = path.get('points', [])
    lengths = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    headings = [math.degrees(math.atan2(b[0]-a[0], -(b[2]-a[2])))
                for a, b in zip(pts, pts[1:])]
    return {'length_m': sum(lengths), 'lengths': lengths, 'headings': headings,
            'height_profile': [p[1] for p in pts]}


def classify_gait(length, duration, actor_type='human'):
    speed = length / duration if duration > 0 else math.inf
    if actor_type == 'vehicle':
        return 'cruise' if speed <= 15 else 'fast'
    return next((name for limit, name in [(1.6, 'walk'), (2.2, 'brisk walk'),
                                         (3.5, 'jog'), (7, 'run')] if speed <= limit), 'sprint')


def validate_brief(brief, actions=None, actors=None):
    """Return structured, user-facing lint. Never include this numeric report in a prompt."""
    issues = []
    def issue(code, **details):
        issues.append({'code': code, **details})
    if not isinstance(brief, dict):
        return [{'code': 'BRIEF_OBJECT_REQUIRED'}]
    if not isinstance(brief.get('tracks', {}), dict) or not isinstance(brief.get('paths', {}), dict):
        return [{'code': 'BRIEF_COLLECTIONS_REQUIRED'}]
    if brief.get('version') != 1:
        issue('BRIEF_VERSION')
    paths = brief.get('paths', {})
    for pid, path in paths.items():
        if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', pid) or not isinstance(path, dict):
            issue('INVALID_PATH', path=str(pid)); continue
        if not isinstance(path.get('owner'), str): issue('PATH_OWNER', path=pid)
        pts = path.get('points', [])
        if not isinstance(pts, list):
            issue('INVALID_PATH', path=pid); continue
        if len(pts) < 2 or any(not isinstance(p, list) or len(p) != 3 or
                              any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in pts):
            issue('INVALID_PATH', path=pid)
        if path.get('speed_profile', 'constant') not in ('constant', 'ease_in_out'):
            issue('INVALID_SPEED_PROFILE', path=pid)
    for actor, track in brief.get('tracks', {}).items():
        if not isinstance(track, dict) or not isinstance(track.get('segments', []), list):
            issue('INVALID_TRACK', actor=actor); continue
        end = 0.0
        seen = set()
        def start_key(seg):
            t = seg.get('t') if isinstance(seg, dict) else None
            return t[0] if isinstance(t, list) and len(t) == 2 and isinstance(t[0], (int, float)) else 0
        for seg in sorted(track.get('segments', []), key=start_key):
            if not isinstance(seg, dict): issue('INVALID_SEGMENT', actor=actor); continue
            sid = seg.get('id')
            window = seg.get('t', [])
            if not isinstance(sid, str): issue('SEGMENT_ID', actor=actor); continue
            if not sid or sid in seen:
                issue('SEGMENT_ID', actor=actor, segment=sid)
            seen.add(sid)
            if not isinstance(window, list) or len(window) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in window) or window[0] < 0 or window[1] <= window[0]:
                issue('INVALID_WINDOW', actor=actor, segment=sid)
                continue
            start, stop = window
            if start < end and not seg.get('flexible') and not any(isinstance(s, dict) and s.get('flexible') for s in track.get('segments', [])):
                issue('OVERLAPPING_WINDOWS', actor=actor, segment=sid, fixes=['trim'])
            end = max(end, stop)
            if seg.get('lock', 'locked') not in ('locked', 'preferred', 'open'):
                issue('INVALID_LOCK', segment=sid)
            alternatives = seg.get('alternatives', {})
            if not isinstance(alternatives, dict) or any(k not in ('variant','target') or not isinstance(v,list) or any(not isinstance(x,str) for x in v) for k,v in alternatives.items()):
                issue('INVALID_ALTERNATIVES', segment=sid)
            tolerance = seg.get('tolerance_s', 0)
            if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance < 0:
                issue('INVALID_TOLERANCE', segment=sid)
            if not isinstance(seg.get('action'), str): issue('UNKNOWN_ACTION', segment=sid); continue
            action = ALIASES.get(seg.get('action'), seg.get('action'))
            if not action or (actions is not None and action not in actions):
                issue('UNKNOWN_ACTION', segment=sid)
            if actors is not None and seg.get('target') and seg['target'] not in actors:
                issue('MISSING_TARGET', segment=sid)
            pid = seg.get('path')
            if pid:
                if not isinstance(pid, str): issue('INVALID_PATH', segment=sid); continue
                path = paths.get(pid)
                if not isinstance(path, dict) or path.get('owner') != actor:
                    issue('PATH_OWNER', actor=actor, segment=sid, path=pid)
                elif action in ACTION_SPEEDS and not any(i.get('path') == pid for i in issues):
                    speed = path_metrics(path)['length_m'] / (stop-start)
                    low, high, _ = ACTION_SPEEDS[action]
                    if not low <= speed <= high:
                        issue('ACTION_SPEED_MISMATCH', segment=sid, speed_mps=speed,
                              fixes=(['lengthen_path', 'shorten_window', 'switch_action'] if speed < low else ['shorten_path', 'stretch_window', 'switch_action']))
    groups = {}
    for actor, track in brief.get('tracks', {}).items():
        for seg in track.get('segments', []):
            if not isinstance(seg, dict) or not seg.get('group_id'): continue
            groups.setdefault(seg['group_id'], []).append((actor, seg))
    for gid, members in groups.items():
        actors_in_group = {actor for actor, _ in members}
        if len(members) < 2 or len(actors_in_group) != len(members):
            issue('GROUP_PARTICIPANTS', group_id=gid)
            continue
        first = members[0][1]
        expected = set(first.get('participants', []))
        if expected != actors_in_group or any(
            set(seg.get('participants', [])) != expected or seg.get('t') != first.get('t')
            or seg.get('action') != first.get('action')
            or seg.get('activity') != first.get('activity') for _, seg in members
        ):
            issue('GROUP_MISMATCH', group_id=gid)
    return issues


def build_digest(brief, scene=None):
    brief = expand_open_windows(brief, synthesize_path_only=False)
    lines = []
    for actor, track in sorted(brief.get('tracks', {}).items()):
        previous = None
        for seg in sorted(track.get('segments', []), key=lambda s: s['t'][0]):
            text = f"{actor}, segment {seg['id']} ({seg.get('lock', 'locked').upper()}"
            text += f", follows {previous}" if previous else ''
            text += f"): {seg['action']}"
            if seg.get('target'):
                text += f" targeting {seg['target']}"
            if seg.get('group_id'):
                text += f" in shared action {seg['group_id']} with {', '.join(seg.get('participants', []))}"
            if seg.get('path'):
                path = brief['paths'][seg['path']]
                text += f" along drawn route {seg['path']}"
                text += describe_route(path, seg['t'], scene)
            if seg['action'] in ('attack','push','shove','shake_hands'):
                text += '. Contact range is enforced by the system'
            # Notes are prose, not a loophole for leaking geometry or numeric timing.
            if seg.get('note'):
                text += '. Style note: ' + re.sub(r'[-+]?\d+(?:\.\d+)?', '[value omitted]', seg['note'])
            if seg.get('alternatives'):
                text += '. Allowed alternatives: ' + json.dumps(seg['alternatives'], sort_keys=True)
            lines.append(text + '.')
            previous = seg['id']
    assigned_paths = {seg.get('path') for track in brief.get('tracks', {}).values()
                      for seg in track.get('segments', [])}
    for pid, path in sorted(brief.get('paths', {}).items()):
        if pid not in assigned_paths:
            lines.append(f"Available drawn route {pid} for {path['owner']}; use it only if the prompt calls for that movement.")
    return '\n'.join(lines)


FORBIDDEN_FIELDS = {'position', 'points', 'coordinates', 'facing_deg', 'at_time', 'time',
                    'duration_s', 'start_s', 'end_s', 't', 'tolerance_s', 'distance_m', 'offset'}


def guard_symbolic(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_FIELDS:
                raise ValueError(f'Numeric/geometry field {key} is forbidden in catalog calls')
            guard_symbolic(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            guard_symbolic(child)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        raise ValueError('Catalog calls must use named levels and segment IDs, never numbers')
    elif isinstance(value, str) and (re.search(r'(?:pos|point|coord)\s*\(|[\[(,]\s*[-+]?\d+(?:\.\d+)?\s*[,\])]', value, re.IGNORECASE) or re.fullmatch(r'[-+]?\d+(?:\.\d+)?(?:\s*(?:s|m|deg|seconds|metres))?', value.strip())):
        raise ValueError('Numeric literals are forbidden in catalog calls')


def merge_brief(brief, plan):
    """Copy intent, compile locked/default preferred segments, and resolve during IDs.

    Timing is exact even for preferred/open windows. Flexibility never follows from lock level.
    """
    result = copy.deepcopy(plan)
    # A route drawn for an actor participating in the prompt is part of that
    # shot. A route for an unrelated background actor stays an optional guide.
    # Authored timeline segments decide their own paths and windows.
    selected_paths = {b.params.get('path_id') for b in result.beats
                      if b.function == 'follow_path'}
    assigned_paths = {s.get('path') for track in brief.get('tracks', {}).values()
                      for s in track.get('segments', [])}
    timeline_actors = {actor for actor, track in brief.get('tracks', {}).items()
                       if track.get('segments')}
    planned_actors = {b.actor_id for b in result.beats} | {s.actor_id for s in result.setup}
    path_only = not bool(result.beats or result.setup or timeline_actors)
    applicable_paths = {
        pid for pid, path in brief.get('paths', {}).items()
        if pid not in selected_paths and pid not in assigned_paths
        and path['owner'] not in timeline_actors
        and (path_only or path['owner'] in planned_actors)
    }
    brief = expand_open_windows(brief, synthesize_path_only=True,
                                path_ids=applicable_paths)
    segments = {(a, s['id']): s for a, tr in brief.get('tracks', {}).items() for s in tr.get('segments', [])}
    claimed = set()
    beats = []
    open_beat_by_actor = {}
    for beat in result.beats:
        if beat.function == 'follow_path':
            pid = beat.params['path_id']
            if pid not in brief.get('paths', {}) or brief['paths'][pid]['owner'] != beat.actor_id:
                raise ValueError(f'Unknown or foreign path {pid}')
            matches = [s for (a, _), s in segments.items() if a == beat.actor_id and s.get('path') == pid]
            if matches:
                beat.params['during'] = matches[0]['id']
                beat.params['action'] = matches[0]['action']
            else:
                path = brief['paths'][pid]
                style = beat.params.get('style') or ('drive' if path.get('actor_type') == 'vehicle' else 'walk')
                action = {'run': 'run_to', 'jog': 'jog_to', 'drive': 'drive_to'}.get(style, 'walk_to')
                duration = max(.2, path_metrics(path)['length_m'] / {'run': 4.2, 'jog': 2.6, 'drive': 8}.get(style, 1.4))
                beat.function = action
                beat.params.update(brief_path=copy.deepcopy(path), action=action)
                beat.duration_condition = TimingCondition('for_duration', duration)
        sid = beat.params.get('during')
        if sid:
            seg = segments.get((beat.actor_id, sid))
            if seg is None:
                raise ValueError(f'Unknown segment {beat.actor_id}.{sid}')
            if seg.get('lock', 'locked') == 'locked':
                continue
            if (beat.actor_id, sid) in claimed:
                raise ValueError(f'Duplicate segment choice {sid}')
            action = beat.params.get('action', beat.function)
            if seg.get('lock') == 'preferred' and action != seg['action']:
                raise ValueError(f'Preferred action cannot be changed: {sid}')
            for field in ('variant', 'target'):
                choice = beat.params.get(field)
                if choice and choice != seg.get(field) and choice not in seg.get('alternatives', {}).get(field, []):
                    if seg.get('lock') == 'preferred':
                        raise ValueError(f'Unlisted preferred {field}: {choice}')
            seg = {**seg, 'action': action, **{k: beat.params[k] for k in ('variant', 'target') if k in beat.params}}
            claimed.add((beat.actor_id, sid))
            beats.append(_segment_beat(beat.actor_id, seg, brief))
        else:
            track = brief.get('tracks', {}).get(beat.actor_id, {})
            if track.get('segments') and not beat.params.get('brief_path') and not any(s.get('path_only') for s in track['segments']):
                # An explicit timeline wins its windows, but does not erase
                # additional prompt intent. Place unspecified prompt work after
                # the last authored segment in that actor's lane.
                if not beat.start_condition or beat.start_condition.type != 'after':
                    anchor = open_beat_by_actor.get(beat.actor_id)
                    if not anchor:
                        authored = [s for s in track['segments'] if not s.get('implicit')]
                        if authored:
                            last = max(authored, key=lambda s: s['t'][1])
                            anchor = f"{beat.actor_id}.{last['id']}"
                    if anchor:
                        beat.start_condition = TimingCondition('after', anchor + '.finished')
            open_beat_by_actor[beat.actor_id] = beat.beat_id
            beats.append(beat)
    for (actor, sid), seg in segments.items():
        if (actor, sid) not in claimed:
            beats.append(_segment_beat(actor, seg, brief))
    from director_solver import prepare_timing
    # Keep prompt beat order: it encodes per-actor sequence and grouped calls.
    # Stable sorting still places authored windows before open prompt work.
    result.beats = sorted(beats, key=lambda b: (0, b.params['exact_window'][0], b.actor_id)
                          if b.params.get('exact_window') else (1, 0, ''))
    event_names = {'arrived','stopped','started','finished','departed'}
    ids = {b.beat_id for b in result.beats}
    for beat in result.beats:
        condition = beat.start_condition
        if condition and condition.type in ('after','with'):
            ref = str(condition.value)
            if ref.split('.',1)[0] in brief.get('tracks', {}) and '.' in ref:
                base, suffix = ref.rsplit('.',1)
                if ref not in ids and not (suffix in event_names and (base in ids or base in brief['tracks'])):
                    raise ValueError('Unknown segment completion event '+ref)
    return prepare_timing(result, brief)


def _segment_beat(actor, seg, brief):
    action = ALIASES.get(seg['action'], seg['action'])
    params = {k: copy.deepcopy(seg[k]) for k in ('target', 'variant', 'group_id', 'participants', 'activity') if k in seg}
    params.update(brief_segment=seg['id'], exact_window=list(seg['t']), action=action, implicit_gap=seg.get('implicit', False))
    if action == 'take_cover' and not seg.get('path'):
        params['in_place_clip'] = 'hiding_grab'
    if seg.get('path'):
        params['brief_path'] = copy.deepcopy(brief['paths'][seg['path']])
        if action in ACTION_SPEEDS:
            params['brief_path']['speed_band'] = list(ACTION_SPEEDS[action][:2])
    return Beat(f"{actor}.{seg['id']}", action, actor, params,
                TimingCondition('at', seg['t'][0]),
                TimingCondition('for_duration', seg['t'][1]-seg['t'][0]))


def resolve_path_for_actor(path, t_window, sample_hz=48):
    """Sample the polyline, retaining every vertex at its exact traversal time.

    Uniform samples alone cut corners when roots interpolate between keys,
    invalidating the swept-disc route that the crowd planner checked.
    """
    pts = path['points']
    metrics = path_metrics(path)
    total = metrics['length_m']
    cumulative = list(accumulate(metrics['lengths']))
    start, end = t_window
    count = max(1, math.ceil((end-start)*sample_hz))
    def progress(u):
        if path.get('speed_profile') == 'ease_in_out':
            if path.get('speed_band'):
                average = total/(end-start)
                low, high = path['speed_band']
                strength = max(0., min(.5, 1-low/max(1e-9, average), high/max(1e-9, average)-1))
                u = u-strength*math.sin(2*math.pi*u)/(2*math.pi)
            else:
                u = u*u*(3-2*u)
        return u

    fractions = {frame/count for frame in range(count+1)}
    if total > 0:
        for distance in cumulative[:-1]:
            fraction = distance/total
            if path.get('speed_profile') == 'ease_in_out':
                low, high = 0., 1.
                # Invert the monotone speed profile to floating-point precision.
                while high-low > math.ulp(1.):
                    middle = (low+high)/2
                    if progress(middle) < fraction:
                        low = middle
                    else:
                        high = middle
                fraction = (low+high)/2
            fractions.add(fraction)
    samples = []
    previous_segment = None
    for fraction in sorted(fractions):
        distance = progress(fraction)*total
        pos, heading = list(pts[-1]), metrics['headings'][-1]
        i = 0 if len(cumulative) == 1 else bisect_left(cumulative, distance)
        while i < len(cumulative) and metrics['lengths'][i] <= 0:
            i += 1
        if i < len(cumulative):
            traversed = cumulative[i - 1] if i else 0.
            alpha = (distance - traversed) / metrics['lengths'][i]
            pos = [a+(b-a)*alpha for a, b in zip(pts[i], pts[i+1])]
            heading = metrics['headings'][i]
        if samples and i != previous_segment:
            previous_t, previous_pos, previous_heading = samples[-1]
            dx, dz = pos[0] - previous_pos[0], pos[2] - previous_pos[2]
            if math.hypot(dx, dz) > 1e-9:
                # The baked root interpolates between these samples. At a
                # corner its actual travel direction is this chord, rather
                # than the old segment's tangent.
                samples[-1] = (previous_t, previous_pos, math.degrees(math.atan2(dx, -dz)))
        samples.append((start+(end-start)*fraction, pos, heading))
        previous_segment = i
    return samples


def author_path_guides(stage, brief):
    from pxr import Gf, UsdGeom, Vt
    for pid, path in sorted(brief.get('paths', {}).items()):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', pid):
            raise ValueError(f'Invalid USD path ID: {pid}')
        curve = UsdGeom.BasisCurves.Define(stage, '/World/Paths/'+pid)
        curve.CreateTypeAttr('linear')
        curve.CreateCurveVertexCountsAttr([len(path['points'])])
        curve.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in path['points']]))
        curve.CreatePurposeAttr('guide')


def check_conformance(brief, performances, tolerance_m=0.05):
    errors = []
    for actor, track in brief.get('tracks', {}).items():
        perf = performances.get(actor)
        for seg in track.get('segments', []):
            if seg.get('lock', 'locked') != 'locked':
                continue
            if perf is None:
                errors.append({'code': 'MISSING_PERFORMANCE', 'actor': actor})
                continue
            start, end = seg['t']
            if seg.get('path'):
                check_path = copy.deepcopy(brief['paths'][seg['path']])
                action = ALIASES.get(seg['action'], seg['action'])
                if action in ACTION_SPEEDS: check_path['speed_band'] = list(ACTION_SPEEDS[action][:2])
                for t, expected, _ in resolve_path_for_actor(check_path, seg['t']):
                    if math.dist(perf.sample(t)[0], expected) > tolerance_m:
                        errors.append({'code': 'PATH_CONFORMANCE', 'segment': seg['id'], 'time_s': t})
                        break
            actions = sorted([a for a in perf.actions if a['end_s'] > start and a['start_s'] < end], key=lambda a: a['start_s'])
            covered = start
            for action in actions:
                if action['start_s'] > covered + .001:
                    break
                covered = max(covered, action['end_s'])
            action_name = ALIASES.get(seg['action'], seg['action'])
            if action_name in ACTION_SPEEDS and seg.get('path'):
                low, high, gait = ACTION_SPEEDS[action_name]
                expected_clip = seg.get('variant') or ('run' if gait in ('jog', 'sprint') else gait)
                if not perf.is_vehicle and any(a['clip'] != expected_clip for a in actions):
                    errors.append({'code': 'CLIP_CONFORMANCE', 'segment': seg['id']})
                # Gait-bounded easing retains the selected action across the entire window.
                for i in range(max(1, math.ceil((end-start)*48))):
                    t = start+i/48
                    stop = min(end, t+1/48)
                    speed = math.dist(perf.sample(t)[0], perf.sample(stop)[0])/max(1e-9, stop-t)
                    if speed < low-.05 or speed > high+.05:
                        errors.append({'code': 'SPEED_CONFORMANCE', 'segment': seg['id'], 'time_s': t})
                        break
            if not perf.is_vehicle and covered < end - .001:
                errors.append({'code': 'WINDOW_CONFORMANCE', 'segment': seg['id']})
    return errors


def solve_timing(brief, plan, actor_initial_states=None, fps=24):
    """Merge symbolic choices into exact user windows using the shared event solver."""
    from timeline_resolver import TimelineResolver
    merged = merge_brief(brief, plan)
    states = actor_initial_states or {a: {} for a in brief.get('tracks', {})}
    return TimelineResolver(fps=fps).resolve(merged, states)


def check_turning_radius(path, vehicle_asset):
    """Circumcircle radius at corners versus the bicycle-model steering limit."""
    wheelbase = float(vehicle_asset.get('wheelbase_m', 2.7))
    steering = float(vehicle_asset.get('max_steering_deg', 35))
    minimum = wheelbase / math.tan(math.radians(steering))
    warnings = []
    for index, (a, b, c) in enumerate(zip(path['points'], path['points'][1:], path['points'][2:]), 1):
        ab = math.hypot(b[0]-a[0], b[2]-a[2])
        bc = math.hypot(c[0]-b[0], c[2]-b[2])
        ac = math.hypot(c[0]-a[0], c[2]-a[2])
        cross = abs((b[0]-a[0])*(c[2]-a[2])-(b[2]-a[2])*(c[0]-a[0]))
        radius = ab*bc*ac/(2*cross) if cross > 1e-9 else math.inf
        if radius < minimum:
            warnings.append({'code': 'TURNING_RADIUS', 'waypoint': index, 'radius_m': radius, 'minimum_radius_m': minimum})
    return warnings


def route_around(path, stage, manifest_db, times=(0,), radius=.45):
    """Explicit, non-mutating one-click repair using the existing inflated-footprint A*."""
    import plan_path
    points = path['points']
    repaired = copy.deepcopy(path)
    result = []
    for start, end in zip(points, points[1:]):
        segment = plan_path.plan(stage, manifest_db, start, end, times, radius=radius,
                                 skip=(path.get('owner', ''),))
        # The routing API returns a waypoint array (or (array, diagnostics) on older versions).
        if isinstance(segment, tuple):
            segment = segment[0]
        segment = [list(map(float, p)) for p in segment]
        result.extend(segment if not result else segment[1:])
    repaired['points'] = result
    return repaired


def expand_open_windows(brief, synthesize_path_only=True, path_ids=None):
    """Give implicit gaps stable IDs and compile every authored route."""
    result = copy.deepcopy(brief)
    tracks = result.setdefault('tracks', {})
    duration = float(result.get('duration_s', max((s['t'][1] for tr in tracks.values() for s in tr.get('segments', [])), default=0)))
    if synthesize_path_only:
        assigned_paths = {s.get('path') for tr in tracks.values() for s in tr.get('segments', [])}
        for pid, path in result.get('paths', {}).items():
            if pid in assigned_paths or (path_ids is not None and pid not in path_ids):
                continue
            actor = path['owner']
            is_vehicle = path.get('actor_type') == 'vehicle' or actor.startswith('car_')
            action, nominal = ('drive_to', 8.) if is_vehicle else ('walk_to', 1.4)
            travel = max(.2, path_metrics(path)['length_m']/nominal)
            track = tracks.setdefault(actor, {})
            if path.get('asset_id'): track.setdefault('asset_id', path['asset_id'])
            if path.get('actor_path'):
                track.setdefault('actor_path', path['actor_path'])
                track.setdefault('label', path['actor_path'].rsplit('/', 1)[-1])
            segments = track.setdefault('segments', [])
            start = max((s['t'][1] for s in segments), default=0.)
            segments.append({'id': 'route_'+pid, 't': [start, start+travel], 'action': action, 'path': pid, 'lock': path.get('lock', 'locked'), 'path_only': True})
    for actor, track in tracks.items():
        cursor, segments = 0., []
        previous = None
        for seg in sorted(track.get('segments', []), key=lambda s: s['t'][0]):
            if seg['t'][0] > cursor:
                segments.append({'id': 'gap_before_'+seg['id'], 't': [cursor, seg['t'][0]], 'action': 'wait', 'lock': 'open', 'implicit': True})
            segments.append(seg); cursor = max(cursor, seg['t'][1]); previous = seg['id']
        if previous and duration > cursor:
            segments.append({'id': 'gap_after_'+previous, 't': [cursor, duration], 'action': 'wait', 'lock': 'open', 'implicit': True})
        track['segments'] = segments
    return result


def symbolic_feedback(error):
    """Keep identifiers and qualitative recovery advice; numeric diagnostics stay local."""
    allowed = {'error_code','code','actor_id','beat_id','stage','message','suggestions','instruction'}
    def prose(text):
        return re.sub(r'(?<![A-Za-z_0-9#])[-+]?\d+(?:\.\d+)?', '[value omitted]', str(text))
    if not isinstance(error, dict): return {'message': prose(error)}
    result = {}
    for key, value in error.items():
        if key not in allowed or 'camera' in key: continue
        result[key] = [prose(x) for x in value] if isinstance(value, list) else prose(value)
    if not result: result['message'] = 'An authored constraint is infeasible. Choose another symbolic action or report a brief conflict.'
    return result


def enrich_brief_choices(brief, manifest):
    """Derive discrete variant menus without changing authored geometry or timing."""
    result = copy.deepcopy(brief)
    clips = manifest.get('clips', {})
    for track in result.get('tracks', {}).values():
        for seg in track.get('segments', []):
            if seg.get('lock') != 'preferred': continue
            action = ALIASES.get(seg['action'], seg['action'])
            alternatives = seg.setdefault('alternatives', {})
            if 'variant' in alternatives: continue
            if action in ACTION_SPEEDS:
                gait = ACTION_SPEEDS[action][2]
                candidates = [name for name in clips if gait in name.split('_') and not any(word in name for word in ('back', 'stairs', 'turn', 'jump', 'climb', 'dive', 'wall'))]
            elif action == 'attack':
                candidates = [name for name, info in clips.items() if info.get('category') == 'combat' and info.get('strike_limb')]
            elif action in ('converse', 'argue', 'greet'):
                candidates = [name for name in clips if name.startswith(('talking_', 'waving', 'standing_arguing'))]
            else:
                candidates = [action] if action in clips else []
            if candidates: alternatives['variant'] = sorted(candidates)
    return result


def describe_route(path, window, scene=None):
    """The sole geometry-to-language boundary: qualitative landmarks, gait and extent."""
    points = path['points']
    scene = scene or {}
    landmarks = []
    for zone in scene.get('zones', []):
        low, high = zone.get('min'), zone.get('max')
        if not low or not high: continue
        center = [(a+b)/2 for a,b in zip(low,high)]
        for name, p in [('center', center), ('north',[center[0],center[1],low[2]]),
                        ('south',[center[0],center[1],high[2]]), ('west',[low[0],center[1],center[2]]), ('east',[high[0],center[1],center[2]])]:
            landmarks.append((zone['id']+'.'+name,p))
    for obj in scene.get('objects', []):
        if obj.get('position') and obj.get('name') and obj.get('actor_id') != path.get('owner'):
            landmarks.append((obj['name'],obj['position']))
    def location(point):
        if not landmarks: return None
        name, p = min(landmarks,key=lambda item:math.hypot(item[1][0]-point[0],item[1][2]-point[2]))
        return name if math.hypot(p[0]-point[0],p[2]-point[2]) <= 3 else None
    first, last = location(points[0]), location(points[-1])
    route = f' from near {first} to near {last}' if first and last and first != last else f' ending near {last}' if last else ''
    if not route:
        dx, dz = points[-1][0]-points[0][0], points[-1][2]-points[0][2]
        if math.hypot(dx,dz)>.01:
            direction=['north','east','south','west'][int((math.degrees(math.atan2(dx,-dz))+45)%360//90)]
            route=f', heading {direction}'
    length=path_metrics(path)['length_m']
    bucket=next((name for upper,name in [(3,'brief'),(8,'short'),(20,'medium')] if length<upper),'long')
    gait=classify_gait(length,window[1]-window[0],path.get('actor_type','human'))
    return route+f'. {bucket.capitalize()} route; {gait}'
