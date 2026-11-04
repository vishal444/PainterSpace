"""Bounded conflict-directed priority search over whole timed trajectories.

Unsolved agents' guessed paths are not immutable obstacles. A conflict branches
on either precedence, and the low-level planner replans under that partial order.
This is priority-based search, not optimal CBS or a completeness guarantee.
"""
import heapq
import itertools
import logging
import math
from dataclasses import replace
from .budget import check_deadline, search_budget, SearchTimeout
from plan_model import StructuredError

from .st_planner import Reservation, blocked_intervals

logger = logging.getLogger(__name__)


class PrefixReservation(Reservation):
    """Protect an agent's authored prefix until its adjustable departure."""
    def __init__(self, reservation, until):
        super().__init__(reservation.actor, reservation.points, reservation.radius,
                         reservation.windows, reservation.mover_radius)
        self.until = until
        self._t_max = until

    def intervals(self, start, end):
        if start < self.until:
            yield from super().intervals(start, min(end, self.until))


def ancestors(edges):
    """Transitive higher-priority agents, or None for a cyclic order."""
    result = {}
    for high, low in edges:
        result.setdefault(low, set()).add(high)
        result.setdefault(high, set())
    for _ in range(len(result)):
        changed = False
        for agent, parents in result.items():
            expanded = parents | set().union(*(result.get(p, set()) for p in parents))
            if agent in expanded:
                return None
            if expanded != parents:
                result[agent] = expanded
                changed = True
        if not changed:
            break
    return result


def priority_search(solve, conflict, max_nodes, failure_conflicts=None, max_time_s=None,
                    failure_signature=None):
    with search_budget(max_time_s):
        return _priority_search(solve, conflict, max_nodes, failure_conflicts, max_time_s, failure_signature)


def _priority_search(solve, conflict, max_nodes, failure_conflicts, max_time_s, failure_signature):
    """Return (solution, last_error, visited); branch on measured conflicts.

    Each solve receives a transitive partial order. Failed constrained nodes are
    de-prioritized behind feasible siblings so a dead precedence branch never
    starves its sibling orderings. A low-level route failure may supply blocking
    pairs through the optional ``failure_conflicts(error)`` callback. This lets
    the root node negotiate precedence even when it could not produce a complete
    provisional solution.
    """
    serial = itertools.count()
    frontier = [(0, 0, next(serial), frozenset())]
    seen = {frozenset()}
    visited, last_error = 0, None
    failed_queries = set()
    while frontier and visited < max_nodes:
        try:
            check_deadline()
        except SearchTimeout:
            return None, _timeout_error(max_time_s), visited
        _, _, _, edges = heapq.heappop(frontier)
        order = ancestors(edges)
        if order is None:
            continue
        visited += 1
        logger.info('Crowd joint node %s/%s: %s precedence constraints',
                    visited, max_nodes, len(edges))
        try:
            solution, error = solve(order)
            check_deadline()
            pair = conflict(solution) if error is None else None
            check_deadline()
        except SearchTimeout:
            return None, _timeout_error(max_time_s), visited
        repeated_failure = False
        if error is not None:
            key = failure_signature(error) if failure_signature else None
            if key is not None:
                repeated_failure = key in failed_queries
                failed_queries.add(key)
            last_error = error
            pairs = list(failure_conflicts(error)) if failure_conflicts else []
            failed_constrained = bool(edges)
        else:
            if pair is None:
                return solution, None, visited
            pairs = [pair]
            failed_constrained = False
        for a, b in pairs:
            logger.info('Crowd trajectory conflict: %s / %s', a, b)
            for edge in ((a, b), (b, a)):
                enqueue_order(frontier, seen, edges, edge, serial,
                              deprioritize=failed_constrained, repeated_failure=repeated_failure)
    return None, last_error, visited


def _timeout_error(max_time_s):
    return StructuredError('CROWD_ROUTE_TIMEOUT',
        'Crowd coordination exceeded its wall-clock allowance.',
        details={'max_joint_time_s': max_time_s})


def enqueue_order(frontier, seen, edges, edge, serial, deprioritize=False, repeated_failure=False):
    """Queue a distinct acyclic partial order, sharing one bounded frontier."""
    if edge[0] == edge[1]:
        return
    child = edges | {edge}
    closure = ancestors(child)
    if closure is None:
        return
    # Different edge lists can encode the same transitive order.
    signature = frozenset((parent, agent) for agent, parents in closure.items()
                          for parent in parents)
    if signature not in seen:
        seen.add(signature)
        # Follow a consistent feasible ordering to completion before its siblings;
        # failed constrained branches go to fallback tier 1 so siblings run first.
        # Identical low-level inputs have already failed; explore new queries
        # first, but retain combinations of constraints that may change them.
        # A matching failure is not a proof that its descendants are unsolvable.
        tier = 2 if repeated_failure else (1 if deprioritize else 0)
        depth_key = len(child) if deprioritize else -len(child)
        heapq.heappush(frontier, (tier, depth_key, next(serial), child))


def first_conflict(reservations, start, end, config, exemptions=(), body_only=(), details=False):
    """Earliest continuously checked conflict, including waits and end holds.

    Exemptions are (actor set, start, end) intentional interaction contracts.
    Both agents' time-varying radii are included in the analytic edge check.
    ``body_only`` scopes base-body clearance to approaching interaction partners.
    An optional fourth value permits intentional contact without extra buffer;
    it never exempts their bodies from collision checks. ``details`` returns a
    measured interior collision rather than just the two actor IDs.
    """
    contact_config = replace(config, buffer_m=0.0)
    first = None
    collision = None
    bounds = {id(r): tuple((min(p[k] for p in r.points), max(p[k] for p in r.points))
                           for k in (1, 2, 3)) for r in reservations}
    radii = {id(r): max([r.radius] + [radius for _, _, radius in r.windows])
             for r in reservations}
    for i, actor in enumerate(reservations):
        check_deadline()
        for other in reservations[i + 1:]:
            # A conservative broad phase rejects only spatially disjoint swept
            # bounds. Crossing edges, radius changes and final holds still use
            # the same continuous narrow phase below.
            a_bounds, b_bounds = bounds[id(actor)], bounds[id(other)]
            clearance = (radii[id(actor)] if other.mover_radius is None else other.mover_radius)
            clearance += radii[id(other)] + config.buffer_m
            if any(a_bounds[k][1] + margin < b_bounds[k][0] or
                   b_bounds[k][1] + margin < a_bounds[k][0]
                   for k, margin in ((0, clearance), (1, config.level_tolerance_m), (2, clearance))):
                continue
            cuts = {start, end}
            shared = [(a, b) for members, a, b in exemptions
                      if actor.actor in members and other.actor in members]
            approaching = [(entry[1], entry[2], bool(entry[3]) if len(entry) > 3 else False)
                           for entry in body_only
                           if actor.actor in entry[0] and other.actor in entry[0]]
            cuts.update(t for a, b in shared for t in (a, b) if start < t < end)
            cuts.update(t for a, b, _ in approaching for t in (a, b) if start < t < end)
            times = sorted(cuts)
            for left, right in zip(times, times[1:]):
                if any(a <= (left + right) / 2 < b for a, b in shared):
                    continue
                active_approaches = [contact for a, b, contact in approaching
                                     if a <= (left + right) / 2 < b]
                reduced = bool(active_approaches)
                interval_config = contact_config if any(active_approaches) else config
                first_actor = Reservation(actor.actor, actor.points, actor.radius) if reduced else actor
                second_actor = Reservation(other.actor, other.points, other.radius) if reduced else other
                for a, b, p, q, radius in first_actor.intervals(left, right):
                    overlaps = blocked_intervals(p, q, a, b, radius, [second_actor], interval_config)
                    if overlaps and (first is None or overlaps[0][0] < first[0]):
                        first = overlaps[0][0], actor.actor, other.actor
                        if details:
                            lo, hi = overlaps[0]
                            # At entry the discs may only touch. Measure inside
                            # the interval so repair receives a positive deficit.
                            when = (lo + hi) / 2
                            own_radius = next(r for _, _, _, _, r in first_actor.intervals(when, hi))
                            other_radius = next(r for _, _, _, _, r in second_actor.intervals(when, hi))
                            if second_actor.mover_radius is not None:
                                own_radius = second_actor.mover_radius
                            needed = own_radius + other_radius + interval_config.buffer_m
                            distance_at_entry = math.dist(first_actor.position(lo)[::2],
                                                          second_actor.position(lo)[::2])
                            if distance_at_entry + 1e-6 < needed:
                                when = lo
                            pa, pb = first_actor.position(when), second_actor.position(when)
                            collision = {'actor': actor.actor, 'other_actor': other.actor,
                                         'time_s': when, 'distance_m': math.hypot(pa[0]-pb[0], pa[2]-pb[2]),
                                         'required_separation_m': needed}
    if first is None:
        return None
    return collision if details else first[1:]
