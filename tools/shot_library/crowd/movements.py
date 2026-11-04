"""Reserve prompt ground locomotion before shared interaction approaches."""
import copy
import hashlib
import math

from plan_model import Beat, StructuredError
from support_surfaces import SUPPORT_LEVEL_TOLERANCE_M
from .st_planner import Reservation, plan_route, blocked_intervals, RouteResult
from .config import CrowdSolverConfig
from .destinations import allocate_destinations, nearby_stops
from .geometry import clears_solids, supported_disc, supports_body_route, snapshot_solids, fallback_obstacle_scene
from .budget import check_deadline
from .joint import PrefixReservation, first_conflict, priority_search
from .activities import activity_areas, first_activity_conflict
from .trajectories import planning_tracks, vehicle_reservations


def movement_source_plan(plan, preserve_approaches=False):
    """Restore compiler-generated routes to their requested movements."""
    result = copy.deepcopy(plan)
    cleaned = []
    for beat in result.beats:
        if beat.params.get('auto_interaction_approach') and not preserve_approaches:
            continue
        if beat.params.get('auto_crowd_movement'):
            original = beat.params.get('crowd_movement_source')
            if original:
                cleaned.append(Beat.from_dict(original))
        else:
            cleaned.append(beat)
    result.beats = cleaned
    return result


def route_movements(plan, states, resolver, synthesizer, scene, envelopes, config=None,
                    preserve_approaches=False):
    """Jointly negotiate precedence instead of freezing provisional peer paths."""
    config = config or CrowdSolverConfig()
    last_pair = None
    route_cache = {}
    if plan.spatial_intents or 'spatial_regions' not in scene:
        from spatial_regions import derive_spatial_regions
        scene = dict(scene, spatial_regions=derive_spatial_regions(scene, config=config))
    scene = dict(scene, solid_boxes=snapshot_solids(scene.get('solid_boxes', [])))
    movable = {b.actor_id for b in plan.beats if b.function in
               ('walk_to', 'run_to', 'jog_to', 'sprint_to')
               and not b.params.get('brief_path') and not b.params.get('exact_window')
               and envelopes.knows(b.actor_id)}
    def solve(order):
        return _route_simultaneous_movements(plan, states, resolver, synthesizer, scene, envelopes,
                                            config, preserve_approaches, order, route_cache)
    def conflict(result):
        nonlocal last_pair
        timeline, error = resolver.resolve(result, states)
        if error:
            raise ValueError(error.message)
        generated = [b for b in timeline.beats if b.params.get('auto_crowd_movement')]
        if not generated:
            return None
        actors = {b.actor_id for b in generated}
        tracks, _, error = planning_tracks(timeline, states, resolver, synthesizer, scene,
                                           seed=result.seed)
        if error:
            raise ValueError(error.message)
        reservations = []
        for actor in sorted(actors):
            windows = [(b.start_time, b.end_time,
                        max(envelopes.beat_reach.get(b.beat_id, 0.),
                            envelopes.movement_radius(actor, b.function)))
                       for b in timeline.beats if b.actor_id == actor]
            reservations.append(Reservation(actor, tracks[actor], envelopes.body[actor], windows))
        exemptions = [(set(b.params.get('participants', [])), b.start_time, b.end_time)
                      for b in timeline.beats if b.params.get('auto_interaction')]
        last_pair = first_conflict(reservations, min(b.start_time for b in generated),
                                   timeline.total_duration_s, config, exemptions)
        if last_pair is None:
            areas = activity_areas(timeline, tracks, envelopes, resolver._sample_traj,
                                   config.level_tolerance_m)
            last_pair = first_activity_conflict(reservations, areas,
                min(b.start_time for b in generated), timeline.total_duration_s, config)
        return last_pair
    def failure_conflicts(error):
        nonlocal last_pair
        # Reorder only adjustable actors that actually obstructed a checked
        # edge or hold. Scenery and authored/locked tracks are never relaxed.
        if error.error_code != 'CROWD_ROUTE_LIMIT' or error.actor_id not in movable:
            return []
        blockers = error.details.get('negotiable_blockers', error.details.get('blocking_actors', []))
        pairs = [(error.actor_id, other) for other in blockers
                 if other in movable and other != error.actor_id]
        if pairs:
            last_pair = pairs[0]
        return pairs
    result, error, visited = priority_search(solve, conflict, config.max_joint_nodes,
                                            failure_conflicts=failure_conflicts,
                                            max_time_s=config.max_joint_time_s,
                                            failure_signature=lambda error: error.details.get('failure_signature'))
    if result is not None:
        return result, None
    if error is None:
        error = StructuredError('CROWD_ROUTE_LIMIT',
            ('Joint trajectory coordination found no conflict-free priority order.'
             if visited < config.max_joint_nodes else
             'Joint trajectory coordination reached its priority search limit.'))
    error.details.update(joint_nodes=visited, max_joint_nodes=config.max_joint_nodes,
                         conflict_actors=last_pair, algorithm='conflict_directed_priority_search',
                         search_status=('priority_orders_exhausted' if visited < config.max_joint_nodes
                                        else 'node_limit_reached'))
    return None, error


def _route_simultaneous_movements(plan, states, resolver, synthesizer, scene, envelopes, config,
                                 preserve_approaches, order, route_cache):
    """Negotiate a shared departure, rerouting instead of shifting checked roots.

    Each iteration checks fresh waits, travel and destination holds. One total
    coordination deadline, max_joint_nodes iterations, and max_delay_s added
    departure delay bound the work. Authored paths/windows retain authority.
    """
    if not any(interaction.timing == 'simultaneous' for interaction in plan.interactions):
        return _route_movements(plan, states, resolver, synthesizer, scene, envelopes,
                               config, preserve_approaches, order, route_cache)
    from interaction_plan import validate_interactions
    candidate = movement_source_plan(plan, preserve_approaches)
    baseline, error = resolver.resolve(candidate, states)
    if error:
        return None, error
    original_starts = {beat.beat_id: beat.start_time for beat in baseline.beats}
    adjustable = {beat.beat_id for beat in candidate.beats
                  if beat.function in ('walk_to', 'run_to', 'jog_to', 'sprint_to')
                  and not beat.params.get('brief_path') and not beat.params.get('exact_window')
                  and envelopes.knows(beat.actor_id)}
    for attempt in range(config.max_joint_nodes):
        check_deadline()
        result, error = _route_movements(candidate, states, resolver, synthesizer, scene, envelopes,
                                        config, preserve_approaches, order, route_cache)
        if error:
            return None, error
        timeline, error = resolver.resolve(result, states)
        if error:
            return None, error
        error = validate_interactions(result, timeline, states)
        # Other timing conflicts are handled by final compiler validation;
        # only flexible ground movement phases can be synchronized here.
        if error is None or error.error_code != 'INTERACTION_SIMULTANEOUS':
            return result, None
        actions = error.details.get('actions', [])
        if not actions or not all(action['beat_id'] in adjustable for action in actions):
            return result, None
        departure = max(action['start_time_s'] for action in actions)
        delay = max(departure - original_starts[action['beat_id']] for action in actions)
        error.details.update(synchronization_attempts=attempt + 1,
                             max_synchronization_attempts=config.max_joint_nodes,
                             added_departure_delay_s=delay, max_delay_s=config.max_delay_s)
        if delay > config.max_delay_s + 1e-7 or attempt + 1 == config.max_joint_nodes:
            return None, error
        candidate = movement_source_plan(result, preserve_approaches)
        by_id = {beat.beat_id: beat for beat in candidate.beats}
        for action in actions:
            by_id[action['beat_id']].params['crowd_not_before_s'] = departure
    return None, error


def _route_movements(plan, states, resolver, synthesizer, scene, envelopes, config,
                     preserve_approaches, order=None, route_cache=None):
    """Replace adjustable semantic ground moves with timed paths and explicit waits.

    Authored paths, fixed director windows, vehicles and stairs retain their
    own contracts. Every later route checks earlier committed reservations.
    The final synthesized-root check guards against subsequent timing changes.
    """
    from director_solver import _approach_beats, _route_clear_of_discs
    config = config or CrowdSolverConfig()
    result = movement_source_plan(plan, preserve_approaches)
    spatial_bindings = {intent.movements[actor]: intent.bindings[actor]
                        for intent in result.spatial_intents for actor in intent.actors
                        if actor in intent.bindings}
    order = order or {}
    candidates = [b.beat_id for b in result.beats if b.function in ('walk_to', 'run_to', 'jog_to', 'sprint_to')
                  and not b.params.get('brief_path') and not b.params.get('exact_window')
                  and b.actor_id in envelopes.body and (b.params.get('target') or b.params.get('to'))]
    if candidates:
        authored = {b.beat_id: copy.deepcopy(b.to_dict()) for b in result.beats if b.beat_id in candidates}
        # A move directly into an automatically staged interaction arrives in
        # that interaction's allocated arena. Do not send actors to a separate
        # floor anchor only to bring them back for the very next action.
        for index, beat in enumerate(result.beats):
            if beat.beat_id not in candidates:
                continue
            following = next((b for b in result.beats[index+1:] if b.actor_id == beat.actor_id
                              and not b.params.get('auto_interaction_approach')), None)
            if (following and following.params.get('center_auto') and following.params.get('center')
                    and beat.actor_id in following.params.get('participants', [])):
                beat.params['target'] = following.params['center']
                beat.params['arrival_area_reference'] = following.params['center']
        baseline, error = resolver.resolve(result, states)
        if error: return None, error
        by_id = {b.beat_id: b for b in result.beats}
        tracks, vehicles, error = planning_tracks(
            baseline, states, resolver, synthesizer, scene, seed=result.seed)
        if error:
            return None, error
        resolved_by_id = {b.beat_id: b for b in baseline.beats}
        def priority(beat_id):
            b = resolved_by_id[beat_id]
            length = math.dist(resolver._sample_traj(tracks[b.actor_id], b.start_time),
                               resolver._sample_traj(tracks[b.actor_id], b.end_time))
            return (b.start_time, -length)
        for bid in candidates:
            resolved = resolved_by_id[bid]
            source = resolver._sample_traj(tracks[resolved.actor_id], resolved.start_time)
            goal = resolver._sample_traj(tracks[resolved.actor_id], resolved.end_time)
            if abs(source[1] - goal[1]) > SUPPORT_LEVEL_TOLERANCE_M:
                # The XZ solver cannot prove support across levels. Never hand
                # this movement back to unreserved spline synthesis silently.
                return None, StructuredError('CROWD_LEVEL_TRANSITION',
                    'Ground movement crosses support levels without an explicit vertical route.',
                    actor_id=resolved.actor_id, beat_id=bid,
                    details={'source_height_m': source[1], 'destination_height_m': goal[1],
                             'level_tolerance_m': SUPPORT_LEVEL_TOLERANCE_M},
                    suggestions=['Choose a destination on the same floor, or author a supported stair or vertical route.'])
        candidates.sort(key=priority)
        requests = []
        moving_actors = {by_id[b].actor_id for b in candidates}
        support_surfaces = scene.get('support_surfaces', [])
        for beat_id in candidates:
            b = resolved_by_id[beat_id]
            radius = max(envelopes.movement_radius(b.actor_id, b.function),
                         envelopes.beat_reach.get(beat_id, 0.))
            solid_radius = envelopes.solid_movement_radius(b.actor_id, b.function, beat_id)
            # Arrival holds include gestures following the move, not just its
            # locomotion clip. Move the destination away from furniture now.
            for following in result.beats[result.beats.index(by_id[beat_id]) + 1:]:
                if following.actor_id != b.actor_id:
                    continue
                if following.function in ('walk_to', 'run_to', 'jog_to', 'sprint_to', 'climb_stairs'):
                    break
                radius = max(radius, envelopes.beat_reach.get(following.beat_id, 0.))
                solid_radius = max(solid_radius, envelopes.solid_beat_reach.get(following.beat_id, 0.))
            binding = spatial_bindings.get(beat_id, {})
            radius = max(radius, binding.get('destination_radius_m', 0.))
            solid_radius = max(solid_radius, binding.get('destination_solid_radius_m', 0.))
            departure = min((later.start_time for later in baseline.beats
                             if later.actor_id == b.actor_id and later.start_time >= b.end_time - 1e-6
                             and later.beat_id != beat_id and (later.params.get('brief_path')
                             or later.function in ('walk_to', 'run_to', 'jog_to', 'sprint_to', 'climb_stairs'))),
                            default=baseline.total_duration_s)
            goal = list(resolver._sample_traj(tracks[b.actor_id], b.end_time))
            # Search only as far as measured ground on the destination level.
            # This lets a blocked local neighborhood use open space elsewhere
            # without inventing unsupported destinations or a scene-specific cap.
            search_radius = max((math.hypot(vertex[0] - goal[0], vertex[1] - goal[2])
                                 for surface in support_surfaces
                                 if abs(float(surface['height_m']) - goal[1]) <= config.level_tolerance_m
                                 for vertex in surface.get('polygon_xz', [])), default=0.)
            # Compass destinations are region contracts too. Preserve the
            # selected side while allocating alternate arrival slots.
            direction_region = None
            from semantic_references import direction_heading
            reference = by_id[beat_id].params.get('target')
            if direction_heading(reference) is not None:
                state = dict(states[b.actor_id],
                             position=list(resolver._sample_traj(tracks[b.actor_id], b.start_time)))
                destination, error = resolver.ref_resolver.resolve(
                    reference, {b.actor_id: state}, source_actor_id=b.actor_id,
                    clearance_radius_m=solid_radius)
                if error or destination is None:
                    return None, StructuredError('CROWD_DESTINATION_LIMIT',
                                                 error or 'Cannot resolve directional arrival region.',
                                                 actor_id=b.actor_id, beat_id=beat_id)
                direction_region = resolver.ref_resolver._spatial_resolver.regions[destination.target_name]
            requests.append({'key': beat_id, 'actor': b.actor_id,
                             'goal': goal,
                             'direction_region': direction_region,
                             'source': list(resolver._sample_traj(tracks[b.actor_id], b.start_time)),
                             'radius': radius, 'start': b.end_time,
                             'solid_radius': solid_radius,
                             'source_region': by_id[beat_id].params.get('spatial_source_region'),
                             'destination_region': (by_id[beat_id].params.get('spatial_destination_region')
                                 or next((region['id'] for region in scene.get('spatial_regions', [])
                                          if region['id'] == by_id[beat_id].params.get('target')), None)),
                             'search_radius_m': search_radius,
                             'end': max(departure, b.end_time + config.departure_step_s)})
        non_moving_reservations = []
        for other, track in tracks.items():
            if other in moving_actors or not envelopes.knows(other):
                continue
            windows = [(b.start_time, b.end_time,
                        max(envelopes.beat_reach.get(b.beat_id, 0.),
                            envelopes.movement_radius(other, b.function)))
                       for b in baseline.beats if b.actor_id == other]
            non_moving_reservations.append(Reservation(other, track, envelopes.body[other], windows))
        planned_areas = activity_areas(baseline, tracks, envelopes, resolver._sample_traj,
                                       config.level_tolerance_m)
        area_reservations = [(area.members, area.reservation()) for area in planned_areas]
        def destination_clear(request, point):
            if request.get('direction_region'):
                from spatial_regions import region_contains
                if not region_contains(request['direction_region'], point, request['solid_radius'], config):
                    return False
            if request['source_region'] or request['destination_region']:
                from spatial_regions import region_contains, source_direction_ok
                index = {region['id']: region for region in scene.get('spatial_regions', [])}
                source_region, destination_region = index.get(request['source_region']), index.get(request['destination_region'])
                if (request['destination_region'] and (not destination_region
                        or not region_contains(destination_region, point, request['solid_radius'], config))):
                    return False
                if (request['source_region'] and (not source_region
                        or not source_direction_ok(source_region, request['source'], point))):
                    return False
            if not (supported_disc(point, request['solid_radius'], scene.get('support_surfaces', []), config)
                    and clears_solids([point], scene.get('solid_boxes', []),
                                      request['solid_radius'] + config.buffer_m)):
                return False
            hold_obstacles = [r for r in non_moving_reservations if r.actor != request['actor']]
            hold_obstacles.extend(res for members, res in area_reservations if request['actor'] not in members)
            hold_obstacles.extend(vehicle_reservations(
                tracks, vehicles, synthesizer, request['solid_radius'] - request['radius']))
            if hold_obstacles and blocked_intervals(point, point, request['start'], request['end'],
                                                    request['radius'], hold_obstacles, config):
                return False
            return True
        def occupied(request):
            # An actor's later movement does not release its current action
            # area. Shared actions also reserve the space between participants.
            for area in planned_areas:
                if request['actor'] not in area.members and area.overlaps(request['start'], request['end']):
                    yield area.position, area.radius
            for other, track in tracks.items():
                if other == request['actor'] or other in moving_actors or not envelopes.knows(other):
                    continue
                # Check the whole hold, including known stationary performances.
                times = {request['start'], request['end']}
                times.update(t[0] for t in track if request['start'] <= t[0] <= request['end'])
                times.update(b.start_time for b in baseline.beats if b.actor_id == other
                             and request['start'] <= b.start_time < request['end'])
                for when in sorted(times):
                    yield (resolver._sample_traj(track, when),
                           envelopes.radius_at(other, when, baseline.beats))
        cached_alloc = route_cache.get('_destinations') if route_cache is not None else None
        if cached_alloc is None:
            destinations, failed = allocate_destinations(requests, destination_clear, occupied, config)
            if route_cache is not None:
                route_cache['_destinations'] = (
                    {k: list(v) for k, v in destinations.items()} if destinations else None,
                    failed,
                )
        else:
            cached_dest, failed = cached_alloc
            destinations = {k: list(v) for k, v in cached_dest.items()} if cached_dest else None
        if failed is not None:
            return None, StructuredError('CROWD_DESTINATION_LIMIT',
                'No separate supported arrival position found within the measured ground search area.',
                beat_id=failed, actor_id=by_id[failed].actor_id,
                details={'destination_rings': config.destination_rings,
                         'destination_candidates': config.destination_candidates,
                         'max_destination_nodes': config.max_destination_nodes,
                         'search_radius_m': next(r['search_radius_m'] for r in requests
                                                 if r['key'] == failed)})
        # Establish all assigned goals together before routing any participant.
        # Keep the authored source for deterministic recompilation and events.
        for bid in candidates:
            by_id[bid].params['target'] = destinations[bid]
        # Ancestor counts give a topological order; retain chronological order
        # among an actor's movements. Allocation itself stays priority independent.
        candidates.sort(key=lambda bid: len(order.get(by_id[bid].actor_id, ())))
    moving_actors = {b.actor_id for b in result.beats if b.beat_id in candidates}
    static_cache = route_cache.setdefault('_static', {}) if route_cache is not None else None
    for beat_id in candidates:
        check_deadline()
        timeline, error = resolver.resolve(result, states)
        if error: return None, error
        by_id = {b.beat_id: b for b in result.beats}
        beat = by_id[beat_id]
        resolved = next(b for b in timeline.beats if b.beat_id == beat_id)
        trajectories, vehicles, error = planning_tracks(
            timeline, states, resolver, synthesizer, scene, seed=result.seed)
        if error:
            return None, error
        actor = beat.actor_id
        request = next(r for r in requests if r['key'] == beat_id)
        travel_radius = max(envelopes.movement_radius(actor, beat.function),
                            envelopes.beat_reach.get(beat_id, 0.))
        solid_radius = envelopes.solid_movement_radius(actor, beat.function, beat_id)
        binding = spatial_bindings.get(beat_id, {})
        travel_radius = max(travel_radius, binding.get('source_radius_m', 0.))
        solid_radius = max(solid_radius, binding.get('source_solid_radius_m', 0.))
        source = resolver._sample_traj(trajectories[actor], resolved.start_time)
        goal = resolver._sample_traj(trajectories[actor], resolved.end_time)
        # Allocation must retain the level contract checked above.
        if abs(source[1] - goal[1]) > SUPPORT_LEVEL_TOLERANCE_M:
            return None, StructuredError('CROWD_LEVEL_TRANSITION',
                'Allocated ground destination changed support levels.',
                actor_id=actor, beat_id=beat_id)
        distance = math.dist(source, goal)
        via = beat.params.get('spatial_via_points', [])
        if distance < 1e-6 and not via:
            # A no-op arrival still carries the allocation and its original goal.
            beat.params['auto_crowd_movement'] = beat_id
            beat.params['crowd_movement_source'] = authored[beat_id]
            beat.params['brief_path'] = {'points': [list(source), list(source)]}
            beat.params['arrival_position'] = list(source)
            continue
        stops = [source, *via, goal]
        travel_distance = sum(math.dist(a, b) for a, b in zip(stops, stops[1:]))
        speed = travel_distance / max(resolved.duration, 1e-6)
        reservations = []
        traj_reservations = []
        for other, trajectory in trajectories.items():
            if other == actor or not envelopes.knows(other): continue
            windows = [(b.start_time, b.end_time,
                        max(envelopes.beat_reach.get(b.beat_id, 0.), envelopes.movement_radius(other, b.function)))
                       for b in timeline.beats if b.actor_id == other and
                       (b.beat_id in envelopes.beat_reach or b.function in
                        ('walk_to', 'run_to', 'jog_to', 'sprint_to'))]
            reservation = Reservation(other, trajectory, envelopes.body[other], windows)
            if other in moving_actors and other not in order.get(actor, ()):
                departure = min((b.start_time for b in timeline.beats
                                 if b.actor_id == other and (b.beat_id in candidates or
                                     b.params.get('auto_crowd_movement'))), default=math.inf)
                reservation = PrefixReservation(reservation, departure)
            reservations.append(reservation)
            traj_reservations.append(reservation)
        # These fixed action windows survive prefix trimming and precedence
        # retries. A route may pass here only outside the action's active time.
        reservations.extend(area.reservation() for area in activity_areas(
            timeline, trajectories, envelopes, resolver._sample_traj, config.level_tolerance_m)
            if actor not in area.members)
        reservations.extend(vehicle_reservations(
            trajectories, vehicles, synthesizer,
            max(solid_radius - travel_radius, request['solid_radius'] - request['radius'])))
        surfaces = [s for s in scene.get('support_surfaces', [])
                    if abs(s['height_m'] - source[1]) <= SUPPORT_LEVEL_TOLERANCE_M]
        vertices = [p for s in surfaces for p in s['polygon_xz']]
        points = [source, goal] + [p[1:] for r in reservations for p in r.points]
        padding = distance + envelopes.body[actor]
        bounds = ((min(p[0] for p in vertices), max(p[0] for p in vertices),
                   min(p[1] for p in vertices), max(p[1] for p in vertices)) if vertices else
                  (min(p[0] for p in points) - padding, max(p[0] for p in points) + padding,
                   min(p[2] for p in points) - padding, max(p[2] for p in points) + padding))
        obstacles = synthesizer._collect_obstacles_at_time(resolved.start_time, resolved.start_time,
                                                          actor, source, goal, {}, fallback_obstacle_scene(scene))
        obs_key = tuple((o['cx'], o['cz'], o['radius']) for o in obstacles)
        static_prefix = (solid_radius, source[1], obs_key)
        def static_clear(path):
            if static_cache is not None:
                path_key = (static_prefix, tuple(tuple(p) for p in path))
                cached_val = static_cache.get(path_key)
                if cached_val is not None:
                    return cached_val
            val = (_route_clear_of_discs(path, obstacles,
                lambda ps: supports_body_route(ps, solid_radius, surfaces, config),
                clearance_margin=0.0) and clears_solids(
                    path, scene.get('solid_boxes', []), solid_radius + config.buffer_m))
            if static_cache is not None:
                static_cache[path_key] = val
            return val
        hold_until = min((b.start_time for b in timeline.beats
                               if b.actor_id == actor and b.start_time >= resolved.end_time - 1e-6
                               and b.beat_id != beat_id and
                               (b.params.get('brief_path') or b.function in ('walk_to', 'run_to', 'jog_to', 'climb_stairs'))),
                               default=timeline.total_duration_s)
        horizon = max(hold_until, resolved.start_time + travel_distance / speed + config.max_delay_s)
        # Every searched edge is inside bounds or joins a source/fallback stop.
        # Reservations disjoint from this whole region cannot affect this query.
        stops = [source, goal] + nearby_stops(source, request['goal'], config)
        region = (min(bounds[0], *(p[0] for p in stops)),
                  max(bounds[1], *(p[0] for p in stops)),
                  min(bounds[2], *(p[2] for p in stops)),
                  max(bounds[3], *(p[2] for p in stops)))
        low_y = min(p[1] for p in stops)
        high_y = max(p[1] for p in stops)
        relevant = []
        for r in reservations:
            reach = max(travel_radius, request['radius'], r.mover_radius or 0.) + r._max_radius + config.buffer_m
            if (r._x_max + reach < region[0] or r._x_min - reach > region[1]
                    or r._z_max + reach < region[2] or r._z_min - reach > region[3]
                    or r._y_max + config.level_tolerance_m < low_y
                    or r._y_min - config.level_tolerance_m > high_y):
                continue
            relevant.append(r)
        reservations = relevant
        reservation_keys = []
        for reservation in reservations:
            intervals = tuple((a, b, tuple(p), tuple(q), radius) for a, b, p, q, radius
                              in reservation.intervals(resolved.start_time, horizon))
            if intervals:
                reservation_keys.append((reservation.actor, reservation.mover_radius, intervals))
        cache_key = (actor, beat_id, tuple(source), tuple(goal), resolved.start_time, speed,
                     travel_radius, solid_radius, request['radius'], hold_until,
                     tuple(bounds), tuple(reservation_keys), tuple(tuple(p) for p in via))
        cached_entry = route_cache.get(cache_key) if route_cache is not None else None
        if cached_entry is not None:
            route, cached_goal, cached_blockers = cached_entry
            goal = list(cached_goal)
            measured_blockers = set(cached_blockers)
        else:
            route = plan_route(source, goal, resolved.start_time, speed, travel_radius,
                               reservations, static_clear, bounds, config, hold_until=hold_until,
                               hold_radius=request['radius'], via=via)
            measured_blockers = set(route.blocking_actors)
            if route.points is None and route.status != 'start_occupied':
                alternatives = nearby_stops(source, request['goal'], config)
                attempts = 0
                for stop in alternatives:
                    check_deadline()
                    if attempts >= config.arrival_route_candidates:
                        break
                    if abs(stop[1]-source[1]) > SUPPORT_LEVEL_TOLERANCE_M:
                        continue
                    if not destination_clear(request, stop):
                        continue
                    if any(r['actor'] != actor and r['start'] < request['end']
                           and request['start'] < r['end']
                           and abs(stop[1]-destinations[r['key']][1]) <= config.level_tolerance_m
                           and math.dist(stop, destinations[r['key']]) + 1e-7
                           < request['radius'] + r['radius'] + config.buffer_m for r in requests):
                        continue
                    attempts += 1
                    stop_points = [source, *via, stop]
                    stop_distance = sum(math.dist(a, b) for a, b in zip(stop_points, stop_points[1:]))
                    if stop_distance < 1e-6:
                        # Staying still is an arrival, not an unchecked escape:
                        # reserve the entire hold, including following gestures.
                        if blocked_intervals(source, source, resolved.start_time, horizon,
                                             request['radius'], reservations, config, measured_blockers):
                            continue
                        alternative = RouteResult([(resolved.start_time, *source),
                                                   (resolved.end_time, *source)], 'ok')
                    else:
                        stop_speed = stop_distance / max(resolved.duration, 1e-6)
                        alternative = plan_route(source, stop, resolved.start_time, stop_speed, travel_radius,
                                                 reservations, static_clear, bounds, config,
                                                 hold_until=hold_until, hold_radius=request['radius'], via=via)
                        measured_blockers.update(alternative.blocking_actors)
                        if alternative.points is not None and blocked_intervals(
                                stop, stop, alternative.points[-1][0], horizon,
                                request['radius'], reservations, config, measured_blockers):
                            continue
                    if alternative.points is not None:
                        route, goal = alternative, list(stop)
                        break
                    if attempts >= config.arrival_route_candidates:
                        break
            if route_cache is not None:
                route_cache[cache_key] = (route, list(goal), tuple(sorted(measured_blockers)))
        if route.points is None:
            active_traj_actors = {
                r.actor for r in traj_reservations
                if getattr(r, '_t_max', math.inf) > resolved.start_time
                and getattr(r, '_t_min', -math.inf) < horizon
            }
            traj_blockers = sorted(b for b in measured_blockers
                                   if b in active_traj_actors and b in moving_actors)
            if route.critical_blockers:
                negotiable = sorted(b for b in route.critical_blockers if b in moving_actors)
            else:
                negotiable = traj_blockers or sorted(b for b in measured_blockers if b in moving_actors)
            return None, StructuredError('CROWD_ROUTE_LIMIT',
                'No reserved route found for the prompt movement within the planning limits.',
                beat_id=beat_id, actor_id=actor,
                details={'planner_status': route.status, 'expansions': route.expansions,
                         'max_delay_s': config.max_delay_s,
                         'route_grid_levels': config.route_grid_levels,
                         'blocking_actors': sorted(measured_blockers),
                         'negotiable_blockers': negotiable,
                         'routed_beats': candidates[:candidates.index(beat_id)],
                         'failure_signature': hashlib.sha256(repr(cache_key).encode()).hexdigest()})
        replacements = _approach_beats('crowd_' + beat_id, actor, route.points)
        used = set(by_id)
        for replacement in replacements:
            replacement.params.pop('auto_interaction_approach', None)
            replacement.params['auto_crowd_movement'] = beat_id
            if replacement.function != 'wait':
                replacement.params = {**authored[beat_id]['params'], **replacement.params,
                                      'arrival_position': list(goal)}
                if beat.params.get('clip') and not beat.params.get('variant'):
                    replacement.params['variant'] = beat.params['clip']
                replacement.function = beat.function
                replacement.params['action'] = beat.function
            elif authored[beat_id]['params'].get('wait_for'):
                replacement.params['wait_for'] = list(authored[beat_id]['params']['wait_for'])
            while replacement.beat_id in used:
                replacement.beat_id += '_route'
            used.add(replacement.beat_id)
        if replacements:
            replacements[-1].beat_id = beat_id  # Preserve .finished event references.
            replacements[-1].params['crowd_movement_source'] = authored[beat_id]
            replacements[-1].params['arrival_position'] = list(goal)
            if beat.params.get('arrival_area_reference'):
                replacements[-1].params['arrival_area_reference'] = beat.params['arrival_area_reference']
            index = result.beats.index(beat)
            result.beats[index:index + 1] = replacements
    return result, None
