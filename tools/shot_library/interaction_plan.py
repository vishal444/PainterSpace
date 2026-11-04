"""Resolve declared interaction intent into timeline constraints and checks."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Optional

from plan_model import Beat, ShotPlan, StructuredError, TimingCondition, INTERACTION_TIMINGS


# Scheduling arithmetic uses seconds; microsecond differences are roundoff,
# not meaningful choreography differences (the timeline is serialized in ms).
INTERACTION_TIME_TOLERANCE_S = 1e-6


_ACTION_REF = re.compile(r"^(.+)\.([A-Za-z][A-Za-z0-9_]*)(?:#([1-9][0-9]*))?$")


def _actor_aliases(actor_states: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    aliases = {actor_id.casefold(): actor_id for actor_id in actor_states}
    for actor_id, state in actor_states.items():
        label = state.get("label")
        if label:
            aliases[str(label).casefold()] = actor_id
    return aliases


def _resolve_actions(plan: ShotPlan, interaction, aliases: Dict[str, str]):
    by_id = {beat.beat_id: beat for beat in plan.beats}
    resolved: List[Optional[Beat]] = [None] * len(interaction.actions)
    pending = {}
    for index, reference in enumerate(interaction.actions):
        if reference in by_id:
            resolved[index] = by_id[reference]
            continue
        match = _ACTION_REF.fullmatch(reference)
        if not match:
            return [], StructuredError(
                "INTERACTION_ACTION_REFERENCE",
                f"Invalid action reference '{reference}' in '{interaction.interaction_id}'.")
        actor_ref, function, occurrence = match.groups()
        actor_id = aliases.get(actor_ref.casefold())
        candidates = [beat for beat in plan.beats
                      if beat.actor_id == actor_id
                      and function in (beat.function, beat.source_function)]
        if not candidates or (occurrence and int(occurrence) > len(candidates)):
            return [], StructuredError(
                "INTERACTION_ACTION_REFERENCE",
                f"Action '{reference}' is absent from '{interaction.interaction_id}'.")
        if occurrence:
            resolved[index] = candidates[int(occurrence) - 1]
        else:
            key = (actor_id, function)
            pending.setdefault(key, ([], candidates))[0].append(index)
    # Resolve exact IDs and explicit occurrences first, independently of their
    # position in the declaration. Bare references can then consume all the
    # remaining occurrences in authored beat order without guessing a subset.
    reserved = {beat.beat_id for beat in resolved if beat is not None}
    for (actor_id, function), (indices, candidates) in pending.items():
        remaining = [beat for beat in candidates if beat.beat_id not in reserved]
        if len(indices) != len(remaining):
            return [], StructuredError(
                "INTERACTION_ACTION_REFERENCE",
                f"Action '{actor_id}.{function}' is ambiguous; select occurrences with #1, #2, and so on.",
                details={'interaction_id': interaction.interaction_id,
                         'action': f'{actor_id}.{function}',
                         'unnumbered_references': len(indices),
                         'remaining_beat_ids': [beat.beat_id for beat in remaining]})
        for index, beat in zip(indices, remaining):
            resolved[index] = beat
    return resolved, None


def check_overlap_actors(interaction, plan, actor_states, resolved_actions=None):
    """Reject known actor conflicts, even when a declaration precedes its actions.

    Unknown exact beat IDs are deferred until the complete plan is resolved.
    Symbolic actor.function references can be checked before any actions exist.
    """
    if interaction.timing not in ('overlap', 'simultaneous'):
        return None
    aliases = _actor_aliases(actor_states)
    by_id = {beat.beat_id: beat for beat in plan.beats}
    by_actor = {}
    for index, reference in enumerate(interaction.actions):
        beat = resolved_actions[index] if resolved_actions is not None else by_id.get(reference)
        if beat is not None:
            actor = beat.actor_id
            action = {'reference': reference, 'beat_id': beat.beat_id, 'function': beat.function}
        else:
            match = _ACTION_REF.fullmatch(reference)
            if match is None:
                continue
            actor_ref, function, _ = match.groups()
            actor = aliases.get(actor_ref.casefold(), actor_ref.casefold())
            action = {'reference': reference, 'beat_id': None, 'function': function}
        by_actor.setdefault(actor, []).append(action)
    for actor, actions in by_actor.items():
        if len(actions) > 1:
            names = ', '.join(action['reference'] for action in actions)
            return StructuredError(
                'INTERACTION_OVERLAP',
                f"Interaction '{interaction.interaction_id}' requires actor '{actor}' to perform "
                f"multiple full-body actions concurrently: {names}.",
                actor_id=actor, beat_id=actions[1]['beat_id'],
                details={'interaction_id': interaction.interaction_id, 'timing': interaction.timing,
                         'conflicting_actions': actions},
                suggestions=[
                    'Keep at most one action per actor in each overlap or simultaneous phase. '
                    "Put that actor's later actions in separate interactions and link them with after. "
                    "Keep different actors' concurrent actions together; preserve explicit prompt timing."])
    return None


def apply_interactions(plan: ShotPlan, actor_states: Dict[str, Dict[str, Any]]) -> Optional[StructuredError]:
    """Attach causal constraints before timeline resolution; reject ambiguous intent."""
    aliases = _actor_aliases(actor_states)
    seen_ids = set()
    actions_by_interaction = {}
    for interaction in plan.interactions:
        if interaction.interaction_id in seen_ids or not interaction.interaction_id:
            return StructuredError("INTERACTION_ID", "Interaction IDs must be nonempty and unique.")
        seen_ids.add(interaction.interaction_id)
        if interaction.timing not in INTERACTION_TIMINGS or not interaction.actions:
            return StructuredError("INTERACTION_SCHEMA", "Interaction timing must be sequence, overlap, parallel or simultaneous with at least one action.")
        if not isinstance(interaction.after, list) or any(not isinstance(parent, str) or not parent for parent in interaction.after):
            return StructuredError('INTERACTION_SCHEMA', 'after must be a list of nonempty interaction IDs.')
        participants = [aliases.get(str(name).casefold()) for name in interaction.participants]
        if not participants or any(actor_id is None for actor_id in participants) or len(set(participants)) != len(participants):
            return StructuredError("INTERACTION_PARTICIPANTS", f"Unknown or repeated participants in '{interaction.interaction_id}'.")
        actions, error = _resolve_actions(plan, interaction, aliases)
        if error:
            return error
        if len({beat.beat_id for beat in actions}) != len(actions):
            return StructuredError("INTERACTION_ACTION_REFERENCE", f"Repeated action in '{interaction.interaction_id}'.")
        if any(beat.actor_id not in participants for beat in actions):
            return StructuredError("INTERACTION_PARTICIPANTS", f"Action actor is not a participant in '{interaction.interaction_id}'.")
        overlap_error = check_overlap_actors(interaction, plan, actor_states, actions)
        if overlap_error:
            return overlap_error
        # Later scheduling passes may add actions of the same function. Bind
        # the declaration to these exact beats before that happens.
        interaction.actions = [beat.beat_id for beat in actions]
        actions_by_interaction[interaction.interaction_id] = actions
        if interaction.timing == "sequence":
            for predecessor, following in zip(actions, actions[1:]):
                wait_for = following.params.setdefault("wait_for", [])
                if predecessor.beat_id not in wait_for:
                    wait_for.append(predecessor.beat_id)
        elif interaction.timing in ('overlap', 'simultaneous'):
            anchor = actions[0]
            if interaction.timing == 'simultaneous':
                # Everyone must be ready before anyone starts. An actor with
                # a longer preceding action must also delay the phase anchor.
                action_ids = {beat.beat_id for beat in actions}
                prior_by_actor = {}
                readiness = set()
                for beat in plan.beats:
                    if beat.beat_id in action_ids:
                        if beat.actor_id in prior_by_actor:
                            readiness.add(prior_by_actor[beat.actor_id])
                        readiness.update(beat.params.get('wait_for', []))
                    prior_by_actor[beat.actor_id] = beat.beat_id
                if readiness.intersection(action_ids):
                    return StructuredError('INTERACTION_SIMULTANEOUS',
                                           'Simultaneous actions cannot wait for one another to finish.',
                                           details={'interaction_id': interaction.interaction_id})
                for beat in actions:
                    wait_for = beat.params.setdefault('wait_for', [])
                    for parent in sorted(readiness):
                        if parent not in wait_for:
                            wait_for.append(parent)
                authored_starts = [float(beat.start_condition.value) for beat in actions
                                   if beat.start_condition and beat.start_condition.type == 'at']
                if authored_starts:
                    for beat in actions:
                        if beat.start_condition is None:
                            beat.start_condition = TimingCondition('at', max(authored_starts))
            for beat in actions[1:]:
                if interaction.timing == 'overlap' and beat.start_condition is None and not beat.params.get("exact_window"):
                    beat.start_condition = TimingCondition("with", f"{anchor.beat_id}.started")
    # Bind after all declarations have resolved so call order is immaterial.
    # Every action in a later phase waits for every action in its predecessors.
    for interaction in plan.interactions:
        for parent in interaction.after:
            if parent not in actions_by_interaction or parent == interaction.interaction_id:
                return StructuredError('INTERACTION_DEPENDENCY', 'Unknown or self-referencing interaction predecessor.',
                                       details={'interaction_id': interaction.interaction_id, 'predecessor': parent})
            predecessors = actions_by_interaction[parent]
            for action in actions_by_interaction[interaction.interaction_id]:
                if any(beat.beat_id == action.beat_id for beat in predecessors):
                    return StructuredError('INTERACTION_DEPENDENCY', 'Successive phases cannot share the same action.',
                                           beat_id=action.beat_id,
                                           details={'interaction_id': interaction.interaction_id, 'predecessor': parent})
                wait_for = action.params.setdefault('wait_for', [])
                for beat in predecessors:
                    if beat.beat_id not in wait_for:
                        wait_for.append(beat.beat_id)
    return None


def validate_interactions(plan: ShotPlan, timeline, actor_states: Dict[str, Dict[str, Any]]) -> Optional[StructuredError]:
    """Check final scheduled times after all other planning passes."""
    aliases = _actor_aliases(actor_states)
    scheduled = {beat.beat_id: beat for beat in timeline.beats}
    resolved_interactions = {}
    for interaction in plan.interactions:
        actions, error = _resolve_actions(plan, interaction, aliases)
        if error:
            return error
        resolved = [_action_interval(beat, scheduled, timeline.beats) for beat in actions]
        if any(beat is None for beat in resolved):
            return StructuredError("INTERACTION_TIMING", f"An action in '{interaction.interaction_id}' is missing from the final timeline.",
                                   details={'interaction_id': interaction.interaction_id,
                                            'missing_beats': [beat.beat_id for beat in actions if beat.beat_id not in scheduled]})
        resolved_interactions[interaction.interaction_id] = resolved
        if interaction.timing == "sequence":
            for first, second in zip(resolved, resolved[1:]):
                if first.end_time > second.start_time + 1e-6:
                    return StructuredError("INTERACTION_TIMING", f"Actions in '{interaction.interaction_id}' are out of order.",
                                           beat_id=second.beat_id, actor_id=second.actor_id,
                                           details={'interaction_id': interaction.interaction_id,
                                                    'predecessor': _action_times(first), 'following': _action_times(second),
                                                    'overlap_s': first.end_time - second.start_time})
        elif interaction.timing == 'simultaneous':
            earliest = min(beat.start_time for beat in resolved)
            latest = max(beat.start_time for beat in resolved)
            if latest - earliest > INTERACTION_TIME_TOLERANCE_S:
                return StructuredError('INTERACTION_SIMULTANEOUS',
                                       f"Actions in '{interaction.interaction_id}' do not start at the same time.",
                                       details={'interaction_id': interaction.interaction_id,
                                                'start_spread_s': latest - earliest,
                                                'actions': [_action_times(beat) for beat in resolved]},
                                       suggestions=['Find routes with a shared departure time or resolve conflicting authored timing; '
                                                    'use parallel timing only if staggered departures are acceptable.'])
        elif interaction.timing == 'overlap' and max(beat.start_time for beat in resolved) >= min(beat.end_time for beat in resolved) - 1e-6:
            return StructuredError("INTERACTION_TIMING", f"Actions in '{interaction.interaction_id}' do not overlap.",
                                   details={'interaction_id': interaction.interaction_id,
                                            'actions': [_action_times(beat) for beat in resolved]})
    for interaction in plan.interactions:
        for parent in interaction.after:
            if parent not in resolved_interactions:
                return StructuredError('INTERACTION_DEPENDENCY', 'Unknown interaction predecessor.',
                                       details={'interaction_id': interaction.interaction_id, 'predecessor': parent})
            predecessor = max(resolved_interactions[parent], key=lambda beat: beat.end_time)
            following = min(resolved_interactions[interaction.interaction_id], key=lambda beat: beat.start_time)
            if predecessor.end_time > following.start_time + 1e-6:
                return StructuredError('INTERACTION_TIMING', 'An interaction starts before its preceding phase finishes.',
                                       beat_id=following.beat_id, actor_id=following.actor_id,
                                       details={'interaction_id': interaction.interaction_id, 'after': parent,
                                                'predecessor': _action_times(predecessor),
                                                'following': _action_times(following),
                                                'overlap_s': predecessor.end_time - following.start_time})
    return None


def _action_times(beat):
    return {'beat_id': beat.beat_id, 'actor_id': beat.actor_id, 'function': beat.function,
            'start_time_s': beat.start_time, 'end_time_s': beat.end_time}


def _action_interval(action, scheduled, timeline_beats):
    """Public movement IDs name arrival, but start means actual departure.

    Routing can replace a walk by initial waits and multiple moving segments.
    Checking only its final segment would both hide staggered departures and
    incorrectly reject simultaneous routes of different lengths.
    """
    beat = scheduled.get(action.beat_id)
    if beat is None:
        return None
    segments = [part for part in timeline_beats
                if part.actor_id == action.actor_id
                and part.params.get('auto_crowd_movement') == action.beat_id]
    moving = [part for part in segments if part.function != 'wait']
    if not moving:
        return beat
    start = min(part.start_time for part in moving)
    end = max(part.end_time for part in segments)
    return replace(beat, start_time=start, end_time=end, duration=end - start)
