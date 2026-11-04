"""Continuously check synthesized roots before an expensive animation bake."""
from dataclasses import replace
from .config import CrowdSolverConfig
from .st_planner import Reservation
from .joint import first_conflict
from .activities import activity_areas, contact_hold_windows, first_activity_conflict


def check_conformance(plan, timeline, performances, envelopes, sample_hz, config=None):
    """Return the first continuous envelope conflict, scoped to shared exchanges.

    Every piecewise-linear root segment, wait, radius transition and final hold
    is checked analytically. ``sample_hz`` remains accepted for API compatibility;
    it no longer controls root-check accuracy. The final capsule validator still
    checks the posed animation separately.
    """
    actors = [a for a in performances if envelopes.knows(a)]
    config = config or CrowdSolverConfig()
    config = replace(config, buffer_m=envelopes.buffer_m)
    roots = [Reservation(a, [(k.time_s, *k.position) for k in performances[a].keyframes],
                         envelopes.body[a],
                         [(b.start_time, b.end_time,
                           max(envelopes.beat_reach.get(b.beat_id, 0.),
                               envelopes.movement_radius(a, b.function)))
                          for b in timeline.beats if b.actor_id == a])
             for a in actors if performances[a].keyframes]
    shared = [b for b in timeline.beats if b.params.get('auto_interaction')
              or (b.params.get('exact_window') and b.params.get('group_id'))]
    participants = {b.params['group_id']: b.params.get('participants', []) for b in shared
                    if b.params.get('group_id') is not None}
    exemptions = [(set(b.params.get('participants', [])), b.start_time, b.end_time) for b in shared]
    contact_groups = {b.params.get('group_id') for b in shared
                      if b.params.get('spacing') == 'contact'}
    approaches = [(set(participants[group]), b.start_time, b.end_time,
                   group in contact_groups)
                  for b in timeline.beats for group in [b.params.get('auto_interaction_approach')]
                  if group in participants]
    approaches.extend((set(members), start, end, True)
                      for members, start, end in contact_hold_windows(timeline))
    conflict = first_conflict(roots, 0., timeline.total_duration_s, config,
                              exemptions, body_only=approaches, details=True)
    tracks = {r.actor: r.points for r in roots}
    samplers = {id(r.points): r.position for r in roots}
    areas = activity_areas(timeline, tracks, envelopes, lambda track, t: samplers[id(track)](t),
                           config.level_tolerance_m)
    area_conflict = first_activity_conflict(roots, areas, 0., timeline.total_duration_s, config, details=True)
    if area_conflict is not None and (conflict is None or area_conflict['time_s'] < conflict['time_s']):
        conflict = area_conflict
    return [conflict] if conflict else []
