"""Motion Synthesis engine for PainterSpace Shot Function Library.

Computes physical paths, obstacle-avoiding Catmull-Rom trajectories, speed-matched gaits,
vehicle dynamics (acceleration, cruise, braking, reverse, wheel spin, steering, pitch),
threat-vector evasive dodges, social formations, turn-taking gesture schedules,
multi-move combat combos, and parabolic jump arcs.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from plan_model import Beat, StructuredError
from semantic_references import (
    GAP_BETWEEN_BODIES,
    GAP_TO_VEHICLE,
    ReferenceResolver,
    ResolvedTarget,
    heading_from_vector,
    vector_from_heading,
)
from timeline_resolver import ResolvedBeat


@dataclass
class Keyframe:
    time_s: float
    position: List[float]        # [x, y, z]
    heading_deg: float          # heading in degrees
    speed_mps: float = 0.0
    action_clip: str = "idle"
    clip_phase: float = 0.0     # 0.0 to 1.0
    wheel_spin_deg: float = 0.0
    steering_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    height_offset_m: float = 0.0


@dataclass
class ActorPerformance:
    actor_id: str
    is_vehicle: bool = False
    asset_id: str = ""
    label: str = ""
    keyframes: List[Keyframe] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)          # full-body clip schedule for bake_animation
    overlay_actions: List[Dict[str, Any]] = field(default_factory=list)  # upper-body layered gestures
    look_at_targets: List[Dict[str, Any]] = field(default_factory=list)  # procedural head/neck gaze IK targets
    allowed_contacts: List[Dict[str, Any]] = field(default_factory=list)
    part_events: List[Dict[str, Any]] = field(default_factory=list)      # vehicle door/light state events

    def add_action(
        self,
        clip: str,
        t_start: float,
        t_end: float,
        fps: int = 24,
        speed: float = 1.0,
        blend_s: float = 0.2,
    ) -> None:
        self.actions.append({
            "clip": clip,
            "start_s": round(float(t_start), 3),
            "end_s": round(max(float(t_end), float(t_start) + 0.1), 3),
            "speed": round(float(speed), 3),
            "blend_s": float(blend_s),
            "start_frame": int(round(t_start * fps)),
            "end_frame": int(round(t_end * fps)),
        })

    def add_overlay_action(
        self,
        clip: str,
        t_start: float,
        t_end: float,
        fps: int = 24,
        weight: float = 0.75,
        blend_s: float = 0.2,
    ) -> None:
        self.overlay_actions.append({
            "clip": clip,
            "start_s": round(float(t_start), 3),
            "end_s": round(max(float(t_end), float(t_start) + 0.1), 3),
            "weight": round(float(weight), 3),
            "blend_s": float(blend_s),
            "start_frame": int(round(t_start * fps)),
            "end_frame": int(round(t_end * fps)),
        })

    def add_look_at(
        self,
        target_pos: List[float],
        t_start: float,
        t_end: float,
        target_actor: str = "",
        weight: float = 0.85,
    ) -> None:
        self.look_at_targets.append({
            "target_pos": [round(float(c), 3) for c in target_pos[:3]],
            "target_actor": target_actor,
            "start_s": round(float(t_start), 3),
            "end_s": round(float(t_end), 3),
            "weight": round(float(weight), 3),
        })

    def sample(self, t: float) -> Tuple[List[float], float]:
        if not self.keyframes:
            return [0.0, 0.0, 0.0], 0.0
        if t <= self.keyframes[0].time_s:
            return list(self.keyframes[0].position), self.keyframes[0].heading_deg
        if t >= self.keyframes[-1].time_s:
            return list(self.keyframes[-1].position), self.keyframes[-1].heading_deg
        for i in range(len(self.keyframes) - 1):
            k0 = self.keyframes[i]
            k1 = self.keyframes[i + 1]
            if k0.time_s <= t <= k1.time_s:
                alpha = (t - k0.time_s) / (k1.time_s - k0.time_s) if k1.time_s > k0.time_s else 0.0
                pos = [
                    k0.position[c] + alpha * (k1.position[c] - k0.position[c])
                    for c in range(3)
                ]
                diff = (k1.heading_deg - k0.heading_deg + 180.0) % 360.0 - 180.0
                heading = (k0.heading_deg + alpha * diff) % 360.0
                return pos, heading
        return list(self.keyframes[-1].position), self.keyframes[-1].heading_deg


class MotionSynthesizer:
    def __init__(
        self,
        manifest: Dict[str, Any],
        fps: int = 24,
        ref_resolver: Optional[ReferenceResolver] = None,
        scene_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.manifest = manifest
        self.fps = fps
        self.clips = manifest.get("clips", {})
        self.assets = {a.get("id"): a for a in manifest.get("assets", [])}
        self.ref_resolver = ref_resolver
        self.scene_data = scene_data or {}

    def _resolve_vehicle_seat_anchor(
        self,
        vehicle_id: str,
        seat_name: str,
        other_performances: Dict[str, ActorPerformance],
    ) -> Tuple[List[float], float]:
        """Resolves local position and facing for any seat on any vehicle asset (data-driven)."""
        v_perf = other_performances.get(vehicle_id)
        v_asset_id = v_perf.asset_id if v_perf and v_perf.asset_id else ""
        v_asset = self.assets.get(v_asset_id)
        if not v_asset:
            v_asset = next(
                (a for a in self.manifest.get("assets", []) if a.get("id") in (v_asset_id, vehicle_id) or "car" in a.get("tags", [])),
                None,
            )

        # 1. Author-defined anchor in asset metadata
        if v_asset and "anchors" in v_asset and seat_name in v_asset["anchors"]:
            adata = v_asset["anchors"][seat_name]
            return list(adata.get("pos", [0.0, 0.0, 0.0])), float(adata.get("facing_deg", 0.0))

        # 2. Derive from steering wheel part if driver seat
        if v_asset and "parts" in v_asset and "driver" in seat_name.lower():
            sw = v_asset["parts"].get("steering_wheel")
            if sw and "hinge" in sw:
                hx, hy, hz = sw["hinge"]
                return [hx, max(0.25, hy * 0.40), hz + 0.35], 0.0

        # 3. Derive from physical bounds
        bounds = [2.0, 1.5, 4.5]
        if v_asset and "bounds_m" in v_asset:
            bounds = list(v_asset["bounds_m"])

        is_passenger = any(k in seat_name.lower() for k in ("passenger", "right", "fr", "rr", "_r"))
        is_rear = any(k in seat_name.lower() for k in ("rear", "back", "rl", "rr", "row2"))

        x_sign = 1.0 if is_passenger else -1.0
        x_pos = x_sign * (bounds[0] * 0.22)
        y_pos = max(0.30, bounds[1] * 0.25)
        z_pos = (bounds[2] * 0.15) if is_rear else (-bounds[2] * 0.05)

        return [x_pos, y_pos, z_pos], 0.0

    def _resolve_vehicle_door_stand(
        self,
        vehicle_id: str,
        door_name: str,
        other_performances: Dict[str, ActorPerformance],
    ) -> Tuple[List[float], float, str]:
        """Resolves local position, facing, and USD prim path for exiting through any vehicle door."""
        v_perf = other_performances.get(vehicle_id)
        v_asset_id = v_perf.asset_id if v_perf and v_perf.asset_id else ""
        v_asset = self.assets.get(v_asset_id)
        if not v_asset:
            v_asset = next(
                (a for a in self.manifest.get("assets", []) if a.get("id") in (v_asset_id, vehicle_id) or "car" in a.get("tags", [])),
                None,
            )

        stand_anchor_name = f"{door_name}_stand" if not door_name.endswith("_stand") else door_name
        base_door_name = door_name.replace("_stand", "")

        # 1. Author-defined stand anchor
        if v_asset and "anchors" in v_asset and stand_anchor_name in v_asset["anchors"]:
            adata = v_asset["anchors"][stand_anchor_name]
            return list(adata.get("pos", [0.0, 0.0, 0.0])), float(adata.get("facing_deg", 90.0)), base_door_name

        # 2. Derive from door hinge in parts table
        if v_asset and "parts" in v_asset:
            pdata = v_asset["parts"].get(base_door_name)
            if not pdata:
                for k, v in v_asset["parts"].items():
                    if "door" in k:
                        pdata = v
                        base_door_name = k
                        break
            if pdata and "hinge" in pdata:
                hx, hy, hz = pdata["hinge"]
                is_left = hx < 0
                x_pos = hx - 0.45 if is_left else hx + 0.45
                facing = 90.0 if is_left else 270.0
                return [x_pos, 0.0, hz + 0.15], facing, base_door_name

        # 3. Derive from physical bounds
        bounds = [2.0, 1.5, 4.5]
        if v_asset and "bounds_m" in v_asset:
            bounds = list(v_asset["bounds_m"])
        is_left = not any(k in door_name.lower() for k in ("right", "fr", "rr", "_r"))
        x_stand = -(bounds[0] * 0.5 + 0.35) if is_left else (bounds[0] * 0.5 + 0.35)
        facing = 90.0 if is_left else 270.0
        return [x_stand, 0.0, 0.15], facing, base_door_name

    def _resolve_stair(self, stair_ref: str) -> Optional[Dict[str, Any]]:
        """Resolves stair metadata by asset:stair_id, stair_id, or asset_id."""
        obj_name = None
        stair_id = stair_ref
        if ":" in stair_ref:
            obj_name, stair_id = stair_ref.split(":", 1)
        elif "." in stair_ref:
            obj_name, stair_id = stair_ref.split(".", 1)

        for a in self.manifest.get("assets", []):
            a_id = a.get("id", "")
            a_label = a.get("label", "")
            if obj_name and obj_name.lower() not in (a_id.lower(), a_label.lower()):
                continue
            stairs = a.get("stairs", {})
            for sid, sdata in stairs.items():
                if sid.lower() == stair_id.lower() or not stair_id or stair_id in sid.lower():
                    return {
                        "asset_id": a_id,
                        "stair_id": sid,
                        "data": sdata,
                        "levels": a.get("levels", []),
                    }
        for a in self.manifest.get("assets", []):
            for sid, sdata in a.get("stairs", {}).items():
                if sid.lower() == stair_id.lower() or stair_id.lower() in sid.lower():
                    return {
                        "asset_id": a.get("id", ""),
                        "stair_id": sid,
                        "data": sdata,
                        "levels": a.get("levels", []),
                    }
        return None

    def _build_live_actor_states(
        self,
        t: float,
        actor_id: str,
        curr_pos: List[float],
        curr_heading: float,
        other_performances: Dict[str, ActorPerformance],
        all_initial_states: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Sample all known actor positions and headings at time t."""
        live_states: Dict[str, Dict[str, Any]] = {}
        if all_initial_states:
            for k, st in all_initial_states.items():
                live_states[k] = dict(st)
        for oid, operf in other_performances.items():
            p_pos, p_head = operf.sample(t)
            st = dict(live_states.get(oid, {}))
            st["position"] = p_pos
            st["facing_deg"] = p_head
            st["asset_id"] = operf.asset_id or st.get("asset_id", "")
            st["label"] = operf.label or st.get("label", "")
            st["is_vehicle"] = operf.is_vehicle
            live_states[oid] = st
        self_st = dict(live_states.get(actor_id, {}))
        self_st["position"] = list(curr_pos)
        self_st["facing_deg"] = float(curr_heading)
        live_states[actor_id] = self_st
        return live_states

    def _resolve_target_at_time(
        self,
        beat: ResolvedBeat,
        t: float,
        actor_id: str,
        curr_pos: List[float],
        curr_heading: float,
        fallback_target: Optional[ResolvedTarget],
        other_performances: Dict[str, ActorPerformance],
        all_initial_states: Optional[Dict[str, Dict[str, Any]]] = None,
        ref_resolver: Optional[ReferenceResolver] = None,
    ) -> Optional[ResolvedTarget]:
        """Dynamically re-resolve a beat's target at time t against live actor trajectories."""
        resolver = ref_resolver or self.ref_resolver
        target_ref = beat.params.get("target") or beat.params.get("to") or beat.params.get("at")
        if resolver is None or target_ref is None:
            return fallback_target

        live_states = self._build_live_actor_states(
            t, actor_id, curr_pos, curr_heading, other_performances, all_initial_states
        )
        res, err = resolver.resolve(str(target_ref), live_states, t, source_actor_id=actor_id)
        if not err and res is not None:
            return res
        return fallback_target

    def _collect_obstacles_at_time(
        self,
        t_start: float,
        t_end: float,
        actor_id: str,
        start_pos: List[float],
        end_pos: List[float],
        other_performances: Dict[str, ActorPerformance],
        scene_data: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, float]]:
        """Collect 2D XZ obstacle circles/boxes active during [t_start, t_end] for path avoidance."""
        obstacles: List[Dict[str, float]] = []
        sdata = scene_data if scene_data is not None else self.scene_data
        t_mid = 0.5 * (t_start + t_end)

        # 1. Static scene props (excluding environment ground/stage/stairs)
        for obj in sdata.get("objects", []):
            oid = str(obj.get("id", obj.get("name", "")))
            asset_id = str(obj.get("asset_id", ""))
            if any(k in oid.lower() or k in asset_id.lower() for k in ("stage", "bridge", "street", "plaza", "ground", "stair")):
                continue
            pos = obj.get("position", [0.0, 0.0, 0.0])
            asset_meta = self.assets.get(asset_id, {})
            bounds = asset_meta.get("bounds_m", [1.2, 1.5, 1.2])
            radius = 0.5 * max(float(bounds[0]), float(bounds[2]))
            # Skip if start or end is inside/right next to this object (e.g. target anchor)
            if math.hypot(start_pos[0] - pos[0], start_pos[2] - pos[2]) < radius + 0.55:
                continue
            if math.hypot(end_pos[0] - pos[0], end_pos[2] - pos[2]) < radius + 0.55:
                continue
            obstacles.append({
                "cx": float(pos[0]),
                "cz": float(pos[2]),
                "radius": radius + 0.38,
            })

        # 2. Other vehicles / actors present at t_mid
        for oid, operf in other_performances.items():
            if oid == actor_id or not operf.keyframes:
                continue
            opos, _ = operf.sample(t_mid)
            if operf.is_vehicle:
                v_meta = self.assets.get(operf.asset_id, {})
                bounds = v_meta.get("bounds_m", [2.0, 1.5, 4.5])
                radius = 0.5 * min(float(bounds[0]), float(bounds[2])) + 0.45
            else:
                radius = 0.65
            if math.hypot(start_pos[0] - opos[0], start_pos[2] - opos[2]) < radius + 0.35:
                continue
            if math.hypot(end_pos[0] - opos[0], end_pos[2] - opos[2]) < radius + 0.35:
                continue
            obstacles.append({
                "cx": float(opos[0]),
                "cz": float(opos[2]),
                "radius": radius,
            })

        return obstacles

    def _plan_smooth_path(
        self,
        start_pos: List[float],
        end_pos: List[float],
        obstacles: List[Dict[str, float]],
        n_samples: int = 32,
    ) -> List[List[float]]:
        """Route around blocking XZ obstacles and smooth via centripetal Catmull-Rom spline."""
        dx = end_pos[0] - start_pos[0]
        dz = end_pos[2] - start_pos[2]
        seg_len = math.hypot(dx, dz)
        if seg_len < 0.25 or not obstacles:
            return [list(start_pos), list(end_pos)]

        ux = dx / seg_len
        uz = dz / seg_len
        nx = -uz
        nz = ux

        # Check which obstacles intersect the straight line segment
        blocking: List[Tuple[float, Dict[str, float], float]] = []
        for obs in obstacles:
            rel_x = obs["cx"] - start_pos[0]
            rel_z = obs["cz"] - start_pos[2]
            proj = rel_x * ux + rel_z * uz
            if 0.1 * seg_len < proj < 0.9 * seg_len:
                lat = rel_x * nx + rel_z * nz
                if abs(lat) < obs["radius"]:
                    blocking.append((proj, obs, lat))

        if not blocking:
            return [list(start_pos), list(end_pos)]

        blocking.sort(key=lambda item: item[0])
        control_pts: List[List[float]] = [list(start_pos)]
        for proj, obs, lat in blocking:
            # Detour to the side of the obstacle opposite to its center offset from the line
            side_sign = -1.0 if lat >= 0.0 else 1.0
            detour_offset = obs["radius"] + 0.35
            wp_x = obs["cx"] + nx * side_sign * detour_offset
            wp_z = obs["cz"] + nz * side_sign * detour_offset
            wp_y = start_pos[1] + (end_pos[1] - start_pos[1]) * (proj / seg_len)
            control_pts.append([wp_x, wp_y, wp_z])
        control_pts.append(list(end_pos))

        # Evaluate Catmull-Rom spline through control_pts
        return self._evaluate_catmull_rom_chain(control_pts, n_samples=n_samples)

    def _evaluate_catmull_rom_chain(
        self, points: List[List[float]], n_samples: int = 32
    ) -> List[List[float]]:
        if len(points) <= 2:
            return [list(p) for p in points]

        pts = [points[0]] + points + [points[-1]]
        sampled: List[List[float]] = []
        n_segs = len(points) - 1
        samples_per_seg = max(4, n_samples // n_segs)

        for i in range(n_segs):
            p0 = np.array(pts[i], dtype=float)
            p1 = np.array(pts[i + 1], dtype=float)
            p2 = np.array(pts[i + 2], dtype=float)
            p3 = np.array(pts[i + 3], dtype=float)
            for j in range(samples_per_seg):
                t = j / float(samples_per_seg)
                t2 = t * t
                t3 = t2 * t
                pos = 0.5 * (
                    (2.0 * p1)
                    + (-p0 + p2) * t
                    + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                    + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
                )
                sampled.append([float(pos[0]), float(pos[1]), float(pos[2])])
        sampled.append(list(points[-1]))
        return sampled

    def _sample_polyline(
        self, polyline: List[List[float]], s_norm: float, default_heading: float
    ) -> Tuple[List[float], float, float]:
        """Sample position, tangent heading, and signed curvature (deg/m) at normalized arc-length s_norm in [0, 1]."""
        if len(polyline) < 2:
            return list(polyline[0]), default_heading, 0.0

        seg_lengths: List[float] = []
        total_len = 0.0
        for i in range(len(polyline) - 1):
            d = math.hypot(polyline[i + 1][0] - polyline[i][0], polyline[i + 1][2] - polyline[i][2])
            seg_lengths.append(d)
            total_len += d

        if total_len < 1e-4:
            return list(polyline[-1]), default_heading, 0.0

        target_d = max(0.0, min(1.0, s_norm)) * total_len
        acc = 0.0
        for i, slen in enumerate(seg_lengths):
            if acc + slen >= target_d or i == len(seg_lengths) - 1:
                u = max(0.0, min(1.0, (target_d - acc) / max(1e-5, slen)))
                p0 = polyline[i]
                p1 = polyline[i + 1]
                pos = [
                    p0[0] + u * (p1[0] - p0[0]),
                    p0[1] + u * (p1[1] - p0[1]),
                    p0[2] + u * (p1[2] - p0[2]),
                ]
                dx = p1[0] - p0[0]
                dz = p1[2] - p0[2]
                heading = heading_from_vector(dx, dz) if slen > 1e-3 else default_heading
                curvature = 0.0
                if i + 2 < len(polyline) and seg_lengths[i + 1] > 1e-3:
                    h_next = heading_from_vector(polyline[i + 2][0] - p1[0], polyline[i + 2][2] - p1[2])
                    diff = (h_next - heading + 180.0) % 360.0 - 180.0
                    curvature = diff / max(0.25, slen)
                return pos, heading, curvature
            acc += slen

        return list(polyline[-1]), default_heading, 0.0

    def synthesize_actor(
        self,
        actor_id: str,
        initial_state: Dict[str, Any],
        beats: List[ResolvedBeat],
        resolved_targets: Dict[str, ResolvedTarget],
        other_performances: Dict[str, ActorPerformance],
        total_duration_s: float,
        seed: int = 42,
        ref_resolver: Optional[ReferenceResolver] = None,
        scene_data: Optional[Dict[str, Any]] = None,
        all_initial_states: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[Optional[ActorPerformance], Optional[StructuredError]]:
        """Synthesize continuous keyframes and performance clips for one actor across all beats."""
        rng = random.Random(seed + hash(actor_id) % 1000)
        is_vehicle = initial_state.get("is_vehicle", False) or "car" in actor_id or "car" in initial_state.get("asset_id", "")
        perf = ActorPerformance(
            actor_id=actor_id,
            is_vehicle=is_vehicle,
            asset_id=initial_state.get("asset_id", ""),
            label=initial_state.get("label", ""),
        )

        curr_pos = list(initial_state.get("position", [0.0, 0.0, 0.0]))
        curr_heading = float(initial_state.get("facing_deg", 0.0))
        curr_wheel_spin = 0.0

        # Seating and vehicle attachment state for characters
        seated_vehicle = initial_state.get("seated_vehicle")
        seat_anchor_name = initial_state.get("seat_anchor", "driver_seat") or "driver_seat"
        at_ref = initial_state.get("at_reference", "")
        if not seated_vehicle and at_ref and ":" in at_ref:
            ref_obj, ref_sub = at_ref.split(":", 1)
            if "seat" in ref_sub.lower() or ref_obj in other_performances:
                seated_vehicle = ref_obj
                seat_anchor_name = ref_sub

        if not is_vehicle and seated_vehicle and seated_vehicle in other_performances:
            local_seat, local_facing = self._resolve_vehicle_seat_anchor(seated_vehicle, seat_anchor_name, other_performances)
            v_perf = other_performances[seated_vehicle]
            v_pos, v_heading = v_perf.sample(0.0)
            theta = math.radians(v_heading)
            rot_x = local_seat[0] * math.cos(theta) - local_seat[2] * math.sin(theta)
            rot_z = local_seat[0] * math.sin(theta) + local_seat[2] * math.cos(theta)
            curr_pos = [v_pos[0] + rot_x, v_pos[1] + local_seat[1], v_pos[2] + rot_z]
            curr_heading = (v_heading + local_facing) % 360.0

        wheel_radius = 0.34
        wheelbase = 2.70

        sorted_beats = sorted(beats, key=lambda b: b.start_time)
        dt = 1.0 / self.fps

        for beat in sorted_beats:
            fn = beat.function
            t_start = beat.start_time
            t_end = beat.end_time
            dur = max(0.01, t_end - t_start)
            target_ref = beat.params.get("target") or beat.params.get("to")
            # Dynamic beat-start reference resolution against live trajectories at t_start
            target = self._resolve_target_at_time(
                beat=beat,
                t=t_start,
                actor_id=actor_id,
                curr_pos=curr_pos,
                curr_heading=curr_heading,
                fallback_target=resolved_targets.get(beat.beat_id),
                other_performances=other_performances,
                all_initial_states=all_initial_states,
                ref_resolver=ref_resolver,
            )

            # -------------------------------------------------------------
            # 1. VEHICLE FUNCTIONS
            # -------------------------------------------------------------
            if is_vehicle:
                if fn in ("drive_to", "drive_along", "drive_past", "drive_toward", "brake_to_stop", "reverse_to"):
                    is_reverse = (fn == "reverse_to")
                    mode = beat.params.get("mode", "stop_short")
                    end_pos = list(curr_pos)

                    if target:
                        end_pos = list(target.position)
                        if fn == "drive_toward":
                            if mode == "stop_short":
                                gap = GAP_TO_VEHICLE.get(str(beat.params.get("gap", "near")).lower(), 1.8)
                                dir_vec = np.array(end_pos) - np.array(curr_pos)
                                dir_len = np.linalg.norm(dir_vec[[0, 2]])
                                if dir_len > 1e-3:
                                    end_pos = list(np.array(end_pos) - (dir_vec / dir_len) * gap)
                            elif mode == "near_miss":
                                end_pos[0] += 0.8
                            elif mode == "collide":
                                pass
                    else:
                        fwd_x, fwd_z = vector_from_heading(curr_heading)
                        sign_dir = -1.0 if is_reverse else 1.0
                        dist = dur * (3.0 if is_reverse else 6.0)
                        end_pos = [curr_pos[0] + sign_dir * fwd_x * dist, curr_pos[1], curr_pos[2] + sign_dir * fwd_z * dist]

                    dist_to_travel = math.hypot(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])
                    v_max = 16.0
                    a_brake = 7.5 if beat.params.get("braking") == "emergency" else 4.5
                    min_brake_dist = (v_max * 0.5) ** 2 / (2.0 * a_brake)

                    if fn == "brake_to_stop" and dist_to_travel < min_brake_dist * 0.4:
                        return None, StructuredError(
                            error_code="INFEASIBLE_BRAKING_DISTANCE",
                            message=f"Vehicle '{actor_id}' cannot brake to stop within {dist_to_travel:.2f} m (needs at least {min_brake_dist:.2f} m).",
                            beat_id=beat.beat_id,
                            actor_id=actor_id,
                            details={"available_m": dist_to_travel, "required_m": min_brake_dist},
                            suggestions=["Start braking earlier or reduce approach speed."],
                        )

                    n_frames = max(1, int(round(dur * self.fps)))
                    dx = end_pos[0] - curr_pos[0]
                    dz = end_pos[2] - curr_pos[2]
                    if dist_to_travel > 0.05:
                        move_dir_heading = heading_from_vector(dx, dz)
                        travel_heading = (move_dir_heading + 180.0) % 360.0 if is_reverse else move_dir_heading
                    else:
                        travel_heading = curr_heading

                    heading_diff = (travel_heading - curr_heading + 180.0) % 360.0 - 180.0

                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        s = 3.0 * (u ** 2) - 2.0 * (u ** 3)
                        pos = [
                            curr_pos[0] + dx * s,
                            curr_pos[1] + (end_pos[1] - curr_pos[1]) * s,
                            curr_pos[2] + dz * s,
                        ]
                        v_inst = (6.0 * u * (1.0 - u)) * (dist_to_travel / dur) if dur > 0 else 0.0

                        delta_dist = (dist_to_travel / n_frames) if n_frames > 0 else 0.0
                        spin_sign = -1.0 if is_reverse else 1.0
                        curr_wheel_spin += spin_sign * (delta_dist / wheel_radius) * (180.0 / math.pi)

                        # Ackermann steering approximation during heading transitions
                        steer = max(-32.0, min(32.0, heading_diff * (1.0 - u))) if abs(heading_diff) > 1.0 else 0.0

                        pitch = 0.0
                        if u > 0.7:
                            pitch = (0.8 if is_reverse else -1.2) * (u - 0.7) / 0.3

                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=pos,
                            heading_deg=travel_heading,
                            speed_mps=v_inst,
                            wheel_spin_deg=curr_wheel_spin,
                            steering_deg=steer,
                            pitch_deg=pitch,
                        ))

                    curr_pos = end_pos
                    curr_heading = travel_heading

                elif fn in ("open_door", "close_door", "set_lights", "wait", "idle"):
                    if fn in ("open_door", "close_door", "set_lights"):
                        perf.part_events.append({
                            "function": fn,
                            "start_s": round(t_start, 3),
                            "end_s": round(t_end, 3),
                            "params": dict(beat.params),
                        })
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            speed_mps=0.0,
                            wheel_spin_deg=curr_wheel_spin,
                        ))

            # -------------------------------------------------------------
            # 2. CHARACTER FUNCTIONS
            # -------------------------------------------------------------
            else:
                is_seated_coupling = (
                    fn in ("drive", "sit_in", "sit_on")
                    or (
                        seated_vehicle is not None
                        and fn not in (
                            "exit_vehicle",
                            "open_part",
                            "walk_to",
                            "run_to",
                            "sneak_to",
                            "jog_to",
                            "crawl_to",
                            "stumble_to",
                            "back_up_to",
                            "carry_to",
                            "pace_while_talking",
                            "swim_to",
                            "fly_to",
                            "approach",
                            "enter_vehicle",
                            "flee_from",
                            "follow",
                        )
                    )
                )
                if is_seated_coupling:
                    if fn in ("drive", "sit_in", "sit_on"):
                        v_target = beat.params.get("vehicle")
                        if not v_target and target_ref and ":" in str(target_ref):
                            v_target, s_name = str(target_ref).split(":", 1)
                            seat_anchor_name = s_name
                        if v_target:
                            seated_vehicle = v_target

                    local_seat, local_facing = self._resolve_vehicle_seat_anchor(seated_vehicle, seat_anchor_name, other_performances)

                    if fn == "honk_horn":
                        clip_name = "honking_horn" if "honking_horn" in self.clips else "sitting"
                    elif fn == "use_phone":
                        clip_name = "talking_on_a_cell_phone" if "talking_on_a_cell_phone" in self.clips else "sitting"
                    elif fn in ("drive", "sit_in", "sit_on", "wait", "idle"):
                        clip_name = "sitting"
                    elif fn in ("react", "flinch"):
                        clip_name = "reaction"
                    elif fn in ("converse", "argue", "gesture", "greet", "emote"):
                        clip_name = "acknowledging"
                    else:
                        clip_name = "sitting"

                    if seated_vehicle and seated_vehicle in other_performances:
                        v_perf = other_performances[seated_vehicle]
                        v_asset_id = v_perf.asset_id or ""
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                        for a_tgt in set(filter(None, [seated_vehicle, v_asset_id])):
                            perf.allowed_contacts.append({
                                "actor": actor_id,
                                "asset": a_tgt,
                                "is_occupant": True,
                                "t0": round(t_start, 3),
                                "t1": round(t_end + 1.5, 3),
                                "max_depth_m": 0.45,
                            })

                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            v_pos, v_heading = v_perf.sample(f_time)
                            theta = math.radians(v_heading)
                            rot_x = local_seat[0] * math.cos(theta) - local_seat[2] * math.sin(theta)
                            rot_z = local_seat[0] * math.sin(theta) + local_seat[2] * math.cos(theta)
                            seat_world_pos = [v_pos[0] + rot_x, v_pos[1] + local_seat[1], v_pos[2] + rot_z]
                            seat_world_heading = (v_heading + local_facing) % 360.0

                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=seat_world_pos,
                                heading_deg=seat_world_heading,
                                speed_mps=0.0,
                                action_clip=clip_name,
                            ))
                        curr_pos = seat_world_pos
                        curr_heading = seat_world_heading
                    else:
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=list(curr_pos),
                                heading_deg=curr_heading,
                                speed_mps=0.0,
                                action_clip=clip_name,
                            ))

                elif fn == "exit_vehicle" or (fn == "open_part" and seated_vehicle is not None):
                    v_id = seated_vehicle or beat.params.get("vehicle")
                    if not v_id:
                        p_ref = beat.params.get("part") or target_ref
                        if p_ref and ":" in str(p_ref):
                            v_id = str(p_ref).split(":")[0]

                    door_part = beat.params.get("door_part")
                    if not door_part and "part" in beat.params and ":" in str(beat.params["part"]):
                        door_part = str(beat.params["part"]).split(":")[1]
                    if not door_part and seat_anchor_name:
                        seat_to_door = {
                            "driver_seat": "door_fl",
                            "driver": "door_fl",
                            "passenger_seat": "door_fr",
                            "passenger": "door_fr",
                            "front_passenger": "door_fr",
                            "seat_rear_left": "door_rl",
                            "rear_left": "door_rl",
                            "seat_rear_right": "door_rr",
                            "rear_right": "door_rr",
                        }
                        door_part = seat_to_door.get(
                            seat_anchor_name,
                            "door_fl" if "left" in seat_anchor_name or "driver" in seat_anchor_name else "door_fr",
                        )
                    if not door_part:
                        door_part = "door_fl"

                    if v_id and v_id in other_performances:
                        v_perf = other_performances[v_id]
                        v_pos, v_heading = v_perf.sample(t_start)
                        v_asset_id = v_perf.asset_id or ""
                    else:
                        v_pos, v_heading = curr_pos, curr_heading
                        v_asset_id = ""

                    local_stand, local_stand_facing, _ = self._resolve_vehicle_door_stand(v_id, door_part, other_performances)

                    theta = math.radians(v_heading)
                    rot_x = local_stand[0] * math.cos(theta) - local_stand[2] * math.sin(theta)
                    rot_z = local_stand[0] * math.sin(theta) + local_stand[2] * math.cos(theta)
                    exit_world_pos = [v_pos[0] + rot_x, 0.0, v_pos[2] + rot_z]
                    exit_world_heading = (v_heading + local_stand_facing) % 360.0

                    clip_name = "opening_door_inwards"
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    for a_tgt in set(filter(None, [v_id, v_asset_id])):
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": a_tgt,
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 1.5, 3),
                            "max_depth_m": 0.50,
                        })

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        s = 3.0 * (u ** 2) - 2.0 * (u ** 3)
                        px = curr_pos[0] + (exit_world_pos[0] - curr_pos[0]) * s
                        py = curr_pos[1] + (exit_world_pos[1] - curr_pos[1]) * s
                        pz = curr_pos[2] + (exit_world_pos[2] - curr_pos[2]) * s
                        diff = (exit_world_heading - curr_heading + 180.0) % 360.0 - 180.0
                        h = (curr_heading + s * diff) % 360.0
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, py, pz],
                            heading_deg=h,
                            speed_mps=0.8,
                            action_clip=clip_name,
                        ))

                    curr_pos = exit_world_pos
                    curr_heading = exit_world_heading
                    seated_vehicle = None

                elif fn == "enter_vehicle":
                    v_id = beat.params.get("vehicle")
                    if not v_id:
                        p_ref = beat.params.get("door") or beat.params.get("seat") or target_ref
                        if p_ref and ":" in str(p_ref):
                            v_id = str(p_ref).split(":")[0]

                    seat_anchor_name = beat.params.get("seat", "driver_seat")
                    local_seat, local_facing = self._resolve_vehicle_seat_anchor(v_id, seat_anchor_name, other_performances)

                    if v_id and v_id in other_performances:
                        v_perf = other_performances[v_id]
                        v_pos, v_heading = v_perf.sample(t_end)
                        v_asset_id = v_perf.asset_id or ""
                        theta = math.radians(v_heading)
                        rot_x = local_seat[0] * math.cos(theta) - local_seat[2] * math.sin(theta)
                        rot_z = local_seat[0] * math.sin(theta) + local_seat[2] * math.cos(theta)
                        seat_world_pos = [v_pos[0] + rot_x, v_pos[1] + local_seat[1], v_pos[2] + rot_z]
                        seat_world_heading = (v_heading + local_facing) % 360.0

                        for a_tgt in set(filter(None, [v_id, v_asset_id])):
                            perf.allowed_contacts.append({
                                "actor": actor_id,
                                "asset": a_tgt,
                                "t0": round(t_start, 3),
                                "t1": round(t_end + 1.5, 3),
                                "max_depth_m": 1.0,
                            })

                        clip_name = "crouch_to_stand"
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            u = frame_idx / float(n_frames)
                            s = 3.0 * (u ** 2) - 2.0 * (u ** 3)
                            px = curr_pos[0] + (seat_world_pos[0] - curr_pos[0]) * s
                            py = curr_pos[1] + (seat_world_pos[1] - curr_pos[1]) * s
                            pz = curr_pos[2] + (seat_world_pos[2] - curr_pos[2]) * s
                            diff = (seat_world_heading - curr_heading + 180.0) % 360.0 - 180.0
                            h = (curr_heading + s * diff) % 360.0
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=[px, py, pz],
                                heading_deg=h,
                                speed_mps=0.8,
                                action_clip="sitting" if u > 0.7 else clip_name,
                            ))

                        curr_pos = seat_world_pos
                        curr_heading = seat_world_heading
                        seated_vehicle = v_id
                    else:
                        seated_vehicle = v_id

                elif fn in (
                    "walk_to", "run_to", "sneak_to", "jog_to", "approach",
                    "flee_from", "follow", "disperse", "take_cover",
                    "crawl_to", "stumble_to", "back_up_to", "carry_to", "swim_to", "fly_to",
                ):
                    is_backward_motion = False
                    if fn in ("run_to", "flee_from", "take_cover"):
                        gait = "run"
                    elif fn == "sneak_to":
                        gait = "sneak"
                    elif fn == "jog_to":
                        gait = "jog"
                    elif fn == "crawl_to":
                        is_backward_motion = str(beat.params.get("direction", "forward")).lower().startswith("back")
                        gait = "crawl_back" if is_backward_motion else "crawl"
                    elif fn == "stumble_to":
                        st_style = str(beat.params.get("style", "injured")).lower()
                        gait = "drunk" if "drunk" in st_style else ("sad" if "sad" in st_style else "injured")
                    elif fn == "back_up_to":
                        is_backward_motion = True
                        b_pace = str(beat.params.get("pace", "walk")).lower()
                        gait = "back_run" if "run" in b_pace else ("back_jog" if "jog" in b_pace else "back_walk")
                    elif fn == "carry_to":
                        c_item = str(beat.params.get("item", "bag")).lower()
                        c_pace = str(beat.params.get("pace", "walk")).lower()
                        gait = "carry_box" if ("box" in c_item or "heavy" in c_item or "jog" in c_pace) else "carry_bag"
                    elif fn == "swim_to":
                        gait = "swim"
                    elif fn == "fly_to":
                        f_style = str(beat.params.get("style", "superhero")).lower()
                        gait = "float" if "float" in f_style or "hover" in f_style else "fly"
                    elif fn in ("approach", "follow", "disperse"):
                        gait = str(beat.params.get("gait", "walk")).lower()
                    else:
                        gait = "walk"

                    default_speeds = {
                        "walk": 1.4, "run": 4.2, "sneak": 0.9, "jog": 2.6,
                        "crawl": 0.55, "crawl_back": 0.50,
                        "injured": 0.85, "drunk": 0.90, "sad": 1.0,
                        "back_walk": 1.0, "back_jog": 1.6, "back_run": 2.6,
                        "carry_bag": 1.25, "carry_box": 2.0,
                        "swim": 1.2, "fly": 5.5, "float": 2.0,
                    }
                    default_clips = {
                        "walk": "walk", "run": "run", "sneak": "crouched_walking", "jog": "run",
                        "crawl": "crawling", "crawl_back": "crawling_backwards",
                        "injured": "injured_walking", "drunk": "drunk_walk", "sad": "sad_walk_arc_right",
                        "back_walk": "backward_walking_turn", "back_jog": "slow_jog_backwards", "back_run": "run_backward",
                        "carry_bag": "walking_with_shopping_bag", "carry_box": "jogging_with_box",
                        "swim": "swimming", "fly": "superhero_flying", "float": "floating",
                    }
                    gait_table = self.manifest.get("assets", [{}])[0].get("gait_table", {})
                    gait_info = gait_table.get(gait, {
                        "clip": default_clips.get(gait, "walk"),
                        "speed_mps": default_speeds.get(gait, 1.4),
                    })
                    nominal_clip_speed = float(gait_info.get("speed_mps", default_speeds.get(gait, 1.4)))
                    clip_name = beat.params.get("clip") or gait_info.get("clip", default_clips.get(gait, "walk"))
                    if clip_name not in self.clips:
                        clip_name = "walk" if "walk" in self.clips else "acknowledging"

                    end_pos = list(curr_pos)
                    target_facing = curr_heading

                    if fn == "flee_from":
                        threat_name = str(beat.params.get("threat") or target_ref or "car_1")
                        threat_pos = list(target.position) if target else [curr_pos[0], curr_pos[1], curr_pos[2] - 4.0]
                        if threat_name in other_performances:
                            threat_pos, _ = other_performances[threat_name].sample(t_start)
                        away_dx = curr_pos[0] - threat_pos[0]
                        away_dz = curr_pos[2] - threat_pos[2]
                        away_len = math.hypot(away_dx, away_dz)
                        flee_dist = float(beat.params.get("distance_m", min(10.0, dur * nominal_clip_speed)))
                        if away_len > 1e-3:
                            end_pos = [
                                curr_pos[0] + (away_dx / away_len) * flee_dist,
                                curr_pos[1],
                                curr_pos[2] + (away_dz / away_len) * flee_dist,
                            ]
                        else:
                            end_pos = [curr_pos[0], curr_pos[1], curr_pos[2] + flee_dist]
                        target_facing = heading_from_vector(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])

                    elif fn == "disperse":
                        center_pos = list(target.position) if target else [0.0, curr_pos[1], 0.0]
                        rad_dx = curr_pos[0] - center_pos[0]
                        rad_dz = curr_pos[2] - center_pos[2]
                        rad_len = math.hypot(rad_dx, rad_dz)
                        disp_dist = float(beat.params.get("distance_m", 3.5))
                        if rad_len > 1e-3:
                            ux, uz = rad_dx / rad_len, rad_dz / rad_len
                        else:
                            angle = (hash(actor_id) % 360) * (math.pi / 180.0)
                            ux, uz = math.sin(angle), math.cos(angle)
                        end_pos = [curr_pos[0] + ux * disp_dist, curr_pos[1], curr_pos[2] + uz * disp_dist]
                        target_facing = heading_from_vector(ux, uz)

                    elif fn == "follow":
                        leader_id = str(beat.params.get("target") or target_ref or "")
                        gap = GAP_BETWEEN_BODIES.get(str(beat.params.get("gap", "conversational")).lower(), 1.5)
                        if leader_id in other_performances:
                            l_end_pos, l_end_head = other_performances[leader_id].sample(t_end)
                            fwd_x, fwd_z = vector_from_heading(l_end_head)
                            end_pos = [l_end_pos[0] - fwd_x * gap, curr_pos[1], l_end_pos[2] - fwd_z * gap]
                            target_facing = l_end_head
                        elif target:
                            end_pos = list(target.position)
                            target_facing = target.facing_deg

                    elif target:
                        end_pos = list(target.position)
                        target_facing = target.facing_deg
                        if fn == "approach":
                            gap = GAP_BETWEEN_BODIES.get(str(beat.params.get("gap", "conversational")).lower(), 1.3)
                            dir_vec = np.array(end_pos) - np.array(curr_pos)
                            dir_len = np.linalg.norm(dir_vec[[0, 2]])
                            if dir_len > 1e-3:
                                end_pos = list(np.array(end_pos) - (dir_vec / dir_len) * gap)
                                target_facing = heading_from_vector(dir_vec[0], dir_vec[2])
                        elif fn == "take_cover":
                            # Step behind cover object relative to threat
                            cover_gap = 0.85
                            dir_vec = np.array(end_pos) - np.array(curr_pos)
                            dir_len = np.linalg.norm(dir_vec[[0, 2]])
                            if dir_len > 1e-3:
                                end_pos = list(np.array(end_pos) - (dir_vec / dir_len) * cover_gap)

                    # Clamp travel distance for slow/specialized gaits so actors never exceed physical stride limits
                    if fn in ("crawl_to", "stumble_to", "back_up_to", "carry_to", "swim_to"):
                        raw_dx = end_pos[0] - curr_pos[0]
                        raw_dz = end_pos[2] - curr_pos[2]
                        raw_dist = math.hypot(raw_dx, raw_dz)
                        max_feasible_dist = dur * nominal_clip_speed * 1.5
                        if raw_dist > max_feasible_dist and raw_dist > 1e-3:
                            scale = max_feasible_dist / raw_dist
                            end_pos = [
                                curr_pos[0] + raw_dx * scale,
                                end_pos[1],
                                curr_pos[2] + raw_dz * scale,
                            ]

                    # Route around obstacles and build smooth spline polyline
                    obstacles = self._collect_obstacles_at_time(
                        t_start, t_end, actor_id, curr_pos, end_pos, other_performances, scene_data
                    )
                    polyline = self._plan_smooth_path(curr_pos, end_pos, obstacles)

                    # Compute total arc length of polyline
                    path_len = sum(
                        math.hypot(polyline[i + 1][0] - polyline[i][0], polyline[i + 1][2] - polyline[i][2])
                        for i in range(len(polyline) - 1)
                    )
                    direct_heading = (
                        heading_from_vector(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])
                        if path_len > 0.05
                        else curr_heading
                    )
                    if is_backward_motion:
                        direct_heading = (direct_heading + 180.0) % 360.0
                        target_facing = direct_heading
                    elif not target or (target.facing_deg == 0.0 and fn in ("walk_to", "run_to", "sneak_to", "jog_to", "crawl_to", "stumble_to", "carry_to", "swim_to", "fly_to")):
                        target_facing = direct_heading

                    # Stride-matched playback speed to prevent foot sliding
                    actual_avg_speed = path_len / max(0.1, dur)
                    playback_speed = max(0.65, min(1.85, actual_avg_speed / max(0.2, nominal_clip_speed)))

                    blend_s = float(beat.params.get("blend_s", 0.2))
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=playback_speed, blend_s=blend_s)

                    n_frames = max(1, int(round(dur * self.fps)))
                    is_continuous = bool(beat.params.get("continuous", False))
                    forward_pitch = 4.5 if gait == "run" else (2.5 if gait == "jog" else 1.0)
                    fly_alt = float(beat.params.get("altitude_m", 2.5)) if fn == "fly_to" else 0.0

                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)

                        if is_continuous:
                            s = u
                            inst_spd = actual_avg_speed
                        else:
                            # Smooth ease-in / cruise / ease-out velocity profile across full [0, 1]
                            s = 0.5 * u + 0.5 * (3.0 * (u ** 2) - 2.0 * (u ** 3))
                            ds_du = 0.5 + 0.5 * (6.0 * u * (1.0 - u))
                            inst_spd = actual_avg_speed * ds_du

                        pos, tangent_heading, curvature = self._sample_polyline(polyline, s, direct_heading)
                        if is_backward_motion:
                            tangent_heading = (tangent_heading + 180.0) % 360.0
                        if fn == "fly_to":
                            pos[1] = pos[1] + fly_alt * math.sin(u * math.pi)

                        # Smoothly blend heading toward final target_facing over the final 18% of stride
                        if not is_continuous and u > 0.82:
                            alpha_h = (u - 0.82) / 0.18
                            smooth_a = 3.0 * (alpha_h ** 2) - 2.0 * (alpha_h ** 3)
                            diff = (target_facing - tangent_heading + 180.0) % 360.0 - 180.0
                            heading = (tangent_heading + smooth_a * diff) % 360.0
                        else:
                            heading = tangent_heading

                        # Centripetal torso roll on curved paths
                        roll_val = max(-8.0, min(8.0, -0.12 * curvature * (inst_spd / max(1.0, nominal_clip_speed))))
                        pitch_val = forward_pitch * math.sin(u * math.pi) if not is_continuous else forward_pitch

                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=pos,
                            heading_deg=heading,
                            speed_mps=inst_spd,
                            action_clip=clip_name,
                            clip_phase=u,
                            pitch_deg=pitch_val,
                            roll_deg=roll_val,
                        ))

                    curr_pos = list(end_pos)
                    curr_heading = target_facing

                elif fn == "pace_while_talking":
                    center_pos = list(target.position) if target else list(curr_pos)
                    clip_name = "talking_phone_pacing" if "talking_phone_pacing" in self.clips else "walk"
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    fwd_x, fwd_z = vector_from_heading(curr_heading)
                    pace_span = float(beat.params.get("span_m", 1.4))
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        osc = math.sin(u * 2.0 * math.pi)
                        d_osc = math.cos(u * 2.0 * math.pi)
                        px = center_pos[0] + fwd_x * (pace_span * 0.5 * osc)
                        pz = center_pos[2] + fwd_z * (pace_span * 0.5 * osc)
                        h = curr_heading if d_osc >= 0 else (curr_heading + 180.0) % 360.0
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, center_pos[1], pz],
                            heading_deg=h,
                            speed_mps=abs(d_osc) * 0.9,
                            action_clip=clip_name,
                            clip_phase=u,
                        ))
                    curr_pos = list(center_pos)

                elif fn == "dodge":
                    threat_actor = str(beat.params.get("threat") or "car_1")
                    threat_perf = other_performances.get(threat_actor)

                    d_lateral = float(beat.params.get("lateral_distance_m", 1.8))
                    t_avail = dur
                    v_req = d_lateral / t_avail
                    v_max_dodge = 3.8

                    if v_req > v_max_dodge:
                        return None, StructuredError(
                            error_code="INFEASIBLE_DODGE",
                            message=f"Dodge infeasible: requires moving {d_lateral:.2f} m in {t_avail:.2f} s ({v_req:.2f} m/s), but maximum dodge speed is {v_max_dodge:.2f} m/s.",
                            beat_id=beat.beat_id,
                            actor_id=actor_id,
                            details={"required_speed_mps": v_req, "max_speed_mps": v_max_dodge, "available_time_s": t_avail},
                            suggestions=["Start dodge earlier, increase dodge duration, or slow down the approaching vehicle."],
                        )

                    # Compute true threat approach vector if threat performance is available
                    dodge_dx = -d_lateral
                    dodge_dz = 0.2
                    threat_look_pos = [curr_pos[0], curr_pos[1], curr_pos[2] - 3.0]
                    if threat_perf is not None and threat_perf.keyframes:
                        p0, th_head = threat_perf.sample(t_start)
                        p1, _ = threat_perf.sample(t_start + min(0.35, dur))
                        threat_look_pos = list(p0)
                        vx = p1[0] - p0[0]
                        vz = p1[2] - p0[2]
                        v_len = math.hypot(vx, vz)
                        if v_len > 1e-3:
                            ux, uz = vx / v_len, vz / v_len
                        else:
                            ux, uz = vector_from_heading(th_head)
                        # Two perpendicular escape normals: n1 = (-uz, ux), n2 = (uz, -ux)
                        rel_x = curr_pos[0] - p0[0]
                        rel_z = curr_pos[2] - p0[2]
                        dot1 = rel_x * (-uz) + rel_z * ux
                        pref_dir = str(beat.params.get("direction", "")).lower()
                        if pref_dir == "left":
                            nx, nz = -uz, ux
                        elif pref_dir == "right":
                            nx, nz = uz, -ux
                        elif abs(dot1) > 0.15:
                            nx, nz = (-uz, ux) if dot1 > 0 else (uz, -ux)
                        else:
                            # Default step toward negative X (sidewalk/clear lane)
                            nx, nz = (-uz, ux) if (-uz) <= (uz) else (uz, -ux)
                        dodge_dx = nx * d_lateral
                        dodge_dz = nz * d_lateral + ux * 0.2

                    end_pos = [curr_pos[0] + dodge_dx, curr_pos[1], curr_pos[2] + dodge_dz]
                    # Orient actor to face the threat as they evade
                    face_threat_heading = heading_from_vector(
                        threat_look_pos[0] - end_pos[0],
                        threat_look_pos[2] - end_pos[2],
                    ) if math.hypot(threat_look_pos[0] - end_pos[0], threat_look_pos[2] - end_pos[2]) > 0.1 else curr_heading

                    perf.add_action("dodging_right", t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.1)
                    perf.add_look_at(threat_look_pos, t_start, t_end, target_actor=threat_actor, weight=0.9)

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        s = math.sin(u * math.pi * 0.5)
                        pos = [
                            curr_pos[0] + dodge_dx * s,
                            curr_pos[1],
                            curr_pos[2] + dodge_dz * s,
                        ]
                        diff = (face_threat_heading - curr_heading + 180.0) % 360.0 - 180.0
                        h = (curr_heading + s * diff) % 360.0
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=pos,
                            heading_deg=h,
                            speed_mps=v_req * math.cos(u * math.pi * 0.5),
                            action_clip="dodging_right",
                            clip_phase=u,
                        ))

                    curr_pos = end_pos
                    curr_heading = face_threat_heading

                elif fn == "climb_stairs":
                    stairs_ref = str(beat.params.get("stairs", "")).strip()
                    direction = str(beat.params.get("direction", "up")).lower()
                    pace = str(beat.params.get("pace", "walk")).lower()
                    speed_mps_param = beat.params.get("speed_mps")

                    if pace not in ("walk", "run"):
                        return None, StructuredError(
                            error_code="INVALID_STAIR_PACE",
                            message=f"Pace '{pace}' is not supported on stairs. Supported paces: 'walk', 'run'.",
                            beat_id=beat.beat_id,
                            actor_id=actor_id,
                            suggestions=["Use pace='walk' or pace='run'."],
                        )

                    max_allowed_speed = 1.0 if direction == "down" else (2.5 if pace == "run" else 0.8)
                    if speed_mps_param is not None and float(speed_mps_param) > max_allowed_speed:
                        return None, StructuredError(
                            error_code="UNSAFE_STAIR_SPEED",
                            message=f"Requested speed {speed_mps_param} m/s exceeds safe stair speed limit ({max_allowed_speed} m/s) for {direction} {pace}.",
                            beat_id=beat.beat_id,
                            actor_id=actor_id,
                            details={"requested_speed": float(speed_mps_param), "max_safe_speed": max_allowed_speed},
                            suggestions=[f"Reduce speed_mps to <= {max_allowed_speed} m/s."],
                        )

                    stair_info = self._resolve_stair(stairs_ref)
                    if not stair_info:
                        sdata = {
                            "centerline": [
                                [curr_pos[0], curr_pos[1], curr_pos[2]],
                                [curr_pos[0], curr_pos[1] + (3.0 if direction == "up" else -3.0), curr_pos[2] + (5.0 if direction == "up" else -5.0)],
                            ],
                            "treads": [{"tread_m": 0.33, "rise_m": 0.2}],
                            "from_level": "L0",
                            "to_level": "L1",
                        }
                        stair_obj_name = "stairs"
                    else:
                        sdata = stair_info["data"]
                        stair_obj_name = stair_info["asset_id"]

                    treads = sdata.get("treads", [])
                    tread_depth = np.mean([float(t.get("tread_m", 0.30)) for t in treads]) if treads else 0.30
                    tread_depth = max(0.15, float(tread_depth))

                    if speed_mps_param is not None:
                        v_h = float(speed_mps_param)
                    elif direction == "down":
                        v_h = 0.90 if pace == "run" else 0.65
                    else:
                        v_h = 2.0 if pace == "run" else 0.65

                    target_step_freq = v_h / tread_depth

                    if direction == "up":
                        clip_name = "running_up_stairs" if pace == "run" else "walking_up_the_stairs"
                    else:
                        clip_name = "descending_stairs_fast" if pace == "run" else "descending_stairs"

                    clip_meta = self.manifest.get("clips", {}).get(clip_name, {})
                    f_clip = float(clip_meta.get("step_frequency_hz") or (3.43 if "run" in clip_name or "fast" in clip_name else 1.714))

                    playback_speed = max(0.5, min(2.5, target_step_freq / max(0.1, f_clip)))

                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=round(playback_speed, 3), blend_s=0.2)

                    raw_cline = sdata.get("centerline", [])
                    if len(raw_cline) < 2:
                        raw_cline = [
                            [curr_pos[0], curr_pos[1], curr_pos[2]],
                            [curr_pos[0], curr_pos[1] + (3.0 if direction == "up" else -3.0), curr_pos[2] + 4.0],
                        ]

                    if direction == "down":
                        cline_pts = [list(pt) for pt in reversed(raw_cline)]
                    else:
                        cline_pts = [list(pt) for pt in raw_cline]

                    p0 = np.array(cline_pts[0], float)
                    d_start = np.linalg.norm(np.array(curr_pos) - p0)
                    if d_start < 4.0:
                        shift_xz = np.array(curr_pos)[[0, 2]] - p0[[0, 2]]
                        for pt in cline_pts:
                            pt[0] += shift_xz[0]
                            pt[2] += shift_xz[1]

                    y_start = float(curr_pos[1])
                    y_end = float(cline_pts[-1][1])
                    total_horiz = sum(
                        math.hypot(cline_pts[i + 1][0] - cline_pts[i][0], cline_pts[i + 1][2] - cline_pts[i][2])
                        for i in range(len(cline_pts) - 1)
                    )
                    total_horiz = max(0.1, total_horiz)

                    travel_dx = cline_pts[-1][0] - cline_pts[0][0]
                    travel_dz = cline_pts[-1][2] - cline_pts[0][2]
                    travel_heading = heading_from_vector(travel_dx, travel_dz) if math.hypot(travel_dx, travel_dz) > 0.05 else curr_heading

                    perf.allowed_contacts.append({
                        "actor": actor_id,
                        "asset": stair_obj_name,
                        "t0": round(t_start, 3),
                        "t1": round(t_end + 0.5, 3),
                        "max_depth_m": 0.45,
                    })

                    n_frames = max(1, int(round(dur * self.fps)))
                    pitch_target = 7.5 if direction == "up" else -3.0

                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)

                        s_curr = u * total_horiz
                        acc = 0.0
                        pos_x, pos_z = cline_pts[-1][0], cline_pts[-1][2]
                        for i in range(len(cline_pts) - 1):
                            seg_len = math.hypot(cline_pts[i + 1][0] - cline_pts[i][0], cline_pts[i + 1][2] - cline_pts[i][2])
                            if acc + seg_len >= s_curr or i == len(cline_pts) - 2:
                                seg_u = max(0.0, min(1.0, (s_curr - acc) / max(1e-4, seg_len)))
                                pos_x = cline_pts[i][0] + seg_u * (cline_pts[i + 1][0] - cline_pts[i][0])
                                pos_z = cline_pts[i][2] + seg_u * (cline_pts[i + 1][2] - cline_pts[i][2])
                                break
                            acc += seg_len

                        d_ease = min(0.35, total_horiz / 3.0)
                        dy_total = y_end - y_start
                        m_slope = dy_total / max(1e-4, total_horiz - d_ease)
                        if s_curr <= 0:
                            pos_y = y_start
                        elif s_curr < d_ease:
                            pos_y = y_start + 0.5 * (m_slope / d_ease) * (s_curr ** 2)
                        elif s_curr <= total_horiz - d_ease:
                            pos_y = y_start + 0.5 * m_slope * d_ease + m_slope * (s_curr - d_ease)
                        elif s_curr < total_horiz:
                            ds = total_horiz - s_curr
                            pos_y = y_end - 0.5 * (m_slope / d_ease) * (ds ** 2)
                        else:
                            pos_y = y_end

                        if u < 0.15:
                            pitch_val = pitch_target * (u / 0.15)
                        elif u > 0.85:
                            pitch_val = pitch_target * ((1.0 - u) / 0.15)
                        else:
                            pitch_val = pitch_target

                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[pos_x, pos_y, pos_z],
                            heading_deg=travel_heading,
                            speed_mps=v_h,
                            action_clip=clip_name,
                            clip_phase=u,
                            pitch_deg=pitch_val,
                        ))

                    curr_pos = [cline_pts[-1][0], y_end, cline_pts[-1][2]]
                    curr_heading = travel_heading

                elif fn in ("react", "hit_reaction", "spooked", "flinch"):
                    style = str(beat.params.get("style", "")).lower()
                    if fn == "hit_reaction":
                        clip_name = "head_hit_during_fight"
                    elif fn == "spooked" or style in ("startled", "spooked", "fear", "panic"):
                        clip_name = "spooked"
                    elif style in ("happy", "cheer", "excited"):
                        clip_name = "clapping"
                    else:
                        clip_name = "reaction"

                    if target and math.hypot(target.position[0] - curr_pos[0], target.position[2] - curr_pos[2]) > 0.1:
                        curr_heading = heading_from_vector(
                            target.position[0] - curr_pos[0],
                            target.position[2] - curr_pos[2],
                        )
                        perf.add_look_at(target.position, t_start, t_end, weight=0.9)

                    if fn in ("hit_reaction", "react"):
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 0.5, 3),
                            "max_depth_m": 0.35,
                        })
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.1)
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            speed_mps=0.0,
                            action_clip=clip_name,
                        ))

                elif fn in ("attack", "push", "shove"):
                    if fn in ("push", "shove"):
                        moves = [beat.params.get("clip", "elbow_punch")]
                    else:
                        moves = beat.params.get("moves", ["left_hook", "elbow_punch"])
                        if isinstance(moves, str):
                            moves = [moves]
                    if not moves:
                        moves = ["left_hook"]

                    t_target = str(beat.params.get("target") or "")
                    target_perf = other_performances.get(t_target)
                    target_asset = target_perf.asset_id if target_perf and target_perf.asset_id else t_target

                    # Auto-face target at start of attack
                    if target and math.hypot(target.position[0] - curr_pos[0], target.position[2] - curr_pos[2]) > 0.05:
                        curr_heading = heading_from_vector(
                            target.position[0] - curr_pos[0],
                            target.position[2] - curr_pos[2],
                        )
                        perf.add_look_at(target.position, t_start, t_end, target_actor=t_target, weight=0.9)

                    # Scoped contact declaration for striking limbs
                    attack_joints = [
                        "LeftHand", "RightHand", "RightArm", "LeftArm",
                        "LeftForeArm", "RightForeArm", "Spine", "Spine1", "Spine2", "Neck", "Head", "Hips",
                    ]
                    if any("kick" in str(m).lower() for m in moves):
                        attack_joints.extend([
                            "LeftFoot", "RightFoot", "LeftLeg", "RightLeg",
                            "LeftUpLeg", "RightUpLeg", "LeftToeBase", "RightToeBase",
                        ])

                    for a_tgt in set(filter(None, [t_target, target_asset])):
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": a_tgt,
                            "joints": attack_joints,
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 0.5, 3),
                            "max_depth_m": 0.35,
                        })

                    # Schedule all moves in the combo sequentially across [t_start, t_end]
                    n_moves = len(moves)
                    sub_dur = dur / float(n_moves)
                    for m_idx, m_clip in enumerate(moves):
                        m_t0 = t_start + m_idx * sub_dur
                        m_t1 = t_start + (m_idx + 1) * sub_dur
                        perf.add_action(str(m_clip), m_t0, m_t1, fps=self.fps, speed=1.0, blend_s=0.15)

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        m_idx = min(n_moves - 1, int(u * n_moves))
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            speed_mps=0.0,
                            action_clip=str(moves[m_idx]),
                            clip_phase=(u * n_moves) % 1.0,
                        ))

                elif fn in ("open_part", "close_part"):
                    clip_name = "opening_door_inwards" if fn == "open_part" else "pulling_lever"
                    part_ref = str(beat.params.get("part", "car_1:door_fl"))
                    part_car = part_ref.split(":")[0] if ":" in part_ref else "car_1"
                    car_perf = other_performances.get(part_car)
                    car_asset = car_perf.asset_id if car_perf and car_perf.asset_id else ""
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    for a_tgt in set(filter(None, [part_car, car_asset])):
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": a_tgt,
                            "shape": "door",
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 1.5, 3),
                            "max_depth_m": 0.30,
                        })
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            speed_mps=0.0,
                            action_clip=clip_name,
                        ))

                elif fn in ("jump_on", "jump_over", "jump_down"):
                    surface_height = float(beat.params.get("height_m", 0.88))
                    if target and target.height_m:
                        surface_height = target.height_m

                    max_jump = 0.90
                    if fn == "jump_on" and surface_height > max_jump:
                        return None, StructuredError(
                            error_code="INFEASIBLE_JUMP_HEIGHT",
                            message=f"Surface '{beat.params.get('target')}' is {surface_height:.2f} m high; maximum character jump height is {max_jump:.2f} m.",
                            beat_id=beat.beat_id,
                            actor_id=actor_id,
                            details={"surface_height_m": surface_height, "max_jump_m": max_jump},
                            suggestions=["Choose a lower surface or use climbing equipment."],
                        )

                    end_pos = list(target.position) if target else list(curr_pos)

                    y_start = float(curr_pos[1])
                    if fn == "jump_on":
                        y_end = surface_height
                        y_apex = max(y_start, y_end) + 0.25
                    elif fn == "jump_down":
                        y_end = float(target.position[1]) if (target and target.position is not None and len(target.position) > 1) else 0.0
                        y_apex = max(y_start, y_end) + 0.15
                    else:  # jump_over
                        y_end = float(target.position[1]) if (target and target.position is not None and len(target.position) > 1) else y_start
                        y_apex = max(y_start, y_end, surface_height) + 0.25

                    target_obj = None
                    if target and target.target_name:
                        target_obj = target.target_name.split(":")[0]
                    elif "target" in beat.params:
                        target_obj = str(beat.params["target"]).split(":")[0]

                    if target_obj and "(" in target_obj:
                        inner = target_obj.split("(")[1].split(",")[0].split(")")[0].strip()
                        target_obj = inner

                    if target_obj:
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": target_obj,
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 10.0, 3),
                            "max_depth_m": 0.50,
                        })

                    perf.add_action("jump", t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.1)

                    dist = math.hypot(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])
                    jump_heading = heading_from_vector(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2]) if dist > 0.05 else curr_heading

                    chord_mid = (y_start + y_end) / 2.0
                    h_lift = max(0.10, y_apex - chord_mid)

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                        pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                        py = y_start + (y_end - y_start) * u + 4.0 * h_lift * u * (1.0 - u)
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, py, pz],
                            heading_deg=jump_heading,
                            action_clip="jump",
                        ))
                    curr_pos = [end_pos[0], y_end, end_pos[2]]
                    curr_heading = jump_heading

                elif fn in ("converse", "argue", "gesture", "huddle", "greet"):
                    # Determine counterpart or group centroid for facing & gaze IK
                    participants = beat.params.get("participants") or []
                    if isinstance(participants, str):
                        participants = [participants]
                    with_actor = beat.params.get("with_actor") or beat.params.get("target")
                    if with_actor and with_actor not in participants:
                        participants = list(participants) + [str(with_actor)]

                    live_states = self._build_live_actor_states(
                        t_start, actor_id, curr_pos, curr_heading, other_performances, all_initial_states
                    )
                    other_positions: List[List[float]] = []
                    for pid in participants:
                        if pid != actor_id and pid in live_states:
                            other_positions.append(list(live_states[pid].get("position", [0.0, 0.0, 0.0])))
                    if not other_positions and target:
                        other_positions.append(list(target.position))

                    end_pos = list(curr_pos)
                    target_facing = curr_heading
                    look_pos: Optional[List[float]] = None

                    if other_positions:
                        cx = float(np.mean([p[0] for p in other_positions]))
                        cy = float(np.mean([p[1] for p in other_positions]))
                        cz = float(np.mean([p[2] for p in other_positions]))
                        look_pos = [cx, cy, cz]
                        dx = cx - curr_pos[0]
                        dz = cz - curr_pos[2]
                        dist_c = math.hypot(dx, dz)
                        if dist_c > 0.05:
                            target_facing = heading_from_vector(dx, dz)
                            if fn == "huddle" and dist_c > 1.1:
                                # Step into a tight 0.85 m circle around group centroid
                                end_pos = [
                                    cx - (dx / dist_c) * 0.85,
                                    curr_pos[1],
                                    cz - (dz / dist_c) * 0.85,
                                ]

                    if look_pos is not None:
                        perf.add_look_at(look_pos, t_start, t_end, weight=0.85)

                    # Expressive non-combat gesture pools (never use head_hit_during_fight for dialogue!)
                    mood = "heated" if fn in ("argue",) or str(beat.params.get("mood", "")).lower() in ("heated", "angry", "tense") else "calm"
                    clip_pool = ["pointing_forward", "reaction", "acknowledging"] if mood == "heated" else ["acknowledging", "pointing_forward", "clapping"]

                    # Turn-taking schedule for multi-second social beats
                    if dur >= 3.2:
                        n_turns = max(2, min(4, int(round(dur / 2.2))))
                        turn_dur = dur / float(n_turns)
                        turn_clips: List[str] = []
                        for t_idx in range(n_turns):
                            c = rng.choice(clip_pool)
                            turn_clips.append(c)
                            perf.add_action(c, t_start + t_idx * turn_dur, t_start + (t_idx + 1) * turn_dur, fps=self.fps, speed=1.0, blend_s=0.25)
                    else:
                        chosen_clip = rng.choice(clip_pool)
                        turn_clips = [chosen_clip]
                        perf.add_action(chosen_clip, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        s_turn = min(1.0, u / 0.25)
                        diff = (target_facing - curr_heading + 180.0) % 360.0 - 180.0
                        h = (curr_heading + s_turn * diff) % 360.0
                        pos = [
                            curr_pos[0] + (end_pos[0] - curr_pos[0]) * s_turn,
                            curr_pos[1] + (end_pos[1] - curr_pos[1]) * s_turn,
                            curr_pos[2] + (end_pos[2] - curr_pos[2]) * s_turn,
                        ]
                        c_idx = min(len(turn_clips) - 1, int(u * len(turn_clips)))
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=pos,
                            heading_deg=h,
                            action_clip=turn_clips[c_idx],
                        ))
                    curr_pos = end_pos
                    curr_heading = target_facing

                elif fn in ("turn_to_face", "look_at"):
                    target_facing = curr_heading
                    if target:
                        dx = target.position[0] - curr_pos[0]
                        dz = target.position[2] - curr_pos[2]
                        if math.hypot(dx, dz) > 0.05:
                            target_facing = heading_from_vector(dx, dz)
                        perf.add_look_at(target.position, t_start, t_end, weight=0.9)

                    perf.add_action("acknowledging", t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        diff = (target_facing - curr_heading + 180.0) % 360.0 - 180.0
                        h = (curr_heading + u * diff) % 360.0 if fn == "turn_to_face" else curr_heading
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=h,
                            action_clip="acknowledging",
                        ))
                    if fn == "turn_to_face":
                        curr_heading = target_facing

                elif fn in ("stand_at", "stand_up", "fall", "pick_up", "put_down"):
                    if fn == "stand_at" and target:
                        curr_pos = list(target.position)
                        if target.facing_deg != 0.0:
                            curr_heading = target.facing_deg
                    clip_map = {
                        "stand_at": "acknowledging",
                        "stand_up": "crouch_to_stand",
                        "fall": "falling_back_death",
                        "pick_up": "crouch_to_stand",
                        "put_down": "crouch_to_stand",
                    }
                    clip_name = str(beat.params.get("clip") or clip_map.get(fn, "acknowledging"))
                    if clip_name not in self.clips:
                        clip_name = "crouch_to_stand" if fn in ("stand_up", "pick_up", "put_down") else "reaction"

                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            speed_mps=0.0,
                            action_clip=clip_name,
                        ))

                elif fn in ("climb_surface", "vault_over", "perform_acrobatics"):
                    if fn == "climb_surface":
                        c_style = str(beat.params.get("style", "wall")).lower()
                        c_dir = str(beat.params.get("direction", "up")).lower()
                        height_m = float(beat.params.get("height_m", 2.4))
                        if "ladder" in c_style:
                            clip_name = "climbing_ladder"
                        elif "rope" in c_style:
                            clip_name = "climbing_a_rope"
                        elif "roof" in c_style:
                            clip_name = "climbing_slanted_roof"
                        elif c_dir == "down":
                            clip_name = "climbing_down_wall"
                        else:
                            clip_name = "climbing"
                        if clip_name not in self.clips:
                            clip_name = "climbing" if "climbing" in self.clips else "crouch_to_stand"

                        end_pos = list(target.position) if target else list(curr_pos)
                        y_start = float(curr_pos[1])
                        y_end = max(0.0, y_start - height_m) if c_dir == "down" else (y_start + height_m)
                        dist_xz = math.hypot(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])
                        climb_heading = heading_from_vector(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2]) if dist_xz > 0.05 else curr_heading

                        t_obj = str(beat.params.get("target") or "").split(":")[0]
                        if t_obj:
                            perf.allowed_contacts.append({
                                "actor": actor_id,
                                "asset": t_obj,
                                "t0": round(t_start, 3),
                                "t1": round(t_end + 1.0, 3),
                                "max_depth_m": 0.55,
                            })
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.15)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            u = frame_idx / float(n_frames)
                            s = 3.0 * (u ** 2) - 2.0 * (u ** 3)
                            px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * s
                            pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * s
                            py = y_start + (y_end - y_start) * s
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=[px, py, pz],
                                heading_deg=climb_heading,
                                speed_mps=abs(y_end - y_start) / max(0.2, dur),
                                action_clip=clip_name,
                                clip_phase=u,
                            ))
                        curr_pos = [end_pos[0], y_end, end_pos[2]]
                        curr_heading = climb_heading

                    elif fn == "vault_over":
                        v_style = str(beat.params.get("style", "vault")).lower()
                        if "dive" in v_style:
                            clip_name = "dive_roll"
                        elif "roll" in v_style:
                            clip_name = "stand_to_roll"
                        else:
                            clip_name = "vault_over_box"
                        if clip_name not in self.clips:
                            clip_name = "jump"

                        if target:
                            obs_pos = list(target.position)
                            dx = obs_pos[0] - curr_pos[0]
                            dz = obs_pos[2] - curr_pos[2]
                            d_len = math.hypot(dx, dz)
                            if d_len > 0.1:
                                ux, uz = dx / d_len, dz / d_len
                                end_pos = [obs_pos[0] + ux * 1.1, curr_pos[1], obs_pos[2] + uz * 1.1]
                                vault_heading = heading_from_vector(ux, uz)
                            else:
                                fwd_x, fwd_z = vector_from_heading(curr_heading)
                                end_pos = [curr_pos[0] + fwd_x * 2.0, curr_pos[1], curr_pos[2] + fwd_z * 2.0]
                                vault_heading = curr_heading
                        else:
                            fwd_x, fwd_z = vector_from_heading(curr_heading)
                            end_pos = [curr_pos[0] + fwd_x * 2.0, curr_pos[1], curr_pos[2] + fwd_z * 2.0]
                            vault_heading = curr_heading

                        t_obj = str(beat.params.get("target") or "").split(":")[0]
                        if t_obj:
                            perf.allowed_contacts.append({
                                "actor": actor_id,
                                "asset": t_obj,
                                "t0": round(t_start, 3),
                                "t1": round(t_end + 1.0, 3),
                                "max_depth_m": 0.65,
                            })
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.15)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            u = frame_idx / float(n_frames)
                            px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                            pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                            py = curr_pos[1] + 0.65 * 4.0 * u * (1.0 - u)
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=[px, py, pz],
                                heading_deg=vault_heading,
                                speed_mps=math.hypot(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2]) / max(0.2, dur),
                                action_clip=clip_name,
                                clip_phase=u,
                            ))
                        curr_pos = list(end_pos)
                        curr_heading = vault_heading

                    else:  # perform_acrobatics
                        move = str(beat.params.get("move", "backflip")).lower()
                        acro_map = {
                            "backflip": "backflip",
                            "cartwheel": "cartwheel",
                            "macaco": "macaco_side",
                            "spin": "spinning",
                            "breakdance": "breakdance_freeze_var_3",
                            "breakdance_freeze": "breakdance_freeze_var_3",
                        }
                        clip_name = acro_map.get(move, move if move in self.clips else "backflip")
                        if clip_name not in self.clips:
                            clip_name = "jump"

                        end_pos = list(target.position) if target else list(curr_pos)
                        lift_h = 0.75 if "flip" in clip_name else 0.15
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.15)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + frame_idx * dt
                            u = frame_idx / float(n_frames)
                            px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                            pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                            py = curr_pos[1] + lift_h * 4.0 * u * (1.0 - u)
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=[px, py, pz],
                                heading_deg=curr_heading,
                                speed_mps=0.0,
                                action_clip=clip_name,
                                clip_phase=u,
                            ))
                        curr_pos = [end_pos[0], curr_pos[1], end_pos[2]]

                elif fn == "pull_object":
                    p_style = str(beat.params.get("style", "heavy")).lower()
                    clip_name = "pulling_a_rope" if "rope" in p_style else "pull_heavy_object"
                    if clip_name not in self.clips:
                        clip_name = "pulling_lever"
                    pull_dist = float(beat.params.get("distance_m", 1.2))
                    if target and math.hypot(target.position[0] - curr_pos[0], target.position[2] - curr_pos[2]) > 0.05:
                        curr_heading = heading_from_vector(
                            target.position[0] - curr_pos[0],
                            target.position[2] - curr_pos[2],
                        )
                        perf.add_look_at(target.position, t_start, t_end, weight=0.85)
                    fwd_x, fwd_z = vector_from_heading(curr_heading)
                    end_pos = [curr_pos[0] - fwd_x * pull_dist, curr_pos[1], curr_pos[2] - fwd_z * pull_dist]
                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        s = 3.0 * (u ** 2) - 2.0 * (u ** 3)
                        px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * s
                        pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * s
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, curr_pos[1], pz],
                            heading_deg=curr_heading,
                            speed_mps=pull_dist / max(0.2, dur),
                            action_clip=clip_name,
                            clip_phase=u,
                        ))
                    curr_pos = list(end_pos)

                elif fn in (
                    "kneel", "lie_down", "interact_object", "throw_toward",
                    "inspect_area", "use_phone", "honk_horn", "shake_hands",
                    "hug_or_kiss", "celebrate", "emote", "dance", "perform_activity",
                    "defend_or_block", "use_weapon", "collapse_or_knockout", "medical_aid",
                ):
                    if target and math.hypot(target.position[0] - curr_pos[0], target.position[2] - curr_pos[2]) > 0.08:
                        curr_heading = heading_from_vector(
                            target.position[0] - curr_pos[0],
                            target.position[2] - curr_pos[2],
                        )
                        perf.add_look_at(target.position, t_start, t_end, weight=0.85)

                    if fn == "kneel":
                        st = str(beat.params.get("style", "kneel")).lower()
                        clip_name = "praying" if "pray" in st else ("kneeling_inspecting" if "inspect" in st else ("kneeling" if "idle" in st else "kneeling_down"))
                    elif fn == "lie_down":
                        st = str(beat.params.get("style", "sleep")).lower()
                        clip_name = "writhing_in_pain_laying_down" if ("writhe" in st or "pain" in st) else ("lying_down" if "rest" in st else "sleeping_idle")
                    elif fn == "interact_object":
                        act = str(beat.params.get("action", "push_button")).lower()
                        obj_map = {
                            "push_button": "button_pushing",
                            "button": "button_pushing",
                            "pull_lever": "pulling_lever",
                            "lever": "pulling_lever",
                            "enter_code": "entering_code",
                            "code": "entering_code",
                            "keypad": "entering_code",
                            "open_cabinet": "using_a_filing_cabinet",
                            "cabinet": "using_a_filing_cabinet",
                            "search_high": "searching_files_high",
                            "dig": "digging",
                            "type": "typing",
                            "write": "writing",
                        }
                        clip_name = obj_map.get(act, act if act in self.clips else "button_pushing")
                    elif fn == "throw_toward":
                        st = str(beat.params.get("style", "overhand")).lower()
                        clip_name = "toss_grenade" if ("grenade" in st or "toss" in st or "underhand" in st) else "throw_object"
                    elif fn == "inspect_area":
                        st = str(beat.params.get("style", "look_around")).lower()
                        if "nervous" in st:
                            clip_name = "nervously_look_around"
                        elif "down" in st:
                            clip_name = "looking_down"
                        elif "ground" in st or "find" in st or "search" in st:
                            clip_name = "finding_object_on_ground"
                        else:
                            clip_name = "looking_around"
                    elif fn == "use_phone":
                        mode = str(beat.params.get("mode", "talk")).lower()
                        clip_name = "talking_phone_pacing" if "pace" in mode else ("typing" if "text" in mode else "talking_on_a_cell_phone")
                    elif fn == "honk_horn":
                        clip_name = "honking_horn"
                    elif fn == "shake_hands":
                        clip_name = "shaking_hands_2"
                    elif fn == "hug_or_kiss":
                        st = str(beat.params.get("style", "kiss")).lower()
                        clip_name = "patting" if ("pat" in st or "comfort" in st or "hug" in st) else "kiss"
                    elif fn == "celebrate":
                        st = str(beat.params.get("style", "cheer")).lower()
                        cel_map = {
                            "cheer": "cheering",
                            "victory": "victory",
                            "clap": "standing_clap",
                            "rally": "rallying",
                            "fist_pump": "happy_hand_gesture",
                        }
                        clip_name = cel_map.get(st, "cheering")
                    elif fn == "emote":
                        em = str(beat.params.get("emotion", "shrug")).lower()
                        em_map = {
                            "cry": "crying",
                            "yell": "yelling_out",
                            "shout": "yelling_out",
                            "shrug": "shrugging",
                            "nod": "lengthy_head_nod",
                            "dismiss": "dismissing_gesture",
                            "bored": "bored",
                            "salute": "salute",
                            "threaten": "threatening",
                            "cocky": "being_cocky",
                            "sad": "sad_idle",
                            "laugh": "sitting_laughing",
                        }
                        clip_name = em_map.get(em, em if em in self.clips else "shrugging")
                    elif fn == "dance":
                        st = str(beat.params.get("style", "groove")).lower()
                        dance_map = {
                            "groove": "groving_to_music_energetically",
                            "silly": "silly_dancing",
                            "breakdance": "breakdance_freeze_var_3",
                            "moonwalk": "moonwalk",
                        }
                        clip_name = dance_map.get(st, "groving_to_music_energetically")
                    elif fn == "perform_activity":
                        act = str(beat.params.get("activity", "guitar")).lower()
                        act_map = {
                            "guitar": "guitar_playing",
                            "piano": "piano_playing",
                            "drums": "playing_drums",
                            "sing": "singing",
                            "rap": "rapping",
                            "basketball": "basketball_dribble",
                            "soccer_kick": "kick_soccerball",
                            "soccer": "kick_soccerball",
                            "jump_rope": "jumping_rope",
                            "fish": "fishing_cast",
                            "smoke": "smoking",
                            "cards": "cards",
                            "gaming": "gaming",
                        }
                        clip_name = act_map.get(act, act if act in self.clips else "guitar_playing")
                    elif fn == "defend_or_block":
                        st = str(beat.params.get("style", "block")).lower()
                        clip_name = "dodging_back" if "back" in st else ("dodging_right" if "right" in st or "dodge" in st else "boxing_block")
                    elif fn == "use_weapon":
                        wpn = str(beat.params.get("weapon", "pistol")).lower()
                        w_act = str(beat.params.get("action", "fire")).lower()
                        if "sword" in wpn:
                            clip_name = "withdrawing_sword" if "draw" in w_act else "stable_sword_outward_slash"
                        elif "knife" in wpn or "dagger" in wpn or "stab" in w_act:
                            clip_name = "sideways_stabbing"
                        elif "rifle" in wpn or "machine" in wpn:
                            clip_name = "machine_gun_gunplay"
                        else:
                            clip_name = "grabbing_pistol" if "draw" in w_act else "shooting_gun"
                    elif fn == "collapse_or_knockout":
                        st = str(beat.params.get("style", "knocked_out")).lower()
                        col_map = {
                            "knocked_out": "knocked_out",
                            "knocked_down": "knocked_down",
                            "dying": "dying_and_falling_backwards",
                            "sweep_fall": "sweep_fall",
                            "slip": "slipping",
                            "trip": "tripping",
                        }
                        clip_name = col_map.get(st, "knocked_out")
                    else:  # medical_aid
                        m_act = str(beat.params.get("action", "cpr")).lower()
                        clip_name = "receiving_cpr" if "receiv" in m_act else "administering_cpr"

                    if clip_name not in self.clips:
                        clip_name = "acknowledging" if "acknowledging" in self.clips else "reaction"

                    if fn in ("shake_hands", "hug_or_kiss", "medical_aid", "defend_or_block", "use_weapon", "collapse_or_knockout", "lie_down"):
                        t_target = str(beat.params.get("target") or beat.params.get("patient") or "")
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": t_target if t_target else None,
                            "t0": round(t_start, 3),
                            "t1": round(t_end + 1.0, 3),
                            "max_depth_m": 0.45,
                        })

                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    is_moonwalk = (clip_name == "moonwalk")
                    fwd_x, fwd_z = vector_from_heading(curr_heading)
                    end_pos = [curr_pos[0] - fwd_x * (0.9 * dur), curr_pos[1], curr_pos[2] - fwd_z * (0.9 * dur)] if is_moonwalk else list(curr_pos)

                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                        pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, curr_pos[1], pz],
                            heading_deg=curr_heading,
                            speed_mps=0.9 if is_moonwalk else 0.0,
                            action_clip=clip_name,
                            clip_phase=u,
                        ))
                    curr_pos = list(end_pos)

                elif fn in ("wait", "idle"):
                    perf.add_action("acknowledging", t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=list(curr_pos),
                            heading_deg=curr_heading,
                            action_clip="acknowledging",
                        ))

        return perf, None


if __name__ == "__main__":
    import os
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent
    app_bin = repo_root / "build" / "PainterSpace"
    env = dict(os.environ)
    qt_lib = "/Users/vishaljose/Qt/6.11.1/macos/lib"
    env["DYLD_FRAMEWORK_PATH"] = f"{qt_lib}:{env.get('DYLD_FRAMEWORK_PATH', '')}".strip(":")
    subprocess.run(["cmake", "--build", str(repo_root / "build")], cwd=str(repo_root), check=False)
    os.execve(str(app_bin), [str(app_bin)], env)

