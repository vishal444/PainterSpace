"""Allocate separate supported arrival slots near requested destination points."""
import math

from .config import CrowdSolverConfig
from .budget import check_deadline


def nearby_stops(source, goal, config):
    """Bounded alternatives on the goal's level, preferring no unnecessary move."""
    candidates = []
    if math.dist(source, goal) <= config.arrival_tolerance_m:
        candidates.append(list(source))
    # Sample toward the arriving actor first, then around the target area.
    base = math.atan2(source[2]-goal[2], source[0]-goal[0])
    for ring in range(1, config.destination_rings+1):
        distance = config.arrival_tolerance_m * ring/config.destination_rings
        count = max(1, math.ceil(math.tau*distance/config.cell_size_m))
        candidates.extend([goal[0]+distance*math.cos(base+i*math.tau/count), goal[1],
                           goal[2]+distance*math.sin(base+i*math.tau/count)] for i in range(count))
    return candidates[:config.destination_candidates]


def _arrival_candidates(request, pitch, config):
    """Compact rings, with slots facing the arriving actor considered first.

    Angular counts follow the chord distance, so neighbors on a ring are at
    least one pitch apart. Adjacent rings are also one pitch apart radially.
    Individual actors may use different bearings; joint checks remain required.
    """
    goal, source = request['goal'], request['source']
    base = math.atan2(source[2] - goal[2], source[0] - goal[0])
    candidates = [list(goal)]
    if (abs(source[1] - goal[1]) <= config.level_tolerance_m
            and math.dist(source, goal) <= config.arrival_tolerance_m
            and list(source) != list(goal)):
        candidates.append(list(source))
    for ring in range(1, config.destination_rings + 1):
        if len(candidates) >= config.destination_candidates:
            break
        distance = pitch * ring
        count = max(1, math.floor(math.nextafter(
            math.pi / math.asin(min(1., pitch / (2 * distance))), math.inf)))
        points = [[goal[0] + distance * math.cos(base + i * math.tau / count),
                   goal[1], goal[2] + distance * math.sin(base + i * math.tau / count)]
                  for i in range(count)]
        points.sort(key=lambda p: (math.dist(source, p), tuple(p)))
        candidates.extend(points)
    return candidates[:config.destination_candidates]


def _farther_arrival_rings(request, pitch, config):
    """Search supported scene space beyond the compact arrival neighborhood.

    The caller supplies a radius measured from support geometry. Keep the
    number of probes bounded while retaining angular coverage at every ring.
    """
    limit = request.get('search_radius_m', pitch * config.destination_rings)
    if pitch <= 0 or limit <= pitch * config.destination_rings:
        return
    goal, source = request['goal'], request['source']
    base = math.atan2(source[2] - goal[2], source[0] - goal[0])
    max_ring = math.ceil(limit / pitch)
    probes = 0
    angular_budget = max(1, config.destination_candidates // config.destination_rings)
    for ring in range(config.destination_rings + 1, max_ring + 1):
        distance = min(limit, pitch * ring)
        count = min(angular_budget, max(1, math.ceil(math.tau * distance / pitch)))
        points = [[goal[0] + distance * math.cos(base + i * math.tau / count),
                   goal[1], goal[2] + distance * math.sin(base + i * math.tau / count)]
                  for i in range(count)]
        points.sort(key=lambda p: (math.dist(source, p), tuple(p)))
        yield points[:max(0, config.max_destination_nodes - probes)]
        probes += len(points)
        if probes >= config.max_destination_nodes:
            break


def _separated(point, radius, other, other_radius, config):
    return (abs(point[1] - other[1]) > config.level_tolerance_m
            or math.hypot(point[0] - other[0], point[2] - other[2]) + 1e-7
            >= radius + other_radius + config.buffer_m)


def _overlapping_visits(a, b):
    return a['actor'] != b['actor'] and a['start'] < b['end'] and b['start'] < a['end']


def allocate_destinations(requests, clear, occupied, config=None):
    """Jointly scatter arrivals around their goals, without partial mutation.

    ``clear(request, point)`` checks support and solids. ``occupied(request)``
    supplies non-moving actors as (position, radius). Arrival slots reserve an
    interval, so disjoint visits can reuse space. No participant is stacked at
    a shared destination, including when that destination names another actor.
    Constrained requests are assigned first, and bounded backtracking revisits
    earlier choices instead of treating them as immutable obstacles. Exhaustion
    returns no assignment; it does not prove that no physical layout exists.
    """
    config = config or CrowdSolverConfig()
    if not requests:
        return {}, None
    choices = {}
    largest = max((r['radius'] for r in requests), default=0.)
    for request in requests:
        check_deadline()
        radius = request['radius']
        blockers = list(occupied(request))
        pitch = radius + max([largest] + [r for _, r in blockers]) + config.buffer_m
        # A zero-radius request with no buffer can only reuse its exact point.
        candidates = (_arrival_candidates(request, pitch, config) if pitch > 0
                      else [list(request['goal'])])
        def available(point):
            return (all(_separated(point, radius, p, r, config) for p, r in blockers)
                    and clear(request, point))
        choices[request['key']] = [point for point in candidates if available(point)]
        if not choices[request['key']]:
            for ring_points in _farther_arrival_rings(request, pitch, config):
                choices[request['key']] = [point for point in ring_points if available(point)]
                if choices[request['key']]:
                    break
        if not choices[request['key']]:
            return None, request['key']

    ordered = [request for _, request in sorted(enumerate(requests), key=lambda item:
               (len(choices[item[1]['key']]), -item[1]['radius'], item[0]))]
    assignments, next_choice = {}, [0] * len(ordered)
    depth, visited, deepest = 0, 0, 0
    # An explicit stack avoids Python's recursion limit for large casts.
    while depth >= 0:
        check_deadline()
        request = ordered[depth]
        options = choices[request['key']]
        chosen = None
        while next_choice[depth] < len(options):
            point = options[next_choice[depth]]
            next_choice[depth] += 1
            if any(_overlapping_visits(request, other)
                   and not _separated(point, request['radius'], assignments[other['key']],
                                      other['radius'], config) for other in ordered[:depth]):
                continue
            if visited >= config.max_destination_nodes:
                return None, request['key']
            visited += 1
            chosen = point
            break
        if chosen is None:
            deepest = max(deepest, depth)
            next_choice[depth] = 0
            depth -= 1
            if depth >= 0:
                assignments.pop(ordered[depth]['key'])
            continue
        assignments[request['key']] = chosen
        depth += 1
        if depth == len(ordered):
            return assignments, None
    return None, ordered[deepest]['key']
