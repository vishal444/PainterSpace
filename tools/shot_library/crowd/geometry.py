"""Clearance against measured solid boxes, including furniture inside buildings."""
import math

from .budget import check_deadline

from support_surfaces import (ROUTE_SAMPLE_STEP_M, STANDING_BODY_HEIGHT_M,
                              WALK_OVER_MAX_HEIGHT_M, contains)


class SolidSnapshot:
    """Owned numeric geometry for one solve; input edits require a new snapshot."""
    def __init__(self, solids):
        solids = list(solids)
        self.owners = frozenset(str(s['owner']) for s in solids if s.get('owner'))
        self.compiled = tuple(
            (*map(float, s['min']), *map(float, s['max'])) for s in solids)
        self.bands = {}

    def __bool__(self):
        return bool(self.compiled)


def snapshot_solids(solids):
    return solids if isinstance(solids, SolidSnapshot) else SolidSnapshot(solids)


def fallback_obstacle_scene(scene):
    """Only unmeasured objects need an approximate whole-object blocker.

    The crowd router checks measured solids separately on every edge. Adding
    an enclosing disc for those same objects would close their open courtyards,
    streets and passages. Incomplete collision coverage retains the fallback.
    """
    measured = snapshot_solids(scene.get('solid_boxes', [])).owners
    uncovered = set(scene.get('unprotected_scene_objects', []))
    return dict(scene, objects=[obj for obj in scene.get('objects', [])
                               if not ({str(obj.get('name', '')), str(obj.get('id', ''))}
                                       & (measured - uncovered))])


def _active_solids(solids, y0, y1):
    snapshot = snapshot_solids(solids)
    key = (y0, y1)
    if key not in snapshot.bands:
        low_limit = y0 + WALK_OVER_MAX_HEIGHT_M
        high_limit = y1 + STANDING_BODY_HEIGHT_M
        snapshot.bands[key] = tuple(
            (lx, lz, hx, hz) for lx, ly, lz, hx, hy, hz in snapshot.compiled
            if hy > low_limit and ly < high_limit)
    return snapshot.bands[key]


def clears_solids(route, solids, radius):
    """Check swept horizontal discs against conservative world box footprints.

    Low supporting faces are walkable. Boxes extending above step height and
    into standing-body height block the route, even inside a target building.
    """
    check_deadline()
    solids = snapshot_solids(solids)
    points = list(route)
    if not points:
        return False
    if not solids:
        return True
    for a, b in zip(points, points[1:] or points):
        check_deadline()
        ax, ay, az = a[0], a[1], a[2]
        bx, by, bz = b[0], b[1], b[2]
        y0 = ay if ay <= by else by
        y1 = by if ay <= by else ay
        active = _active_solids(solids, y0, y1)
        if not active:
            continue
        dx = bx - ax
        dz = bz - az
        abs_dx = abs(dx)
        abs_dz = abs(dz)
        for lx, lz, hx, hz in active:
            left_x, right_x = lx - radius, hx + radius
            if (ax <= left_x and bx <= left_x) or (ax >= right_x and bx >= right_x):
                continue
            left_z, right_z = lz - radius, hz + radius
            if (az <= left_z and bz <= left_z) or (az >= right_z and bz >= right_z):
                continue
            start, end = 0.0, 1.0
            if abs_dx < 1e-12:
                if ax <= left_x or ax >= right_x:
                    continue
            else:
                t0 = (left_x - ax) / dx
                t1 = (right_x - ax) / dx
                enter = t0 if t0 <= t1 else t1
                leave = t1 if t0 <= t1 else t0
                if enter > start:
                    start = enter
                if leave < end:
                    end = leave
            if start >= end:
                continue
            if abs_dz < 1e-12:
                if az <= left_z or az >= right_z:
                    continue
            else:
                t0 = (left_z - az) / dz
                t1 = (right_z - az) / dz
                enter = t0 if t0 <= t1 else t1
                leave = t1 if t0 <= t1 else t0
                if enter > start:
                    start = enter
                if leave < end:
                    end = leave
            if start < end and end > 0.0 and start < 1.0:
                return False
    return True


def supported_disc(point, radius, surfaces, config):
    if not surfaces:
        return True
    if any(contains(surface, point, radius) for surface in surfaces):
        return True
    # Check the union so adjacent support faces do not introduce artificial walls.
    return all(any(contains(s, p) for s in surfaces) for p in
               [point] + [[point[0] + radius * math.cos(i * math.tau / config.arena_perimeter_samples),
                           point[1], point[2] + radius * math.sin(i * math.tau / config.arena_perimeter_samples)]
                          for i in range(config.arena_perimeter_samples)])


def supports_body_route(route, radius, surfaces, config):
    points = list(route)
    for a, b in zip(points, points[1:] or points):
        check_deadline()
        steps = max(1, math.ceil(math.dist(a, b) / ROUTE_SAMPLE_STEP_M))
        for step in range(steps + 1):
            p = [a[k] + (b[k] - a[k]) * step / steps for k in range(3)]
            if not supported_disc(p, radius, surfaces, config):
                return False
    return bool(points)
