"""Static route checks for compiler-selected starting positions.

Prefer clear approaches before asking the bounded router to find detours.
This is a placement proposal check, not a replacement for timed routing or
vehicle steering and final collision validation.
"""
import math
from functools import lru_cache

from crowd.geometry import clears_solids, supports_body_route, snapshot_solids
from crowd.st_planner import plan_route


class PlacementRoutes:
    def __init__(self, scene, config):
        self.config = config
        self.surfaces = scene.get('support_surfaces', [])
        self.solids = snapshot_solids(scene.get('solid_boxes', []))
        vertices = [p for face in self.surfaces for p in face['polygon_xz']]
        self.bounds = ((min(p[0] for p in vertices), max(p[0] for p in vertices),
                        min(p[1] for p in vertices), max(p[1] for p in vertices)) if vertices else None)
        # Caches belong to one placement solve; class-level caches would retain
        # old scene snapshots and edge queries across repeated compilations.
        self.clear = lru_cache(maxsize=None)(self.clear)
        self.reachable = lru_cache(maxsize=None)(self.reachable)

    def clear(self, a, b, radius):
        return (clears_solids([a, b], self.solids, radius + self.config.buffer_m)
                and supports_body_route([a, b], radius, self.surfaces, self.config))

    def cost(self, start, stops, radius, preferred=None):
        route = [start, *stops]
        length = sum(math.dist(a, b) for a, b in zip(route, route[1:]))
        blocked = sum(not self.clear(tuple(a), tuple(b), radius) for a, b in zip(route, route[1:]))
        # A blocked approach ranks after every clear route in the measured
        # extent, rather than using a fixed scene-specific penalty.
        span = (math.hypot(self.bounds[1] - self.bounds[0], self.bounds[3] - self.bounds[2])
                if self.bounds else length)
        preference = math.dist(start, preferred) if preferred is not None else length
        return preference + blocked * (len(route) * span + self.config.cell_size_m)

    def reachable(self, start, stops, radius, speed):
        route = [start, *stops]
        if all(self.clear(a, b, radius) for a, b in zip(route, route[1:])):
            return True
        if self.bounds is None:
            return False
        result = plan_route(start, stops[-1], 0., speed, radius, [],
                            lambda path: all(self.clear(tuple(a), tuple(b), radius)
                                             for a, b in zip(path, path[1:] or path)),
                            self.bounds, self.config, via=stops[:-1])
        return result.points is not None


def place_unpositioned_movers(plan, states, scene, manifest, root, config, locked_actors=()):
    """Jointly choose starts only for new movers with no authored location.

    Existing actors, named anchors, seats, spatial contracts, exact paths
    and Director tracks retain their own placement authority. Compass placements
    can move within their measured directional regions. Vehicle proposals
    use a circumscribed body disc; steering is still checked by synthesis.
    """
    from crowd_spacing import GROUND_TRAVEL_CLIPS, _actor_reach
    from crowd.arena_layout import assign_arenas
    from semantic_references import ReferenceResolver, direction_heading
    from spatial_regions import derive_spatial_regions, sample_region
    from motion_synthesis import MotionSynthesizer
    from plan_model import StructuredError
    from crowd.budget import search_budget, SearchTimeout

    spatial = {actor for intent in plan.spatial_intents for actor in intent.actors}
    protected = set(locked_actors) | spatial
    resolver = ReferenceResolver(manifest, scene, config)
    synth = MotionSynthesizer(manifest)
    from vehicle_routes import VehicleRoutes
    routes = PlacementRoutes(scene, config)
    vehicle_routes = VehicleRoutes(scene, config)
    records = {}
    direction_regions = {}
    from spatial_references import SpatialReferenceResolver
    from spatial_regions import region_contains
    spatial_resolver = SpatialReferenceResolver(manifest, scene, config)
    for setup in plan.setup:
        actor = setup.actor_id
        if (setup.action_type != 'place' or actor in protected or actor not in states
                or (setup.position is not None and direction_heading(setup.at_reference) is None)
                or (setup.at_reference and direction_heading(setup.at_reference) is None)
                or states[actor].get('seated_vehicle')):
            continue
        beats = [beat for beat in plan.beats if beat.actor_id == actor]
        movement = next((beat for beat in beats if beat.function not in ('wait', 'idle', 'turn_to_face')), None)
        if (movement is None or movement.function not in set(GROUND_TRAVEL_CLIPS) | {'drive_to', 'reverse_to'}
                or movement.params.get('brief_path') or movement.params.get('exact_window')):
            continue
        reference = movement.params.get('target')
        if isinstance(reference, str) and reference.split(':', 1)[0] in states:
            continue
        target, error = resolver.resolve(reference, states, source_actor_id=actor)
        # Relative or moving targets must be resolved by the motion scheduler,
        # rather than freezing a placement-dependent target into this proposal.
        if error or target is None or target.target_type not in ('point', 'zone', 'anchor', 'region'):
            continue
        state = states[actor]
        radius = (synth._moving_body_radius(state['asset_id']) if state.get('is_vehicle') else
                  _actor_reach(actor, state['asset_id'], plan, manifest, root))
        records[actor] = (setup, movement, radius, tuple(float(v) for v in target.position))
        if direction_heading(setup.at_reference) is not None:
            direction_regions[actor] = spatial_resolver.directional_regions(setup.at_reference, state)
    if not records or not routes.bounds:
        return None
    from timeline_resolver import TimelineResolver
    timeline, error = TimelineResolver(ref_resolver=resolver).resolve(plan, states)
    if error:
        return None  # The normal scheduler reports unresolved timing/targets.
    durations = {beat.beat_id: beat.duration for beat in timeline.beats}
    speeds = {actor: max(math.dist(states[actor]['position'], goal), config.cell_size_m)
                    / durations[movement.beat_id]
              for actor, (_, movement, _, goal) in records.items()
              if durations.get(movement.beat_id, 0.) > 0.}
    if len(speeds) != len(records):
        return None
    floors = [r for r in derive_spatial_regions(scene, manifest, config) if r['kind'] == 'floor']
    groups, candidates = [], {}
    for actor, (setup, movement, radius, goal) in records.items():
        original = tuple(states[actor]['position'])
        def cost(spot, original=original, goal=goal, radius=radius):
            # Preserve the provisional location when it already offers a clear
            # approach; otherwise prefer a nearby clear departure corridor.
            return routes.cost(spot['position'], [goal], radius, preferred=original)
        groups.append({'key': actor, 'radius': radius, 'sources': [original], 'preferred': original,
                       'candidate_cost': cost})
        candidates[actor] = [{'name': 'initial', 'position': list(original)}]
    occupied = []
    for actor, state in states.items():
        if actor not in records and state.get('position') is not None:
            radius = (synth._moving_body_radius(state['asset_id']) if state.get('is_vehicle') else
                      _actor_reach(actor, state['asset_id'], plan, manifest, root))
            occupied.append((state['position'], radius))
    def allowed(actor, point):
        return actor not in direction_regions or any(
            region_contains(region, point, records[actor][2], config)
            for region in direction_regions[actor])

    def accepts(chosen):
        # Vehicle synthesis uses the same checked polyline planner.
        return all((vehicle_routes.plan(tuple(chosen[actor]['position']), goal, radius,
                            float(synth.assets[states[actor]['asset_id']]['bounds_m'][1])
                            if synth.assets.get(states[actor]['asset_id'], {}).get('bounds_m') else math.inf)[0] is not None
                    if states[actor].get('is_vehicle') else
                    routes.reachable(tuple(chosen[actor]['position']), (goal,), radius, speeds[actor]))
                   for actor, (_, _, radius, goal) in records.items())
    chosen = None
    try:
        with search_budget(config.max_joint_time_s):
            for level in range(config.standing_search_levels):
                for actor in records:
                    original = states[actor]['position']
                    for floor in direction_regions.get(actor, floors):
                        if abs(floor['height_m'] - original[1]) <= config.level_tolerance_m:
                            candidates[actor].extend(sample_region(floor, scene, config, level))
                eligible = {actor: [spot for spot in spots if allowed(actor, spot['position'])]
                            for actor, spots in candidates.items()}
                chosen = assign_arenas(groups, eligible, occupied,
                                       lambda point, radius: routes.clear(tuple(point), tuple(point), radius),
                                       config, accept=accepts)
                if chosen is not None:
                    break
    except SearchTimeout:
        return StructuredError('START_ROUTE_LIMIT', 'Starting-layout search reached its time limit.',
                               details={'max_joint_time_s': config.max_joint_time_s})
    if chosen is None:
        return StructuredError('START_ROUTE_LIMIT',
            'No complete clear, reachable starting layout found within placement search limits.',
            details={'actors': list(records), 'standing_samples': config.standing_samples,
                     'layout_candidates': config.layout_candidates})
    for actor, (setup, movement, radius, goal) in records.items():
        point = list(chosen[actor]['position'])
        setup.position = point
        states[actor]['position'] = point
        if setup.facing_deg is None and not setup.facing_reference:
            heading = math.degrees(math.atan2(goal[0] - point[0], point[2] - goal[2]))
            if movement.function == 'reverse_to':
                heading += 180.
            setup.facing_deg = states[actor]['facing_deg'] = heading % 360.
    return None
