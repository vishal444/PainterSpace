# PainterSpace Shot Function Library: Architecture Audit, Bug Analysis & Next-Generation Improvements

**Date:** October 20, 2026  
**Scope:** End-to-end audit of `tools/shot_library/`, `tools/collision/`, `tools/bake_animation.py`, `tools/generate_usda_scene.py`, and `src/main.cpp` against the *Shot Function Library (Plan, Logic, Execution)* specification.

---

## Executive Summary

The **Declarative Shot Plan Compiler** architecture (`Prompt -> Function Catalog -> ShotPlan -> TimelineResolver -> MotionSynthesizer -> UsdSkel Baker -> Validators -> shot.usda`) is **fundamentally the right solution** for PainterSpace. Moving 4D space-time math, Euler angle conventions, UsdSkel clip baking, and camera frustum solving out of raw LLM text generation and into deterministic Python code eliminates the root causes of broken shots.

However, a deep audit of the current implementation reveals **critical architectural disconnects and bugs** that currently prevent the system from working on arbitrary user prompts, allow physical and visual defects to slip through, and make human movement look robotic.

### Quick Scorecard of Current Implementation

| Subsystem | Design Quality | Implementation Status | Critical Finding |
| :--- | :---: | :---: | :--- |
| **1. LLM Planner (`llm_planner.py`)** | High | **Prototype / Regex Stub (25%)** | Uses ~10 hardcoded `if "keyword" in prompt:` branches with hardcoded coordinates; any other prompt falls back to a single actor walking down `lane_east`. |
| **2. C++ & CLI Integration (`main.cpp`, `generate_usda_scene.py`)** | High | **Disconnected (30%)** | API Mode still prompts OpenAI for raw `#usda 1.0` text directly instead of using `shot_library`. |
| **3. Function Catalog & Identity (`function_catalog.py`, `actor_registry.py`)** | High | **Well Implemented (85%)** | Solid ID/label separation, appearance variants, customData persistence, and ~40 catalog methods. |
| **4. Semantic References (`semantic_references.py`)** | High | **Partial / Buggy Timing (65%)** | Resolves all targets at `t = 0.0` before motion synthesis instead of at beat start time (`t_start`), breaking interactions with moving targets. |
| **5. Timeline Resolver (`timeline_resolver.py`)** | High | **Partial (70%)** | Good DAG cycle detection and idle fill, but uses hardcoded default durations (`3.0s`) instead of physics-derived `distance / speed`. |
| **6. Human Motion Realism (`motion_synthesis.py`, `bake_animation.py`)** | Medium | **Robotic / Linear (50%)** | Straight-line lerp without pathfinding; root translation speed decoupled from clip stride speed (causing foot skating); no turn-in-place, acceleration curves, upper-body layering, or gaze/foot IK. |
| **7. Collision Avoidance & Validation (`tools/collision/`)** | High | **Good Math, Disconnected Pipeline (60%)** | `colliders.py` and `plan_path.py` have strong geometry math, but `MotionSynthesizer` **never calls `plan_path.py`**, vehicle-vs-vehicle/prop collisions are never checked, and actor-vs-actor checks ignore limbs. |
| **8. Camera & Prompt Adherence (`camera_controller.py`, `compiler.py`)** | Medium | **Shallow Heuristics (45%)** | Camera uses hardcoded coordinates ignoring actor positions; frustum check only tests `dist > 35m` (ignores camera angle/FOV); prompt verification only checks 4 regexes. |

---

## Part 1: Detailed Implementation Audit & Bug Analysis

### 1.1 `LLMShotPlanner` is a Hardcoded Keyword Script, Not a Real LLM Planner
- **Location:** [`tools/shot_library/llm_planner.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/llm_planner.py#L46-L588)
- **What is happening:** `_plan_from_prompt_raw()` does **not** call an LLM with tool/function definitions. Instead, it checks ~10 hardcoded string conditions (`if "stair" in p:`, `if "drone" in p:`, `if "two cars" in p:`, `if "10" in p:`, `if "parkour" in p:`).
- **Consequences:**
  1. **Silent Fallback on Novel Prompts:** Any prompt that does not match one of those ~10 keyword combinations falls through to [lines 583–588](file:///Users/vishaljose/PainterSpace/tools/shot_library/llm_planner.py#L583-L588), which ignores the user's prompt entirely and outputs a default single character walking from `lane_east.start` to `lane_east.stop_mark`.
  2. **Violates "Names, Not Numbers" Principle:** Inside the hardcoded branches (e.g., [lines 81–132](file:///Users/vishaljose/PainterSpace/tools/shot_library/llm_planner.py#L81-L132)), the planner passes raw coordinate strings like `target="pos(0.0, 1.70, -11.37, 180.0)"` to `run_to` instead of calling `self.catalog.climb_stairs(...)` or using semantic anchors.

### 1.2 `src/main.cpp` and `tools/generate_usda_scene.py` Still Ask the LLM for Raw `#usda 1.0`
- **Locations:** [`src/main.cpp`](file:///Users/vishaljose/PainterSpace/src/main.cpp#L613-L720) and [`tools/generate_usda_scene.py`](file:///Users/vishaljose/PainterSpace/tools/generate_usda_scene.py#L202-L233)
- **What is happening:** When generating a shot in API mode, both C++ and Python still send a system prompt instructing the model: *"Return only a complete OpenUSD ASCII layer beginning with #usda 1.0"*. Neither file invokes `ShotCompiler` or `FunctionCatalog`.
- **Agent Queue Disconnect:** In [`src/main.cpp`](file:///Users/vishaljose/PainterSpace/src/main.cpp#L367-L368), `writeAgentTaskQueue` still instructs the agent to author raw OpenUSD ASCII and use `tools/bake_shot_animations.py` rather than building a `shot_plan.json` and compiling via `tools/shot_library/compile_shot.py`.

### 1.3 Target Resolution Happens at `t = 0.0` Instead of Beat Start Time
- **Location:** [`tools/shot_library/compiler.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L211-L235)
- **Bug:** Section 5 of the design plan specifies: *"A reference is resolved at the moment the beat starts, not at planning time, so a moving target is followed correctly."* However, `ShotCompiler.compile()` resolves **all** semantic targets in step 4 ([line 220](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L220)) using `actor_states_at_start` at `current_time_s = 0.0` *before* motion synthesis runs in step 5!
- **Impact:** When `car_1` drives from `lane_east.start` (`z = -11.0`) to `stop_mark` (`z = 5.0`), and `human_1` executes `run_to("human_1", target="car_1:hood_strike", after="car_1.stopped")`, `car_1:hood_strike` is resolved against `car_1`'s initial position at `t = 0.0` (`z = -11.0`). The character runs to the empty starting point of the car instead of where the car stopped.
- **Fix:** Move semantic target resolution *inside* `MotionSynthesizer.synthesize_actor()` at each beat's `t_start`, sampling `other_performances[target_actor].sample(t_start)` for dynamic actors.

### 1.4 Locomotion Speed vs. Duration Mismatch Causes Foot Sliding (Moonwalking)
- **Locations:** [`tools/shot_library/timeline_resolver.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/timeline_resolver.py#L346-L365) and [`tools/shot_library/motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L566-L640)
- **Bug:**
  1. `TimelineResolver._default_duration_for_function()` assigns a fixed `3.0` seconds to every `walk_to`, `run_to`, and `approach` beat unless `for_duration` was explicitly passed.
  2. In `MotionSynthesizer.synthesize_actor()` ([lines 596–622](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L596-L622)), the character's root position is linearly interpolated across `dist` over `0.88 * dur` (so actual ground speed is $v_{\text{actual}} = \frac{\text{dist}}{0.88 \times \text{dur}}$), while the animation clip (`walk` or `run`) is always baked with `speed = 1.0` ([line 596](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L596))!
  3. Moreover, during the last 12% of the beat (`u > 0.88`, [lines 622–627](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L622-L627)), the character stands completely still (`pos = list(end_pos)`, `inst_spd = 0.0`) while rotating toward `target_facing`, **but the `walk` or `run` animation clip continues playing at full speed in place** until `t_end`!
- **Impact:** Characters visibly skate across the ground when `dist` does not equal `1.4 * 2.64 = 3.7m`, and march/sprint in place at the end of every walk/run beat while turning.

### 1.5 Unimplemented & Stubbed Catalog Functions in `MotionSynthesizer`
- **Locations:** [`tools/shot_library/function_catalog.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/function_catalog.py) vs. [`tools/shot_library/motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py)
- **Bug:** The following functions exist in `FunctionCatalog` and create beats in `ShotPlan`, but have **no handler** in `MotionSynthesizer.synthesize_actor()`:
  - Locomotion: `follow`, `flee_from`, `stand_at`
  - Interactions: `stand_up`, `pick_up`, `put_down`
  - Reactions: `fall`
  - Social: `greet`, `huddle`, `disperse`
  - Combat: `push`, `shove`, `take_cover`
  - Vehicles: `reverse_to`, `open_door`, `close_door`, `set_lights`
- **Social Scene Stubs (`converse`, `argue`):** In [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L1026-L1042), `converse` and `argue` do not implement Section 8.5 of the spec at all:
  - No circle/arc formation solver around `center`.
  - Participants do not walk into formation or turn to face each other.
  - No turn-taking schedule (speaker vs. listener).
  - For `argue`, `clip_pool` ([line 1028](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L1028)) is `["head_hit_during_fight", "reaction", "pointing_forward"]`—causing speakers in an argument to randomly play a getting-punched-in-the-head reaction clip in a loop.

### 1.6 `dodge` Ignores the Threat's Position and Velocity Vector
- **Location:** [`tools/shot_library/motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L641-L690)
- **Bug:**
  - `threat_perf = other_performances.get(threat_actor)` is retrieved on [line 644](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L644) and **never used**.
  - `dodge_dx = -d_lateral` and `dodge_dz = 0.2` ([lines 663–664](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L663-L664)) are hardcoded, so the character always dodges toward `-X`, even if the car is approaching along the `X` axis or if `-X` steps *into* the car's path or off a bridge!
  - `dodge_heading` is computed on [line 666](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L666) and never used (`heading_deg=curr_heading` is written on [line 684](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L684)).

### 1.7 `attack` Drops Multi-Move Combos and Masks Collisions Instead of Solving Standoff
- **Location:** [`tools/shot_library/motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L881-L918)
- **Bug:**
  - Only `moves[0]` is scheduled (`strike_clip = moves[0]`, [line 885](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L885)); subsequent moves in `moves=["left_hook", "elbow_punch", "kick_to_the_groin"]` are discarded.
  - The attacker does not automatically step into strike standoff range (`reach_m` from target collider surface) or turn to face the target.
  - Instead of allowing only the striking hand/foot (`strike_limb`) during `contact_window_rel`, [lines 891–904](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L891-L904) add `Spine`, `Spine1`, `Spine2`, `Neck`, `Head`, `Hips`, and all limbs to `allow_collisions.json` with `max_depth_m = 0.35`, effectively turning off torso collision detection between combatants.

### 1.8 Camera Controller & Frustum Validator Bugs
- **Location:** [`tools/shot_library/camera_controller.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/camera_controller.py#L33-L169)
- **Bugs:**
  1. **Hardcoded Camera Positions:** `synthesize_camera()` places the camera at fixed world coordinates (`[0.0, 2.2, 10.0]` for `medium`, `[0.0, 4.0, 16.0]` for `wide`, `[6.8, 2.5, 6.5]` for `three_quarter`) regardless of where the actors actually are in the scene! If the action happens near a tree at `(-3.2, 0, 4.7)` or down the street at `z = -12`, the camera points at `(0, 0, 0)` and misses the actors.
  2. **Broken Frustum Check:** `validate_frustum()` ([lines 144–169](file:///Users/vishaljose/PainterSpace/tools/shot_library/camera_controller.py#L144-L169)) never projects 3D actor bounding boxes into the camera's view matrix or checks horizontal/vertical FOV (`focal_length`, `horizontal_aperture = 36`, `vertical_aperture = 20.25`). It only checks `if dist > 35.0:`. An actor standing 2 meters *behind* the camera passes validation.
  3. **Ignored Lint & Camera Failures in `compiler.py`:** In [`compiler.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L391-L405), when `lint_passed` or `camera_passed` is `False`, `validation_report["success"]` is **not** set to `False`!

---

## Part 2: Deep Analysis of the Collision Avoidance Logic

### 2.1 What Is Good in `tools/collision/`
The low-level geometry and USD inspection primitives in [`tools/collision/colliders.py`](file:///Users/vishaljose/PainterSpace/tools/collision/colliders.py), [`tools/collision/plan_path.py`](file:///Users/vishaljose/PainterSpace/tools/collision/plan_path.py), and [`tools/collision/validate_collisions.py`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py) are well-conceived:
1. **Posed Skeleton Capsule Evaluation (`CharacterModel`):** Evaluates actual `UsdSkel` bone transforms (`ComputeJointSkelTransforms`) at sub-frame resolution (`substeps=2`, i.e., 48 Hz) so fast limb swings or sprinting characters cannot tunnel through thin colliders.
2. **Live Part Transforms (`_part_transform`):** Doors, hoods, and trunks are evaluated at their actual time-sampled USD transforms at time `tc`, so an opening car door sweeps out its true 3D volume.
3. **Multi-Level & Stair Portal Awareness:** `plan_path.py` slices obstacles by floor height bands (`[floor_y + 0.15m, floor_y + 2.10m]`) and routes across floor levels via stair centerlines (`scene_stairs`).

### 2.2 Critical Flaws in the Current Collision Avoidance & Validation Pipeline

1. **Critical Disconnect: `MotionSynthesizer` Never Calls `plan_path.py`**
   - Although [`plan_path.py`](file:///Users/vishaljose/PainterSpace/tools/collision/plan_path.py) implements A* obstacle routing, [`MotionSynthesizer`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L566-L640) never imports or calls it. All `walk_to`, `run_to`, and `approach` beats synthesize a straight line from start to target, walking straight through cars, trees, and other characters.

2. **No Vehicle-vs-Vehicle or Vehicle-vs-Environment Collision Detection**
   - In [`validate_collisions.py`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py#L70-L71):
     ```python
     actors = [o for o in objs if o.is_actor]
     props = [o for o in objs if not o.is_actor and o.protected]
     ```
   - `is_actor` is `True` **only** for skinned human characters ([`colliders.py`](file:///Users/vishaljose/PainterSpace/tools/collision/colliders.py#L196)). Cars (`cars.car1`) are classified as `props`.
   - The validator only tests `actors` vs. `props` and `actors` vs. `actors`. **It never tests moving `props` (vehicles) against other `props` (other vehicles, buildings, trees, barriers).** Two cars can drive straight through each other or through a building without triggering a single violation.

3. **Actor-vs-Actor Validation Ignores Arms and Legs**
   - In [`validate_collisions.py`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py#L225-L234), character-to-character collision only checks `models[a.name].torso_capsules(tc, xc)` (`hips`, `spine`, `neck`, `head`).
   - All arm, forearm, hand, thigh, calf, and foot capsules in `models[a.name].segs` are skipped during actor-vs-actor checks, allowing characters standing close together or fighting to pass their arms and legs through each other's bodies.

4. **Static Time-Union Grid Blocks Valid Dynamic Paths (`plan_path.py`)**
   - `obstacles(stage, db, times, ...)` ([`plan_path.py`](file:///Users/vishaljose/PainterSpace/tools/collision/plan_path.py#L74-L111)) takes the **static union** of all obstacle positions across all `times` in `[t0, t1]`.
   - If a car drives along a lane from `t = 0s` to `t = 4s`, its swept volume blocks the entire lane for the whole `[0s, 4s]` window, preventing a pedestrian from crossing behind the car at `t = 3.5s` after the car has already passed `(x, z)`.
   - Conversely, when multiple characters are synthesized sequentially in `compiler.py`, `plan_path.py` reads from a USD stage file on disk—which hasn't been written yet during `MotionSynthesizer` execution!

5. **Destructive Auto-Repair (`repair_violations` in `validate_collisions.py`)**
   - [`repair_violations()`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py#L443-L503) takes an actor's position at `t0` (start of shot) and `t1` (end of shot) and overwrites the entire `xformOp:translate` and `xformOp:rotateY` timeSamples with a single constant-speed A* path from `t0` to `t1`.
   - For any multi-beat shot (e.g., wait -> dodge -> run to car -> attack), `--repair` destroys the entire choreography timing and replaces it with a single slow walk from start to end.

---

## Part 3: Recommended Corrections & Improvements

### 3.1 Unifying Collision Avoidance Directly Inside `MotionSynthesizer` (Space-Time A* + ORCA)
1. **In-Memory Obstacle & Trajectory Map (No USD Stage Required Prior to Synthesis):**
   - Pass `scene_data` (static world-space `BoxGroup` footprints from `scene.usda` and manifest colliders) plus `other_performances` (already-synthesized moving trajectories of vehicles and higher-priority actors) directly into `MotionSynthesizer`.
2. **Time-Expanded A* (`(x, z, t)` Grid) for Locomotion Beats:**
   - When planning a route for beat `[t_start, t_end]` at speed $v_{\text{gait}}$, evaluate dynamic obstacles (moving vehicles and other characters) at the **exact time** $t(s) = t_{\text{start}} + \frac{s}{v_{\text{gait}}}$ when the actor reaches arc-length $s$, rather than taking a static union over `[t_start, t_end]`.
3. **Reciprocal Velocity Obstacles (RVO / ORCA) for Multi-Character Crowds & Social Formations:**
   - When 2 to 10+ characters move simultaneously (e.g., walking into a social circle, dispersing, or crowd combat), apply a lightweight 2D ORCA (Optimal Reciprocal Collision Avoidance) velocity adjustment step at each frame `dt` so characters naturally yield shoulder space and never clip through each other.
4. **Full-Body Capsule Checks + Vehicle-vs-World Validation:**
   - In [`validate_collisions.py`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py#L225), replace `torso_capsules()` with all bone segments `models[a.name].segs` (plus hand spheres) for Actor-vs-Actor checks.
   - Add a **Moving Prop vs. All Props** pass in `validate_collisions.py` using Separating Axis Theorem (SAT) on the 3D oriented bounding boxes (`corners_world()`) of any vehicle/prop with time-varying transforms against all other vehicles and static scene colliders.
5. **Beat-Local Standoff Binary Search Instead of Global `--repair`:**
   - If `validate_collisions.py` reports a penetration during a specific beat window `[first_s, last_s]` (e.g., during an `attack`, `open_part`, or `approach` beat), feed `push_out_m` back into `MotionSynthesizer` for **that specific beat's target offset** (up to 5 bounded iterations) rather than overwriting the actor's entire timeline.

---

### 3.2 Making Human Asset Movements Genuinely Realistic

Currently, human movement looks mechanical because root translation is a constant-velocity straight line with instantaneous speed changes, single-layer full-body clip switching, and no biomechanical coupling. Here are the 6 high-impact upgrades for human realism:

#### 1. Stride-Locked Root Velocity & Ease-In / Ease-Out Acceleration Curves
- **Problem:** Linear interpolation (`s = u / 0.88`) starts and stops instantaneously and decouples root speed from the animation clip's stride length.
- **Solution:**
  - Every locomotion clip in `manifest.json` defines its natural stride speed $v_{\text{clip}}$ (`1.4 m/s` for `walk`, `2.2 m/s` for `jog`, `4.2 m/s` for `run`).
  - Compute the path length $L = \sum \|\Delta \mathbf{p}_i\|$ from the smoothed A* waypoints.
  - If the user/plan did not lock an exact duration, set beat duration from physics:
    $$T_{\text{beat}} = \frac{L}{v_{\text{clip}}} + T_{\text{accel}} + T_{\text{decel}}$$
  - Use a trapezoidal velocity profile $v(t)$ (smooth ramp-up over $0.35\text{ s}$, cruise at $v_{\text{clip}}$, smooth deceleration over $0.40\text{ s}$).
  - In `bake_animation.py`, dynamically warp the clip phase $\phi(t)$ by the instantaneous root displacement $\Delta s(t)$:
    $$\Delta \phi(t) = \frac{\|\mathbf{p}(t + \Delta t) - \mathbf{p}(t)\|}{L_{\text{stride}}}$$
    where $L_{\text{stride}} = v_{\text{clip}} \cdot T_{\text{clip}}$. Because clip phase advances strictly in proportion to distance traveled per frame, **foot sliding becomes mathematically impossible** even during acceleration, deceleration, and turns!

#### 2. Smooth B-Spline Trajectories with Banking & Anticipatory Turn-in-Place
- **Problem:** Characters snap between piecewise-linear A* segments or spin in place while playing a walking clip during the last 12% of a beat (`u > 0.88`).
- **Solution:**
  - Fit a centripetal Catmull-Rom spline through the A* waypoints (clamped against obstacle keep-out zones).
  - **Initial Turn-in-Place Phase:** If the initial heading $\theta_0$ differs from the initial path tangent $\theta_{\text{path}}$ by $> 35^\circ$, insert a dedicated $0.3\text{ s}$ turn-in-place phase before forward translation begins.
  - **Centripetal Torso Banking on Curves:** When running along a curved path with angular velocity $\omega = \frac{d\theta}{dt}$ and speed $v$, add a subtle inward roll/lean on the root or spine:
    $$\text{roll}_{\text{deg}} = \text{clamp}\left(-k_{\text{bank}} \cdot v \cdot \omega,\ -8^\circ,\ +8^\circ\right)$$

#### 3. Upper-Body / Lower-Body Skeletal Layering in `bake_animation.py`
- **Problem:** Currently, [`bake_animation.py`](file:///Users/vishaljose/PainterSpace/tools/bake_animation.py#L280-L293) evaluates only **one** clip at a time (`action_at(actions, time)`) across all joints. A character cannot walk while waving, point while arguing and stepping forward, or look startled while jogging.
- **Solution:**
  - Extend `bake_animation.py` to support **two animation tracks**: `base_actions` (full body / locomotion) and `overlay_actions` (upper body / gestures / reactions).
  - Define an upper-body joint mask rooted at `Spine` / `Spine1` (`Spine`, `Spine1`, `Spine2`, `Neck`, `Head`, `LeftShoulder`, `LeftArm`, `LeftForeArm`, `LeftHand`, `RightShoulder`, `RightArm`, `RightForeArm`, `RightHand`) with feather weights ($0.4$ on `Spine`, $0.75$ on `Spine1`, $1.0$ on `Spine2` and above).
  - Slerp `overlay_actions` rotations onto `base_actions` rotations using the per-joint mask weight. This immediately unlocks walking + talking, running + looking back, and varied posture during social scenes.

#### 4. Procedural Gaze / Head & Neck Look-At IK
- **Problem:** When `look_at`, `react`, `converse`, or `argue` is called, characters only rotate their entire rigid root transform (`rotateY`) or stand frozen looking straight ahead.
- **Solution:**
  - Add a procedural post-pass in `bake_animation.py` driven by a `look_at_targets` timeline (`[{"start_s": t0, "end_s": t1, "target_pos": [x, y, z]}]`).
  - Distribute the horizontal/vertical aim angle toward `target_pos` across `Spine2` ($15\%$), `Neck` ($35\%$), and `Head` ($50\%$), clamped to human anatomical limits ($\pm 55^\circ$ yaw, $\pm 35^\circ$ pitch) with smooth Critically Damped Spring interpolation.

#### 5. Procedural Idle Micro-Motion & Breathing (Eliminating "Mannequin Freeze")
- **Problem:** In [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L1064-L1075), every `wait` or `idle` beat plays `"acknowledging"` (which is a non-looping 1.5s nod gesture that freezes on its last frame for the rest of the wait!).
- **Solution:**
  - Add dedicated looping `idle_neutral`, `idle_breathing`, and `idle_look_around` clips to `manifest.json`.
  - In `bake_animation.py`, layer a subtle procedural breathing & weight-shift sine wave ($\sim 0.25\text{ Hz}$ on `Spine1`/`Spine2` pitch and $\sim 0.12\text{ Hz}$ lateral hip sway with per-actor phase offset `hash(actor_id)`) whenever an actor is stationary, so crowds and listeners look alive and organic.

#### 6. Two-Bone Analytical Foot IK for Ground, Curbs, and Stairs
- **Problem:** Even with smooth root elevation $y(s)$ on stairs or uneven ground, in-place animation clips lift and plant feet relative to a flat $y=0$ plane, causing feet to hover above or clip into individual stair treads.
- **Solution:**
  - Implement Phase 7 of [`docs/STAIRS_AND_LEVELS_SPEC.md`](file:///Users/vishaljose/PainterSpace/docs/STAIRS_AND_LEVELS_SPEC.md#L184-L185): during contact phases of each foot (`LeftFoot`, `RightFoot`), raycast/query the exact tread or surface height $y_{\text{surface}}(x_{\text{foot}}, z_{\text{foot}})$, lock the ankle world XZ position during stance phase (eliminating residual foot skate), and solve an analytical two-bone IK on `UpLeg -> Leg -> Foot` plus pelvis height clamping (`Hips.y`).

---

### 3.3 Guaranteeing Prompt Adherence: Making the System Diligently Fulfill Every User Condition

To ensure that **any** prompt produces a shot that faithfully renders every entity, spatial relationship, action sequence, mood, and camera framing requested by the user:

#### 1. Replace Regex Matching in `LLMShotPlanner` with True Multi-Turn LLM Tool Calling
- Expose `FunctionCatalog`'s methods as strict JSON Schema tools to the LLM (both in OpenAI API mode and in Gemini/Claude Agent mode).
- Use a **2-Stage Structured Generation Loop**:
  1. **Stage A — Prompt Decomposition & Requirement Checklist (`PromptContract`):**
     Before calling any placement or beat functions, the planner extracts an explicit machine-verifiable checklist from the user's prompt:
     - `required_actors`: count, roles, gender/appearance descriptors, initial spatial relations (e.g., *"inside car_1"*, *"near the oak tree"*).
     - `required_props`: environment assets or props mentioned in the prompt.
     - `required_beats`: ordered sequence of actions/interactions per actor (`actor -> verb -> target -> timing_relation`).
     - `required_camera`: shot scale (`wide`, `medium`, `close`), angle/elevation (`drone`, `low_angle`, `over_the_shoulder`), and movement (`push_in`, `track_with`, `orbit`).
  2. **Stage B — Tool Execution with Immediate Feedback:**
     The LLM calls `place`, `walk_to`, `drive_to`, `dodge`, `argue`, `camera_frame`, etc. Each call validates references and physical feasibility immediately, returning resolved positions, durations, and event names (`beat_1.stopped`).

#### 2. Upgrade `_verify_prompt_adherence()` into a Full Semantic & Spatio-Temporal Contract Verifier
- Currently, [`compiler.py:_verify_prompt_adherence()`](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L852-L945) only checks 4 hardcoded regexes (`couple`, `drone`, `happy->surprise`, and prop nouns).
- Upgrade it to verify the compiled USD stage and resolved timeline against the `PromptContract`:
  1. **Entity & Count Verification:** Every actor and prop in `PromptContract.required_actors` and `required_props` exists on the composed `shot.usda` stage and is visible (`ComputeVisibility != invisible`).
  2. **Action & Sequence Verification:** Every required action in `PromptContract.required_beats` maps to a non-empty `ResolvedBeat` in `timeline.beats` with matching causal ordering (`t_start(B) >= t_end(A)` for *"after A, do B"*).
  3. **Spatial Relation Verification:** Verify at the relevant timestamp $t$ that spatial prepositions in the prompt hold on the composed USD stage:
     - *"near / beside X"* $\implies \|\mathbf{p}_A(t) - \mathbf{p}_X(t)\| \le 3.0\text{ m}$
     - *"in front of X"* $\implies (\mathbf{p}_A(t) - \mathbf{p}_X(t)) \cdot \mathbf{f}_X(t) > 0$
     - *"inside vehicle V"* $\implies \mathbf{p}_A(t) \in \text{AABB}_{\text{cabin}}(V, t)$
     - *"on top of surface S"* $\implies |y_A(t) - y_S| < 0.08\text{ m}$
  4. **True 3D Camera Frustum & Occlusion Verification:**
     - At every sample step $t \in [0, T_{\text{end}}]$, transform the 3D world-space bounding box of each focal actor into `/World/ShotCam` normalized device coordinates (NDC $[-1, +1]^2$) using the camera's true perspective projection matrix (derived from `focalLength`, `horizontalAperture`, `verticalAperture`, and `xformOp:translate` / `xformOp:rotateXYZ`).
     - Verify that:
       - $z_{\text{cam}} < -\text{near\_clip}$ (actor is in front of the camera, never behind it!).
       - At least $80\%$ of focal actors' NDC bounding boxes lie within $[-0.9, +0.9]^2$ across their active beats.
       - Raycast from `/World/ShotCam` to each focal actor's chest/head to verify no large static building, tree, or vehicle completely occludes the primary subject from the camera!
  5. **Closed-Loop Auto-Revision with Visual/Structured Feedback:**
     - When any validator (`collision`, `shot_linter`, `frustum_occlusion`, or `prompt_contract`) fails, return the structured violation report (with exact beat IDs, timestamps, and suggested fixes) to the LLM planner for up to 3 automatic revision turns before final render.

---

## Part 4: Prioritized Action Roadmap (When Ready to Implement)

| Priority | Task | Files to Update | Impact |
| :---: | :--- | :--- | :--- |
| **P0** | **Dynamic Beat-Start Reference Resolution**: Resolve semantic targets inside `MotionSynthesizer` at `t_start` using live sampled trajectories of moving actors instead of at `t = 0.0`. | [`compiler.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L211-L235), [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py), [`semantic_references.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/semantic_references.py) | Fixes broken multi-beat interactions with moving vehicles and characters. |
| **P0** | **Wire `plan_path` A* + Catmull-Rom Splines + Stride-Matched Duration into `MotionSynthesizer`**: Route all locomotion beats around static & dynamic obstacles, compute duration from `path_length / gait_speed`, and lock animation playback speed to root velocity. | [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py#L566-L640), [`timeline_resolver.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/timeline_resolver.py), [`plan_path.py`](file:///Users/vishaljose/PainterSpace/tools/collision/plan_path.py) | Eliminates walking through props and eliminates foot skating/moonwalking. |
| **P0** | **True Camera Look-At Frustum Solver & Projection Validator**: Compute `/World/ShotCam` position and `rotateXYZ` from the 3D bounding-box union of `focus_actors`, and validate true perspective NDC frustum + line-of-sight occlusion. | [`camera_controller.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/camera_controller.py), [`compiler.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/compiler.py#L399-L405) | Guarantees focal actors are always inside the frame and never behind the camera or occluded. |
| **P1** | **Full-Limb Actor-vs-Actor & Vehicle-vs-World Collision Checks + Beat-Local Standoff Solver**: Check all bone capsules between actors, check OBB collisions for moving vehicles, and adjust standoff per-beat using `push_out_m`. | [`validate_collisions.py`](file:///Users/vishaljose/PainterSpace/tools/collision/validate_collisions.py), [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py) | Prevents limb clipping in fights/crowds and vehicle-to-vehicle penetrations. |
| **P1** | **Complete Missing Catalog Handlers & Social Formation Solver**: Implement circle/arc formations, turn-taking gesture schedules, `follow`, `flee_from`, `push`/`shove`, multi-move `attack` combos, and perpendicular vector `dodge`. | [`motion_synthesis.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/motion_synthesis.py) | Makes all ~40 functions in `FunctionCatalog` work as designed. |
| **P1** | **Upper/Lower Body Animation Layering, Gaze IK & Two-Bone Foot IK**: Add upper-body feather masking, spine/neck/head look-at IK, idle breathing, and foot-plant IK in `bake_animation.py`. | [`bake_animation.py`](file:///Users/vishaljose/PainterSpace/tools/bake_animation.py), [`manifest_schema.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/manifest_schema.py) | Transforms stiff single-clip playback into natural, expressive human motion. |
| **P2** | **Replace Regex Planner & Raw USDA Prompts with LLM Tool-Calling + `PromptContract` Verifier**: Connect `src/main.cpp` and `generate_usda_scene.py` to `ShotCompiler` and use LLM tool calling with closed-loop `PromptContract` verification. | [`llm_planner.py`](file:///Users/vishaljose/PainterSpace/tools/shot_library/llm_planner.py), [`generate_usda_scene.py`](file:///Users/vishaljose/PainterSpace/tools/generate_usda_scene.py), [`src/main.cpp`](file:///Users/vishaljose/PainterSpace/src/main.cpp) | Makes the system genuinely self-sufficient for any user prompt in both API and Agent modes. |

