"""Bounded static vehicle routing shared by placement and motion synthesis."""
import math
from functools import lru_cache

from crowd.budget import SearchTimeout, search_budget
from crowd.st_planner import plan_route
from route_placement import PlacementRoutes

# Numerical tolerance for contact with a measured support plane (metres).
SUPPORT_CONTACT_EPSILON_M = 1e-7


class VehicleRoutes(PlacementRoutes):
    def __init__(self, scene, config):
        super().__init__(scene, config)
        self.plan = lru_cache(maxsize=None)(self.plan)
        self.clear_vehicle = lru_cache(maxsize=None)(self.clear_vehicle)
        self._body_solids = lru_cache(maxsize=None)(self._body_solids)

    def _body_solids(self, lower, upper):
        from crowd.geometry import snapshot_solids
        # Vehicle roots cannot step over low objects like walking characters.
        # Ignore only surfaces at/below the support plane or above the body.
        return snapshot_solids([{'min': [lx, -math.inf, lz], 'max': [hx, math.inf, hz]}
                                for lx, ly, lz, hx, hy, hz in self.solids.compiled
                                if hy > lower + SUPPORT_CONTACT_EPSILON_M and ly < upper - SUPPORT_CONTACT_EPSILON_M])

    def clear_vehicle(self, a, b, radius, height):
        from crowd.geometry import clears_solids, supports_body_route
        solids = self._body_solids(min(a[1], b[1]), max(a[1], b[1]) + height)
        return (bool(self.surfaces)
                and clears_solids([a, b], solids, radius + self.config.buffer_m)
                and supports_body_route([a, b], radius, self.surfaces, self.config))

    def plan(self, start, goal, radius, height=math.inf):
        """Return a checked polyline or a reason; never substitute an unsafe line."""
        clear = lambda a, b: self.clear_vehicle(a, b, radius, height)
        if not clear(start, start):
            return None, 'invalid_start'
        if not clear(goal, goal):
            return None, 'invalid_destination'
        if clear(start, goal):
            return [list(start), list(goal)], ''
        if self.bounds is None:
            return None, 'missing_support_geometry'
        try:
            with search_budget(self.config.max_joint_time_s):
                result = plan_route(start, goal, 0., 1., radius, [],
                                    lambda path: all(clear(tuple(a), tuple(b))
                                                     for a, b in zip(path, path[1:] or path)),
                                    self.bounds, self.config)
                if result.points is None:
                    return None, result.status
                points = [list(p[1:]) for p in result.points]
                # Remove redundant grid corners only when the full swept
                # footprint still clears both scenery and support boundaries.
                simplified, index = [points[0]], 0
                while index < len(points) - 1:
                    following = next(i for i in range(len(points)-1, index, -1)
                                     if clear(tuple(points[index]), tuple(points[i])))
                    simplified.append(points[following])
                    index = following
                return simplified, ''
        except SearchTimeout:
            return None, 'time_limit'


def position_on_route(points, fraction):
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    remaining = max(0., min(1., fraction)) * sum(lengths)
    for a, b, length in zip(points, points[1:], lengths):
        if length > 0 and remaining <= length:
            return [x + (y-x) * remaining/length for x, y in zip(a, b)]
        remaining -= length
    return list(points[-1])


def check_vehicle_roots(performances, synthesizer, timeline=None):
    """Check all authored/generated vehicle segments, including stationary holds."""
    scene = synthesizer.scene_data
    vehicle_ids = {actor for actor, performance in performances.items() if performance.is_vehicle}
    # The compiler adds parked vehicle geometry as support for characters.
    # Vehicle checks use their live trajectories below, not those static copies.
    scene = dict(scene,
                 support_surfaces=[face for face in scene.get('support_surfaces', [])
                                   if face.get('owner') not in vehicle_ids],
                 solid_boxes=[box for box in scene.get('solid_boxes', [])
                              if box.get('owner') not in vehicle_ids])
    from crowd.config import CrowdSolverConfig
    config = getattr(synthesizer.ref_resolver, 'crowd_config', None) or CrowdSolverConfig()
    routes = (VehicleRoutes(scene, config)
              if scene.get('support_surfaces') or scene.get('solid_boxes') else None)
    for actor, performance in performances.items():
        if routes is None or not performance.is_vehicle or not performance.keyframes:
            continue
        radius = synthesizer._moving_body_radius(performance.asset_id)
        keys = performance.keyframes
        bounds = synthesizer.assets.get(performance.asset_id, {}).get('bounds_m')
        height = float(bounds[1]) if bounds else math.inf
        for a, b in zip(keys, keys[1:] or keys):
            if not routes.clear_vehicle(tuple(a.position), tuple(b.position), radius, height):
                return [{'actor': actor, 'start_s': a.time_s, 'end_s': b.time_s,
                         'start': a.position, 'destination': b.position,
                         'radius_m': radius, 'reason': 'unsupported_or_obstructed_segment'}]
    from crowd.st_planner import Reservation
    from crowd.joint import first_conflict
    reservations = [Reservation(actor, [(k.time_s, *k.position) for k in performance.keyframes],
                                synthesizer._moving_body_radius(performance.asset_id))
                    for actor, performance in performances.items()
                    if performance.is_vehicle and performance.keyframes]
    end = max((r.points[-1][0] for r in reservations), default=0.)
    exemptions = [(set((beat.actor_id, beat.params['target'])), beat.start_time, beat.end_time)
                  for beat in (timeline.beats if timeline is not None else [])
                  if beat.params.get('mode') == 'collide'
                  and isinstance(beat.params.get('target'), str)
                  and beat.params['target'] in performances]
    conflict = first_conflict(reservations, 0., end, config, exemptions, details=True)
    return [dict(conflict, reason='vehicle_trajectory_overlap')] if conflict else []


def prefix_route(points, fraction):
    """Retain every corner up to a fractional arc-length stop."""
    if fraction >= 1.:
        return points
    remaining = sum(math.dist(a, b) for a, b in zip(points, points[1:])) * max(0., fraction)
    result = [points[0]]
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        if remaining <= length:
            break
        result.append(b)
        remaining -= length
    result.append(position_on_route(points, fraction))
    return result
