"""Bounded validation feedback for replanning, without modifying authored motion."""
import math


def clearance_deficit(report):
    """Return measured overlap depth, or None for failures spacing cannot repair.

    Missing coverage, invalid support, validator errors and authored-conformance
    failures are terminal: increasing clearance cannot certify those failures.
    """
    error = report.get('error', {})
    if error.get('error_code') == 'CROWD_CONFORMANCE':
        depths = [c['required_separation_m'] - c['distance_m']
                  for c in error.get('details', {}).get('conflicts', [])]
    elif error:
        return None
    else:
        collision = report.get('validation', {}).get('collision_report', {})
        if (collision.get('error') or collision.get('level_violations')
                or collision.get('unprotected_assets') or collision.get('unprotected_actors')):
            return None
        depths = [c['worst_depth_m'] for c in collision.get('violations', [])]
    if not depths or not all(math.isfinite(d) and d > 0 for d in depths):
        return None
    return max(depths)


def next_clearance(config, current, report):
    deficit = clearance_deficit(report)
    if deficit is None:
        return None
    # A cell-derived minimum makes progress even for tiny residual penetration.
    candidate = min(current + max(deficit, config.cell_size_m / 2),
                    config.buffer_m + config.max_repair_clearance_m)
    return candidate if candidate > current else None
