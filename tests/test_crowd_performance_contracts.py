"""Optimizations retain continuous checks and correct actual shot warnings."""
import math
from pathlib import Path
import random
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'shot_library'))
from camera_controller import CameraController, CameraKeyframe
from crowd.config import CrowdSolverConfig
from crowd.joint import first_conflict, priority_search
from crowd.st_planner import Reservation, blocked_intervals
from director_brief import resolve_path_for_actor


def reference_intervals(r, start, end):
    cuts = sorted({start, end} | {t for t in r.times if start < t < end} |
                  {t for a, b, _ in r.windows for t in (a, b) if start < t < end})
    for a, b in zip(cuts, cuts[1:]):
        radius = max([r.radius] + [v for low, high, v in r.windows if low <= (a + b) / 2 < high])
        yield a, b, r.position(a), r.position(b), radius


@pytest.mark.parametrize('count', [2, 4, 20, 40])
def test_indexed_intervals_and_broadphase_match_exhaustive_checks(count):
    rng = random.Random(count)
    config = CrowdSolverConfig()
    reservations = []
    for index in range(count):
        points = [(t, rng.uniform(-10, 10), rng.choice([0, 2]), rng.uniform(-10, 10))
                  for t in range(5)]
        windows = [(rng.uniform(-2, 2), rng.uniform(2, 6), rng.uniform(.1, 2)) for _ in range(4)]
        r = Reservation(str(index), points, .3, windows,
                        mover_radius=.8 if index % 3 == 0 else None)
        reservations.append(r)
        for start, end in [(-3, 8), (0, 4), (1.5, 2.5), (3, 3), (7, 8)]:
            assert list(r.intervals(start, end)) == list(reference_intervals(r, start, end))
    earliest = None
    for i, a in enumerate(reservations):
        for b in reservations[i + 1:]:
            for left, right, p, q, radius in reference_intervals(a, -3, 8):
                overlaps = blocked_intervals(p, q, left, right, radius, [b], config)
                if overlaps and (earliest is None or overlaps[0][0] < earliest[0]):
                    earliest = overlaps[0][0], a.actor, b.actor
    expected = earliest[1:] if earliest else None
    assert first_conflict(reservations, -3, 8, config) == expected
    detail = first_conflict(reservations, -3, 8, config, details=True)
    if expected is None:
        assert detail is None
    else:
        assert (detail['actor'], detail['other_actor']) == expected
        assert detail['distance_m'] < detail['required_separation_m']


def test_broadphase_retains_fast_crossing_and_held_destination():
    a = Reservation('a', [(0, -10, 0, 0), (.01, 10, 0, 0)], .3)
    b = Reservation('b', [(0, 0, 0, -10), (.01, 0, 0, 10)], .3)
    assert first_conflict([a, b], 0, 1, CrowdSolverConfig()) == ('a', 'b')
    c = Reservation('c', [(0, 20, 0, 0), (2, 10, 0, 0)], .3)
    assert first_conflict([a, c], 0, 3, CrowdSolverConfig()) == ('a', 'c')


def test_joint_search_does_not_resolve_equivalent_transitive_orders():
    signatures = []
    def solve(order):
        signatures.append(frozenset((p, a) for a, parents in order.items() for p in parents))
        return order, None
    def conflict(order):
        for a, b in [('a', 'b'), ('b', 'c')]:
            if a not in order.get(b, ()) and b not in order.get(a, ()):
                return a, b
        # Once these two relationships are known, some branches imply this
        # third relationship already. A duplicate edge must not spend a node.
        return 'a', 'c'
    priority_search(solve, conflict, 32)
    assert len(signatures) == len(set(signatures))


@pytest.mark.parametrize('depth', [10, 50, 100])
def test_camera_uses_projection_at_all_distances(depth):
    camera = [CameraKeyframe(0, [0, .9, 0], [0, 0, 0]),
              CameraKeyframe(2, [0, .9, 0], [0, 0, 0])]
    controller = CameraController()
    ok, warnings = controller.validate_frustum(camera, {'a': [(0, [0, 0, -depth])]}, ['a'])
    assert ok and not warnings
    ok, warnings = controller.validate_frustum(camera, {'a': [(0, [depth * 2, 0, -depth])]}, ['a'])
    assert not ok and all('frustum' in w for w in warnings)
    ok, warnings = controller.validate_frustum(camera, {'a': [(0, [0, 0, depth])]}, ['a'])
    assert not ok and all('behind' in w for w in warnings)


@pytest.mark.parametrize('points', [
    [[0, 0, 0], [1, 0, 0], [1, 0, 1]],
    [[0, 0, 0], [0, 0, 0], [1, 0, 0], [1, 0, 1]],
    [[0, 0, 0], [0, 0, 0]],
])
def test_sampled_travel_heading_follows_interpolated_root_at_corners(points):
    samples = resolve_path_for_actor({'points': points}, (0, 1), sample_hz=7)
    assert samples[0][1] == points[0] and samples[-1][1] == points[-1]
    for (_, a, heading), (_, b, _) in zip(samples, samples[1:]):
        dx, dz = b[0] - a[0], b[2] - a[2]
        if math.hypot(dx, dz) > 1e-9:
            expected = math.degrees(math.atan2(dx, -dz))
            assert abs((heading - expected + 180) % 360 - 180) < 1e-7


def test_geometry_edits_invalidate_previous_clearance_without_global_cache():
    from crowd.geometry import clears_solids, snapshot_solids
    boxes = [{'min': [10., 0., 10.], 'max': [11., 2., 11.]}]
    route = [[-1., 0., 0.], [1., 0., 0.]]
    snapshot = snapshot_solids(boxes)
    assert clears_solids(route, boxes, .2)
    boxes[0]['min'][:] = [-.5, 0., -.5]
    boxes[0]['max'][:] = [.5, 2., .5]
    assert not clears_solids(route, boxes, .2)
    assert clears_solids(route, snapshot, .2)  # An owned snapshot stays immutable.
    assert not clears_solids(route, snapshot_solids(boxes), .2)
    boxes[0] = {'min': [10., 0., 10.], 'max': [11., 2., 11.]}
    assert clears_solids(route, boxes, .2)
    boxes.append({'min': [-.5, 0., -.5], 'max': [.5, 2., .5]})
    assert not clears_solids(route, boxes, .2)
    boxes.pop()
    assert clears_solids(route, boxes, .2)


@pytest.mark.parametrize('stage', ['solve', 'conflict'])
def test_joint_deadline_rejects_results_completed_after_timeout(monkeypatch, stage):
    import crowd.budget as budget
    now = [0.]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: now[0])

    def solve(order):
        if stage == 'solve':
            now[0] = 2.
        return {}, None

    def conflict(solution):
        now[0] = 2.
        return None

    result, error, visited = priority_search(solve, conflict, 128, max_time_s=1.)
    assert result is None and error.error_code == 'CROWD_ROUTE_TIMEOUT'
    assert visited == 1
    budget.check_deadline()  # The budget scope must be reset after failure.


def test_joint_deadline_interrupts_inside_low_level_search(monkeypatch):
    import crowd.budget as budget
    from crowd.st_planner import plan_route
    now = [0.]
    checks = []
    monkeypatch.setattr(budget.time, 'monotonic', lambda: now[0])

    def static_clear(points):
        checks.append(points)
        now[0] += .1
        return True

    obstacle = Reservation('stationary', [(0., 0., 0., 0.)], .4)

    def solve(order):
        route = plan_route([-2., 0., 0.], [2., 0., 0.], 0., 1., .3,
                           [obstacle], static_clear, (-5., 5., -5., 5.))
        return route, None

    result, error, visited = priority_search(solve, lambda r: None, 128, max_time_s=.5)
    assert result is None and error.error_code == 'CROWD_ROUTE_TIMEOUT'
    assert visited == 1 and len(checks) < 10


def test_nested_search_budget_preserves_parent_deadline(monkeypatch):
    import crowd.budget as budget
    now = [0.]
    monkeypatch.setattr(budget.time, 'monotonic', lambda: now[0])
    with budget.search_budget(1.):
        with budget.search_budget(100.):
            now[0] = 2.
            with pytest.raises(budget.SearchTimeout):
                budget.check_deadline()
    budget.check_deadline()
