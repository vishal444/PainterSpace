"""Bounded floor sampling for initial placements whose advertised markers fail."""
import math

from .budget import check_deadline
from support_surfaces import contains, polygon_area


def floor_candidates(scene, owner, side, original, config, level):
    """Sample the owning floor, preserving its local left/right and height.

    Cell centers cover the entire measured extent rather than only the vicinity
    of an advertised marker. Progressively finer grids share one raw sample
    budget per actor. Callers still check full animated support and clearance.
    No measured owner floor means no generated candidates.
    """
    faces = [face for face in scene.get('support_surfaces', [])
             if str(face.get('owner', '')) == owner
             and abs(float(face['height_m']) - original[1]) <= config.level_tolerance_m
             and polygon_area(face.get('polygon_xz', [])) > 0]
    if not faces:
        return []
    footprint = [point for face in faces for point in face['polygon_xz']]
    cx = (min(p[0] for p in footprint) + max(p[0] for p in footprint)) / 2
    cz = (min(p[1] for p in footprint) + max(p[1] for p in footprint)) / 2
    heading = next((float(obj.get('heading_deg', 0)) for obj in scene.get('objects', [])
                    if obj.get('name') == owner), 0.)
    angle = math.radians(heading)
    # Four times as many samples halves grid pitch in each refinement level.
    weights = [4 ** index for index in range(config.standing_search_levels)]
    cumulative = sum(weights[:level])
    budget = (config.standing_samples * (cumulative + weights[level]) // sum(weights)
              - config.standing_samples * cumulative // sum(weights))
    areas = [polygon_area(face['polygon_xz']) for face in faces]
    total_area = sum(areas)
    consumed_area = 0.
    result = []
    for index, (face, area) in enumerate(zip(faces, areas)):
        check_deadline()
        quota = (int(budget * (consumed_area + area) / total_area)
                 - int(budget * consumed_area / total_area))
        consumed_area += area
        if quota < 1:
            continue
        polygon = face['polygon_xz']
        x0, x1 = min(p[0] for p in polygon), max(p[0] for p in polygon)
        z0, z1 = min(p[1] for p in polygon), max(p[1] for p in polygon)
        width, depth = x1 - x0, z1 - z0
        nx = min(quota, max(1, int(math.sqrt(quota * width / depth))),
                 max(1, math.ceil(width / config.cell_size_m)))
        nz = min(max(1, quota // nx), max(1, math.ceil(depth / config.cell_size_m)))
        for ix in range(nx):
            check_deadline()
            x = x0 + (ix + .5) * width / nx
            for iz in range(nz):
                z = z0 + (iz + .5) * depth / nz
                local_x = (x - cx) * math.cos(angle) + (z - cz) * math.sin(angle)
                if ('left' if local_x < 0 else 'right') != side:
                    continue
                point = [x, float(face['height_m']), z]
                if not contains(face, point):
                    continue
                result.append({'owner': owner, 'generated': True,
                               'name': f'floor_{level}_{index}_{ix}_{iz}',
                               'position': point})
    return result
