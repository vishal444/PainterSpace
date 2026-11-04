# PainterSpace – System Analysis, Bugs & Improvement Backlog

- **Date of analysis:** 2026-10-21
- **Scope:** C++/Qt app (`src/`), Python tools (`tools/`, `tools/shot_library/`, `tools/collision/`), tests (`tests/`), build (`CMakeLists.txt`), docs, repo hygiene.
- **Method:** static review of the code, targeted greps, and running the existing test suites. **No source code was modified** – this document is a findings report only.
- **Severity legend:** 🔴 High (wrong behaviour / blocks users) · 🟠 Medium · 🟡 Low (polish / hygiene).

> [!NOTE]
> Findings are marked **Verified** when I reproduced them (test run or direct code inspection) and **Suspected** when they are inferred from reading the code and should be confirmed before fixing.
> The previous `docs/SHOT_SYSTEM_ANALYSIS_AND_IMPROVEMENTS.md` is deleted in the working tree (`git status`: ` D`). This file does not replace its content; restore it from git if it is still needed.

---

## 1. System overview

| Layer | Files | Approx. LOC | Role |
|---|---|---|---|
| Qt/OpenUSD GUI | `src/MainWindow.cpp`, `src/UsdViewportWidget.cpp`, `src/main.cpp`, `src/ShotEngine.cpp` | ~7,700 | Storyboard UI, Hydra Storm viewport, editing layer, CLI modes, process orchestration, rendering/encoding |
| Shot function library | `tools/shot_library/*` | ~9,000 | LLM planner → function catalog → timeline resolver → motion synthesis → USDA compiler → camera |
| Collision toolkit | `tools/collision/*` | ~3,200 | Asset ingest, A* path planning, 48 Hz capsule/OBB validation, repair |
| Animation | `tools/bake_animation.py`, `bake_shot_animations.py`, `bind_baked_animation.py`, `export_mixamo_usd.py` | ~1,300 | Clip baking, blending, UsdSkel binding |
| Scene/state | `tools/scene_manager.py`, `world_state.py`, `sync_manifest.py`, `generate_usda_scene.py`, `process_agent_queue.py` | ~3,000 | Versioning, world-state JSON, manifest sync, legacy LLM USDA generation, agent queue |
| Tests | `tests/`, `tools/shot_library/test_all_phases.py`, `tools/collision/test_collision_tools.py`, `tools/test_*.py` | ~2,000 | Mixed unittest / ad-hoc scripts |

Key observation: there are **two parallel generation paths**: (a) the deterministic shot-library pipeline (`llm_planner.py` → `compiler.py`), and (b) the older "LLM writes USDA text" path (`generate_usda_scene.py`, `main.cpp --generate-usda`, `ShotEngine::buildOpenUsdStage`). They overlap in responsibility, use different models/env-vars, and are only loosely reconciled (see §4).

---

## 2. Test-suite health (verified by running)

| Suite | Command | Result |
|---|---|---|
| Regression + scene manager + stairs | `.venv/bin/python -m unittest discover -s tests` | **32 tests, 4 failures** at analysis time → **all pass after B-01 fix** |
| Shot library phases | `.venv/bin/python tools/shot_library/test_all_phases.py` | Failed at Phase 1 at analysis time → **Phases 0–9 pass after B-01 fix** |
| Collision toolkit | `tools/collision/test_collision_tools.py` | Prints `ALL COLLISION TESTS PASSED` although several rows read `FAIL` (see B-12) |
| C++ `--usd-test-suite` | needs build | Not run (requires built binary) |

Failing regression tests:
1. `test_dynamic_beat_start_reference_resolution`
2. `test_expanded_fundamental_function_catalog`
3. `test_general_llm_planner_zero_hardcoded_coordinates`
4. `test_multi_move_attack_and_non_combat_argue_clips`

**Common root cause** in every failure: `validation.bake_errors = ["Failed to bake/bind character human_N: Expected one Skeleton below /World/<Actor>; found 0"]`, with `lint_passed`, `collision_passed`, `camera_passed` all `True`. In other words the planner, timeline, collision and camera stages work; the **character bake/bind step fails** and `success` becomes `False`. See B-01.

---

## 3. Bugs

### ✅ B-01 – Character bake/bind fails in tests ("Expected one Skeleton … found 0") — *FIXED 2026-10-21*
> **Resolution:** root cause was test setup, not the binder. Compiled shots reference `../../../assets/...` relative to the project root, but the tests compiled into temp folders with no `assets/`. Fixes (tests only):
> - `tests/test_regression_issues.py` `setUp` symlinks the real `assets/` into each temp project, and skips the test if `assets/` is missing.
> - `tools/shot_library/test_all_phases.py` now uses `<root>/scenes/scene_01/` + `<root>/assets` symlink, and prints `SKIPPED` if `assets/` is missing. Also fixed a stale Phase 8 check (`-65.0` vs emitted `-65`).
> - Result: 32/32 regression tests and Phases 0–9 pass.
> - **Still open:** the binder's error message is not more descriptive (I-08 partial), and there is no tracked rigged fixture, so a fresh clone skips these tests rather than running them.
>
> *Original finding below.*

- **Where:** `tools/bind_baked_animation.py` / `tools/bake_shot_animations.py`, invoked from `compiler.py` (bake step), exercised by `tests/test_regression_issues.py` and `test_all_phases.py`.
- **Symptom:** 4 regression tests + Phase 1 of `test_all_phases.py` fail; compile `success: False`.
- **Likely causes (to investigate):**
  1. The tests build temp scenes referencing human assets in `assets/usdc_assets/` – the `assets/` directory is **git-ignored**, so a fresh clone has no skeleton-bearing `human.usdc`/`human.norm.usda`, and the tests do not skip or provide fixtures.
  2. The binder searches below the prim path of the *actor Xform* (`/World/Pedestrian`, `/World/Actor1`) but the `.norm.usda` wrapper or reference may not expose a `UsdSkelRoot`/`Skeleton` under that path (reference path/`defaultPrim` mismatch).
- **Impact:** CI is red; regressions in the compiler cannot be detected; the "100 % deterministic compile" claim in the README is unverified.
- **Suggested fix:** make the binder error actionable (list what prims were found), ship a minimal rigged fixture under `tests/fixtures/`, and mark tests that need real assets with `skipUnless(asset_exists)`.

### 🔴 B-02 – Python modules run `cmake --build` and `execve` the GUI when executed as `__main__` — *Verified*
- **Where:** the same copy-pasted `if __name__ == "__main__":` block at the end of
  `tools/shot_library/compiler.py:1116`, `function_catalog.py:999`, `llm_planner.py:1339`, `motion_synthesis.py:2120`, `timeline_resolver.py:~505`.
- **Symptom:** running any of these modules as a script (or `python -m`, or some IDE "run file" actions) triggers a full C++ build and replaces the process with the GUI binary.
- **Cause:** a launcher snippet was pasted into library modules. It also hard-codes `/Users/vishaljose/Qt/6.11.1/macos/lib` and `build/PainterSpace`.
- **Suggested fix:** delete the blocks (or move to one `scripts/run_app.py`).

### ✅ B-03 – Non-deterministic actor seed (`hash(actor_id)`) breaks replay determinism — *FIXED 2026-10-21*
> **Resolution:** added `_stable_hash()` (`zlib.crc32`) in `motion_synthesis.py` and replaced both `hash(actor_id)` uses (RNG seed at line ~522 and the arc angle at ~1013). New test `test_compile_is_independent_of_pythonhashseed` compiles the same plan in subprocesses with `PYTHONHASHSEED` 1/2/12345 and compares hashes; it passes with the fix and fails with the old `hash()` (verified).
>
> *Original finding below.*

- **Where:** `tools/shot_library/motion_synthesis.py:522` – `random.Random(seed + hash(actor_id) % 1000)`.
- **Problem:** Python's `str.__hash__` is randomised per process (`PYTHONHASHSEED`). Gesture pools, appearance jitter, etc. that depend on this RNG will differ between runs, contradicting the "byte-identical USDA from `shot_plan.json`" guarantee (README Phase 7).
- **Suggested fix:** use a stable hash (`zlib.crc32`, `hashlib.sha1`) and add a test that compiles the same plan in two subprocesses with different `PYTHONHASHSEED` and diffs the output.

### 🔴 B-04 – Silent failure when saving scene versions — *Verified by inspection*
- **Where:** `tools/scene_manager.py:~251` – the block that updates `scene.json` (snapshots, `configured`, `last_scene_edit_saved`) is wrapped in `except Exception: pass`.
- **Impact:** if `scene.json` is malformed or not writable, the version snapshot is created but metadata is not, causing later shots to inherit the wrong scene version with no error shown to the user.
- **Also:** `scene_manager.py:50` and `:78` swallow all exceptions while detecting the scene sublayer; failure returns `None` and the caller treats it as "not linked".
- **Suggested fix:** log + surface the error, narrow the exception types (`OSError`, `json.JSONDecodeError`, `Tf.ErrorException`).

### 🟠 B-05 – Collision repair silently skipped on errors — *Verified by inspection*
- `tools/bake_shot_animations.py:313` – the `author_root_motion` re-routing step is in `except Exception: pass`; the root layer may be left unsaved/partially edited and the user is never told the collision path-repair did not happen.
- `tools/collision/validate_collisions.py:353` – failure to import/evaluate stairs (`plan_path.scene_stairs`) silently sets `stairs = None`, so **level-integrity checks run without stair data** and can report false violations or miss real ones.
- `compiler.py:95,103,928,976` and `compile_shot.py:72,100,161` – same pattern around manifest/scene JSON and the validator.
- **Suggested fix:** replace bare `except Exception: pass` with logged, specific handlers; propagate a warning into `validation_report.json`.

### 🟠 B-06 – LLM response handling can drop calls silently — *Verified by inspection*
- **Where:** `llm_planner.py:337, 348, 471`.
  - Bad `arguments` JSON → `fn_args = {}` and the tool is still invoked with empty args (can create a bogus beat or raise a confusing structured error).
  - If `content` is not JSON the `except: pass` hides it, then `executed == 0` falls through to the offline planner **without telling the user the LLM output was discarded**.
  - `urlopen(..., timeout=25)` is short for large tool-calling prompts, and failures fall back to the semantic planner silently (only appended to `execution_log`).
- **Suggested fix:** skip calls with unparsable args, record them in the structured error feedback for the retry loop, and surface "LLM unavailable – using offline planner" in the UI.

### 🟠 B-07 – Gait stride linter flags planner-generated shots — *Verified (test log)*
- Test output: `Actor 'human_1' speed (6.18 m/s) exceeds max stride limit for clip 'walk' (1.40 m/s)` repeated for ~dozens of frames in the "walk 3 s to `lane_west.stop_mark`" scenario while `run_to` for the other actor passes.
- **Impact:** the planner/compiler produces a `walk_to` over a distance that requires 6 m/s in 3 s (fixed `duration`), instead of choosing `run` or extending the beat. Warnings are generated one-per-frame, bloating `validation_report.json`.
- **Suggested fix:** (1) planner/synthesizer should derive duration from distance ÷ clip nominal speed (or auto-upgrade clip walk→jog→run); (2) linter should aggregate violations per beat (first/last time, peak speed).

### 🟠 B-08 – Blocking process calls on the GUI thread — *Verified by inspection*
- `MainWindow.cpp:156 (15 s), 978 (10 s), 1882 (30 s)`, `UsdViewportWidget.cpp:807 (15 s)`, `ShotEngine.cpp:678/685/717/901/981/1045` (30 s … **10 min**) all use `QProcess::waitForFinished` synchronously.
- **Impact:** UI freezes during manifest sync, world-state prep, collision validation (up to 2 min), rendering and video encoding (up to 10 min each). On macOS this surfaces as the beach-ball and the OS may flag the app as not responding.
- **Suggested fix:** move to async `QProcess` (signals) or `QtConcurrent`; show progress and allow cancel. `MainWindow` already does this for generation – extend the pattern.

### 🟠 B-09 – Unsafe/shared temp file path for `--render-spec` — *Verified*
- `main.cpp:921` writes `/tmp/render_shot_<pid>.usda` (and `:955` default `/tmp/painterspace_usd_spike.png`). Predictable names in a world-writable dir; the temp file is only removed on the failure path before opening (`:932`, `:937` – confirm all return paths).
- **Suggested fix:** use `QTemporaryDir`/`QTemporaryFile` (already included in `main.cpp`).

### 🟠 B-10 – Test-suite code mutates real project data — *Verified by inspection*
- `main.cpp:~1055-1064` (`--usd-test-suite`) edits `scenes/scene_01/shot_001/user_edits.usda`, then deletes it with `QFile::remove(viewport.userEditsLayerPath())`. If the test crashes in between, or the user had real edits in that shot, **user work is lost** (the removal is unconditional).
- Also `tools/scene_manager.py` tests/`tests/test_scene_manager.py` should be audited for the same (they appear to use temp dirs – good – but the C++ suite does not).
- **Suggested fix:** copy the shot to a temp directory before running the C++ suite.

### 🟠 B-11 – Inconsistent LLM configuration across entry points — *Verified*
| Entry point | Model env var | Default | Temperature |
|---|---|---|---|
| `main.cpp` | `PAINTERSPACE_OPENAI_MODEL` | `gpt-4.1-mini` | 0.7 |
| `generate_usda_scene.py` | `PAINTERSPACE_OPENAI_MODEL` | `gpt-4.1-mini` | — |
| `llm_planner.py` | `PAINTERSPACE_LLM_MODEL` | `gpt-4o-mini` | 0.1 |
- Different env vars and defaults for the same feature mean users who set one variable get mixed behaviour. `agentMode` is decided by `OPENAI_API_KEY.isEmpty()` in four places (`MainWindow.cpp:1005,1930`, `main.cpp:432`) with slightly different extra conditions.
- **Suggested fix:** centralise config (one helper in C++ and one in Python, or a `config.json`), document in README.

### 🟡 B-12 – Collision test reports success while rows fail — *Verified*
- `test_collision_tools.py` output contains `G1 … FAIL`, `G3 … FAIL`, `J actor into actor FAIL` and then `ALL COLLISION TESTS PASSED`. If those are *expected* negative controls they should be labelled `EXPECTED-FAIL`/`DETECTED`; if not, the final assertion is too weak.

### 🟡 B-13 – Documentation vs. code mismatches — *Verified*
- README says **76** catalog verbs in one place and "~40" in others (`function_catalog.py` docstring: "~40"; actual public `def`s ≈ 102 including queries/setup). Counts should be generated from `export_tool_schemas()`.
- README: "Phase 0–9" tests and `--usd-test-suite (Phases 0–3)`; actual state is red (see §2).
- README references `assets/` (models, `human.usdc`, `facial_expressions/`) but `assets/` is in `.gitignore`, while `CMakeLists.txt` lists `assets/facial_expressions/*` as sources → **fresh clone fails to configure/build** (see B-14).
- README mentions `run_walk_test_actions.json`/`anim_run_walk_test.usda` under `assets/…` which are not tracked.
- The foundational-law test (`test_general_llm_planner_zero_hardcoded_coordinates`) currently fails for a compile reason, so the "automated enforcement" is not actually enforcing. Note `llm_planner.py` still contains the literal text ``pos(...)`` in docstring/system prompt (lines 6, 206); make sure the scan regex ignores comments/strings that *forbid* it.

### 🔴 B-14 – Build not portable (hard-coded paths, missing assets) — *Verified*
- `CMakeLists.txt:8` `set(CMAKE_PREFIX_PATH /Users/vishaljose/Qt/6.11.1/macos)` – overrides any user-supplied prefix.
- `.vscode/launch.json`, `tasks.json` and five Python modules hard-code `/Users/vishaljose/Qt/...`.
- `CMakeLists.txt:34-50` lists `assets/facial_expressions/*` in `add_executable`, but `assets/` is git-ignored → configure error on a clean checkout.
- `target_include_directories … /opt/homebrew/include` (Apple-silicon Homebrew only).
- OpenUSD is the **Apple system `libusd_ms`** with headers fetched by `scripts/setup_openusd_sdk.sh` at configure time (network-dependent `execute_process`, result not checked).
- **Suggested fix:** `if(NOT DEFINED CMAKE_PREFIX_PATH)`/env var `QT_DIR`; use `HEADER_FILE_ONLY` list only when files exist; `find_package(pxr)` or a cache variable for USD; check `execute_process` result and `message(FATAL_ERROR …)`.

### 🟡 B-15 – Python interpreter discovery duplicated and fragile — *Verified*
- `.venv/bin/python` vs `.venv/bin/python3` are used inconsistently (`MainWindow.cpp:141,975`, `ShotEngine.cpp:702`, `UsdViewportWidget.cpp:802`, `main.cpp:94`); only `main.cpp` has fallback logic, the rest do not honour `PAINTERSPACE_PYTHON` consistently. `compile_shot.py` / `compiler.py` also mutate `sys.path` with the venv site-packages.
- **Suggested fix:** one `findPython()` helper (C++) and use `sys.executable` in Python subprocess calls.

### 🟡 B-16 – Generated artefacts and bytecode committed — *Verified*
- `scripts/__pycache__/*.pyc` is tracked despite `__pycache__/` being in `.gitignore` (added before ignore rule).
- 278 frame PNGs (`scenes/*/shot_*/frames/…`), 308 images/videos total, 74 MB `scenes/`, 210 MB `.git`. `task_queue/completed_*.json` run logs are committed too.
- **Suggested fix:** `git rm --cached` these, add `scenes/**/frames/`, `task_queue/`, `*.pyc` to `.gitignore`, consider Git LFS for thumbnails/videos.

### 🟡 B-17 – Duplicate/unused tooling scripts — *Suspected*
- `tools/inspect_asset.py` (8 lines), `inspect_asset_dimensions.py` (13), `validate_rig.py` (13), `export_poseable_glb.py` (23), `render_asset.py`, `render_rest.py`, `test_pose.py` are tiny Blender one-offs with no docs or CLI args; `tools/test_*.py` are not discoverable by `unittest discover -s tests`.

---

## 4. Architecture & design observations

1. **Two competing generation pipelines.** `generate_usda_scene.py` + `ShotEngine::buildOpenUsdStage` (LLM emits USDA/ShotSpec JSON) vs. the shot library (LLM emits function calls). Decide which is the long-term path, deprecate the other, or isolate it behind a clear "legacy" flag. Currently README documents both without guidance on which the GUI uses in which mode (API-key vs. "agent mode").
2. **"Agent mode" via file queue** (`task_queue/pending_request.json`, `completed_*.json`) is a hand-rolled IPC. There is no locking, schema version, timeout, or stale-request detection (`main.cpp:335` just deletes `llm_response.json`). Concurrent GUI/CLI use can race.
3. **Giant translation units.** `UsdViewportWidget.cpp` (2.7k), `MainWindow.cpp` (2.4k), `motion_synthesis.py` (2.1k), `llm_planner.py` (1.3k), `world_state.py` (1.3k), `main.cpp` (1.25k – mixes CLI parsing, HTTP, rendering, and a test suite). Split by responsibility (CLI commands, networking, test harness; gait/vehicle/social/combat synthesizers; offline planner vs. API client).
4. **`main.cpp` contains an integration test suite** (`--usd-test-suite`) in production code. Move to a separate test target (`ctest`) and use temp copies (see B-10).
5. **Planner offline mode contradicts the "foundational law".** README forbids keyword scripts, yet `_plan_general_semantic` is a regex/keyword clause mapper (`then`, `after`, `while` …). That is acceptable as a fallback, but it is still keyword-driven; coverage of unseen prompts should be measured (see §5 test ideas) and the law's wording relaxed or the fallback labelled.
6. **Planner has no hard cap on actors/beats/duration** before compile → large prompts can create very long timelines and 48 Hz collision validation cost grows O(frames × actor-pairs × capsules (24)).
7. **Error taxonomy.** `StructuredError` exists, but many failures are plain strings in `validation.bake_errors`, `lint_warnings`, `camera_warnings`. Unify into one schema so the LLM retry loop can consume all of them.
8. **Two coordinate/heading conventions.** README: north = −Z, `heading_deg` clockwise, `rotateY = -heading`; tests (`pos(0,0,4,180)`) and compiler use `pos(...)` strings. Keep conversions in one module and unit-test them (round-trip).
9. **Security hygiene.** API key is read only from env (good). But: `Authorization` bearer is placed in request headers in three places; user-supplied `PAINTERSPACE_LLM_URL` is posted to without scheme validation (can send the key to an arbitrary host – only send the key to the OpenAI endpoint unless explicitly allowed). Prompts and scene data are sent to a third party – add a notice/opt-in.
10. **Validation limits.** The 8 MiB/10 000-prim limits and path whitelisting in `ShotEngine` apply to the legacy path; confirm the shot-library output goes through the same gate before being opened by Hydra.

---

## 5. Improvements / backlog

### 5.1 Quick wins (≤ 1 day each)
| ID | Item | Related |
|---|---|---|
| I-01 | Delete the `__main__` launcher blocks in five library modules | B-02 |
| I-02 | Replace `hash()` with a stable hash; add `PYTHONHASHSEED` determinism test | B-03 |
| I-03 | Log (don't swallow) exceptions in `scene_manager.py`, `compile_shot.py`, `compiler.py`, `bake_shot_animations.py` | B-04, B-05 |
| I-04 | `git rm --cached` pyc, frames, `task_queue/completed_*`; update `.gitignore` | B-16 |
| I-05 | Honour `CMAKE_PREFIX_PATH`/`QT_DIR`; remove hard-coded user paths in CMake, `.vscode`, Python | B-14 |
| I-06 | Aggregate stride-linter warnings per beat | B-07 |
| I-07 | Single LLM config helper (model, URL, temperature, timeout) | B-11 |
| I-08 | Make binder error message list skeleton candidates; add fixture/skip logic to tests | B-01 |
| I-09 | Label expected-failure rows in the collision test and assert on them | B-12 |

### 5.2 Medium effort
| ID | Item |
|---|---|
| I-10 | Async `QProcess` for validation, render, encode, manifest sync; progress bar + cancel button |
| I-11 | CI (GitHub Actions / local script): `unittest discover`, `test_all_phases`, linter, `ruff`/`black`; macOS build job; fail on test skips that hide missing assets |
| I-12 | Replace hand-rolled queue with a small typed protocol (JSON schema + atomic write/rename + timestamps/timeouts + lock file) |
| I-13 | Auto-derive beat duration from path length and clip nominal speed; auto-select walk/jog/run |
| I-14 | Extract `CommandLine` handlers from `main.cpp` into a `Cli` module; move test suite to `tests/cpp` and a CMake `add_test` |
| I-15 | Add `requirements.txt` pins (`numpy` is imported by `bake_shot_animations.py` but is not listed – only `usd-core`, `Pillow`) |
| I-16 | Generate README facts (verb count, phase list) from code/`export_tool_schemas()` to prevent drift |
| I-17 | Property-based/fuzz tests for `ReferenceResolver` (`beside`, `in_front_of`, `nearest_to`) and for the coordinate/heading conversion helpers |
| I-18 | Planner prompt corpus (e.g. 50 diverse prompts) run in offline mode as a regression benchmark: success rate, compile time, collision count |

### 5.3 Larger / strategic
| ID | Item |
|---|---|
| I-19 | Pick one generation architecture (shot-library vs. legacy USDA text) and retire the other |
| I-20 | Split giant files (see §4.3) and add type hints / `mypy` to `tools/shot_library` |
| I-21 | Performance: vectorise the 48 Hz capsule-vs-OBB checks with NumPy and add broad-phase (AABB sweep) to cut O(n²) pairs; cache baked animation by plan hash |
| I-22 | Cross-platform support: currently macOS-only (system `libusd_ms`, Metal, `/Users/...`). Consider documented Linux/Windows build via `usd-core`/OpenUSD source build |
| I-23 | UX: undo for scene-level (main scene) edits, visible validation errors panel (bake/collision/lint) instead of log-only, LLM-offline notice |
| I-24 | Data management: Git LFS or external storage for `assets/` and render outputs, plus a `scripts/fetch_assets.sh` so a fresh clone can build and pass tests |

---

## 6. Suggested order of work

1. **Restore a green test suite** – B-01 (+ fixtures), B-12.
2. **Correctness/determinism** – B-03, B-04, B-05, B-06, B-07.
3. **Portability & hygiene** – B-14, B-02, B-16, B-15, B-11.
4. **Responsiveness & safety** – B-08, B-09, B-10.
5. **Architecture clean-up** – §4 items 1–4, then §5.3.

## 7. Appendix – how findings were obtained

```bash
git ls-files; wc -l …                       # inventory
git grep -nE "except Exception:|hash\(|/Users/vishaljose|waitForFinished|/tmp" -- src tools
.venv/bin/python -m unittest discover -s tests
.venv/bin/python tools/shot_library/test_all_phases.py
.venv/bin/python tools/collision/test_collision_tools.py
```

Not covered: building/running the C++ binary and GUI interaction, live LLM calls, real Hydra rendering, and review of generated USD data under `scenes/`. Items marked *Suspected* should be confirmed before work starts.

