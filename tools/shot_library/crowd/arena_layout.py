"""Deterministic bounded joint assignment of automatic meeting areas.

Arena discs include formation extent and measured limb reach. A beam search
handles mutually conflicting candidates, which ordinary bipartite matching
cannot express. Failure reports search exhaustion rather than a packing proof.
"""
import math

from .config import CrowdSolverConfig
from .budget import check_deadline


def assign_arenas(groups, candidates, occupied, supported, config=None, accept=None,
                  diagnostics=None):
    """Return ``{group_key: spot}`` or ``None``, without mutating inputs.

    Groups supply participants' positions, arena radius and preferred position.
    An optional candidate_cost(spot) ranks route-aware placement proposals;
    callers still provide support, occupancy and complete-layout checks.
    Occupied entries are (position, radius) for actors who will not approach.
    The caller decides which groups overlap in time and calls once per wave.
    ``accept`` can reject a complete layout that leaves no room for a later
    interaction; other retained layouts are tried before reporting exhaustion.
    """
    config = config or CrowdSolverConfig()
    stats = diagnostics if diagnostics is not None else {}
    stats.update(groups={}, pair_conflicts=0, beam_pruned=0, acceptance_rejected=0)
    choices = {}
    for group in groups:
        check_deadline()
        counts = dict(candidates=len(candidates[group['key']]), clearance=0,
                      support_or_solids=0, occupied=0, valid=0, candidate_pruned=0)
        stats['groups'][str(group['key'])] = counts
        radius = group['radius']
        solid_radius = group.get('solid_radius', radius)
        def cost(spot):
            # Squared travel breaks the equal-distance tie along a pair's
            # entire source-to-source line in favor of a balanced rendezvous.
            score = (group['candidate_cost'](spot) if group.get('candidate_cost') else
                     sum(math.dist(p, spot['position']) ** 2 for p in group['sources']))
            return (score,
                    math.dist(group['preferred'], spot['position']), spot['name'])
        options = []
        for spot in candidates[group['key']]:
            check_deadline()
            point = spot['position']
            if spot.get('clearance_m') is not None and spot['clearance_m'] < solid_radius + config.buffer_m:
                counts['clearance'] += 1
                continue
            if not supported(point, solid_radius):
                counts['support_or_solids'] += 1
                continue
            if any(abs(point[1] - other[1]) <= config.level_tolerance_m
                   and math.hypot(point[0] - other[0], point[2] - other[2])
                   < radius + other_radius + config.buffer_m for other, other_radius in occupied):
                counts['occupied'] += 1
                continue
            options.append(spot)
        options.sort(key=cost)
        counts['valid'] = len(options)
        counts['candidate_pruned'] = max(0, len(options) - config.layout_candidates)
        choices[group['key']] = [(spot, cost(spot)[0]) for spot in options[:config.layout_candidates]]
    if any(not options for options in choices.values()):
        return None
    # Most constrained group first; stable authored order breaks ties.
    ordered = sorted(enumerate(groups), key=lambda item: (len(choices[item[1]['key']]), item[0]))
    beam = [(0., {}, [])]
    for _, group in ordered:
        expanded = []
        for score, assigned, discs in beam:
            check_deadline()
            for spot, cost in choices[group['key']]:
                point, radius = spot['position'], group['radius']
                if any(abs(point[1] - p[1]) <= config.level_tolerance_m
                       and math.hypot(point[0] - p[0], point[2] - p[2])
                       < radius + r + config.buffer_m for p, r in discs):
                    stats['pair_conflicts'] += 1
                    continue
                expanded.append((score + cost, {**assigned, group['key']: spot}, discs + [(point, radius)]))
        expanded.sort(key=lambda entry: entry[0])
        stats['beam_pruned'] += max(0, len(expanded) - config.layout_beam_width)
        beam = expanded[:config.layout_beam_width]
        if not beam: return None
    for _, assigned, _ in beam:
        check_deadline()
        if accept is None or accept(assigned):
            return assigned
        stats['acceptance_rejected'] += 1
    return None
