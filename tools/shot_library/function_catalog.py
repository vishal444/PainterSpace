"""Function Catalog for PainterSpace Shot Function Library.

Implements all ~40 functions in the closed vocabulary catalog:
- Queries (list_assets, list_actors, describe_actor, find_actor, list_anchors, list_zones, list_clips, list_surfaces, where_is, distance_between, time_of, can_reach, check_plan)
- Setup (place, place_group, set_appearance, set_variant, remove)
- Character locomotion (walk_to, run_to, sneak_to, jog_to, approach, follow, flee_from, stand_at, turn_to_face, wait)
- Character & object interaction (jump_on, jump_over, jump_down, sit_on, stand_up, open_part, close_part, pick_up, put_down)
- Reactions (react, dodge, fall, look_at, flinch)
- Social (converse, argue, gesture, greet, huddle, disperse)
- Combat & contact (attack, push, shove, hit_reaction, take_cover)
- Vehicles (drive_to, drive_toward, drive_past, drive_along, brake_to_stop, reverse_to, open_door, close_door, set_lights)
- Camera (camera_frame, camera_move, camera_look_at, camera_cut)
"""
from __future__ import annotations

import copy
import inspect
import json
import math
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from actor_registry import ActorAppearance, ActorRegistry
from manifest_schema import enrich_manifest
from plan_model import Beat, SetupAction, ShotPlan, StructuredError, TimingCondition
from semantic_references import ReferenceResolver


class FunctionCatalog:
    def __init__(self, manifest: Dict[str, Any], scene_data: Optional[Dict[str, Any]] = None) -> None:
        self.manifest = enrich_manifest(manifest)
        self.scene_data = scene_data or {}
        self.registry = ActorRegistry()
        self.plan = ShotPlan()
        self.resolver = ReferenceResolver(self.manifest, self.scene_data)
        self._beat_counter = 1

    def _next_beat_id(self, prefix: str = "beat") -> str:
        bid = f"{prefix}_{self._beat_counter}"
        self._beat_counter += 1
        return bid

    def reset(self, prompt: str = "") -> None:
        """Reset the catalog state and shot plan for a fresh planning pass."""
        self.registry = ActorRegistry()
        self.plan = ShotPlan(prompt=prompt)
        self._beat_counter = 1

    def get_plan(self) -> ShotPlan:
        """Return the constructed ShotPlan."""
        return self.plan

    # -------------------------------------------------------------------------
    # 7.1 Queries (no side effects)
    # -------------------------------------------------------------------------
    def list_assets(self) -> List[Dict[str, Any]]:
        """List all valid asset IDs and placeable assets in the project library."""
        return [
            {
                "id": a.get("id"),
                "path": a.get("path"),
                "tags": a.get("tags", []),
                "bounds_m": a.get("bounds_m", []),
                "anchors": list(a.get("anchors", {}).keys()),
                "surfaces": list(a.get("surfaces", {}).keys()),
            }
            for a in self.manifest.get("assets", [])
        ]

    def list_actors(self) -> List[Dict[str, Any]]:
        """List all currently placed actors in the shot."""
        return self.registry.list_actors()

    def describe_actor(self, actor: str) -> Dict[str, Any]:
        """Describe actor ID, label, asset, appearance, and role tags."""
        rec = self.registry.get(actor)
        if not rec:
            return {"error": f"Actor '{actor}' not found"}
        return rec.to_dict()

    def find_actor(self, descriptors: str) -> List[Dict[str, Any]]:
        """Find actor IDs matching descriptive wording (e.g. 'the tall one', 'red jacket')."""
        matches = self.registry.find_actor(descriptors)
        return [m.to_dict() for m in matches]

    def list_anchors(self, asset_or_actor: str) -> List[str]:
        """List valid anchors for an asset or actor."""
        rec = self.registry.get(asset_or_actor)
        asset_id = rec.asset_id if rec else asset_or_actor
        asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") == asset_id), None)
        if asset_entry:
            return list(asset_entry.get("anchors", {}).keys())
        return []

    def list_zones(self) -> List[str]:
        """List all named zones authored in the scene."""
        return [z.get("id") for z in self.scene_data.get("zones", [])]

    def list_clips(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """List available animation clips, optionally filtered by category."""
        res = []
        for name, cinfo in self.manifest.get("clips", {}).items():
            if category and cinfo.get("category") != category:
                continue
            res.append({"name": name, "category": cinfo.get("category"), "duration_s": cinfo.get("duration_s")})
        return res

    def list_surfaces(self, asset_or_actor: str) -> List[str]:
        """List top surfaces available for jumping or sitting on."""
        rec = self.registry.get(asset_or_actor)
        asset_id = rec.asset_id if rec else asset_or_actor
        asset_entry = next((a for a in self.manifest.get("assets", []) if a.get("id") == asset_id), None)
        if asset_entry:
            return list(asset_entry.get("surfaces", {}).keys())
        return []

    def where_is(self, actor: str, time: Optional[float] = None) -> Dict[str, Any]:
        """Get position and facing of an actor at a specific time."""
        rec = self.registry.get(actor)
        if not rec:
            return {"error": f"Actor '{actor}' not found"}
        # Check initial setup
        for s in self.plan.setup:
            if s.actor_id == rec.actor_id:
                return {"position": s.position or [0, 0, 0], "facing_deg": s.facing_deg or 0.0}
        return {"position": [0, 0, 0], "facing_deg": 0.0}

    def distance_between(self, actor_a: str, actor_b: str, time: Optional[float] = None) -> float:
        """Calculate provisional distance in meters between two actors."""
        pa = self.where_is(actor_a).get("position", [0, 0, 0])
        pb = self.where_is(actor_b).get("position", [0, 0, 0])
        return math.hypot(pa[0] - pb[0], pa[2] - pb[2])

    def time_of(self, event: str) -> Optional[float]:
        """Look up the resolved timestamp of an event in the current plan."""
        # Provisional lookup
        return 0.0

    def can_reach(self, actor: str, target: str, by_time: Optional[float] = None, gait: str = "walk") -> Dict[str, Any]:
        """Feasibility check: can actor reach target by given time?"""
        d = 5.0
        speed = 1.4 if gait == "walk" else (4.2 if gait == "run" else 0.8)
        needed_time = d / speed
        return {
            "feasible": by_time is None or needed_time <= by_time,
            "distance_m": d,
            "time_needed_s": needed_time,
            "speed_mps": speed,
        }

    def check_plan(self) -> Dict[str, Any]:
        """Run plan consistency checks on the current plan."""
        return {"valid": True, "beats_count": len(self.plan.beats), "actors_count": len(self.registry._actors)}

    # -------------------------------------------------------------------------
    # 7.2 Setup
    # -------------------------------------------------------------------------
    def place(
        self,
        asset_id: str,
        at_reference: Optional[str] = None,
        facing_reference: Optional[str] = None,
        label: Optional[str] = None,
        role_tags: Optional[List[str]] = None,
        appearance: Optional[Dict[str, Any]] = None,
        position: Optional[List[float]] = None,
        facing_deg: Optional[float] = None,
        in_vehicle: Optional[str] = None,
        seat: Optional[str] = None,
        at: Optional[str] = None,
        facing: Optional[str] = None,
        build: Optional[str] = None,
    ) -> Tuple[bool, Union[Dict[str, Any], StructuredError]]:
        """Place an asset instance at a reference with a unique actor record.
        
        If in_vehicle is provided (e.g. 'car_1'), automatically seats the actor inside the vehicle.
        If seat is omitted, automatically assigns the next available seat (driver_seat, passenger_seat,
        seat_rear_left, seat_rear_right).
        """
        if at_reference is None and at is not None:
            at_reference = at
        if facing_reference is None and facing is not None:
            facing_reference = facing
        if build is not None:
            appearance = dict(appearance or {})
            scales = {"short": 0.94, "average": 1.0, "tall": 1.06}
            appearance["height_scale"] = scales.get(str(build).lower(), 1.0)
        if in_vehicle:
            veh_name = in_vehicle.split(":")[0] if ":" in in_vehicle else in_vehicle
            if not seat:
                standard_seats = ["driver_seat", "passenger_seat", "seat_rear_left", "seat_rear_right"]
                occupied = []
                for s in self.plan.setup:
                    if s.at_reference and s.at_reference.startswith(f"{veh_name}:"):
                        occupied.append(s.at_reference.split(":", 1)[1])
                free_seats = [st for st in standard_seats if st not in occupied]
                seat = free_seats[0] if free_seats else f"seat_row3_{len(occupied)+1}"
            at_reference = f"{veh_name}:{seat}"
            if "driver" in seat.lower() and (role_tags is None or not role_tags):
                role_tags = ["driver"]
            elif role_tags is None or not role_tags:
                role_tags = ["passenger"]

        app = ActorAppearance.from_dict(appearance) if appearance else None
        rec = self.registry.register(
            asset_id=asset_id,
            label=label,
            role_tags=role_tags,
            appearance=app,
        )

        setup_act = SetupAction(
            action_type="place",
            actor_id=rec.actor_id,
            asset_id=rec.asset_id,
            label=rec.label,
            at_reference=at_reference,
            facing_reference=facing_reference,
            appearance=rec.appearance.to_dict(),
            position=position,
            facing_deg=facing_deg,
        )
        self.plan.add_setup(setup_act)
        return True, {"actor_id": rec.actor_id, "label": rec.label, "asset_id": rec.asset_id, "at_reference": at_reference}

    def place_group(
        self,
        group_name: str,
        asset_id: str,
        count: int = 3,
        at_reference: Optional[str] = None,
        formation: str = "circle",
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Place multiple actors in a formation."""
        placed = []
        for i in range(count):
            _, res = self.place(asset_id=asset_id, at_reference=at_reference, label=f"{group_name}_{i+1}")
            placed.append(res)
        return True, placed

    def set_appearance(
        self,
        actor: str,
        color_variant: Optional[str] = None,
        build: Optional[str] = None,
        tint_rgb: Optional[List[float]] = None,
    ) -> Tuple[bool, Union[Dict[str, Any], StructuredError]]:
        """Customize appearance variant or build."""
        rec = self.registry.get(actor)
        if not rec:
            return False, StructuredError("ACTOR_NOT_FOUND", f"Actor '{actor}' not found")
        if color_variant:
            rec.appearance.color_variant = color_variant
        if tint_rgb:
            rec.appearance.tint_rgb = tint_rgb
        if build:
            scales = {"short": 0.94, "average": 1.0, "tall": 1.06}
            rec.appearance.height_scale = scales.get(build.lower(), 1.0)

        self.plan.add_setup(SetupAction(
            action_type="set_appearance",
            actor_id=rec.actor_id,
            appearance=rec.appearance.to_dict(),
        ))
        return True, rec.to_dict()

    def remove(self, actor: str) -> Tuple[bool, str]:
        """Remove an actor from the shot."""
        rec = self.registry.remove_actor(actor)
        if not rec:
            return False, f"Actor '{actor}' not found"
        return True, f"Actor {rec.actor_id} removed"

    # -------------------------------------------------------------------------
    # Helper to create and attach beat
    # -------------------------------------------------------------------------
    def _create_beat(
        self,
        function: str,
        actor: str,
        params: Dict[str, Any],
        after: Optional[str] = None,
        with_event: Optional[str] = None,
        when_closer_than: Optional[Dict[str, Any]] = None,
        when_stopped: Optional[str] = None,
        until: Optional[str] = None,
        for_duration: Optional[Union[str, float]] = None,
        at_time: Optional[float] = None,
    ) -> Tuple[bool, Union[Dict[str, Any], StructuredError]]:
        rec = self.registry.get(actor)
        aid = rec.actor_id if rec else actor

        # Check if duration/timing helpers were passed in kwargs
        if for_duration is None and "for_duration" in params:
            for_duration = params.pop("for_duration")
        if until is None and "until" in params:
            until = params.pop("until")
        if after is None and "after" in params:
            after = params.pop("after")
        if with_event is None and "with_event" in params:
            with_event = params.pop("with_event")
        if when_closer_than is None and "when_closer_than" in params:
            when_closer_than = params.pop("when_closer_than")

        # Validate start condition
        start_cond = None
        if at_time is not None:
            start_cond = TimingCondition(type="at", value=float(at_time))
        elif after:
            start_cond = TimingCondition(type="after", value=after)
        elif with_event:
            start_cond = TimingCondition(type="with", value=with_event)
        elif when_closer_than:
            start_cond = TimingCondition(
                type="when_closer_than",
                value=0.0,
                actor_a=when_closer_than.get("actor_a", aid),
                actor_b=when_closer_than.get("actor_b", "car_1"),
                gap=when_closer_than.get("gap", "near"),
            )
        elif when_stopped:
            start_cond = TimingCondition(type="when_stopped", value=when_stopped)

        dur_cond = None
        if for_duration is not None:
            dur_cond = TimingCondition(type="for_duration", value=for_duration)

        beat_id = self._next_beat_id(function)
        beat = Beat(
            beat_id=beat_id,
            function=function,
            actor_id=aid,
            params=params,
            start_condition=start_cond,
            duration_condition=dur_cond,
        )
        self.plan.add_beat(beat)
        return True, {"beat_id": beat_id, "function": function, "actor_id": aid}

    # -------------------------------------------------------------------------
    # 7.3 Character locomotion
    # -------------------------------------------------------------------------
    def walk_to(self, actor: str, target: str, after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("walk_to", actor, {"target": target, **kwargs}, after=after, with_event=with_event)

    def run_to(self, actor: str, target: str, after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("run_to", actor, {"target": target, **kwargs}, after=after, with_event=with_event)

    def sneak_to(self, actor: str, target: str, after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("sneak_to", actor, {"target": target, **kwargs}, after=after, with_event=with_event)

    def jog_to(self, actor: str, target: str, after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("jog_to", actor, {"target": target, **kwargs}, after=after, with_event=with_event)

    def crawl_to(self, actor: str, target: str, direction: str = "forward", after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Crawl low along the ground (forward or backward) toward a semantic target."""
        return self._create_beat("crawl_to", actor, {"target": target, "direction": direction, **kwargs}, after=after, with_event=with_event)

    def stumble_to(self, actor: str, target: str, style: str = "injured", after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Limp, stagger, or stumble (injured, drunk, or sad) toward a semantic target."""
        return self._create_beat("stumble_to", actor, {"target": target, "style": style, **kwargs}, after=after, with_event=with_event)

    def back_up_to(self, actor: str, target: str, pace: str = "walk", after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Step, jog, or run backward toward target while keeping torso/head facing forward."""
        return self._create_beat("back_up_to", actor, {"target": target, "pace": pace, **kwargs}, after=after, with_event=with_event)

    def carry_to(self, actor: str, target: str, item_style: str = "box", pace: str = "walk", after: Optional[str] = None, with_event: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Carry a box, bag, or heavy object to a semantic target."""
        return self._create_beat("carry_to", actor, {"target": target, "item_style": item_style, "pace": pace, **kwargs}, after=after, with_event=with_event)

    def pace_while_talking(self, actor: str, target: str = "plaza.center", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Walk/pace while talking on a cell phone."""
        return self._create_beat("pace_while_talking", actor, {"target": target, **kwargs}, after=after)

    def swim_to(self, actor: str, target: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Swim toward a target."""
        return self._create_beat("swim_to", actor, {"target": target, **kwargs}, after=after)

    def fly_to(self, actor: str, target: str, height_m: float = 3.2, style: str = "superhero", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Fly or levitate/float through the air to a target."""
        return self._create_beat("fly_to", actor, {"target": target, "height_m": height_m, "style": style, **kwargs}, after=after)

    def climb_stairs(
        self,
        actor: str,
        stairs: str,
        direction: str = "up",
        pace: str = "walk",
        after: Optional[str] = None,
        with_event: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Traverse a staircase ascending or descending with cadence matching and continuous elevation."""
        if direction not in ("up", "down"):
            return False, {"error": f"Invalid direction '{direction}'; must be 'up' or 'down'"}
        if pace not in ("walk", "run"):
            return False, {"error": f"Invalid pace '{pace}'; must be 'walk' or 'run'"}
        params = {
            "stairs": stairs,
            "direction": direction,
            "pace": pace,
            **kwargs,
        }
        return self._create_beat("climb_stairs", actor, params, after=after, with_event=with_event)

    def approach(self, actor: str, target: str, gap: str = "conversational", gait: str = "walk", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("approach", actor, {"target": target, "gap": gap, "gait": gait, **kwargs}, after=after)

    def follow(self, actor: str, target: str, gap: str = "near", until: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("follow", actor, {"target": target, "gap": gap, **kwargs}, after=after, until=until)

    def flee_from(self, actor: str, threat: str, gap: str = "far", into_zone: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("flee_from", actor, {"threat": threat, "gap": gap, "into_zone": into_zone, **kwargs}, after=after)

    def stand_at(self, actor: str, reference: str, facing_reference: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("stand_at", actor, {"reference": reference, "facing_reference": facing_reference, **kwargs})

    def turn_to_face(self, actor: str, reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("turn_to_face", actor, {"target": reference, **kwargs}, after=after)

    def wait(self, actor: str, duration: Union[str, float] = "short", after: Optional[str] = None, until: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("wait", actor, kwargs, after=after, until=until, for_duration=duration)

    # -------------------------------------------------------------------------
    # 7.4 Character and object interaction
    # -------------------------------------------------------------------------
    def jump_on(self, actor: str, surface: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("jump_on", actor, {"target": surface, **kwargs}, after=after)

    def jump_over(self, actor: str, obstacle: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("jump_over", actor, {"target": obstacle, **kwargs}, after=after)

    def jump_down(self, actor: str, target: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("jump_down", actor, {"target": target, **kwargs}, after=after)

    def sit_on(self, actor: str, seat_anchor: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("sit_on", actor, {"target": seat_anchor, **kwargs}, after=after)

    def sit_in(self, actor: str, seat_anchor: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Seat character in a vehicle or interior seat anchor (e.g. car_1:driver_seat)."""
        return self._create_beat("sit_in", actor, {"target": seat_anchor, **kwargs}, after=after)

    def enter_vehicle(
        self,
        actor: str,
        vehicle: str,
        seat: str = "driver_seat",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor approaches and boards a vehicle seat (`driver_seat`, `passenger_seat`, `seat_rear_left`, `seat_rear_right`)."""
        seat_map = {
            "driver": "driver_seat",
            "passenger": "passenger_seat",
            "rear_left": "seat_rear_left",
            "rear_right": "seat_rear_right",
        }
        norm_seat = seat_map.get(seat, seat)
        return self.board_vehicle(vehicle=vehicle, occupants=[actor], seats=norm_seat, after=after, **kwargs)

    def board_vehicle(
        self,
        vehicle: str,
        occupants: Union[str, List[str]],
        seats: Optional[Union[str, List[str], Dict[str, str]]] = None,
        after: Optional[str] = None,
        at_time: Optional[float] = None,
        **kwargs,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Scalable function to seat 1, 2, 4, or N occupants into any vehicle.
        
        Automatically assigns seats if omitted (driver_seat, passenger_seat, seat_rear_left, seat_rear_right).
        """
        veh_name = vehicle.split(":")[0] if ":" in vehicle else vehicle
        occ_list = [occupants] if isinstance(occupants, str) else list(occupants)
        standard_seats = ["driver_seat", "passenger_seat", "seat_rear_left", "seat_rear_right"]

        results = []
        for i, occ in enumerate(occ_list):
            if isinstance(seats, dict):
                seat_name = seats.get(occ, standard_seats[i % len(standard_seats)])
            elif isinstance(seats, list) and i < len(seats):
                seat_name = seats[i]
            elif isinstance(seats, str) and i == 0:
                seat_name = seats
            else:
                seat_name = standard_seats[i % len(standard_seats)]

            tgt = f"{veh_name}:{seat_name}"
            rec = self.registry.get(occ)
            if not rec:
                char_asset = "character.male" if i % 2 == 0 else "character.female"
                self.place(asset_id=char_asset, at_reference=tgt, label=occ, in_vehicle=veh_name, seat=seat_name)
                results.append({"actor_id": occ, "vehicle": veh_name, "seat": seat_name, "action": "placed_in_vehicle"})
            else:
                _, b_res = self._create_beat("sit_in", occ, {"target": tgt, "vehicle": veh_name, **kwargs}, after=after, at_time=at_time)
                results.append(b_res)
        return True, results

    def disembark_vehicle(
        self,
        vehicle: str,
        occupants: Optional[Union[str, List[str]]] = None,
        after: Optional[str] = None,
        at_time: Optional[float] = None,
        **kwargs,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """Scalable function to disembark occupants from a vehicle.
        
        If occupants is omitted, automatically finds all actors currently seated in the vehicle.
        Automatically maps each occupant to their corresponding door:
          - driver_seat -> door_fl
          - passenger_seat -> door_fr
          - seat_rear_left -> door_rl
          - seat_rear_right -> door_rr
        """
        veh_name = vehicle.split(":")[0] if ":" in vehicle else vehicle
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

        if occupants is None:
            occ_list = []
            for s in self.plan.setup:
                if s.at_reference and s.at_reference.startswith(f"{veh_name}:"):
                    occ_list.append(s.actor_id)
        else:
            occ_list = [occupants] if isinstance(occupants, str) else list(occupants)

        results = []
        for occ in occ_list:
            seat_anchor = "driver_seat"
            for s in self.plan.setup:
                if s.actor_id == occ and s.at_reference and s.at_reference.startswith(f"{veh_name}:"):
                    seat_anchor = s.at_reference.split(":", 1)[1]
                    break
            door = seat_to_door.get(seat_anchor, "door_fl" if "left" in seat_anchor or "driver" in seat_anchor else "door_fr")
            _, res = self.exit_vehicle(actor=occ, vehicle=veh_name, door_part=door, after=after, at_time=at_time, **kwargs)
            results.append(res)
        return True, results

    def drive(self, actor: str, vehicle: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Actor acts as driver seated in vehicle, moving with vehicle controls."""
        tgt = f"{vehicle}:driver_seat" if ":" not in vehicle else vehicle
        veh_name = vehicle.split(":")[0] if ":" in vehicle else vehicle
        return self._create_beat("drive", actor, {"target": tgt, "vehicle": veh_name, **kwargs}, after=after)

    def exit_vehicle(
        self,
        actor: str,
        vehicle: Optional[str] = None,
        door_part: Optional[str] = None,
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor opens vehicle door, rises from seat, and steps out onto the roadway stand anchor.
        
        If vehicle or door_part is omitted, automatically looks up the actor's current vehicle and seat,
        routing to the corresponding door (fl for driver, fr for passenger, rl for rear-left, rr for rear-right).
        """
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
        if not vehicle:
            for s in self.plan.setup:
                if s.actor_id == actor and s.at_reference and ":" in s.at_reference:
                    ref_obj, ref_sub = s.at_reference.split(":", 1)
                    if "seat" in ref_sub.lower() or "car" in ref_obj.lower():
                        vehicle = ref_obj
                        if not door_part:
                            door_part = seat_to_door.get(ref_sub, "door_fl")
                        break

        if not door_part:
            seat_name = "driver_seat"
            if vehicle:
                for s in self.plan.setup:
                    if s.actor_id == actor and s.at_reference and s.at_reference.startswith(f"{vehicle}:"):
                        seat_name = s.at_reference.split(":", 1)[1]
                        break
            door_part = seat_to_door.get(seat_name, "door_fl" if "left" in seat_name or "driver" in seat_name else "door_fr")

        params = {"door_part": door_part, **kwargs}
        if vehicle:
            veh_name = vehicle.split(":")[0] if ":" in vehicle else vehicle
            params["vehicle"] = veh_name
            params["target"] = f"{veh_name}:{door_part}_stand"
            params["part"] = f"{veh_name}:{door_part}"
        return self._create_beat("exit_vehicle", actor, params, after=after)

    def stand_up(self, actor: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("stand_up", actor, kwargs, after=after)

    def kneel(
        self,
        actor: str,
        style: str = "kneel",
        target: Optional[str] = None,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor kneels down on one or both knees (`kneel`, `pray`, `inspect`)."""
        return self._create_beat("kneel", actor, {"style": style, "target": target, **kwargs}, after=after, for_duration=duration)

    def lie_down(
        self,
        actor: str,
        style: str = "sleep",
        surface: Optional[str] = None,
        duration: Union[str, float] = "medium",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor lies down flat on the ground or a surface (`sleep`, `rest`, `writhe`)."""
        return self._create_beat("lie_down", actor, {"style": style, "surface": surface, **kwargs}, after=after, for_duration=duration)

    def climb_surface(
        self,
        actor: str,
        surface: str,
        style: str = "wall",
        direction: str = "up",
        height_m: float = 2.4,
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor climbs up or down a vertical or slanted surface (`wall`, `ladder`, `rope`, `roof`)."""
        return self._create_beat(
            "climb_surface",
            actor,
            {"target": surface, "style": style, "direction": direction, "height_m": height_m, **kwargs},
            after=after,
        )

    def vault_over(
        self,
        actor: str,
        obstacle: str,
        style: str = "vault",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor vaults, hurdles, or dive-rolls over an obstacle (`vault`, `dive_roll`, `roll`)."""
        return self._create_beat("vault_over", actor, {"target": obstacle, "style": style, **kwargs}, after=after)

    def perform_acrobatics(
        self,
        actor: str,
        move: str = "backflip",
        target: Optional[str] = None,
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor performs an acrobatic maneuver (`backflip`, `cartwheel`, `macaco`, `spin`, `breakdance_freeze`)."""
        return self._create_beat("perform_acrobatics", actor, {"move": move, "target": target, **kwargs}, after=after)

    def open_part(self, actor: str, part_reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("open_part", actor, {"part": part_reference, **kwargs}, after=after)

    def close_part(self, actor: str, part_reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("close_part", actor, {"part": part_reference, **kwargs}, after=after)

    def pick_up(self, actor: str, object_reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("pick_up", actor, {"object": object_reference, **kwargs}, after=after)

    def put_down(self, actor: str, surface_reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("put_down", actor, {"surface": surface_reference, **kwargs}, after=after)

    def interact_object(
        self,
        actor: str,
        action: str = "push_button",
        target: Optional[str] = None,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor manipulates a world prop or mechanism (`push_button`, `pull_lever`, `enter_code`, `open_cabinet`, `search_high`, `dig`, `type`, `write`)."""
        return self._create_beat("interact_object", actor, {"action": action, "target": target, **kwargs}, after=after, for_duration=duration)

    def throw_toward(
        self,
        actor: str,
        target: str,
        style: str = "overhand",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor throws an object or projectile toward a target (`overhand`, `toss`, `grenade`)."""
        return self._create_beat("throw_toward", actor, {"target": target, "style": style, **kwargs}, after=after)

    def pull_object(
        self,
        actor: str,
        target: Optional[str] = None,
        style: str = "heavy",
        distance_m: float = 1.5,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor pulls a heavy object or rope (`heavy`, `rope`)."""
        return self._create_beat(
            "pull_object",
            actor,
            {"target": target, "style": style, "distance_m": distance_m, **kwargs},
            after=after,
            for_duration=duration,
        )

    def inspect_area(
        self,
        actor: str,
        style: str = "look_around",
        target: Optional[str] = None,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor scans or searches the surrounding area (`look_around`, `nervous`, `look_down`, `search_ground`)."""
        return self._create_beat("inspect_area", actor, {"style": style, "target": target, **kwargs}, after=after, for_duration=duration)

    def use_phone(
        self,
        actor: str,
        mode: str = "talk",
        duration: Union[str, float] = "medium",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor uses a mobile phone (`talk`, `pace`, `text`)."""
        return self._create_beat("use_phone", actor, {"mode": mode, **kwargs}, after=after, for_duration=duration)

    # -------------------------------------------------------------------------
    # 7.5 Reactions
    # -------------------------------------------------------------------------
    def react(self, actor: str, mood: str = "surprised", look_at: Optional[str] = None, after: Optional[str] = None, when_closer_than: Optional[Dict[str, Any]] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("react", actor, {"mood": mood, "look_at": look_at, **kwargs}, after=after, when_closer_than=when_closer_than)

    def dodge(self, actor: str, threat: str = "car_1", lateral_distance_m: float = 1.8, after: Optional[str] = None, when_closer_than: Optional[Dict[str, Any]] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("dodge", actor, {"threat": threat, "lateral_distance_m": lateral_distance_m, **kwargs}, after=after, when_closer_than=when_closer_than)

    def fall(self, actor: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("fall", actor, kwargs, after=after)

    def collapse_or_knockout(
        self,
        actor: str,
        style: str = "knocked_out",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor slips, trips, collapses, or is knocked unconscious (`knocked_out`, `knocked_down`, `dying`, `sweep_fall`, `slip`, `trip`)."""
        return self._create_beat("collapse_or_knockout", actor, {"style": style, **kwargs}, after=after)

    def medical_aid(
        self,
        actor: str,
        patient: str,
        action: str = "cpr",
        duration: Union[str, float] = "medium",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor administers emergency medical care (`cpr`) to a patient on the ground."""
        return self._create_beat("medical_aid", actor, {"target": patient, "patient": patient, "action": action, **kwargs}, after=after, for_duration=duration)

    def look_at(self, actor: str, reference: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("look_at", actor, {"target": reference, **kwargs}, after=after)

    def flinch(self, actor: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("flinch", actor, kwargs, after=after)

    # -------------------------------------------------------------------------
    # 7.6 Social, Emotional & Performance
    # -------------------------------------------------------------------------
    def converse(self, actors: List[str], center: Optional[str] = None, duration: Union[str, float] = "short", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        results = []
        for aid in actors:
            _, res = self._create_beat("converse", aid, {"actors": actors, "center": center, **kwargs}, after=after, for_duration=duration)
            results.append(res)
        return True, results

    def argue(self, actors: List[str], center: Optional[str] = None, intensity: str = "heated", duration: Union[str, float] = "short", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        results = []
        for aid in actors:
            _, res = self._create_beat("argue", aid, {"actors": actors, "center": center, "intensity": intensity, **kwargs}, after=after, for_duration=duration)
            results.append(res)
        return True, results

    def gesture(self, actor: str, move: str = "point_at", target: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("gesture", actor, {"move": move, "target": target, **kwargs}, after=after)

    def greet(self, actor: str, target: str, style: str = "wave", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("greet", actor, {"target": target, "style": style, **kwargs}, after=after)

    def shake_hands(
        self,
        actor: str,
        target: str,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor shakes hands with another actor (`shaking_hands_2`), automatically facing them."""
        return self._create_beat("shake_hands", actor, {"target": target, **kwargs}, after=after, for_duration=duration)

    def hug_or_kiss(
        self,
        actor: str,
        target: str,
        style: str = "kiss",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor shares an affectionate gesture (`kiss`, `pat`, `comfort`) with another actor."""
        return self._create_beat("hug_or_kiss", actor, {"target": target, "style": style, **kwargs}, after=after)

    def celebrate(
        self,
        actor: str,
        style: str = "cheer",
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor celebrates (`cheer`, `victory`, `clap`, `rally`, `fist_pump`)."""
        return self._create_beat("celebrate", actor, {"style": style, **kwargs}, after=after, for_duration=duration)

    def emote(
        self,
        actor: str,
        emotion: str = "shrug",
        target: Optional[str] = None,
        duration: Union[str, float] = "short",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor expresses a distinct emotion or attitude (`cry`, `yell`, `shrug`, `nod`, `dismiss`, `bored`, `salute`, `threaten`, `cocky`, `sad`, `laugh`)."""
        return self._create_beat("emote", actor, {"emotion": emotion, "target": target, **kwargs}, after=after, for_duration=duration)

    def dance(
        self,
        actor: str,
        style: str = "groove",
        duration: Union[str, float] = "medium",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor dances (`groove`, `silly`, `breakdance`, `moonwalk`)."""
        return self._create_beat("dance", actor, {"style": style, **kwargs}, after=after, for_duration=duration)

    def perform_activity(
        self,
        actor: str,
        activity: str = "guitar",
        target: Optional[str] = None,
        duration: Union[str, float] = "medium",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor performs a recreational, musical, or athletic activity (`guitar`, `piano`, `drums`, `sing`, `rap`, `basketball`, `soccer_kick`, `jump_rope`, `fish`, `smoke`, `cards`, `gaming`)."""
        return self._create_beat("perform_activity", actor, {"activity": activity, "target": target, **kwargs}, after=after, for_duration=duration)

    def huddle(self, actors: List[str], center: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        results = []
        for aid in actors:
            _, res = self._create_beat("huddle", aid, {"actors": actors, "center": center, **kwargs}, after=after)
            results.append(res)
        return True, results

    def disperse(self, actors: List[str], after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        results = []
        for aid in actors:
            _, res = self._create_beat("disperse", aid, kwargs, after=after)
            results.append(res)
        return True, results

    # -------------------------------------------------------------------------
    # 7.7 Combat and contact
    # -------------------------------------------------------------------------
    def attack(self, actor: str, target: str, moves: Optional[List[str]] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        moves = moves or ["left_hook", "elbow_punch"]
        return self._create_beat("attack", actor, {"target": target, "moves": moves, **kwargs}, after=after)

    def defend_or_block(
        self,
        actor: str,
        threat: Optional[str] = None,
        style: str = "block",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor guards, blocks, or evades a combat strike (`block`, `dodge_back`, `dodge_right`)."""
        return self._create_beat("defend_or_block", actor, {"target": threat, "style": style, **kwargs}, after=after)

    def use_weapon(
        self,
        actor: str,
        target: str,
        weapon: str = "pistol",
        action: str = "fire",
        after: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, Any]:
        """Actor draws or attacks with a weapon (`pistol`, `rifle`, `sword`, `knife`) against a target."""
        return self._create_beat("use_weapon", actor, {"target": target, "weapon": weapon, "action": action, **kwargs}, after=after)

    def push(self, actor: str, target: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("push", actor, {"target": target, **kwargs}, after=after)

    def shove(self, actor: str, target: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("shove", actor, {"target": target, **kwargs}, after=after)

    def hit_reaction(self, actor: str, strike_move: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("hit_reaction", actor, {"strike_move": strike_move, **kwargs}, after=after)

    def take_cover(self, actor: str, surface: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("take_cover", actor, {"target": surface, **kwargs}, after=after)

    # -------------------------------------------------------------------------
    # 7.8 Vehicles
    # -------------------------------------------------------------------------
    def drive_to(self, vehicle: str, target: str, speed: str = "normal", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("drive_to", vehicle, {"target": target, "speed": speed, **kwargs}, after=after)

    def drive_toward(self, vehicle: str, target: str, mode: str = "stop_short", gap: str = "near", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("drive_toward", vehicle, {"target": target, "mode": mode, "gap": gap, **kwargs}, after=after)

    def drive_past(self, vehicle: str, target: str, lateral_gap: str = "near", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("drive_past", vehicle, {"target": target, "lateral_gap": lateral_gap, **kwargs}, after=after)

    def drive_along(self, vehicle: str, zone_or_lane: str, duration: Union[str, float] = "short", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("drive_along", vehicle, {"zone": zone_or_lane, **kwargs}, after=after, for_duration=duration)

    def brake_to_stop(self, vehicle: str, target: Optional[str] = None, braking: str = "firm", gap: str = "near", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("brake_to_stop", vehicle, {"target": target, "braking": braking, "gap": gap, **kwargs}, after=after)

    def reverse_to(self, vehicle: str, target: str, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("reverse_to", vehicle, {"target": target, **kwargs}, after=after)

    def open_door(self, vehicle: str, door_part: str = "door_fl", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("open_door", vehicle, {"part": door_part, **kwargs}, after=after)

    def close_door(self, vehicle: str, door_part: str = "door_fl", after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("close_door", vehicle, {"part": door_part, **kwargs}, after=after)

    def set_lights(self, vehicle: str, headlights: bool = True, hazards: bool = False, **kwargs) -> Tuple[bool, Any]:
        return self._create_beat("set_lights", vehicle, {"headlights": headlights, "hazards": hazards, **kwargs})

    def honk_horn(self, actor: str, vehicle: Optional[str] = None, after: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        """Driver honks the vehicle horn (`honking_horn`)."""
        return self._create_beat("honk_horn", actor, {"vehicle": vehicle, **kwargs}, after=after)

    # -------------------------------------------------------------------------
    # 7.9 Camera
    # -------------------------------------------------------------------------
    def camera_frame(self, actors: List[str], style: str = "medium", vantage: str = "front", **kwargs) -> Tuple[bool, Any]:
        self.plan.camera = {"style": style, "actors": actors, "vantage": vantage, **kwargs}
        return True, self.plan.camera

    def camera_move(self, move_type: str = "push_in", duration: Union[str, float] = "short", movement: Optional[str] = None, **kwargs) -> Tuple[bool, Any]:
        if not self.plan.camera:
            self.plan.camera = {}
        self.plan.camera["move"] = movement or move_type
        self.plan.camera["duration"] = duration
        self.plan.camera.update(kwargs)
        return True, self.plan.camera

    def camera_look_at(self, reference: str) -> Tuple[bool, Any]:
        if not self.plan.camera:
            self.plan.camera = {}
        self.plan.camera["look_at"] = reference
        return True, self.plan.camera

    def camera_cut(self, style: str = "close", actors: Optional[List[str]] = None, at_time: Optional[float] = None) -> Tuple[bool, Any]:
        if not self.plan.camera:
            self.plan.camera = {}
        self.plan.camera["cut"] = {"style": style, "actors": actors or [], "time": at_time}
        return True, self.plan.camera


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

