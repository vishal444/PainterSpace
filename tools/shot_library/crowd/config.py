from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CrowdSolverConfig:
    """Planning tolerances and resource limits, independent of scene or cast size.

    Delay is bounded relative to unobstructed travel, not absolute shot time.
    Departure sampling limits completeness; failures are never geometry proofs.
    """
    # Required free space beyond the protected body/animation envelopes.
    buffer_m: float = 0.3
    cell_size_m: float = 0.4
    departure_step_s: float = 0.25
    max_delay_s: float = 8.0
    max_expansions: int = 4096
    # Try coarse-to-fine spatial grids under one total expansion budget.
    route_grid_levels: int = 3
    layout_beam_width: int = 48
    layout_candidates: int = 128
    # Floor-derived initial placement retries, after trying advertised markers.
    # Each level widens the assignment beam and refines the sampled floor grid.
    standing_search_levels: int = 3
    standing_samples: int = 4096  # Total raw floor samples per adjustable actor.
    # Fraction of a measured floor span advertised as each boundary entry area.
    entry_region_fraction: float = 0.25
    arena_perimeter_samples: int = 32
    max_formations: int = 4096
    priority_retries: int = 4  # Legacy API compatibility; joint search uses max_joint_nodes.
    max_joint_nodes: int = 128
    max_joint_time_s: float = 60.0
    level_tolerance_m: float = 0.12
    destination_rings: int = 6
    destination_candidates: int = 256
    # Bound joint arrival-layout backtracking independently of pathfinding.
    max_destination_nodes: int = 4096
    # Maximum error from a requested arrival area when accepting a nearby stop.
    arrival_tolerance_m: float = 2.0
    arrival_route_candidates: int = 8
    # Unrelated actors retain space for arms and legs as well as their torsos.
    # Explicit interaction contacts remain scoped by their own contracts.
    allow_actor_limb_contacts: bool = False
    # Rebuild from authored input after validation failures, never edit checked roots.
    max_repair_attempts: int = 3
    max_repair_clearance_m: float = 1.0  # Extra planning buffer above buffer_m.

    def __post_init__(self):
        if not 0 < self.entry_region_fraction < 0.5:
            raise ValueError('Entry region fraction must lie between zero and one half')
        if min(self.cell_size_m, self.departure_step_s) <= 0:
            raise ValueError("Crowd spatial and temporal steps must be positive")
        if min(self.buffer_m, self.max_delay_s, self.level_tolerance_m, self.arrival_tolerance_m) < 0:
            raise ValueError("Crowd clearances and delay must be nonnegative")
        if min(self.max_expansions, self.route_grid_levels, self.layout_beam_width, self.layout_candidates,
               self.standing_search_levels, self.standing_samples,
               self.arena_perimeter_samples, self.max_formations,
               self.destination_rings, self.destination_candidates, self.max_destination_nodes, self.max_joint_nodes,
               self.arrival_route_candidates) < 1:
            raise ValueError("Crowd search budgets must be positive")
        if self.priority_retries < 0 or self.max_repair_attempts < 0:
            raise ValueError("Crowd retry counts must be nonnegative")
        if not isinstance(self.max_repair_attempts, int):
            raise ValueError("Crowd repair attempts must be an integer")
        if not isinstance(self.route_grid_levels, int):
            raise ValueError("Crowd grid levels must be an integer")
        if any(not isinstance(value, int) for value in
               (self.standing_search_levels, self.standing_samples)):
            raise ValueError("Standing search budgets must be integers")
        if not math.isfinite(self.max_repair_clearance_m) or self.max_repair_clearance_m < 0:
            raise ValueError("Crowd repair clearance must be finite and nonnegative")
        if not math.isfinite(self.max_joint_time_s) or self.max_joint_time_s <= 0:
            raise ValueError("Crowd joint search timeout must be finite and positive")
