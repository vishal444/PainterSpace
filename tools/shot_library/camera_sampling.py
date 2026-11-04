"""Deterministic camera timing, orientation and baked-skeleton target sampling."""
from __future__ import annotations
import bisect
import math
from pxr import Gf, Usd, UsdGeom, UsdSkel
from camera_rig import ArcLengthSpline, evaluate_ease


def quaternion_xyz(degrees):
    return (Gf.Rotation(Gf.Vec3d(1, 0, 0), degrees[0]) *
            Gf.Rotation(Gf.Vec3d(0, 1, 0), degrees[1]) *
            Gf.Rotation(Gf.Vec3d(0, 0, 1), degrees[2])).GetQuat()


def interpolate_keys(keys, t, field, default, quaternion=False):
    ordered = sorted(keys, key=lambda k: k['t'])
    if not ordered:
        return quaternion_xyz(default) if quaternion else default
    if t <= ordered[0]['t']:
        value = ordered[0][field]
        return quaternion_xyz(value) if quaternion else value
    for a, b in zip(ordered, ordered[1:]):
        if a['t'] <= t < b['t']:
            u = evaluate_ease((t-a['t'])/(b['t']-a['t']), b.get('ease', 'constant'))
            if quaternion:
                qa, qb = quaternion_xyz(a[field]), quaternion_xyz(b[field])
                if Gf.Dot(qa, qb) < 0: qb = -qb
                return Gf.Slerp(u, qa, qb)
            return a[field]+(b[field]-a[field])*u
    value = ordered[-1][field]
    return quaternion_xyz(value) if quaternion else value


class CameraPathSampler:
    """Centripetal spline pacing with per-waypoint keys and dwell, or speed keys."""
    def __init__(self, path, duration):
        self.path = path
        self.points = path['waypoints']
        self.spline = ArcLengthSpline([p['p'] for p in self.points])
        self.start, self.end = path.get('time_range', [0, duration])
        if 'speed_mps' in path:
            if path['speed_mps'] <= 0: raise ValueError('Camera speed must be positive')
            self.end = self.start+self.spline.total_length/path['speed_mps']+sum(p.get('dwell_s', 0) for p in self.points)
        if self.end <= self.start: raise ValueError('Camera time range must be positive')
        self.distances = [0.]
        for i in range(1, len(self.points)):
            self.distances.append(self.spline.lut_distances[min(i*self.spline.samples_per_segment, len(self.spline.lut_distances)-1)])
        dwell = sum(p.get('dwell_s', 0) for p in self.points)
        if dwell > self.end-self.start: raise ValueError('Camera dwell exceeds its time range')
        travel = self.end-self.start-dwell
        self.arrivals = []
        elapsed = self.start
        for i, point in enumerate(self.points):
            if i:
                elapsed += travel*(self.distances[i]-self.distances[i-1])/max(1e-9, self.spline.total_length)
            arrival = float(point.get('t', elapsed))
            if self.arrivals and arrival < self.arrivals[-1]+self.points[i-1].get('dwell_s', 0):
                raise ValueError('Camera waypoint times overlap a dwell')
            self.arrivals.append(arrival)
            elapsed = arrival+point.get('dwell_s', 0)
        self.has_waypoint_timing = any('t' in p or p.get('dwell_s', 0) for p in self.points)

    def sample(self, t):
        speed = self.path.get('speed', {})
        if self.has_waypoint_timing:
            distance = 0.
            for i in range(len(self.points)):
                depart = self.arrivals[i]+self.points[i].get('dwell_s', 0)
                distance = self.distances[i]
                if t <= depart: break
                if i+1 < len(self.points) and t < self.arrivals[i+1]:
                    u = (t-depart)/max(1e-9, self.arrivals[i+1]-depart)
                    ease = self.points[i+1].get('ease', speed.get('profile', 'constant'))
                    distance += evaluate_ease(u, ease)*(self.distances[i+1]-distance)
                    break
        else:
            u = max(0., min(1., (t-self.start)/(self.end-self.start)))
            if speed.get('keys'):
                u = interpolate_keys(speed['keys'], u, 'distance', u)
            else:
                u = evaluate_ease(u, speed.get('profile', 'constant'))
            distance = self.spline.total_length*u
        return self.spline.sample_at_distance(distance)


class StageTargets:
    def __init__(self, stage):
        self.stage = stage
        self.actors = {str(p.GetCustomDataByKey('actor_id')): p for p in stage.Traverse() if p.GetCustomDataByKey('actor_id')}
        self.cache = UsdSkel.Cache()
        for p in stage.Traverse():
            if p.IsA(UsdSkel.Root): self.cache.Populate(UsdSkel.Root(p), Usd.PrimDefaultPredicate)
        self.skeletons = {}
        for aid, p in self.actors.items():
            for child in Usd.PrimRange(p):
                if child.IsA(UsdSkel.Skeleton):
                    query = self.cache.GetSkelQuery(UsdSkel.Skeleton(child))
                    if query:
                        self.skeletons[aid] = (child, query, [str(j).rsplit('/', 1)[-1].rsplit(':', 1)[-1] for j in query.GetJointOrder()])
                    break

    def prim(self, actor):
        return self.actors.get(actor) or self.stage.GetPrimAtPath(actor if actor.startswith('/') else '/World/'+actor)

    def sample(self, target, tc):
        actor = target['actor']
        prim = self.prim(actor)
        if not prim: raise ValueError('Missing focus target '+actor)
        joint = target.get('joint', 'Chest' if actor in self.skeletons else '')
        position = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(tc).ExtractTranslation()
        if joint:
            aliases = {'Chest': ['Spine2', 'Chest', 'Spine1'], 'Hands': ['LeftHand', 'RightHand']}
            names = aliases.get(joint, [joint])
            entry = self.skeletons.get(actor)
            if not entry: raise ValueError('Missing focus skeleton '+actor)
            skel, query, order = entry
            indices = [order.index(name) for name in names if name in order]
            if joint == 'Chest': indices = indices[:1]
            if not indices: raise ValueError('Missing focus joint '+actor+':'+joint)
            transforms = query.ComputeJointSkelTransforms(tc)
            world = UsdGeom.Xformable(skel).ComputeLocalToWorldTransform(tc)
            position = sum(((transforms[i]*world).ExtractTranslation() for i in indices), Gf.Vec3d(0))/len(indices)
        return position+Gf.Vec3d(*target.get('offset', [0, 0, 0]))
