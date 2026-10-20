"""Camera Controller and Frustum Validator for PainterSpace Shot Function Library.

Synthesizes camera positioning, framing styles (wide, medium, close, over_the_shoulder,
top_down, three_quarter), camera movement paths (push_in, pull_out, orbit, track_with, crane_up),
multi-camera cuts, and validates actor visibility in the 3D perspective camera frustum.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from plan_model import StructuredError
from semantic_references import heading_from_vector, vector_from_heading


@dataclass
class CameraKeyframe:
    time_s: float
    position: List[float]       # [x, y, z]
    rotation_xyz: List[float]   # [rotX, rotY, rotZ] in degrees
    focal_length: float = 35.0
    horizontal_aperture: float = 36.0
    vertical_aperture: float = 20.25


class CameraController:
    def __init__(self, fps: int = 24) -> None:
        self.fps = fps

    @staticmethod
    def _look_at_rotation_xyz(cam_pos: List[float], target_pos: List[float]) -> List[float]:
        """Compute USD camera rotateXYZ (pitch, yaw, roll) in degrees to look at target_pos."""
        dx = float(target_pos[0]) - float(cam_pos[0])
        dy = float(target_pos[1]) - float(cam_pos[1])
        dz = float(target_pos[2]) - float(cam_pos[2])
        horiz_dist = math.hypot(dx, dz)
        if horiz_dist < 1e-4 and abs(dy) < 1e-4:
            return [0.0, 0.0, 0.0]
        if horiz_dist < 1e-4:
            return [-90.0 if dy < 0 else 90.0, 0.0, 0.0]
        yaw_deg = math.degrees(math.atan2(-dx, -dz))
        pitch_deg = math.degrees(math.atan2(dy, horiz_dist))
        return [round(pitch_deg, 3), round(yaw_deg, 3), 0.0]

    def _compute_actors_bounds(
        self,
        actor_positions_over_time: Dict[str, List[Tuple[float, List[float]]]],
        tracked_actors: List[str],
        t_start: float,
        t_end: float,
    ) -> Tuple[List[float], float]:
        """Compute 3D centroid and horizontal bounding radius for tracked actors over [t_start, t_end]."""
        actors = [a for a in tracked_actors if a in actor_positions_over_time]
        if not actors:
            actors = list(actor_positions_over_time.keys())
        pts: List[List[float]] = []
        for aid in actors:
            for t, p in actor_positions_over_time.get(aid, []):
                if t_start - 0.05 <= t <= t_end + 0.05:
                    pts.append(p)
        if not pts:
            return [0.0, 0.0, 0.0], 2.0

        arr = np.array(pts, dtype=float)
        min_pt = np.min(arr, axis=0)
        max_pt = np.max(arr, axis=0)
        center = 0.5 * (min_pt + max_pt)
        dx_span = float(max_pt[0] - min_pt[0])
        dy_span = float(max_pt[1] - min_pt[1])
        dz_span = float(max_pt[2] - min_pt[2])
        radius = 0.5 * float(math.sqrt(dx_span * dx_span + dz_span * dz_span + (dy_span * 2.2) ** 2))
        return [float(center[0]), float(center[1]), float(center[2])], max(1.5, radius)

    def synthesize_camera(
        self,
        camera_spec: Optional[Dict[str, Any]],
        actor_positions_over_time: Dict[str, List[Tuple[float, List[float]]]],
        total_duration_s: float,
    ) -> List[CameraKeyframe]:
        """Synthesize camera keyframes based on framing style, frustum solver, movements, and cuts."""
        dt = 1.0 / self.fps
        n_frames = max(1, int(round(total_duration_s * self.fps)))
        keyframes: List[CameraKeyframe] = []

        style = "medium"
        vantage = "eye_level"
        tracked_actors: List[str] = []
        if camera_spec:
            style = str(camera_spec.get("style", "medium")).lower()
            vantage = str(camera_spec.get("vantage", "eye_level")).lower()
            tracked_actors = list(camera_spec.get("actors", []))

        centroid, radius = self._compute_actors_bounds(
            actor_positions_over_time, tracked_actors, 0.0, total_duration_s
        )
        look_target = [centroid[0], centroid[1] + 1.1, centroid[2]]

        # Compute frustum-aware default camera pose if not manually pinned
        focal_len = 35.0
        horiz_ap = 36.0
        if vantage in ("aerial", "drone", "high_angle") or style in ("drone", "aerial"):
            focal_len = 28.0
            tan_half_fov = (horiz_ap * 0.5) / focal_len
            req_dist = max(12.5, (radius + 2.0) / tan_half_fov)
            base_pos = [centroid[0] + 1.2, centroid[1] + 5.8, centroid[2] + req_dist]
            base_rot = self._look_at_rotation_xyz(base_pos, look_target)
        elif style == "wide":
            focal_len = 28.0
            tan_half_fov = (horiz_ap * 0.5) / focal_len
            req_dist = max(14.0, (radius + 2.0) / tan_half_fov)
            base_pos = [centroid[0], centroid[1] + 4.0, centroid[2] + req_dist]
            base_rot = self._look_at_rotation_xyz(base_pos, look_target)
        elif style == "close":
            focal_len = 50.0
            base_pos = [centroid[0], centroid[1] + 1.6, centroid[2] + 3.8]
            base_rot = self._look_at_rotation_xyz(base_pos, look_target)
        elif style == "top_down":
            focal_len = 24.0
            base_pos = [centroid[0], centroid[1] + max(18.0, radius * 2.2), centroid[2] + 0.01]
            base_rot = [-90.0, 0.0, 0.0]
        elif style == "over_the_shoulder":
            focal_len = 35.0
            base_pos = [centroid[0] - 0.8, centroid[1] + 1.6, centroid[2] + 3.2]
            base_rot = self._look_at_rotation_xyz(base_pos, look_target)
        elif style in ("three_quarter", "three_quarter_front_left", "front_left_three_quarter"):
            focal_len = 35.0
            base_pos = [6.8, 2.5, 6.5]
            base_rot = [-12.0, 42.0, 0.0]
        else:  # medium
            focal_len = 35.0
            tan_half_fov = (horiz_ap * 0.5) / focal_len
            req_dist = max(9.5, (radius + 1.5) / tan_half_fov)
            base_pos = [centroid[0], centroid[1] + 2.2, centroid[2] + req_dist]
            base_rot = self._look_at_rotation_xyz(base_pos, look_target)

        if camera_spec and "start_position" in camera_spec:
            base_pos = list(camera_spec["start_position"])
            if "start_rotation" not in camera_spec:
                base_rot = self._look_at_rotation_xyz(base_pos, look_target)
        if camera_spec and "start_rotation" in camera_spec:
            base_rot = list(camera_spec["start_rotation"])

        move_type = camera_spec.get("move") if camera_spec else None
        cuts = camera_spec.get("cuts", []) if camera_spec else []

        for frame_idx in range(n_frames + 1):
            t = frame_idx * dt
            pos = list(base_pos)
            rot = list(base_rot)
            cur_focal = focal_len

            # Support camera cuts if specified
            active_cut = None
            for cut in cuts:
                if float(cut.get("at_s", 0.0)) <= t:
                    active_cut = cut
            if active_cut is not None:
                c_style = str(active_cut.get("style", style)).lower()
                if "position" in active_cut:
                    pos = list(active_cut["position"])
                elif c_style == "close":
                    pos = [centroid[0] + 0.6, centroid[1] + 1.6, centroid[2] + 3.4]
                    cur_focal = 50.0
                elif c_style == "wide":
                    pos = [centroid[0], centroid[1] + 4.2, centroid[2] + 15.0]
                    cur_focal = 28.0
                rot = list(active_cut.get("rotation", self._look_at_rotation_xyz(pos, look_target)))

            elif camera_spec and "end_position" in camera_spec:
                u = min(1.0, t / max(0.1, total_duration_s))
                end_p = camera_spec["end_position"]
                pos = [base_pos[c] + u * (end_p[c] - base_pos[c]) for c in range(3)]
                if "end_rotation" in camera_spec:
                    end_r = camera_spec["end_rotation"]
                    rot = [base_rot[c] + u * (end_r[c] - base_rot[c]) for c in range(3)]
                else:
                    rot = self._look_at_rotation_xyz(pos, look_target)
            elif style in ("three_quarter", "three_quarter_front_left", "front_left_three_quarter") and move_type == "push_in":
                u = min(1.0, t / max(0.1, total_duration_s))
                pos = [6.8 + (5.2 - 6.8) * u, 2.5 + (1.9 - 2.5) * u, 6.5 + (4.0 - 6.5) * u]
                rot = [-12.0 + (-10.0 - (-12.0)) * u, 42.0 + (38.0 - 42.0) * u, 0.0]
            elif (vantage in ("aerial", "drone", "high_angle") or style in ("drone", "aerial")) and move_type == "push_in":
                u = min(1.0, t / max(0.1, total_duration_s))
                end_p = [centroid[0] + 0.3, centroid[1] + 1.9, centroid[2] + 4.8]
                pos = [base_pos[c] + u * (end_p[c] - base_pos[c]) for c in range(3)]
                rot = self._look_at_rotation_xyz(pos, look_target)
            elif move_type == "push_in":
                u = min(1.0, t / max(0.1, total_duration_s))
                pos[2] -= 3.0 * u
                if not (camera_spec and "start_rotation" in camera_spec):
                    rot = self._look_at_rotation_xyz(pos, look_target)
            elif move_type == "pull_out":
                u = min(1.0, t / max(0.1, total_duration_s))
                pos[2] += 4.0 * u
                if not (camera_spec and "start_rotation" in camera_spec):
                    rot = self._look_at_rotation_xyz(pos, look_target)
            elif move_type == "crane_up":
                u = min(1.0, t / max(0.1, total_duration_s))
                pos[1] += 3.5 * u
                rot[0] -= 10.0 * u
            elif move_type == "orbit":
                u = min(1.0, t / max(0.1, total_duration_s))
                orbit_deg = float(camera_spec.get("angle_deg", 45.0)) if camera_spec else 45.0
                theta = math.radians(orbit_deg * u)
                rel_x = base_pos[0] - centroid[0]
                rel_z = base_pos[2] - centroid[2]
                pos[0] = centroid[0] + rel_x * math.cos(theta) + rel_z * math.sin(theta)
                pos[2] = centroid[2] - rel_x * math.sin(theta) + rel_z * math.cos(theta)
                rot = self._look_at_rotation_xyz(pos, look_target)
            elif move_type == "track_with" and tracked_actors:
                target_actor = tracked_actors[0]
                samples = actor_positions_over_time.get(target_actor, [])
                if samples:
                    curr_actor_pos = self._sample_pos(samples, t)
                    pos[0] = curr_actor_pos[0]
                    pos[2] = curr_actor_pos[2] + 7.5
                    rot = self._look_at_rotation_xyz(pos, [curr_actor_pos[0], curr_actor_pos[1] + 1.1, curr_actor_pos[2]])

            keyframes.append(CameraKeyframe(
                time_s=t,
                position=pos,
                rotation_xyz=rot,
                focal_length=cur_focal,
            ))

        return keyframes

    def _sample_pos(self, samples: List[Tuple[float, List[float]]], t: float) -> List[float]:
        if not samples:
            return [0.0, 0.0, 0.0]
        if t <= samples[0][0]:
            return list(samples[0][1])
        if t >= samples[-1][0]:
            return list(samples[-1][1])
        for i in range(len(samples) - 1):
            t0, p0 = samples[i]
            t1, p1 = samples[i + 1]
            if t0 <= t <= t1:
                alpha = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return [p0[c] + alpha * (p1[c] - p0[c]) for c in range(3)]
        return list(samples[-1][1])

    def validate_frustum(
        self,
        camera_keyframes: List[CameraKeyframe],
        actor_positions: Dict[str, List[Tuple[float, List[float]]]],
        requested_actors: List[str],
    ) -> Tuple[bool, List[str]]:
        """Validate that all requested actors stay in front of the camera and within its 3D perspective frustum."""
        if not requested_actors or not camera_keyframes:
            return True, []

        warnings: List[str] = []
        check_times = [0.0, camera_keyframes[-1].time_s * 0.5, camera_keyframes[-1].time_s]

        for aid in requested_actors:
            samples = actor_positions.get(aid, [])
            if not samples:
                continue
            for t in check_times:
                actor_pt = self._sample_pos(samples, t)
                cam_kf = min(camera_keyframes, key=lambda k: abs(k.time_s - t))

                # Evaluate torso center (y + 0.9 m) relative to camera pose
                dx = float(actor_pt[0]) - float(cam_kf.position[0])
                dy = (float(actor_pt[1]) + 0.9) - float(cam_kf.position[1])
                dz = float(actor_pt[2]) - float(cam_kf.position[2])
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)

                if dist > 40.0:
                    warnings.append(f"Actor '{aid}' is very far from camera ({dist:.1f} m) at t={t:.1f} s")
                    continue

                pitch_rad = math.radians(float(cam_kf.rotation_xyz[0]))
                yaw_rad = math.radians(float(cam_kf.rotation_xyz[1]))

                # Camera basis vectors in world space (USD camera looks along local -Z)
                fx = -math.sin(yaw_rad) * math.cos(pitch_rad)
                fy = math.sin(pitch_rad)
                fz = -math.cos(yaw_rad) * math.cos(pitch_rad)

                rx = math.cos(yaw_rad)
                ry = 0.0
                rz = -math.sin(yaw_rad)

                ux = math.sin(yaw_rad) * math.sin(pitch_rad)
                uy = math.cos(pitch_rad)
                uz = math.cos(yaw_rad) * math.sin(pitch_rad)

                d_front = dx * fx + dy * fy + dz * fz
                if d_front <= 0.25:
                    warnings.append(f"Actor '{aid}' is behind the camera (depth={d_front:.2f} m) at t={t:.1f} s")
                    continue

                x_cam = dx * rx + dy * ry + dz * rz
                y_cam = dx * ux + dy * uy + dz * uz

                tan_half_h = (cam_kf.horizontal_aperture * 0.5) / max(1.0, cam_kf.focal_length)
                tan_half_v = (cam_kf.vertical_aperture * 0.5) / max(1.0, cam_kf.focal_length)

                ndc_x = (x_cam / d_front) / tan_half_h
                ndc_y = (y_cam / d_front) / tan_half_v

                if abs(ndc_x) > 1.35 or abs(ndc_y) > 1.35:
                    warnings.append(
                        f"Actor '{aid}' is outside camera frustum (ndc=({ndc_x:.2f}, {ndc_y:.2f})) at t={t:.1f} s"
                    )

        return len(warnings) == 0, warnings
