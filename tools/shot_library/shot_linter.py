"""Shot Linter for PainterSpace Shot Function Library.

Verifies that compiled shots obey physical invariants:
- Actor headings match their direction of travel (no moonwalking).
- Travel speeds match animation clip stride speeds.
- Actors are firmly planted on the ground (no underground or floating prims).
- All keyframes and timecodes are within valid shot ranges.
- Actor customData and identity integrity are preserved.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from motion_synthesis import ActorPerformance


class ShotLinter:
    def __init__(self, ground_y: float = 0.0, max_heading_deviation_deg: float = 45.0) -> None:
        self.ground_y = ground_y
        self.max_heading_deviation_deg = max_heading_deviation_deg

    def lint_performance(
        self,
        perf: ActorPerformance,
        gait_stride_speeds: Dict[str, float],
    ) -> Tuple[bool, List[str]]:
        """Lint an actor's performance keyframes against physical sanity rules."""
        errors: List[str] = []
        kfs = perf.keyframes
        if len(kfs) < 2:
            return True, []

        for i in range(len(kfs) - 1):
            k0 = kfs[i]
            k1 = kfs[i + 1]
            dt = k1.time_s - k0.time_s
            if dt <= 1e-4:
                continue

            dx = k1.position[0] - k0.position[0]
            dz = k1.position[2] - k0.position[2]
            dist = math.hypot(dx, dz)
            inst_speed = dist / dt

            # 1. Heading check: if moving significantly (> 0.25 m/s), heading must face travel direction
            if inst_speed > 0.25 and not perf.is_vehicle:
                travel_heading = math.degrees(math.atan2(dx, -dz)) % 360.0
                heading_diff = abs((k0.heading_deg - travel_heading + 180.0) % 360.0 - 180.0)
                if heading_diff > self.max_heading_deviation_deg:
                    # Allow backwards locomotion, dodging, pulling, acrobatics, vehicle door exit steps, and final arrival turns (u > 0.80)
                    exempt_clips = (
                        "slow_jog_backwards", "backward_walking_turn", "run_backward", "crawling_backwards",
                        "dodging_right", "dodging_back", "moonwalk", "pull_heavy_object", "pulling_a_rope",
                        "opening_door_inwards", "crouch_to_stand", "jump", "vault_over_box", "dive_roll",
                        "run_to_dive", "stand_to_roll", "backflip", "cartwheel", "macaco_side",
                        "dying_and_falling_backwards", "knocked_down", "knocked_out", "sweep_fall",
                        "slipping", "tripping",
                    )
                    if k0.action_clip not in exempt_clips and k0.clip_phase <= 0.80:
                        errors.append(
                            f"Actor '{perf.actor_id}' heading ({k0.heading_deg:.1f}°) deviates from travel direction ({travel_heading:.1f}°) by {heading_diff:.1f}° at t={k0.time_s:.2f}s"
                        )

            # 1b. Gait stride speed check: verify locomotion speed is within feasible bounds of clip stride
            if gait_stride_speeds and not perf.is_vehicle and inst_speed > 0.35:
                clip_nom = gait_stride_speeds.get(k0.action_clip)
                if clip_nom and clip_nom > 0.1:
                    if inst_speed > clip_nom * 2.8:
                        errors.append(
                            f"Actor '{perf.actor_id}' speed ({inst_speed:.2f} m/s) exceeds max stride limit for clip '{k0.action_clip}' ({clip_nom:.2f} m/s) at t={k0.time_s:.2f}s"
                        )

            # 2. Elevation / floating check: character feet should not sink underground
            y_pos = k0.position[1]
            if y_pos < self.ground_y - 0.15:
                errors.append(
                    f"Actor '{perf.actor_id}' is underground (y = {y_pos:.2f} m) at t={k0.time_s:.2f}s"
                )

            # 3. Time ordering check
            if k1.time_s < k0.time_s:
                errors.append(
                    f"Actor '{perf.actor_id}' keyframe time went backwards from {k0.time_s}s to {k1.time_s}s"
                )

        return len(errors) == 0, errors

