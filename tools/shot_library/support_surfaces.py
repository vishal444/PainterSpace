"""Horizontal support geometry for character placement and walking.

The scene reader supplies measured flat top-face polygons from USD boxes and
meshes. Actors and routes are supported across adjacent faces at the same
level. Unknown geometry is never represented as a support polygon.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Sequence


# A character's standing capsule needs this much horizontal room from an edge.
ACTOR_SUPPORT_MARGIN_M = 0.35
# Stair endpoints can lie on the exact edge of the adjoining platform.
SUPPORT_EDGE_EPSILON_M = 0.02
SUPPORT_LEVEL_TOLERANCE_M = 0.12
# Faces whose top corners differ more than this are ramps, not flat landings.
MAX_SUPPORT_FACE_HEIGHT_VARIATION_M = 0.02
# Ground-level meetings can use the existing scene floor convention; elevated
# meetings require an explicit measured support face.
ELEVATED_SUPPORT_THRESHOLD_M = 0.30
ROUTE_SAMPLE_STEP_M = 0.25
# Standing spots form a reusable grid for crowd placement. The spacing allows
# two standing capsules to remain clear of each other.
STANDING_SPOT_SPACING_M = 1.5
STANDING_BODY_HEIGHT_M = 1.8
STANDING_MAX_STEP_M = 0.15
# A short raised curb or strip can be crossed by an ordinary walk with a
# lifted root. Taller obstacles require the existing vault/jump route contract.
WALK_OVER_MAX_HEIGHT_M = 0.20
# An ordinary actor can vault onto low furniture and vehicle tops. Higher
# levels require an explicitly planned climb, stair, or jump action.
MAX_ROUTE_VAULT_HEIGHT_M = 1.50
# Bound queue context while allowing crowd shots; callers can raise this for
# larger scenes or disable the limit when all valid locations are needed.
DEFAULT_MAX_STANDING_SPOTS_PER_SIDE = 64


def derive_surface_spots(faces: Iterable[Dict[str, Any]],
                         solids: Iterable[Dict[str, Any]],
                         floor_heights: Dict[str, float],
                         spatial_cell_m: float = STANDING_SPOT_SPACING_M) -> list[Dict[str, Any]]:
    """Advertise character-sized positions on measured elevated flat geometry.

    A spot may span adjacent coplanar faces, so triangulated surfaces are
    supported even when no single triangle contains the standing capsule.
    Faces remain available to route support checks independently of the spots.
    """
    faces = [face for face in faces
             if is_convex(face.get("polygon_xz", []))
             and str(face.get("owner", "")) in floor_heights
             and float(face["height_m"]) - floor_heights[str(face["owner"])]
             >= ELEVATED_SUPPORT_THRESHOLD_M]
    solids = list(solids)
    cell_size = spatial_cell_m
    bins: Dict[tuple[int, int], list[Dict[str, Any]]] = {}
    for face in faces:
        polygon = face["polygon_xz"]
        x0, x1 = min(p[0] for p in polygon), max(p[0] for p in polygon)
        z0, z1 = min(p[1] for p in polygon), max(p[1] for p in polygon)
        for ix in range(math.floor(x0 / cell_size), math.floor(x1 / cell_size) + 1):
            for iz in range(math.floor(z0 / cell_size), math.floor(z1 / cell_size) + 1):
                bins.setdefault((ix, iz), []).append(face)

    def nearby(x: float, z: float) -> list[Dict[str, Any]]:
        return bins.get((math.floor(x / cell_size), math.floor(z / cell_size)), [])

    def supported(x: float, y: float, z: float) -> bool:
        for angle_index in range(12):
            angle = math.tau * angle_index / 12
            px = x + ACTOR_SUPPORT_MARGIN_M * math.cos(angle)
            pz = z + ACTOR_SUPPORT_MARGIN_M * math.sin(angle)
            if not any(contains(face, [px, y, pz]) for face in nearby(px, pz)):
                return False
        return True

    result: list[Dict[str, Any]] = []
    chosen: Dict[str, list[list[float]]] = {}
    # Dense triangulated assets can put thousands of faces in one actor-sized
    # patch. Keep all faces for coverage, but try one candidate per small cell.
    candidate_cells: Dict[tuple[str, int, int, int], Dict[str, Any]] = {}
    candidate_step = ACTOR_SUPPORT_MARGIN_M
    for face in faces:
        polygon = face["polygon_xz"]
        cx = sum(point[0] for point in polygon) / len(polygon)
        cz = sum(point[1] for point in polygon) / len(polygon)
        key = (str(face["owner"]),
               round(float(face["height_m"]) / SUPPORT_LEVEL_TOLERANCE_M),
               math.floor(cx / candidate_step), math.floor(cz / candidate_step))
        previous = candidate_cells.get(key)
        if previous is None or polygon_area(polygon) > polygon_area(previous["polygon_xz"]):
            candidate_cells[key] = face
    ordered = sorted(candidate_cells.values(), key=lambda face: (-float(face["height_m"]),
                                                                -polygon_area(face["polygon_xz"]),
                                                                str(face["owner"])))
    for face in ordered:
        owner = str(face["owner"])
        y = float(face["height_m"])
        if owner not in floor_heights or y - floor_heights[owner] < ELEVATED_SUPPORT_THRESHOLD_M:
            continue
        polygon = face["polygon_xz"]
        x = sum(point[0] for point in polygon) / len(polygon)
        z = sum(point[1] for point in polygon) / len(polygon)
        if not supported(x, y, z):
            continue
        if any(abs(point[1] - y) <= SUPPORT_LEVEL_TOLERANCE_M
               and math.hypot(point[0] - x, point[2] - z) < STANDING_SPOT_SPACING_M
               for point in chosen.get(owner, [])):
            continue
        if any(box["min"][1] < y + STANDING_BODY_HEIGHT_M
               and box["max"][1] > y + STANDING_MAX_STEP_M
               and box["min"][0] - ACTOR_SUPPORT_MARGIN_M <= x <= box["max"][0] + ACTOR_SUPPORT_MARGIN_M
               and box["min"][2] - ACTOR_SUPPORT_MARGIN_M <= z <= box["max"][2] + ACTOR_SUPPORT_MARGIN_M
               for box in solids):
            continue
        if any(y + STANDING_MAX_STEP_M < float(other["height_m"]) < y + STANDING_BODY_HEIGHT_M
               and contains(other, [x, other["height_m"], z])
               for other in nearby(x, z)):
            continue
        point = [x, y, z]
        chosen.setdefault(owner, []).append(point)
        result.append({"owner": owner, "name": "", "position": point,
                       "surface": face})
    counts: Dict[str, int] = {}
    for spot in result:
        owner = spot["owner"]
        counts[owner] = counts.get(owner, 0) + 1
        spot["name"] = f"surface_{counts[owner]:03d}"
    return result


def polygon_area(polygon: Sequence[Sequence[float]]) -> float:
    polygon = list(polygon)
    return abs(sum(a[0] * b[1] - b[0] * a[1]
                   for a, b in zip(polygon, polygon[1:] + polygon[:1]))) * 0.5


def is_convex(polygon: Sequence[Sequence[float]]) -> bool:
    """Require one inside half-plane per edge for standing support."""
    polygon = list(polygon)
    if len(polygon) < 3:
        return False
    turns = []
    for a, b, c in zip(polygon, polygon[1:] + polygon[:1],
                       polygon[2:] + polygon[:2]):
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-8:
            turns.append(cross > 0)
    return bool(turns) and all(turn == turns[0] for turn in turns)


_SURFACE_CACHE: Dict[int, tuple] = {}


def _compiled_surface(polygon: Sequence[Sequence[float]]) -> tuple | None:
    cached = _SURFACE_CACHE.get(id(polygon))
    if cached is not None and cached[0] is polygon:
        return cached[1]
    if len(polygon) < 3:
        return None
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    winding = 1.0 if sum(a[0] * b[1] - b[0] * a[1]
                         for a, b in zip(pts, pts[1:] + pts[:1])) >= 0 else -1.0
    edges = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        dx, dz = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dz)
        if length <= 1e-9:
            return None
        scale = winding / length
        edges.append((a[0], a[1], dx * scale, dz * scale))
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]
    compiled = (min(xs), max(xs), min(zs), max(zs), tuple(edges))
    if len(_SURFACE_CACHE) > 4096:
        _SURFACE_CACHE.clear()
    _SURFACE_CACHE[id(polygon)] = (polygon, compiled)
    return compiled


def contains(surface: Dict[str, Any], point: Sequence[float], margin: float = 0.0) -> bool:
    """Check a point and its clearance circle against a convex support face."""
    polygon = surface.get("polygon_xz", [])
    if len(polygon) < 3 or abs(float(point[1]) - float(surface["height_m"])) > SUPPORT_LEVEL_TOLERANCE_M:
        return False
    compiled = _compiled_surface(polygon)
    if compiled is None:
        return False
    x0, x1, z0, z1, edges = compiled
    px, pz = float(point[0]), float(point[2])
    pad = SUPPORT_EDGE_EPSILON_M - margin + 1e-6
    if px < x0 - pad or px > x1 + pad or pz < z0 - pad or pz > z1 + pad:
        return False
    for ax, az, nx, nz in edges:
        signed_distance = nx * (pz - az) - nz * (px - ax)
        if signed_distance + SUPPORT_EDGE_EPSILON_M < margin:
            return False
    return True


def supports_disc(surfaces: Iterable[Dict[str, Any]], point: Sequence[float],
                  radius: float = ACTOR_SUPPORT_MARGIN_M) -> bool:
    """Check capsule-footprint support across adjacent coplanar faces."""
    faces = list(surfaces)
    if any(contains(face, point, radius) for face in faces):
        return True
    if not any(contains(face, point) for face in faces):
        return False
    for index in range(12):
        angle = math.tau * index / 12
        sample = [float(point[0]) + radius * math.cos(angle),
                  float(point[1]),
                  float(point[2]) + radius * math.sin(angle)]
        if not any(contains(face, sample) for face in faces):
            return False
    return True


def supports_route(surfaces: Dict[str, Any] | Iterable[Dict[str, Any]],
                   route: Iterable[Sequence[float]]) -> bool:
    """A route may cross adjacent support faces at the same level."""
    surfaces = [surfaces] if isinstance(surfaces, dict) else list(surfaces)
    points = list(route)
    if not points:
        return False
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(math.hypot(b[0] - a[0], b[2] - a[2]) / ROUTE_SAMPLE_STEP_M))
        for index in range(steps + 1):
            t = index / steps
            point = [a[axis] + (b[axis] - a[axis]) * t for axis in range(3)]
            if not any(contains(surface, point) for surface in surfaces):
                return False
    return any(contains(surface, points[0]) for surface in surfaces)


def project_drawn_route(points: Sequence[Sequence[float]],
                        surfaces: Iterable[Dict[str, Any]],
                        starting_height_m: float,
                        solids: Iterable[Dict[str, Any]] = (),
                        sample_step_m: float = ROUTE_SAMPLE_STEP_M
                        ) -> tuple[list[list[float]], list[tuple[int, int]]]:
    """Lift a drawn ground-plane route onto measured, reachable support tops.

    Returns dense 3-D points and index intervals for vault animations. The
    authored X/Z trace is preserved; only its height is resolved. A transition
    needs room on both sides so the character rises before reaching an edge.
    """
    points = [list(map(float, point)) for point in points]
    if len(points) < 2:
        return points, []
    # An explicitly authored 3-D route keeps its authored heights.
    if (max(point[1] for point in points) - min(point[1] for point in points) > SUPPORT_LEVEL_TOLERANCE_M
            or any(abs(point[1] - starting_height_m) > SUPPORT_LEVEL_TOLERANCE_M for point in points)):
        return points, []
    x0 = min(point[0] for point in points) - ACTOR_SUPPORT_MARGIN_M
    x1 = max(point[0] for point in points) + ACTOR_SUPPORT_MARGIN_M
    z0 = min(point[2] for point in points) - ACTOR_SUPPORT_MARGIN_M
    z1 = max(point[2] for point in points) + ACTOR_SUPPORT_MARGIN_M
    faces = [face for face in surfaces
             if face.get("polygon_xz")
             and min(vertex[0] for vertex in face["polygon_xz"]) <= x1
             and max(vertex[0] for vertex in face["polygon_xz"]) >= x0
             and min(vertex[1] for vertex in face["polygon_xz"]) <= z1
             and max(vertex[1] for vertex in face["polygon_xz"]) >= z0]
    solids = list(solids)
    baseline = float(starting_height_m)
    if not faces:
        return points, []
    dense: list[list[float]] = [points[0]]
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(math.hypot(b[0] - a[0], b[2] - a[2]) / sample_step_m))
        for index in range(1, steps + 1):
            u = index / steps
            dense.append([a[0] + (b[0] - a[0]) * u,
                          a[1] + (b[1] - a[1]) * u,
                          a[2] + (b[2] - a[2]) * u])
    walk_over_boxes = [box for box in solids
                       if baseline < float(box['max'][1]) <= baseline + WALK_OVER_MAX_HEIGHT_M
                       and float(box['min'][1]) < baseline + STANDING_BODY_HEIGHT_M]
    # Cache face bounds once. A detailed mesh may have thousands of distinct
    # triangle heights; only geometry overlapping this root's support disc can
    # contribute a candidate height or support it. This keeps projection local
    # without dropping any face that the original disc test could use.
    bounded_faces = [(face, float(face['height_m']),
                      min(v[0] for v in face['polygon_xz']), max(v[0] for v in face['polygon_xz']),
                      min(v[1] for v in face['polygon_xz']), max(v[1] for v in face['polygon_xz']))
                     for face in faces]
    heights: list[float] = []
    for point in dense:
        px, pz = point[0], point[2]
        nearby_faces = [(face, height) for face, height, lx, hx, lz, hz in bounded_faces
                        if lx <= px + ACTOR_SUPPORT_MARGIN_M and hx >= px - ACTOR_SUPPORT_MARGIN_M
                        and lz <= pz + ACTOR_SUPPORT_MARGIN_M and hz >= pz - ACTOR_SUPPORT_MARGIN_M]
        candidates = sorted({height for face, height in nearby_faces
                             if baseline + STANDING_MAX_STEP_M < height <= baseline + MAX_ROUTE_VAULT_HEIGHT_M
                             and abs(height - float(point[1])) <= MAX_ROUTE_VAULT_HEIGHT_M}, reverse=True)
        nearby_solids = [box for box in solids
                         if box['min'][0] - ACTOR_SUPPORT_MARGIN_M <= px <= box['max'][0] + ACTOR_SUPPORT_MARGIN_M
                         and box['min'][2] - ACTOR_SUPPORT_MARGIN_M <= pz <= box['max'][2] + ACTOR_SUPPORT_MARGIN_M]
        height = baseline
        for candidate in candidates:
            level_faces = [face for face, face_height in nearby_faces
                           if abs(face_height - candidate) <= SUPPORT_LEVEL_TOLERANCE_M]
            blocked = any(box['min'][1] < candidate + STANDING_BODY_HEIGHT_M
                          and box['max'][1] > candidate + STANDING_MAX_STEP_M
                          for box in nearby_solids)
            if not blocked and supports_disc(level_faces, [px, candidate, pz]):
                height = candidate
                break
        # Narrow low strips cannot support a whole standing disc, but a walker
        # can lift a foot across them. Raise the root through their footprint
        # without treating their tops as a place to stand.
        for box in walk_over_boxes:
            if (box['min'][0] - ACTOR_SUPPORT_MARGIN_M <= point[0] <= box['max'][0] + ACTOR_SUPPORT_MARGIN_M
                    and box['min'][2] - ACTOR_SUPPORT_MARGIN_M <= point[2] <= box['max'][2] + ACTOR_SUPPORT_MARGIN_M):
                height = max(height, float(box['max'][1]))
        heights.append(height)
    heights[0] = baseline
    if all(abs(height - baseline) <= STANDING_MAX_STEP_M for height in heights):
        return points, []
    for point, height in zip(dense, heights):
        point[1] = height
    boundaries = [index for index in range(1, len(dense))
                  if abs(heights[index] - heights[index - 1]) > STANDING_MAX_STEP_M]
    spans: list[tuple[int, int]] = []
    last_end = -1
    for boundary in boundaries:
        start = max(last_end + 1, boundary - 2)
        end = min(len(dense) - 1, boundary + 2)
        if end <= start:
            continue
        y0, y1 = heights[start], heights[end]
        small_step = abs(y1 - y0) <= WALK_OVER_MAX_HEIGHT_M
        # Small steps lift the walking root onto the support. Higher changes
        # retain the vault arc and jump clip used for authored routes.
        apex = max(y0, y1) + (SUPPORT_EDGE_EPSILON_M if small_step else ACTOR_SUPPORT_MARGIN_M)
        lift = max(0.0 if small_step else 0.1, apex - (y0 + y1) / 2)
        for index in range(start + 1, end):
            u = (index - start) / (end - start)
            dense[index][1] = y0 + (y1 - y0) * u + 4 * lift * u * (1 - u)
        if not small_step:
            spans.append((start, end))
        last_end = end
    return dense, spans


def derive_standing_spots(surfaces: Iterable[Dict[str, Any]],
                          solids: Iterable[Dict[str, Any]],
                          headings: Dict[str, float] | None = None,
                          max_per_side: int | None = DEFAULT_MAX_STANDING_SPOTS_PER_SIDE) -> list[Dict[str, Any]]:
    """Find clear, connected standing locations on each object's main floor.

    The broad lowest top face establishes the floor level. Smaller overlapping
    faces up to one step above it can supply the actual standing height. Solid
    boxes above the feet exclude walls and furniture. Only the largest connected
    set of grid cells is advertised so actors can route between advertised spots.
    """
    by_owner: Dict[str, list[Dict[str, Any]]] = {}
    for surface in surfaces:
        by_owner.setdefault(str(surface.get("owner", "")), []).append(surface)
    solids = list(solids)
    headings = headings or {}
    result: list[Dict[str, Any]] = []
    for owner, faces in by_owner.items():
        # Accumulate coplanar face area so tessellation does not make a broad
        # tiled floor look smaller than one large roof or decorative ledge.
        levels = []
        for face in sorted(faces, key=lambda item: float(item['height_m'])):
            area = polygon_area(face['polygon_xz'])
            if not levels or float(face['height_m']) - levels[-1][0] > SUPPORT_LEVEL_TOLERANCE_M:
                levels.append([float(face['height_m']), area])
            else:
                levels[-1][1] += area
        if not levels:
            continue
        broad_area = max(area for _, area in levels)
        base_height = min(height for height, area in levels
                          if area >= broad_area * 0.5)
        floors = [face for face in faces
                  if base_height - SUPPORT_LEVEL_TOLERANCE_M <= float(face["height_m"])
                  <= base_height + STANDING_MAX_STEP_M]
        if not floors:
            continue
        # Cover the union extent, not just the largest triangle or tile.
        x_values = [point[0] for face in floors for point in face['polygon_xz']]
        z_values = [point[1] for face in floors for point in face['polygon_xz']]
        center_x = (min(x_values) + max(x_values)) * 0.5
        center_z = (min(z_values) + max(z_values)) * 0.5
        cells: Dict[tuple[int, int], list[float]] = {}
        nx = math.ceil((max(x_values) - min(x_values)) / STANDING_SPOT_SPACING_M)
        nz = math.ceil((max(z_values) - min(z_values)) / STANDING_SPOT_SPACING_M)
        # Other scene objects can occupy this floor too (for example a club
        # standing on a broad ground platform).
        owner_solids = solids

        def clear_at(point: Sequence[float]) -> bool:
            x, y, z = point
            return not any(
                solid["min"][1] < y + STANDING_BODY_HEIGHT_M
                and solid["max"][1] > y + STANDING_MAX_STEP_M
                and solid["min"][0] - ACTOR_SUPPORT_MARGIN_M <= x <= solid["max"][0] + ACTOR_SUPPORT_MARGIN_M
                and solid["min"][2] - ACTOR_SUPPORT_MARGIN_M <= z <= solid["max"][2] + ACTOR_SUPPORT_MARGIN_M
                for solid in owner_solids)

        def solid_clearance(point: Sequence[float]) -> float | None:
            """Horizontal room to the nearest solid overlapping standing body height."""
            x, y, z = point
            distances = [math.hypot(max(solid["min"][0] - x, 0.0, x - solid["max"][0]),
                                    max(solid["min"][2] - z, 0.0, z - solid["max"][2]))
                         for solid in owner_solids
                         if solid["min"][1] < y + STANDING_BODY_HEIGHT_M
                         and solid["max"][1] > y + STANDING_MAX_STEP_M]
            return min(distances) if distances else None

        def route_clear(start: Sequence[float], end: Sequence[float]) -> bool:
            steps = max(1, math.ceil(math.hypot(end[0] - start[0], end[2] - start[2])
                                     / ROUTE_SAMPLE_STEP_M))
            for index in range(steps + 1):
                t = index / steps
                point = [start[axis] + (end[axis] - start[axis]) * t for axis in range(3)]
                covered = [face for face in floors
                           if abs(float(face['height_m']) - point[1]) <= STANDING_MAX_STEP_M
                           and contains(face, [point[0], float(face['height_m']), point[2]])]
                if not clear_at(point) or not any(
                    supports_disc(floors, [point[0], float(face['height_m']), point[2]])
                    for face in covered):
                    return False
            return True

        for ix in range(nx):
            x = min(x_values) + (ix + 0.5) * STANDING_SPOT_SPACING_M
            for iz in range(nz):
                z = min(z_values) + (iz + 0.5) * STANDING_SPOT_SPACING_M
                covered = [face for face in floors if contains(
                    face, [x, float(face["height_m"]), z])]
                if not covered:
                    continue
                y = max(float(face["height_m"]) for face in covered)
                if clear_at([x, y, z]) and supports_disc(floors, [x, y, z]):
                    cells[(ix, iz)] = [x, y, z]
        # Keep the largest four-connected region. This excludes isolated ledges
        # and spots cut off by full-height obstacles.
        remaining = set(cells)
        regions = []
        while remaining:
            start = min(remaining)
            region = {start}
            queue = [start]
            remaining.remove(start)
            for cell in queue:
                for neighbor in ((cell[0] - 1, cell[1]), (cell[0] + 1, cell[1]),
                                 (cell[0], cell[1] - 1), (cell[0], cell[1] + 1)):
                    if (neighbor in remaining
                            and abs(cells[cell][1] - cells[neighbor][1]) <= STANDING_MAX_STEP_M
                            and route_clear(cells[cell], cells[neighbor])):
                        remaining.remove(neighbor)
                        region.add(neighbor)
                        queue.append(neighbor)
            regions.append(region)
        if not regions:
            continue
        region = max(regions, key=lambda item: (len(item), -min(item)[0], -min(item)[1]))
        sides = {"left": [], "right": []}
        angle = math.radians(headings.get(owner, 0.0))
        for cell in region:
            point = cells[cell]
            local_x = (point[0] - center_x) * math.cos(angle) + (point[2] - center_z) * math.sin(angle)
            side = "left" if local_x < 0 else "right"
            sides[side].append(point)
        for side, points in sides.items():
            # Prefer the center of the room while distributing spots along the
            # chosen side; references remain deterministic for the same scene.
            points.sort(key=lambda p: (abs(p[2] - center_z), abs(p[0] - center_x), p[2], p[0]))
            selected = points[:max_per_side] if max_per_side is not None else points
            for index, point in enumerate(selected, 1):
                result.append({"owner": owner, "name": f"standing_{side}_{index:02d}",
                               "position": point, "clearance_m": solid_clearance(point)})
    return result
