"""Numeric constraints for director windows and contact, isolated from symbolic planning."""
from __future__ import annotations
import copy
import heapq
import math


# Keep interaction routing bounded when many occupied meeting areas would
# otherwise multiply center, angle, and delay choices. Callers can override
# these search budgets when a larger offline search is appropriate.
DEFAULT_ROUTE_EXPANSION_BUDGET = 2048
DEFAULT_INTERACTION_ROUTE_ATTEMPTS = 48
INTERACTION_CENTER_SEARCH_DIVISOR = 4
# Meeting-area candidates are also generated on rings around the actors' natural
# midpoint, so a crowded midpoint can be nudged aside instead of abandoned for a
# far-away standing spot. Ring spacing follows the formation radius; this only
# bounds how many rings are generated.
CENTER_NUDGE_RINGS = 3
CENTER_NUDGE_DIRECTIONS = 8


def _route_clear_of_actors(route, start_time, speed, actor, participants,
                           trajectories, sample_trajectory, clearance,
                           level_tolerance, sample_step):
    """Check a planned approach against other actors throughout its travel.

    Actor positions are sampled at the time the approach reaches each point,
    rather than at one snapshot of the scene. ``clearance`` is either one
    distance or a callable ``(other_actor_id, time) -> distance``.
    """
    elapsed = 0.0
    for source, destination in zip(route, route[1:]):
        length = math.hypot(destination[0] - source[0], destination[2] - source[2])
        steps = max(1, math.ceil(length / sample_step))
        for step in range(steps + 1):
            fraction = step / steps
            point = [source[axis] + fraction * (destination[axis] - source[axis])
                     for axis in range(3)]
            when = start_time + (elapsed + fraction * length) / speed
            for other, trajectory in trajectories.items():
                if other == actor or other in participants:
                    continue
                other_position = sample_trajectory(trajectory, when)
                needed = clearance(other, when) if callable(clearance) else clearance
                if (abs(point[1] - other_position[1]) <= level_tolerance
                        and math.hypot(point[0] - other_position[0],
                                       point[2] - other_position[2]) < needed):
                    return False
        elapsed += length
    return True


def _segment_clear_of_discs(a, b, obstacles, clearance_margin):
    dx, dz = b[0] - a[0], b[2] - a[2]
    length_sq = dx * dx + dz * dz
    for obstacle in obstacles:
        radius = obstacle["radius"] + clearance_margin
        cx, cz = obstacle["cx"], obstacle["cz"]
        if length_sq <= 1e-12:
            distance = math.hypot(a[0] - cx, a[2] - cz)
        else:
            fraction = max(0.0, min(1.0,
                ((cx - a[0]) * dx + (cz - a[2]) * dz) / length_sq))
            distance = math.hypot(a[0] + fraction * dx - cx,
                                  a[2] + fraction * dz - cz)
        if distance + 1e-6 < radius:
            return False
    return True


def _route_clear_of_discs(route, obstacles, supported, clearance_margin=0.02):
    """Check every segment against keep-out discs and measured support."""
    def segment_clear(a, b):
        return (_segment_clear_of_discs(a, b, obstacles, clearance_margin)
                and supported([a, b]))

    return bool(route) and supported([route[0]]) and all(
        segment_clear(a, b) for a, b in zip(route, route[1:] or route))


def _clear_route_around_discs(start, goal, obstacles, supported,
                              clearance_margin=0.02,
                              max_expansions=DEFAULT_ROUTE_EXPANSION_BUDGET):
    """Find a supported route with bounded-grid A* around obstacle footprints.

    The former all-pairs visibility graph tested every ring vertex against
    every other vertex, which became prohibitive for crowded interactions.
    Grid bounds follow the requested route and supplied obstacles. Every A*
    edge still uses the exact disc and support checks used by the caller.
    """
    from support_surfaces import ROUTE_SAMPLE_STEP_M

    if _route_clear_of_discs([start, goal], obstacles, supported,
                             clearance_margin=clearance_margin):
        return [list(start), list(goal)]

    length = math.hypot(goal[0] - start[0], goal[2] - start[2])
    cell = max(ROUTE_SAMPLE_STEP_M / 4.0, min(ROUTE_SAMPLE_STEP_M, length / 4.0))
    largest_radius = max((obstacle["radius"] + clearance_margin for obstacle in obstacles),
                         default=cell)
    padding = max(2.0 * largest_radius, 2.0 * cell)
    x0 = min([start[0], goal[0]] + [o["cx"] - o["radius"] for o in obstacles]) - padding
    x1 = max([start[0], goal[0]] + [o["cx"] + o["radius"] for o in obstacles]) + padding
    z0 = min([start[2], goal[2]] + [o["cz"] - o["radius"] for o in obstacles]) - padding
    z1 = max([start[2], goal[2]] + [o["cz"] + o["radius"] for o in obstacles]) + padding
    nx = max(1, math.ceil((x1 - x0) / cell) + 1)
    nz = max(1, math.ceil((z1 - z0) / cell) + 1)
    index_of = lambda p: (round((p[0] - x0) / cell), round((p[2] - z0) / cell))
    source, target = index_of(start), index_of(goal)
    if source == target:
        return None

    def point_at(index):
        if index == source:
            return list(start)
        if index == target:
            return list(goal)
        return [x0 + index[0] * cell, start[1], z0 + index[1] * cell]

    def heuristic(index):
        return math.hypot((index[0] - target[0]) * cell,
                          (index[1] - target[1]) * cell)

    costs = {source: 0.0}
    parents = {}
    frontier = [(heuristic(source), 0.0, source)]
    edges = {}
    support_cache = {}
    expanded = 0

    def point_supported(index):
        if index not in support_cache:
            support_cache[index] = supported([point_at(index)])
        return support_cache[index]

    while frontier:
        _, cost, node = heapq.heappop(frontier)
        if cost > costs.get(node, math.inf):
            continue
        expanded += 1
        if expanded > max_expansions:
            return None
        if node == target:
            route = []
            while node in parents:
                route.append(point_at(node))
                node = parents[node]
            route.append(list(start))
            route.reverse()
            # Remove collinear grid points without introducing unchecked cuts.
            compact = [route[0]]
            for index in range(1, len(route) - 1):
                a, b, c = compact[-1], route[index], route[index + 1]
                cross = (b[0] - a[0]) * (c[2] - b[2]) - (b[2] - a[2]) * (c[0] - b[0])
                if abs(cross) > 1e-9:
                    compact.append(b)
            compact.append(route[-1])
            return compact
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            neighbor = (node[0] + dx, node[1] + dz)
            if not (0 <= neighbor[0] < nx and 0 <= neighbor[1] < nz):
                continue
            edge = (node, neighbor) if node < neighbor else (neighbor, node)
            if edge not in edges:
                a, b = point_at(node), point_at(neighbor)
                if not point_supported(node) or not point_supported(neighbor):
                    edges[edge] = False
                elif (math.hypot(b[0] - a[0], b[2] - a[2]) > ROUTE_SAMPLE_STEP_M + 1e-9
                      and not supported([a, b])):
                    edges[edge] = False
                else:
                    edges[edge] = _segment_clear_of_discs(
                        a, b, obstacles, clearance_margin)
            if not edges[edge]:
                continue
            distance = math.hypot(point_at(node)[0] - point_at(neighbor)[0],
                                  point_at(node)[2] - point_at(neighbor)[2])
            candidate = cost + distance
            if candidate + 1e-9 < costs.get(neighbor, math.inf):
                costs[neighbor] = candidate
                parents[neighbor] = node
                heapq.heappush(frontier, (candidate + heuristic(neighbor), candidate, neighbor))
    return None


def _interaction_support_details(group_id, group_beats, participants, positions,
                                 initial_states, center_ref, center, spacing,
                                 supports, level_supports, reason):
    """Expose the geometry behind a failed meeting without dumping every face."""
    from support_surfaces import contains

    actors = []
    for actor in participants:
        position = positions[actor]
        candidates = []
        for surface in supports:
            polygon = surface['polygon_xz']
            centroid_x = sum(vertex[0] for vertex in polygon) / len(polygon)
            centroid_z = sum(vertex[1] for vertex in polygon) / len(polygon)
            covers_xz = contains(surface, [position[0], surface['height_m'], position[2]])
            candidates.append((
                (not covers_xz, abs(surface['height_m'] - position[1]),
                 math.hypot(centroid_x - position[0], centroid_z - position[2])),
                {
                    'prim_path': surface['prim_path'],
                    'height_m': surface['height_m'],
                    'vertical_gap_m': surface['height_m'] - position[1],
                    'covers_xz': covers_xz,
                },
            ))
        candidates.sort(key=lambda item: item[0])
        actors.append({
            'actor_id': actor,
            'at_reference': initial_states[actor].get('at_reference'),
            'position': position,
            'level_supports': [surface['prim_path'] for surface in level_supports
                               if contains(surface, position)],
            'nearest_surfaces': [item[1] for item in candidates[:3]],
        })
    return {
        'group_id': group_id,
        'beat_ids': [beat.beat_id for beat in group_beats],
        'reason': reason,
        'participants': list(participants),
        'spacing': spacing,
        'center_reference': center_ref,
        'resolved_center': center,
        'support_surface_count': len(supports),
        'level_support_count': len(level_supports),
        'actors': actors,
    }


def prepare_timing(plan, brief):
    """Propagate lane interval bounds, moving only explicitly flexible windows.

    Backward upper-bound propagation permits an earlier flexible segment to move
    earlier to make room for a fixed successor. Forward scheduling chooses the
    closest feasible start to each author's requested start.
    """
    result = copy.deepcopy(plan)
    definitions = {(actor, seg['id']): seg for actor, track in brief.get('tracks', {}).items() for seg in track.get('segments', [])}
    lanes = {}
    for beat in result.beats:
        if beat.params.get('brief_segment') and not beat.params.get('implicit_gap'): lanes.setdefault(beat.actor_id, []).append(beat)
    for actor, beats in lanes.items():
        beats.sort(key=lambda b: b.params['exact_window'][0])
        bounds = []
        for beat in beats:
            seg = definitions[actor, beat.params['brief_segment']]
            start, end = seg['t']
            tolerance = float(seg.get('tolerance_s', 0)) if seg.get('flexible', False) else 0.
            bounds.append([max(0, start-tolerance), start+tolerance, end-start, start])
        for i in range(len(bounds)-2, -1, -1):
            bounds[i][1] = min(bounds[i][1], bounds[i+1][1]-bounds[i][2])
        previous_end = 0.
        for beat, (lower, upper, duration, authored) in zip(beats, bounds):
            lower = max(lower, previous_end)
            if lower > upper+1e-9: raise ValueError(f'Infeasible lane constraints for {actor}.{beat.params["brief_segment"]}')
            start = max(lower, min(upper, authored))
            if abs(start) < 1e-9: start = 0.
            beat.params['authored_window'] = list(beat.params['exact_window'])
            beat.params['exact_window'] = [start, start+duration]
            beat.start_condition.value = start
            beat.duration_condition.value = duration
            previous_end = start+duration
    shared_windows = {}
    for beat in result.beats:
        gid = beat.params.get('group_id')
        if not gid or not beat.params.get('exact_window'): continue
        window = beat.params['exact_window']
        if gid in shared_windows and any(abs(a-b) > 1e-8 for a, b in zip(window, shared_windows[gid])):
            raise ValueError(f'Shared action {gid} cannot fit the same window for every participant')
        shared_windows[gid] = window
    windows = {(b.actor_id, b.params.get('brief_segment')): b.params['exact_window'] for b in result.beats if b.params.get('exact_window') and not b.params.get('implicit_gap')}
    kept = []
    for beat in result.beats:
        if beat.params.get('implicit_gap'):
            sid = beat.params['brief_segment']
            if sid.startswith('gap_before_'):
                following = windows.get((beat.actor_id, sid[len('gap_before_'):]))
                end = following[0] if following else beat.params['exact_window'][1]
                start = max((w[1] for (a, _), w in windows.items() if a == beat.actor_id and w[1] <= end), default=0.)
            else:
                previous = windows.get((beat.actor_id, sid[len('gap_after_'):]))
                start = previous[1] if previous else beat.params['exact_window'][0]
                end = beat.params['exact_window'][1]
            if end <= start+1e-8: continue
            beat.params['exact_window'] = [start, end]
            beat.start_condition.value = start; beat.duration_condition.value = end-start
        kept.append(beat)
    # A prompt's beat order carries per-character dependencies. Keep that order
    # among open beats while placing authored windows on their fixed timeline.
    result.beats = sorted(kept, key=lambda b: (0, b.params['exact_window'][0], b.actor_id)
                          if b.params.get('exact_window') else (1, 0, ''))
    return result


def effective_brief(brief, timeline):
    data = copy.deepcopy(brief)
    windows = {(b.actor_id, b.params.get('brief_segment')): [b.start_time, b.end_time] for b in timeline.beats if b.params.get('brief_segment')}
    for actor, track in data.get('tracks', {}).items():
        for seg in track.get('segments', []):
            if (actor, seg['id']) in windows: seg['t'] = windows[actor, seg['id']]
    return data


def link_prompt_interactions(plan, initial_states):
    """Turn reciprocal prompt actions into shared beats before route scheduling.

    Explicitly timed or brief-locked exchanges keep their authored relationship.
    A one-sided physical contact or attack still gives the other character an
    action in the same interval, so the target does not freeze during contact.
    """
    from plan_model import Beat
    result = copy.deepcopy(plan)
    labels = {str(state.get('label', '')).lower(): actor for actor, state in initial_states.items()
              if state.get('label')}

    def actor_id(value):
        return labels.get(str(value).lower(), value)

    for beat in result.beats:
        for key in ('target', 'threat', 'look_at'):
            if isinstance(beat.params.get(key), str):
                beat.params[key] = actor_id(beat.params[key])

    used_beat_ids = {beat.beat_id for beat in result.beats}
    used_group_ids = {beat.params.get('group_id') for beat in result.beats
                      if beat.params.get('group_id')}

    def unique_id(base, used):
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f'{base}_{suffix}'
            suffix += 1
        used.add(candidate)
        return candidate

    def compatible_timing(first, second):
        if set(first.params.get('wait_for', [])) != set(second.params.get('wait_for', [])):
            return False
        a, b = first.start_condition, second.start_condition
        if a is None or b is None:
            return a is b
        if a.type != b.type:
            return False
        if a.type == 'at':
            return abs(float(a.value) - float(b.value)) < 1e-8
        return a.type == 'after' and a.value == b.value

    def shared(first, second, spacing):
        group = unique_id('prompt_interaction_' + first.beat_id, used_group_ids)
        participants = [first.actor_id, second.actor_id]
        for beat in (first, second):
            beat.params['group_id'] = group
            beat.params['participants'] = participants
            beat.params['spacing'] = spacing

    i = 0
    while i < len(result.beats):
        beat = result.beats[i]
        target = beat.params.get('target')
        target_is_character = (isinstance(target, str) and target in initial_states
                               and not initial_states[target].get('is_vehicle'))
        next_beat = result.beats[i + 1] if i + 1 < len(result.beats) else None
        if beat.params.get('exact_window') or beat.params.get('group_id') or not target_is_character:
            i += 1
            continue

        if beat.function in ('hug_or_kiss', 'shake_hands'):
            reciprocal = (next_beat is not None and next_beat.function == beat.function
                          and next_beat.actor_id == target
                          and next_beat.params.get('target') == beat.actor_id
                          and not next_beat.params.get('exact_window')
                          and not next_beat.params.get('group_id')
                          and compatible_timing(beat, next_beat))
            if reciprocal:
                partner = next_beat
            else:
                partner = Beat(beat_id=unique_id(beat.beat_id + '_partner', used_beat_ids), function=beat.function,
                               actor_id=target,
                               params={**beat.params, 'target': beat.actor_id},
                               start_condition=copy.deepcopy(beat.start_condition),
                               duration_condition=copy.deepcopy(beat.duration_condition))
                result.beats.insert(i + 1, partner)
            shared(beat, partner, 'contact')
            i += 2
            continue

        if beat.function == 'attack':
            reciprocal = (next_beat is not None and next_beat.actor_id == target
                          and next_beat.function in ('attack', 'defend_or_block', 'dodge',
                                                     'hit_reaction')
                          and not next_beat.params.get('exact_window')
                          and not next_beat.params.get('group_id')
                          and compatible_timing(beat, next_beat)
                          and (next_beat.params.get('target') == beat.actor_id
                               or next_beat.params.get('threat') == beat.actor_id
                               or next_beat.params.get('look_at') == beat.actor_id
                               or next_beat.function == 'hit_reaction'))
            if reciprocal:
                partner = next_beat
            else:
                partner = Beat(beat_id=unique_id(beat.beat_id + '_reaction', used_beat_ids), function='hit_reaction',
                               actor_id=target,
                               params={'target': beat.actor_id,
                                       'wait_for': list(beat.params.get('wait_for', []))},
                               start_condition=copy.deepcopy(beat.start_condition),
                               duration_condition=copy.deepcopy(beat.duration_condition))
                result.beats.insert(i + 1, partner)
            shared(beat, partner, 'combat')
            i += 2
            continue
        i += 1
    return result


def _approach_beats(group_id, actor, timed_points):
    """Encode waits explicitly; an arc-length path alone cannot represent them."""
    from plan_model import Beat, TimingCondition
    from crowd.st_planner import Reservation
    points = Reservation(actor, timed_points, 0.0).points
    beats = []
    moving = []

    def emit(path):
        if len(path) < 2 or path[-1][0] <= path[0][0] + 1e-7:
            return
        waiting = all(math.dist(p[1:], path[0][1:]) < 1e-7 for p in path)
        params = {'auto_interaction_approach': group_id}
        if not waiting:
            params.update(brief_path={'points': [list(p[1:]) for p in path]}, action='walk_to')
        beats.append(Beat(f'{group_id}_approach_{actor}_{len(beats)}',
                          'wait' if waiting else 'walk_to', actor, params,
                          TimingCondition('at', path[0][0]),
                          TimingCondition('for_duration', path[-1][0] - path[0][0])))

    for a, b in zip(points, points[1:]):
        if math.dist(a[1:], b[1:]) < 1e-7:
            emit(moving)
            moving = []
            emit([a, b])
        else:
            if not moving: moving = [a]
            moving.append(b)
    emit(moving)
    return beats


def schedule_prompt_interactions(plan, initial_states, timeline_resolver, synthesizer, scene_data,
                                 max_route_attempts=DEFAULT_INTERACTION_ROUTE_ATTEMPTS,
                                 envelopes=None, crowd_config=None, continuation_beats=()):
    """Create route-timed approaches and a shared start for prompt-only interactions.

    ``envelopes`` (``crowd_spacing.ActorEnvelopes``) supplies measured body radii
    and per-beat clip reach. A moving actor keeps its body radius plus the other
    actor's radius at that moment plus a buffer, so a dancer blocks space only
    while dancing. Without envelopes, or for actors it does not know, the
    standing-grid spacing is used so callers without measured data keep the
    previous behavior.

    ``continuation_beats`` supplies lookahead when scheduling only a prefix.
    These beats affect endpoint clearance but are not scheduled or returned.
    """
    from plan_model import Beat, TimingCondition, StructuredError
    from crowd.config import CrowdSolverConfig
    from crowd.st_planner import Reservation, plan_route
    from crowd.geometry import clears_solids, supports_body_route, snapshot_solids
    from crowd.activities import activity_areas
    from types import SimpleNamespace
    crowd_config = crowd_config or CrowdSolverConfig()
    solids = snapshot_solids(scene_data.get('solid_boxes', []))
    if envelopes is not None and envelopes.buffer_m != crowd_config.buffer_m:
        envelopes = copy.copy(envelopes)
        envelopes.buffer_m = crowd_config.buffer_m
    from support_surfaces import (ACTOR_SUPPORT_MARGIN_M, ELEVATED_SUPPORT_THRESHOLD_M,
                                  ROUTE_SAMPLE_STEP_M, SUPPORT_EDGE_EPSILON_M,
                                  SUPPORT_LEVEL_TOLERANCE_M, contains, polygon_area)
    # Compiled shot plans can be reopened as author input. Routes generated by
    # an earlier compile contain old actor positions; rebuild them from the
    # current trajectories instead of treating those routes as authored beats.
    source = copy.deepcopy(plan)
    source.beats = [beat for beat in source.beats
                    if not beat.params.get('auto_interaction_approach')]
    for beat in source.beats:
        authored = beat.params.get('interaction_start_condition')
        if authored is not None:
            beat.start_condition = TimingCondition.from_dict(authored) if authored else None
    result = link_prompt_interactions(source, initial_states)
    # A free-form approach immediately before a shared interaction expresses
    # the desired arrival, which this scheduler routes and times itself. Keeping
    # both creates a second, unchecked arrival at the target's current position
    # and can make the actual formation unreachable. Authored routes and timed
    # approaches remain explicit constraints.
    for index in range(len(result.beats) - 1, -1, -1):
        beat = result.beats[index]
        if (beat.function != 'approach' or beat.start_condition is not None
                or beat.duration_condition is not None
                or beat.params.get('brief_path') or beat.params.get('exact_window')
                or beat.params.get('wait_for')
                or any(beat.beat_id in item.params.get('wait_for', [])
                       for item in result.beats)):
            continue
        following = result.beats[index + 1] if index + 1 < len(result.beats) else None
        if (following and following.params.get('group_id')
                and following.actor_id == beat.actor_id
                and beat.params.get('target') in following.params.get('participants', [])):
            result.beats.pop(index)
    group_ids = list(dict.fromkeys(b.params['group_id'] for b in result.beats
                                  if b.params.get('group_id') and not b.params.get('exact_window')))
    # Only explicit shared triggers establish a wave. Coincidental actor
    # availability, or an already compiled plan with lost trigger metadata,
    # cannot establish the director's intended synchronization.
    import json
    wave_members = {}
    wave_for_group = {}
    wave_actors = {}
    for gid in group_ids:
        members = [b for b in result.beats if b.params.get('group_id') == gid]
        condition = members[0].start_condition
        if (not all(b.function == 'turn_to_face' for b in members) or condition is None
                or condition.type not in ('at', 'after', 'with')):
            continue
        key = json.dumps(condition.to_dict(), sort_keys=True)
        actors = {b.actor_id for b in members}
        if actors & wave_actors.get(key, set()):
            continue
        wave_actors.setdefault(key, set()).update(actors)
        wave_members.setdefault(key, []).append(gid)
        wave_for_group[gid] = key
    ordered = []
    for gid in group_ids:
        for candidate in wave_members.get(wave_for_group.get(gid), [gid]):
            if candidate not in ordered:
                ordered.append(candidate)
    group_ids = ordered
    wave_arrivals, wave_free_arrivals = {}, {}
    scheduled_groups = set()
    for group_id in group_ids:
        timeline, error = timeline_resolver.resolve(result, initial_states)
        if error: return None, error
        group_beats = [b for b in result.beats if b.params.get('group_id') == group_id
                       and not b.params.get('exact_window')]
        if not group_beats: continue
        first_index = min(result.beats.index(b) for b in group_beats)
        participants = list(dict.fromkeys(group_beats[0].params.get('participants') or
                                              group_beats[0].params.get('actors') or
                                              [b.actor_id for b in group_beats]))
        if len(participants) < 2 or any(actor not in initial_states for actor in participants):
            return None, StructuredError('INTERACTION_PARTICIPANTS',
                                         f'Shared interaction {group_id} needs at least two scene characters')
        preceding = {b.beat_id for b in result.beats[:first_index]}
        # Wave ordering can solve a group that occurs later in the source list
        # first. Its committed paths and final occupants still block this group.
        # Source-list prefixes alone let both groups overlook each other's routes.
        prior_beats = sorted((b for b in timeline.beats if b.beat_id in preceding or
                             (b.actor_id not in participants and (
                                 b.params.get('group_id') in scheduled_groups or
                                 b.params.get('auto_interaction_approach') in scheduled_groups))),
                             key=lambda b: (b.start_time, b.end_time))
        by_id = {b.beat_id: b for b in result.beats}
        from crowd.trajectories import planning_tracks
        provisional, _, physical_error = planning_tracks(
            timeline, initial_states, timeline_resolver, synthesizer, scene_data,
            seed=result.seed, beats=prior_beats)
        if physical_error:
            return None, physical_error
        available = {actor: max((b.end_time for b in prior_beats if b.actor_id == actor), default=0.)
                     for actor in participants}
        positions = {actor: list(timeline_resolver._sample_traj(provisional[actor], available[actor]))
                     for actor in participants}
        natural_center = [sum(positions[actor][axis] for actor in participants) / len(participants)
                          for axis in range(3)]
        center = list(natural_center)
        center_ref = group_beats[0].params.get('center')
        # A center the compiler assigned (crowd spacing) is a preference ranked by
        # travel cost with the other candidates. An authored center stays first.
        center_is_authored = bool(center_ref) and (not group_beats[0].params.get('center_auto')
                                                  or group_beats[0].params.get('arena_layout_center'))
        if center_ref and timeline_resolver.ref_resolver:
            states = {actor: {'position': positions.get(actor, list(state.get('position', [0, 0, 0])))}
                      for actor, state in initial_states.items()}
            resolved, ref_error = timeline_resolver.ref_resolver.resolve(str(center_ref), states,
                                                                         max(available.values()))
            if ref_error or resolved is None:
                return None, StructuredError('INTERACTION_CENTER',
                                             f'Cannot resolve meeting area {center_ref}: {ref_error}')
            center = list(resolved.position)
        spacing = group_beats[0].params.get('spacing')
        close_contact = spacing == 'contact' or group_beats[0].function in ('shake_hands', 'hug_or_kiss')
        radius = {'contact': .45, 'combat': .55, 'conversational': .85}.get(
            spacing, .5 if close_contact else max(.85, .42 * len(participants)))
        from support_surfaces import STANDING_SPOT_SPACING_M
        timeline_beats = timeline.beats

        def clearance_between(mover, other, when):
            if envelopes is not None and envelopes.knows(mover) and envelopes.knows(other):
                return (max(envelopes.body[mover], interaction_radii.get(mover, 0.))
                        + envelopes.radius_at(other, when, timeline_beats) + envelopes.buffer_m)
            return STANDING_SPOT_SPACING_M

        baseline_start = max((next(b.start_time for b in timeline.beats if b.beat_id == beat.beat_id)
                              for beat in group_beats), default=0.)
        if max(positions[actor][1] for actor in participants) - min(positions[actor][1] for actor in participants) > .6:
            return None, StructuredError('PAIR_LEVEL_MISMATCH',
                                         f'Actors in {group_id} finish on different levels; choose a reachable shared platform.')
        level_supports = []
        supports = scene_data.get('support_surfaces', [])
        if supports:
            level_supports = [surface for surface in supports
                              if abs(surface['height_m'] - natural_center[1]) <= SUPPORT_LEVEL_TOLERANCE_M]
            if not all(any(contains(surface, positions[actor]) for surface in level_supports)
                       for actor in participants):
                return None, StructuredError('INTERACTION_NO_SHARED_SUPPORT',
                                             f'Actors in {group_id} are not on connected standing surfaces.',
                                             beat_id=group_beats[0].beat_id,
                                             details=_interaction_support_details(
                                                 group_id, group_beats, participants, positions,
                                                 initial_states, center_ref, center, spacing,
                                                 supports, level_supports, 'actor_without_level_support'),
                                             suggestions=['Use a meeting point and actor placements on the same measured standing surface.'])
        elif scene_data.get('objects') and any(abs(positions[actor][1]) > ELEVATED_SUPPORT_THRESHOLD_M
                                               for actor in participants):
            return None, StructuredError('INTERACTION_SUPPORT_UNKNOWN',
                                         f'Cannot verify elevated standing support for {group_id}.')
        # The interaction's close-spacing contract ends when ordinary motion or
        # an unpaired performance begins. Reserve that next phase at the source
        # now; a route planner cannot escape a source already inside its peer's
        # protected envelope. Include lookahead when compiling only a prefix.
        pose_radii, interaction_radii, exit_radii = {}, {}, {}
        if envelopes is not None:
            for actor in participants:
                if not envelopes.knows(actor):
                    continue
                pose_radii[actor] = envelopes.solid_movement_radius(actor, 'walk_to')
                interaction_radii[actor] = envelopes.body[actor]
                exit_radii[actor] = envelopes.body[actor]
                for following in [*result.beats[first_index:], *continuation_beats]:
                    if following.actor_id != actor:
                        continue
                    ground_move = following.function in ('walk_to', 'run_to', 'jog_to', 'sprint_to')
                    if ground_move:
                        exit_radii[actor] = max(exit_radii[actor],
                            envelopes.movement_radius(actor, following.function),
                            envelopes.beat_reach.get(following.beat_id, 0.))
                        pose_radii[actor] = max(pose_radii[actor],
                            envelopes.solid_movement_radius(actor, following.function, following.beat_id))
                    if ground_move or following.function == 'climb_stairs':
                        break
                    pose_radii[actor] = max(pose_radii[actor],
                        envelopes.solid_beat_reach.get(following.beat_id, 0.))
                    interaction_radii[actor] = max(interaction_radii[actor],
                        envelopes.beat_reach.get(following.beat_id, 0.))
                    if not following.params.get('group_id'):
                        exit_radii[actor] = max(exit_radii[actor],
                            envelopes.beat_reach.get(following.beat_id, 0.))
            for i, actor in enumerate(participants):
                for j in range(i + 1, len(participants)):
                    other = participants[j]
                    # Kisses and handshakes require contact range. Do not
                    # silently widen that explicit interaction contract.
                    if not close_contact and actor in exit_radii and other in exit_radii:
                        separation = exit_radii[actor] + exit_radii[other] + crowd_config.buffer_m
                        # Slots lie on a circle; derive its radius from each
                        # pair's chord rather than assuming identical bodies.
                        chord = 2 * math.sin(math.pi * (j - i) / len(participants))
                        radius = max(radius, separation / chord)

        # Candidate meeting areas: the requested/assigned center, the actors' natural
        # midpoint, nudges around that midpoint, support centroids and standing
        # spots. Candidates are ranked by the participants' total travel so
        # approaches stay short and cross the crowd as little as possible.
        candidates = [list(center), list(natural_center)]
        step = 2 * radius
        for ring in range(1, CENTER_NUDGE_RINGS + 1):
            for index in range(CENTER_NUDGE_DIRECTIONS):
                angle = 2 * math.pi * index / CENTER_NUDGE_DIRECTIONS
                candidates.append([natural_center[0] + ring * step * math.cos(angle),
                                   natural_center[1],
                                   natural_center[2] + ring * step * math.sin(angle)])
        if level_supports:
            candidates.extend(
                [sum(vertex[0] for vertex in surface['polygon_xz']) / len(surface['polygon_xz']),
                 surface['height_m'],
                 sum(vertex[1] for vertex in surface['polygon_xz']) / len(surface['polygon_xz'])]
                for surface in level_supports)
            candidates.extend(
                list(spot['position']) for spot in scene_data.get('standing_spots', [])
                if abs(spot['position'][1] - natural_center[1]) <= SUPPORT_LEVEL_TOLERANCE_M)

        def travel_cost(point):
            return sum(math.dist(positions[actor], point) for actor in participants)

        candidates.sort(key=travel_cost)
        # For a close two-person interaction, first try keeping one partner in
        # place and bringing the other into contact range. This avoids routing
        # both actors across each other's starting position in a narrow area.
        stationary_centers = []
        if close_contact and len(participants) == 2:
            for stationary, mover in ((participants[0], participants[1]),
                                      (participants[1], participants[0])):
                stay, moving = positions[stationary], positions[mover]
                dx, dz = moving[0] - stay[0], moving[2] - stay[2]
                distance = math.hypot(dx, dz)
                if distance > 1e-6:
                    stationary_centers.append([
                        stay[0] + radius * dx / distance,
                        stay[1],
                        stay[2] + radius * dz / distance])
        # Explicit user centers retain priority. Automatically assigned arena
        # centers are preferences, so local contact placements may be tried first.
        authored_center = (center_is_authored and
                           not group_beats[0].params.get('center_auto'))
        center_options = ([list(center)] if authored_center else []) + stationary_centers + candidates

        prior_areas = (
            activity_areas(SimpleNamespace(beats=prior_beats), provisional, envelopes,
                           timeline_resolver._sample_traj, crowd_config.level_tolerance_m)
            if envelopes is not None else []
        )
        unrelated_areas = [area for area in prior_areas if not (area.members & set(participants))]

        def formation_blocker(slots, when):
            """First non-participant that is too close to a slot at ``when``, else ``None``."""
            for mover, point in slots.items():
                for other, trajectory in provisional.items():
                    if other in participants:
                        continue
                    other_pos = timeline_resolver._sample_traj(trajectory, when)
                    if (abs(point[1] - other_pos[1]) <= SUPPORT_LEVEL_TOLERANCE_M
                            and math.hypot(point[0] - other_pos[0], point[2] - other_pos[2])
                            + 1e-6 < clearance_between(mover, other, when)):
                        return other
                if envelopes is not None and envelopes.knows(mover):
                    mover_reach = max(envelopes.body[mover], interaction_radii.get(mover, 0.))
                    for area in unrelated_areas:
                        if (area.overlaps(when, when + 1e-6)
                                and abs(point[1] - area.position[1]) <= SUPPORT_LEVEL_TOLERANCE_M
                                and math.hypot(point[0] - area.position[0], point[2] - area.position[2])
                                + 1e-6 < mover_reach + area.radius + envelopes.buffer_m):
                            return min(area.members)
            return None

        def clear_formation(slots, when):
            return formation_blocker(slots, when) is None

        support_cache = {}
        support_bounds = [
            (surface, min(point[0] for point in surface['polygon_xz']),
             max(point[0] for point in surface['polygon_xz']),
             min(point[1] for point in surface['polygon_xz']),
             max(point[1] for point in surface['polygon_xz']))
            for surface in level_supports if len(surface.get('polygon_xz', [])) >= 3]

        def supported(route):
            if not level_supports:
                return True
            for a, b in zip(route, route[1:] or route):
                steps = max(1, math.ceil(math.hypot(b[0] - a[0], b[2] - a[2])
                                         / ROUTE_SAMPLE_STEP_M))
                for index in range(steps + 1):
                    fraction = index / steps
                    point = tuple(a[axis] + (b[axis] - a[axis]) * fraction for axis in range(3))
                    if point not in support_cache:
                        support_cache[point] = any(
                            x0 - SUPPORT_EDGE_EPSILON_M <= point[0] <= x1 + SUPPORT_EDGE_EPSILON_M
                            and z0 - SUPPORT_EDGE_EPSILON_M <= point[2] <= z1 + SUPPORT_EDGE_EPSILON_M
                            and abs(point[1] - surface['height_m']) <= SUPPORT_LEVEL_TOLERANCE_M
                            and contains(surface, point)
                            for surface, x0, x1, z0, z1 in support_bounds)
                    if not support_cache[point]:
                        return False
            return bool(route)

        # Candidate delays come from actual preceding action completions. Waiting
        # can let a moving blocker clear the corridor without changing the shot.
        phase_wait_for = list(dict.fromkeys(
            parent for beat in group_beats for parent in beat.params.get('wait_for', [])))
        resolved_ends = {b.beat_id: b.end_time for b in timeline.beats}
        trigger_start = max((resolved_ends[parent] for parent in phase_wait_for), default=0.0)
        for beat in group_beats:
            condition = beat.start_condition
            if condition is None:
                continue
            if condition.type == 'at':
                trigger_start = max(trigger_start, float(condition.value))
            elif condition.type in ('after', 'with'):
                event_time = timeline.events.get(str(condition.value), baseline_start)
                trigger_start = max(trigger_start, event_time +
                                    (timeline_resolver.settle_hold_s if condition.type == 'after' else 0.0))
            else:
                trigger_start = max(trigger_start, baseline_start)
        # A new shared interaction follows the preceding actions of every
        # participant. Routing an early-free partner before the other finishes
        # can place them directly in the later actor's authored path.
        earliest_start = max(trigger_start, *available.values())
        start_options = sorted({earliest_start} | {
            beat.end_time for beat in prior_beats
            if beat.actor_id not in participants and beat.end_time > earliest_start})
        approaches = None
        interaction_positions = None
        support = None
        had_formation = False
        route_attempts = 0
        formation_attempts = 0
        # Why candidates were rejected, so a failure explains itself instead of
        # only reporting that the attempt budget ran out.
        blockers = {}
        rejections = {'formation_off_support': 0, 'formation_occupied_at_start': 0,
                      'route_blocked': 0, 'formation_occupied_at_arrival': 0,
                      'center_budget_exhausted': 0}
        planner_statuses = {}
        last_route_failure = None
        seen_centers = set()
        # Mesh faces often share edge directions. Budget actual actor-slot
        # arrangements, not the number of polygons that proposed them. Keys
        # use micrometre precision only to collapse floating-point trig noise;
        # the original coordinates still undergo all clearance checks.
        seen_formations = set()
        for preferred in center_options:
            key = tuple(round(float(value), 3) for value in preferred)
            if key in seen_centers:
                continue
            seen_centers.add(key)
            if level_supports and not any(contains(surface, preferred) for surface in level_supports):
                continue
            source = positions[participants[0]]
            base_angle = math.atan2(source[2] - preferred[2], source[0] - preferred[0])
            angles = [base_angle + 2 * math.pi * index / 8 for index in range(8)]
            angles.extend(math.atan2(b[1] - a[1], b[0] - a[0])
                          for surface in level_supports
                          for a, b in zip(surface['polygon_xz'],
                                          surface['polygon_xz'][1:] + surface['polygon_xz'][:1]))
            center_attempts = 0
            center_exhausted = False
            for angle in angles:
                already_staged = (
                    all(math.dist(positions[actor], preferred) <= radius + .35
                        for actor in participants)
                    and all(.75 <= math.dist(positions[a], positions[b]) <= 2 * radius
                            for index, a in enumerate(participants)
                            for b in participants[index + 1:])
                    and (close_contact or all(math.dist(positions[a], positions[b]) + 1e-7 >=
                            exit_radii[a] + exit_radii[b] + crowd_config.buffer_m
                            for index, a in enumerate(participants) if a in exit_radii
                            for b in participants[index + 1:] if b in exit_radii)))
                destinations = (
                    {actor: list(positions[actor]) for actor in participants}
                    if already_staged else {
                        actor: [preferred[0] + radius * math.cos(angle + 2 * math.pi * index / len(participants)),
                                positions[actor][1],
                                preferred[2] + radius * math.sin(angle + 2 * math.pi * index / len(participants))]
                        for index, actor in enumerate(participants)})
                formation_key = tuple((actor, *(round(value, 6) for value in point))
                                      for actor, point in sorted(destinations.items()))
                if formation_key in seen_formations:
                    continue
                seen_formations.add(formation_key)
                if not all(supported([destination]) and
                           (not level_supports or any(contains(surface, destination, ACTOR_SUPPORT_MARGIN_M)
                                                      for surface in level_supports))
                           and (actor not in pose_radii or (
                               clears_solids([destination], solids,
                                             pose_radii[actor] + crowd_config.buffer_m)
                               and supports_body_route([destination], pose_radii[actor],
                                                       level_supports, crowd_config)))
                           for actor, destination in destinations.items()):
                    rejections['formation_off_support'] += 1
                    continue
                had_formation = True
                for start_time in start_options:
                    formation_attempts += 1
                    if formation_attempts > crowd_config.max_formations:
                        return None, StructuredError('INTERACTION_SEARCH_LIMIT',
                            f'Formation candidate budget exhausted for {group_id}.',
                            beat_id=group_beats[0].beat_id,
                            details={'group_id': group_id, 'formation_attempts': formation_attempts - 1,
                                     'attempts': route_attempts, 'rejections': rejections,
                                     'planner_statuses': planner_statuses,
                                     'top_blockers': dict(sorted(blockers.items(),
                                                                key=lambda item: -item[1])[:5])},
                            suggestions=['Spread the meeting areas or stagger the crowd; search exhaustion does not prove infeasibility.'])
                    blocker = formation_blocker(destinations, max(start_time, *available.values()))
                    if blocker is not None:
                        rejections['formation_occupied_at_start'] += 1
                        blockers[blocker] = blockers.get(blocker, 0) + 1
                        continue
                    candidate_approaches = []
                    route_reservations = []
                    arrival_times = []
                    blocked = False
                    # Reserve short local repositioning before a longer crossing
                    # route, regardless of the order actors were named in the call.
                    route_order = sorted(destinations, key=lambda actor: (
                        math.dist(positions[actor], destinations[actor]), actor))
                    for actor in route_order:
                        destination = destinations[actor]
                        actor_source = positions[actor]
                        actor_start = max(start_time, available[actor])
                        if math.dist(actor_source, destination) <= .05:
                            arrival_times.append(actor_start)
                            continue
                        if center_attempts >= max(1, max_route_attempts // INTERACTION_CENTER_SEARCH_DIVISOR):
                            center_exhausted = True
                            rejections['center_budget_exhausted'] += 1
                            blocked = True
                            break
                        route_attempts += 1
                        center_attempts += 1
                        if route_attempts > max_route_attempts:
                            return None, StructuredError(
                                'INTERACTION_SEARCH_LIMIT',
                                f'Could not find a clear approach for {group_id} within the route search budget.',
                                beat_id=group_beats[0].beat_id,
                                details={'group_id': group_id, 'attempts': route_attempts - 1,
                                         'rejections': dict(rejections),
                                         'planner_statuses': dict(planner_statuses),
                                         'last_route_failure': last_route_failure,
                                         'top_blockers': dict(sorted(blockers.items(), key=lambda item: -item[1])[:5])},
                                suggestions=['Spread the meeting areas, stagger the crowd, or increase the route search budget.'])
                        obstacles = synthesizer._collect_obstacles_at_time(
                            actor_start, actor_start, actor, actor_source, destination, {}, scene_data)
                        if envelopes is not None and envelopes.knows(actor):
                            # Intentional interaction partners use the group's
                            # formation/body contract. Full clip reach remains
                            # required against every unrelated character.
                            reservations = [Reservation(r.actor, r.points, envelopes.body[r.actor],
                                                        mover_radius=envelopes.body[actor])
                                            for r in route_reservations]
                            for other, trajectory in provisional.items():
                                if other in participants or not envelopes.knows(other):
                                    continue
                                windows = [(b.start_time, b.end_time,
                                            max(envelopes.beat_reach.get(b.beat_id, 0.),
                                                envelopes.movement_radius(other, b.function)))
                                           for b in timeline_beats if b.actor_id == other
                                           and (b.beat_id in envelopes.beat_reach or b.function in
                                                ('walk_to', 'run_to', 'jog_to', 'sprint_to'))]
                                reservations.append(Reservation(other, trajectory, envelopes.body[other], windows))
                            reservations.extend(area.reservation() for area in unrelated_areas)
                            points = [actor_source, destination] + [list(p[1:]) for r in reservations for p in r.points]
                            polygon_points = [p for surface in level_supports for p in surface['polygon_xz']]
                            padding = crowd_config.cell_size_m * 2 + envelopes.body[actor]
                            bounds = ((min(p[0] for p in polygon_points), max(p[0] for p in polygon_points),
                                       min(p[1] for p in polygon_points), max(p[1] for p in polygon_points))
                                      if polygon_points else
                                      (min(p[0] for p in points) - padding, max(p[0] for p in points) + padding,
                                       min(p[2] for p in points) - padding, max(p[2] for p in points) + padding))
                            travel_radius = envelopes.movement_radius(actor, 'walk_to')
                            solid_radius = envelopes.solid_movement_radius(actor, 'walk_to')
                            planned = plan_route(
                                actor_source, destination, actor_start, 1.4, travel_radius, reservations,
                                lambda route: (_route_clear_of_discs(route, obstacles, supported, clearance_margin=0.0)
                                    and clears_solids(route, solids,
                                                      solid_radius + crowd_config.buffer_m)
                                    and supports_body_route(route, solid_radius, level_supports, crowd_config)),
                                bounds, crowd_config,
                                hold_until=min((b.start_time for b in timeline_beats
                                    if b.actor_id == actor and b.start_time > baseline_start + 1e-6
                                    and b.function in ('walk_to', 'run_to', 'jog_to', 'climb_stairs', 'turn_to_face')
                                    and b.params.get('group_id') != group_id),
                                    default=timeline.total_duration_s),
                                hold_radius=interaction_radii.get(actor, travel_radius))
                            if planned.points is None:
                                planner_statuses[planned.status] = planner_statuses.get(planned.status, 0) + 1
                                for blocker in planned.blocking_actors:
                                    blockers[blocker] = blockers.get(blocker, 0) + 1
                                last_route_failure = {
                                    'actor': actor, 'status': planned.status,
                                    'source': list(actor_source), 'destination': list(destination),
                                    'blocking_actors': list(planned.blocking_actors),
                                    'critical_blockers': list(planned.critical_blockers),
                                    'expansions': planned.expansions,
                                    'route_reservations': [
                                        {'actor': reservation.actor,
                                         'start': list(reservation.points[0]),
                                         'end': list(reservation.points[-1]),
                                         'radius': reservation.radius}
                                        for reservation in reservations],
                                    'direct_route_checks': {
                                        'supported': supported([actor_source, destination]),
                                        'clear_of_scene_obstacles': _route_clear_of_discs(
                                            [actor_source, destination], obstacles, supported,
                                            clearance_margin=0.0),
                                        'clear_of_solids': clears_solids(
                                            [actor_source, destination], solids,
                                            solid_radius + crowd_config.buffer_m),
                                        'body_supported': supports_body_route(
                                            [actor_source, destination], solid_radius,
                                            level_supports, crowd_config)}}
                                rejections['route_blocked'] += 1
                                blocked = True
                                break
                            arrival = planned.points[-1][0]
                            arrival_times.append(arrival)
                            route_reservations.append(Reservation(actor, planned.points, travel_radius))
                            candidate_approaches.extend(_approach_beats(group_id, actor, planned.points))
                            continue
                        obstacles.extend(
                            {"cx": other_pos[0], "cz": other_pos[2],
                             "radius": clearance_between(actor, other, actor_start)}
                            for other, trajectory in provisional.items() if other not in participants
                            for other_pos in [timeline_resolver._sample_traj(trajectory, actor_start)]
                            if abs(other_pos[1] - actor_source[1]) <= SUPPORT_LEVEL_TOLERANCE_M)
                        route = synthesizer._plan_smooth_path(actor_source, destination, obstacles)
                        if not _route_clear_of_discs(route, obstacles, supported,
                                                     clearance_margin=0.0):
                            route = _clear_route_around_discs(
                                actor_source, destination, obstacles, supported,
                                clearance_margin=0.0)
                        if route is None or not _route_clear_of_actors(
                                route, actor_start, 1.4, actor, participants,
                                provisional, timeline_resolver._sample_traj,
                                lambda other, when, mover=actor: clearance_between(mover, other, when),
                                SUPPORT_LEVEL_TOLERANCE_M,
                                ROUTE_SAMPLE_STEP_M):
                            rejections['route_blocked'] += 1
                            blocked = True
                            break
                        length = sum(math.dist(a, b) for a, b in zip(route, route[1:]))
                        duration = length / 1.4
                        arrival_times.append(actor_start + duration)
                        candidate_approaches.append(Beat(
                            beat_id=f'{group_id}_approach_{actor}', function='walk_to', actor_id=actor,
                            params={'brief_path': {'points': route},
                                    'action': 'walk_to', 'auto_interaction_approach': group_id},
                            start_condition=TimingCondition('at', actor_start),
                            duration_condition=TimingCondition('for_duration', duration)))
                    if not blocked and not clear_formation(destinations, max(arrival_times)):
                        rejections['formation_occupied_at_arrival'] += 1
                        blocked = True
                    if blocked:
                        if center_exhausted:
                            break
                        continue
                    approaches = candidate_approaches
                    interaction_positions = destinations
                    meeting_time = max(start_time, *arrival_times)
                    center = list(preferred)
                    support = next((surface for surface in level_supports if contains(surface, center)), None)
                    break
                if approaches is not None:
                    break
                if center_exhausted:
                    break
            if approaches is not None:
                break
        if approaches is None:
            error_code = ('INTERACTION_APPROACH_OCCUPIED' if had_formation
                          else 'INTERACTION_NO_SHARED_SUPPORT')
            details = ({'group_id': group_id, 'rejections': dict(rejections),
                                         'top_blockers': dict(sorted(blockers.items(), key=lambda item: -item[1])[:5])} if had_formation else
                       _interaction_support_details(
                           group_id, group_beats, participants, positions,
                           initial_states, center_ref, center, spacing,
                           supports, level_supports, 'no_supported_formation'))
            return None, StructuredError(
                error_code,
                (f'No collision-free supported approach found in the candidate set for {group_id}.'
                 if had_formation else
                 f'Actors in {group_id} have no common standing surface for this formation.'),
                beat_id=group_beats[0].beat_id,
                details=details,
                suggestions=['Move the meeting area or allow the crowd more space.'])
        for beat in group_beats:
            beat.params.setdefault('interaction_start_condition',
                                   beat.start_condition.to_dict() if beat.start_condition else {})
            beat.start_condition = TimingCondition('at', meeting_time)
            beat.params['participants'] = participants
            beat.params['interaction_positions'] = interaction_positions
            beat.params['auto_interaction'] = True
            if support:
                beat.params['support_surface'] = support['prim_path']
        wave = wave_for_group.get(group_id)
        if wave is not None:
            wave_arrivals[group_id] = meeting_time
            wave_free_arrivals[group_id] = max(baseline_start, *available.values()) + max(
                math.dist(positions[a], interaction_positions[a]) / 1.4 for a in participants)
            members = wave_members[wave]
            if len(members) > 1 and all(g in wave_arrivals for g in members):
                common_start = max(wave_arrivals[g] for g in members)
                delay = common_start - max(wave_free_arrivals[g] for g in members)
                if delay > crowd_config.max_delay_s + 1e-6:
                    return None, StructuredError('INTERACTION_DELAYED',
                        'The shared interaction wave exceeds the configured delay allowance.',
                        details={'groups': members, 'delay_s': delay,
                                 'max_delay_s': crowd_config.max_delay_s})
                for other_beat in result.beats:
                    if other_beat.params.get('group_id') in members:
                        other_beat.start_condition = TimingCondition('at', common_start)
                        other_beat.params['crowd_wave_delay_s'] = max(0.0, delay)
        for approach in approaches:
            approach.params['wait_for'] = list(phase_wait_for)
        result.beats[first_index:first_index] = approaches
        # An actor synthesized first cannot sample the second actor's future
        # trajectory. Carry the agreed meeting positions into contact beats.
        active_positions = dict(interaction_positions)
        for following in result.beats[first_index + len(approaches):]:
            if following.params.get('group_id') not in (None, group_id):
                break
            target_id = following.params.get('target')
            if (isinstance(target_id, str) and target_id in active_positions
                    and following.function in ('attack', 'push', 'shove', 'defend_or_block')):
                following.params['paired_target_position'] = list(active_positions[target_id])
            if following.actor_id in active_positions and following.function in (
                'walk_to', 'run_to', 'jog_to', 'approach', 'climb_stairs', 'dodge', 'flee_from',
            ):
                active_positions.pop(following.actor_id)
        scheduled_groups.add(group_id)
    return result, None


def ensure_in_range(actor_a, actor_b, window, reach_m=.75, available_start=0., route=None, fps=24, hold_until=math.inf):
    """Approach before contact, then hold a target-relative standoff for the window.

    Never edits preceding authoritative actions or paths. Infeasible approaches
    return a structured error instead of teleporting or moving a locked window.
    """
    from motion_synthesis import Keyframe
    start, end = window
    source, heading = actor_a.sample(start)
    target, _ = actor_b.sample(start)
    distance = math.hypot(source[0]-target[0], source[2]-target[2])
    standoff = max(.65, reach_m+.25)
    if distance > 1e-8: direction = [(source[0]-target[0])/distance, 0, (source[2]-target[2])/distance]
    else: direction = [0., 0., 1.]
    desired = [target[j]+direction[j]*standoff for j in range(3)]
    desired[1] = source[1]
    travel = math.dist(source, desired)
    if abs(distance-standoff) < .15:
        direction = [(source[0]-target[0])/max(distance, 1e-8), 0, (source[2]-target[2])/max(distance, 1e-8)]
        standoff = distance
        travel = 0.
    points = route(source, desired) if route and travel > .01 else [source, desired]
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
    available = start-available_start
    if travel > .01 and (available <= 0 or length/available > 7):
        return {'error_code': 'CONTACT_UNREACHABLE', 'actor_id': actor_a.actor_id,
                'details': {'window': window, 'distance_m': length, 'available_s': available},
                'suggestions': ['Draw an approach path ending in contact range, leave an open approach gap, or mark the contact window flexible.']}
    if travel > .01:
        from director_brief import resolve_path_for_actor
        approach_start = max(available_start, start-length/4.2)
        speed = length/max(.001, start-approach_start)
        gait, nominal = ('run', 4.2) if speed >= 2.2 else ('walk', 1.4)
        # Remove only idle fills in the usable approach gap.
        actor_a.actions = [a for a in actor_a.actions if not (a['start_s'] >= available_start-1e-6 and a['end_s'] <= start+1e-6)]
        if approach_start > available_start:
            actor_a.add_action('acknowledging', available_start, approach_start, fps=fps)
        actor_a.add_action(gait, approach_start, start, fps=fps, speed=speed/nominal)
        actor_a.keyframes = [k for k in actor_a.keyframes if not approach_start <= k.time_s < start]
        for t, p, h in resolve_path_for_actor({'points': points, 'speed_profile': 'constant'}, [approach_start, start]):
            actor_a.keyframes.append(Keyframe(t, p, h, speed_mps=speed, action_clip=gait))
    original = sorted(actor_a.keyframes, key=lambda k: k.time_s)
    def clip_at(t):
        return next((a['clip'] for a in reversed(actor_a.actions) if a['start_s'] <= t < a['end_s']), 'acknowledging')
    actor_a.keyframes = [k for k in original if not start <= k.time_s <= end]
    for i in range(math.ceil((end-start)*48)+1):
        t = min(end, start+i/48)
        target_position, _ = actor_b.sample(t)
        p = [target_position[j]+direction[j]*standoff for j in range(3)]
        p[1] = source[1]
        h = math.degrees(math.atan2(target_position[0]-p[0], -(target_position[2]-p[2])))
        actor_a.keyframes.append(Keyframe(t, p, h, action_clip=clip_at(t)))
    final_position = actor_a.sample(end)[0] if not actor_a.keyframes else actor_b.sample(end)[0]
    final_position = [final_position[j]+direction[j]*standoff for j in range(3)]
    final_position[1] = source[1]
    for k in actor_a.keyframes:
        if end < k.time_s < hold_until and k.action_clip in ('idle', 'acknowledging'):
            k.position = list(final_position)
    actor_a.keyframes.sort(key=lambda k: k.time_s)
    actor_a.actions.sort(key=lambda a: a['start_s'])
    return None


def solve_shared_actions(brief, performances, synthesizer, scene_data, fps=24):
    """Stage selected participants around one shared interaction area before its window."""
    from motion_synthesis import Keyframe
    from director_brief import resolve_path_for_actor
    groups = {}
    for actor, track in brief.get('tracks', {}).items():
        for seg in track.get('segments', []):
            if seg.get('group_id') and seg.get('action') != 'attack':
                groups.setdefault(seg['group_id'], []).append((actor, seg))
    errors = []
    for group_id, members in sorted(groups.items(), key=lambda item: item[1][0][1]['t'][0]):
        members.sort(key=lambda pair: pair[1]['participants'].index(pair[0]))
        start, end = members[0][1]['t']
        positions = [performances[actor].sample(start)[0] for actor, _ in members]
        center = [sum(position[c] for position in positions) / len(positions) for c in range(3)]
        radius = max(.85, .42 * len(members))
        already_staged = (all(math.dist(position, center) <= radius + .35 for position in positions)
                          and all(math.dist(a, b) >= .75 for index, a in enumerate(positions)
                                      for b in positions[index + 1:]))
        first = positions[0]
        base_angle = math.atan2(first[2] - center[2], first[0] - center[0])
        for index, (actor, seg) in enumerate(members):
            perf = performances[actor]
            previous_end = max((item['t'][1] for item in brief['tracks'][actor]['segments']
                                if item is not seg and item['t'][1] <= start), default=0.)
            angle = base_angle + 2 * math.pi * index / len(members)
            desired = (list(positions[index]) if already_staged else
                       [center[0] + radius * math.cos(angle), positions[index][1],
                        center[2] + radius * math.sin(angle)])
            source = perf.sample(start)[0]
            if math.dist(source, desired) <= .05:
                route = [source, desired]
            else:
                obstacles = synthesizer._collect_obstacles_at_time(previous_end, start, actor,
                                                                   source, desired, performances, scene_data)
                route = synthesizer._plan_smooth_path(source, desired, obstacles)
            length = sum(math.dist(a, b) for a, b in zip(route, route[1:]))
            available = start - previous_end
            if length > .05 and (available <= 0 or length / available > 7):
                errors.append({'error_code': 'SHARED_ACTION_UNREACHABLE', 'group_id': group_id,
                               'actor_id': actor, 'distance_m': length, 'available_s': available,
                               'suggestions': ['Move the shared action later or leave an approach gap.']})
                continue
            if length > .05:
                approach_start = max(previous_end, start - length / 4.2)
                speed = length / max(.001, start - approach_start)
                clip, nominal = ('run', 4.2) if speed >= 2.2 else ('walk', 1.4)
                perf.actions = [a for a in perf.actions if not (a['start_s'] >= approach_start - 1e-6
                                 and a['end_s'] <= start + 1e-6)]
                perf.add_action(clip, approach_start, start, fps=fps, speed=speed / nominal)
                perf.keyframes = [k for k in perf.keyframes if not approach_start <= k.time_s < start]
                for t, position, heading in resolve_path_for_actor(
                        {'points': route, 'speed_profile': 'constant'}, [approach_start, start]):
                    perf.keyframes.append(Keyframe(t, position, heading, speed_mps=speed, action_clip=clip))
            heading = math.degrees(math.atan2(center[0] - desired[0], -(center[2] - desired[2])))
            for key in perf.keyframes:
                if start <= key.time_s <= end:
                    key.position = list(desired)
                    key.heading_deg = heading
            perf.keyframes.append(Keyframe(start, list(desired), heading,
                                          action_clip=seg['action']))
            perf.keyframes.append(Keyframe(end, list(desired), heading,
                                          action_clip=seg['action']))
            perf.keyframes.sort(key=lambda key: key.time_s)
    return errors


def solve_contacts(brief, timeline, performances, manifest, synthesizer, scene_data, initial_states, ref_resolver=None):
    """Run after all actor trajectories exist, so contact never depends on actor order."""
    from motion_synthesis import ActorPerformance, Keyframe
    errors = []
    for actor, track in sorted(brief.get('tracks', {}).items()):
        previous_end = 0.
        for seg in sorted(track.get('segments', []), key=lambda s: s['t'][0]):
            if seg.get('implicit'): continue
            start, end = seg['t']
            if seg['action'] in ('attack', 'push', 'shove', 'shake_hands', 'hug_or_kiss', 'take_cover', 'converse', 'argue', 'greet') and seg.get('target'):
                a, b = performances.get(actor), performances.get(seg['target'])
                if b is None and seg['target'] in initial_states:
                    state = initial_states[seg['target']]
                    b = ActorPerformance(seg['target'], keyframes=[Keyframe(0, state['position'], state.get('facing_deg', 0))])
                if b is None and ref_resolver:
                    points = []
                    for i in range(math.ceil((end-start)*48)+1):
                        t = min(end, start+i/48)
                        states = copy.deepcopy(initial_states)
                        for aid, performance in performances.items():
                            pos, facing = performance.sample(t)
                            states[aid] = {**states.get(aid, {}), 'position': pos, 'facing_deg': facing}
                        resolved, _ = ref_resolver.resolve(seg['target'], states, t, source_actor_id=actor)
                        if resolved: points.append(Keyframe(t, list(resolved.position), resolved.facing_deg))
                    if points: b = ActorPerformance(seg['target'], keyframes=points)
                if a is None or b is None:
                    errors.append({'error_code': 'CONTACT_TARGET', 'actor_id': actor, 'segment': seg['id']})
                else:
                    clip = seg.get('variant', 'left_hook' if seg['action'] == 'attack' else 'elbow_punch')
                    reach = float(manifest.get('clips', {}).get(clip, {}).get('reach_m', .75))
                    if seg['action'] in ('converse','argue','greet'): reach = 1.05
                    if seg['action'] == 'take_cover':
                        asset = next((item for item in manifest.get('assets', []) if item.get('id') == b.asset_id), {})
                        bounds = asset.get('bounds_m', {})
                        if isinstance(bounds, dict):
                            size = bounds.get('size', [2., 2., 2.])
                        elif isinstance(bounds, list) and len(bounds) == 3: size = bounds
                        else: size = [2., 2., 2.]
                        reach = max(float(size[0]), float(size[2]))/2+.35
                    def route(p, q):
                        obstacles = synthesizer._collect_obstacles_at_time(previous_end, start, actor, p, q, performances, scene_data)
                        return synthesizer._plan_smooth_path(p, q, obstacles)
                    following = [s for s in track.get('segments', []) if not s.get('implicit') and s['t'][0] >= end and s is not seg]
                    hold_until = min((s['t'][0] for s in following), default=math.inf)
                    error = ensure_in_range(a, b, [start, end], reach, previous_end, route, timeline.fps, hold_until)
                    for next_seg in following:
                        if abs(next_seg['t'][0]-end) < 1e-6 and next_seg.get('path'):
                            if math.dist(a.sample(end)[0], brief['paths'][next_seg['path']]['points'][0]) > .05:
                                error = {'error_code': 'CONTACT_PATH_DISCONTINUITY', 'actor_id': actor, 'segment': next_seg['id']}
                    if error: errors.append(error)
            previous_end = end
    return errors


def fit_contact_windows(plan, brief, initial_states, manifest):
    """Use explicit tolerance to create an approach gap when contact requires one."""
    result = copy.deepcopy(plan)
    definitions = {(a, s['id']): s for a, tr in brief.get('tracks', {}).items() for s in tr.get('segments', [])}
    for actor in sorted(brief.get('tracks', {})):
        previous_end = 0.
        position = list(initial_states.get(actor, {}).get('position', [0, 0, 0]))
        for beat in sorted((b for b in result.beats if b.actor_id == actor and b.params.get('brief_segment') and not b.params.get('implicit_gap')), key=lambda b: b.params['exact_window'][0]):
            seg = definitions[actor, beat.params['brief_segment']]
            start, end = beat.params['exact_window']
            if beat.function in ('attack', 'push', 'shove', 'shake_hands', 'hug_or_kiss', 'take_cover', 'converse', 'argue', 'greet') and seg.get('flexible'):
                target = initial_states.get(seg.get('target'), {}).get('position')
                if target:
                    reach = manifest.get('clips', {}).get(seg.get('variant', 'left_hook'), {}).get('reach_m', .75)+.25
                    needed = max(0, math.dist(position, target)-reach)/7
                    earliest = previous_end+needed
                    shifted = max(start, earliest)
                    if shifted > seg['t'][0]+seg.get('tolerance_s', 0)+1e-8:
                        raise ValueError('Contact cannot be reached within explicit window tolerance')
                    beat.params['exact_window'] = [shifted, shifted+end-start]
                    beat.start_condition.value = shifted
                    start, end = beat.params['exact_window']
            if beat.params.get('brief_path'): position = list(beat.params['brief_path']['points'][-1])
            previous_end = end
    # Check lane overlap after contact adjustments; never silently shift a fixed neighbor.
    for actor in brief.get('tracks', {}):
        end = 0.
        for b in sorted((b for b in result.beats if b.actor_id == actor and b.params.get('exact_window') and not b.params.get('implicit_gap')), key=lambda b: b.params['exact_window'][0]):
            if b.params['exact_window'][0] < end-1e-8: raise ValueError('Contact approach conflicts with a neighboring window')
            end = b.params['exact_window'][1]
    return result


def check_baked_conformance(brief, stage_path, performances, fps=24, tolerance_m=.05):
    """Read composed USD transforms and the baker's actual source-clip samples at 48 Hz."""
    from pathlib import Path
    from pxr import Usd, UsdGeom
    from director_brief import ACTION_SPEEDS, ALIASES
    stage_path = Path(stage_path)
    stage = Usd.Stage.Open(str(stage_path))
    actors = {str(p.GetCustomDataByKey('actor_id')): p for p in stage.Traverse() if p.GetCustomDataByKey('actor_id')}
    errors = []
    for actor, track in brief.get('tracks', {}).items():
        prim, performance = actors.get(actor), performances.get(actor)
        animation_path = stage_path.parent / ('anim_'+actor+'.usda')
        animation = Usd.Stage.Open(str(animation_path)) if animation_path.exists() else None
        clip = animation.GetPrimAtPath('/BakedAnim').GetAttribute('ps:actionClip') if animation else None
        for seg in track.get('segments', []):
            if seg.get('lock', 'locked') != 'locked': continue
            if not prim or performance is None:
                errors.append({'code': 'BAKED_ACTOR_MISSING', 'actor': actor}); continue
            start, end = seg['t']
            for i in range(max(1, math.ceil((end-start)*48))):
                t = start+i/48
                actual = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode(t*fps)).ExtractTranslation()
                expected = performance.sample(t)[0]
                action = ALIASES.get(seg['action'], seg['action'])
                if action in ACTION_SPEEDS:
                    stop = min(end, t+1/48)
                    next_position = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode(stop*fps)).ExtractTranslation()
                    speed = math.dist(actual, next_position)/max(1e-9, stop-t)
                    low, high, _ = ACTION_SPEEDS[action]
                    if not low-.1 <= speed <= high+.1:
                        errors.append({'code': 'BAKED_SPEED', 'actor': actor, 'segment': seg['id'], 'time_s': t}); break
                if math.dist(actual, expected) > tolerance_m:
                    errors.append({'code': 'BAKED_PATH', 'actor': actor, 'segment': seg['id'], 'time_s': t}); break
                if not performance.is_vehicle:
                    expected_clip = next((a['clip'] for a in reversed(performance.actions) if a['start_s'] <= t < a['end_s']), None)
                    baked_t = t*fps
                    if not clip or clip.Get(Usd.TimeCode(baked_t)) != expected_clip:
                        errors.append({'code': 'BAKED_CLIP', 'actor': actor, 'segment': seg['id'], 'time_s': t}); break
    return errors
