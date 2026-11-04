# Shared interaction regression cases

These three tests in [test_director_brief.py](../tests/test_director_brief.py) were excluded from the reported 562-test regression run. Their failures had been reported before the recent spatial-contract changes. That history does not establish their current status or explain their cause.

This document records the cases and their existing assertions. The tests were inspected, but were **not rerun for this documentation task**. No current failure traceback or confirmed diagnosis is available here. None should be treated as an accepted permanent exclusion.

## 1. Use the latest arrival and wait for other participants

**Test:** `test_prompt_interaction_uses_latest_route_arrival_and_waits`

**Coverage:** Interaction scheduling and timeline resolution through `schedule_prompt_interactions` and `TimelineResolver`.

The fixture places two characters at different starting positions. The second character has a preceding two-second wait. Both have three-second conversation actions belonging to the same meeting group, initially requested at time zero. A test route provider returns straight paths, and the scene context is empty.

The test expects:

- Scheduling and timeline resolution succeed.
- Both conversation actions start together.
- Automatic approach movements exist for both participants.
- The first character arrives before the second.
- The conversation starts at the second character's arrival.
- An idle-fill beat covers the first character's early arrival until the second arrives.

**Behavior to protect:** A shared action starts only after every participant is ready, including prior actions and actual routed travel. Early arrivals wait safely until the shared start.

**Investigation needed:** Check the scheduler result before inspecting timeline assertions. Establish whether the fixture provides the support and actor information required by the current scheduler. If scheduling succeeds, inspect approach destinations, durations, completion events and the final shared start. Determine whether the asserted arrival order and a specifically marked idle-fill beat remain required behavior or depend on an older scheduling representation.

## 2. Preserve a shared meeting start through compilation

**Test:** `test_prompt_meeting_compiles_to_one_shared_start`

**Coverage:** The compiler pipeline and the saved `resolved_timeline.json` artifact.

The fixture explicitly places two character assets, gives the second a preceding two-second wait, and requests the same grouped conversation for both. It compiles into a temporary shot with `validate=False`, without supplying a scene stage.

The test expects:

- Compilation succeeds and writes the resolved timeline.
- The timeline contains two conversation actions with exactly equal start times.
- Automatic approaches are present, with the first character arriving earlier.
- The shared conversation start equals the second character's arrival.

**Behavior to protect:** Compilation preserves the scheduler's readiness dependencies and shared start through routing, synthesis and timeline serialization.

**Investigation needed:** Identify the first compiler error, if any. Check whether compilation without a scene supplies enough measured support for automatic approaches; `validate=False` should not be assumed to disable planning requirements. If compilation succeeds, compare the scheduled and serialized timelines. As in case 1, distinguish the essential latest-arrival barrier from the fixture's particular arrival-order assumption.

**Coverage limit:** This test disables final validation. Even a passing result does not independently establish collision freedom or final animated contact correctness.

## 3. Route both participants into a pair interaction

**Test:** `test_prompt_pair_contact_routes_both_participants`

**Coverage:** Automatic approach scheduling for a targeted pair action.

The fixture starts two characters apart and requests one `shake_hands` action from the first character targeting the second. It supplies a straight-path test provider and an empty scene context.

The test expects:

- Scheduling and timeline resolution succeed.
- Automatic approach movements cover both participants.
- The handshake starts at the latest approach completion.
- The handshake records both character IDs in its `participants` parameter.

**Behavior to protect:** The interaction identifies both participants, brings them to compatible contact positions, and waits for their required approaches to finish.

**Investigation needed:** Check participant discovery, meeting-position allocation, route generation and the action's completion dependencies. Verify the current policy for moving both participants: routing both is appropriate for this unconstrained fixture, but should not become a universal rule that overrides an explicitly stationary or locked participant. Determine whether missing scene support prevents scheduling before these assertions can be evaluated.

**Coverage limit:** This is a scheduler test using straight route doubles. It does not independently verify obstacle avoidance, hand alignment or final animation contact.

## Reproduce and resolve

Run all three explicitly from the repository root, using the project's Python environment:

```sh
.venv/bin/python -m pytest \
  tests/test_director_brief.py::test_prompt_interaction_uses_latest_route_arrival_and_waits \
  tests/test_director_brief.py::test_prompt_meeting_compiles_to_one_shared_start \
  tests/test_director_brief.py::test_prompt_pair_contact_routes_both_participants \
  -vv --tb=long
```

For each failure, record the first failing assertion or structured error, the relevant generated approaches and dependencies, and whether the cause is a system defect, an incomplete fixture or an outdated expectation. These are investigation categories, not established diagnoses.

Preserve valid readiness, synchronization and contact requirements. If a fixture lacks geometry required by the current contract, supply a representative supported scene instead of weakening geometry checks. If an assertion depends on an obsolete representation or incidental arrival order, replace it with evidence of the intended behavior. If the implementation is wrong, fix the reusable scheduling or compilation logic.

Resolution is complete when each case passes without deselection, the underlying behavior is covered across representative inputs, and the relevant regression suite runs with these tests included. A passing isolated test is not a substitute for final collision and motion validation where those guarantees are required.
