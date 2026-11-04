"""Camera Rig data model, spline evaluator, aim solver, shot size presets,
and USD authoring/baking for Manual Camera Director Mode.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from pxr import Gf, Sdf, Tf, Usd, UsdGeom, Vt
    HAVE_USD = True
except ImportError:
    HAVE_USD = False

from camera_controller import CameraKeyframe


# -----------------------------------------------------------------------------
# Constants & Enums
# -----------------------------------------------------------------------------

SHOT_SIZE_HEIGHT_TARGETS: Dict[str, float] = {
    "extreme_wide": 16.0,
    "wide": 3.0,
    "medium": 1.4,
    "medium_close_up": 0.85,
    "close_up": 0.45,
    "extreme_close_up": 0.18,
    "over_the_shoulder": 1.4,
    "top_down": 3.5,
}

SENSOR_PRESETS: Dict[str, Tuple[float, float]] = {
    # preset: (horizontal_aperture_mm, vertical_aperture_mm)
    "16_9": (36.0, 20.25),
    "super35": (24.89, 18.66),
    "full_frame": (36.0, 24.0),
}


@dataclass
class PathPoint:
    position: List[float]               # [x, y, z]
    time_s: Optional[float] = None
    focal_length: Optional[float] = None
    roll_deg: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"position": [round(float(v), 4) for v in self.position]}
        if self.time_s is not None:
            d["time_s"] = round(float(self.time_s), 4)
        if self.focal_length is not None:
            d["focal_length"] = round(float(self.focal_length), 2)
        if abs(self.roll_deg) > 1e-4:
            d["roll_deg"] = round(float(self.roll_deg), 2)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PathPoint:
        return cls(
            position=[float(v) for v in data.get("position", [0.0, 0.0, 0.0])],
            time_s=float(data["time_s"]) if "time_s" in data and data["time_s"] is not None else None,
            focal_length=float(data["focal_length"]) if "focal_length" in data and data["focal_length"] is not None else None,
            roll_deg=float(data.get("roll_deg", 0.0)),
        )


@dataclass
class CameraRigData:
    schema_version: int = 1
    points: List[PathPoint] = field(default_factory=list)
    path_start_s: float = 0.0
    path_end_s: float = 5.0
    ease: str = "ease_in_out"           # linear, ease_in, ease_out, ease_in_out
    aim_mode: str = "look_at"           # free, look_at, frame, path_tangent
    aim_targets: List[str] = field(default_factory=list)
    aim_bone: str = ""
    aim_offset: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    aim_blend_s: float = 0.5
    aim_smoothing: float = 0.2
    shot_size: str = "medium"
    shot_subject: str = ""
    shot_framing_mode: str = "move_camera"  # move_camera, change_lens
    focal_start: float = 35.0
    focal_end: float = 35.0
    dof_enabled: bool = False
    fstop: float = 2.8
    focus_distance: float = 5.0
    shake_amp: float = 0.0
    shake_freq: float = 2.0
    sensor_preset: str = "16_9"
    aspect_ratio: str = "16_9"
    dolly_zoom_enabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "points": [p.to_dict() for p in self.points],
            "path_start_s": round(float(self.path_start_s), 4),
            "path_end_s": round(float(self.path_end_s), 4),
            "ease": self.ease,
            "aim_mode": self.aim_mode,
            "aim_targets": list(self.aim_targets),
            "aim_bone": self.aim_bone,
            "aim_offset": [round(float(v), 4) for v in self.aim_offset],
            "aim_blend_s": round(float(self.aim_blend_s), 4),
            "aim_smoothing": round(float(self.aim_smoothing), 4),
            "shot_size": self.shot_size,
            "shot_subject": self.shot_subject,
            "shot_framing_mode": self.shot_framing_mode,
            "focal_start": round(float(self.focal_start), 2),
            "focal_end": round(float(self.focal_end), 2),
            "dof_enabled": self.dof_enabled,
            "fstop": round(float(self.fstop), 2),
            "focus_distance": round(float(self.focus_distance), 2),
            "shake_amp": round(float(self.shake_amp), 4),
            "shake_freq": round(float(self.shake_freq), 2),
            "sensor_preset": self.sensor_preset,
            "aspect_ratio": self.aspect_ratio,
            "dolly_zoom_enabled": self.dolly_zoom_enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CameraRigData:
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            points=[PathPoint.from_dict(p) for p in data.get("points", [])],
            path_start_s=float(data.get("path_start_s", 0.0)),
            path_end_s=float(data.get("path_end_s", 5.0)),
            ease=str(data.get("ease", "ease_in_out")),
            aim_mode=str(data.get("aim_mode", "look_at")),
            aim_targets=list(data.get("aim_targets", [])),
            aim_bone=str(data.get("aim_bone", "")),
            aim_offset=[float(v) for v in data.get("aim_offset", [0.0, 0.0, 0.0])],
            aim_blend_s=float(data.get("aim_blend_s", 0.5)),
            aim_smoothing=float(data.get("aim_smoothing", 0.2)),
            shot_size=str(data.get("shot_size", "medium")),
            shot_subject=str(data.get("shot_subject", "")),
            shot_framing_mode=str(data.get("shot_framing_mode", "move_camera")),
            focal_start=float(data.get("focal_start", 35.0)),
            focal_end=float(data.get("focal_end", 35.0)),
            dof_enabled=bool(data.get("dof_enabled", False)),
            fstop=float(data.get("fstop", 2.8)),
            focus_distance=float(data.get("focus_distance", 5.0)),
            shake_amp=float(data.get("shake_amp", 0.0)),
            shake_freq=float(data.get("shake_freq", 2.0)),
            sensor_preset=str(data.get("sensor_preset", "16_9")),
            aspect_ratio=str(data.get("aspect_ratio", "16_9")),
            dolly_zoom_enabled=bool(data.get("dolly_zoom_enabled", False)),
        )


# -----------------------------------------------------------------------------
# Spline Math & Arc-Length Parameterization
# -----------------------------------------------------------------------------

def _catmull_rom_segment(
    p0: np.ndarray, p1: np.ndarray, p2: np.ndarray, p3: np.ndarray,
    u: float, alpha: float = 0.5
) -> Tuple[np.ndarray, np.ndarray]:
    """Evaluate centripetal Catmull-Rom spline on segment between p1 and p2 at local u in [0, 1].
    Returns (position, tangent).
    """
    def knot(a: np.ndarray, b: np.ndarray) -> float:
        dist = float(np.linalg.norm(b - a))
        return max(1e-5, dist ** alpha)

    t0 = 0.0
    t1 = t0 + knot(p0, p1)
    t2 = t1 + knot(p1, p2)
    t3 = t2 + knot(p2, p3)

    t = t1 + u * (t2 - t1)

    # Barry and Goldman pyramid evaluation
    a1 = ((t1 - t) * p0 + (t - t0) * p1) / (t1 - t0)
    a2 = ((t2 - t) * p1 + (t - t1) * p2) / (t2 - t1)
    a3 = ((t3 - t) * p2 + (t - t2) * p3) / (t3 - t2)

    b1 = ((t2 - t) * a1 + (t - t0) * a2) / (t2 - t0)
    b2 = ((t3 - t) * a2 + (t - t1) * a3) / (t3 - t1)

    c = ((t2 - t) * b1 + (t - t1) * b2) / (t2 - t1)

    # Numerical derivative for robust tangent
    eps = 1e-4
    t_next = t + eps * (t2 - t1)
    a1_n = ((t1 - t_next) * p0 + (t_next - t0) * p1) / (t1 - t0)
    a2_n = ((t2 - t_next) * p1 + (t_next - t1) * p2) / (t2 - t1)
    a3_n = ((t3 - t_next) * p2 + (t_next - t2) * p3) / (t3 - t2)
    b1_n = ((t2 - t_next) * a1_n + (t_next - t0) * a2_n) / (t2 - t0)
    b2_n = ((t3 - t_next) * a2_n + (t_next - t1) * a3_n) / (t3 - t1)
    c_n = ((t2 - t_next) * b1_n + (t_next - t1) * b2_n) / (t2 - t1)

    tangent = (c_n - c) / (eps * max(1e-5, t2 - t1))
    norm_tan = np.linalg.norm(tangent)
    if norm_tan > 1e-6:
        tangent = tangent / norm_tan
    else:
        tangent = np.array([0.0, 0.0, -1.0], dtype=float)

    return c, tangent


class ArcLengthSpline:
    """Arc-length parameterized Centripetal Catmull-Rom spline."""

    def __init__(self, raw_points: List[List[float]], samples_per_segment: int = 64) -> None:
        if not raw_points:
            self.points = [np.array([0.0, 1.6, 5.0], dtype=float)]
        else:
            self.points = [np.array(p[:3], dtype=float) for p in raw_points]

        self.samples_per_segment = max(16, samples_per_segment)
        self.num_points = len(self.points)
        self.total_length = 0.0
        self.lut_distances: List[float] = [0.0]
        self.lut_samples: List[Tuple[int, float]] = [(0, 0.0)]  # (segment_idx, local_u)
        self._build_lut()

    def _get_4_points(self, seg_idx: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        # Segment seg_idx is between self.points[seg_idx] and self.points[seg_idx + 1]
        n = self.num_points
        p1 = self.points[seg_idx]
        p2 = self.points[seg_idx + 1]

        if seg_idx == 0:
            p0 = 2.0 * p1 - p2
        else:
            p0 = self.points[seg_idx - 1]

        if seg_idx + 2 < n:
            p3 = self.points[seg_idx + 2]
        else:
            p3 = 2.0 * p2 - p1

        return p0, p1, p2, p3

    def _build_lut(self) -> None:
        if self.num_points < 2:
            self.total_length = 0.0
            self.lut_distances = [0.0, 1.0]
            self.lut_samples = [(0, 0.0), (0, 1.0)]
            return

        cum_dist = 0.0
        self.lut_distances = [0.0]
        self.lut_samples = [(0, 0.0)]

        prev_pos = self.points[0]
        num_segments = self.num_points - 1

        for seg in range(num_segments):
            p0, p1, p2, p3 = self._get_4_points(seg)
            for step in range(1, self.samples_per_segment + 1):
                u = float(step) / float(self.samples_per_segment)
                pos, _ = _catmull_rom_segment(p0, p1, p2, p3, u)
                d = float(np.linalg.norm(pos - prev_pos))
                cum_dist += d
                self.lut_distances.append(cum_dist)
                self.lut_samples.append((seg, u))
                prev_pos = pos

        self.total_length = max(1e-6, cum_dist)

    def sample_at_distance(self, dist_s: float) -> Tuple[List[float], List[float]]:
        """Returns (position [x, y, z], tangent [dx, dy, dz]) at arc-length distance dist_s."""
        if self.num_points < 2:
            p = [float(v) for v in self.points[0]]
            return p, [0.0, 0.0, -1.0]

        clamped_dist = max(0.0, min(dist_s, self.total_length))

        # Binary search in LUT
        idx = np.searchsorted(self.lut_distances, clamped_dist)
        if idx <= 0:
            seg, u = self.lut_samples[0]
        elif idx >= len(self.lut_distances):
            seg, u = self.lut_samples[-1]
        else:
            d0 = self.lut_distances[idx - 1]
            d1 = self.lut_distances[idx]
            ratio = (clamped_dist - d0) / max(1e-6, (d1 - d0))
            seg0, u0 = self.lut_samples[idx - 1]
            seg1, u1 = self.lut_samples[idx]
            if seg0 == seg1:
                seg = seg0
                u = u0 + ratio * (u1 - u0)
            else:
                seg = seg1 if ratio > 0.5 else seg0
                u = u1 if ratio > 0.5 else u0

        p0, p1, p2, p3 = self._get_4_points(seg)
        pos, tan = _catmull_rom_segment(p0, p1, p2, p3, u)
        return [float(v) for v in pos], [float(v) for v in tan]


# -----------------------------------------------------------------------------
# Easing Curves
# -----------------------------------------------------------------------------

def evaluate_ease(t_norm: float, ease_type: str = "ease_in_out") -> float:
    t = max(0.0, min(1.0, float(t_norm)))
    ease = ease_type.lower().replace("-", "_").replace(" ", "_")
    if ease in ("linear", "constant", "none"):
        return t
    if ease in ("easein", "ease_in"):
        return t * t
    if ease in ("easeout", "ease_out"):
        return t * (2.0 - t)
    # Default: ease_in_out (smoothstep / cosine)
    return 0.5 * (1.0 - math.cos(math.pi * t))


# -----------------------------------------------------------------------------
# Aim Rotation Math
# -----------------------------------------------------------------------------

def look_at_rotation_xyz(cam_pos: List[float], target_pos: List[float], roll_deg: float = 0.0) -> List[float]:
    """Compute USD camera rotateXYZ (pitch, yaw, roll) in degrees to look at target_pos from cam_pos."""
    dx = float(target_pos[0]) - float(cam_pos[0])
    dy = float(target_pos[1]) - float(cam_pos[1])
    dz = float(target_pos[2]) - float(cam_pos[2])
    horiz_dist = math.hypot(dx, dz)
    if horiz_dist < 1e-4 and abs(dy) < 1e-4:
        return [0.0, 0.0, round(roll_deg, 2)]
    if horiz_dist < 1e-4:
        pitch = -90.0 if dy < 0 else 90.0
        return [pitch, 0.0, round(roll_deg, 2)]

    yaw_deg = math.degrees(math.atan2(-dx, -dz))
    pitch_deg = math.degrees(math.atan2(dy, horiz_dist))
    return [round(pitch_deg, 3), round(yaw_deg, 3), round(roll_deg, 3)]


def path_tangent_rotation_xyz(tangent: List[float], roll_deg: float = 0.0) -> List[float]:
    """Compute USD camera rotateXYZ (pitch, yaw, roll) in degrees from forward tangent vector."""
    dx = float(tangent[0])
    dy = float(tangent[1])
    dz = float(tangent[2])
    horiz_dist = math.hypot(dx, dz)
    if horiz_dist < 1e-4:
        pitch = -90.0 if dy < 0 else 90.0
        return [pitch, 0.0, round(roll_deg, 2)]
    yaw_deg = math.degrees(math.atan2(-dx, -dz))
    pitch_deg = math.degrees(math.atan2(dy, horiz_dist))
    return [round(pitch_deg, 3), round(yaw_deg, 3), round(roll_deg, 3)]


# -----------------------------------------------------------------------------
# Seeded Shake Layer
# -----------------------------------------------------------------------------

def evaluate_seeded_shake(
    time_s: float, seed: int = 42, amp: float = 0.0, freq: float = 2.0
) -> Tuple[List[float], List[float]]:
    """Deterministic pseudo-random camera shake. Returns (pos_offset, rot_offset)."""
    if amp <= 1e-5:
        return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

    h = hashlib.sha256(f"shake_seed_{seed}".encode("utf-8")).digest()
    phases = [float(b) / 255.0 * 2.0 * math.pi for b in h[:12]]

    t = float(time_s)
    # 3-harmonic sine synthesis
    f = float(freq)
    dx = amp * 0.06 * (math.sin(f * 2.0 * math.pi * t + phases[0]) + 0.5 * math.sin(f * 3.7 * math.pi * t + phases[1]))
    dy = amp * 0.06 * (math.sin(f * 2.4 * math.pi * t + phases[2]) + 0.5 * math.sin(f * 4.1 * math.pi * t + phases[3]))
    dz = amp * 0.03 * (math.sin(f * 1.9 * math.pi * t + phases[4]) + 0.5 * math.sin(f * 3.2 * math.pi * t + phases[5]))

    dpitch = amp * 1.2 * (math.sin(f * 2.1 * math.pi * t + phases[6]) + 0.5 * math.sin(f * 3.5 * math.pi * t + phases[7]))
    dyaw   = amp * 1.2 * (math.sin(f * 1.8 * math.pi * t + phases[8]) + 0.5 * math.sin(f * 4.3 * math.pi * t + phases[9]))
    droll  = amp * 0.8 * (math.sin(f * 2.5 * math.pi * t + phases[10]) + 0.5 * math.sin(f * 3.9 * math.pi * t + phases[11]))

    return [dx, dy, dz], [dpitch, dyaw, droll]


# -----------------------------------------------------------------------------
# Shot Size & Framing Solver
# -----------------------------------------------------------------------------

def compute_shot_size_distance(
    shot_size: str,
    subject_height: float,
    focal_length_mm: float,
    vertical_aperture_mm: float = 20.25,
) -> float:
    """Compute standoff distance D to achieve the target framing occupancy for a shot size."""
    size_key = shot_size.lower().replace("-", "_").replace(" ", "_")
    target_extent = SHOT_SIZE_HEIGHT_TARGETS.get(size_key, 1.4)
    # Scale by subject height relative to a standard human height (1.8m)
    scaled_extent = target_extent * max(0.2, (subject_height / 1.8))
    # D = (scaled_extent * f) / vertical_aperture
    return max(0.5, (scaled_extent * focal_length_mm) / max(1.0, vertical_aperture_mm))


def compute_shot_size_focal(
    shot_size: str,
    subject_height: float,
    distance_m: float,
    vertical_aperture_mm: float = 20.25,
) -> float:
    """Compute focal length (mm) to achieve target framing occupancy at a fixed distance."""
    size_key = shot_size.lower().replace("-", "_").replace(" ", "_")
    target_extent = SHOT_SIZE_HEIGHT_TARGETS.get(size_key, 1.4)
    scaled_extent = target_extent * max(0.2, (subject_height / 1.8))
    # f = (D * vertical_aperture) / scaled_extent
    focal = (distance_m * vertical_aperture_mm) / max(0.05, scaled_extent)
    return max(12.0, min(200.0, focal))


# -----------------------------------------------------------------------------
# Rig Evaluator
# -----------------------------------------------------------------------------

class CameraRigEvaluator:
    """Evaluates a CameraRigData specification into per-frame CameraKeyframe objects."""

    def __init__(self, fps: int = 24) -> None:
        self.fps = fps

    def evaluate(
        self,
        rig: CameraRigData,
        time_s: float,
        actor_positions: Optional[Dict[str, List[float]]] = None,
        subject_height: float = 1.8,
        initial_subject_distance: Optional[float] = None,
    ) -> CameraKeyframe:
        """Evaluate rig at time_s."""
        actor_positions = actor_positions or {}
        raw_pts = [p.position for p in rig.points]
        spline = ArcLengthSpline(raw_pts)

        # 1. Arc-length distance via ease
        dur = max(0.01, rig.path_end_s - rig.path_start_s)
        norm_t = (time_s - rig.path_start_s) / dur
        clamped_norm_t = max(0.0, min(1.0, norm_t))
        eased_norm_dist = evaluate_ease(clamped_norm_t, rig.ease)
        target_dist = spline.total_length * eased_norm_dist

        cam_pos, tangent = spline.sample_at_distance(target_dist)

        # 2. Lens / Focal length interpolation
        # Per-point focal length if available, else linear/ease between focal_start and focal_end
        focal_mm = rig.focal_start + (rig.focal_end - rig.focal_start) * eased_norm_dist

        # Dolly zoom helper (Vertigo): focal dynamically adjusted to keep subject framing constant
        horiz_ap, vert_ap = SENSOR_PRESETS.get(rig.sensor_preset.lower(), (36.0, 20.25))

        target_pos = [0.0, 1.0, 0.0]
        if rig.aim_targets:
            first_target = rig.aim_targets[0]
            if first_target in actor_positions:
                target_pos = list(actor_positions[first_target])
            elif f"/World/{first_target}" in actor_positions:
                target_pos = list(actor_positions[f"/World/{first_target}"])
        elif rig.shot_subject:
            if rig.shot_subject in actor_positions:
                target_pos = list(actor_positions[rig.shot_subject])
            elif f"/World/{rig.shot_subject}" in actor_positions:
                target_pos = list(actor_positions[f"/World/{rig.shot_subject}"])

        resolved_target = [
            target_pos[0] + rig.aim_offset[0],
            target_pos[1] + rig.aim_offset[1],
            target_pos[2] + rig.aim_offset[2],
        ]

        cur_dist_to_target = math.sqrt(
            (cam_pos[0] - resolved_target[0]) ** 2 +
            (cam_pos[1] - resolved_target[1]) ** 2 +
            (cam_pos[2] - resolved_target[2]) ** 2
        )

        if rig.dolly_zoom_enabled and initial_subject_distance and initial_subject_distance > 1e-3:
            focal_mm = max(12.0, min(200.0, rig.focal_start * (cur_dist_to_target / initial_subject_distance)))

        # 3. Aim solve
        roll_deg = 0.0
        # If points have roll overrides, interpolate roll
        if rig.points:
            p_start_roll = rig.points[0].roll_deg
            p_end_roll = rig.points[-1].roll_deg
            roll_deg = p_start_roll + (p_end_roll - p_start_roll) * eased_norm_dist

        if rig.aim_mode == "look_at" or rig.aim_mode == "frame":
            rot_xyz = look_at_rotation_xyz(cam_pos, resolved_target, roll_deg=roll_deg)
        elif rig.aim_mode == "path_tangent":
            rot_xyz = path_tangent_rotation_xyz(tangent, roll_deg=roll_deg)
        else:
            # Free aim: forward along default or tangent
            rot_xyz = path_tangent_rotation_xyz(tangent, roll_deg=roll_deg)

        # 4. Seeded shake
        if rig.shake_amp > 1e-4:
            pos_shk, rot_shk = evaluate_seeded_shake(time_s, seed=42, amp=rig.shake_amp, freq=rig.shake_freq)
            cam_pos = [cam_pos[i] + pos_shk[i] for i in range(3)]
            rot_xyz = [rot_xyz[i] + rot_shk[i] for i in range(3)]

        # 5. Focus distance
        focus_dist = rig.focus_distance
        if rig.dof_enabled:
            focus_dist = max(0.1, cur_dist_to_target)

        return CameraKeyframe(
            time_s=time_s,
            position=[round(v, 4) for v in cam_pos],
            rotation_xyz=[round(v, 3) for v in rot_xyz],
            focal_length=round(focal_mm, 2),
            horizontal_aperture=horiz_ap,
            vertical_aperture=vert_ap,
        )

    def evaluate_sequence(
        self,
        rig: CameraRigData,
        start_s: float,
        end_s: float,
        actor_positions_over_time: Optional[Dict[str, List[Tuple[float, List[float]]]]] = None,
        subject_height: float = 1.8,
    ) -> List[CameraKeyframe]:
        """Evaluate keyframes for all frames in [start_s, end_s]."""
        actor_positions_over_time = actor_positions_over_time or {}
        n_frames = max(1, int(round((end_s - start_s) * self.fps)) + 1)
        keyframes: List[CameraKeyframe] = []

        # Find initial subject distance for dolly zoom
        init_cam = rig.points[0].position if rig.points else [0.0, 1.6, 5.0]
        init_tgt = [0.0, 1.0, 0.0]
        if rig.aim_targets or rig.shot_subject:
            tgt_name = rig.aim_targets[0] if rig.aim_targets else rig.shot_subject
            samples = actor_positions_over_time.get(tgt_name, [])
            if samples:
                init_tgt = samples[0][1]
        init_dist = math.sqrt(sum((init_cam[i] - init_tgt[i]) ** 2 for i in range(3)))

        for i in range(n_frames):
            t = start_s + float(i) / float(self.fps)
            cur_actor_pos: Dict[str, List[float]] = {}
            for aid, samples in actor_positions_over_time.items():
                if samples:
                    # Find closest sample in time
                    best = min(samples, key=lambda s: abs(s[0] - t))
                    cur_actor_pos[aid] = best[1]

            kf = self.evaluate(
                rig=rig,
                time_s=t,
                actor_positions=cur_actor_pos,
                subject_height=subject_height,
                initial_subject_distance=init_dist,
            )
            keyframes.append(kf)

        return keyframes


# -----------------------------------------------------------------------------
# Rig Presets Generator
# -----------------------------------------------------------------------------

def create_rig_preset(
    preset_name: str,
    target_pos: Optional[List[float]] = None,
    distance: float = 5.0,
    duration_s: float = 5.0,
    shot_size: str = "medium",
) -> CameraRigData:
    """Generate parametric camera rig presets."""
    tgt = [float(v) for v in (target_pos or [0.0, 1.0, 0.0])]
    preset = preset_name.lower().replace("-", "_").replace(" ", "_")

    points: List[PathPoint] = []
    aim_mode = "look_at"
    ease = "ease_in_out"

    if preset == "dolly_in":
        # Travel along -Z from distance to distance * 0.45
        p_start = [tgt[0], tgt[1] + 0.3, tgt[2] + distance]
        p_end   = [tgt[0], tgt[1] + 0.2, tgt[2] + distance * 0.45]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "dolly_out":
        p_start = [tgt[0], tgt[1] + 0.2, tgt[2] + distance * 0.5]
        p_end   = [tgt[0], tgt[1] + 0.4, tgt[2] + distance * 1.5]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "truck_left":
        p_start = [tgt[0] + distance * 0.6, tgt[1] + 0.3, tgt[2] + distance]
        p_end   = [tgt[0] - distance * 0.6, tgt[1] + 0.3, tgt[2] + distance]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "truck_right":
        p_start = [tgt[0] - distance * 0.6, tgt[1] + 0.3, tgt[2] + distance]
        p_end   = [tgt[0] + distance * 0.6, tgt[1] + 0.3, tgt[2] + distance]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "pedestal_up":
        p_start = [tgt[0], tgt[1] - 0.4, tgt[2] + distance]
        p_end   = [tgt[0], tgt[1] + 1.8, tgt[2] + distance]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "pedestal_down":
        p_start = [tgt[0], tgt[1] + 1.8, tgt[2] + distance]
        p_end   = [tgt[0], tgt[1] - 0.4, tgt[2] + distance]
        points = [PathPoint(p_start), PathPoint(p_end)]
    elif preset == "crane_arc":
        p0 = [tgt[0] + distance * 0.5, tgt[1] - 0.3, tgt[2] + distance]
        p1 = [tgt[0], tgt[1] + 1.2, tgt[2] + distance * 0.85]
        p2 = [tgt[0] - distance * 0.4, tgt[1] + 2.5, tgt[2] + distance * 0.7]
        points = [PathPoint(p0), PathPoint(p1), PathPoint(p2)]
    elif preset == "orbit":
        # 120-degree circular orbit arc around target
        angles = [-0.6, -0.2, 0.2, 0.6]  # radians
        r = distance
        for ang in angles:
            x = tgt[0] + r * math.sin(ang)
            z = tgt[2] + r * math.cos(ang)
            points.append(PathPoint([x, tgt[1] + 0.3, z]))
    else:
        # Default stationary or subtle push
        p_start = [tgt[0], tgt[1] + 0.3, tgt[2] + distance]
        p_end   = [tgt[0], tgt[1] + 0.3, tgt[2] + distance * 0.85]
        points = [PathPoint(p_start), PathPoint(p_end)]

    return CameraRigData(
        points=points,
        path_start_s=0.0,
        path_end_s=max(0.5, duration_s),
        ease=ease,
        aim_mode=aim_mode,
        aim_offset=[0.0, 0.2, 0.0],
        shot_size=shot_size,
    )


# -----------------------------------------------------------------------------
# Ramer-Douglas-Peucker (RDP) Spline Simplification
# -----------------------------------------------------------------------------

def simplify_points_rdp(points: List[List[float]], epsilon: float = 0.05) -> List[List[float]]:
    """Simplify 3D polyline using the Ramer-Douglas-Peucker algorithm."""
    if len(points) <= 2:
        return points

    p_first = np.array(points[0], dtype=float)
    p_last = np.array(points[-1], dtype=float)
    seg_vec = p_last - p_first
    seg_len = float(np.linalg.norm(seg_vec))

    max_dist = 0.0
    index = 0

    for i in range(1, len(points) - 1):
        pt = np.array(points[i], dtype=float)
        if seg_len < 1e-6:
            dist = float(np.linalg.norm(pt - p_first))
        else:
            cross = np.linalg.norm(np.cross(pt - p_first, seg_vec))
            dist = float(cross / seg_len)

        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist > epsilon:
        left = simplify_points_rdp(points[: index + 1], epsilon)
        right = simplify_points_rdp(points[index:], epsilon)
        return left[:-1] + right
    else:
        return [points[0], points[-1]]


# -----------------------------------------------------------------------------
# OpenUSD Authoring & Baking
# -----------------------------------------------------------------------------

def bake_rig_to_stage(
    stage: Any,
    rig: CameraRigData,
    start_frame: int,
    end_frame: int,
    fps: int = 24,
    actor_positions_over_time: Optional[Dict[str, List[Tuple[float, List[float]]]]] = None,
) -> bool:
    """Bake CameraRigData to /World/ShotCam time samples and /World/CameraRig/Rig_01 in USD stage."""
    if not HAVE_USD or stage is None:
        return False

    evaluator = CameraRigEvaluator(fps=fps)
    start_s = float(start_frame) / float(fps)
    end_s = float(end_frame) / float(fps)
    keyframes = evaluator.evaluate_sequence(
        rig=rig,
        start_s=start_s,
        end_s=end_s,
        actor_positions_over_time=actor_positions_over_time,
    )

    # 1. Author /World/CameraRig/Rig_01 BasisCurves prim
    rig_prim_path = Sdf.Path("/World/CameraRig/Rig_01")
    rig_xform = UsdGeom.Xform.Define(stage, rig_prim_path)
    rig_prim = rig_xform.GetPrim()
    rig_prim.SetCustomDataByKey("schemaVersion", rig.schema_version)

    # Attributes on Rig_01
    rig_prim.CreateAttribute("ps:path_start_s", Sdf.ValueTypeNames.Float).Set(float(rig.path_start_s))
    rig_prim.CreateAttribute("ps:path_end_s", Sdf.ValueTypeNames.Float).Set(float(rig.path_end_s))
    rig_prim.CreateAttribute("ps:ease", Sdf.ValueTypeNames.Token).Set(rig.ease)
    rig_prim.CreateAttribute("ps:aim_mode", Sdf.ValueTypeNames.Token).Set(rig.aim_mode)
    rig_prim.CreateAttribute("ps:aim_offset", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(*rig.aim_offset))
    rig_prim.CreateAttribute("ps:aim_smoothing", Sdf.ValueTypeNames.Float).Set(float(rig.aim_smoothing))
    rig_prim.CreateAttribute("ps:shot_size", Sdf.ValueTypeNames.Token).Set(rig.shot_size)
    rig_prim.CreateAttribute("ps:focal_start", Sdf.ValueTypeNames.Float).Set(float(rig.focal_start))
    rig_prim.CreateAttribute("ps:focal_end", Sdf.ValueTypeNames.Float).Set(float(rig.focal_end))
    rig_prim.CreateAttribute("ps:sensor_preset", Sdf.ValueTypeNames.Token).Set(rig.sensor_preset)
    rig_prim.CreateAttribute("ps:aspect_ratio", Sdf.ValueTypeNames.Token).Set(rig.aspect_ratio)

    if rig.aim_targets:
        rel = rig_prim.CreateRelationship("ps:aim_targets")
        rel.SetTargets([Sdf.Path(t) for t in rig.aim_targets])

    # PositionPath BasisCurves
    curve_path = rig_prim_path.AppendChild("PositionPath")
    curves = UsdGeom.BasisCurves.Define(stage, curve_path)
    curves.GetTypeAttr().Set(UsdGeom.Tokens.cubic)
    curves.GetBasisAttr().Set("catmullRom")

    pts = [Gf.Vec3f(*p.position) for p in rig.points]
    curves.GetPointsAttr().Set(Vt.Vec3fArray(pts))
    curves.GetCurveVertexCountsAttr().Set(Vt.IntArray([len(pts)]))

    # 2. Bake onto /World/ShotCam
    cam_prim_path = Sdf.Path("/World/ShotCam")
    cam_prim = stage.GetPrimAtPath(cam_prim_path)
    if not cam_prim or not cam_prim.IsA(UsdGeom.Camera):
        cam_geom = UsdGeom.Camera.Define(stage, cam_prim_path)
        cam_prim = cam_geom.GetPrim()

    cam_prim.SetCustomDataByKey("painterspace_mode", "manual")
    cam_prim.SetCustomDataByKey("painterspace_rig", str(rig_prim_path))
    cam_prim.SetCustomDataByKey("painterspace_baked", True)

    cam_geom = UsdGeom.Camera(cam_prim)
    cam_xf = UsdGeom.Xformable(cam_prim)

    # Ensure xformOp:translate and xformOp:rotateXYZ
    translate_op = None
    rotate_op = None
    for op in cam_xf.GetOrderedXformOps():
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
            translate_op = op
        elif op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ:
            rotate_op = op

    if not translate_op:
        translate_op = cam_xf.AddTranslateOp()
    if not rotate_op:
        rotate_op = cam_xf.AddRotateXYZOp()

    cam_xf.SetXformOpOrder([translate_op, rotate_op])

    horiz_ap, vert_ap = SENSOR_PRESETS.get(rig.sensor_preset.lower(), (36.0, 20.25))
    cam_geom.GetHorizontalApertureAttr().Set(horiz_ap)
    cam_geom.GetVerticalApertureAttr().Set(vert_ap)
    cam_geom.GetClippingRangeAttr().Set(Gf.Vec2f(0.1, 1000.0))

    # Continuous unwrapping for rotations to eliminate gimbal/360-degree flips
    prev_rot = None
    for kf in keyframes:
        frame_num = int(round(kf.time_s * fps))
        tc = Usd.TimeCode(frame_num)

        pos_vec = Gf.Vec3d(kf.position[0], kf.position[1], kf.position[2])
        translate_op.Set(pos_vec, tc)

        rot_deg = list(kf.rotation_xyz)
        if prev_rot is not None:
            for axis in range(3):
                diff = rot_deg[axis] - prev_rot[axis]
                while diff > 180.0:
                    rot_deg[axis] -= 360.0
                    diff = rot_deg[axis] - prev_rot[axis]
                while diff < -180.0:
                    rot_deg[axis] += 360.0
                    diff = rot_deg[axis] - prev_rot[axis]
        prev_rot = list(rot_deg)

        rot_vec = Gf.Vec3f(rot_deg[0], rot_deg[1], rot_deg[2])
        rotate_op.Set(rot_vec, tc)

        cam_geom.GetFocalLengthAttr().Set(float(kf.focal_length), tc)

    if keyframes:
        first_kf = keyframes[0]
        translate_op.Set(Gf.Vec3d(first_kf.position[0], first_kf.position[1], first_kf.position[2]), Usd.TimeCode.Default())
        rotate_op.Set(Gf.Vec3f(first_kf.rotation_xyz[0], first_kf.rotation_xyz[1], first_kf.rotation_xyz[2]), Usd.TimeCode.Default())
        cam_geom.GetFocalLengthAttr().Set(float(first_kf.focal_length), Usd.TimeCode.Default())

    return True


def read_rig_from_stage(stage: Any, rig_path: str = "/World/CameraRig/Rig_01") -> Optional[CameraRigData]:
    """Read CameraRigData from USD stage if present."""
    if not HAVE_USD or stage is None:
        return None

    prim = stage.GetPrimAtPath(Sdf.Path(rig_path))
    if not prim or not prim.IsValid():
        return None

    rig = CameraRigData()

    if prim.HasCustomDataKey("schemaVersion"):
        rig.schema_version = int(prim.GetCustomDataByKey("schemaVersion"))

    def get_attr(name: str, default: Any) -> Any:
        attr = prim.GetAttribute(name)
        if attr and attr.IsValid() and attr.HasAuthoredValue():
            return attr.Get()
        return default

    rig.path_start_s = float(get_attr("ps:path_start_s", rig.path_start_s))
    rig.path_end_s = float(get_attr("ps:path_end_s", rig.path_end_s))
    rig.ease = str(get_attr("ps:ease", rig.ease))
    rig.aim_mode = str(get_attr("ps:aim_mode", rig.aim_mode))
    offset = get_attr("ps:aim_offset", None)
    if offset:
        rig.aim_offset = [float(offset[0]), float(offset[1]), float(offset[2])]
    rig.aim_smoothing = float(get_attr("ps:aim_smoothing", rig.aim_smoothing))
    rig.shot_size = str(get_attr("ps:shot_size", rig.shot_size))
    rig.focal_start = float(get_attr("ps:focal_start", rig.focal_start))
    rig.focal_end = float(get_attr("ps:focal_end", rig.focal_end))
    rig.sensor_preset = str(get_attr("ps:sensor_preset", rig.sensor_preset))
    rig.aspect_ratio = str(get_attr("ps:aspect_ratio", rig.aspect_ratio))

    rel = prim.GetRelationship("ps:aim_targets")
    if rel and rel.IsValid():
        rig.aim_targets = [str(t) for t in rel.GetTargets()]

    # Read BasisCurves
    curve_prim = stage.GetPrimAtPath(Sdf.Path(rig_path).AppendChild("PositionPath"))
    if curve_prim and curve_prim.IsValid():
        curves = UsdGeom.BasisCurves(curve_prim)
        pts_attr = curves.GetPointsAttr()
        if pts_attr and pts_attr.IsValid():
            pts = pts_attr.Get()
            if pts:
                rig.points = [PathPoint([float(p[0]), float(p[1]), float(p[2])]) for p in pts]

    return rig


def convert_llm_camera_to_rig(stage: Any, fps: int = 24, tolerance: float = 0.05) -> Optional[CameraRigData]:
    """Convert an existing LLM baked /World/ShotCam into an editable CameraRigData."""
    if not HAVE_USD or stage is None:
        return None

    cam_prim = stage.GetPrimAtPath(Sdf.Path("/World/ShotCam"))
    if not cam_prim or not cam_prim.IsValid() or not cam_prim.IsA(UsdGeom.Camera):
        return None

    cam_geom = UsdGeom.Camera(cam_prim)
    cam_xf = UsdGeom.Xformable(cam_prim)

    # Collect time samples
    samples: List[Tuple[float, List[float]]] = []
    tc_samples = cam_geom.GetFocalLengthAttr().GetTimeSamples()
    if not tc_samples:
        tc_samples = [0.0]

    for tc in tc_samples:
        t_s = float(tc) / float(fps)
        xf_matrix = cam_xf.ComputeLocalToWorldTransform(Usd.TimeCode(tc))
        trans = xf_matrix.ExtractTranslation()
        samples.append((t_s, [float(trans[0]), float(trans[1]), float(trans[2])]))

    if not samples:
        return None

    raw_points = [s[1] for s in samples]
    simplified = simplify_points_rdp(raw_points, epsilon=tolerance)

    points = [PathPoint(p) for p in simplified]
    focal_start = float(cam_geom.GetFocalLengthAttr().Get(Usd.TimeCode(tc_samples[0])) or 35.0)
    focal_end = float(cam_geom.GetFocalLengthAttr().Get(Usd.TimeCode(tc_samples[-1])) or focal_start)

    path_start_s = float(tc_samples[0]) / float(fps)
    path_end_s = float(tc_samples[-1]) / float(fps)
    if path_end_s <= path_start_s:
        path_end_s = path_start_s + 5.0

    return CameraRigData(
        points=points,
        path_start_s=path_start_s,
        path_end_s=path_end_s,
        ease="linear",
        aim_mode="look_at",
        focal_start=round(focal_start, 2),
        focal_end=round(focal_end, 2),
    )
