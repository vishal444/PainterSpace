"""Safe-interval A* with continuous swept-disc checks and bounded departures.

The grid and sampled departure times make this an incomplete planner. Every
accepted edge (including waits and goal holds) is checked continuously against
piecewise-linear reservations. Exhaustion is not evidence of infeasibility.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
import heapq
import math

from .config import CrowdSolverConfig
from .budget import check_deadline

EPS = 1e-7


def _overlap_interval(a, b, radius, level):
    """Fractions where linear relative displacement intersects a standing disc."""
    radius = max(0., radius - EPS)
    lo, hi = 0.0, 1.0
    dy = b[1] - a[1]
    if abs(dy) < EPS:
        if abs(a[1]) > level:
            return None
    else:
        y0, y1 = sorted(((-level - a[1]) / dy, (level - a[1]) / dy))
        lo, hi = max(lo, y0), min(hi, y1)
    dx, dz = b[0] - a[0], b[2] - a[2]
    aa = dx * dx + dz * dz
    bb = 2 * (a[0] * dx + a[2] * dz)
    cc = a[0] ** 2 + a[2] ** 2 - radius ** 2
    if aa < EPS * EPS:
        if cc >= 0:
            return None
    else:
        discriminant = bb * bb - 4 * aa * cc
        if discriminant <= 0:
            return None
        root = math.sqrt(discriminant)
        lo = max(lo, (-bb - root) / (2 * aa))
        hi = min(hi, (-bb + root) / (2 * aa))
    return (lo, hi) if hi > lo + EPS else None


class Reservation:
    """An actor holds its last position until it actually departs, even after a beat."""
    def __init__(self, actor, points, radius, radius_windows=(), mover_radius=None):
        self.actor = actor
        # Drop redundant samples, retaining endpoints and changes of velocity.
        compact = []
        for item in sorted(points):
            point = tuple(float(v) for v in item)
            if compact and abs(point[0] - compact[-1][0]) < EPS:
                compact[-1] = point
                continue
            while len(compact) >= 2:
                a, b = compact[-2:]
                if any(abs((b[k] - a[k]) * (point[0] - b[0]) -
                           (point[k] - b[k]) * (b[0] - a[0])) > EPS for k in (1, 2, 3)):
                    break
                compact.pop()
            compact.append(point)
        if not compact:
            raise ValueError("A reservation needs a position")
        self.points = compact
        self.times = [p[0] for p in compact]
        self.radius = radius
        self.windows = tuple(radius_windows)
        self.mover_radius = mover_radius
        # Reservations are immutable during a solve. Prepare their change points
        # once rather than sorting every key and reach window for every edge.
        self._cuts = sorted(set(self.times) |
                            {t for a, b, _ in self.windows for t in (a, b)})
        self._reach_times = sorted({t for a, b, _ in self.windows for t in (a, b)})
        self._reaches = [max([radius] + [r for a, b, r in self.windows
                                        if a <= (left + right) / 2 < b])
                         for left, right in zip(self._reach_times, self._reach_times[1:])]
        self._max_radius = max([radius] + [r for _, _, r in self.windows])
        self._x_min = min(p[1] for p in compact)
        self._x_max = max(p[1] for p in compact)
        self._y_min = min(p[2] for p in compact)
        self._y_max = max(p[2] for p in compact)
        self._z_min = min(p[3] for p in compact)
        self._z_max = max(p[3] for p in compact)
        self._t_min = -math.inf
        self._t_max = math.inf

    def position(self, t):
        i = bisect_right(self.times, t)
        if i == 0:
            return self.points[0][1:]
        if i == len(self.points):
            return self.points[-1][1:]
        a, b = self.points[i - 1:i + 1]
        u = (t - a[0]) / (b[0] - a[0])
        return tuple(a[k] + u * (b[k] - a[k]) for k in (1, 2, 3))

    def intervals(self, t0, t1):
        if t1 <= t0:
            return
        cuts = [t0, *self._cuts[bisect_right(self._cuts, t0):bisect_left(self._cuts, t1)], t1]
        for left, right in zip(cuts, cuts[1:]):
            middle = (left + right) / 2
            index = bisect_right(self._reach_times, middle) - 1
            radius = self._reaches[index] if 0 <= index < len(self._reaches) else self.radius
            yield left, right, self.position(left), self.position(right), radius


def blocked_intervals(source, destination, start, end, radius, reservations, config,
                      blocking_actors=None):
    result = []
    duration = end - start
    if duration <= EPS:
        end = start + EPS * 2
        duration = end - start
    sx, sy, sz = source[0], source[1], source[2]
    dx, dy, dz = destination[0], destination[1], destination[2]
    q_xmin = sx if sx <= dx else dx
    q_xmax = dx if sx <= dx else sx
    q_ymin = sy if sy <= dy else dy
    q_ymax = dy if sy <= dy else sy
    q_zmin = sz if sz <= dz else dz
    q_zmax = dz if sz <= dz else sz
    level_tol = config.level_tolerance_m
    buf = config.buffer_m
    for other in reservations:
        check_deadline()
        if end <= getattr(other, '_t_min', -math.inf) or start >= getattr(other, '_t_max', math.inf):
            continue
        querying_radius = radius if other.mover_radius is None else other.mover_radius
        max_reach = querying_radius + getattr(other, '_max_radius', other.radius) + buf
        if (q_xmax + max_reach < getattr(other, '_x_min', -math.inf)
                or q_xmin - max_reach > getattr(other, '_x_max', math.inf)
                or q_zmax + max_reach < getattr(other, '_z_min', -math.inf)
                or q_zmin - max_reach > getattr(other, '_z_max', math.inf)
                or q_ymax + level_tol < getattr(other, '_y_min', -math.inf)
                or q_ymin - level_tol > getattr(other, '_y_max', math.inf)):
            continue
        for a, b, p, q, other_radius in other.intervals(start, end):
            u, v = (a - start) / duration, (b - start) / duration
            rel_a = [sx + u * (dx - sx) - p[0], sy + u * (dy - sy) - p[1], sz + u * (dz - sz) - p[2]]
            rel_b = [sx + v * (dx - sx) - q[0], sy + v * (dy - sy) - q[1], sz + v * (dz - sz) - q[2]]
            overlap = _overlap_interval(rel_a, rel_b, querying_radius + other_radius + buf,
                                        level_tol)
            if overlap:
                result.append((a + overlap[0] * (b - a), a + overlap[1] * (b - a)))
                if blocking_actors is not None:
                    blocking_actors.update(getattr(other, 'members', (other.actor,)))
    return sorted(result)


@dataclass
class RouteResult:
    points: list | None  # (absolute time, x, y, z); repeated positions represent waits
    status: str
    expansions: int = 0
    blocking_actors: tuple = ()  # All measured conflicts retained for diagnostics.
    critical_blockers: tuple = ()  # Exact blockers when the start itself is occupied.


def plan_route(start, goal, start_time, speed, radius, reservations, static_clear,
               bounds, config=None, hold_s=0.0, hold_until=None, hold_radius=None, via=None):
    """Try coarse-to-fine grids with unchanged geometric clearance checks.

    Large detours need fewer nodes on a coarse grid; narrow passages can fall
    back to the configured base cell size. All levels share max_expansions,
    and every accepted long edge is checked continuously just like a fine edge.
    """
    config = config or CrowdSolverConfig()
    if via:
        # Ordered route constraints share one expansion budget. Every leg uses
        # the same static geometry and reservations; only the last leg holds.
        points, used, blockers, critical = [], 0, set(), set()
        delay_remaining = config.max_delay_s
        stops = [start, *via, goal]
        for index, (a, b) in enumerate(zip(stops, stops[1:])):
            check_deadline()
            if used >= config.max_expansions:
                return RouteResult(None, 'expansion_limit', used, tuple(sorted(blockers)), tuple(sorted(critical)))
            last_leg = index == len(stops) - 2
            leg = plan_route(a, b, points[-1][0] if points else start_time, speed, radius,
                             reservations, static_clear, bounds,
                             replace(config, max_expansions=config.max_expansions - used,
                                     max_delay_s=delay_remaining),
                             hold_s if last_leg else 0., hold_until if last_leg else None,
                             hold_radius if last_leg else radius)
            used += leg.expansions
            blockers.update(leg.blocking_actors)
            critical.update(leg.critical_blockers)
            if leg.points is None:
                return RouteResult(None, leg.status, used, tuple(sorted(blockers)), tuple(sorted(critical)))
            delay_remaining = max(0., delay_remaining - max(0.,
                leg.points[-1][0] - leg.points[0][0] - math.dist(a, b) / speed))
            points.extend(leg.points if not points else leg.points[1:])
        return RouteResult(points, 'ok', used, tuple(sorted(blockers)), tuple(sorted(critical)))
    used, blockers, critical = 0, set(), set()
    last = RouteResult(None, 'expansion_limit')
    for level in range(config.route_grid_levels):
        check_deadline()
        remaining = config.max_expansions - used
        if remaining <= 0:
            break
        budget = max(1, remaining // (config.route_grid_levels - level))
        grid = replace(config, cell_size_m=config.cell_size_m * 2 ** (config.route_grid_levels - level - 1),
                       max_expansions=budget)
        last = _plan_route(start, goal, start_time, speed, radius, reservations, static_clear,
                           bounds, grid, hold_s, hold_until, hold_radius)
        used += last.expansions
        blockers.update(last.blocking_actors)
        critical.update(last.critical_blockers)
        if last.points is not None or last.status in ('unsupported_endpoint', 'start_occupied'):
            break
    return RouteResult(last.points, last.status, used, tuple(sorted(blockers)), tuple(sorted(critical)))


def _plan_route(start, goal, start_time, speed, radius, reservations, static_clear,
                bounds, config=None, hold_s=0.0, hold_until=None, hold_radius=None):
    config = config or CrowdSolverConfig()
    hold_radius = radius if hold_radius is None else max(radius, hold_radius)
    if speed <= 0 or radius < 0 or hold_s < 0:
        raise ValueError("Invalid route speed, radius or hold")
    latest_arrival = start_time + math.dist(start, goal) / speed + config.max_delay_s
    horizon = latest_arrival + hold_s
    if hold_until is not None:
        horizon = max(horizon, hold_until + EPS * 2)
    def hold_end(arrival):
        return max(arrival + hold_s, hold_until if hold_until is not None else arrival)
    if not static_clear([start]) or not static_clear([goal]):
        return RouteResult(None, "unsupported_endpoint")

    # An occupied source cannot be escaped by waiting or by choosing another
    # goal. Check the actual departure instant, not every future source visit.
    source_blockers = set()
    if blocked_intervals(start, start, start_time, start_time, radius,
                         reservations, config, source_blockers):
        exact = tuple(sorted(source_blockers))
        return RouteResult(None, 'start_occupied', 0, exact, exact)

    blocking_actors = set()
    def failure(status, expansions=0):
        return RouteResult(None, status, expansions, tuple(sorted(blocking_actors)))

    def clear(a, b, t0, t1, query_radius=None):
        return not blocked_intervals(a, b, t0, t1,
                                     radius if query_radius is None else query_radius,
                                     reservations, config, blocking_actors)

    direct_end = start_time + math.dist(start, goal) / speed
    if static_clear([start, goal]):
        # A temporary obstruction often needs only a safe departure delay.
        # Test those waits before spending thousands of nodes on spatial detours.
        # Both the complete source wait and final hold remain continuously checked.
        delay = 0.
        while delay <= config.max_delay_s + EPS:
            check_deadline()
            departure, arrival = start_time + delay, direct_end + delay
            if delay and not clear(start, start, start_time, departure):
                break  # Every longer wait would contain this same collision.
            if (clear(start, goal, departure, arrival)
                    and clear(goal, goal, arrival, hold_end(arrival), hold_radius)):
                points = [(start_time, *start)]
                if delay:
                    points.append((departure, *start))
                points.append((arrival, *goal))
                return RouteResult(points, "ok")
            delay += config.departure_step_s

    cell = config.cell_size_m
    x0, x1, z0, z1 = bounds
    source, target = ("source",), ("target",)
    def point(node):
        if node == source: return start
        if node == target: return goal
        return [x0 + node[0] * cell, start[1], z0 + node[1] * cell]

    def index(p):
        return round((p[0] - x0) / cell), round((p[2] - z0) / cell)

    safe_cache, edge_cache = {}, {}
    def safe(node):
        if node not in safe_cache:
            p = point(node)
            if not static_clear([p]):
                safe_cache[node] = []
            else:
                blocked = blocked_intervals(p, p, start_time, horizon,
                                            hold_radius if node == target else radius,
                                            reservations, config, blocking_actors)
                intervals, previous = [], start_time
                for a, b in blocked:
                    if a > previous + EPS: intervals.append((previous, a - EPS))
                    previous = max(previous, b + EPS)
                if previous < horizon: intervals.append((previous, horizon))
                safe_cache[node] = intervals
        return safe_cache[node]

    initial = next((i for i, (a, b) in enumerate(safe(source)) if a <= start_time <= b), None)
    if initial is None:
        return failure("start_occupied")
    initial_state = (source, initial)
    costs, parents = {initial_state: start_time}, {}
    serial = 0
    frontier = [(start_time + math.dist(start, goal) / speed, serial, start_time, initial_state)]
    expanded = 0
    while frontier:
        check_deadline()
        _, _, queued_arrival, state = heapq.heappop(frontier)
        if queued_arrival > costs[state] + EPS:
            continue
        node, interval = state
        arrival = costs[state]
        expanded += 1
        if expanded > config.max_expansions:
            return failure("expansion_limit", expanded - 1)
        if node == target and hold_end(arrival) <= safe(node)[interval][1]:
            chain = [(arrival, *goal)]
            while state in parents:
                previous, departure = parents[state]
                chain.append((departure, *point(previous[0])))
                if departure > costs[previous] + EPS:
                    chain.append((costs[previous], *point(previous[0])))
                state = previous
            chain.append((start_time, *start))
            chain.reverse()
            return RouteResult(chain, "ok", expanded)
        p = point(node)
        ix, iz = index(p) if node in (source, target) else node
        neighbors = [(ix + dx, iz + dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1)
                     if dx or dz or node == source]
        if math.dist(p, goal) <= cell * 2: neighbors.append(target)
        for neighbor in neighbors:
            q = point(neighbor)
            if not (x0 <= q[0] <= x1 and z0 <= q[2] <= z1): continue
            edge = node, neighbor
            if edge not in edge_cache: edge_cache[edge] = static_clear([p, q])
            if not edge_cache[edge]: continue
            travel = math.dist(p, q) / speed
            if travel < EPS: continue
            for j, (low, high) in enumerate(safe(neighbor)):
                if neighbor == target and hold_until is not None and high < hold_until:
                    continue
                departure = max(arrival, low - travel)
                latest = min(safe(node)[interval][1], high - travel, latest_arrival - travel)
                while departure <= latest + EPS:
                    check_deadline()
                    next_arrival = departure + travel
                    next_state = neighbor, j
                    if next_arrival >= costs.get(next_state, math.inf) - EPS: break
                    if clear(p, q, departure, next_arrival):
                        costs[next_state] = next_arrival
                        parents[next_state] = state, departure
                        serial += 1
                        heapq.heappush(frontier, (next_arrival + math.dist(q, goal) / speed,
                                                  serial, next_arrival, next_state))
                        break
                    departure += config.departure_step_s
    return failure("horizon_exhausted", expanded)
