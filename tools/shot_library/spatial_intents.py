"""Bind declared spatial intent to jointly checked source and arrival layouts."""
import copy
import math
import heapq
from dataclasses import replace
from functools import lru_cache

from crowd.arena_layout import assign_arenas
from crowd.budget import search_budget, SearchTimeout, check_deadline
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids
from crowd_spacing import _actor_reach
from interaction_plan import apply_interactions, _action_interval
from plan_model import StructuredError, TimingCondition
from spatial_regions import (derive_spatial_regions, region_contains, sample_region,
                             nearest_source_point, source_direction_ok, source_seed, valid_destination)
from spatial_regions import intent_source_ids, origin_layout_valid, visits_in_order
from support_surfaces import WALK_OVER_MAX_HEIGHT_M, STANDING_BODY_HEIGHT_M


GAIT_FUNCTIONS = {'walk': 'walk_to', 'run': 'run_to', 'jog': 'jog_to'}
# Calibrated nominal clip speeds shared with MotionSynthesizer's defaults.
DEFAULT_GAIT_SPEEDS_MPS = {'walk': 1.4, 'run': 4.2, 'jog': 2.6}
# Numerical guard for nearly reversing turns when deriving follower headway.
DIRECTION_COSINE_EPSILON = 1e-6


def _error(code, message, intent, **details):
    return StructuredError(code, message, details={'intent_id': intent.intent_id, **details},
                           suggestions=['Use current scene-owned region IDs and compatible ground-level actors; '
                                        'increase the bounded layout budgets or choose different measured regions if needed.'])


def bind_spatial_intents(plan, states, scene, manifest, root, config=None, locked_actors=(), variant=0):
    """Commit only a complete binding; failure leaves both inputs untouched."""
    if not plan.spatial_intents:
        return None
    config = config or CrowdSolverConfig()
    candidate, predicted = copy.deepcopy(plan), copy.deepcopy(states)
    regions = {region['id']: region for region in derive_spatial_regions(scene, manifest, config)}
    seen_actors, seen_ids = set(), set()
    setups = {setup.actor_id: setup for setup in candidate.setup if setup.action_type == 'place'}
    beats = {beat.beat_id: beat for beat in candidate.beats}
    locked = set(locked_actors)
    for intent in candidate.spatial_intents:
        if not intent.intent_id or intent.intent_id in seen_ids:
            return _error('SPATIAL_INTENT_SCHEMA', 'Spatial intent IDs must be nonempty and unique.', intent)
        seen_ids.add(intent.intent_id)
        if (not intent.actors or len(set(intent.actors)) != len(intent.actors)
                or set(intent.movements) != set(intent.actors) or intent.gait not in GAIT_FUNCTIONS):
            return _error('SPATIAL_INTENT_SCHEMA', 'Provide distinct actors and one supported ground movement per actor.', intent)
        source, destination = regions.get(intent.source_region), regions.get(intent.destination_region)
        if not source or not valid_destination(destination, regions):
            return _error('SPATIAL_REGION_REFERENCE', 'Unknown, stale or incompatible source/destination region.', intent,
                          source_region=intent.source_region, destination_region=intent.destination_region)
        try:
            source_ids = intent_source_ids(intent, regions, variant)
            route_regions = [regions[key] for key in list(source_ids.values()) + intent.via_regions]
        except (KeyError, ValueError) as exc:
            return _error('SPATIAL_REGION_REFERENCE', str(exc), intent)
        if any(abs(region['height_m'] - destination['height_m']) > config.level_tolerance_m for region in route_regions):
            return _error('SPATIAL_REGION_LEVEL', 'Approach regions must share a support level; author a vertical route explicitly.', intent)
        interaction = next((item for item in candidate.interactions if item.interaction_id == intent.intent_id), None)
        if (interaction is None or interaction.timing != intent.departure_timing
                or set(interaction.actions) != set(intent.movements.values())):
            return _error('SPATIAL_INTENT_TIMING', 'Spatial movement and its declared departure interaction must agree.', intent)
        for actor in intent.actors:
            if actor in seen_actors or actor not in setups or actor not in predicted:
                return _error('SPATIAL_INTENT_ACTORS', 'Each placed actor may have one initial region approach contract.', intent, actor=actor)
            seen_actors.add(actor)
            beat = beats.get(intent.movements[actor])
            if beat is None or beat.actor_id != actor or beat.function != GAIT_FUNCTIONS[intent.gait]:
                return _error('SPATIAL_INTENT_MOVEMENT', 'The linked movement is missing or incompatible with the requested gait.', intent, actor=actor)
            if actor in locked or beat.params.get('brief_path') or beat.params.get('exact_window'):
                return _error('SPATIAL_INTENT_LOCKED', 'Region allocation cannot overwrite Director tracks or authored routes/windows.', intent, actor=actor)
            if predicted[actor].get('is_vehicle') or predicted[actor].get('seated_vehicle'):
                return _error('SPATIAL_INTENT_ACTORS', 'Region approaches require unseated ground characters.', intent, actor=actor)
            prior = [item for item in candidate.beats[:candidate.beats.index(beat)] if item.actor_id == actor]
            if any(item.function not in ('wait', 'idle', 'turn_to_face') for item in prior):
                return _error('SPATIAL_INTENT_SOURCE', 'Only an initial approach can allocate a new starting position.', intent, actor=actor)
    # Resolve clip durations, including named travel/performance durations,
    # before reserving swept animation envelopes. Targets bind below.
    error = apply_interactions(candidate, predicted)
    if error:
        return error
    from timeline_resolver import TimelineResolver
    timeline, error = TimelineResolver().resolve(candidate, predicted)
    if error:
        return error
    durations = {beat.beat_id: beat.duration for beat in timeline.beats}
    fixed = [(state['position'], _actor_reach(actor, state.get('asset_id', ''), candidate, manifest, root,
                                             durations=durations))
             for actor, state in predicted.items() if actor not in seen_actors and state.get('position') is not None]
    try:
        with search_budget(config.max_joint_time_s):
            for intent in candidate.spatial_intents:
                source_ids = intent_source_ids(intent, regions, variant)
                sources = {actor: regions[key] for actor, key in source_ids.items()}
                destination = regions[intent.destination_region]
                error = _bind_one(candidate, predicted, intent, sources, destination, scene, manifest,
                                  root, config, fixed, durations, setups, beats, variant, regions)
                if error:
                    return error
                for binding in intent.bindings.values():
                    if isinstance(binding, dict) and 'source' in binding:
                        fixed.extend([(binding['source'], binding['source_radius_m']),
                                      (binding['destination'], binding['destination_radius_m'])])
    except SearchTimeout:
        return StructuredError('SPATIAL_INTENT_LIMIT', 'Spatial layout search reached its shared time budget.',
                               details={'max_joint_time_s': config.max_joint_time_s})
    plan.setup, plan.beats, plan.interactions = candidate.setup, candidate.beats, candidate.interactions
    plan.spatial_intents = candidate.spatial_intents
    states.clear()
    states.update(predicted)
    return None


def _bind_one(plan, states, intent, sources, destination, scene, manifest, root, config,
              occupied, durations, setups, beats, variant, regions):
    actors = intent.actors
    source = sources[actors[0]]
    source_radii, destination_radii, source_solid, destination_solid = {}, {}, {}, {}
    explicit = {}
    for actor in actors:
        movement = beats[intent.movements[actor]]
        schedule = [beat for beat in plan.beats if beat.actor_id == actor]
        index = schedule.index(movement)
        end = next((i for i in range(index + 1, len(schedule))
                    if schedule[i].function in GAIT_FUNCTIONS.values() or schedule[i].function == 'climb_stairs'), len(schedule))
        initial = replace(plan, beats=schedule[:index + 1])
        arrival = replace(plan, beats=schedule[index:end])
        appearance = states[actor].get('appearance') or setups[actor].appearance
        scale = float(appearance.get('height_scale', 1.) if isinstance(appearance, dict)
                      else getattr(appearance, 'height_scale', 1.))
        # Existing crowd travel envelopes are measured at the base rig scale.
        # Reserve at least that envelope; larger variants need extra room.
        scale = max(1., scale)
        for output, selected, limbs in ((source_radii, initial, not config.allow_actor_limb_contacts),
                                        (destination_radii, arrival, not config.allow_actor_limb_contacts),
                                        (source_solid, initial, True), (destination_solid, arrival, True)):
            output[actor] = scale * _actor_reach(actor, states[actor]['asset_id'], selected, manifest, root,
                                               include_limbs=limbs, durations=durations)
        old = intent.bindings.get(actor, {})
        if setups[actor].position is not None and not old.get('generated_start'):
            explicit[actor] = list(setups[actor].position)
        elif setups[actor].at_reference and not old.get('generated_start'):
            from semantic_references import ReferenceResolver
            reference = setups[actor].at_reference
            # Advertised standing markers are placement preferences, matching
            # the standing allocator's contract. Other authored references
            # retain their resolved position and must satisfy the region.
            if not any(token in reference for token in (':standing_left_', ':standing_right_')):
                resolved, _ = ReferenceResolver(manifest, scene).resolve(reference, states, source_actor_id=actor)
                if resolved is None:
                    return _error('SPATIAL_INTENT_SOURCE', 'An authored source reference cannot be resolved.', intent, actor=actor)
                explicit[actor] = list(resolved.position)

    @lru_cache(maxsize=None)
    def clear(region_id, point, radius):
        check_deadline()
        region = regions[region_id]
        return (region_contains(region, point, radius, config)
                and clears_solids([point], scene.get('solid_boxes', []), radius + config.buffer_m))

    used_regions = {r['id']: r for r in [*sources.values(), destination]}
    pools = {key: [] for key in used_regions}
    # Seed a gathering formation across the measured approach direction.
    # Local envelope-derived seeds avoid spending a global floor sample budget
    # to discover actor-sized spacing in a small gathering area.
    center = destination['preferred']
    anchor = nearest_source_point(source, center)
    dx, dz = anchor[0] - center[0], anchor[2] - center[2]
    length = math.hypot(dx, dz)
    if length:
        normal = [dx / length, dz / length]
    else:
        bounds = destination['bounds_xz']
        normal = [1., 0.] if bounds[2] - bounds[0] >= bounds[3] - bounds[1] else [0., 1.]
    tangent = [-normal[1], normal[0]]
    radius = max(destination_radii.values())
    pitch = max(config.cell_size_m, 2 * radius + config.buffer_m)
    if variant:
        # A golden-angle spiral explores alternative gathering centers using
        # measured actor pitch, without scene-specific retry coordinates.
        angle = variant * math.pi * (3 - math.sqrt(5))
        distance = math.sqrt(variant) * pitch
        center = [center[0] + distance * (math.cos(angle) * tangent[0] + math.sin(angle) * normal[0]),
                  center[1],
                  center[2] + distance * (math.cos(angle) * tangent[1] + math.sin(angle) * normal[1])]
    projections = [p[0] * tangent[0] + p[1] * tangent[1]
                   for face in destination['faces'] for p in face['polygon_xz']]
    columns = min(len(actors), max(1, math.floor((max(projections) - min(projections) - 2 * radius) / pitch) + 1))
    rows = math.ceil(len(actors) / columns)
    preferred = {}
    origin_groups = {}
    if intent.origin_layout != 'specified':
        for actor in actors:
            origin_groups.setdefault(tuple(sources[actor]['entry_normal']), []).append(actor)
        # Incoming groups retain separate arrival fronts. Matching each front
        # to its entry direction avoids funneling parallel walkers into a ring.
        group_columns = {}
        for direction, group in origin_groups.items():
            axis = 1 if direction[0] else 0
            bounds = destination['bounds_xz']
            span = bounds[axis + 2] - bounds[axis]
            group_columns[direction] = min(len(group), max(1, math.floor((span - 2 * radius) / pitch) + 1))
        front_offset = (pitch / 2 if len(origin_groups) == 2 else
                        (max(group_columns.values()) - 1) * pitch / 2 + pitch)
    for index, actor in enumerate(actors):
        row, column = divmod(index, columns)
        count_in_row = min(columns, len(actors) - row * columns)
        along = (column - (count_in_row - 1) / 2) * pitch
        across = (row - (rows - 1) / 2) * pitch
        point = [center[0] + along * tangent[0] + across * normal[0], destination['height_m'],
                 center[2] + along * tangent[1] + across * normal[1]]
        if origin_groups:
            direction = tuple(sources[actor]['entry_normal'])
            group = origin_groups[direction]
            group_row, group_column = divmod(group.index(actor), group_columns[direction])
            row_count = min(group_columns[direction], len(group) - group_row * group_columns[direction])
            along = (group_column - (row_count - 1) / 2) * pitch
            across = front_offset + group_row * pitch
            point = [center[0] + direction[0] * across - direction[1] * along, destination['height_m'],
                     center[2] + direction[1] * across + direction[0] * along]
        preferred[actor] = point
        pools[destination['id']].append({'name': 'formation_' + actor, 'position': point})
    if origin_groups:
        # Translate the whole formation before assigning individual slots.
        # Obstacle edges supply geometry-derived alternatives, so a blocked
        # preferred center need not force a large cast into crossing routes.
        offsets = {actor: [point[0] - center[0], point[2] - center[2]]
                   for actor, point in preferred.items()}
        extent = [min(p[i] for p in offsets.values()) for i in (0, 1)] + [
                  max(p[i] for p in offsets.values()) for i in (0, 1)]
        pad = max(destination_solid.values()) + config.buffer_m
        axes = [{center[0]}, {center[2]}]
        for box in scene.get('solid_boxes', []):
            if (box['max'][1] <= destination['height_m'] + WALK_OVER_MAX_HEIGHT_M
                    or box['min'][1] >= destination['height_m'] + STANDING_BODY_HEIGHT_M):
                continue
            for i, axis in enumerate((0, 2)):
                axes[i].update((box['min'][axis] - extent[i + 2] - pad,
                                box['max'][axis] - extent[i] + pad))
        axes = [sorted(values, key=lambda v: abs(v - center[axis]))[:config.layout_candidates]
                for values, axis in zip(axes, (0, 2))]
        centers = heapq.nsmallest(config.layout_candidates,
            ((math.hypot(x - center[0], z - center[2]), x, z) for x in axes[0] for z in axes[1]))
        for _, x, z in centers:
            check_deadline()
            points = {a: [x + offset[0], destination['height_m'], z + offset[1]]
                      for a, offset in offsets.items()}
            if all(clear(destination['id'], tuple(p), destination_solid[a]) for a, p in points.items()):
                for actor, point in points.items():
                    preferred[actor][:] = point
                break
    from route_placement import PlacementRoutes
    placement_routes = PlacementRoutes(scene, config)

    @lru_cache(maxsize=None)
    def route_waypoints(actor, goal):
        via_points = []
        for region_id in intent.via_regions:
            region = regions[region_id]
            radius = source_solid[actor] + config.buffer_m
            seed = source_seed(region, goal, radius)
            options = [{'position': seed}]
            for level in range(config.standing_search_levels):
                options.extend(sample_region(region, scene, config, level, variant))
            options = [spot['position'] for spot in options
                       if clear(region_id, tuple(spot['position']), source_solid[actor])]
            if region['kind'] == 'corner':
                # A far point in the same quadrant is not evidence of passing
                # this corner. Keep discretization alternatives local to the
                # clearance-expanded footprint corner.
                options = [point for point in options if math.dist(point, seed) <= config.cell_size_m]
            if not options:
                return None
            via_points.append(min(options, key=lambda p: math.dist(p, region['preferred'])))
        return tuple(tuple(point) for point in via_points)

    seen = {key: set() for key in pools}
    attempts = []
    chosen_sources = None
    chosen_goals = None
    gait_table = manifest.get('assets', [{}])[0].get('gait_table', {}) if manifest.get('assets') else {}
    speed = float(gait_table.get(intent.gait, {}).get('speed_mps', DEFAULT_GAIT_SPEEDS_MPS[intent.gait]))
    if not math.isfinite(speed) or speed <= 0:
        return _error('SPATIAL_INTENT_GAIT', 'The nominal gait speed must be finite and positive.', intent)
    for level in range(config.standing_search_levels):
        check_deadline()
        for region in used_regions.values():
            for spot in sample_region(region, scene, config, level, variant):
                key = tuple(spot['position'])
                if key not in seen[region['id']]:
                    pools[region['id']].append(spot)
                    seen[region['id']].add(key)
        goal_candidates = {actor: [spot for spot in pools[destination['id']]
                                   if clear(destination['id'], tuple(spot['position']), destination_solid[actor])]
                           for actor in actors}
        goals = [{'key': actor, 'radius': destination_radii[actor], 'solid_radius': destination_solid[actor],
                  'sources': [preferred[actor]], 'preferred': preferred[actor]} for actor in actors]
        source_candidates = {actor: [spot for spot in pools[sources[actor]['id']]
                                     if clear(sources[actor]['id'], tuple(spot['position']), source_solid[actor])]
                             for actor in actors}

        def accepts(assignment):
            nonlocal chosen_sources
            requests, candidates = [], {}
            for actor in actors:
                check_deadline()
                source = sources[actor]
                goal = assignment[actor]['position']
                via_points = route_waypoints(actor, tuple(goal))
                if via_points is None:
                    return False
                stops = (*via_points, tuple(goal))
                approach = stops[0]
                anchor = nearest_source_point(source, approach)
                if intent.via_regions and intent.departure_timing == 'simultaneous':
                    # Seed followers along the incoming route, with spacing
                    # derived from the group's protected motion envelopes.
                    # Equal-speed followers then turn at different times even
                    # though everyone departs simultaneously.
                    group = [a for a in actors if sources[a]['id'] == source['id']]
                    dx, dz = anchor[0] - approach[0], anchor[2] - approach[2]
                    length = math.hypot(dx, dz)
                    gap = 2 * max(source_radii[a] for a in group) + config.buffer_m
                    for member in group:
                        member_goal = tuple(assignment[member]['position'])
                        member_via = route_waypoints(member, member_goal)
                        if member_via is None:
                            return False
                        member_stops = (*member_via, member_goal)
                        route = [nearest_source_point(sources[member], member_stops[0]), *member_stops]
                        for a, b, c in zip(route, route[1:], route[2:]):
                            incoming = [b[i] - a[i] for i in (0, 2)]
                            outgoing = [c[i] - b[i] for i in (0, 2)]
                            product = math.hypot(*incoming) * math.hypot(*outgoing)
                            if product:
                                cosine = max(-1., min(1., sum(x*y for x, y in zip(incoming, outgoing)) / product))
                                half_cosine = math.sqrt((1. + cosine) / 2.)
                                # A turn reduces straight-line separation between
                                # equal-speed followers. Reserve the corresponding
                                # arc-distance headway before ranking source slots.
                                gap = max(gap, (2 * max(source_radii[a] for a in group) + config.buffer_m)
                                          / max(half_cosine, DIRECTION_COSINE_EPSILON))
                    offset = group.index(actor) * gap
                    if length:
                        anchor = [anchor[0] + dx * offset / length, anchor[1],
                                  anchor[2] + dz * offset / length]
                requests.append({'key': actor, 'radius': source_radii[actor], 'solid_radius': source_solid[actor],
                                 'sources': [anchor], 'preferred': anchor,
                                 'candidate_cost': lambda spot, stops=stops, actor=actor, anchor=tuple(anchor):
                                     placement_routes.cost(spot['position'], stops, source_solid[actor],
                                                           preferred=anchor)})
                seed = source_seed(source, approach, source_solid[actor] + config.buffer_m)
                options = ([{'name': 'authored', 'position': explicit[actor]}] if actor in explicit
                           else source_candidates[actor] + [{'name': 'source_seed_' + actor, 'position': seed},
                                                             {'name': 'approach_seed_' + actor, 'position': anchor}])
                candidates[actor] = [spot for spot in options
                                     if source_direction_ok(source, spot['position'], goal)
                                     and clear(source['id'], tuple(spot['position']), source_solid[actor])]
            def compatible_departures(chosen):
                for actor in actors:
                    if actor in explicit:
                        continue
                    stops = (*route_waypoints(actor, tuple(assignment[actor]['position'])),
                             tuple(assignment[actor]['position']))
                    if not placement_routes.reachable(tuple(chosen[actor]['position']), stops,
                                                      source_solid[actor], speed):
                        return False
                if not origin_layout_valid(intent, sources,
                        {a: chosen[a]['position'] for a in actors},
                        {a: assignment[a]['position'] for a in actors}, destination):
                    return False
                if (intent.departure_timing != 'simultaneous'
                        or intent.origin_layout != 'specified'):
                    return True
                # Reconsider endpoint layouts that force walkers through an
                # earlier arrival. Continuous straight-line reservations are
                # a conservative proposal filter; full routing still follows.
                from crowd.st_planner import Reservation
                from crowd.joint import first_conflict
                durations_by_actor, paths = {}, {}
                for actor in actors:
                    movement = beats[intent.movements[actor]]
                    authored_duration = (movement.duration_condition is not None
                                         and not intent.bindings.get(actor, {}).get('generated_duration'))
                    route = [chosen[actor]['position'],
                             *route_waypoints(actor, tuple(assignment[actor]['position'])),
                             assignment[actor]['position']]
                    distances = [math.dist(a, b) for a, b in zip(route, route[1:])]
                    length = sum(distances)
                    duration = durations[movement.beat_id] if authored_duration else length / speed
                    durations_by_actor[actor] = duration
                    elapsed = 0.
                    points = [(0., *route[0])]
                    for distance, point in zip(distances, route[1:]):
                        elapsed += duration * distance / length if length else 0.
                        points.append((elapsed, *point))
                    paths[actor] = points
                horizon = max(durations_by_actor.values()) + config.departure_step_s
                reservations = [Reservation(actor, paths[actor], source_radii[actor],
                                            [(durations_by_actor[actor], horizon, destination_radii[actor])])
                                for actor in actors]
                return first_conflict(reservations, 0., horizon, config) is None

            chosen_sources = assign_arenas(requests, candidates, occupied,
                                           lambda point, radius: True, config, accept=compatible_departures)
            return chosen_sources is not None

        diagnostics = {'level': level, 'variant': variant}
        chosen_goals = assign_arenas(goals, goal_candidates, occupied,
                                     lambda point, radius: True, config,
                                     accept=accepts, diagnostics=diagnostics)
        attempts.append(diagnostics)
        if chosen_goals is not None:
            break
    if chosen_goals is None:
        return _error('SPATIAL_INTENT_LIMIT', 'No complete supported source/arrival layout found within the spatial search budget.',
                      intent, attempts=attempts, standing_samples=config.standing_samples,
                      layout_candidates=config.layout_candidates, layout_beam_width=config.layout_beam_width,
                      source_candidates=len(pools[source['id']]), destination_candidates=len(pools[destination['id']]))
    bindings = {}
    for actor in actors:
        start, goal = list(chosen_sources[actor]['position']), list(chosen_goals[actor]['position'])
        via_points = [list(point) for point in route_waypoints(actor, tuple(goal))]
        setup, movement = setups[actor], beats[intent.movements[actor]]
        old = intent.bindings.get(actor, {})
        source = sources[actor]
        generated_heading = (setup.facing_deg is None and not setup.facing_reference) or old.get('generated_heading', False)
        if actor not in explicit:
            setup.at_reference, setup.position = None, start
        states[actor]['position'], states[actor]['at_reference'] = start, setup.at_reference
        if generated_heading:
            first_target = via_points[0] if via_points else goal
            heading = math.degrees(math.atan2(first_target[0] - start[0], start[2] - first_target[2])) % 360
            setup.facing_deg = states[actor]['facing_deg'] = heading
        generated_duration = movement.duration_condition is None or old.get('generated_duration', False)
        if generated_duration:
            route = [start, *via_points, goal]
            movement.duration_condition = TimingCondition('for_duration', sum(math.dist(a, b) for a, b in zip(route, route[1:])) / speed)
        if via_points:
            movement.params['spatial_via_points'] = via_points
        else:
            movement.params.pop('spatial_via_points', None)
        movement.params.update(target=goal, spatial_intent=intent.intent_id,
                               spatial_source_region=source['id'], spatial_destination_region=destination['id'])
        bindings[actor] = {'source': start, 'destination': goal, 'generated_start': actor not in explicit,
                           'source_region': source['id'],
                           'via_points': via_points,
                           'generated_heading': generated_heading, 'generated_duration': generated_duration,
                           'source_radius_m': source_radii[actor], 'destination_radius_m': destination_radii[actor],
                           'source_solid_radius_m': source_solid[actor], 'destination_solid_radius_m': destination_solid[actor]}
    bindings['_search'] = {'attempts': len(attempts), 'variant': variant}
    intent.bindings = bindings
    return None


def validate_spatial_intents(plan, timeline, performances, scene, manifest, config=None):
    if not plan.spatial_intents:
        return None
    config = config or CrowdSolverConfig()
    regions = {region['id']: region for region in derive_spatial_regions(scene, manifest, config)}
    scheduled = {beat.beat_id: beat for beat in timeline.beats}
    by_id = {beat.beat_id: beat for beat in plan.beats}
    for intent in plan.spatial_intents:
        destination = regions.get(intent.destination_region)
        try:
            declared = intent_source_ids(intent, regions)
            sources = {a: regions[intent.bindings.get(a, {}).get('source_region', declared[a])]
                       for a in intent.actors}
        except (KeyError, ValueError):
            sources = {}
        if not sources or not valid_destination(destination, regions):
            return _error('SPATIAL_REGION_REFERENCE', 'A spatial region changed before conformance validation.', intent)
        starts, goals = {}, {}
        for actor in intent.actors:
            source = sources[actor]
            movement = by_id.get(intent.movements[actor])
            binding = intent.bindings.get(actor)
            performance = performances.get(actor)
            if not movement or not binding or not performance or not performance.keyframes:
                return _error('SPATIAL_INTENT_CONFORMANCE', 'Missing bound movement or synthesized trajectory.', intent, actor=actor)
            interval = _action_interval(movement, scheduled, timeline.beats)
            if interval is None:
                return _error('SPATIAL_INTENT_CONFORMANCE', 'The arrival event is missing.', intent, actor=actor)
            start, _ = performance.sample(interval.start_time)
            goal, _ = performance.sample(interval.end_time)
            starts[actor], goals[actor] = start, goal
            if intent.origin_layout == 'specified' and source['id'] != declared[actor]:
                return _error('SPATIAL_INTENT_CONFORMANCE', 'Bound source differs from the declared origin.', intent, actor=actor)
            if not (region_contains(source, start, binding['source_solid_radius_m'], config)
                    and region_contains(destination, goal, binding['destination_solid_radius_m'], config)
                    and source_direction_ok(source, start, goal)):
                return _error('SPATIAL_INTENT_CONFORMANCE', 'Synthesized source/arrival motion violates the declared regions or direction.',
                              intent, actor=actor, source=list(start), destination=list(goal))
            via_points = binding.get('via_points', [])
            if len(via_points) != len(intent.via_regions):
                return _error('SPATIAL_INTENT_CONFORMANCE', 'Required route areas have no bound waypoints.', intent, actor=actor)
            for key, point in zip(intent.via_regions, via_points):
                region = regions.get(key)
                if region is None or not region_contains(region, point, binding['source_solid_radius_m'], config):
                    return _error('SPATIAL_INTENT_CONFORMANCE', 'A route waypoint left its declared area.', intent, actor=actor)
                if region['kind'] == 'corner' and math.dist(point, source_seed(
                        region, goal, binding['source_solid_radius_m'] + config.buffer_m)) > config.cell_size_m:
                    return _error('SPATIAL_ROUTE_CONFORMANCE', 'A corner waypoint is too far from its measured footprint corner.',
                                  intent, actor=actor)
            points = [list(start)] + [list(k.position) for k in performance.keyframes
                      if interval.start_time < k.time_s < interval.end_time] + [list(goal)]
            if not visits_in_order(points, via_points, config.cell_size_m):
                return _error('SPATIAL_ROUTE_CONFORMANCE', 'Synthesized motion skipped or reordered required route waypoints.',
                              intent, actor=actor)
        if not origin_layout_valid(intent, sources, starts, goals, destination):
            return _error('SPATIAL_ORIGIN_CONFORMANCE', 'Synthesized origins violate the declared distribution around the gathering.', intent)
    return None
