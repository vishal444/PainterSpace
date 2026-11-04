# Crowd fixes under a compute-cost constraint

This follow-up implements cheaper repeated calculations and corrects the reproduced heading/camera warnings while retaining the existing search limits and collision sample count. It does **not** implement every proposed reliability extension. Full joint interaction backtracking, automatic timed arena reuse, orientation-aware body envelopes, swept skeletal validation and validator-driven localized repair remain future work. Those features can require additional search or collision checks; a universal zero-cost guarantee would be misleading.

## Implemented changes

| Area | Change | Protection retained |
| --- | --- | --- |
| Reservation lookups | Build timing boundaries and reach intervals once per reservation; use binary search for each query. | The same movement keys, reach changes and final holds are checked. |
| Joint conflict detection | Reject pairs whose conservative swept bounds are spatially disjoint before running the continuous narrow phase. | Crossing edges, changing radii, different levels and destination holds still receive the original analytic check when their bounds can overlap. |
| Priority search | Deduplicate transitive orders, rather than treating different edge lists for the same order as separate alternatives. | Priority cycles remain rejected. Existing node and low-level expansion budgets remain unchanged. |
| Polyline sampling | Index cumulative segment lengths instead of repeatedly scanning every segment. Align corner headings with the actual movement between sampled root positions. | Path positions, duration, speed-profile behavior and authored geometry are preserved. No smoothing is introduced through obstacles. |
| Camera validation | Select camera samples and compute projection bases once per check time. Use the actual frustum test at every distance. | Subjects behind the camera and outside the frustum still fail. The arbitrary 40-metre rejection is removed: distance alone does not make a wide shot invalid. |

The camera correction does not move an authored camera, hide actual framing errors, or establish a minimum subject size. Camera validation retains its existing three check times. It now evaluates distant subjects geometrically instead of returning a distance warning before checking their projection.

The heading correction changes orientation at sampled polyline corners to match the baked root's interpolated travel direction. Regression tests cover straight paths, corners, repeated points and stationary paths. Because orientation affects posed bodies, the club replay was also collision-validated after the correction.

Reservation indexes use additional per-reservation memory. This update is not a claim of zero memory overhead or constant time for every possible input.

## Measurements and validation

Component comparisons used seven interleaved repeats of the saved pre-change modules and updated modules in the same process. The table gives median times per call; they are component measurements, not total compile or render speedups.

| Component case | Before | Updated | Ratio |
| --- | ---: | ---: | ---: |
| 4 separated stationary actors: conflict check | 0.0326 ms | 0.0129 ms | 2.53× faster |
| 20 separated stationary actors: conflict check | 1.0904 ms | 0.2199 ms | 4.96× faster |
| 40 separated stationary actors: conflict check | 4.9501 ms | 0.8395 ms | 5.90× faster |
| Short interval lookup in a 100-key reservation | 0.0119 ms | 0.0059 ms | 2.02× faster |
| Camera validation with 20 subjects | 6.6528 ms | 0.3781 ms | 17.60× faster |
| Sampling a 40-point path | 1.6182 ms | 1.0170 ms | 1.59× faster |
| Sampling a 2-point path | 0.9095 ms | 0.8671 ms | Approximately unchanged |

The initial updated club replay took **124.45 seconds**, compared with the previously recorded **125.67 seconds** for compilation, baking and validation. A final-code replay then took **123.73 seconds**, again passing all three checks. These two updated runs show no observed aggregate slowdown against that recorded baseline; the small differences are not enough to establish a reliable overall speedup. CPU load, caches and subprocess startup can affect these timings. Rendering was not measured.

The updated replay passed collision, heading lint and camera checks: zero disallowed penetrations at the same **3,379 samples**, with no missing collider coverage, level violations, heading warnings or camera warnings. The contact policy remains `core_body`; all body parts remain checked against scenery. No search budgets, validation sample counts or validator timeouts were increased.

The full suite passed **230 tests**. A subsequent focused run of **50 tests** covered the final path-lookup optimization. New differential tests compare indexed reservation intervals and broad-phase results with exhaustive checks for 2, 4, 20 and 40 actors. Tests also retain fast crossings and final holds, and verify in-frame, off-screen and behind-camera subjects at several distances.

See [CROWD_PERFORMANCE_RESULTS.json](CROWD_PERFORMANCE_RESULTS.json) for measurements, validation and explicit scope. [CROWD_JOINT_VALIDATION_RESULTS.json](CROWD_JOINT_VALIDATION_RESULTS.json) preserves the earlier successful replay, including its then-outstanding warnings; it is historical evidence rather than the current warning status.

## Reproducing component comparisons

Save the baseline versions of `st_planner.py`, `joint.py`, `camera_controller.py` and `director_brief.py` in a separate directory before changing them. From the repository root:

```sh
.venv/bin/python tools/benchmark_crowd_performance.py \
  --baseline-dir /path/to/baseline-modules --output /path/to/results.json
.venv/bin/python -m pytest tests -q
```

The benchmark requires an explicit baseline snapshot; it does not assume that `HEAD` contains the earlier implementation. Temporary baseline/replay files used during development are not permanent repository artifacts.

## What is still not solved by this update

The existing joint movement planner still has bounded search and conservative circular envelopes. Interaction approaches still lack full joint layout/path backtracking. Arenas are not automatically reused by temporal wave, skeletal validation remains sampled, and failed final validation does not have a completed local replanning loop. The performance improvements above do not remove those limits.

Stronger algorithms could spend saved time on additional work, but their cost varies with the input. Implementing them safely under a strict runtime ceiling requires a separate resource-budget contract and benchmarks that include difficult and failing shots. They cannot honestly be promised as universally free improvements.
