"""Motion Synthesis engine for PainterSpace Shot Function Library.

Computes physical paths, obstacle-avoiding Catmull-Rom trajectories, speed-matched gaits,
vehicle dynamics (acceleration, cruise, braking, reverse, wheel spin, steering, pitch),
threat-vector evasive dodges, social formations, turn-taking gesture schedules,
multi-move combat combos, and parabolic jump arcs.
"""
from __future__ import annotations

import math
import heapq
import random
import zlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from crowd_spacing import DANCE_STYLE_CLIPS, defense_clip, reaction_clip, dialogue_clips
from plan_model import StructuredError
from semantic_references import (
    GAP_BETWEEN_BODIES,
    GAP_TO_VEHICLE,
    ReferenceResolver,
    ResolvedTarget,
    heading_from_vector,
    vector_from_heading,
)
from timeline_resolver import JUMP_TAKEOFF_S, ResolvedBeat

# Free space between conservative XZ footprints for ordinary vehicle travel.
VEHICLE_TRAVEL_CLEARANCE_M = 0.35
# Space for a walking actor's torso and animated limbs around solid props.
HUMAN_ROUTE_CLEARANCE_M = 0.65
# Extra exit clearance keeps the first walking root outside the routing boundary.
VEHICLE_EXIT_ROUTE_BUFFER_M = 0.15
# Polygon resolution for collision-safe visibility routes around circular footprints.
ROUTE_OBSTACLE_SIDES = 20
# Numeric tolerance for agreement between reserved and synthesized root endpoints.
ROUTE_ENDPOINT_TOLERANCE_M = 1e-4



def _stable_hash(text: str) -> int:
    """Process-independent string hash (Python's built-in hash() is salted per run)."""
    return zlib.crc32(text.encode("utf-8"))


def stair_route_geometry(stair_data: Dict[str, Any], direction: str = "up",
                         lane_offset_m: float = 0.0, exit_distance_m: float = 0.0
                         ) -> Tuple[List[List[float]], float, List[float]]:
    """Return the actual tread route and final position used by stair synthesis."""
    points = [list(point) for point in stair_data.get("centerline", [])]
    if direction == "down":
        points.reverse()
    if len(points) < 2:
        raise ValueError("A stair route requires at least two centerline points")
    dx, dz = points[-1][0] - points[0][0], points[-1][2] - points[0][2]
    direct_length = max(1e-6, math.hypot(dx, dz))
    if lane_offset_m:
        for point in points:
            point[0] += -dz / direct_length * lane_offset_m
            point[2] += dx / direct_length * lane_offset_m
    total_horizontal = max(0.1, sum(
        math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(points, points[1:])))
    arrival = [points[-1][0] + exit_distance_m * dx / total_horizontal,
               points[-1][1],
               points[-1][2] + exit_distance_m * dz / total_horizontal]
    return points, total_horizontal, arrival


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
    vehicle_supported: bool = False  # Seat/door motion can place the rig root below the road.


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
            # Route splitting can produce valid sub-millisecond segments and
            # very slow final steps. Rounding can invert their interval or
            # turn positive playback speed into zero before the bake.
            "start_s": float(t_start),
            "end_s": float(t_end),
            "speed": float(speed),
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
        solid_envelopes: Optional[Any] = None,
    ) -> None:
        self.manifest = manifest
        self.fps = fps
        self.clips = manifest.get("clips", {})
        self.assets = {a.get("id"): a for a in manifest.get("assets", [])}
        self.ref_resolver = ref_resolver
        self.scene_data = scene_data or {}
        self.solid_envelopes = solid_envelopes
        self._vehicle_routes = None

    def _solid_action_radius(self, actor_id: str, beat_id: str = "",
                             function: str = "") -> float:
        """Use measured full-body animation reach when available."""
        envelopes = self.solid_envelopes
        if envelopes is None or not envelopes.knows(actor_id):
            return HUMAN_ROUTE_CLEARANCE_M
        return max(HUMAN_ROUTE_CLEARANCE_M,
                   envelopes.solid_movement_radius(actor_id, function, beat_id)
                   + envelopes.buffer_m)

    def _arrival_action_radius(self, actor_id: str, movement_beat: ResolvedBeat,
                               schedule: List[ResolvedBeat],
                               include_next_movement: bool = False) -> float:
        """Reserve the movement pose and following stationary poses."""
        radius = self._solid_action_radius(actor_id, movement_beat.beat_id,
                                           movement_beat.function)
        moving_functions = {
            "walk_to", "run_to", "jog_to", "sneak_to", "approach", "flee_from",
            "follow", "disperse", "crawl_to", "stumble_to", "back_up_to",
            "carry_to", "swim_to", "fly_to", "take_cover", "jump_on",
            "jump_down", "jump_over", "climb_stairs", "climb_surface",
            "vault_over", "exit_vehicle", "enter_vehicle",
        }
        after_arrival = False
        for beat in schedule:
            if beat.beat_id == movement_beat.beat_id:
                after_arrival = True
                continue
            if not after_arrival:
                continue
            if beat.function in moving_functions:
                if include_next_movement:
                    radius = max(radius, self._solid_action_radius(
                        actor_id, beat.beat_id, beat.function))
                break
            radius = max(radius, self._solid_action_radius(actor_id, beat.beat_id,
                                                           beat.function))
        return radius

    def _moving_body_radius(self, asset_id: str) -> float:
        """Conservative XZ footprint radius from an asset's measured bounds."""
        bounds = self.assets.get(asset_id, {}).get("bounds_m", [2.0, 1.5, 4.5])
        return 0.5 * math.hypot(float(bounds[0]), float(bounds[2]))

    def _vehicle_neighbors(self, actor_id: str, other_performances, all_initial_states):
        """Include synthesized trajectories and as-yet-unsynthesized parked vehicles."""
        states = dict(all_initial_states or {})
        for other_id, performance in other_performances.items():
            states.setdefault(other_id, {"asset_id": performance.asset_id,
                                         "is_vehicle": performance.is_vehicle})
        for other_id, state in states.items():
            if other_id == actor_id:
                continue
            asset_id = str(state.get("asset_id", ""))
            asset = self.assets.get(asset_id, {})
            if not (state.get("is_vehicle") or "vehicle" in asset.get("tags", [])):
                continue
            performance = other_performances.get(other_id)
            yield other_id, asset_id, performance, state

    def _vehicle_clear_at(self, actor_id, asset_id, position, time_s,
                          other_performances, all_initial_states, margin_m):
        radius = self._moving_body_radius(asset_id)
        for other_id, other_asset, performance, state in self._vehicle_neighbors(
                actor_id, other_performances, all_initial_states):
            other_pos = (performance.sample(time_s)[0] if performance and performance.keyframes
                         else state.get("position", [0.0, 0.0, 0.0]))
            required = radius + self._moving_body_radius(other_asset) + margin_m
            if math.hypot(position[0] - other_pos[0], position[2] - other_pos[2]) < required:
                return False, other_id
        return True, None

    def _static_vehicle_route(self, start, goal, asset_id):
        from crowd.config import CrowdSolverConfig
        from vehicle_routes import VehicleRoutes
        # Standalone synthesis without a scene remains useful for unit/preview
        # work. Validated compiler runs already require scene collision coverage.
        if not self.scene_data.get('support_surfaces') and not self.scene_data.get('solid_boxes'):
            return [list(start), list(goal)], ''
        if self._vehicle_routes is None:
            config = getattr(self.ref_resolver, 'crowd_config', None) or CrowdSolverConfig()
            self._vehicle_routes = VehicleRoutes(self.scene_data, config)
        bounds = self.assets.get(asset_id, {}).get('bounds_m')
        height = float(bounds[1]) if bounds else math.inf
        return self._vehicle_routes.plan(tuple(start), tuple(goal), self._moving_body_radius(asset_id), height)

    def _resolve_vehicle_seat_anchor(
        self,
        vehicle_id: str,
        seat_name: str,
        other_performances: Dict[str, ActorPerformance],
        occupant_asset_id: Optional[str] = None,
        occupant_height_scale: float = 1.0,
    ) -> Tuple[List[float], float]:
        """Resolve a seat and keep the occupant's measured height below its cabin roof."""
        v_perf = other_performances.get(vehicle_id)
        v_asset_id = v_perf.asset_id if v_perf and v_perf.asset_id else ""
        v_asset = self.assets.get(v_asset_id)
        if not v_asset:
            v_asset = next(
                (a for a in self.manifest.get("assets", []) if a.get("id") in (v_asset_id, vehicle_id) or "car" in a.get("tags", [])),
                None,
            )

        def fit_roof(position: List[float]) -> List[float]:
            occupant = self.assets.get(occupant_asset_id) if occupant_asset_id else None
            # A generic fallback anchor may be useful for an unknown vehicle, but
            # its dimensions must not be used to alter another asset's seat.
            if (not v_asset or not occupant
                    or v_asset.get("id") != (v_asset_id or vehicle_id)):
                return position
            cabin = v_asset.get("cabin")
            cabin_max = cabin.get("max", []) if cabin else []
            roof_y = cabin_max[1] if len(cabin_max) > 1 else None
            if roof_y is None:
                bounds = v_asset.get("bounds_m")
                roof_y = bounds[1] if bounds and len(bounds) > 1 else None
            bounds_max = occupant.get("bounds_max")
            head_y = (bounds_max[1] if bounds_max and len(bounds_max) > 1
                      else occupant.get("collision", {}).get("character", {}).get("height_m"))
            if roof_y is None or head_y is None:
                return position
            fitted = list(position)
            fitted[1] = min(fitted[1], float(roof_y) - float(head_y) * occupant_height_scale)
            return fitted

        # 1. Author-defined anchor in asset metadata
        if v_asset and "anchors" in v_asset and seat_name in v_asset["anchors"]:
            adata = v_asset["anchors"][seat_name]
            return fit_roof(list(adata.get("pos", [0.0, 0.0, 0.0]))), float(adata.get("facing_deg", 0.0))

        # 2. Derive from steering wheel part if driver seat
        if v_asset and "parts" in v_asset and "driver" in seat_name.lower():
            sw = v_asset["parts"].get("steering_wheel")
            if sw and "hinge" in sw:
                hx, hy, hz = sw["hinge"]
                return fit_roof([hx, max(0.25, hy * 0.40), hz + 0.35]), 0.0

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

        return fit_roof([x_pos, y_pos, z_pos]), 0.0

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
        """Resolve a stair on a placed asset and convert its measurements to world space."""
        instance, separator, stair_id = stair_ref.partition(":")
        if not separator:
            instance, stair_id = "", stair_ref
        assets = self.manifest.get("assets", [])
        by_id = {asset.get("id"): asset for asset in assets}
        scene_objects = self.scene_data.get("objects", [])
        candidates = scene_objects if "objects" in self.scene_data else [
            {"name": asset.get("id"), "asset": asset.get("id")} for asset in assets]
        for obj in candidates:
            asset = by_id.get(obj.get("asset") or obj.get("asset_id"))
            if not asset:
                continue
            if instance and instance.lower() not in (
                    str(obj.get("name", "")).lower(), str(asset.get("id", "")).lower(),
                    str(asset.get("label", "")).lower()):
                continue
            for sid, authored in (obj.get("stairs") or asset.get("stairs", {})).items():
                if stair_id.lower() not in (sid.lower(), str(asset.get("id", "")).lower()):
                    continue
                data = dict(authored)
                matrix = obj.get("world_transform")
                if matrix and not authored.get("world_space"):
                    transform = np.array(matrix, dtype=float)

                    def world(point):
                        p = np.array([*point, 1.0], dtype=float) @ transform
                        return [float(v) for v in p[:3]]

                    local_line = authored.get("centerline", [])
                    if len(local_line) >= 2:
                        data["centerline"] = [world(p) for p in local_line]
                        first, last = local_line[0], local_line[-1]
                        dx, dz = last[0] - first[0], last[2] - first[2]
                        local_length = max(1e-6, math.hypot(dx, dz))
                        cross = [-dz / local_length, 0.0, dx / local_length]
                        edge = world([first[i] + cross[i] for i in range(3)])
                        origin = data["centerline"][0]
                        width_scale = math.hypot(edge[0] - origin[0], edge[2] - origin[2])
                        world_line = data["centerline"]
                        world_length = sum(math.hypot(b[0] - a[0], b[2] - a[2])
                                           for a, b in zip(world_line, world_line[1:]))
                        data["width_m"] = float(authored.get("width_m", 0.0)) * width_scale
                        depth_scale = world_length / local_length
                        data["treads"] = [{**t, "tread_m": float(t.get("tread_m", 0.3)) * depth_scale,
                                           "rise_m": float(t.get("rise_m", 0.2)) *
                                           abs(world_line[-1][1] - world_line[0][1]) /
                                           max(1e-6, abs(last[1] - first[1]))}
                                          for t in authored.get("treads", [])]
                return {"asset_id": asset.get("id", ""), "instance": obj.get("name"),
                        "stair_id": sid, "data": data, "levels": asset.get("levels", [])}
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
        radius = (self.solid_envelopes.solid_movement_radius(actor_id, beat.function, beat.beat_id)
                  if self.solid_envelopes is not None and self.solid_envelopes.knows(actor_id) else None)
        resolve_target = (resolver.resolve_facing if beat.function in ('turn_to_face', 'look_at')
                          else resolver.resolve)
        res, err = resolve_target(target_ref, live_states, t, source_actor_id=actor_id,
                                    clearance_radius_m=radius)
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
        actor_radius_m: float = HUMAN_ROUTE_CLEARANCE_M,
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
                "radius": radius + actor_radius_m,
            })

        # 2. Other vehicles / actors present at t_mid
        for oid, operf in other_performances.items():
            if oid == actor_id or not operf.keyframes:
                continue
            opos, _ = operf.sample(t_mid)
            if operf.is_vehicle:
                radius = self._moving_body_radius(operf.asset_id) + actor_radius_m
            else:
                radius = actor_radius_m
            # An actor may intentionally begin or finish a vehicle interaction
            # within its footprint. Such a route needs a separate contact beat.
            if math.hypot(start_pos[0] - opos[0], start_pos[2] - opos[2]) < radius:
                continue
            if math.hypot(end_pos[0] - opos[0], end_pos[2] - opos[2]) < radius:
                continue
            obstacles.append({
                "cx": float(opos[0]),
                "cz": float(opos[2]),
                "radius": radius,
            })

        return obstacles

    def _spaced_actor_destination(
        self,
        actor_id: str,
        target_id: str,
        target_pos: List[float],
        start_pos: List[float],
        arrival_time: float,
        other_performances: Dict[str, ActorPerformance],
        all_initial_states: Optional[Dict[str, Dict[str, Any]]],
        scene_data: Optional[Dict[str, Any]],
        actor_radius_m: float = HUMAN_ROUTE_CLEARANCE_M,
    ) -> Optional[List[float]]:
        """Reserve a supported arrival position near an actor without stacking arrivals."""
        from support_surfaces import (ACTOR_SUPPORT_MARGIN_M, STANDING_SPOT_SPACING_M,
                                      SUPPORT_LEVEL_TOLERANCE_M, contains)

        target_perf = other_performances.get(target_id)
        if target_perf and target_perf.keyframes:
            target_pos, _ = target_perf.sample(arrival_time)
        occupied = []
        for other_id, state in (all_initial_states or {}).items():
            if other_id in (actor_id, target_id):
                continue
            performance = other_performances.get(other_id)
            position = (performance.sample(arrival_time)[0]
                        if performance and performance.keyframes else state.get("position"))
            if position and abs(float(position[1]) - float(target_pos[1])) <= SUPPORT_LEVEL_TOLERANCE_M:
                occupied.append(position)
        for other_id, performance in other_performances.items():
            if other_id in (actor_id, target_id) or other_id in (all_initial_states or {}):
                continue
            if performance.keyframes:
                position, _ = performance.sample(arrival_time)
                if abs(position[1] - target_pos[1]) <= SUPPORT_LEVEL_TOLERANCE_M:
                    occupied.append(position)
        vehicle_exclusions = [
            (performance.sample(arrival_time)[0],
             self._moving_body_radius(performance.asset_id) + actor_radius_m)
            for performance in other_performances.values()
            if performance.is_vehicle and performance.keyframes
        ]

        surfaces = [surface for surface in (scene_data or self.scene_data).get("support_surfaces", [])
                    if abs(float(surface["height_m"]) - float(target_pos[1]))
                    <= SUPPORT_LEVEL_TOLERANCE_M]
        dx, dz = start_pos[0] - target_pos[0], start_pos[2] - target_pos[2]
        base_angle = math.atan2(dz, dx)
        clearance = STANDING_SPOT_SPACING_M
        best = None
        # Each additional ring can hold more actors. The number of rings grows
        # from the number of potential arrivals, not from a particular shot.
        required_rings = max(
            (math.ceil((math.hypot(target_pos[0] - position[0],
                                   target_pos[2] - position[2]) + radius_m)
                       / clearance)
             for position, radius_m in vehicle_exclusions),
            default=1,
        )
        for ring in range(1, max(len(occupied) + 2, required_rings + 1)):
            radius = clearance * ring
            slots = max(8, math.ceil(2.0 * math.pi * radius / clearance))
            candidates = []
            for index in range(slots):
                angle = base_angle + 2.0 * math.pi * index / slots
                candidate = [target_pos[0] + radius * math.cos(angle),
                             target_pos[1],
                             target_pos[2] + radius * math.sin(angle)]
                if surfaces and not any(contains(surface, candidate, ACTOR_SUPPORT_MARGIN_M)
                                        for surface in surfaces):
                    continue
                if any(math.hypot(candidate[0] - other[0], candidate[2] - other[2])
                       < clearance for other in occupied):
                    continue
                if any(math.hypot(candidate[0] - position[0], candidate[2] - position[2]) < radius_m
                       for position, radius_m in vehicle_exclusions):
                    continue
                candidates.append(candidate)
            if candidates:
                best = min(candidates, key=lambda point: math.hypot(
                    point[0] - start_pos[0], point[2] - start_pos[2]))
                break
        return best

    def _plan_smooth_path(
        self,
        start_pos: List[float],
        end_pos: List[float],
        obstacles: List[Dict[str, float]],
        n_samples: int = 32,
    ) -> List[List[float]]:
        """Find a collision-safe visibility route around circular XZ footprints."""
        dx = end_pos[0] - start_pos[0]
        dz = end_pos[2] - start_pos[2]
        seg_len = math.hypot(dx, dz)
        if seg_len < 0.25 or not obstacles:
            return [list(start_pos), list(end_pos)]

        ux = dx / seg_len
        uz = dz / seg_len
        def segment_distance(a, b, cx, cz):
            vx, vz = b[0] - a[0], b[2] - a[2]
            along = max(0.0, min(1.0, ((cx - a[0]) * vx + (cz - a[2]) * vz)
                                      / max(1e-12, vx * vx + vz * vz)))
            return math.hypot(a[0] + along * vx - cx, a[2] + along * vz - cz)

        blocking = []
        for obs in obstacles:
            if segment_distance(start_pos, end_pos, obs["cx"], obs["cz"]) < obs["radius"]:
                blocking.append(obs)

        if not blocking:
            return [list(start_pos), list(end_pos)]

        # Include nearby circles that a detour could encounter. The radius of
        # blocking circles determines the local search corridor.
        reach = 2.0 * max(obs["radius"] for obs in blocking)
        nearby = [obs for obs in obstacles if segment_distance(
            start_pos, end_pos, obs["cx"], obs["cz"]) <= obs["radius"] + reach]
        nodes = [list(start_pos), list(end_pos)]
        for obs in nearby:
            # Expand the vertex ring so its straight chords stay outside the
            # original circle, including between adjacent waypoints.
            ring_radius = (obs["radius"] + 0.05) / math.cos(math.pi / ROUTE_OBSTACLE_SIDES)
            for index in range(ROUTE_OBSTACLE_SIDES):
                angle = 2.0 * math.pi * index / ROUTE_OBSTACLE_SIDES
                x = obs["cx"] + ring_radius * math.cos(angle)
                z = obs["cz"] + ring_radius * math.sin(angle)
                if any(math.hypot(x - other["cx"], z - other["cz"]) < other["radius"]
                       for other in nearby if other is not obs):
                    continue
                projection = max(0.0, min(1.0, ((x - start_pos[0]) * ux + (z - start_pos[2]) * uz) / seg_len))
                nodes.append([x, start_pos[1] + (end_pos[1] - start_pos[1]) * projection, z])

        def visible(a, b):
            return all(segment_distance(a, b, obs["cx"], obs["cz"]) >= obs["radius"]
                       for obs in nearby)

        distances = [math.inf] * len(nodes)
        parents = [-1] * len(nodes)
        distances[0] = 0.0
        frontier = [(0.0, 0)]
        visited = set()
        while frontier:
            distance, index = heapq.heappop(frontier)
            if index in visited:
                continue
            if index == 1:
                break
            visited.add(index)
            for next_index in range(1, len(nodes)):
                if next_index in visited or next_index == index or not visible(nodes[index], nodes[next_index]):
                    continue
                edge = math.hypot(nodes[index][0] - nodes[next_index][0],
                                  nodes[index][2] - nodes[next_index][2])
                candidate = distance + edge
                if candidate < distances[next_index]:
                    distances[next_index] = candidate
                    parents[next_index] = index
                    heapq.heappush(frontier, (candidate, next_index))
        if not math.isfinite(distances[1]):
            return []
        route = []
        index = 1
        while index != -1:
            route.append(nodes[index])
            index = parents[index]
        route.reverse()
        if any(segment_distance(a, b, obs["cx"], obs["cz"]) < obs["radius"]
               for a, b in zip(route, route[1:]) for obs in obstacles):
            return []
        return route

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
        stop_at_time: Optional[float] = None,
    ) -> Tuple[Optional[ActorPerformance], Optional[StructuredError]]:
        """Synthesize continuous keyframes and performance clips for one actor across all beats."""
        rng = random.Random(seed + _stable_hash(actor_id) % 1000)
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
        appearance = initial_state.get("appearance")
        occupant_height_scale = float(
            appearance.get("height_scale", 1.0) if isinstance(appearance, dict)
            else getattr(appearance, "height_scale", 1.0))
        seat_anchor_name = initial_state.get("seat_anchor", "driver_seat") or "driver_seat"
        at_ref = initial_state.get("at_reference", "")
        if not seated_vehicle and at_ref and ":" in at_ref:
            ref_obj, ref_sub = at_ref.split(":", 1)
            if "seat" in ref_sub.lower() or ref_obj in other_performances:
                seated_vehicle = ref_obj
                seat_anchor_name = ref_sub

        if not is_vehicle and seated_vehicle and seated_vehicle in other_performances:
            local_seat, local_facing = self._resolve_vehicle_seat_anchor(
                seated_vehicle, seat_anchor_name, other_performances,
                initial_state.get("asset_id"), occupant_height_scale)
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
            # Planning can request a physical prefix while keeping the full
            # schedule available for animation-reach reservations at its end.
            if stop_at_time is not None and beat.start_time >= stop_at_time - 1e-9:
                break
            fn = beat.function
            t_start = beat.start_time
            t_end = beat.end_time
            dur = max(0.01, t_end - t_start)
            target_ref = beat.params.get("target") or beat.params.get("to")
            # Older plans represented boarding as sit_in. Only an occupant
            # already coupled to this vehicle may use the seated hold branch;
            # an outside actor must pass through the entry transition.
            if fn == "sit_in":
                host = beat.params.get("vehicle")
                if not host and isinstance(target_ref, str) and ":" in target_ref:
                    host = target_ref.split(":", 1)[0]
                if (host in other_performances and other_performances[host].is_vehicle
                        and seated_vehicle != host):
                    fn = "enter_vehicle"
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
            paired_positions = beat.params.get("interaction_positions") or {}
            paired_position = ((paired_positions.get(target_ref) if isinstance(target_ref, str) else None)
                               or beat.params.get("paired_target_position"))
            if paired_position is not None and target_ref not in other_performances:
                target = ResolvedTarget(
                    position=np.array(paired_position, dtype=float),
                    facing_deg=0.0,
                    clearance_radius=0.35,
                    target_type="actor",
                    target_name=str(target_ref),
                )

            if beat.params.get("variant"):
                from director_brief import ACTION_SPEEDS
                if fn not in ACTION_SPEEDS and fn != "attack":
                    beat.params["in_place_clip"] = beat.params["variant"]
                beat.params["clip"] = beat.params["variant"]
                if fn == "attack":
                    beat.params["moves"] = [beat.params["variant"]]
            if beat.params.get("in_place_clip"):
                clip = beat.params["in_place_clip"]
                if clip not in self.clips:
                    return None, StructuredError("BRIEF_CLIP", f"Missing clip {clip}", beat_id=beat.beat_id)
                perf.add_action(clip, t_start, t_end, fps=self.fps)
                for i in range(math.ceil(dur*self.fps)+1):
                    perf.keyframes.append(Keyframe(min(t_end,t_start+i/self.fps), list(curr_pos), curr_heading, action_clip=clip))
                continue
            # A manifest clip is an admissible timeline action, with no free-text fallback.
            if fn in self.clips and not beat.params.get("brief_path"):
                perf.add_action(fn, t_start, t_end, fps=self.fps, blend_s=min(.25, dur))
                for i in range(max(1, math.ceil(dur*self.fps))+1):
                    t = min(t_end, t_start+i/self.fps)
                    perf.keyframes.append(Keyframe(t, list(curr_pos), curr_heading, action_clip=fn))
                continue

            # User geometry is resolved here, never by the planner. Exact path windows
            # bypass autonomous routing so a compiler cannot silently redraw intent.
            if beat.params.get("brief_path"):
                from director_brief import resolve_path_for_actor, ACTION_SPEEDS, path_metrics
                path = dict(beat.params["brief_path"])
                if (beat.params.get('auto_crowd_movement') and path.get('points')
                        and math.dist(curr_pos, path['points'][0]) > ROUTE_ENDPOINT_TOLERANCE_M):
                    return None, StructuredError(
                        'PLANNED_ROUTE_DISCONTINUITY',
                        'The reserved route does not start at the synthesized actor position.',
                        beat_id=beat.beat_id, actor_id=actor_id,
                        details={'actual_start_m': list(curr_pos),
                                 'planned_start_m': list(path['points'][0])})
                action = beat.params.get("action", fn)
                gait = ACTION_SPEEDS.get(action, (0, 0, "walk"))[2]
                clip = beat.params.get("variant") or ("run" if gait in ("jog", "sprint") else gait)
                jump_spans = []
                if not is_vehicle:
                    from support_surfaces import project_drawn_route
                    path["points"], jump_spans = project_drawn_route(
                        path["points"], self.scene_data.get("support_surfaces", []),
                        float(curr_pos[1]), self.scene_data.get("solid_boxes", []))
                length = path_metrics(path)["length_m"]
                stationary = length == 0.0
                if stationary:
                    # A destination can already be reached after allocation or
                    # replanning. Keep its hold and completion event, using the
                    # same resting clip as wait rather than a zero-speed gait.
                    clip = 'acknowledging'
                if not is_vehicle and clip not in self.clips:
                    return None, StructuredError("BRIEF_CLIP", f"Missing clip {clip}", beat_id=beat.beat_id)
                speed = length / dur
                nominal = {"walk": 1.4, "run": 4.2, "jog": 2.6, "sprint": 6.0}.get(gait, speed or 1)
                samples = resolve_path_for_actor(path, (t_start, t_end))
                jump_windows = []
                if jump_spans:
                    # The route sampler includes every vertex at its traversal
                    # time, even with easing. Match vertices in order so clip
                    # windows follow the actual speed profile.
                    vertex_times = []
                    sample_index = 0
                    for vertex in path["points"]:
                        while sample_index < len(samples) - 1 and math.dist(samples[sample_index][1], vertex) > 1e-5:
                            sample_index += 1
                        vertex_times.append(samples[sample_index][0])
                    jump_windows = [(vertex_times[a], vertex_times[b]) for a, b in jump_spans]
                if not is_vehicle:
                    cursor = t_start
                    for jump_start, jump_end in jump_windows:
                        if jump_start > cursor + 1e-6:
                            perf.add_action(clip, cursor, jump_start, fps=self.fps,
                                            speed=1.0 if stationary else speed/nominal,
                                            blend_s=min(.25, jump_start - cursor))
                        perf.add_action("jump", jump_start, jump_end, fps=self.fps,
                                        blend_s=min(.1, jump_end - jump_start))
                        cursor = jump_end
                    if t_end > cursor + 1e-6 or not jump_windows:
                        perf.add_action(clip, cursor, t_end, fps=self.fps,
                                        speed=1.0 if stationary else speed/nominal,
                                        blend_s=min(.25, t_end - cursor))
                if not is_vehicle and not stationary and path.get("speed_profile") == "ease_in_out" and not jump_windows:
                    low, high = path.get("speed_band", [0, speed*1.5])
                    strength = max(0., min(.5, 1-low/max(1e-9,speed), high/max(1e-9,speed)-1))
                    perf.actions[-1]["speed_keys"] = [[t-t_start, speed*(1-strength*math.cos(2*math.pi*(t-t_start)/dur))/nominal] for t, _, _ in samples]
                # Replace the preceding endpoint to make the new action own the boundary.
                perf.keyframes = [k for k in perf.keyframes if k.time_s < t_start]
                last_pos, last_heading, last_time = samples[0][1], samples[0][2], samples[0][0]
                for t, pos, heading in samples:
                    if stationary:
                        heading = curr_heading
                    step = math.dist(last_pos, pos)
                    current_speed = step/max(1e-9, t-last_time) if t > last_time else speed
                    steering = 0.
                    if is_vehicle:
                        curr_wheel_spin += math.degrees(step/wheel_radius)
                        delta = math.radians((heading-last_heading+180)%360-180)
                        steering = math.degrees(math.atan(wheelbase*delta/max(1e-6,step))) if step > 0 else 0.
                    sample_clip = ("jump" if any(a - 1e-6 <= t <= b + 1e-6
                                                 for a, b in jump_windows) else clip)
                    perf.keyframes.append(Keyframe(t, pos, heading, speed_mps=current_speed, action_clip=sample_clip,
                                                  wheel_spin_deg=curr_wheel_spin, steering_deg=steering))
                    last_pos, last_heading, last_time = pos, heading, t
                curr_pos = list(samples[-1][1])
                if not stationary:
                    curr_heading = samples[-1][2]
                continue

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
                                gap = (ref_resolver or self.ref_resolver or ReferenceResolver(self.manifest)).resolve_gap(
                                    beat.params.get("gap", "near"), is_vehicle=True)
                                target_state = (all_initial_states or {}).get(str(target_ref), {})
                                target_perf = other_performances.get(str(target_ref))
                                target_asset = target_state.get("asset_id") or (target_perf.asset_id if target_perf else "")
                                target_radius = (self._moving_body_radius(target_asset)
                                                 if target_asset else float(target.clearance_radius))
                                gap += self._moving_body_radius(perf.asset_id) + target_radius
                                dir_vec = np.array(end_pos) - np.array(curr_pos)
                                dir_len = np.linalg.norm(dir_vec[[0, 2]])
                                if dir_len > 1e-3:
                                    end_pos = list(np.array(curr_pos) + (dir_vec / dir_len) * max(0.0, dir_len - gap))
                            elif mode == "near_miss":
                                end_pos[0] += 0.8
                            elif mode == "collide":
                                pass
                    else:
                        fwd_x, fwd_z = vector_from_heading(curr_heading)
                        sign_dir = -1.0 if is_reverse else 1.0
                        dist = dur * (3.0 if is_reverse else 6.0)
                        end_pos = [curr_pos[0] + sign_dir * fwd_x * dist, curr_pos[1], curr_pos[2] + sign_dir * fwd_z * dist]

                    route, route_error = self._static_vehicle_route(curr_pos, end_pos, perf.asset_id)
                    if route is None:
                        return None, StructuredError(
                            'VEHICLE_ROUTE_LIMIT',
                            f"No supported, clear vehicle route for '{actor_id}': {route_error}.",
                            beat_id=beat.beat_id, actor_id=actor_id,
                            details={'reason': route_error, 'start': list(curr_pos), 'destination': end_pos},
                            suggestions=['Choose a reachable destination or adjust placement within the requested region.'])
                    from vehicle_routes import prefix_route
                    from director_brief import resolve_path_for_actor
                    dist_to_travel = sum(math.dist(a, b) for a, b in zip(route, route[1:]))
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

                    from crowd.config import CrowdSolverConfig
                    from crowd.st_planner import Reservation, blocked_intervals
                    clearance_config = replace(
                        getattr(self.ref_resolver, 'crowd_config', None) or CrowdSolverConfig(),
                        buffer_m=VEHICLE_TRAVEL_CLEARANCE_M)
                    neighbors = [Reservation(other_id,
                        [(k.time_s, *k.position) for k in performance.keyframes]
                        if performance and performance.keyframes else [(t_start, *state['position'])],
                        self._moving_body_radius(other_asset))
                        for other_id, other_asset, performance, state in self._vehicle_neighbors(
                            actor_id, other_performances, all_initial_states)]

                    def trajectory_clear(scale):
                        # Test the actual piecewise-linear motion continuously;
                        # fast crossings between frames must not slip through.
                        samples = resolve_path_for_actor(
                            {'points': prefix_route(route, scale), 'speed_profile': 'ease_in_out'},
                            (t_start, t_end), self.fps * 2)
                        for (ta, a, _), (tb, b, _) in zip(samples, samples[1:]):
                            if blocked_intervals(a, b, ta, tb, self._moving_body_radius(perf.asset_id),
                                                 neighbors, clearance_config):
                                return False, 'vehicle_trajectory'
                        return True, None

                    travel_scale = 1.0
                    if mode != "collide" and not trajectory_clear(1.0)[0]:
                        if not trajectory_clear(0.0)[0]:
                            return None, StructuredError(
                                "MOVING_OBJECT_CLEARANCE",
                                f"'{actor_id}' has no collision-free waiting position during this move.",
                                beat_id=beat.beat_id, actor_id=actor_id,
                                suggestions=["Increase starting separation or adjust the routes."])
                        low, high = 0.0, 1.0
                        for _ in range(18):
                            middle = (low + high) * 0.5
                            if trajectory_clear(middle)[0]:
                                low = middle
                            else:
                                high = middle
                        travel_scale = low
                        if fn not in ('drive_toward', 'brake_to_stop'):
                            return None, StructuredError(
                                'VEHICLE_DESTINATION_CLEARANCE',
                                f"'{actor_id}' cannot reach the requested destination without crossing another vehicle.",
                                beat_id=beat.beat_id, actor_id=actor_id,
                                details={'destination': end_pos, 'reachable_fraction': travel_scale},
                                suggestions=['Adjust arrival spacing or sequence the vehicle movements.'])

                    last_frame_pos = list(curr_pos)

                    actual_route = prefix_route(route, travel_scale)
                    samples = resolve_path_for_actor(
                        {'points': actual_route, 'speed_profile': 'ease_in_out'}, (t_start, t_end), self.fps * 2)
                    last_frame_time = t_start
                    for frame_idx, (f_time, pos, segment_heading) in enumerate(samples):
                        u = (f_time - t_start) / dur
                        travel_heading = (curr_heading if dist_to_travel * travel_scale <= 1e-9 else
                                          (segment_heading + 180.) % 360. if is_reverse else segment_heading)
                        delta_dist = math.hypot(pos[0] - last_frame_pos[0], pos[2] - last_frame_pos[2])
                        elapsed = f_time - last_frame_time
                        v_inst = delta_dist / elapsed if elapsed > 0 else 0.0
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

                        last_frame_pos = pos
                        last_frame_time = f_time

                    curr_pos = last_frame_pos
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

                    local_seat, local_facing = self._resolve_vehicle_seat_anchor(
                        seated_vehicle, seat_anchor_name, other_performances,
                        initial_state.get("asset_id"), occupant_height_scale)

                    if fn == "honk_horn":
                        clip_name = "honking_horn" if "honking_horn" in self.clips else "sitting"
                    elif fn == "use_phone":
                        clip_name = "talking_on_a_cell_phone" if "talking_on_a_cell_phone" in self.clips else "sitting"
                    elif fn in ("drive", "sit_in", "sit_on", "wait", "idle"):
                        clip_name = "driving" if fn == "drive" and "driving" in self.clips else "sitting"
                    elif fn in ("react", "flinch"):
                        clip_name = "reaction"
                    elif fn in ("converse", "argue", "gesture", "greet", "emote"):
                        clip_name = "acknowledging"
                    else:
                        clip_name = "sitting"

                    if seated_vehicle and seated_vehicle in other_performances:
                        v_perf = other_performances[seated_vehicle]
                        perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": seated_vehicle,
                            "is_occupant": True,
                            "t0": float(t_start),
                            "t1": float(t_end),
                            "max_depth_m": 0.45,
                        })

                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + dur * frame_idx / n_frames
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
                                vehicle_supported=True,
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
                    if v_asset_id:
                        # Authored door anchors can be suitable for a static rig yet
                        # leave animated limbs inside the body after disembarking.
                        safe_radius = (self._moving_body_radius(v_asset_id)
                                       + self._arrival_action_radius(
                                           actor_id, beat, sorted_beats,
                                           include_next_movement=True)
                                       + VEHICLE_EXIT_ROUTE_BUFFER_M)
                        radial = math.hypot(local_stand[0], local_stand[2])
                        if radial < safe_radius:
                            if radial < 1e-6:
                                side = -1.0 if "_l" in door_part else 1.0
                                local_stand[0], local_stand[2] = side, 0.0
                                radial = 1.0
                            local_stand[0] *= safe_radius / radial
                            local_stand[2] *= safe_radius / radial

                    # Interpolate in the vehicle's local frame. A car may still
                    # be moving or turning during the exit; a world-space path
                    # frozen at t_start can put the actor back through its body.
                    theta = math.radians(v_heading)
                    offset_x = curr_pos[0] - v_pos[0]
                    offset_z = curr_pos[2] - v_pos[2]
                    start_local_x = offset_x * math.cos(theta) + offset_z * math.sin(theta)
                    start_local_z = -offset_x * math.sin(theta) + offset_z * math.cos(theta)
                    start_local_y = curr_pos[1] - v_pos[1]
                    start_local_heading = (curr_heading - v_heading) % 360.0
                    if v_id and v_id in other_performances:
                        end_vehicle_pos, end_vehicle_heading = other_performances[v_id].sample(t_end)
                    else:
                        end_vehicle_pos, end_vehicle_heading = v_pos, v_heading
                    end_theta = math.radians(end_vehicle_heading)
                    exit_world_pos = [
                        end_vehicle_pos[0] + local_stand[0] * math.cos(end_theta)
                        - local_stand[2] * math.sin(end_theta),
                        end_vehicle_pos[1] + local_stand[1],
                        end_vehicle_pos[2] + local_stand[0] * math.sin(end_theta)
                        + local_stand[2] * math.cos(end_theta),
                    ]
                    exit_world_heading = (end_vehicle_heading + local_stand_facing) % 360.0

                    # Keep the seated pose while the root clears the cabin.
                    # Start the standing action only after reaching free space.
                    clear_time = t_start + 0.5 * dur
                    seated_clip = next((name for name in ("sitting", "driving", "male_sitting_pose")
                                        if name in self.clips), "opening_door_inwards")
                    standing_clip = next((name for name in ("standing_up", "exiting_car", "opening_door_inwards")
                                          if name in self.clips), seated_clip)
                    perf.add_action(seated_clip, t_start, clear_time, fps=self.fps, speed=1.0, blend_s=0.0)
                    perf.add_action(standing_clip, clear_time, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    if v_id:
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": v_id,
                            "is_occupant": bool(v_id and v_id in other_performances),
                            "t0": float(t_start),
                            "t1": float(clear_time),
                            "max_depth_m": 0.50,
                        })

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + dur * frame_idx / n_frames
                        u = frame_idx / float(n_frames)
                        # Seated translation first, standing rise after clearance.
                        travel_u = min(1.0, u * 2.0)
                        rise_u = max(0.0, u * 2.0 - 1.0)
                        travel_s = 3.0 * travel_u ** 2 - 2.0 * travel_u ** 3
                        rise_s = 3.0 * rise_u ** 2 - 2.0 * rise_u ** 3
                        if v_id and v_id in other_performances:
                            frame_vehicle_pos, frame_vehicle_heading = other_performances[v_id].sample(f_time)
                        else:
                            frame_vehicle_pos, frame_vehicle_heading = v_pos, v_heading
                        frame_theta = math.radians(frame_vehicle_heading)
                        local_x = start_local_x + (local_stand[0] - start_local_x) * travel_s
                        local_z = start_local_z + (local_stand[2] - start_local_z) * travel_s
                        local_y = start_local_y + (local_stand[1] - start_local_y) * rise_s
                        px = (frame_vehicle_pos[0] + local_x * math.cos(frame_theta)
                              - local_z * math.sin(frame_theta))
                        py = frame_vehicle_pos[1] + local_y
                        pz = (frame_vehicle_pos[2] + local_x * math.sin(frame_theta)
                              + local_z * math.cos(frame_theta))
                        diff = (local_stand_facing - start_local_heading + 180.0) % 360.0 - 180.0
                        h = (frame_vehicle_heading + start_local_heading + travel_s * diff) % 360.0
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, py, pz],
                            heading_deg=h,
                            speed_mps=0.8,
                            action_clip=seated_clip if u < 0.5 else standing_clip,
                            vehicle_supported=bool(v_id and v_id in other_performances),
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

                    seat_anchor_name = beat.params.get("seat") or (
                        str(target_ref).split(":", 1)[1]
                        if target_ref and ":" in str(target_ref) else "driver_seat")
                    local_seat, local_facing = self._resolve_vehicle_seat_anchor(
                        v_id, seat_anchor_name, other_performances,
                        initial_state.get("asset_id"), occupant_height_scale)

                    if v_id and v_id in other_performances:
                        v_perf = other_performances[v_id]
                        v_pos, v_heading = v_perf.sample(t_end)
                        theta = math.radians(v_heading)
                        rot_x = local_seat[0] * math.cos(theta) - local_seat[2] * math.sin(theta)
                        rot_z = local_seat[0] * math.sin(theta) + local_seat[2] * math.cos(theta)
                        seat_world_pos = [v_pos[0] + rot_x, v_pos[1] + local_seat[1], v_pos[2] + rot_z]
                        seat_world_heading = (v_heading + local_facing) % 360.0

                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": v_id,
                            "is_occupant": True,
                            "t0": float(t_start),
                            "t1": float(t_end),
                            "max_depth_m": 1.0,
                        })

                        clip_name = "crouch_to_stand"
                        seated_clip = next((name for name in ("sitting", "driving", "male_sitting_pose")
                                            if name in self.clips), clip_name)
                        # Finish changing pose outside the cabin. Keeping a
                        # standing clip until arrival makes the next action
                        # blend that standing pose through the vehicle roof.
                        lowered_time = t_start + 0.5 * dur
                        perf.add_action(clip_name, t_start, lowered_time, fps=self.fps, speed=1.0, blend_s=0.2)
                        perf.add_action(seated_clip, lowered_time, t_end, fps=self.fps, speed=1.0, blend_s=0.0)
                        n_frames = max(1, int(round(dur * self.fps)))
                        for frame_idx in range(n_frames + 1):
                            f_time = t_start + dur * frame_idx / n_frames
                            u = frame_idx / float(n_frames)
                            # Crouch outside the car before moving under its roof.
                            lower_u = min(1.0, u * 2.0)
                            travel_u = max(0.0, u * 2.0 - 1.0)
                            lower_s = 3.0 * lower_u ** 2 - 2.0 * lower_u ** 3
                            travel_s = 3.0 * travel_u ** 2 - 2.0 * travel_u ** 3
                            px = curr_pos[0] + (seat_world_pos[0] - curr_pos[0]) * travel_s
                            py = curr_pos[1] + (seat_world_pos[1] - curr_pos[1]) * lower_s
                            pz = curr_pos[2] + (seat_world_pos[2] - curr_pos[2]) * travel_s
                            diff = (seat_world_heading - curr_heading + 180.0) % 360.0 - 180.0
                            h = (curr_heading + travel_s * diff) % 360.0
                            perf.keyframes.append(Keyframe(
                                time_s=f_time,
                                position=[px, py, pz],
                                heading_deg=h,
                                speed_mps=0.8,
                                action_clip=seated_clip if u >= 0.5 else clip_name,
                                vehicle_supported=True,
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

                    if (target and target.target_type == "actor"
                            and fn in ("walk_to", "run_to", "jog_to", "sneak_to", "approach")
                            and target.target_name in other_performances):
                        # A moving target is a rendezvous at arrival time, not
                        # a snapshot of its old placement at departure time.
                        arrival_pos, arrival_heading = other_performances[target.target_name].sample(t_end)
                        target = replace(target, position=np.array(arrival_pos, dtype=float),
                                         facing_deg=arrival_heading)

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
                            angle = (_stable_hash(actor_id) % 360) * (math.pi / 180.0)
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
                        elif target.target_type == "actor" and fn in (
                                "walk_to", "run_to", "jog_to", "sneak_to"):
                            target_id = str(target.target_name)
                            destination = self._spaced_actor_destination(
                                actor_id, target_id, end_pos, curr_pos, t_end,
                                other_performances, all_initial_states, scene_data,
                                actor_radius_m=self._arrival_action_radius(
                                    actor_id, beat, sorted_beats))
                            if destination is None:
                                return None, StructuredError(
                                    "ACTOR_APPROACH_OCCUPIED",
                                    f"No clear supported arrival position near '{target_id}'.",
                                    beat_id=beat.beat_id,
                                    actor_id=actor_id,
                                    suggestions=["Choose a less crowded target or stagger the arrivals."],
                                )
                            end_pos = destination
                            target_facing = heading_from_vector(
                                target.position[0] - end_pos[0],
                                target.position[2] - end_pos[2])
                        elif fn == "take_cover":
                            # Step behind cover object relative to threat
                            cover_gap = 0.85
                            dir_vec = np.array(end_pos) - np.array(curr_pos)
                            dir_len = np.linalg.norm(dir_vec[[0, 2]])
                            if dir_len > 1e-3:
                                end_pos = list(np.array(end_pos) - (dir_vec / dir_len) * cover_gap)

                    if beat.params.get("exact_window") and target is None and fn in ("walk_to", "run_to", "jog_to"):
                        dx, dz = vector_from_heading(curr_heading)
                        end_pos = [curr_pos[0]+dx*nominal_clip_speed*dur, curr_pos[1], curr_pos[2]+dz*nominal_clip_speed*dur]

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
                        t_start, t_end, actor_id, curr_pos, end_pos, other_performances,
                        scene_data, actor_radius_m=self._solid_action_radius(
                            actor_id, beat.beat_id, fn),
                    )
                    polyline = self._plan_smooth_path(curr_pos, end_pos, obstacles)
                    if not polyline:
                        return None, StructuredError(
                            "ROUTE_CLEARANCE_FAILED",
                            f"No clear route for '{actor_id}' around the current obstacles.",
                            beat_id=beat.beat_id, actor_id=actor_id,
                            suggestions=["Move the start or destination clear of nearby objects."])

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

                    first_motion_frame = len(perf.keyframes)
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

                        # Keep the body aligned with travel while moving. A later
                        # stationary beat may turn toward the target on arrival.
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

                    for frame_index in range(first_motion_frame, len(perf.keyframes) - 1):
                        here, following = perf.keyframes[frame_index:frame_index + 2]
                        dx = following.position[0] - here.position[0]
                        dz = following.position[2] - here.position[2]
                        if math.hypot(dx, dz) > 1e-6:
                            travel = heading_from_vector(dx, dz)
                            here.heading_deg = (travel + 180.0) % 360.0 if is_backward_motion else travel

                    curr_pos = list(end_pos)
                    curr_heading = heading

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
                        return None, StructuredError(
                            "STAIR_NOT_IN_SCENE", f"No placed stair matches '{stairs_ref}'.",
                            beat_id=beat.beat_id, actor_id=actor_id)
                    sdata = stair_info["data"]
                    stair_obj_name = stair_info["asset_id"]

                    treads = sdata.get("treads", [])
                    tread_depth = np.mean([float(t.get("tread_m", 0.30)) for t in treads]) if treads else 0.30
                    tread_depth = max(0.15, float(tread_depth))

                    if direction == "up":
                        clip_name = "running_up_stairs" if pace == "run" else "walking_up_the_stairs"
                    else:
                        clip_name = "descending_stairs_fast" if pace == "run" else "descending_stairs"

                    raw_cline = sdata.get("centerline", [])
                    if len(raw_cline) < 2:
                        return None, StructuredError(
                            "STAIR_GEOMETRY_MISSING", "The placed stair has no usable centerline.",
                            beat_id=beat.beat_id, actor_id=actor_id)

                    lane_offset = float(beat.params.get("stair_lane_offset_m", 0.0))
                    if lane_offset:
                        width = float(sdata.get("width_m", 0.0))
                        if width <= 0 or abs(lane_offset) > width * 0.5 - 0.2:
                            return None, StructuredError(
                                "INVALID_STAIR_LANE", "Stair lane is outside the usable tread width.",
                                beat_id=beat.beat_id, actor_id=actor_id)
                    exit_distance = max(0.0, float(beat.params.get("stair_exit_distance_m", 0.0)))
                    cline_pts, total_horiz, stair_arrival = stair_route_geometry(
                        sdata, direction, lane_offset, exit_distance)

                    # The centerline belongs to the staircase. Moving it by the
                    # actor's starting offset sends side entrants through a wall
                    # and outside the recognized stair portal.
                    p0 = np.array(cline_pts[0], float)
                    travel_x = cline_pts[-1][0] - cline_pts[0][0]
                    travel_z = cline_pts[-1][2] - cline_pts[0][2]
                    travel_len = max(1e-6, math.hypot(travel_x, travel_z))
                    back_x, back_z = -travel_x / travel_len * 0.7, -travel_z / travel_len * 0.7
                    approach_pts = [list(curr_pos)]
                    if math.hypot(curr_pos[0] - p0[0], curr_pos[2] - p0[2]) > 0.5:
                        approach_pts.extend([
                            [curr_pos[0] + back_x, curr_pos[1], curr_pos[2] + back_z],
                            [p0[0] + back_x, p0[1], p0[2] + back_z],
                        ])
                    approach_pts.append(list(p0))
                    approach_lengths = [math.dist(a, b) for a, b in zip(approach_pts, approach_pts[1:])]
                    approach_distance = sum(approach_lengths)
                    approach_speed = 3.0 if pace == "run" else 1.25
                    approach_duration = (approach_distance / approach_speed
                                         if approach_distance > 0.15 else 0.0)
                    exit_duration = exit_distance / approach_speed
                    climb_duration = dur - approach_duration - exit_duration
                    requested_climb_speed = float(speed_mps_param) if speed_mps_param is not None else (2.0 if pace == "run" else 0.65)
                    min_climb_duration = total_horiz / min(max_allowed_speed, requested_climb_speed)
                    if climb_duration + 1e-6 < min_climb_duration:
                        return None, StructuredError(
                            error_code="STAIR_WINDOW_TOO_SHORT",
                            message="The stair action has too little time to reach the entry and climb safely.",
                            beat_id=beat.beat_id, actor_id=actor_id,
                            details={"required_duration_s": round(approach_duration + min_climb_duration + exit_duration, 3)},
                            suggestions=["Allow more time for the approach and stair climb."],
                        )
                    climb_start = t_start + approach_duration
                    climb_speed = total_horiz / climb_duration
                    clip_meta = self.manifest.get("clips", {}).get(clip_name, {})
                    f_clip = float(clip_meta.get("step_frequency_hz") or (3.43 if "run" in clip_name or "fast" in clip_name else 1.714))
                    # One animated footfall should cover one authored tread.
                    playback_speed = max(0.5, min(2.5, (climb_speed / tread_depth) / max(0.1, f_clip)))

                    if approach_duration:
                        approach_clip = "run" if pace == "run" else "walk"
                        perf.add_action(approach_clip, t_start, climb_start, fps=self.fps,
                                        speed=1.0, blend_s=0.2)
                        approach_frames = max(1, int(round(approach_duration * self.fps)))
                        for frame_idx in range(approach_frames + 1):
                            u = frame_idx / approach_frames
                            remaining = u * approach_distance
                            pos = list(p0)
                            approach_heading = curr_heading
                            for seg_idx, seg_len in enumerate(approach_lengths):
                                if remaining <= seg_len or seg_idx == len(approach_lengths) - 1:
                                    a, b = approach_pts[seg_idx:seg_idx + 2]
                                    v = min(1.0, remaining / max(1e-6, seg_len))
                                    pos = [a[axis] + v * (b[axis] - a[axis]) for axis in range(3)]
                                    approach_heading = heading_from_vector(b[0] - a[0], b[2] - a[2])
                                    break
                                remaining -= seg_len
                            perf.keyframes.append(Keyframe(
                                time_s=t_start + u * approach_duration,
                                position=pos,
                                heading_deg=approach_heading, speed_mps=approach_speed,
                                action_clip=approach_clip, clip_phase=u,
                            ))

                    climb_end = t_end - exit_duration
                    perf.add_action(clip_name, climb_start, climb_end, fps=self.fps,
                                    speed=round(playback_speed, 3), blend_s=0.2)

                    travel_dx = cline_pts[-1][0] - cline_pts[0][0]
                    travel_dz = cline_pts[-1][2] - cline_pts[0][2]
                    travel_heading = heading_from_vector(travel_dx, travel_dz) if math.hypot(travel_dx, travel_dz) > 0.05 else curr_heading

                    perf.allowed_contacts.append({
                        "actor": actor_id,
                        "asset": stair_obj_name,
                        "t0": float(t_start),
                        "t1": float(t_end + 0.5),
                        "max_depth_m": 0.45,
                    })

                    n_frames = max(1, int(round(climb_duration * self.fps)))
                    pitch_target = 7.5 if direction == "up" else -3.0

                    for frame_idx in range(n_frames + 1):
                        f_time = climb_start + frame_idx * climb_duration / n_frames
                        u = frame_idx / float(n_frames)

                        s_curr = u * total_horiz
                        acc = 0.0
                        pos_x, pos_y, pos_z = cline_pts[-1]
                        for i in range(len(cline_pts) - 1):
                            seg_len = math.hypot(cline_pts[i + 1][0] - cline_pts[i][0], cline_pts[i + 1][2] - cline_pts[i][2])
                            if acc + seg_len >= s_curr or i == len(cline_pts) - 2:
                                seg_u = max(0.0, min(1.0, (s_curr - acc) / max(1e-4, seg_len)))
                                pos_x = cline_pts[i][0] + seg_u * (cline_pts[i + 1][0] - cline_pts[i][0])
                                pos_y = cline_pts[i][1] + seg_u * (cline_pts[i + 1][1] - cline_pts[i][1])
                                pos_z = cline_pts[i][2] + seg_u * (cline_pts[i + 1][2] - cline_pts[i][2])
                                break
                            acc += seg_len

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
                            speed_mps=climb_speed,
                            action_clip=clip_name,
                            clip_phase=u,
                            pitch_deg=pitch_val,
                        ))

                    curr_pos = list(cline_pts[-1])
                    curr_heading = travel_heading
                    if exit_duration:
                        exit_clip = "run" if pace == "run" else "walk"
                        perf.add_action(exit_clip, climb_end, t_end, fps=self.fps,
                                        speed=1.0, blend_s=0.2)
                        exit_frames = max(1, int(round(exit_duration * self.fps)))
                        for frame_idx in range(exit_frames + 1):
                            u = frame_idx / exit_frames
                            perf.keyframes.append(Keyframe(
                                time_s=climb_end + u * exit_duration,
                                position=[curr_pos[0] + u * exit_distance * travel_dx / total_horiz,
                                          curr_pos[1],
                                          curr_pos[2] + u * exit_distance * travel_dz / total_horiz],
                                heading_deg=travel_heading, speed_mps=approach_speed,
                                action_clip=exit_clip, clip_phase=u))
                        curr_pos = stair_arrival

                elif fn in ("react", "hit_reaction", "spooked", "flinch"):
                    clip_name = reaction_clip(fn, beat.params)

                    if target and math.hypot(target.position[0] - curr_pos[0], target.position[2] - curr_pos[2]) > 0.1:
                        curr_heading = heading_from_vector(
                            target.position[0] - curr_pos[0],
                            target.position[2] - curr_pos[2],
                        )
                        perf.add_look_at(target.position, t_start, t_end, weight=0.9)

                    contact_target = beat.params.get("target") or beat.params.get("threat")
                    if fn in ("hit_reaction", "react") and contact_target:
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": str(contact_target),
                            "t0": t_start,
                            "t1": t_end,
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

                elif fn in ("attack", "push", "shove", "group_fight"):
                    if fn in ("push", "shove"):
                        moves = [beat.params.get("clip", "elbow_punch")]
                    else:
                        moves = beat.params.get("moves", ["left_hook", "elbow_punch"])
                        if isinstance(moves, str):
                            moves = [moves]
                    if not moves:
                        moves = ["left_hook"]

                    t_target = str(beat.params.get("target") or "")

                    if fn == "group_fight":
                        live_states = self._build_live_actor_states(
                            t_start, actor_id, curr_pos, curr_heading, other_performances, all_initial_states)
                        planned_positions = beat.params.get("opponent_positions", {})
                        positions = [(live_states[opponent]["position"] if opponent in other_performances
                                      else planned_positions.get(opponent, live_states[opponent]["position"]))
                                     for opponent in beat.params.get("opponents", [])
                                     if opponent in live_states]
                        if not positions:
                            return None, StructuredError("FIGHT_GROUPS", "Group fight has no available opposing characters",
                                                         beat_id=beat.beat_id, actor_id=actor_id)
                        center = np.mean(positions, axis=0)
                        curr_heading = heading_from_vector(center[0] - curr_pos[0], center[2] - curr_pos[2])
                        perf.add_look_at(center, t_start, t_end, weight=0.9)

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

                    if t_target:
                        perf.allowed_contacts.append({
                            "actor": actor_id,
                            "asset": t_target,
                            "joints": attack_joints,
                            "t0": t_start,
                            "t1": t_end,
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
                            "t0": float(t_start),
                            "t1": float(t_end + 1.5),
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

                    # Maximum character jump height: standing jump ~0.9m, running jump/vault up to 1.50m (e.g. car roof at 1.47m)
                    max_jump = 1.50
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
                            "t0": float(t_start),
                            "t1": float(t_end + 10.0),
                            "max_depth_m": 0.50,
                        })

                    dist = math.hypot(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2])
                    jump_heading = heading_from_vector(end_pos[0] - curr_pos[0], end_pos[2] - curr_pos[2]) if dist > 0.05 else curr_heading

                    from support_surfaces import STANDING_BODY_HEIGHT_M
                    long_drop = fn == "jump_down" and y_start - y_end > STANDING_BODY_HEIGHT_M
                    takeoff_duration = min(JUMP_TAKEOFF_S, dur * 0.25)
                    if long_drop:
                        perf.add_action("jumping_down", t_start, t_start + takeoff_duration,
                                        fps=self.fps, speed=1.0, blend_s=0.1)
                        perf.add_action("falling_in_air_idle", t_start + takeoff_duration,
                                        t_end, fps=self.fps, speed=1.0, blend_s=0.1)
                    else:
                        perf.add_action("jump", t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.1)

                    chord_mid = (y_start + y_end) / 2.0
                    h_lift = max(0.10, y_apex - chord_mid)

                    n_frames = max(1, int(round(dur * self.fps)))
                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + frame_idx * dt
                        u = frame_idx / float(n_frames)
                        px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                        pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                        if long_drop:
                            takeoff_u = takeoff_duration / dur
                            if u <= takeoff_u:
                                py = y_start + (y_apex - y_start) * (u / takeoff_u)
                                clip = "jumping_down"
                            else:
                                fall_u = (u - takeoff_u) / (1.0 - takeoff_u)
                                py = y_apex - (y_apex - y_end) * fall_u * fall_u
                                clip = "falling_in_air_idle"
                        else:
                            py = y_start + (y_end - y_start) * u + 4.0 * h_lift * u * (1.0 - u)
                            clip = "jump"
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, py, pz],
                            heading_deg=jump_heading,
                            action_clip=clip,
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
                    staged_positions = beat.params.get("interaction_positions") or {}
                    for pid in participants:
                        if pid != actor_id and pid in staged_positions:
                            other_positions.append(list(staged_positions[pid]))
                        elif pid != actor_id and pid in live_states:
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
                    clip_pool = dialogue_clips(fn, beat.params)

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
                                "t0": float(t_start),
                                "t1": float(t_end + 1.0),
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
                                "t0": float(t_start),
                                "t1": float(t_end + 1.0),
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
                        clip_name = DANCE_STYLE_CLIPS.get(st, DANCE_STYLE_CLIPS["groove"])
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
                        clip_name = defense_clip(beat.params)
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
                        if t_target:
                            perf.allowed_contacts.append({
                                "actor": actor_id,
                                "asset": t_target,
                                "t0": t_start,
                                "t1": t_end,
                                "max_depth_m": 0.45,
                            })

                    perf.add_action(clip_name, t_start, t_end, fps=self.fps, speed=1.0, blend_s=0.2)
                    n_frames = max(1, int(round(dur * self.fps)))
                    from crowd_spacing import dance_root_speed
                    root_speed = dance_root_speed(beat.params) if fn == 'dance' else 0.0
                    fwd_x, fwd_z = vector_from_heading(curr_heading)
                    end_pos = [curr_pos[0] + fwd_x * root_speed * dur, curr_pos[1],
                               curr_pos[2] + fwd_z * root_speed * dur]

                    for frame_idx in range(n_frames + 1):
                        f_time = t_start + dur * frame_idx / n_frames
                        u = frame_idx / float(n_frames)
                        px = curr_pos[0] + (end_pos[0] - curr_pos[0]) * u
                        pz = curr_pos[2] + (end_pos[2] - curr_pos[2]) * u
                        perf.keyframes.append(Keyframe(
                            time_s=f_time,
                            position=[px, curr_pos[1], pz],
                            heading_deg=curr_heading,
                            speed_mps=abs(root_speed),
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

