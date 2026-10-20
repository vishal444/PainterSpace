"""Semantic Reference Resolution for PainterSpace Shot Function Library.

Resolves semantic targets (Actors, Anchors, Parts, Zones, Zone markers, Surfaces,
Relative points, Directions, and Named levels) into concrete 3D positions, facings,
and keep-out clearances.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np


# Named levels tables
GAP_BETWEEN_BODIES = {
    "contact": 0.05,
    "adjacent": 0.70,
    "conversational": 1.30,
    "near": 2.00,
    "far": 4.00,
}

GAP_TO_VEHICLE = {
    "contact": 0.05,
    "adjacent": 0.40,
    "near": 1.80,
    "far": 4.50,
}

PACE_LEVELS = {
    "slow": "sneak",
    "normal": "walk",
    "fast": "run",
}

DURATION_LEVELS = {
    "brief": 2.0,
    "short": 4.0,
    "long": 8.0,
}

BRAKING_LEVELS = {
    "gentle": 2.0,
    "firm": 4.5,
    "emergency": 7.5,
}

BUILD_LEVELS = {
    "short": 0.94,
    "average": 1.00,
    "tall": 1.06,
}


@dataclass
class ResolvedTarget:
    position: np.ndarray        # shape (3,), in meters [x, y, z]
    facing_deg: float           # heading in degrees (0 = north/-Z, 90 = east/+X)
    clearance_radius: float     # keep-out margin around target in meters
    target_type: str            # "actor", "anchor", "zone", "marker", "surface", "relative"
    target_name: str
    target_part: Optional[str] = None
    target_asset: Optional[str] = None
    height_m: Optional[float] = None
    area_m2: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position": [round(float(v), 3) for v in self.position],
            "facing_deg": round(float(self.facing_deg), 1),
            "clearance_radius": round(float(self.clearance_radius), 3),
            "target_type": self.target_type,
            "target_name": self.target_name,
            "target_part": self.target_part,
            "height_m": self.height_m,
            "area_m2": self.area_m2,
        }


def heading_from_vector(dx: float, dz: float) -> float:
    """World-space XZ heading: 0 = north (-Z), 90 = east (+X), clockwise from above."""
    if math.hypot(dx, dz) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(dx, -dz)) % 360.0


def vector_from_heading(deg: float) -> Tuple[float, float]:
    """Return unit vector (dx, dz) for a given heading in degrees."""
    rad = math.radians(deg)
    return math.sin(rad), -math.cos(rad)


class ReferenceResolver:
    def __init__(self, manifest: Dict[str, Any], scene_data: Optional[Dict[str, Any]] = None) -> None:
        self.manifest = manifest
        self.scene_data = scene_data or {}
        self._assets_by_id = {a.get("id"): a for a in manifest.get("assets", [])}
        for a in manifest.get("assets", []):
            aid = str(a.get("id", ""))
            if "." in aid:
                self._assets_by_id.setdefault(aid.split(".")[-1], a)
            for alias in a.get("aliases", []):
                self._assets_by_id[alias] = a

    def resolve_gap(self, gap_name_or_val: Union[str, float], is_vehicle: bool = False) -> float:
        """Resolve named gap level to meters."""
        try:
            return float(gap_name_or_val)
        except (ValueError, TypeError):
            pass
        table = GAP_TO_VEHICLE if is_vehicle else GAP_BETWEEN_BODIES
        return table.get(str(gap_name_or_val).lower(), 1.30 if not is_vehicle else 1.80)

    def resolve_duration(self, dur_name_or_val: Union[str, float]) -> float:
        """Resolve named duration level to seconds."""
        try:
            return float(dur_name_or_val)
        except (ValueError, TypeError):
            pass
        return DURATION_LEVELS.get(str(dur_name_or_val).lower(), 3.0)

    def resolve_braking(self, brake_name_or_val: Union[str, float]) -> float:
        """Resolve named braking level to deceleration in m/s^2."""
        try:
            return float(brake_name_or_val)
        except (ValueError, TypeError):
            pass
        return BRAKING_LEVELS.get(str(brake_name_or_val).lower(), 4.5)

    def resolve(
        self,
        ref: str,
        actor_states: Dict[str, Dict[str, Any]],
        current_time_s: float = 0.0,
        source_actor_id: Optional[str] = None,
    ) -> Tuple[Optional[ResolvedTarget], str]:
        """Resolve any semantic reference string at current_time_s.

        Returns (ResolvedTarget, error_message).
        """
        if not ref or not isinstance(ref, str):
            return None, "Empty reference string"

        ref = ref.strip()

        # 0. Check coordinate: point(x, y, z) or pos(x, y, z)
        coord_match = re.match(r"^(?:point|pos|coord)\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)(?:\s*,\s*(-?[\d.]+))?\s*\)$", ref, re.IGNORECASE)
        if coord_match:
            px, py, pz = float(coord_match.group(1)), float(coord_match.group(2)), float(coord_match.group(3))
            facing = float(coord_match.group(4)) if coord_match.group(4) is not None else 0.0
            return ResolvedTarget(
                position=np.array([px, py, pz], float),
                facing_deg=facing,
                clearance_radius=0.35,
                target_type="point",
                target_name=ref,
            ), ""

        # 0b. Check cardinal directions: north, south, east, west, etc.
        cardinal_map = {
            "north": (0.0, 0.0, -10.0),
            "northeast": (45.0, 7.07, -7.07),
            "north_east": (45.0, 7.07, -7.07),
            "east": (90.0, 10.0, 0.0),
            "southeast": (135.0, 7.07, 7.07),
            "south_east": (135.0, 7.07, 7.07),
            "south": (180.0, 0.0, 10.0),
            "southwest": (225.0, -7.07, 7.07),
            "south_west": (225.0, -7.07, 7.07),
            "west": (270.0, -10.0, 0.0),
            "northwest": (315.0, -7.07, -7.07),
            "north_west": (315.0, -7.07, -7.07),
        }
        if ref.lower() in cardinal_map:
            deg, ox, oz = cardinal_map[ref.lower()]
            src_pos = np.array(actor_states.get(source_actor_id or "", {}).get("position", [0.0, 0.0, 0.0]), float)
            return ResolvedTarget(
                position=src_pos + np.array([ox, 0.0, oz], float),
                facing_deg=deg,
                clearance_radius=0.35,
                target_type="direction",
                target_name=ref,
            ), ""

        # 0c. Check relative actor selectors: nearest_to(target[, filter]), left_of(target[, gap]), right_of(target[, gap])
        sel_match = re.match(r"^(nearest_to|left_of|right_of)\((.+)\)$", ref, re.IGNORECASE)
        if sel_match:
            sel_type = sel_match.group(1).lower()
            sel_args = [a.strip() for a in sel_match.group(2).split(",") if a.strip()]
            inner_ref = sel_args[0] if sel_args else ""
            extra_arg = sel_args[1].lower() if len(sel_args) > 1 else ""
            base_res, err = self.resolve(inner_ref, actor_states, current_time_s)
            if not base_res:
                return None, f"Could not resolve selector base '{inner_ref}': {err}"
            if sel_type == "nearest_to":
                best_aid = None
                best_d = 1e18
                for aid, astate in actor_states.items():
                    if aid in (inner_ref, source_actor_id) or str(astate.get("label", "")).lower() == inner_ref.lower():
                        continue
                    if extra_arg and extra_arg not in aid.lower() and extra_arg not in str(astate.get("asset_id", "")).lower() and extra_arg not in str(astate.get("label", "")).lower():
                        continue
                    apos = np.array(astate.get("position", [0.0, 0.0, 0.0]), float)
                    d = float(np.linalg.norm(apos[[0, 2]] - base_res.position[[0, 2]]))
                    if d < best_d:
                        best_d = d
                        best_aid = aid
                if best_aid:
                    return self.resolve(best_aid, actor_states, current_time_s)
                return base_res, ""
            else:
                side = "left" if sel_type == "left_of" else "right"
                gap_arg = extra_arg if extra_arg else "1.3"
                return self.resolve(f"beside({inner_ref}, {side}, {gap_arg})", actor_states, current_time_s, source_actor_id)

        # 1. Check if relative point: in_front_of(target, gap), behind(target, gap), beside(target, side, gap)
        rel_match = re.match(r"^(in_front_of|behind|beside)\((.+)\)$", ref, re.IGNORECASE)
        if rel_match:
            rel_type = rel_match.group(1).lower()
            inner_args = [a.strip() for a in rel_match.group(2).split(",")]
            target_ref = inner_args[0]
            target_res, err = self.resolve(target_ref, actor_states, current_time_s)
            if not target_res:
                return None, f"Could not resolve relative target '{target_ref}': {err}"

            gap = 1.3
            side = "right"
            if len(inner_args) > 1:
                arg1_low = inner_args[1].lower()
                if arg1_low in ("left", "right", "front", "back"):
                    side = arg1_low
                    if len(inner_args) > 2:
                        gap = self.resolve_gap(inner_args[2])
                else:
                    gap = self.resolve_gap(inner_args[1])

            dx, dz = vector_from_heading(target_res.facing_deg)
            tpos = target_res.position.copy()

            if rel_type == "in_front_of" or (rel_type == "beside" and side == "front"):
                pos = tpos + np.array([dx * gap, 0.0, dz * gap])
                facing = (target_res.facing_deg + 180.0) % 360.0
            elif rel_type == "behind" or (rel_type == "beside" and side == "back"):
                pos = tpos - np.array([dx * gap, 0.0, dz * gap])
                facing = target_res.facing_deg
            elif rel_type == "beside":
                # Perpendicular: right is (dz, -dx), left is (-dz, dx)
                rx, rz = (dz, -dx) if side == "right" else (-dz, dx)
                pos = tpos + np.array([rx * gap, 0.0, rz * gap])
                facing = target_res.facing_deg
            else:
                pos = tpos
                facing = target_res.facing_deg

            return ResolvedTarget(
                position=pos,
                facing_deg=facing,
                clearance_radius=0.35,
                target_type="relative",
                target_name=ref,
                target_asset=target_res.target_asset or target_res.target_name,
            ), ""

        # 2. Check direction helper: toward(actor), away_from(actor)
        dir_match = re.match(r"^(toward|away_from)\((.+)\)$", ref, re.IGNORECASE)
        if dir_match:
            mode = dir_match.group(1).lower()
            target_ref = dir_match.group(2).strip()
            target_res, err = self.resolve(target_ref, actor_states, current_time_s)
            if not target_res:
                return None, f"Could not resolve target for direction '{target_ref}': {err}"
            if not source_actor_id or source_actor_id not in actor_states:
                return None, f"Source actor required to resolve '{ref}'"
            src_pos = np.array(actor_states[source_actor_id].get("position", [0, 0, 0]), float)
            vec = target_res.position - src_pos
            vec[1] = 0.0
            heading = heading_from_vector(vec[0], vec[2])
            if mode == "away_from":
                heading = (heading + 180.0) % 360.0
            return ResolvedTarget(
                position=target_res.position,
                facing_deg=heading,
                clearance_radius=0.35,
                target_type="direction",
                target_name=ref,
            ), ""

        # 3. Check for colon-separated references:
        #    e.g. car_1:door_fl_stand (anchor), car_1:roof (surface), car_1:door_fl (part)
        if ":" in ref:
            obj_ref, sub_ref = [s.strip() for s in ref.split(":", 1)]
            obj_res, err = self.resolve(obj_ref, actor_states, current_time_s)
            if not obj_res:
                return None, f"Base object '{obj_ref}' in '{ref}' not found: {err}"

            # Look up asset metadata
            asset_id = actor_states.get(obj_ref, {}).get("asset_id") or getattr(obj_res, "target_asset", None) or obj_res.target_name
            asset_meta = self._assets_by_id.get(asset_id, {})

            # 3a. Check anchors (with standard vehicle anchor fallback)
            default_vehicle_anchors = {
                "driver_seat": {"pos": [-0.36, 0.35, -0.15], "facing_deg": 0.0},
                "passenger_seat": {"pos": [0.36, 0.35, -0.15], "facing_deg": 0.0},
                "seat_rear_left": {"pos": [-0.36, 0.35, 0.75], "facing_deg": 0.0},
                "seat_rear_right": {"pos": [0.36, 0.35, 0.75], "facing_deg": 0.0},
                "hood_strike": {"pos": [0.0, 0.0, -1.9], "facing_deg": 0.0},
                "door_fl_strike": {"pos": [-1.2, 0.0, -0.2], "facing_deg": 90.0},
                "door_fl_stand": {"pos": [-1.35, 0.0, 0.15], "facing_deg": 90.0},
                "door_fr_stand": {"pos": [1.35, 0.0, 0.15], "facing_deg": 270.0},
                "door_rl_stand": {"pos": [-1.35, 0.0, 0.85], "facing_deg": 90.0},
                "door_rr_stand": {"pos": [1.35, 0.0, 0.85], "facing_deg": 270.0},
            }
            anchors = dict(default_vehicle_anchors)
            anchors.update(asset_meta.get("anchors", {}))
            if sub_ref in anchors:
                adata = anchors[sub_ref]
                rel_pos = np.array(adata.get("pos", [0, 0, 0]), float)
                # Rotate rel_pos by obj_res.facing_deg
                theta = math.radians(obj_res.facing_deg)
                # Heading theta where 0 = north (-Z): rotation matrix in XZ
                rot_x = rel_pos[0] * math.cos(theta) - rel_pos[2] * math.sin(theta)
                rot_z = rel_pos[0] * math.sin(theta) + rel_pos[2] * math.cos(theta)
                world_pos = obj_res.position + np.array([rot_x, rel_pos[1], rot_z])
                facing = (obj_res.facing_deg + adata.get("facing_deg", 0.0)) % 360.0
                return ResolvedTarget(
                    position=world_pos,
                    facing_deg=facing,
                    clearance_radius=0.45,
                    target_type="anchor",
                    target_name=ref,
                    target_part=adata.get("part"),
                ), ""

            # 3b. Check surfaces
            surfaces = asset_meta.get("surfaces", {})
            if sub_ref in surfaces:
                sdata = surfaces[sub_ref]
                rel_ctr = np.array(sdata.get("center", [0, sdata.get("height_m", 0.0), 0]), float)
                theta = math.radians(obj_res.facing_deg)
                rot_x = rel_ctr[0] * math.cos(theta) - rel_ctr[2] * math.sin(theta)
                rot_z = rel_ctr[0] * math.sin(theta) + rel_ctr[2] * math.cos(theta)
                world_pos = obj_res.position + np.array([rot_x, rel_ctr[1], rot_z])
                return ResolvedTarget(
                    position=world_pos,
                    facing_deg=obj_res.facing_deg,
                    clearance_radius=0.20,
                    target_type="surface",
                    target_name=ref,
                    height_m=sdata.get("height_m"),
                    area_m2=sdata.get("area_m2"),
                ), ""

            # 3c. Check parts
            parts = asset_meta.get("parts", {})
            if sub_ref in parts:
                pdata = parts[sub_ref]
                hinge = np.array(pdata.get("hinge", [0, 0, 0]), float)
                theta = math.radians(obj_res.facing_deg)
                rot_x = hinge[0] * math.cos(theta) - hinge[2] * math.sin(theta)
                rot_z = hinge[0] * math.sin(theta) + hinge[2] * math.cos(theta)
                world_pos = obj_res.position + np.array([rot_x, hinge[1], rot_z])
                return ResolvedTarget(
                    position=world_pos,
                    facing_deg=obj_res.facing_deg,
                    clearance_radius=0.30,
                    target_type="part",
                    target_name=ref,
                    target_part=sub_ref,
                ), ""

            # 3d. Check stairs (including :top and :bottom portals)
            stairs = asset_meta.get("stairs", {})
            stair_key = sub_ref
            stair_portal = "bottom"
            if sub_ref.endswith("_top") or sub_ref.endswith(":top"):
                stair_key = re.sub(r"(_top|:top)$", "", sub_ref)
                stair_portal = "top"
            elif sub_ref.endswith("_bottom") or sub_ref.endswith(":bottom"):
                stair_key = re.sub(r"(_bottom|:bottom)$", "", sub_ref)
                stair_portal = "bottom"
            elif sub_ref in ("top", "bottom") and obj_ref in stairs:
                stair_key = obj_ref
                stair_portal = sub_ref

            if stair_key in stairs:
                sdata = stairs[stair_key]
                cline = sdata.get("centerline", [])
                if stair_portal == "top":
                    pt = np.array(cline[-1] if cline else [0, 1.7, -11.37], float)
                    prev_pt = np.array(cline[0] if cline else [0, 0, -5.0], float)
                    local_facing = heading_from_vector(prev_pt[0] - pt[0], prev_pt[2] - pt[2])
                else:
                    pt = np.array(cline[0] if cline else [0, 0, 0], float)
                    next_pt = np.array(cline[-1] if len(cline) > 1 else [0, 1.7, -11.37], float)
                    local_facing = heading_from_vector(next_pt[0] - pt[0], next_pt[2] - pt[2])
                theta = math.radians(obj_res.facing_deg)
                rot_x = pt[0] * math.cos(theta) - pt[2] * math.sin(theta)
                rot_z = pt[0] * math.sin(theta) + pt[2] * math.cos(theta)
                world_pos = obj_res.position + np.array([rot_x, pt[1], rot_z])
                return ResolvedTarget(
                    position=world_pos,
                    facing_deg=(obj_res.facing_deg + local_facing) % 360.0,
                    clearance_radius=0.45,
                    target_type="stairs",
                    target_name=ref,
                ), ""

            valid_subs = list(anchors.keys()) + list(surfaces.keys()) + list(parts.keys()) + list(stairs.keys())
            return None, f"Unknown sub-reference '{sub_ref}' on '{obj_ref}'. Valid alternatives: {', '.join(valid_subs)}"

        # 3e. Check standalone stair IDs across manifest assets (e.g. "stairs_main", "stairs_main_top", "stairs_main_bottom")
        stair_lookup = ref.lower()
        stair_portal = "bottom"
        if stair_lookup.endswith("_top"):
            stair_lookup = stair_lookup[:-4]
            stair_portal = "top"
        elif stair_lookup.endswith("_bottom"):
            stair_lookup = stair_lookup[:-7]
            stair_portal = "bottom"
        for a in self.manifest.get("assets", []):
            stairs = a.get("stairs", {})
            for sid, sdata in stairs.items():
                if sid.lower() == stair_lookup or (stair_lookup in ("stairs", "staircase") and stairs):
                    cline = sdata.get("centerline", [])
                    if stair_portal == "top":
                        pt = np.array(cline[-1] if cline else [0.0, 1.70, -11.37], float)
                        other = np.array(cline[0] if cline else [0.0, 0.0, -5.18], float)
                        facing = heading_from_vector(other[0] - pt[0], other[2] - pt[2])
                    else:
                        pt = np.array(cline[0] if cline else [0.0, 0.0, -5.18], float)
                        other = np.array(cline[-1] if len(cline) > 1 else [0.0, 1.70, -11.37], float)
                        facing = heading_from_vector(other[0] - pt[0], other[2] - pt[2])
                    return ResolvedTarget(
                        position=pt,
                        facing_deg=facing,
                        clearance_radius=0.45,
                        target_type="stairs",
                        target_name=sid,
                        target_asset=a.get("id"),
                    ), ""

        # 4. Check Zone markers: e.g. lane_east.start, lane_east.stop_mark, lane_east.north, lane_east.south
        if "." in ref:
            parts = ref.split(".", 1)
            zone_id, marker_name = parts[0], parts[1]
            zone_markers = self.manifest.get("zone_markers", {})
            if zone_id in zone_markers and marker_name in zone_markers[zone_id]:
                mdata = zone_markers[zone_id][marker_name]
                return ResolvedTarget(
                    position=np.array(mdata.get("pos", [0, 0, 0]), float),
                    facing_deg=float(mdata.get("facing_deg", 0.0)),
                    clearance_radius=0.50,
                    target_type="marker",
                    target_name=ref,
                ), ""
            # Dynamic procedural zone marker fallback from scene_data zones or standard street geometry
            zone_x_map = {
                "lane_east": 1.6,
                "lane_west": -1.6,
                "sidewalk_east": 4.2,
                "sidewalk_west": -4.2,
                "center": 0.0,
                "plaza": 0.0,
            }
            for z in self.scene_data.get("zones", []):
                zid = z.get("id", "")
                if zid == zone_id:
                    bmin = z.get("min", [0, 0, -12])
                    bmax = z.get("max", [0, 0, 12])
                    zone_x_map[zone_id] = 0.5 * (float(bmin[0]) + float(bmax[0]))
            if zone_id in zone_x_map:
                zx = zone_x_map[zone_id]
                marker_z_map = {
                    "north": (-9.0, 180.0),
                    "far_north": (-12.0, 180.0),
                    "start": (-8.0, 180.0) if "west" in zone_id else (8.0, 0.0),
                    "end": (8.0, 180.0) if "west" in zone_id else (-8.0, 0.0),
                    "south": (8.0, 0.0),
                    "far_south": (11.0, 0.0),
                    "center": (0.0, 0.0),
                    "stop_mark": (1.5, 0.0) if "east" in zone_id else (-1.5, 180.0),
                    "mid_north": (-3.5, 180.0),
                    "mid_south": (3.5, 0.0),
                }
                if marker_name in marker_z_map:
                    mz, mfacing = marker_z_map[marker_name]
                    return ResolvedTarget(
                        position=np.array([zx, 0.0, mz], float),
                        facing_deg=mfacing,
                        clearance_radius=0.50,
                        target_type="marker",
                        target_name=ref,
                    ), ""

        # 5. Check Zones: e.g. lane_east, lane_west, bridge.lane_east
        zone_name = ref.split(".")[-1]
        for zone in self.scene_data.get("zones", []):
            if zone.get("id") == zone_name:
                bmin = np.array(zone.get("min", [0, 0, 0]), float)
                bmax = np.array(zone.get("max", [0, 0, 0]), float)
                center = (bmin + bmax) / 2.0
                return ResolvedTarget(
                    position=center,
                    facing_deg=0.0,
                    clearance_radius=float(np.linalg.norm(bmax[[0, 2]] - bmin[[0, 2]]) / 2.0),
                    target_type="zone",
                    target_name=ref,
                ), ""

        # 6. Check Actor in actor_states (by actor_id, label, or asset_id)
        for aid, astate in actor_states.items():
            if aid == ref or astate.get("label", "").lower() == ref.lower() or astate.get("asset_id") == ref:
                pos = np.array(astate.get("position", [0.0, 0.0, 0.0]), float)
                facing = float(astate.get("facing_deg", 0.0))
                is_veh = astate.get("is_vehicle", False)
                return ResolvedTarget(
                    position=pos,
                    facing_deg=facing,
                    clearance_radius=1.8 if is_veh else 0.35,
                    target_type="actor",
                    target_name=aid,
                    target_asset=astate.get("asset_id"),
                ), ""

        # 7. Check scene props/objects from scene_data (e.g. Car, DowntownTower)
        for obj in self.scene_data.get("objects", []):
            if obj.get("name") == ref or obj.get("id") == ref:
                pos = np.array(obj.get("position", [0, 0, 0]), float)
                facing = float(obj.get("heading_deg", 0.0))
                return ResolvedTarget(
                    position=pos,
                    facing_deg=facing,
                    clearance_radius=1.5,
                    target_type="scene_object",
                    target_name=ref,
                    target_asset=obj.get("asset") or obj.get("asset_id"),
                ), ""

        # If not resolved, produce helpful suggestions
        valid_actors = [f"{aid} ({s.get('label')})" for aid, s in actor_states.items()]
        valid_zones = [z.get("id") for z in self.scene_data.get("zones", [])]
        return None, f"Unknown reference '{ref}'. Valid actors: {valid_actors}; Valid zones: {valid_zones}"
