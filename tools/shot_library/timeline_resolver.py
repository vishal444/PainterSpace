"""Timeline Resolver for PainterSpace Shot Function Library.

Builds dependency graphs from beat timing conditions, detects cycles, evaluates
proximity triggers via provisional trajectories, computes physics/clip durations,
inserts idle fills, enforces settle holds, and outputs a complete resolved timeline.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from plan_model import Beat, ShotPlan, StructuredError, TimingCondition
from semantic_references import DURATION_LEVELS, GAP_BETWEEN_BODIES, GAP_TO_VEHICLE, ReferenceResolver


@dataclass
class ResolvedBeat:
    beat_id: str
    actor_id: str
    function: str
    start_time: float
    end_time: float
    duration: float
    params: Dict[str, Any] = field(default_factory=dict)
    is_idle_fill: bool = False
    events: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "actor_id": self.actor_id,
            "function": self.function,
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "duration": round(float(self.duration), 3),
            "params": self.params,
            "is_idle_fill": self.is_idle_fill,
            "events": {k: round(float(v), 3) for k, v in self.events.items()},
        }


@dataclass
class ResolvedTimeline:
    total_duration_s: float
    fps: int = 24
    beats: List[ResolvedBeat] = field(default_factory=list)
    events: Dict[str, float] = field(default_factory=dict)
    actor_schedules: Dict[str, List[ResolvedBeat]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_duration_s": round(float(self.total_duration_s), 3),
            "fps": self.fps,
            "beats": [b.to_dict() for b in self.beats],
            "events": {k: round(float(v), 3) for k, v in self.events.items()},
            "actor_schedules": {
                aid: [b.to_dict() for b in b_list]
                for aid, b_list in self.actor_schedules.items()
            },
        }


class TimelineResolver:
    def __init__(
        self,
        fps: int = 24,
        settle_hold_s: float = 0.20,
        duration_estimator: Optional[Callable[[Beat, Dict[str, Any]], float]] = None,
        trajectory_sampler: Optional[Callable[[str, float], Tuple[float, float, float]]] = None,
        ref_resolver: Optional[ReferenceResolver] = None,
    ) -> None:
        self.fps = fps
        self.settle_hold_s = settle_hold_s
        self.duration_estimator = duration_estimator
        self.trajectory_sampler = trajectory_sampler
        self.ref_resolver = ref_resolver

    def resolve(
        self,
        plan: ShotPlan,
        actor_initial_states: Dict[str, Dict[str, Any]],
        estimated_durations: Optional[Dict[str, float]] = None,
    ) -> Tuple[Optional[ResolvedTimeline], Optional[StructuredError]]:
        """Resolve all beats into start and end times, inserting idle fills."""
        estimated_durations = estimated_durations or {}
        beats = plan.beats
        if not beats:
            return ResolvedTimeline(total_duration_s=2.0, fps=self.fps), None

        # 1. Build dependency graph
        beat_map: Dict[str, Beat] = {b.beat_id: b for b in beats}
        dependencies: Dict[str, Set[str]] = {b.beat_id: set() for b in beats}
        actor_last_beat: Dict[str, str] = {}

        for b in beats:
            # Implicit actor sequential dependency if no condition specified
            if b.start_condition is None:
                if b.actor_id in actor_last_beat:
                    dependencies[b.beat_id].add(actor_last_beat[b.actor_id])
            else:
                cond = b.start_condition
                if cond.type in ("after", "with"):
                    ref = str(cond.value)
                    parent_beat_id = self._extract_beat_id_from_event(ref, beat_map)
                    if parent_beat_id and parent_beat_id in beat_map:
                        if parent_beat_id != b.beat_id:
                            dependencies[b.beat_id].add(parent_beat_id)
                elif cond.type == "when_closer_than":
                    # Depends on all prior beats of actor_a and actor_b
                    for aid in (cond.actor_a, cond.actor_b):
                        if aid and aid in actor_last_beat:
                            dependencies[b.beat_id].add(actor_last_beat[aid])
                elif cond.type == "when_stopped":
                    aid = str(cond.value)
                    if aid in actor_last_beat:
                        dependencies[b.beat_id].add(actor_last_beat[aid])

            if b.duration_condition is not None and b.duration_condition.type == "until":
                ref = str(b.duration_condition.value)
                parent_beat_id = self._extract_beat_id_from_event(ref, beat_map)
                if parent_beat_id and parent_beat_id in beat_map and parent_beat_id != b.beat_id:
                    dependencies[b.beat_id].add(parent_beat_id)

            actor_last_beat[b.actor_id] = b.beat_id

        # 2. Cycle detection via Kahn's algorithm / DFS
        visited = set()
        rec_stack = set()
        cycle_path: List[str] = []

        def find_cycle(node: str, path: List[str]) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for dep in dependencies.get(node, set()):
                if dep not in visited:
                    if find_cycle(dep, path + [dep]):
                        return True
                elif dep in rec_stack:
                    cycle_path.extend(path + [dep])
                    return True
            rec_stack.remove(node)
            return False

        for b_id in beat_map:
            if b_id not in visited:
                if find_cycle(b_id, [b_id]):
                    return None, StructuredError(
                        error_code="TIMELINE_CYCLE",
                        message=f"Circular timing dependency detected involving beats: {' -> '.join(cycle_path)}",
                        beat_id=b_id,
                        details={"cycle_beats": cycle_path},
                        suggestions=["Remove or alter 'after' or 'with' start conditions to break the cycle."],
                    )

        # 3. Resolve start/end times in topological order
        in_degree = {b_id: len(deps) for b_id, deps in dependencies.items()}
        queue = [b_id for b_id, deg in in_degree.items() if deg == 0]

        resolved_beats: Dict[str, ResolvedBeat] = {}
        events: Dict[str, float] = {}
        actor_end_times: Dict[str, float] = {aid: 0.0 for aid in actor_initial_states}

        # Provisional trajectories for proximity conditions: actor_id -> list of (time, x, y, z)
        provisional_positions: Dict[str, List[Tuple[float, float, float, float]]] = {
            aid: [(0.0, float(s.get("position", [0, 0, 0])[0]), float(s.get("position", [0, 0, 0])[1]), float(s.get("position", [0, 0, 0])[2]))]
            for aid, s in actor_initial_states.items()
        }

        while queue:
            b_id = queue.pop(0)
            beat = beat_map[b_id]
            aid = beat.actor_id

            # Determine start time
            start_time = actor_end_times.get(aid, 0.0)

            if beat.start_condition is not None:
                cond = beat.start_condition
                if cond.type == "at":
                    start_time = float(cond.value)
                elif cond.type == "after":
                    ev_name = str(cond.value)
                    if ev_name in events:
                        start_time = events[ev_name] + self.settle_hold_s
                    else:
                        parent_b = self._extract_beat_id_from_event(ev_name, beat_map)
                        if parent_b and parent_b in resolved_beats:
                            start_time = resolved_beats[parent_b].end_time + self.settle_hold_s
                elif cond.type == "with":
                    ev_name = str(cond.value)
                    if ev_name in events:
                        start_time = events[ev_name]
                    else:
                        parent_b = self._extract_beat_id_from_event(ev_name, beat_map)
                        if parent_b and parent_b in resolved_beats:
                            start_time = resolved_beats[parent_b].start_time
                elif cond.type == "when_closer_than":
                    # Sample proximity from provisional trajectories
                    actor_a = cond.actor_a or aid
                    actor_b = cond.actor_b or "car_1"
                    gap_dist = GAP_TO_VEHICLE.get(str(cond.gap).lower(), 1.8)
                    t_prox = self._find_proximity_time(
                        actor_a, actor_b, gap_dist, provisional_positions, start_time
                    )
                    start_time = max(start_time, t_prox)
                elif cond.type == "when_stopped":
                    target_aid = str(cond.value)
                    ev_key = f"{target_aid}.stopped"
                    start_time = events.get(ev_key, actor_end_times.get(target_aid, start_time))

            # Ensure start time is not before actor's previous action
            if start_time < actor_end_times.get(aid, 0.0):
                is_cont = bool(beat.params.get("continuous", False))
                locomotion_fns = (
                    "walk_to", "run_to", "drive_to", "climb_stairs",
                    "crawl_to", "stumble_to", "back_up_to", "carry_to",
                    "pace_while_talking", "swim_to", "fly_to", "climb_surface", "vault_over",
                )
                start_time = actor_end_times.get(aid, 0.0) + (0.0 if is_cont else (self.settle_hold_s if beat.function in locomotion_fns else 0.0))

            # Determine duration
            if beat.duration_condition is not None and beat.duration_condition.type == "for_duration":
                dur_val = beat.duration_condition.value
                if isinstance(dur_val, (int, float)):
                    duration = float(dur_val)
                else:
                    duration = DURATION_LEVELS.get(str(dur_val).lower().strip(), 3.0)
            elif beat.duration_condition is not None and beat.duration_condition.type == "until":
                ev_name = str(beat.duration_condition.value)
                target_t = events.get(ev_name)
                if target_t is None:
                    parent_b = self._extract_beat_id_from_event(ev_name, beat_map)
                    if parent_b and parent_b in resolved_beats:
                        target_t = resolved_beats[parent_b].end_time
                duration = max(0.5, (target_t - start_time) if target_t is not None else self._default_duration_for_function(beat))
            elif b_id in estimated_durations:
                duration = float(estimated_durations[b_id])
            elif self.duration_estimator:
                duration = self.duration_estimator(beat, actor_initial_states)
            else:
                duration = self._default_duration_for_function(beat)

            duration = max(0.2, duration)
            end_time = start_time + duration

            # Register events
            b_events = {
                f"{b_id}.started": start_time,
                f"{b_id}.finished": end_time,
                f"{aid}.started": start_time,
                f"{aid}.finished": end_time,
            }
            if beat.function in (
                "walk_to", "run_to", "drive_to", "approach", "dodge", "climb_stairs",
                "crawl_to", "stumble_to", "back_up_to", "carry_to", "swim_to", "fly_to",
                "climb_surface", "vault_over",
            ):
                b_events[f"{aid}.arrived"] = end_time
                b_events[f"{b_id}.arrived"] = end_time
            if beat.function in ("drive_to", "brake_to_stop", "drive_toward"):
                b_events[f"{aid}.stopped"] = end_time
                b_events[f"{b_id}.stopped"] = end_time

            events.update(b_events)

            resolved_b = ResolvedBeat(
                beat_id=b_id,
                actor_id=aid,
                function=beat.function,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                params=beat.params,
                events=b_events,
            )
            resolved_beats[b_id] = resolved_b
            actor_end_times[aid] = end_time

            # Update provisional trajectory for aid
            self._update_provisional_trajectory(aid, start_time, end_time, beat, provisional_positions)

            # Check dependent nodes
            for dep_id, deps in dependencies.items():
                if b_id in deps:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        queue.append(dep_id)

        # 4. Compute total shot duration
        total_duration = max((b.end_time for b in resolved_beats.values()), default=2.0)
        total_duration = math.ceil(total_duration * self.fps) / self.fps + 0.5  # hold tail

        if plan.max_duration_s is not None and total_duration > plan.max_duration_s:
            return None, StructuredError(
                error_code="MAX_DURATION_EXCEEDED",
                message=f"Shot duration ({total_duration:.2f} s) exceeds maximum allowed duration ({plan.max_duration_s:.2f} s)",
                details={"computed_duration_s": total_duration, "max_duration_s": plan.max_duration_s},
                suggestions=["Shorten beat durations, increase movement speeds, or raise max_duration_s."],
            )

        # 5. Insert idle fills for actors with gaps
        all_resolved_beats: List[ResolvedBeat] = []
        actor_schedules: Dict[str, List[ResolvedBeat]] = {}

        # Group beats by actor
        by_actor: Dict[str, List[ResolvedBeat]] = {}
        for b in resolved_beats.values():
            by_actor.setdefault(b.actor_id, []).append(b)

        for aid in actor_initial_states:
            a_beats = sorted(by_actor.get(aid, []), key=lambda x: x.start_time)
            full_actor_beats: List[ResolvedBeat] = []
            curr_time = 0.0

            for b in a_beats:
                if b.start_time > curr_time + 0.05:
                    idle_beat = ResolvedBeat(
                        beat_id=f"idle_{aid}_{int(curr_time * 100)}",
                        actor_id=aid,
                        function="wait",
                        start_time=curr_time,
                        end_time=b.start_time,
                        duration=b.start_time - curr_time,
                        is_idle_fill=True,
                    )
                    full_actor_beats.append(idle_beat)
                    all_resolved_beats.append(idle_beat)

                full_actor_beats.append(b)
                all_resolved_beats.append(b)
                curr_time = b.end_time

            # Tail idle fill (or extend continuous activity like dance / perform_activity)
            if curr_time < total_duration:
                if full_actor_beats and full_actor_beats[-1].function in ("dance", "perform_activity", "celebrate"):
                    last_b = full_actor_beats[-1]
                    last_b.end_time = total_duration
                    last_b.duration = total_duration - last_b.start_time
                else:
                    tail_idle = ResolvedBeat(
                        beat_id=f"idle_{aid}_{int(curr_time * 100)}",
                        actor_id=aid,
                        function="wait",
                        start_time=curr_time,
                        end_time=total_duration,
                        duration=total_duration - curr_time,
                        is_idle_fill=True,
                    )
                    full_actor_beats.append(tail_idle)
                    all_resolved_beats.append(tail_idle)

            actor_schedules[aid] = full_actor_beats

        all_resolved_beats.sort(key=lambda x: (x.start_time, x.actor_id))

        timeline = ResolvedTimeline(
            total_duration_s=total_duration,
            fps=self.fps,
            beats=all_resolved_beats,
            events=events,
            actor_schedules=actor_schedules,
        )
        return timeline, None

    def _extract_beat_id_from_event(self, ref: str, beat_map: Dict[str, Beat]) -> Optional[str]:
        if "." in ref:
            prefix = ref.split(".", 1)[0]
            if prefix in beat_map:
                return prefix
            # Try actor prefix: find latest beat for that actor
            matching = [b for b in beat_map.values() if b.actor_id == prefix]
            if matching:
                return matching[-1].beat_id
        elif ref in beat_map:
            return ref
        return None

    def _default_duration_for_function(self, beat: Beat) -> float:
        fn = beat.function
        if fn in (
            "walk_to", "run_to", "sneak_to", "jog_to", "drive_to", "approach",
            "dodge", "flee_from", "follow", "disperse", "reverse_to",
            "crawl_to", "stumble_to", "back_up_to", "carry_to", "pace_while_talking",
            "swim_to", "fly_to", "climb_surface", "pull_object",
        ):
            return 3.0
        elif fn == "climb_stairs":
            pace = beat.params.get("pace", "walk")
            return 2.5 if pace == "run" else 6.0
        elif fn in (
            "react", "hit_reaction", "flinch", "gesture", "greet",
            "shake_hands", "hug_or_kiss", "celebrate", "emote",
            "defend_or_block", "use_weapon", "throw_toward", "honk_horn",
        ):
            return 2.0
        elif fn in ("turn_to_face", "look_at", "stand_at", "set_lights"):
            return 1.0
        elif fn in ("converse", "argue", "huddle", "dance", "perform_activity", "medical_aid", "use_phone", "lie_down"):
            return 5.0
        elif fn in (
            "jump_on", "jump_over", "jump_down", "take_cover", "fall", "stand_up",
            "vault_over", "perform_acrobatics", "collapse_or_knockout", "kneel",
            "interact_object", "inspect_area",
        ):
            return 2.2
        elif fn in ("attack", "push", "shove", "pick_up", "put_down", "open_door", "close_door"):
            return 2.0
        elif fn == "wait":
            return 2.5
        return 2.0

    def _find_proximity_time(
        self,
        actor_a: str,
        actor_b: str,
        threshold_dist: float,
        provisional: Dict[str, List[Tuple[float, float, float, float]]],
        min_time: float,
    ) -> float:
        traj_a = provisional.get(actor_a, [(0.0, 0.0, 0.0, 0.0)])
        traj_b = provisional.get(actor_b, [(0.0, 0.0, 0.0, 0.0)])

        max_t = max(traj_a[-1][0], traj_b[-1][0], min_time + 4.0)
        dt = 1.0 / self.fps
        t = min_time

        while t <= max_t + 1.0:
            pa = self._sample_traj(traj_a, t)
            pb = self._sample_traj(traj_b, t)
            dist = math.hypot(pa[0] - pb[0], pa[2] - pb[2])
            if dist <= threshold_dist:
                return t
            t += dt

        return min_time + 1.0

    def _sample_traj(
        self, traj: List[Tuple[float, float, float, float]], t: float
    ) -> Tuple[float, float, float]:
        if not traj:
            return 0.0, 0.0, 0.0
        if t <= traj[0][0]:
            return traj[0][1], traj[0][2], traj[0][3]
        if t >= traj[-1][0]:
            return traj[-1][1], traj[-1][2], traj[-1][3]
        for i in range(len(traj) - 1):
            t0, x0, y0, z0 = traj[i]
            t1, x1, y1, z1 = traj[i + 1]
            if t0 <= t <= t1:
                alpha = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return x0 + alpha * (x1 - x0), y0 + alpha * (y1 - y0), z0 + alpha * (z1 - z0)
        return traj[-1][1], traj[-1][2], traj[-1][3]

    def _update_provisional_trajectory(
        self,
        aid: str,
        start_time: float,
        end_time: float,
        beat: Beat,
        provisional: Dict[str, List[Tuple[float, float, float, float]]],
    ) -> None:
        last_pos = self._sample_traj(provisional.get(aid, []), start_time)
        traj = provisional.setdefault(aid, [])

        target = beat.params.get("target") or beat.params.get("to")
        resolved_pos: Optional[List[float]] = None
        if isinstance(target, (list, tuple)) and len(target) >= 3:
            resolved_pos = [float(target[0]), float(target[1]), float(target[2])]
        elif target is not None and self.ref_resolver is not None:
            live_states: Dict[str, Dict[str, Any]] = {}
            for k, k_traj in provisional.items():
                p_xyz = self._sample_traj(k_traj, start_time)
                live_states[k] = {"position": [p_xyz[0], p_xyz[1], p_xyz[2]], "facing_deg": 0.0}
            res, err = self.ref_resolver.resolve(str(target), live_states, start_time, source_actor_id=aid)
            if not err and res is not None:
                resolved_pos = list(res.position)

        # Estimate new position
        new_pos = list(last_pos)
        if beat.function == "drive_to" or (beat.function == "drive_toward" and beat.params.get("mode") != "collide"):
            if resolved_pos is not None:
                new_pos = resolved_pos
            else:
                new_pos[2] += 12.0
        elif beat.function in (
            "walk_to", "run_to", "sneak_to", "jog_to", "approach", "stand_at",
            "crawl_to", "stumble_to", "back_up_to", "carry_to", "swim_to", "fly_to",
            "vault_over", "climb_surface",
        ):
            if resolved_pos is not None:
                new_pos = resolved_pos
            else:
                new_pos[2] += 3.0
        elif beat.function == "dodge":
            new_pos[0] -= 1.6

        traj.append((start_time, last_pos[0], last_pos[1], last_pos[2]))
        traj.append((end_time, new_pos[0], new_pos[1], new_pos[2]))
        traj.sort(key=lambda pt: pt[0])


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


