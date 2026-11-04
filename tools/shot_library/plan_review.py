"""Read-only review of explicit prompt timing and directional staging.

This is a bounded English grammar, not unrestricted natural-language proof.
Action vocabulary comes from the plan's functions and semantic parameters;
scene nouns come from measured object names and manifest aliases/tags.
Unresolved explicit directives are reported rather than silently certified.
"""
from __future__ import annotations

import copy
import math
import re

from interaction_plan import apply_interactions
from support_surfaces import STANDING_BODY_HEIGHT_M, WALK_OVER_MAX_HEIGHT_M


# English choreography grammar. These rules do not depend on a cast, scene,
# particular action, or a requested number of people.
SIMULTANEOUS = re.compile(r'\b(?:together|simultaneously|at the same time|in unison)\b', re.I)
SEQUENCE = re.compile(r'\b(?:one (?:by one|at a time)|in turns|take turns|taking turns|in succession)\b', re.I)
PHASE_SPLIT = re.compile(r'\s*(?:[;.\n]+|\b(?:and then|then|afterwards|subsequently)\b)\s*', re.I)
SOURCE = re.compile(r'\bfrom\s+(?:the\s+)?(?:direction|side)\s+of\s+(?:the\s+)?([^,;.]+)', re.I)
ORIGIN_LAYOUT_CUES = {
    'opposite': re.compile(r'\b(?:opposites? sides?|opposing (?:sides?|directions?))\b', re.I),
    'surrounding': re.compile(r'\bfrom\s+(?:all sides|all directions|every direction)\b', re.I),
}
CORNER_ROUTE = re.compile(r'\baround\s+[^,;.]*?\bcorners?\b', re.I)
ALL_FINISHED = re.compile(r'\bafter\s+(?:everyone|everybody|all(?:\s+\w+)?)\s+(?:has\s+|have\s+)?(?:arriv\w*|finish\w*|reach\w*)', re.I)
MOTION_FUNCTIONS = frozenset(('walk_to', 'run_to', 'jog_to', 'sprint_to', 'approach',
                              'sneak_to', 'crawl_to', 'drive_to', 'fly_to', 'swim_to'))
FUNCTION_WORD_ALIASES = {
    'converse': ('talk', 'chat', 'speak'), 'defend_or_block': ('defend', 'block'),
    'hug_or_kiss': ('hug', 'kiss'), 'shake_hands': ('handshake',),
}
# Numerical tolerance for comparisons of world positions, in metres.
POSITION_EPSILON_M = 1e-6


def _forms(word):
    word = word.casefold()
    forms = {word, word + 's', word + 'ed', word + 'ing', word + 'es'}
    if word.endswith('e'):
        forms.add(word[:-1] + 'ing')
        forms.add(word + 'd')
    if len(word) > 2 and word[-1] not in 'aeiouwxy' and word[-2] in 'aeiou':
        forms.add(word + word[-1] + 'ing')
    return forms


def _vocabulary(plan):
    words = {}
    for beat in plan.beats:
        roots = set(beat.function.split('_')) - {'to', 'from', 'on', 'or', 'while', 'in', 'at'}
        roots.update(FUNCTION_WORD_ALIASES.get(beat.function, ()))
        if beat.function in MOTION_FUNCTIONS:
            roots.update(('move', 'approach', 'depart', 'leave', 'come'))
        for key in ('activity', 'action', 'gait'):
            value = beat.params.get(key)
            if isinstance(value, str):
                roots.update(value.split('_'))
        roots.difference_update(('to', 'from', 'on', 'or', 'while', 'in', 'at'))
        for root in roots:
            for form in _forms(root):
                words.setdefault(form, set()).add(beat.function)
    return words


def _matches(text, vocabulary):
    return [(match.start(), match.end(), vocabulary[match.group().casefold()])
            for match in re.finditer(r'\b\w+\b', text)
            if match.group().casefold() in vocabulary]


def _actors_in(text, states):
    mentioned = set()
    for actor, state in states.items():
        for alias in (actor, state.get('label')):
            if not alias:
                continue
            # Single-letter actor names must be capitalized to distinguish
            # the label A from the English article "a".
            pattern = r'\b' + re.escape(str(alias).upper() if len(str(alias)) == 1 else str(alias)) + r'\b'
            flags = 0 if len(str(alias)) == 1 else re.I
            if re.search(pattern, text, flags):
                mentioned.add(actor)
    return mentioned


def _initial_states(plan, scene, manifest):
    from semantic_references import ReferenceResolver
    resolver = ReferenceResolver(manifest, scene)
    states = {}
    for setup in plan.setup:
        if setup.action_type != 'place':
            continue
        state = {'label': setup.label, 'asset_id': setup.asset_id,
                 'facing_deg': setup.facing_deg or 0.}
        if setup.at_reference:
            target, _ = resolver.resolve(setup.at_reference, states)
            if target is not None:
                state['position'] = list(target.position)
                state['facing_deg'] = target.facing_deg
        elif setup.position is not None:
            state['position'] = list(setup.position)
        states[setup.actor_id] = state
    return states


def _dependencies(plan):
    graph = {beat.beat_id: set(beat.params.get('wait_for', [])) for beat in plan.beats}
    previous = {}
    for beat in plan.beats:
        if beat.actor_id in previous:
            graph[beat.beat_id].add(previous[beat.actor_id])
        previous[beat.actor_id] = beat.beat_id
        condition = beat.start_condition
        if condition and condition.type == 'after':
            reference = str(condition.value)
            parent = reference if reference in graph else reference.rsplit('.', 1)[0]
            if parent in graph:
                graph[beat.beat_id].add(parent)
    def ancestors(beat_id):
        result, pending = set(), list(graph.get(beat_id, ()))
        while pending:
            parent = pending.pop()
            if parent not in result:
                result.add(parent)
                pending.extend(graph.get(parent, ()))
        return result
    return {beat: ancestors(beat) for beat in graph}


def _source_objects(noun, scene, manifest):
    assets = {asset.get('id'): asset for asset in manifest.get('assets', [])}
    matches = []
    def normalize(text):
        text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', str(text))
        return ' '.join(re.findall(r'\w+', text.replace('_', ' ').casefold()))
    noun = normalize(noun)
    for obj in scene.get('objects', []):
        asset = assets.get(obj.get('asset') or obj.get('asset_id'), {})
        aliases = [obj.get('name') or obj.get('id'), obj.get('asset') or obj.get('asset_id')]
        aliases += list(asset.get('aliases', [])) + list(asset.get('tags', []))
        score = max((len(normalize(alias)) for alias in aliases if alias
                     and any(re.match(re.escape(form) + r'\b', noun)
                             for form in _forms(normalize(alias)))), default=0)
        if score:
            matches.append((score, obj))
    best = max((score for score, _ in matches), default=0)
    return [obj for score, obj in matches if score == best]


def _direction_check(beat, states, scene, manifest, objects, plan):
    from semantic_references import ReferenceResolver
    state = states.get(beat.actor_id, {})
    if 'position' not in state:
        return False, 'Starting reference has no resolved position.'
    prior = [item for item in plan.beats[:plan.beats.index(beat)] if item.actor_id == beat.actor_id]
    if any(item.function not in ('wait', 'idle', 'turn_to_face') for item in prior):
        return False, 'Source after preceding actions needs a resolved trajectory; it cannot be verified from initial placement.'
    source = state['position']
    path = beat.params.get('brief_path', {}).get('points', [])
    if path:
        source, goal = path[0], path[-1]
    else:
        target, error = ReferenceResolver(manifest, scene).resolve(beat.params.get('target'), states,
                                                                 source_actor_id=beat.actor_id)
        if target is None or target.target_type == 'actor':
            return False, 'Requested arrival direction is unresolved or depends on another actor trajectory.'
        goal = target.position
    regions = []
    for obj in objects:
        owner = obj.get('name') or obj.get('id')
        boxes = [box for box in scene.get('solid_boxes', []) if box.get('owner') == owner
                 and box['max'][1] - box['min'][1] > WALK_OVER_MAX_HEIGHT_M
                 and box['min'][1] < source[1] + STANDING_BODY_HEIGHT_M
                 and box['max'][1] > source[1] + WALK_OVER_MAX_HEIGHT_M]
        if boxes:
            regions.extend((box['min'][0], box['max'][0], box['min'][2], box['max'][2]) for box in boxes)
        elif obj.get('position') is not None:
            x, _, z = obj['position']
            regions.append((x, x, z, z))
    def nearest(point, region):
        x0, x1, z0, z1 = region
        return [min(x1, max(x0, point[0])), min(z1, max(z0, point[2]))]
    if not regions:
        return False, 'Named source has no measured position or obstacle footprint.'
    region = min(regions, key=lambda region: math.dist([source[0], source[2]], nearest(source, region)))
    source_distance = math.dist([source[0], source[2]], nearest(source, region))
    goal_distance = math.dist([goal[0], goal[2]], nearest(goal, region))
    toward_source = [a - b for a, b in zip(nearest(goal, region), [goal[0], goal[2]])]
    start_side = [source[0] - goal[0], source[2] - goal[2]]
    valid = (source_distance + POSITION_EPSILON_M < goal_distance
             and sum(a * b for a, b in zip(toward_source, start_side)) > POSITION_EPSILON_M)
    return valid, {'source_position': list(source), 'arrival_position': list(goal),
                   'message': 'The start must be on the named source side of the arrival and closer to its measured footprint.',
                   'source_distance_m': source_distance, 'arrival_distance_m': goal_distance,
                   'geometry_basis': 'measured obstacle footprint' if any(region[0] != region[1] or region[2] != region[3] for region in regions)
                   else 'scene object position'}


def review_plan(plan, scene=None, manifest=None, initial_states=None, locked_actors=()):
    """Return JSON feedback, without editing the input or compiling/simulating it."""
    from crowd.movements import movement_source_plan
    scene, manifest = scene or {}, manifest or {}
    source = movement_source_plan(plan)
    states = copy.deepcopy(initial_states) if initial_states is not None else _initial_states(source, scene, manifest)
    # Existing actors referenced without setup still participate in timing checks.
    for beat in source.beats:
        states.setdefault(beat.actor_id, {})
    report = {'passed': True, 'requirements': [], 'violations': [], 'overrides': [],
              'pending_spatial_bindings': [],
              'scope': 'explicit English timing cues, ordered group phases and from-direction clauses'}
    from spatial_regions import derive_spatial_regions, intent_source_ids, valid_destination
    available = scene.get('spatial_regions')
    region_index = {region['id']: region for region in
                    (available if available is not None else derive_spatial_regions(scene, manifest))}
    pending = {}
    if source.spatial_intents:
        for intent in source.spatial_intents:
            a, b = region_index.get(intent.source_region), region_index.get(intent.destination_region)
            try:
                origins = intent_source_ids(intent, region_index)
                valid_regions = all(key in region_index for key in list(origins.values()) + intent.via_regions)
            except (KeyError, ValueError):
                origins, valid_regions = {}, False
            if not a or not valid_destination(b, region_index) or not valid_regions:
                report['violations'].append({'code': 'SPATIAL_REGION_REFERENCE',
                                              'reason': 'Use current scene-owned origin, route and floor or boundary entry destination IDs.',
                                              'intent_id': intent.intent_id})
            for actor, movement in intent.movements.items():
                if not intent.bindings.get(actor):
                    pending[movement] = (intent, region_index.get(origins.get(actor)), b)
                    report['pending_spatial_bindings'].append({'intent_id': intent.intent_id,
                                                               'actor_id': actor, 'beat_id': movement})
    report['passed'] = not report['violations']
    if not source.prompt.strip():
        return report
    # These directives require an explicit contract. Timing or mere collision
    # freedom cannot establish where actors came from or which corner they used.
    all_moving = {beat.actor_id for beat in source.beats if beat.function in MOTION_FUNCTIONS}
    def directive_actors(match):
        before = re.split(r'[,;.\n]', source.prompt[:match.start()])[-1]
        mentioned = _actors_in(before, states)
        relevant = all_moving & mentioned if mentioned else all_moving
        return relevant, relevant - set(locked_actors)

    def positive_match(pattern):
        return next((match for match in pattern.finditer(source.prompt)
                     if not re.search(r'\b(?:not|never|without)\s+(?:from\s+)?$',
                                      source.prompt[:match.start()], re.I)), None)
    for kind, pattern in ORIGIN_LAYOUT_CUES.items():
        match = positive_match(pattern)
        if match:
            relevant, moving = directive_actors(match)
            covered = {actor for intent in source.spatial_intents if intent.origin_layout == kind
                       for actor in intent.actors}
            requirement = {'kind': 'origin_layout', 'layout': kind, 'evidence': match.group(),
                           'actors': sorted(moving)}
            report['requirements'].append(requirement)
            if moving != relevant:
                report['overrides'].append({**requirement, 'actors': sorted(relevant - moving),
                                            'reason': 'Explicit Director tracks take precedence.'})
            if not relevant or not moving.issubset(covered):
                report['violations'].append({**requirement, 'code': 'SPATIAL_ORIGIN_CONTRACT',
                    'reason': 'Declare the requested origin_layout for all approaching actors.'})
    match = positive_match(CORNER_ROUTE)
    if match:
        relevant, moving = directive_actors(match)
        covered = {actor for intent in source.spatial_intents
                   if any(region_index.get(key, {}).get('kind') == 'corner' for key in intent.via_regions)
                   for actor in intent.actors}
        requirement = {'kind': 'route_via_corner', 'evidence': match.group(), 'actors': sorted(moving)}
        report['requirements'].append(requirement)
        if moving != relevant:
            report['overrides'].append({**requirement, 'actors': sorted(relevant - moving),
                                        'reason': 'Explicit Director tracks take precedence.'})
        if not relevant or not moving.issubset(covered):
            report['violations'].append({**requirement, 'code': 'SPATIAL_ROUTE_CONTRACT',
                'reason': 'Declare ordered via_regions containing measured corner areas for the approaching actors.'})
    report['passed'] = not report['violations']
    original = copy.deepcopy(source)
    error = apply_interactions(source, states)
    if error:
        report['passed'] = False
        report['violations'].append({'code': 'INVALID_INTERACTION', 'error': error.to_dict()})
        return report
    ancestors = _dependencies(source)
    vocabulary = _vocabulary(source)
    occurrences = {}
    previous_group = []
    previous_ordered = False
    clauses = []
    cursor, ordered = 0, False
    for delimiter in PHASE_SPLIT.finditer(source.prompt):
        clauses.append((source.prompt[cursor:delimiter.start()], ordered))
        ordered = bool(re.search(r'\b(?:then|afterwards|subsequently)\b', delimiter.group(), re.I))
        cursor = delimiter.end()
    clauses.append((source.prompt[cursor:], ordered))
    locked = set(locked_actors)

    def check(kind, clause, beats, valid, reason):
        requirement = {'kind': kind, 'evidence': clause, 'beat_ids': [b.beat_id for b in beats],
                       'actors': [b.actor_id for b in beats]}
        report['requirements'].append(requirement)
        if not valid:
            report['violations'].append({**requirement, 'code': kind.upper(), 'reason': reason})

    for clause, ordered in clauses:
        if not clause.strip():
            continue
        source_match = SOURCE.search(clause)
        # Arrival qualifiers constrain a later phase, not its action vocabulary.
        action_text = ALL_FINISHED.split(clause)[0]
        matches = _matches(action_text, vocabulary)
        cues = [(match, 'simultaneous') for match in SIMULTANEOUS.finditer(action_text)]
        cues += [(match, 'sequence') for match in SEQUENCE.finditer(action_text)]
        active_cues = [(cue, kind) for cue, kind in cues
                       if not re.search(r'\b(?:not|never|without)\s+(?:\w+\s+)?$', action_text[:cue.start()], re.I)]
        if cues and not active_cues and not source_match:
            continue
        functions = set()
        if active_cues and matches:
            cue, _ = min(active_cues, key=lambda item: item[0].start())
            before = [match for match in matches if match[1] <= cue.start()]
            chosen = max(before, key=lambda match: match[1]) if before else min(matches, key=lambda match: match[0])
            functions = chosen[2]
        elif matches:
            functions = matches[0][2]
        elif source_match:
            functions = {beat.function for beat in source.beats if beat.function in MOTION_FUNCTIONS}
        mentioned = _actors_in(clause, states)
        functions_by_actor = {}
        if active_cues:
            cue = min(active_cues, key=lambda item: item[0].start())[0]
            eligible = [match for match in matches if match[1] <= cue.start()] or matches
            previous_end = 0
            complete = True
            for begin, end, action_functions in eligible:
                subjects = _actors_in(action_text[previous_end:begin], states)
                if not subjects:
                    complete = False
                    break
                for actor in subjects:
                    functions_by_actor.setdefault(actor, set()).update(action_functions)
                previous_end = end
            if not complete:
                functions_by_actor = {}
        selected = []
        for actor in states:
            if functions_by_actor and actor not in functions_by_actor:
                continue
            if mentioned and actor not in mentioned:
                continue
            actor_functions = functions_by_actor.get(actor, functions)
            candidates = [beat for beat in original.beats if beat.actor_id == actor and beat.function in actor_functions]
            key = (actor, tuple(sorted(actor_functions)))
            occurrence = occurrences.get(key, 0)
            if occurrence < len(candidates):
                selected.append(candidates[occurrence])
                occurrences[key] = occurrence + 1
        overridden = [beat for beat in selected if beat.actor_id in locked]
        if overridden:
            report['overrides'].append({'evidence': clause, 'actors': [b.actor_id for b in overridden],
                                        'reason': 'Explicit Director tracks take precedence over prose.'})
        selected = [beat for beat in selected if beat.actor_id not in locked]
        if active_cues and mentioned:
            missing = mentioned - {beat.actor_id for beat in selected} - locked
            if missing:
                check('missing_group_action', clause, selected, False,
                      f"Named participants have no corresponding action in this phase: {', '.join(sorted(missing))}.")
        for cue, kind in active_cues:
            if not selected and not overridden:
                check(kind, clause, [], False, 'Cannot ground the explicit timing cue to action calls. Supply unambiguous actions and declarations.')
                continue
            ids = {beat.beat_id for beat in selected}
            valid = len(selected) <= 1
            if kind == 'simultaneous' and not valid:
                valid = any(interaction.timing == 'simultaneous' and ids.issubset(interaction.actions)
                            for interaction in source.interactions)
            elif kind == 'sequence' and not valid:
                valid = all(a.beat_id in ancestors[b.beat_id] or b.beat_id in ancestors[a.beat_id]
                            for i, a in enumerate(selected) for b in selected[i + 1:])
            check(kind, clause, selected, valid,
                  'Use an explicit simultaneous interaction for equal starts; preserve one-by-one requests as completion dependencies.')
        shared_later = any(kind == 'simultaneous' for _, kind in active_cues)
        if selected and previous_group and (ALL_FINISHED.search(clause) or
                                            (ordered and (previous_ordered or shared_later)
                                             and len(previous_group) > 1 and len(selected) > 1)):
            valid = all(first.beat_id in ancestors[second.beat_id]
                        for first in previous_group for second in selected if first.beat_id != second.beat_id)
            check('phase_barrier', clause, selected, valid,
                  {'message': 'Every later action must depend on every preceding arrival/completion.',
                   'predecessors': [beat.beat_id for beat in previous_group], 'suggestion': 'Link phase declarations with after.'})
        if source_match:
            objects = _source_objects(source_match.group(1), scene, manifest)
            approaches = []
            for actor in states:
                if actor in locked or (mentioned and actor not in mentioned):
                    continue
                move = next((beat for beat in original.beats
                             if beat.actor_id == actor and beat.function in MOTION_FUNCTIONS), None)
                if move:
                    approaches.append(move)
            if not approaches and overridden:
                continue
            if not objects or not approaches:
                check('source_direction', clause, approaches, False,
                      'Named source or approaching actions cannot be grounded in scene data; do not substitute unrelated street markers.')
            else:
                for beat in approaches:
                    if beat.beat_id in pending:
                        intent, region, destination = pending[beat.beat_id]
                        owners = {obj.get('name') or obj.get('id') for obj in objects}
                        valid = bool(region and destination and region.get('source_owner') in owners)
                        reason = {'message': 'Spatial binding is delegated to the compiler using the declared source owner.',
                                  'intent_id': intent.intent_id, 'source_region': intent.source_region}
                    else:
                        valid, reason = _direction_check(beat, states, scene, manifest, objects, original)
                    check('source_direction', clause, [beat], valid, reason)
            if approaches and selected and shared_later and not any(beat in approaches for beat in selected):
                check('phase_barrier', clause, selected,
                      all(first.beat_id in ancestors[second.beat_id] for first in approaches for second in selected),
                      'The shared performance must wait for every approach to finish.')
        if selected:
            previous_group = selected
            previous_ordered = any(kind == 'simultaneous' for _, kind in active_cues) and len(selected) > 1
    report['passed'] = not report['violations']
    return report


def review_suggestions(report):
    """Qualitative feedback survives the existing symbolic retry filter.

    Detailed coordinates remain in the local report. Action IDs, evidence and
    recovery advice go back to the LLM, including on automatic retries.
    """
    suggestions = []
    for violation in report.get('violations', []):
        reason = violation.get('reason', violation.get('error', {}).get('message', 'Revise the interaction declaration.'))
        if isinstance(reason, dict):
            reason = reason.get('message', str(reason))
        if violation['code'] == 'SOURCE_DIRECTION':
            reason += (' Use stage_region_approach with listed spatial_regions to delegate source/arrival placement '
                       'to the compiler. If no compatible regions are listed, report that limitation; do not invent IDs.')
        error = violation.get('error', {})
        actions = violation.get('beat_ids') or [
            action.get('reference') or action.get('beat_id')
            for action in error.get('details', {}).get('conflicting_actions', [])]
        action_text = f" Actions: {', '.join(actions)}." if actions else ''
        suggestions.append(f"{violation['code']}: {violation.get('evidence', '')}{action_text} {reason}")
        suggestions.extend(error.get('suggestions', []))
    return suggestions
