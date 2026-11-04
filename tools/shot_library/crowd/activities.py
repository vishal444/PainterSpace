"""Timed space occupied by scheduled actions, independent of route priority."""
from dataclasses import dataclass
import math

from .st_planner import Reservation, EPS
from .joint import first_conflict
from support_surfaces import SUPPORT_LEVEL_TOLERANCE_M


@dataclass(frozen=True)
class ActivityArea:
    members: frozenset
    start: float
    end: float
    position: tuple
    radius: float

    def overlaps(self, start, end):
        return self.start < end and start < self.end

    def reservation(self):
        return ActivityReservation(self)


class ActivityReservation(Reservation):
    """An action occupies its area only during its resolved time window."""
    def __init__(self, area):
        super().__init__(min(area.members), [(area.start, *area.position)], area.radius)
        self.members = area.members
        self.start, self.end = area.start, area.end
        self._t_min, self._t_max = area.start, area.end

    def intervals(self, start, end):
        yield from super().intervals(max(start, self.start), min(end, self.end))


def contact_hold_windows(timeline):
    """Keep a completed contact formation scoped until either partner moves again."""
    windows, seen = [], set()
    horizon = getattr(timeline, 'total_duration_s',
                      max((beat.end_time for beat in timeline.beats), default=0.0))
    for beat in timeline.beats:
        params = beat.params
        members = frozenset(params.get('participants') or ())
        if (len(members) < 2 or params.get('spacing') != 'contact'
                or not params.get('auto_interaction')):
            continue
        key = (params.get('group_id'), beat.end_time, members)
        if key in seen:
            continue
        seen.add(key)
        end = min((following.start_time for following in timeline.beats
                   if following.actor_id in members
                   and following.start_time >= beat.end_time - EPS
                   and following.function != 'wait'
                   and following.params.get('group_id') != params.get('group_id')),
                  default=horizon)
        if end > beat.end_time + EPS:
            windows.append((members, beat.end_time, end))
    return windows


def activity_areas(timeline, tracks, envelopes, sample,
                   level_tolerance=SUPPORT_LEVEL_TOLERANCE_M):
    """Reserve stationary performances and the space between shared participants.

    A shared formation uses a conservative enclosing disc of the participant
    positions and measured clip envelopes. It protects the interaction's area,
    including the gap between participants, from unrelated actors. Participants
    retain their own contact contract. No function name, scene or cast size is
    used to identify fights: resolved shared-action metadata defines the area.
    """
    areas, shared = [], {}
    single_by_actor = {}
    approach_windows = {}
    group_members = {}
    shared_windows = []
    contact_windows = contact_hold_windows(timeline)
    for b in timeline.beats:
        group = b.params.get('auto_interaction_approach')
        if group:
            lo, hi = approach_windows.get(group, (b.start_time, b.end_time))
            approach_windows[group] = (min(lo, b.start_time), max(hi, b.end_time))
        gid = b.params.get('group_id')
        if gid and b.params.get('participants'):
            group_members[gid] = set(b.params['participants'])
            shared_windows.append((frozenset(b.params['participants']), b.start_time, b.end_time))
    for beat in timeline.beats:
        actor = beat.actor_id
        if not envelopes.knows(actor) or actor not in tracks or beat.end_time <= beat.start_time:
            continue
        params = beat.params
        if params.get('auto_interaction_approach'):
            continue  # Approach partners have their own body/contact contract.
        members = frozenset(a for a in params.get('participants', [])
                            if a in tracks and envelopes.knows(a))
        positions = params.get('interaction_positions') or {}
        # Prompt-linked pairs already have a shared group before the later
        # interaction scheduler assigns positions and marks auto_interaction.
        # Treating either participant as an unrelated singleton at this stage
        # makes every intended contact look like an unresolvable crowd clash.
        if len(members) > 1 and (positions or params.get('auto_interaction')
                                or params.get('group_id')):
            key = (params.get('group_id'), members, beat.start_time, beat.end_time)
            shared.setdefault(key, positions)
            continue  # Their shared area retains the participants' contact contract.

        # Only stationary intervals are area obstacles. Traveling actors keep
        # their timed trajectories, so another actor can wait for them to pass.
        points = [sample(tracks[actor], beat.start_time), sample(tracks[actor], beat.end_time)]
        points.extend(p[1:] for p in tracks[actor] if beat.start_time < p[0] < beat.end_time)
        if any(math.dist(points[0], p) > EPS for p in points[1:]):
            continue
        radius = max(envelopes.body[actor], envelopes.beat_reach.get(beat.beat_id, 0.),
                     envelopes.movement_radius(actor, beat.function))
        pos = tuple(points[0])
        def add_area(members, start, end):
            if end - start <= EPS:
                return
            area = ActivityArea(members, start, end, pos, radius)
            if len(members) > 1:
                areas.append(area)
                return
            actor_areas = single_by_actor.setdefault(actor, [])
            if (actor_areas
                    and math.dist(actor_areas[-1].position, pos) <= EPS
                    and abs(actor_areas[-1].radius - radius) <= EPS
                    and start <= actor_areas[-1].end + EPS):
                prev = actor_areas[-1]
                merged = ActivityArea(prev.members, min(prev.start, start),
                                      max(prev.end, end), prev.position, radius)
                actor_areas[-1] = merged
                areas[areas.index(prev)] = merged
            else:
                actor_areas.append(area)
                areas.append(area)

        # A partner waiting while the other approaches keeps its full activity
        # reach against outsiders, but their shared contact contract governs
        # the pair. Split an idle beat when it only partly overlaps the route.
        partner_windows = [
            (max(beat.start_time, lo), min(beat.end_time, hi), group_members[g])
            for g, (lo, hi) in approach_windows.items()
            if beat.beat_id.startswith('idle_') and actor in group_members.get(g, ())
            and lo < beat.end_time - EPS and beat.start_time < hi - EPS
        ]
        if beat.beat_id.startswith('idle_'):
            partner_windows.extend(
                (max(beat.start_time, lo), min(beat.end_time, hi), members)
                for members, lo, hi in shared_windows
                if actor in members and lo < beat.end_time - EPS
                and beat.start_time < hi - EPS)
            partner_windows.extend(
                (max(beat.start_time, lo), min(beat.end_time, hi), members)
                for members, lo, hi in contact_windows
                if actor in members and lo < beat.end_time - EPS
                and beat.start_time < hi - EPS)
        cuts = sorted({beat.start_time, beat.end_time} |
                      {t for lo, hi, _ in partner_windows for t in (lo, hi)})
        for start, end in zip(cuts, cuts[1:]):
            active = [members for lo, hi, members in partner_windows
                      if lo < end - EPS and start < hi - EPS]
            members = frozenset({actor}.union(*(set(group) for group in active)))
            add_area(members, start, end)

    for (_, members, start, end), explicit_positions in shared.items():
        points = {actor: explicit_positions.get(actor, sample(tracks[actor], start))
                  for actor in sorted(members)}
        # Separate floors never create an enclosing obstacle between levels.
        levels = []
        for actor in sorted(points, key=lambda a: points[a][1]):
            if not levels or points[actor][1] - points[levels[-1][0]][1] > level_tolerance:
                levels.append([])
            levels[-1].append(actor)
        for actors in levels:
            center = tuple(sum(points[a][k] for a in actors) / len(actors) for k in range(3))
            # Include every reach transition inside the shared action interval.
            times = {start}
            times.update(b.start_time for b in timeline.beats if b.actor_id in actors
                         and start < b.start_time < end)
            radius = max(math.hypot(points[a][0] - center[0], points[a][2] - center[2])
                         + max(envelopes.radius_at(a, t, timeline.beats) for t in times)
                         for a in actors)
            # Represent each occupied height so the enclosing area's vertical
            # coverage contains the participants' individual level tolerances.
            for height in sorted({points[a][1] for a in actors}):
                areas.append(ActivityArea(frozenset(actors), start, end,
                                          (center[0], height, center[2]), radius))
    return areas


def first_activity_conflict(reservations, areas, start, end, config, details=False):
    """Check unrelated actors against areas, preserving member contact contracts."""
    first = None
    for area in areas:
        obstacle = area.reservation()
        for reservation in reservations:
            if reservation.actor in area.members:
                continue
            conflict = first_conflict([reservation, obstacle], start, end, config, details=True)
            if conflict is not None and (first is None or conflict['time_s'] < first['time_s']):
                first = {**conflict, 'occupied_area_members': sorted(area.members)}
    if first is None or details:
        return first
    return first['actor'], first['other_actor']
