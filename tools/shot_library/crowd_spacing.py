"""Assign scene-derived standing spots with clearance for authored character motion."""
from __future__ import annotations

import math
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any
from crowd.config import CrowdSolverConfig

from plan_model import StructuredError
from support_surfaces import (ACTOR_SUPPORT_MARGIN_M, STANDING_SPOT_SPACING_M,
                              SUPPORT_LEVEL_TOLERANCE_M)


# The clip mapping is shared with motion synthesis so clearance follows the
# animation that will actually be baked, including future style additions.
DANCE_STYLE_CLIPS = {
    "groove": "groving_to_music_energetically",
    "silly": "silly_dancing",
    "breakdance": "breakdance_freeze_var_3",
    "moonwalk": "moonwalk",
}

# Authored root translation for dance styles, in metres per second. Shared by
# synthesis and standing-envelope allocation.
DANCE_ROOT_SPEED_MPS = {"moonwalk": -0.9}


def dance_root_speed(params):
    # Dancing at a standing marker should not silently become locomotion.
    # Traveling dances opt in and retain their swept-area reservation.
    if params.get('travel', 'in_place') == 'in_place':
        return 0.0
    if any(params.get(key) for key in ('variant', 'clip', 'in_place_clip')):
        return 0.0
    clip = DANCE_STYLE_CLIPS.get(str(params.get('style', 'groove')).lower(),
                                DANCE_STYLE_CLIPS['groove'])
    return DANCE_ROOT_SPEED_MPS.get(clip, 0.0)

# Ground travel actions use these same default clips during synthesis.
GROUND_TRAVEL_CLIPS = {'walk_to': 'walk', 'run_to': 'run', 'jog_to': 'run', 'sprint_to': 'run'}

# Additional room between two swept body envelopes accounts for interpolation
# between sampled animation poses and small differences in clothing geometry.
MOTION_CLEARANCE_BUFFER_M = CrowdSolverConfig().buffer_m
# Independent fight pairs need separate areas for both participants and their
# moving limbs. The standing grid's body-spacing rule defines the scale.
PAIR_CENTER_SEPARATION_M = 2.0 * STANDING_SPOT_SPACING_M + MOTION_CLEARANCE_BUFFER_M


@lru_cache(maxsize=64)
def _clip_reach(path: str, modified_ns: int, include_limbs: bool = True) -> float | None:
    """Horizontal reach of the posed body, optionally excluding arm/leg joints."""
    from pxr import Usd, UsdGeom, UsdSkel
    from colliders import joint_leaf, segment_radius, is_limb_segment

    stage = Usd.Stage.Open(path)
    if not stage:
        return None
    skeleton = next((prim for prim in stage.Traverse() if prim.IsA(UsdSkel.Skeleton)), None)
    if skeleton is None:
        return None
    root = next((prim for prim in stage.Traverse() if prim.IsA(UsdSkel.Root)), None)
    if root is None:
        return None
    cache = UsdSkel.Cache()
    cache.Populate(UsdSkel.Root(root), Usd.PrimDefaultPredicate)
    query = cache.GetSkelQuery(UsdSkel.Skeleton(skeleton))
    joints = [joint_leaf(joint) for joint in query.GetJointOrder()]
    if not joints:
        return None
    hip = next((index for index, name in enumerate(joints) if name.lower().endswith("hips")), 0)
    topology = query.GetTopology()
    protected = {index for index, name in enumerate(joints)
                 if include_limbs or not is_limb_segment(name)}
    capsule_radius = max((segment_radius(joints[topology.GetParent(index)], name) or 0.0
                          for index, name in enumerate(joints) if topology.GetParent(index) >= 0
                          and (include_limbs or not is_limb_segment(
                              joints[topology.GetParent(index)] + '>' + name))), default=0.0)

    animation = next((prim for prim in stage.Traverse() if prim.IsA(UsdSkel.Animation)), None)
    samples = set()
    if animation is not None:
        schema = UsdSkel.Animation(animation)
        for attr in (schema.GetRotationsAttr(), schema.GetTranslationsAttr(), schema.GetScalesAttr()):
            samples.update(attr.GetTimeSamples())
    if not samples:
        samples = {float(stage.GetStartTimeCode())}
    ordered = sorted(samples)
    # Joint positions can reach farther between keys as rotations interpolate.
    times = sorted(samples | {(left + right) * 0.5 for left, right in zip(ordered, ordered[1:])})
    first_tc = Usd.TimeCode(times[0])
    first_transforms = query.ComputeJointSkelTransforms(first_tc)
    if not first_transforms:
        return None
    first_matrix = UsdGeom.Xformable(skeleton).ComputeLocalToWorldTransform(first_tc)
    origin = first_matrix.Transform(first_transforms[hip].ExtractTranslation())
    reach = 0.0
    for time in times:
        tc = Usd.TimeCode(time)
        matrix = UsdGeom.Xformable(skeleton).ComputeLocalToWorldTransform(tc)
        for index, transform in enumerate(query.ComputeJointSkelTransforms(tc)):
            if index not in protected:
                continue
            point = matrix.Transform(transform.ExtractTranslation())
            reach = max(reach, math.hypot(float(point[0] - origin[0]),
                                          float(point[2] - origin[2])))
    return reach + capsule_radius


def _body_radius(asset_id: str, manifest: dict) -> float:
    """Horizontal radius of the character while moving, without animation reach.

    The manifest's character collision radius describes the body capsule. The
    asset bounds are only a fallback because they include outstretched arms in
    the rest pose and would overstate the space a walking character needs.
    """
    asset = next((item for item in manifest.get("assets", []) if item.get("id") == asset_id), {})
    collision_radius = float((asset.get("collision") or {}).get("character", {}).get("radius_m", 0.0))
    if collision_radius > 0.0:
        return collision_radius
    bounds = asset.get("bounds_m") or [0.0, 0.0, 0.0]
    return max(ACTOR_SUPPORT_MARGIN_M, float(bounds[0]) * 0.5, float(bounds[2]) * 0.5)


def _beat_clip_names(beat: Any, clips=None) -> list[str]:
    """Clip choices used by synthesis, including every possible social gesture."""
    names = []
    clips = clips or {}
    for key in ('in_place_clip', 'clip', 'variant'):
        if beat.params.get(key) in clips:
            names.append(beat.params[key])
    if beat.function in clips:
        names.append(beat.function)
    if beat.function in GROUND_TRAVEL_CLIPS:
        names.append(GROUND_TRAVEL_CLIPS[beat.function])
    if beat.function == "dance":
        style = str(beat.params.get("style", "groove")).lower()
        names.append(DANCE_STYLE_CLIPS.get(style, DANCE_STYLE_CLIPS["groove"]))
    moves = beat.params.get("moves") or []
    names.extend([moves] if isinstance(moves, str) else moves)
    if beat.function == 'defend_or_block':
        names.append(defense_clip(beat.params))
    if beat.function in ('react', 'hit_reaction', 'spooked', 'flinch'):
        names.append(reaction_clip(beat.function, beat.params))
    if beat.function in ('converse', 'argue', 'gesture', 'huddle', 'greet'):
        names.extend(dialogue_clips(beat.function, beat.params))
    return names


def defense_clip(params):
    style = str(params.get('style', 'block')).lower()
    return 'dodging_back' if 'back' in style else ('dodging_right' if 'right' in style or 'dodge' in style else 'boxing_block')


def reaction_clip(function, params):
    style = str(params.get('style', '')).lower()
    if function == 'hit_reaction': return 'head_hit_during_fight'
    if function == 'spooked' or style in ('startled', 'spooked', 'fear', 'panic'): return 'spooked'
    if style in ('happy', 'cheer', 'excited'): return 'clapping'
    return 'reaction'


def dialogue_clips(function, params):
    heated = function == 'argue' or str(params.get('mood', '')).lower() in ('heated', 'angry', 'tense')
    return ['pointing_forward', 'reaction', 'acknowledging'] if heated else ['acknowledging', 'pointing_forward', 'clapping']


def beat_reach(beat: Any, manifest: dict, project_root: Path, include_limbs=True) -> float | None:
    """Measured horizontal reach of the clips a beat plays, or ``None`` if unmeasurable."""
    reach = None
    for name in _beat_clip_names(beat, manifest.get('clips', {})):
        clip_file = manifest.get("clips", {}).get(name, {}).get("file")
        if not clip_file:
            continue
        path = (project_root / clip_file).resolve()
        if path.exists():
            measured = _clip_reach(str(path), path.stat().st_mtime_ns, include_limbs)
            if measured is not None:
                reach = max(reach or 0.0, measured)
    return reach


class ActorEnvelopes:
    """Horizontal space each actor occupies, which depends on what it is doing.

    ``body`` is the base radius. ``beat_reach`` and ``travel_reach`` measure
    protected actor-to-actor anatomy: core body by default, full body in strict
    mode. The separate solid reach maps always include limbs. Animated torso
    leaning and head motion remain protected even when limb contacts are allowed.
    """

    def __init__(self, body: dict[str, float], beat_reach: dict[str, float],
                 buffer_m: float = MOTION_CLEARANCE_BUFFER_M, travel_reach=None,
                 solid_beat_reach=None, solid_travel_reach=None):
        self.body = dict(body)
        self.beat_reach = dict(beat_reach)
        self.buffer_m = buffer_m
        self.travel_reach = dict(travel_reach or {})
        self.solid_beat_reach = dict(beat_reach if solid_beat_reach is None else solid_beat_reach)
        self.solid_travel_reach = dict(self.travel_reach if solid_travel_reach is None else solid_travel_reach)

    def solid_movement_radius(self, actor_id, function, beat_id=None):
        return max(self.body[actor_id], self.solid_travel_reach.get(function, 0.),
                   self.solid_beat_reach.get(beat_id, 0.))

    def movement_radius(self, actor_id, function):
        return max(self.body[actor_id], self.travel_reach.get(function, 0.))

    def knows(self, actor_id: str) -> bool:
        return actor_id in self.body

    def radius_at(self, actor_id: str, when: float, timeline_beats: Any) -> float:
        """Radius at ``when`` given resolved beats (objects with beat_id/actor_id/start_time/end_time)."""
        radius = self.body[actor_id]
        for beat in timeline_beats:
            if beat.actor_id == actor_id and beat.start_time <= when < beat.end_time:
                radius = max(radius, self.beat_reach.get(beat.beat_id, 0.),
                             self.movement_radius(actor_id, getattr(beat, 'function', '')))
        return radius

    def clearance(self, mover: str, other: str, when: float, timeline_beats: Any) -> float:
        return self.body[mover] + self.radius_at(other, when, timeline_beats) + self.buffer_m


def actor_envelopes(plan: Any, initial_states: dict, manifest: dict,
                    project_root: Path, config=None) -> ActorEnvelopes:
    """Measure body radii and per-beat clip reach for every non-vehicle actor."""
    config = config or CrowdSolverConfig()
    include_limbs = not config.allow_actor_limb_contacts
    body = {actor_id: _body_radius(state.get("asset_id", ""), manifest)
            for actor_id, state in initial_states.items() if not state.get("is_vehicle")}
    reach, solid_reach = {}, {}
    for beat in plan.beats:
        if beat.actor_id in body:
            measured = beat_reach(beat, manifest, project_root, include_limbs)
            if measured is not None:
                reach[beat.beat_id] = measured
            solid = beat_reach(beat, manifest, project_root)
            if solid is not None:
                solid_reach[beat.beat_id] = solid
    from plan_model import Beat
    travel_reach = {function: measured for function in GROUND_TRAVEL_CLIPS
                    if (measured := beat_reach(Beat('', function, '', {}), manifest, project_root, include_limbs)) is not None}
    solid_travel = {function: measured for function in GROUND_TRAVEL_CLIPS
                    if (measured := beat_reach(Beat('', function, '', {}), manifest, project_root)) is not None}
    return ActorEnvelopes(body, reach, config.buffer_m, travel_reach, solid_reach, solid_travel)


def _actor_reach(actor_id: str, asset_id: str, plan: Any, manifest: dict, project_root: Path,
                 include_limbs=True, durations=None) -> float:
    """Largest horizontal radius the actor needs at any time, for initial placement."""
    asset = next((item for item in manifest.get("assets", []) if item.get("id") == asset_id), {})
    collision_radius = float((asset.get("collision") or {}).get("character", {}).get("radius_m", 0.0))
    bounds = asset.get("bounds_m") or [0.0, 0.0, 0.0]
    reach = max(ACTOR_SUPPORT_MARGIN_M, collision_radius,
                float(bounds[0]) * 0.5, float(bounds[2]) * 0.5)
    if not include_limbs:
        reach = max(ACTOR_SUPPORT_MARGIN_M, _body_radius(asset_id, manifest))
    for beat in plan.beats:
        if beat.actor_id != actor_id:
            continue
        measured = beat_reach(beat, manifest, project_root, include_limbs)
        if measured is not None:
            reach = max(reach, measured)
    # Bound cumulative authored root travel as well as the skeleton's reach.
    # This reserves the swept dance area even before its heading is resolved.
    drift = sum(abs(dance_root_speed(b.params)) * (durations or {}).get(b.beat_id, 0.)
                for b in plan.beats if b.actor_id == actor_id and b.function == 'dance')
    return reach + drift


def _initial_placement_plan(plan, timeline_beats):
    """Reserve the starting visit, including the first ground departure.

    Later performance envelopes belong to their arrival allocations. Use
    resolved times rather than list order; without a timeline retain the
    conservative whole-shot envelope. Unknown relocation types also retain it.
    """
    if timeline_beats is None:
        return plan
    departures = {}
    starts = {}
    for beat in timeline_beats:
        starts[beat.beat_id] = beat.start_time
        if beat.function in GROUND_TRAVEL_CLIPS:
            departures[beat.actor_id] = min(departures.get(beat.actor_id, math.inf), beat.end_time)
    return replace(plan, beats=[beat for beat in plan.beats
                               if starts.get(beat.beat_id, -math.inf)
                               < departures.get(beat.actor_id, math.inf)])


def space_standing_actors(plan: Any, initial_states: dict, scene_data: dict,
                          manifest: dict, project_root: Path,
                          locked_actors: set[str] | None = None, crowd_config=None,
                          durations=None, timeline_beats=None) -> StructuredError | None:
    """Jointly place standing actors using markers, then measured floor samples.

    Explicit coordinates, vehicle seats, and director-locked actors are left
    intact. They still reserve their occupied area for the adjustable actors.
    Floor samples preserve the marker's owner, local side and support level.
    Generated placements serialize as coordinates rather than fictitious markers.
    """
    locked_actors = locked_actors or set()
    crowd_config = crowd_config or CrowdSolverConfig()
    spots = {(str(spot.get("owner")), str(spot.get("name"))): spot
             for spot in scene_data.get("standing_spots", [])}
    center_assignments = []
    inherited_centers = []
    assigned_pairs = {}
    center_positions = []
    groups = {}
    for beat in plan.beats:
        group_id = beat.params.get("group_id")
        if group_id and not beat.params.get('exact_window'):
            groups.setdefault(group_id, []).append(beat)
    explicit_centers = {}
    for beats in groups.values():
        participants = tuple(sorted(beats[0].params.get('participants') or
                                    {beat.actor_id for beat in beats}))
        reference = beats[0].params.get('center')
        if len(participants) == 2 and reference and (
                ':' not in reference or tuple(reference.split(':', 1)) not in spots):
            explicit_centers[participants] = reference
    for group_id, beats in groups.items():
        if any(beat.actor_id in locked_actors for beat in beats):
            continue
        participants = tuple(sorted(set(beats[0].params.get('participants') or
                                        [beat.actor_id for beat in beats])))
        if len(participants) != 2:
            continue
        reference = beats[0].params.get("center") or ""
        if reference:
            # A director-authored semantic center remains authoritative. Only
            # scene-derived standing markers may be reassigned automatically.
            if ':' not in reference or tuple(reference.split(':', 1)) not in spots:
                continue
            owner, name = reference.split(':', 1)
        else:
            if participants in explicit_centers:
                inherited_centers.extend((beat, explicit_centers[participants]) for beat in beats)
                continue
            if any(actor not in initial_states for actor in participants):
                continue
            sources = [initial_states[actor]['position'] for actor in participants]
            level = sum(source[1] for source in sources) / len(sources)
            preferred = [sum(source[axis] for source in sources) / len(sources)
                         for axis in range(3)]
            available = [spot for spot in spots.values()
                         if abs(spot['position'][1] - level) <= crowd_config.level_tolerance_m]
            if not available:
                continue
            nearest = min(available, key=lambda spot: (math.dist(spot['position'], preferred),
                                                        str(spot.get('owner')), spot['name']))
            owner, name = str(nearest['owner']), nearest['name']
            reference = f'{owner}:{name}'
        if initial_states:
            # Physical plans are allocated jointly after the provisional
            # standing positions are known, before either set of edits commits.
            center_assignments.extend((beat, reference) for beat in beats)
            continue
        pair_key = (owner, participants)
        if pair_key in assigned_pairs:
            chosen = assigned_pairs[pair_key]
        else:
            original = spots[(owner, name)]["position"]
            side = name.split("_", 2)[1]
            candidates = [spot for (spot_owner, spot_name), spot in spots.items()
                          if spot_owner == owner and spot_name.startswith(f"standing_{side}_")]
            candidates.sort(key=lambda spot: (math.hypot(spot["position"][0] - original[0],
                                                         spot["position"][2] - original[2]),
                                              spot["name"]))
            chosen = next((spot for spot in candidates
                           if (spot.get("clearance_m") is None
                               or spot["clearance_m"] >= STANDING_SPOT_SPACING_M * 0.5
                               + MOTION_CLEARANCE_BUFFER_M)
                           and all(abs(spot["position"][1] - point[1]) > SUPPORT_LEVEL_TOLERANCE_M
                                   or math.hypot(spot["position"][0] - point[0],
                                                 spot["position"][2] - point[2])
                                   >= PAIR_CENTER_SEPARATION_M
                                   for point in center_positions)), None)
            if chosen is None:
                return StructuredError(
                    "CROWD_SPACING",
                    f"No separate supported meeting area on the {side} side of {owner} for {group_id}.",
                    beat_id=beats[0].beat_id,
                    suggestions=["Spread the fight areas across more of the floor or stagger the interactions."],
                )
            assigned_pairs[pair_key] = chosen
            center_positions.append(chosen["position"])
        center_assignments.extend((beat, f"{owner}:{chosen['name']}") for beat in beats)

    adjustable = []
    for setup in plan.setup:
        if (setup.action_type != "place" or setup.actor_id in locked_actors
                or setup.position is not None):
            continue
        reference = setup.at_reference or ""
        if ":standing_left_" not in reference and ":standing_right_" not in reference:
            continue
        owner, name = reference.split(":", 1)
        if (owner, name) not in spots or setup.actor_id not in initial_states:
            continue
        state = initial_states[setup.actor_id]
        if state.get("is_vehicle") or state.get("seated_vehicle"):
            continue
        adjustable.append((setup, state, owner, name))
    if not adjustable:
        layout_error = _joint_pair_centers(plan, initial_states, scene_data, manifest, project_root,
                                           locked_actors, center_assignments, crowd_config, durations)
        if layout_error:
            return layout_error
        for beat, reference in center_assignments:
            beat.params["center"] = reference
            beat.params["center_auto"] = True
        for beat, reference in inherited_centers:
            beat.params['center'] = reference
        return None

    actor_ids = {setup.actor_id for setup, _, _, _ in adjustable}
    placement_plan = _initial_placement_plan(plan, timeline_beats)
    reaches = {actor_id: _actor_reach(actor_id, state["asset_id"], placement_plan, manifest, project_root,
                                    not crowd_config.allow_actor_limb_contacts, durations)
               for actor_id, state in initial_states.items()}
    solid_reaches = {actor_id: _actor_reach(actor_id, state['asset_id'], placement_plan, manifest, project_root,
                                         durations=durations)
                     for actor_id, state in initial_states.items()}

    occupied = [(actor_id, state["position"], reaches[actor_id])
                for actor_id, state in initial_states.items()
                if actor_id not in actor_ids and actor_id in reaches and state.get("position")]
    # Allocate the cast together: committing each nearest spot greedily can
    # strand later actors even when moving an earlier actor would fit everyone.
    # Reuse the bounded disc-layout search used for meeting arenas.
    from crowd.arena_layout import assign_arenas
    from crowd.geometry import supported_disc, clears_solids
    from route_placement import PlacementRoutes
    from semantic_references import ReferenceResolver
    placement_routes = PlacementRoutes(scene_data, crowd_config)
    placement_resolver = ReferenceResolver(manifest, scene_data)
    placement_groups, candidate_sets = [], {}
    for setup, state, owner, name in adjustable:
        side = name.split("_", 2)[1]
        original = spots[(owner, name)]["position"]
        reach = reaches[setup.actor_id]
        placement_groups.append({"key": setup.actor_id, "radius": reach,
                                 "solid_radius": solid_reaches[setup.actor_id],
                                 "sources": [original], "preferred": original})
        first_action = next((beat for beat in plan.beats if beat.actor_id == setup.actor_id
                             and beat.function not in ('wait', 'idle', 'turn_to_face')), None)
        if (first_action and first_action.function in GROUND_TRAVEL_CLIPS
                and not first_action.params.get('brief_path') and not first_action.params.get('exact_window')):
            target, error = placement_resolver.resolve(first_action.params.get('target'), initial_states,
                                                       source_actor_id=setup.actor_id)
            if not error and target is not None and target.target_type in ('point', 'zone', 'anchor'):
                goal = tuple(target.position)
                radius = solid_reaches[setup.actor_id]
                def approach_cost(spot, goal=goal, radius=radius, original=original):
                    return placement_routes.cost(spot['position'], [goal], radius, preferred=original)
                placement_groups[-1]['candidate_cost'] = approach_cost
        candidate_sets[setup.actor_id] = [
            spot for (spot_owner, spot_name), spot in spots.items()
            if spot_owner == owner and spot_name.startswith(f"standing_{side}_")
            and abs(spot['position'][1] - original[1]) <= crowd_config.level_tolerance_m]

    geometry_checks = {'unsupported': 0, 'solids': 0}

    @lru_cache(maxsize=None)
    def checked_placement(point, radius):
        if not supported_disc(point, radius, scene_data.get('support_surfaces', []), crowd_config):
            geometry_checks['unsupported'] += 1
            return False
        if not clears_solids([point], scene_data.get('solid_boxes', []),
                             radius + crowd_config.buffer_m):
            geometry_checks['solids'] += 1
            return False
        return True

    def placement_supported(point, radius):
        return checked_placement(tuple(point), radius)

    def leaves_meeting_room(chosen):
        predicted = {actor: dict(state) for actor, state in initial_states.items()}
        for actor, spot in chosen.items():
            predicted[actor]['position'] = list(spot['position'])
        return _joint_pair_centers(plan, predicted, scene_data, manifest, project_root,
                                    locked_actors, list(center_assignments), crowd_config, durations) is None

    from crowd.standing_layout import floor_candidates
    attempts = []
    chosen_spots = None
    seen = {actor: {tuple(spot['position']) for spot in options}
            for actor, options in candidate_sets.items()}
    for attempt in range(crowd_config.standing_search_levels + 1):
        if attempt:
            for setup, state, owner, name in adjustable:
                actor = setup.actor_id
                for spot in floor_candidates(scene_data, owner, name.split('_', 2)[1],
                                             spots[(owner, name)]['position'], crowd_config, attempt - 1):
                    point = tuple(spot['position'])
                    if point not in seen[actor]:
                        candidate_sets[actor].append(spot)
                        seen[actor].add(point)
        # Keep the marker-only fast path; then progressively widen both the
        # spatial sample and the joint search without relaxing clearances.
        search_config = replace(crowd_config,
                                layout_candidates=crowd_config.layout_candidates * 2 ** max(0, attempt - 1),
                                layout_beam_width=crowd_config.layout_beam_width * 2 ** max(0, attempt - 1))
        diagnostics = {'attempt': attempt,
                       'layout_candidates': search_config.layout_candidates,
                       'layout_beam_width': search_config.layout_beam_width}
        chosen_spots = assign_arenas(placement_groups, candidate_sets,
                                    [(point, radius) for _, point, radius in occupied],
                                    placement_supported, search_config,
                                    accept=leaves_meeting_room if center_assignments else None,
                                    diagnostics=diagnostics)
        attempts.append(diagnostics)
        if chosen_spots is not None:
            break
    if chosen_spots is None:
        return StructuredError(
            "CROWD_SPACING",
            "No collision-free supported standing layout found within the crowd search budget.",
            details={"actor_count": len(adjustable),
                     "layout_candidates": crowd_config.layout_candidates,
                     "layout_beam_width": crowd_config.layout_beam_width,
                     "standing_samples": crowd_config.standing_samples,
                     "standing_search_levels": crowd_config.standing_search_levels,
                     "geometry_rejections": geometry_checks,
                     "search_attempts": attempts,
                     "actor_reach_m": reaches,
                     "initial_placement_beats": {actor: [b.beat_id for b in placement_plan.beats
                                                         if b.actor_id == actor] for actor in reaches},
                     "candidate_counts": {actor: len(options) for actor, options in candidate_sets.items()}},
            suggestions=["Inspect candidate rejections and search pruning; increase standing_samples or "
                         "standing_search_levels, use more floor space, or choose shorter-reach actions. "
                         "A bounded search failure is not proof that no layout exists."],
        )
    assignments = []
    for setup, state, owner, name in adjustable:
        chosen = chosen_spots[setup.actor_id]
        reference = None if chosen.get('generated') else f"{owner}:{chosen['name']}"
        assignments.append((setup, state, reference, list(chosen["position"])))
    predicted = {actor: dict(state) for actor, state in initial_states.items()}
    for setup, state, reference, position in assignments:
        predicted[setup.actor_id]["position"] = position
    layout_error = _joint_pair_centers(plan, predicted, scene_data, manifest, project_root,
                                       locked_actors, center_assignments, crowd_config, durations)
    if layout_error:
        return layout_error
    for setup, state, reference, position in assignments:
        setup.at_reference = reference
        if reference is None:
            setup.position = list(position)
            setup.facing_deg = state.get('facing_deg', 0.)
        state["at_reference"] = reference
        state["position"] = position
    for beat, reference in center_assignments:
        beat.params["center"] = reference
        beat.params["center_auto"] = True
    for beat, reference in inherited_centers:
        beat.params['center'] = reference
    return None


def _joint_pair_centers(plan, states, scene, manifest, root, locked, assignments, config, durations=None):
    """Jointly allocate automatic arenas, keeping explicit/director positions intact.

    Distinct actor sets reserve separate arenas conservatively for the shot;
    repeated pairs reuse their arena. This does not infer missing prompt waves.
    """
    from crowd.arena_layout import assign_arenas
    from support_surfaces import contains
    if not states or not assignments:
        return None
    spots = {(str(s.get("owner")), s["name"]): s for s in scene.get("standing_spots", [])}
    groups, candidate_sets, key_for_beat = {}, {}, {}
    radii = {actor: _actor_reach(actor, state.get("asset_id", ""), plan, manifest, root,
                               not config.allow_actor_limb_contacts, durations)
             for actor, state in states.items() if not state.get("is_vehicle")}
    solid_radii = {actor: _actor_reach(actor, states[actor].get('asset_id', ''), plan, manifest, root,
                                    durations=durations)
                   for actor in radii}
    for beat, reference in assignments:
        participants = tuple(sorted(beat.params.get("participants") or []))
        if len(participants) != 2 or any(a not in states or a in locked for a in participants):
            continue
        owner, name = reference.split(":", 1)
        key = owner, participants
        key_for_beat[beat.beat_id] = key
        if key in groups: continue
        formation_radius = {"contact": .45, "combat": .55, "conversational": .85}.get(
            beat.params.get("spacing"), .85)
        groups[key] = {"key": key, "radius": formation_radius + max(radii[a] for a in participants),
                       "solid_radius": formation_radius + max(solid_radii[a] for a in participants),
                       "sources": [states[a]["position"] for a in participants],
                       "preferred": spots[(owner, name)]["position"]}
        # Automatic staging must stay on the participants' support level.
        # A requested center owned by another scene object is only a preference;
        # its owner must not force a crowd off its existing floor.
        candidate_sets[key] = [spot for spot in spots.values()
                               if all(abs(spot['position'][1]-states[a]['position'][1])
                                      <= config.level_tolerance_m for a in participants)]
    if not groups: return None
    moving = {a for _, participants in groups for a in participants}
    occupied = [(states[a]["position"], r) for a, r in radii.items() if a not in moving]
    supports = scene.get("support_surfaces", [])
    def supported(point, radius):
        if not supports: return True
        # Test the disc perimeter against the union, not each tessellated face.
        return all(any(contains(s, [point[0] + radius * math.cos(angle), point[1],
                                   point[2] + radius * math.sin(angle)]) for s in supports)
                   for angle in [i * math.tau / config.arena_perimeter_samples
                                 for i in range(config.arena_perimeter_samples)])
    chosen = assign_arenas(list(groups.values()), candidate_sets, occupied, supported, config)
    if chosen is None:
        return StructuredError("INTERACTION_LAYOUT_LIMIT", "No joint meeting layout found within the arena search budget.",
                               details={"groups": [str(k) for k in groups]},
                               suggestions=["Allow more floor space or stagger the interactions."])
    for index, (beat, reference) in enumerate(assignments):
        key = key_for_beat.get(beat.beat_id)
        if key in chosen:
            assignments[index] = beat, f"{chosen[key]['owner']}:{chosen[key]['name']}"
            beat.params['arena_layout_center'] = True
    return None
