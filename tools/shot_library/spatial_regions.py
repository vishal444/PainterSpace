"""Scene-owned supported floor regions and obstacle-side approach semantics.

IDs fingerprint measured geometry; a stale or invented ID cannot silently bind
to a different place. Approach regions mean free floor near an owner's measured
obstacle footprints, not an interior, doorway, or a specific component building.
"""
import hashlib
import json
import math

from crowd.config import CrowdSolverConfig
from crowd.geometry import supported_disc
from support_surfaces import contains, polygon_area, STANDING_BODY_HEIGHT_M, WALK_OVER_MAX_HEIGHT_M

# Numerical tolerance only, in world metres.
REGION_EPSILON_M = 1e-6


def destination_support_id(region):
    """Arrival areas are whole floors or boundary bands on those floors.

    Keep the selected band as the destination; resolving its support must not
    widen the arrival area to the entire floor.
    """
    if not region:
        return None
    if region['kind'] == 'floor':
        return region['id']
    if region['kind'] == 'entry':
        return region.get('support_region')
    return None


def valid_destination(region, regions):
    support = regions.get(destination_support_id(region))
    return bool(support and support['kind'] == 'floor')


def intent_source_ids(intent, regions, variant=0):
    """Resolve a declared distribution without deriving intent from actor names."""
    if intent.origin_layout == 'specified':
        ids = intent.source_regions or [intent.source_region] * len(intent.actors)
        if len(ids) != len(intent.actors):
            raise ValueError('Provide one source region per actor')
        return dict(zip(intent.actors, ids))
    if intent.source_regions or intent.origin_layout not in ('opposite', 'surrounding'):
        raise ValueError('Unsupported origin layout or conflicting explicit sources')
    destination = regions[intent.destination_region]
    support_id = destination_support_id(destination)
    entries = {tuple(r['entry_normal']): r['id'] for r in regions.values()
               if r['kind'] == 'entry' and r.get('support_region') == support_id}
    bounds = destination['bounds_xz']
    axis = int(bounds[3] - bounds[1] > bounds[2] - bounds[0])
    if variant % 2:
        axis = 1 - axis
    normals = [(-1, 0), (1, 0)] if axis == 0 else [(0, -1), (0, 1)]
    if intent.origin_layout == 'surrounding':
        normals += [(0, -1), (0, 1)] if axis == 0 else [(-1, 0), (1, 0)]
    if len(intent.actors) < len(normals):
        raise ValueError('Not enough actors to populate the declared entry sides')
    return {actor: entries[normals[index * len(normals) // len(intent.actors)]]
            for index, actor in enumerate(intent.actors)}


def origin_layout_valid(intent, sources, starts, goals, destination=None):
    if intent.origin_layout == 'specified':
        return True
    support_id = destination_support_id(destination) if destination else intent.destination_region
    if any(region.get('support_region') != support_id for region in sources.values()):
        return False
    normals = {tuple(r.get('entry_normal', [])) for r in sources.values()}
    if any(len(n) != 2 for n in normals):
        return False
    if intent.origin_layout == 'opposite':
        if len(normals) != 2 or any(tuple(-v for v in n) not in normals for n in normals):
            return False
    elif intent.origin_layout == 'surrounding':
        if len(normals) != 4:
            return False
    else:
        return False
    center = [sum(p[i] for p in goals.values()) / len(goals) for i in (0, 2)]
    return all(sum((starts[actor][axis] - center[i]) * region['entry_normal'][i]
                   for i, axis in enumerate((0, 2))) > REGION_EPSILON_M
               for actor, region in sources.items())


def visits_in_order(points, waypoints, tolerance):
    """Swept-segment waypoint checks, retaining progress for repeated visits."""
    segment, fraction = 0, 0.
    for waypoint in waypoints:
        found = False
        while segment + 1 < len(points):
            a, b = points[segment], points[segment + 1]
            delta = [y - x for x, y in zip(a, b)]
            length2 = sum(v * v for v in delta)
            t = max(fraction, min(1., sum((w - x) * v for w, x, v in zip(waypoint, a, delta)) / length2)) if length2 else fraction
            if math.dist([x + t * v for x, v in zip(a, delta)], waypoint) <= tolerance:
                fraction, found = t, True
                break
            segment, fraction = segment + 1, 0.
        if not found:
            return False
    return True


def _id(owner, kind, geometry):
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()[:16]
    return f'{owner}:{kind}_{digest}'


def derive_spatial_regions(scene, manifest=None, config=None):
    config = config or CrowdSolverConfig()
    uncovered = set(scene.get('unprotected_scene_objects', []))
    objects = {str(obj.get('name') or obj.get('id')): obj for obj in scene.get('objects', [])}
    assets = {asset.get('id'): asset for asset in (manifest or {}).get('assets', [])}
    grouped = {}
    for face in scene.get('support_surfaces', []):
        if polygon_area(face.get('polygon_xz', [])) <= 0:
            continue
        owner = str(face.get('owner', ''))
        if not owner or owner in uncovered:
            continue
        grouped.setdefault(owner, []).append(face)
    floors = []
    for owner, faces in sorted(grouped.items()):
        levels = []
        for face in sorted(faces, key=lambda f: (float(f['height_m']), f['polygon_xz'])):
            if not levels or abs(face['height_m'] - levels[-1][0]['height_m']) > config.level_tolerance_m:
                levels.append([])
            levels[-1].append(face)
        standing = [spot['position'][1] for spot in scene.get('standing_spots', [])
                    if str(spot.get('owner')) == owner]
        for level in levels:
            height = float(level[0]['height_m'])
            if standing and not any(abs(h - height) <= config.level_tolerance_m for h in standing):
                continue
            x = [p[0] for face in level for p in face['polygon_xz']]
            z = [p[1] for face in level for p in face['polygon_xz']]
            geometry = {'height_m': height, 'faces': level}
            floors.append({'id': _id(owner, 'floor', geometry), 'kind': 'floor', 'owner': owner,
                           'height_m': height, 'faces': level,
                           'bounds_xz': [min(x), min(z), max(x), max(z)],
                           'preferred': [(min(x) + max(x)) / 2, height, (min(z) + max(z)) / 2],
                           'description': 'Measured supported floor; gathering placement prefers its interior center.'})
    result = list(floors)
    # Entries are world-space boundary bands intersected with actual support,
    # never a promise of clearance or a doorway. Opposing normals are explicit.
    for floor in floors:
        x0, z0, x1, z1 = floor['bounds_xz']
        for axis, names in ((0, ('west', 'east')), (1, ('north', 'south'))):
            for sign, name in zip((-1, 1), names):
                bounds = [x0, z0, x1, z1]
                span = bounds[axis + 2] - bounds[axis]
                bounds[axis + (2 if sign < 0 else 0)] = (
                    bounds[axis] + span * config.entry_region_fraction if sign < 0
                    else bounds[axis + 2] - span * config.entry_region_fraction)
                geometry = {'floor': floor['id'], 'selection_bounds_xz': bounds}
                result.append({**floor, 'id': _id(floor['owner'], 'entry', geometry), 'kind': 'entry',
                               'support_region': floor['id'], 'selection_bounds_xz': bounds,
                               'entry_normal': [sign if axis == 0 else 0, sign if axis == 1 else 0],
                               'preferred': [(bounds[0] + bounds[2]) / 2, floor['height_m'],
                                             (bounds[1] + bounds[3]) / 2],
                               'description': f'{name} boundary entry on measured floor (world axes). No doorway is implied.'})
    for owner, obj in sorted(objects.items()):
        if owner in uncovered:
            continue
        for floor in floors:
            height = floor['height_m']
            boxes = [box for box in scene.get('solid_boxes', [])
                     if box.get('owner') == owner
                     and box['max'][1] - box['min'][1] > WALK_OVER_MAX_HEIGHT_M
                     and box['min'][1] < height + STANDING_BODY_HEIGHT_M
                     and box['max'][1] > height + WALK_OVER_MAX_HEIGHT_M]
            if not boxes:
                continue
            geometry = {'floor': floor['id'], 'source_boxes': boxes}
            approach_id = _id(owner, 'approach', geometry)
            tags = assets.get(obj.get('asset') or obj.get('asset_id'), {}).get('tags', [])
            result.append({**floor, 'id': approach_id, 'kind': 'approach',
                           'source_owner': owner, 'source_tags': list(tags), 'source_boxes': boxes,
                           'support_region': floor['id'],
                           'description': f'Free supported approach space near measured obstacles owned by {owner}; '
                                          'compiler chooses source-side positions relative to the destination. '
                                          'This does not identify doors or interiors.'})
            # Each measured obstacle footprint exposes exterior corner
            # quadrants. Ordered via areas express route topology separately
            # from origins; full static/dynamic clearance is still required.
            seen_corners = set()
            for box in boxes:
                for sx in (-1, 1):
                    for sz in (-1, 1):
                        x = box['min' if sx < 0 else 'max'][0]
                        z = box['min' if sz < 0 else 'max'][2]
                        key = (x, z, sx, sz)
                        probe = [x + sx * REGION_EPSILON_M, z + sz * REGION_EPSILON_M]
                        if key in seen_corners or any(
                                other['min'][0] < probe[0] < other['max'][0]
                                and other['min'][2] < probe[1] < other['max'][2]
                                for other in boxes):
                            continue
                        seen_corners.add(key)
                        bounds = [floor['bounds_xz'][0] if sx < 0 else x,
                                  floor['bounds_xz'][1] if sz < 0 else z,
                                  x if sx < 0 else floor['bounds_xz'][2],
                                  z if sz < 0 else floor['bounds_xz'][3]]
                        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                            continue
                        geometry = {'approach': approach_id, 'corner': [x, z], 'normal': [sx, sz]}
                        result.append({**floor, 'id': _id(owner, 'corner', geometry), 'kind': 'corner',
                                       'source_owner': owner, 'support_region': floor['id'],
                                       'selection_bounds_xz': bounds, 'corner': [x, height, z],
                                       'corner_normal': [sx, sz], 'preferred': [x, height, z],
                                       'description': f'Exterior corner quadrant of measured {owner} footprint '
                                                      f'({"west" if sx < 0 else "east"}, '
                                                      f'{"north" if sz < 0 else "south"}). '
                                                      'Use ordered via_regions to require a route around it.'})
    return result


def region_descriptors(regions):
    """Compact inventory for an LLM; geometry stays owned by the compiler."""
    keys = ('id', 'kind', 'owner', 'source_owner', 'source_tags', 'support_region',
            'height_m', 'bounds_xz', 'selection_bounds_xz', 'entry_normal', 'description')
    result = []
    for region in regions:
        if region['kind'] == 'corner':
            # Avoid repeating an entire floor's bounds for every footprint
            # corner. Position and quadrant make compound-owner corners usable.
            selected = ('id', 'kind', 'source_owner', 'support_region', 'height_m', 'corner', 'corner_normal')
        else:
            selected = keys
        result.append({key: region[key] for key in selected if key in region})
    return result


def region_contains(region, point, radius, config):
    bounds = region.get('selection_bounds_xz')
    if bounds and not (bounds[0] + radius <= point[0] <= bounds[2] - radius
                       and bounds[1] + radius <= point[2] <= bounds[3] - radius):
        return False
    # Root lifting over a measured curb/paint strip remains on this floor's
    # horizontal region. Full route and posed support checks still establish
    # the actual standing height; this does not authorize another floor.
    delta = point[1] - region['height_m']
    if delta < -config.level_tolerance_m or delta > max(WALK_OVER_MAX_HEIGHT_M, config.level_tolerance_m) + REGION_EPSILON_M:
        return False
    projected = [point[0], region['height_m'], point[2]]
    return supported_disc(projected, radius, region['faces'], config)


def nearest_source_point(region, point):
    if region['kind'] != 'approach':
        if region.get('entry_normal'):
            normal = region['entry_normal']
            return [region['preferred'][0] if normal[0] else point[0], region['height_m'],
                    region['preferred'][2] if normal[1] else point[2]]
        return list(region['preferred'])
    candidates = []
    for box in region.get('source_boxes', []):
        candidates.append([min(box['max'][0], max(box['min'][0], point[0])), region['height_m'],
                           min(box['max'][2], max(box['min'][2], point[2]))])
    return min(candidates, key=lambda p: math.hypot(p[0] - point[0], p[2] - point[2])) if candidates else None


def source_direction_ok(region, source, goal):
    if region['kind'] != 'approach':
        normal = region.get('entry_normal')
        return (math.dist(source, goal) > REGION_EPSILON_M if normal is None else
                sum((source[a] - goal[a]) * normal[i] for i, a in enumerate((0, 2))) > REGION_EPSILON_M)
    nearest = nearest_source_point(region, source)
    toward = nearest_source_point(region, goal)
    if nearest is None or toward is None:
        return False
    source_distance = math.hypot(source[0] - nearest[0], source[2] - nearest[2])
    goal_distance = math.hypot(goal[0] - toward[0], goal[2] - toward[2])
    # Numerical tolerance only; no example-specific distance or bearing rule.
    epsilon = REGION_EPSILON_M
    return (source_distance + epsilon < goal_distance and
            (source[0] - goal[0]) * (toward[0] - goal[0]) +
            (source[2] - goal[2]) * (toward[2] - goal[2]) > epsilon)


def source_seed(region, goal, clearance):
    """Nearest boundary of a clearance-expanded measured source footprint."""
    if region['kind'] != 'approach':
        if region.get('corner_normal'):
            return [region['corner'][0] + region['corner_normal'][0] * clearance,
                    region['height_m'], region['corner'][2] + region['corner_normal'][1] * clearance]
        point = nearest_source_point(region, goal)
        bounds = region.get('selection_bounds_xz', region['bounds_xz'])
        return [min(bounds[2] - clearance, max(bounds[0] + clearance, point[0])), region['height_m'],
                min(bounds[3] - clearance, max(bounds[1] + clearance, point[2]))]
    points = []
    for box in region.get('source_boxes', []):
        pad = clearance + REGION_EPSILON_M
        points.append([min(box['max'][0] + pad, max(box['min'][0] - pad, goal[0])),
                       region['height_m'],
                       min(box['max'][2] + pad, max(box['min'][2] - pad, goal[2]))])
    return min(points, key=lambda p: math.dist(goal, p)) if points else None


def sample_region(region, scene, config, level, variant=0):
    """Coarse-to-fine grids share standing_samples across all search levels."""
    from crowd.budget import check_deadline
    faces = region['faces']
    weights = [4 ** i for i in range(config.standing_search_levels)]
    before = sum(weights[:level])
    budget = (config.standing_samples * (before + weights[level]) // sum(weights)
              - config.standing_samples * before // sum(weights))
    areas = [polygon_area(face['polygon_xz']) for face in faces]
    total, consumed = sum(areas), 0.
    result = []
    def radical_inverse(index, base):
        value, weight = 0., 1. / base
        while index:
            value += (index % base) * weight
            index //= base
            weight /= base
        return value
    # Halton cell offsets give deterministic alternative layouts on retries;
    # prime bases decorrelate the two horizontal axes. Default uses centers.
    ox = radical_inverse(variant + 1, 2) if variant else .5
    oz = radical_inverse(variant + 1, 3) if variant else .5
    for index, (face, area) in enumerate(zip(faces, areas)):
        check_deadline()
        quota = int(budget * (consumed + area) / total) - int(budget * consumed / total)
        consumed += area
        if quota < 1:
            continue
        polygon = face['polygon_xz']
        x0, x1 = min(p[0] for p in polygon), max(p[0] for p in polygon)
        z0, z1 = min(p[1] for p in polygon), max(p[1] for p in polygon)
        selection = region.get('selection_bounds_xz')
        if selection:
            x0, z0 = max(x0, selection[0]), max(z0, selection[1])
            x1, z1 = min(x1, selection[2]), min(z1, selection[3])
        width, depth = x1 - x0, z1 - z0
        if width <= 0 or depth <= 0:
            continue
        nx = min(quota, max(1, int(math.sqrt(quota * width / depth))), max(1, math.ceil(width / config.cell_size_m)))
        nz = min(max(1, quota // nx), max(1, math.ceil(depth / config.cell_size_m)))
        for ix in range(nx):
            check_deadline()
            for iz in range(nz):
                point = [x0 + (ix + ox) * width / nx, float(face['height_m']), z0 + (iz + oz) * depth / nz]
                if contains(face, point):
                    result.append({'name': f'grid_{level}_{index}_{ix}_{iz}', 'position': point})
    if level == 0:
        result.extend({'name': 'marker_' + str(i), 'position': list(spot['position'])}
                      for i, spot in enumerate(scene.get('standing_spots', []))
                      if region_contains(region, spot['position'], 0., config))
        if region_contains(region, region['preferred'], 0., config):
            result.append({'name': 'preferred', 'position': list(region['preferred'])})
    return result
