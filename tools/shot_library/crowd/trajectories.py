"""Planning tracks share vehicle and occupant motion with final synthesis."""
from motion_synthesis import Keyframe, stair_route_geometry
from .st_planner import Reservation


def planning_tracks(timeline, states, resolver, synthesizer, scene, seed=42, beats=None):
    """Return physical vehicle/occupant prefixes followed by semantic moves.

    Vehicle exits cannot be estimated as a move to a door anchor: synthesis
    reserves room for the standing clip and follows the moving host. Ground
    routing must start from that same endpoint. No animation is baked here.
    """
    selected = list(timeline.beats if beats is None else beats)
    tracks = {a: [(0., *s['position'])] for a, s in states.items()}
    performances, prefix_ends = {}, {}
    if hasattr(synthesizer, 'synthesize_actor'):
        for actor, state in states.items():
            if not state.get('is_vehicle'):
                continue
            schedule = timeline.actor_schedules.get(actor, [])
            performance, error = synthesizer.synthesize_actor(
                actor, state, schedule, {}, performances, timeline.total_duration_s,
                seed=seed, ref_resolver=resolver.ref_resolver, scene_data=scene,
                all_initial_states=states)
            if error:
                return None, None, error
            if not performance.keyframes:
                performance.keyframes = [Keyframe(0., list(state['position']),
                                                  float(state.get('facing_deg', 0.)))]
            performances[actor] = performance
            prefix_ends[actor] = max((b.end_time for b in selected if b.actor_id == actor), default=0.)
        for actor, state in states.items():
            if state.get('is_vehicle'):
                continue
            coupling = [b for b in selected if b.actor_id == actor
                        and b.function in ('exit_vehicle', 'enter_vehicle', 'sit_in')]
            if not coupling:
                continue
            cutoff = max(b.end_time for b in coupling)
            performance, error = synthesizer.synthesize_actor(
                actor, state, timeline.actor_schedules.get(actor, []), {}, performances,
                timeline.total_duration_s, seed=seed, ref_resolver=resolver.ref_resolver,
                scene_data=scene, all_initial_states=states, stop_at_time=cutoff)
            if error:
                return None, None, error
            performances[actor] = performance
            prefix_ends[actor] = cutoff
        for actor, cutoff in prefix_ends.items():
            performance = performances[actor]
            tracks[actor].extend((k.time_s, *k.position) for k in performance.keyframes
                                 if k.time_s <= cutoff)
            position, _ = performance.sample(cutoff)
            tracks[actor].append((cutoff, *position))
            tracks[actor].sort(key=lambda p: p[0])
    for beat in sorted(selected, key=lambda b: (b.start_time, b.end_time)):
        if beat.actor_id in prefix_ends and beat.end_time <= prefix_ends[beat.actor_id] + 1e-9:
            continue
        if beat.function == 'climb_stairs' and hasattr(synthesizer, '_resolve_stair'):
            stair = synthesizer._resolve_stair(str(beat.params.get('stairs', '')))
            if stair and len(stair['data'].get('centerline', [])) >= 2:
                _, _, arrival = stair_route_geometry(
                    stair['data'], beat.params.get('direction', 'up'),
                    float(beat.params.get('stair_lane_offset_m', 0.)),
                    float(beat.params.get('stair_exit_distance_m', 0.)))
                tracks[beat.actor_id].append((beat.end_time, *arrival))
                tracks[beat.actor_id].sort(key=lambda p: p[0])
                continue
        resolver._update_provisional_trajectory(
            beat.actor_id, beat.start_time, beat.end_time, beat, tracks, states)
    return tracks, {a: p for a, p in performances.items() if p.is_vehicle}, None


def vehicle_reservations(tracks, vehicles, synthesizer, extra_clearance=0.):
    """Moving solids protect full-body reach even when actor limb contact is allowed."""
    return [Reservation(actor, tracks[actor], synthesizer._moving_body_radius(perf.asset_id)
                        + max(0., extra_clearance))
            for actor, perf in vehicles.items()]
