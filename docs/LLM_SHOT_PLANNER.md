# PainterSpace General LLM Shot Planner Architecture

This document explains how the **PainterSpace LLM Shot Planner** ([`tools/shot_library/llm_planner.py`](../tools/shot_library/llm_planner.py)) translates any natural-language storyboard prompt into a verified, collision-free OpenUSD shot (`shot.usda` + `anim_*.usda`).

---

## 1. The Foundational Architectural Law

> [!IMPORTANT]
> **THE IRONCLAD RULE OF PAINTERSPACE:**
> [`LLMShotPlanner`](../tools/shot_library/llm_planner.py) must **always** be a general, scene-graph-driven, compositional planner — **never** a hardcoded keyword script (`if "keyword" in prompt:`) and **never** a generator of raw 3D coordinates (`pos(x, y, z)`).

### Why Hardcoded Scripts & Raw Coordinates Fail
1. **Hardcoded scenario branches (`if "stairs" in prompt:`) break on novel prompts**: A keyword script only works for the handful of demo sentences its author anticipated. A real storyboard studio requires a general system that can compose *any* combination of actors, vehicles, props, and actions.
2. **LLMs are bad at simultaneous 4D space-time math**: Asking an LLM to guess raw `(x, y, z)` coordinates, Euler angles (`rotateXYZ`), and frame numbers in its head leads to moonwalking characters, cars driving backward, actors standing outside the camera frustum, and limbs passing through solid doors.

### The Solution: Intent in the Planner, Physics in the Compiler
PainterSpace strictly separates **cinematic intent** from **3D math and physics**:
- **The Planner ([`llm_planner.py`](../tools/shot_library/llm_planner.py))** decides *who* is in the shot, *where* they start semantically, *what* high-level actions they perform, and *when* each action triggers relative to other events.
- **The Compiler ([`compiler.py`](../tools/shot_library/compiler.py), [`motion_synthesis.py`](../tools/shot_library/motion_synthesis.py), [`camera_controller.py`](../tools/shot_library/camera_controller.py), [`bake_animation.py`](../tools/bake_animation.py), [`validate_collisions.py`](../tools/collision/validate_collisions.py))** computes the exact 3D coordinates, obstacle-avoiding splines, stride-matched animation speeds, door hinge angles, camera frustum transforms, and 48 Hz skeletal capsule collision checks.

---

## 2. End-to-End Execution Flow

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 1. INPUTS                                                                  │
│    • User Storyboard Prompt ("A car stops; the driver gets out & argues")  │
│    • Live Scene Graph (scene.usda -> objects, zones, existing props/trees) │
│    • Asset Manifest (manifest.json -> anchors, surfaces, parts, stairs)    │
│    • Auto-Introspected FunctionCatalog Schema (~40 callable tools)         │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 2. LLM SHOT PLANNER (tools/shot_library/llm_planner.py)                    │
│                                                                            │
│    ┌──────────────────────────────────────────────────────────────────┐    │
│    │ Mode A: Live LLM Tool-Calling (_plan_with_llm_api)               │    │
│    │ • Sends System Prompt + Scene Context + Tool Schemas to LLM      │    │
│    │ • LLM returns structured JSON tool calls                         │    │
│    │ • _invoke_catalog_call() executes calls on FunctionCatalog       │    │
│    └───────────────────────────────┬──────────────────────────────────┘    │
│                                    │ (or offline fallback)                 │
│    ┌───────────────────────────────▼──────────────────────────────────┐    │
│    │ Mode B: Compositional Semantic Planner (_plan_general_semantic)  │    │
│    │ • Discovers/places scene props & environment context             │    │
│    │ • Extracts vehicles & human actors + semantic starting anchors   │    │
│    │ • Splits prompt into temporal/causal clauses (; / then / when)   │    │
│    │ • Maps each clause to FunctionCatalog calls & camera framing     │    │
│    └───────────────────────────────┬──────────────────────────────────┘    │
└────────────────────────────────────┼───────────────────────────────────────┘
                                     │
                                     ▼
                          Structured ShotPlan
                            (shot_plan.json)
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 3. DETERMINISTIC SHOT COMPILER (tools/shot_library/compiler.py)            │
│    • TimelineResolver: Resolves causal DAG (after, when_closer_than)       │
│    • ReferenceResolver: Resolves semantic targets at beat start time t     │
│    • MotionSynthesizer: Obstacle-avoiding splines, vehicle physics, IK     │
│    • CameraController: 3D bounding-box perspective frustum solver          │
│    • Baker & Binder: Quaternion slerp + upper-body overlays + gaze IK      │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ 4. VALIDATION & CLOSED-LOOP SELF-CORRECTION (plan_and_compile_with_retry)  │
│    • ShotLinter (heading alignment, gait stride speed limits)              │
│    • 3D Perspective Camera Frustum Validator (NDC bounds check)            │
│    • Prompt Adherence Validator (required assets, counts, emotional beats) │
│    • 48 Hz Skeletal Capsule Collision Validator (validate_collisions.py)   │
│                                                                            │
│    ├── If ALL PASS ──► Writes final shot.usda + anim_*.usda + sidecars     │
│    └── If ANY FAIL ──► Sends structured JSON error back to Step 2 (Retry)  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Step 1: What Context the Planner Receives

Before planning a shot, [`LLMShotPlanner`](../tools/shot_library/llm_planner.py) gathers three live data sources:

### A. Auto-Generated Tool Schemas (`export_tool_schemas()`)
[`LLMShotPlanner.export_tool_schemas()`](../tools/shot_library/llm_planner.py#L144-L189) uses Python's `inspect.signature` and docstrings to automatically introspect every public method on [`FunctionCatalog`](../tools/shot_library/function_catalog.py) (**76 fundamental building-block tools**) into an OpenAI-compatible `tools` JSON schema:

| Category | Callable Tools in `FunctionCatalog` (76 Total) |
| --- | --- |
| **Scene Setup** | `place`, `place_group`, `set_appearance`, `remove` |
| **Locomotion & Expressive Gaits** | `walk_to`, `run_to`, `sneak_to`, `jog_to`, `crawl_to`, `stumble_to`, `back_up_to`, `carry_to`, `pace_while_talking`, `swim_to`, `fly_to`, `climb_stairs`, `approach`, `follow`, `flee_from`, `stand_at`, `turn_to_face`, `wait` |
| **Posture, Vertical & Acrobatic Traversal** | `kneel`, `lie_down`, `stand_up`, `sit_on`, `climb_surface`, `vault_over`, `perform_acrobatics`, `jump_on`, `jump_over`, `jump_down` |
| **World, Device & Prop Interaction** | `interact_object`, `throw_toward`, `pull_object`, `inspect_area`, `use_phone`, `open_part`, `close_part`, `pick_up`, `put_down` |
| **Vehicles & Vehicle Occupancy** | `drive_to`, `drive_toward`, `drive_past`, `drive_along`, `brake_to_stop`, `reverse_to`, `open_door`, `close_door`, `set_lights`, `honk_horn`, `board_vehicle`, `disembark_vehicle`, `exit_vehicle`, `sit_in`, `drive` |
| **Social, Emotional & Performance** | `converse`, `argue`, `gesture`, `greet`, `shake_hands`, `hug_or_kiss`, `celebrate`, `emote`, `dance`, `perform_activity`, `huddle`, `disperse` |
| **Combat, Defense & Weapons** | `attack`, `defend_or_block`, `use_weapon`, `push`, `shove`, `hit_reaction`, `take_cover` |
| **Reactions, Collapse & Medical** | `react`, `dodge`, `fall`, `collapse_or_knockout`, `medical_aid`, `look_at`, `flinch` |
| **Camera Framing & Motion** | `camera_frame`, `camera_move`, `camera_look_at`, `camera_cut` |

### B. Live Scene Graph (`scene_data`) & Asset Manifest (`manifest.json`)
[`build_system_prompt()`](../tools/shot_library/llm_planner.py#L191-L221) inspects the current `scene.usda` and [`manifest.json`](../manifest.json) so the planner knows:
- Which objects already exist on the stage (e.g., `OakTree`, `StreetAvenue`, `CitySquareStairs`).
- Which road/pedestrian zones exist (`lane_east`, `lane_west`, `sidewalk_east`, `sidewalk_west`, `plaza`).
- Which semantic anchors, surfaces, doors, and stair portals each asset provides.

### C. The Semantic Reference Grammar
Instead of coordinates, the planner addresses space using semantic expressions resolved by [`ReferenceResolver`](../tools/shot_library/semantic_references.py):
- **Zone Markers**: `lane_east.north`, `lane_east.south`, `lane_east.center`, `lane_east.stop_mark`, `lane_west.north`, `sidewalk_west.center`, `plaza.center`
- **Vehicle Anchors & Surfaces**: `car_1:door_fl_stand`, `car_1:door_fr_stand`, `car_1:hood_front`, `car_1:trunk_rear`, `car_1:roof`, `car_1:hood`
- **Stair Portals**: `stairs_main`, `stairs_main_top`, `stairs_main_bottom`
- **Relative Spatial Selectors**: `in_front_of(actor, gap)`, `behind(actor, gap)`, `beside(actor, left|right, gap)`, `left_of(actor, gap)`, `right_of(actor, gap)`, `nearest_to(actor, type)`
- **Cardinal Directions**: `north`, `south`, `east`, `west`

---

## 4. Step 2: How the Two Planning Modes Work

### Mode A: Live LLM Tool-Calling (`_plan_with_llm_api`)
When `OPENAI_API_KEY` or `PAINTERSPACE_LLM_URL` is set in the environment:
1. [`_plan_with_llm_api()`](../tools/shot_library/llm_planner.py#L283-L347) sends the system prompt, the user's storyboard prompt, and the `tools` schema to the LLM API (`gpt-4o-mini` by default, configurable via `PAINTERSPACE_LLM_MODEL`).
2. The LLM responds with a sequence of structured `tool_calls` (or a JSON `{"calls": [...]}` array).
3. [`_invoke_catalog_call()`](../tools/shot_library/llm_planner.py#L349-L364) validates each tool call, enforces the Foundational Law (replacing any raw `pos(...)` string if an LLM ever attempts one), and executes the method directly on `self.catalog`.

### Mode B: Offline Compositional Semantic Planner (`_plan_general_semantic`)
When running offline or without an API key, [`_plan_general_semantic()`](../tools/shot_library/llm_planner.py#L369-L390) decomposes any prompt compositionally in four stages:
1. **Environment Context Discovery ([`_ensure_environment_context`](../tools/shot_library/llm_planner.py#L398-L435))**:
   - Uses [`ManifestAssetMatcher`](../tools/shot_library/llm_planner.py#L34-L127) to check if the prompt mentions trees, stairs, crates, barriers, benches, etc. If an object is already in `scene.usda`, it reuses its prim name (e.g., `OakTree`); if missing, it places the asset at a semantic zone (`sidewalk_west.center`, `plaza.center`).
2. **Actor & Vehicle Extraction ([`_extract_and_place_actors`](../tools/shot_library/llm_planner.py#L437-L547))**:
   - Counts vehicles (`"a car"`, `"two cars"`) and humans (explicit numbers like `"three people"`, collectives like `"couple"`, `"crowd"`, `"group"`, or distinct roles like `"driver"`, `"passenger"`, `"runner"`).
   - Places each actor semantically based on their role in the prompt:
     - Seated inside a vehicle (`in_vehicle="car_1", seat="driver_seat"`) if they disembark or drive.
     - At a stair portal (`stairs_main_top` or `stairs_main_bottom`) if traversing stairs.
     - Relative to a scene object (`beside(OakTree, right, conversational)`) or counterpart (`in_front_of(human_1, conversational)`).
3. **Multi-Clause Temporal Segmentation ([`_split_into_clauses`](../tools/shot_library/llm_planner.py#L549-L557) & [`_synthesize_clauses`](../tools/shot_library/llm_planner.py#L559-L725))**:
   - Splits the prompt on temporal/causal conjunctions (`;`, `then`, `and then`, `after that`, `next`, `suddenly`, `when`, `as`, `while`, `, but`).
   - Maps each clause's verb to the matching [`FunctionCatalog`](../tools/shot_library/function_catalog.py) method and chains them using causal events (`after="car_1.stopped"`, `when_closer_than={"actor_a": "human_1", "actor_b": "car_1", "gap": "far"}`).
4. **Compositional Camera Synthesis ([`_synthesize_camera`](../tools/shot_library/llm_planner.py#L748-L795))**:
   - Extracts camera framing `style` (`wide`, `medium`, `close`, `top_down`, `over_the_shoulder`, `three_quarter`), `vantage` (`eye_level` vs. `aerial`/`drone`), and `move` (`push_in`, `pull_out`, `orbit`, `crane_up`, `track_with`).

---

## 5. How Semantic Words Become Exact Numbers (The Numerical Resolution Pipeline)

When the LLM returns tool calls, those calls contain **only semantic words** (like `at_reference="lane_east.north"`, `target="car_1:door_fl_stand"`, `after="car_1.stopped"`). Yet OpenUSD (`shot.usda`) requires **exact numbers** at every single frame (`0, 1, 2, ..., 240`):
- Exact 3D positions `(x, y, z)` in meters
- Exact rotation angles `(rotX, rotY, rotZ)` in degrees
- Exact frame numbers and animation playback speeds

Here is the exact 5-stage numerical pipeline inside [`ShotCompiler.compile()`](../tools/shot_library/compiler.py#L53-L475) that turns those words into numbers and routes them into the right synthesizer function:

### Stage 1: Initial Placement Words $\rightarrow$ Starting `(x, y, z, heading_deg)` ([`ReferenceResolver.resolve`](../tools/shot_library/semantic_references.py#L124-L475))
In [`compiler.py` (lines 114–173)](../tools/shot_library/compiler.py#L114-L173), for each `place(...)` action in `plan.setup`, [`ReferenceResolver.resolve()`](../tools/shot_library/semantic_references.py#L124-L475) looks up the semantic string in [`manifest.json`](../manifest.json) and `scene.usda` and computes a concrete `ResolvedTarget` (`position: [x, y, z]`, `facing_deg: float`):

| Semantic String in Plan | Where the Numbers Come From | Exact Numerical Output `(x, y, z), facing_deg` |
| --- | --- | --- |
| `"lane_east.north"` | Zone `lane_east` center $X = 1.6\text{ m}$ + marker `north` $Z = -9.0\text{ m}$ | `(1.60, 0.00, -9.00), 180.0°` (facing South) |
| `"stairs_main_top"` | `manifest.json` $\rightarrow$ `stairs_main.centerline[-1]` | `(0.00, 1.70, -11.37), 180.0°` |
| `"beside(OakTree, right, conversational)"` | Reads `/World/OakTree` transform `(-3.18, 0.0, 4.74)` from `scene.usda` + adds perpendicular offset $1.30\text{ m}$ (`GAP_BETWEEN_BODIES["conversational"]`) | `(-1.88, 0.00, 4.74), 0.0°` |
| `in_vehicle="car_1", seat="driver_seat"` | Reads `car_1` world pose `(1.60, 0.00, -9.00, 180°)` + rotates local `driver_seat` anchor `[-0.36, 0.35, -0.15]` by $180^\circ$ | `(1.96, 0.35, -8.85), 180.0°` |

### Stage 2: Timing Words $\rightarrow$ Exact Seconds `[t_start, t_end]` & Frame Numbers ([`TimelineResolver.resolve`](../tools/shot_library/timeline_resolver.py#L72-L310))
Next, [`TimelineResolver.resolve()`](../tools/shot_library/timeline_resolver.py#L72-L310) converts timing words (`after="car_1.stopped"`, `when_closer_than=...`, `for_duration="short"`) into exact floating-point seconds `[start_time, end_time]`:
- Named durations are looked up in `DURATION_LEVELS` (`"brief" -> 2.0s`, `"short" -> 4.0s`, `"long" -> 8.0s`) or computed from `distance / speed`.
- Event triggers like `after="car_1.stopped"` look up the exact `end_time` of `car_1`'s `drive_to` beat (e.g., $t = 2.80\text{ s}$) + settle margin ($0.20\text{ s}$) $\rightarrow$ `start_time = 3.00s` (Frame $72$ at $24\text{ fps}$).
- Proximity triggers (`when_closer_than={"actor_a": "human_1", "actor_b": "car_1", "gap": "far"}`) step forward at $1/24\text{ s}$ intervals along the provisional trajectories of `human_1` and `car_1` and trigger at the exact frame where $\lVert \mathbf{p}_a(t) - \mathbf{p}_b(t) \rVert \le 4.0\text{ m}$.

### Stage 3: Dynamic Beat-Start Target Resolution at $t = t_{\text{start}}$ ([`MotionSynthesizer._resolve_target_at_time`](../tools/shot_library/motion_synthesis.py#L303-L355))
Because actors and vehicles **move during the shot**, a target like `"car_1:door_fl_stand"` is at a *different* 3D coordinate at $t = 3.0\text{ s}$ than it was at $t = 0.0\text{ s}$:
1. [`compiler.py` (lines 247–269)](../tools/shot_library/compiler.py#L247-L269) synthesizes vehicles first, then characters.
2. When a character beat starts at $t = t_{\text{start}}$, [`_build_live_actor_states(t_start)`](../tools/shot_library/motion_synthesis.py#L274-L301) samples the exact `(x, y, z)` and `heading_deg` of every vehicle and actor at $t = t_{\text{start}}$.
3. [`_resolve_target_at_time()`](../tools/shot_library/motion_synthesis.py#L303-L355) re-runs [`ReferenceResolver.resolve()`](../tools/shot_library/semantic_references.py#L124-L475) using those live coordinates:
   $$\mathbf{p}_{\text{world}}(t_{\text{start}}) = \mathbf{p}_{\text{car}}(t_{\text{start}}) + R_y(\theta_{\text{car}}(t_{\text{start}})) \cdot \mathbf{p}_{\text{local\_anchor}}$$
   This guarantees that walking to `"car_1:door_fl_stand"` after `"car_1.stopped"` routes the character to the exact spot where the car came to a stop.

### Stage 4: Routing Numerical Data into the Matching Physics/Kinematics Function ([`MotionSynthesizer.synthesize_actor`](../tools/shot_library/motion_synthesis.py#L508-L1665))
Inside [`MotionSynthesizer.synthesize_actor()`](../tools/shot_library/motion_synthesis.py#L508-L1665), a dispatcher switches on `beat.function` (`fn`) and feeds `curr_pos`, `curr_heading`, `target.position`, `t_start`, and `t_end` into the specific mathematical solver for that action:

- **Vehicle Driving (`drive_to`, `drive_toward`, `brake_to_stop`, `reverse_to`)**:
  - Computes travel distance $D = \sqrt{\Delta x^2 + \Delta z^2}$ and heading $\theta = \text{atan2}(\Delta x, -\Delta z)$.
  - Evaluates smoothstep progress $s(u) = 3u^2 - 2u^3$ for $u \in [0, 1]$ at $24\text{ fps}$.
  - Computes wheel spin $\Delta \phi_{\text{wheel}} = (\Delta d / r_{\text{wheel}}) \cdot \frac{180}{\pi}$, Ackermann steering angle, and braking nose-dive pitch (`pitch_deg = -1.2°`).
- **Character Locomotion (`walk_to`, `run_to`, `sneak_to`, `jog_to`, `approach`, `flee_from`, `follow`)**:
  - Collects obstacle bounding boxes at $[t_{\text{start}}, t_{\text{end}}]$ via [`_collect_obstacles_at_time()`](../tools/shot_library/motion_synthesis.py#L357-L406).
  - Computes bypass waypoints around any obstacle and fits a smooth Catmull-Rom spline via [`_plan_smooth_path()`](../tools/shot_library/motion_synthesis.py#L408-L464).
  - Samples arc-length position `(x, y, z)`, tangent heading $\theta(s)$, and curvature $\kappa(s)$ at every frame (`_sample_polyline()`).
  - Computes stride-matched animation speed $\text{speed} = \text{clamp}(v_{\text{actual}} / v_{\text{clip}}, 0.65, 1.85)$ so feet never slide on the ground.
- **Staircase Traversal (`climb_stairs`)**:
  - Reads the stair `centerline` polyline and `treads` (`tread_m`, `rise_m`) from [`manifest.json`](../manifest.json).
  - Computes continuous horizontal progression $(x(u), z(u))$ and smooth ease-in/linear-slope/ease-out vertical elevation $y(u)$ from bottom landing to top landing, plus step-frequency cadence matching.
- **Jumping & Vaulting (`jump_on`, `jump_over`, `jump_down`)**:
  - Reads target surface height $h_{\text{surface}}$ from `target.height_m` (e.g., `car_1:roof` $\rightarrow 1.47\text{ m}$).
  - Solves a ballistic parabola $y(u) = (1-u)y_0 + u y_1 + 4 h_{\text{arc}} u(1-u)$ over the airborne phase $u \in [0.22, 0.78]$.
- **Evasive Dodge (`dodge`)**:
  - Samples the approaching threat's velocity vector $\mathbf{v}_{\text{threat}}$ at $t_{\text{start}}$, computes the perpendicular escape normal $\mathbf{n}_{\perp} = (-v_z, v_x) / \lVert \mathbf{v} \rVert$, and translates the actor $1.8\text{ m}$ laterally clear of the vehicle corridor.

### Stage 5: Writing Frame-by-Frame Numbers to OpenUSD ([`compiler.py`](../tools/shot_library/compiler.py#L477-L770) & [`bake_animation.py`](../tools/bake_animation.py))
Finally, [`ShotCompiler`](../tools/shot_library/compiler.py) converts the synthesized `Keyframe` arrays into OpenUSD timeSamples:
1. **`shot.usda` Root Transforms**: Writes `xformOp:translate.timeSamples` (`frame: (x, y, z)`), `xformOp:rotateY.timeSamples` (`frame: -heading_deg`), `xformOp:rotateX.timeSamples`, and nested door hinge `over "Doors" { over "Door_FL" { xformOp:rotateY.timeSamples } }`.
2. **`ShotCam` Perspective Frustum Transform ([`CameraController.synthesize_camera`](../tools/shot_library/camera_controller.py#L74-L213))**: Computes the 3D bounding box centroid $\mathbf{c}$ and radius $R$ of all tracked actors over $[0, T]$, solves standoff distance $D = (R + \text{margin}) / \tan(\text{FOV} / 2)$, and computes camera `(x, y, z)` and `rotateXYZ = (pitch, yaw, 0)` at every frame.
3. **`anim_<actor>.usda` Skeletal Joint Rotations ([`bake_animation.py`](../tools/bake_animation.py))**: Evaluates quaternion `slerp` across all 65 `UsdSkel` joints at $24\text{ fps}$, blends upper-body gesture overlays, adds procedural `Spine2`/`Neck`/`Head` gaze IK rotations toward `look_at_targets`, and binds the animation to `/World/<Actor>` in `shot.usda`.

---

## 6. Step 3: Closed-Loop Validation & Retry (`plan_and_compile_with_retry`)

Once the planner produces a [`ShotPlan`](../tools/shot_library/plan_model.py#L84-L141) (`shot_plan.json`), [`plan_and_compile_with_retry()`](../tools/shot_library/llm_planner.py#L252-L279) passes it to [`ShotCompiler.compile()`](../tools/shot_library/compiler.py#L53-L475), which synthesizes the 3D trajectories and runs four automated verification gates:

1. **Physical Feasibility & Gait Stride Linter ([`ShotLinter`](../tools/shot_library/shot_linter.py))**:
   - Checks braking distances (`INFEASIBLE_BRAKING_DISTANCE`), evasive dodge speeds (`INFEASIBLE_DODGE`), velocity-to-heading alignment, and gait stride speeds.
2. **3D Perspective Camera Frustum Validator ([`CameraController.validate_frustum`](../tools/shot_library/camera_controller.py#L230-L296))**:
   - Projects tracked actors into the camera's 3D perspective Normalized Device Coordinates (`ndc_x`, `ndc_y`, `d_front`) across the shot to verify actors stay in front of the lens and inside the frame.
3. **Prompt Adherence Verifier ([`ShotCompiler._verify_prompt_adherence`](../tools/shot_library/compiler.py#L895-L995))**:
   - Verifies the composed OpenUSD stage contains all required assets, character counts, drone camera elevations, and emotional transitions requested by the prompt.
4. **48 Hz Skeletal Capsule Collision Validator ([`validate_collisions.py`](../tools/collision/validate_collisions.py))**:
   - Evaluates 24 posed `UsdSkel` bone capsules per character against vehicle OBBs, articulated doors, trees/props, and other characters at half-frame resolution.

### What Happens on Failure?
- If **all checks pass**, compilation succeeds on Attempt 1 and writes `shot.usda`, `anim_*.usda`, `shot_plan.json`, `resolved_timeline.json`, and `validation_report.json`.
- If **any check fails**, [`plan_and_compile_with_retry()`](../tools/shot_library/llm_planner.py#L252-L279) extracts the structured JSON diagnostic (error code, offending beat/actor, penetration depth, or unresolved reference suggestions), injects it into [`build_system_prompt(error_feedback=...)`](../tools/shot_library/llm_planner.py#L191-L221), and re-runs the planner to self-correct the plan.

---

## 6. Concrete Walkthrough Example

**User Prompt:**
> *"A car drives up and stops; the driver and passenger get out and argue heatedly."*

### 1. Planner Output (`FunctionCatalog` Calls)
```python
catalog.place("cars.car1", at_reference="lane_east.north", label="Car1")                  # -> car_1
catalog.place("character.human", in_vehicle="car_1", seat="driver_seat", label="Actor1")  # -> human_1
catalog.place("character.human", in_vehicle="car_1", seat="passenger_seat", label="Actor2") # -> human_2

catalog.drive_to("car_1", target="lane_east.stop_mark", at_time=0.0, for_duration=2.8)
catalog.disembark_vehicle(vehicle="car_1", occupants=["human_1", "human_2"], after="car_1.stopped", for_duration=2.4)
catalog.argue(actors=["human_1", "human_2"], intensity="heated", at_time=5.4, for_duration=3.2)
catalog.camera_frame(actors=["human_1", "human_2"], style="medium", vantage="eye_level")
```

### 2. What the Deterministic Compiler Computes Automatically
- **Frames 0–67 ($0.0\text{ s}$–$2.8\text{ s}$)**: `car_1` drives South from `lane_east.north` to `lane_east.stop_mark` with wheel spin, braking deceleration, and nose-dive pitch. `human_1` and `human_2` are automatically coupled to `driver_seat` and `passenger_seat` inside the moving cabin.
- **Frames 72–130 ($3.0\text{ s}$–$5.4\text{ s}$)**: Triggered by `car_1.stopped`, `Door_FL` swings open $-65^\circ$ and `Door_FR` swings open $+65^\circ$ under a unified `over "Doors"` block while `human_1` and `human_2` step out to `car_1:door_fl_stand` and `car_1:door_fr_stand` at `car_1`'s stopped position.
- **Frames 130–226 ($5.4\text{ s}$–$9.4\text{ s}$)**: `human_1` and `human_2` turn to face each other, engage procedural head/neck Gaze IK (`look_at_targets`), and alternate non-combat argument gestures (`pointing_forward`, `reaction`, `acknowledging`).
- **Camera (`/World/ShotCam`)**: Solves the 3D bounding box of `human_1` and `human_2` and frames the action with verified perspective frustum visibility.

