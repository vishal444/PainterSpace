"""Resolve measured region IDs to body-sized, supported semantic targets."""
import math

from crowd.budget import SearchTimeout, search_budget
from crowd.config import CrowdSolverConfig
from crowd.geometry import clears_solids, snapshot_solids
from route_placement import PlacementRoutes
from spatial_regions import derive_spatial_regions, region_contains, sample_region, source_seed
from support_surfaces import ACTOR_SUPPORT_MARGIN_M


class SpatialReferenceResolver:
    """Geometry and caches are owned by one scene-resolution snapshot."""
    def __init__(self, manifest, scene, config=None):
        self.config = config or CrowdSolverConfig()
        self.scene = scene
        self.assets = {a['id']: a for a in manifest.get('assets', [])}
        self.regions = {r['id']: r for r in derive_spatial_regions(scene, manifest, self.config)}
        self.solids = snapshot_solids(scene.get('solid_boxes', []))
        self.routes = PlacementRoutes(scene, self.config)
        self.cache = {}

    def body_radius(self, state, override=None):
        asset = self.assets.get(state.get('asset_id'), {})
        bounds = asset.get('bounds_m') or [0., 0., 0.]
        vehicle = state.get('is_vehicle') or 'vehicle' in asset.get('tags', []) or 'car' in asset.get('tags', [])
        radius = (math.hypot(bounds[0], bounds[2]) / 2 if vehicle else
                  max(bounds[0], bounds[2]) / 2)
        appearance = state.get('appearance') or {}
        scale = (appearance.get('height_scale', 1.) if isinstance(appearance, dict)
                 else getattr(appearance, 'height_scale', 1.))
        collision = ((asset.get('collision') or {}).get('character') or {}).get('radius_m', 0.)
        return max(ACTOR_SUPPORT_MARGIN_M, radius * float(scale), float(collision) * float(scale), override or 0.)

    def directional_regions(self, reference, state):
        from semantic_references import direction_heading
        heading = direction_heading(reference)
        if heading is None:
            return []
        normal = [round(math.sin(math.radians(heading))), -round(math.cos(math.radians(heading)))]
        result = []
        for floor in list(self.regions.values()):
            if floor['kind'] != 'floor':
                continue
            if state.get('position') is not None and abs(state['position'][1] - floor['height_m']) > self.config.level_tolerance_m:
                continue
            bounds = list(floor['bounds_xz'])
            for axis, sign in enumerate(normal):
                if sign:
                    span = bounds[axis + 2] - bounds[axis]
                    bounds[axis + (2 if sign < 0 else 0)] = (
                        bounds[axis] + span * self.config.entry_region_fraction if sign < 0
                        else bounds[axis + 2] - span * self.config.entry_region_fraction)
            region_id = floor['id'] + ':direction_' + str(int(heading))
            region = dict(floor, id=region_id, kind='entry', support_region=floor['id'],
                          selection_bounds_xz=bounds, entry_normal=normal,
                          preferred=[(bounds[0]+bounds[2])/2, floor['height_m'], (bounds[1]+bounds[3])/2])
            self.regions[region_id] = region
            result.append(region)
        return result

    def resolve_direction(self, reference, state, clearance_radius=None):
        regions = self.directional_regions(reference, state)
        source = state.get('position')
        if source is not None:
            regions.sort(key=lambda r: math.dist(source, r['preferred']))
        for region in regions:
            resolved, _ = self.resolve(region['id'], state, clearance_radius)
            if resolved is not None:
                point, radius = resolved
                return (point, radius, region['id']), ''
        return None, f'No clear supported {reference} region for this mover within the configured search limits.'

    def resolve(self, reference, state, clearance_radius=None):
        region = self.regions[reference]
        if region['kind'] not in ('floor', 'entry', 'corner'):
            return None, 'Obstacle-side approaches require a staged spatial approach contract.'
        radius = self.body_radius(state, clearance_radius)
        source = tuple(state['position']) if state.get('position') is not None else None
        config = self.config
        asset = self.assets.get(state.get('asset_id'), {})
        vehicle = bool(state.get('is_vehicle') or set(asset.get('tags', [])) & {'vehicle', 'car'})
        bounds = asset.get('bounds_m')
        height = float(bounds[1]) if bounds else math.inf
        key = (reference, radius, source, vehicle, height)
        if key in self.cache:
            return self.cache[key]
        vehicle_routes = None
        if vehicle:
            from vehicle_routes import VehicleRoutes
            vehicle_routes = VehicleRoutes(self.scene, config)
        def clear(point):
            local_corner = (region['kind'] != 'corner' or math.dist(point, preferred) <= config.cell_size_m)
            body_clear = (vehicle_routes.clear_vehicle(tuple(point), tuple(point), radius, height)
                          if vehicle_routes is not None else
                          clears_solids([point], self.solids, radius + config.buffer_m))
            return (local_corner and body_clear and region_contains(region, point, radius, config))
        preferred = source_seed(region, source or region['preferred'], radius + config.buffer_m)
        candidates = [preferred, region['preferred']]
        try:
            with search_budget(config.max_joint_time_s):
                for level in range(config.standing_search_levels):
                    valid = [p for p in candidates if p is not None and clear(p)]
                    # A clear projected entry point avoids scanning an entire
                    # landscape for ordinary road-to-boundary destinations.
                    if valid and (source is None or self.routes.clear(source, tuple(valid[0]), radius)):
                        result = ((list(valid[0]), radius), '')
                        self.cache[key] = result
                        return result
                    candidates.extend(spot['position'] for spot in sample_region(region, self.scene, config, level))
                    valid = [p for p in candidates if p is not None and clear(p)]
                    if valid:
                        valid.sort(key=lambda p: self.routes.cost(source, [p], radius)
                                   if source is not None else math.dist(p, preferred))
                        # Endpoint resolution promises supported clear space;
                        # full timed routing and steering remain downstream.
                        result = ((list(valid[0]), radius), '')
                        self.cache[key] = result
                        return result
        except SearchTimeout:
            return None, f'Region resolution reached its geometry search time limit for {reference}.'
        return None, (f'No clear supported point in region {reference} for a mover radius of {radius:.3f} m '
                      f'within {config.standing_samples} samples. The region was not widened.')
