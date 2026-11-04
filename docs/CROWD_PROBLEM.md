# Crowd placement, interaction routing, and collision validation

For the consolidated current implementation, see [CROWD_IMPLEMENTATION.md](CROWD_IMPLEMENTATION.md). This document also preserves historical failures and intermediate results.

This document describes the current compiler implementation for shots with many actors, especially actors placed at scene-derived standing spots and then sent to shared interactions. It reflects the code as of 2026-10-24. The archived 20-actor club shot now compiles and passes the sampled collision validator under the requested limb-contact policy; the algorithms remain bounded and incomplete for arbitrary inputs.

## Current status

**Performance follow-up:** [CROWD_PERFORMANCE_UPDATE.md](CROWD_PERFORMANCE_UPDATE.md) records faster repeated calculations and corrections to sampled path headings and camera projection checks. The updated club replay passes collision, heading and camera checks with unchanged search limits and 3,379 collision samples; the suite now has 230 passing tests. The earlier replay described below retained warnings that this follow-up resolves. Full joint interaction backtracking, temporal arena reuse, orientation-aware envelopes, swept skeletal validation and localized repair remain unimplemented under the requested no-additional-compute constraint.

**Latest joint-planning replay:** The archived 20-character club plan now compiles successfully, passes synthesized-root conformance, and passes full posed-capsule collision validation at **3,379 time samples** over **70.375 seconds**: zero penetrations, zero level violations, and no missing actor/asset collider coverage. The former hand/scenery penetration is absent. Character limb contacts are allowed; core-body and all scenery checks stay enabled. All **218 tests pass**. The successful ground-movement search visited 23 joint priority nodes with the existing 8-second route-delay limit. Separate heading and camera warnings remain; this is a crowd collision result, not a claim that every shot-quality check passes. See [`CROWD_VALIDATION_RESULTS.json`](CROWD_VALIDATION_RESULTS.json) for the current collision report and [`CROWD_JOINT_VALIDATION_RESULTS.json`](CROWD_JOINT_VALIDATION_RESULTS.json) for provenance, configuration, tests and warnings. The replay was generated in an isolated temporary scene; live user shots were not regenerated.

Ground movements now use conflict-directed partial-priority search (PBS-style): solve independent paths, detect continuous conflicts, branch on either agent yielding, and replan under that order. Fixed prefixes remain reserved; unsolved future guesses do not become permanent obstacles. Meeting and arrival placement includes the limbs of following stationary performances. Interaction scheduling also retains reservations for already solved groups that occur later in the source list, fixing overlaps introduced by wave reordering.

## Historical replay results

The following reports predate the successful replay above.

**Limb-contact policy:** Incidental arm and leg contacts between characters are now permitted by default, as requested. The planner measures core-body reach for character separation and full-body reach for scenery. Final validation protects the pelvis, torso, neck and head against other characters, and still checks all body parts against solid objects. Set `allow_actor_limb_contacts=False` (or standalone `--strict-actor-limbs`) for the former strict rule. All **204 tests pass**, including explicit limb/body/head/scenery and serial/parallel policy checks. At five selected times from the earlier bake, the four reported character limb contacts are permitted and the hand/club penetration remains blocked. This sample check is not a complete revalidation or a successful new club compile; the latest replay still stops at a route expansion limit. See [`CROWD_LIMB_CONTACT_VALIDATION_RESULTS.json`](CROWD_LIMB_CONTACT_VALIDATION_RESULTS.json). The following replay results describe earlier policies.

**Shared-destination follow-up:** Ordinary ground movements now reserve separate arrival slots near the requested target instead of sharing its exact point. Slots clear stationary actors, other arrival holds, support edges and measured solid boxes; route clearance includes measured walking/running reach. The compiler only previews interactions before the first adjustable ground movement, then allocates and routes the arrivals before solving later approaches. This avoids requiring an approach from an unsafe, unallocated convergence point. All **182 tests pass**, including a real-character bake with a solid object at the common destination that passes full capsule validation with zero penetrations. The archived club replay currently stops before baking with `CROWD_ROUTE_LIMIT` (`run_to_55`, departure horizon exhausted), rather than accepting an unsafe path. This is search exhaustion, not proof that the club cannot fit a safe shot. See [`CROWD_DESTINATION_VALIDATION_RESULTS.json`](CROWD_DESTINATION_VALIDATION_RESULTS.json). The five-penetration report below describes the earlier implementation and remains historical evidence.

The follow-up implementation now includes joint automatic arena assignment, safe-interval routes with continuous swept-disc checks, explicit waits, ordinary ground-movement reservations, bounded ground-movement priority retries, trigger preservation, shared-trigger stage synchronization, and a pre-bake synthesized-root check. The Python test suite passes **170 tests** (`.venv/bin/python -m pytest tests -q`). Generated layout cases cover 4, 10, 20 and 40 actors; routing tests cover moving blockers, mid-route waits, fast crossings, goal holds, completion events and recompilation. These are targeted properties, not a completeness or collision-freedom proof.

The archived 20-actor plan was replayed in an isolated temporary scene, restoring its archived shot-level club edits and rebasing their asset references. The live scene and user shots were not regenerated. The replay now passes the synthesized-root check and bakes all actors, so it gets past the previous `INTERACTION_SEARCH_LIMIT`. The compiler's final validator encountered its existing 120-second timeout. A separate complete validation of that bake at 3,603 time samples found **five remaining penetrations**: four actor/actor leg or foot conflicts during the ordinary run phase, and one actor/club hand conflict during a later approach. There were no level violations or missing collider/skeleton coverage. The historical strict-policy report is [`CROWD_VALIDATION_RESULTS_STRICT_HISTORICAL.json`](CROWD_VALIDATION_RESULTS_STRICT_HISTORICAL.json).

That earlier crowd replay was therefore **not fully resolved**. It used manifest moving-body radii and lacked clearance checks against the measured solid boxes inside buildings. The shared-destination follow-up addresses those modeling gaps conservatively, but the proposal's localized validator-driven repair and per-wave arena backtracking remain unimplemented. The archived plan also contains already solved, serialized numeric interaction times; new trigger preservation prevents further information loss but cannot recover the original missing shared triggers. The earlier report of 17 penetrations is historical and is not an identical before/after benchmark for these replays.

For the standalone replay validation, use the baked shot and its allowance file:

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/collision/validate_collisions.py path/to/shot.usda \
  --manifest manifest.json --allow path/to/allow_collisions.json --workers 4 \
  --json path/to/collision_report.json
```

The following sections describe the earlier implementation baseline; see the implementation-progress section in `CROWD_SOLUTION.md` and the README for the newer behavior and limits.

## Pipeline and ownership

```text
PainterSpace tool calls / shot plan
    -> scene context: support faces, solids, standing spots
    -> initial actor states and crowd spacing
    -> contact-window fitting and prompt-interaction scheduling
    -> resolved timeline, motion synthesis, and USD bake
    -> full-shot collision validation and report
```

The queue LLM endpoint supplies declarative calls. The compiler, not the LLM response writer, computes safe placements and routes when PainterSpace executes those calls. Crowd spacing runs in `ShotCompiler` after initial actor states are built and before contact-window fitting and motion synthesis. A spacing or route error returns a structured compile failure before animation baking. Collision validation runs after the shot is authored.

The main inputs are the shot plan's setup and beats, initial actor states, scene-derived `standing_spots` and support/solid geometry, manifest asset collision and bounds data, and animation clip files. Director-authored tracks in the brief identify actors whose initial positions are locked.

## Standing spots and initial spacing

`derive_standing_spots` builds a 1.5 m grid over measured horizontal support faces. For each scene owner it selects the broad floor level, accepts nearby step-height faces, rejects cells too close to solids or support edges, and retains the largest four-connected group of reachable cells. It names spots `standing_left_NN` or `standing_right_NN` relative to the owner's heading. The default catalog limit is 64 spots per side; callers can change or disable that limit. Each spot has a position and `clearance_m`, the horizontal distance to the nearest solid box overlapping a standing body's height. A missing `clearance_m` means no such solid was found.

`space_standing_actors` only adjusts `place` setup actions that refer to these derived standing spots. It keeps each actor on the same scene owner and named side. Explicit coordinate placements, vehicle seats, and actors with director tracks stay in place and reserve space for adjustable actors.

For each actor, the placement radius is the maximum of the domain support margin, manifest character collision radius, half the asset's horizontal bounds, and measured reach of any planned `dance` clip. The dance measurement opens the USD skeleton, samples authored translation/rotation/scale times plus midpoints, and takes the greatest horizontal joint distance from the first sampled hip position plus the largest configured body-segment capsule radius. Results are cached by clip path and file modification time. If a clip cannot be measured, manifest and support dimensions still provide a fallback radius.

Actors with larger radii are assigned first. Candidate spots are tried in distance order from the original reference, with deterministic name tie-breaking. A candidate must have enough static-solid clearance and must remain at least `radius_a + radius_b + 0.12 m` from every occupied actor on the same level. All assignments are collected before modifying the plan and initial states. If any actor cannot fit, `CROWD_SPACING` identifies the actor and the compiler returns without partially applying the relocation.

For prompt-generated `stage_pair` meetings that use derived standing centers, independent pairs receive centers on the same owner and side separated by at least `2 * 1.5 + 0.12 = 3.12 m` on the same level. Repeated interactions of the same two participants reuse their center. This allocates distinct *areas*; it does not reserve those areas across time or prove that approaches to them are feasible. If no suitable center exists, compilation returns `CROWD_SPACING` with the affected beat.

These distances are implementation parameters for the current geometry and clips, not a general packing proof. The measured clip radius is a conservative horizontal envelope around the starting hip, while the final blended and procedural pose is checked later by the validator.

## Interaction scheduling and routes

`schedule_prompt_interactions` links prompt-only shared actions, resolves preceding beats, and builds provisional trajectories for the other actors. It considers requested and scene-derived meeting centers, formation angles, and possible start times. Candidate destinations and paths must lie on measured support and clear obstacle footprints. An initially smooth path is checked first; when it fails, `_clear_route_around_discs` searches a bounded eight-neighbor grid with A*. Every candidate edge is checked against obstacle discs and support, so the grid is a route-finding aid rather than a replacement for those geometric checks.

The scheduler then samples other actors' provisional positions at the time the approaching actor would reach each route point. This detects some moving-actor conflicts and lets an interaction use a later start or another center. Timeline trajectory lookup uses binary search. The search does not reserve all agents' space-time paths jointly, and a route accepted here can still collide after the final animation is baked.

The default route limits are 2,048 grid expansions per A* search and 48 approach attempts per interaction, with a per-center share of that budget. `schedule_prompt_interactions` accepts a larger `max_route_attempts` for callers doing slower offline searches; `_clear_route_around_discs` similarly accepts `max_expansions`. Reaching the interaction budget returns `INTERACTION_SEARCH_LIMIT`, including group, beat, and attempt count. That means the bounded search did not find a clear approach; it is **not** a proof that no solution exists.

That earlier implementation used local geometric A* and temporal clearance checks. The current movement solver adds bounded PBS-style joint coordination with safe-interval routing; it does not claim CBS/ECBS, WHCA*, PIBT, LaCAM, ORCA or flow/ILP. The separate `tools/collision/plan_path.py` provides single-actor static-obstacle A* and is also not a joint multi-agent solver.

## Final collision validation

`validate_collisions.py` evaluates the composed USD shot at each frame and, by default, one half-frame substep (24 fps shots therefore get 48 time samples per second). It checks posed skeletal body capsules against scene collision boxes and other actors, moving rigid assets, support-level integrity, missing collider coverage, and narrowly scoped allowed contacts. The output includes `violations`, `level_violations`, `unprotected_assets`, `unprotected_actors`, `time_steps`, and an overall `ok` result. The compiler writes `collision_report.json` and includes the result in its validation report.

For longer shots, `validate_parallel` divides disjoint time samples among USD worker processes, merges violation intervals and worst depths, then checks level integrity across the full timeline. Short shots use the serial path to avoid process startup cost. The CLI defaults to up to four workers; `--workers 1` forces serial validation. For example:

```sh
.venv/bin/python tools/collision/validate_collisions.py path/to/shot.usda \
  --manifest manifest.json --workers 4 --json path/to/collision_report.json
```

The compiler invokes the validator subprocess with a 120-second timeout. When the shot has a `user_edits.usda` layer, the compiler does not run its automatic repair retry. Parallel validation improves elapsed time but keeps the same samples and collision criteria; it does not make an invalid shot valid.

## Capabilities and limits

| Area | Current capability | Limit |
| --- | --- | --- |
| Initial crowd placement | Spreads derived standing-spot actors using asset size, collision radius, and planned dance clip reach | Does not move explicit, seated, or director-locked placements; greedily assigning largest radii first can miss a feasible global arrangement |
| Scene geometry | Uses measured support faces, grid connectivity, and horizontally measured solid clearance | Standing-spot availability depends on the scene's exposed support and collision geometry; unrepresented solids cannot be avoided |
| Pair meetings | Gives independent generated pairs distinct centers and reuses centers for repeated pairs | Centers are not a complete formation, timing, or route reservation |
| Routes | Uses supported geometric paths, bounded A*, alternate centers and times, and sampled moving-actor checks | No joint space-time search or completeness guarantee; finite budgets can stop before a feasible choice is found |
| Animation clearance | Samples dance USD skeleton reach for initial placement | Other actions, attack limbs, clip blending, root motion, and procedural changes can exceed the initial estimate |
| Final validation | Checks baked poses over the shot and reports actual collisions; long shots can use multiple processes | Sampling can miss events between substeps; validation remains a separate, potentially expensive stage and retains a 120-second compiler timeout |

## Evidence and how to diagnose failures

The focused regression suite for crowd spacing, pair staging, timeline, director briefs, and prior issues passed 72 tests in 6.51 seconds. Synthetic tests cover separated dancers, occupied and solid-adjacent spots, insufficient room without partial mutation, clip reach exceeding rest bounds, and independent/repeated pair centers. Parallel and serial validation were compared on clear and colliding cases. On one 20-actor five-second slice, validation took 20.6 seconds serial and 7.7 seconds with four workers; one full validation took 66.6 seconds and found 17 penetrations. These are measurements from that workload, not guaranteed speedups for other shots.

When a compile fails:

1. `CROWD_SPACING`: inspect the named actor or pair, available same-side standing spots, support area, and solid clearance. Reduce local density, use a larger supported area, or change the choreography.
2. `INTERACTION_SEARCH_LIMIT`: inspect the reported group and beat. Try distributing meeting areas, staggering groups, or raising the route-attempt budget if a slower search is acceptable. A larger budget may still fail if no route exists.
3. A collision report after baking: inspect the actors, body parts, `first_s`/`last_s`, `worst_depth_m`, and level violations. Initial spacing alone cannot resolve collisions caused later by trajectories or animated limbs.

## Source map

- [`tools/shot_library/support_surfaces.py`](../tools/shot_library/support_surfaces.py): support containment, routes on support, and standing-spot derivation.
- [`tools/shot_library/crowd_spacing.py`](../tools/shot_library/crowd_spacing.py): animation reach, pair-center assignment, and initial relocation.
- [`tools/shot_library/director_solver.py`](../tools/shot_library/director_solver.py): shared-interaction scheduling and bounded routes.
- [`tools/shot_library/timeline_resolver.py`](../tools/shot_library/timeline_resolver.py): resolved trajectory sampling.
- [`tools/shot_library/compiler.py`](../tools/shot_library/compiler.py): pipeline integration and collision-validator invocation.
- [`tools/collision/validate_collisions.py`](../tools/collision/validate_collisions.py): final collision checks and parallel execution.
- [`tests/test_crowd_spacing.py`](../tests/test_crowd_spacing.py), [`tests/test_stage_pair.py`](../tests/test_stage_pair.py), and [`tests/test_collision_coverage.py`](../tests/test_collision_coverage.py): focused regression coverage.
