# PainterSpace

PainterSpace is a Qt / OpenUSD + Hydra Storm 3D storyboard studio where the composed USD stage (`user_edits.usda` over `shot.usda` over `scene.usda`) is the single source of truth for interactive editing, LLM shot generation, and rendering.

> [!IMPORTANT]
> ## THE FOUNDATIONAL ARCHITECTURAL LAW OF PAINTERSPACE
> ### General Scene-Graph-Driven Shot Planning — Never Hardcoded Scenario Scripts
>
> **This is the non-negotiable, foundational law of the PainterSpace codebase:**
>
> 1. **`LLMShotPlanner` ([`tools/shot_library/llm_planner.py`](tools/shot_library/llm_planner.py)) MUST ALWAYS be a general, compositional, scene-graph-driven planner — NEVER a hardcoded keyword script.**
>    - **STRICTLY FORBIDDEN**: Writing `if "some_scenario" in prompt:` template branches that emit pre-baked sequences or hardcoded 3D coordinate literals like `pos(0.0, 1.70, -11.37, 180.0)` or `cam_start = [-3.18, 6.2, 16.5]`.
>    - **WHY**: Hardcoded scenario scripts only work for the exact ~10 demo prompts they were written for and collapse on every new creative prompt a user types in the studio. Our goal is a **genuinely general animation engine** capable of rendering realistic, physically grounded animations for **any** prompt in **any** scene.
> 2. **Zero Raw Coordinates in the Planner (`pos(...)` Ban)**:
>    - The planner must **never** guess or hardcode raw `(x, y, z)` world coordinates, Euler rotation angles, or frame numbers.
>    - Every actor placement, target destination, surface interaction, stair traversal, and camera framing must be expressed strictly through **semantic references** resolved dynamically from the live `scene.usda` stage (`scene_data`) and [`manifest.json`](manifest.json):
>      - **Zone & Roadway Markers**: `lane_east.north`, `lane_east.south`, `lane_east.center`, `lane_east.stop_mark`, `lane_west.north`, `sidewalk_west.center`, `plaza.center`
>      - **Asset Anchors, Surfaces & Parts**: `car_1:door_fl_stand`, `car_1:hood_front`, `car_1:roof`, `car_1:hood`, `car_1:door_fl`, `in_vehicle="car_1"`
>      - **Dynamic Stair Portals**: `stairs_main`, `stairs_main_top`, `stairs_main_bottom` (discovered from `manifest.json` stair centerlines and tread geometry)
>      - **Relative Spatial Selectors**: `in_front_of(actor, gap)`, `behind(actor, gap)`, `beside(actor, left|right, gap)`, `left_of(actor, gap)`, `right_of(actor, gap)`, `nearest_to(actor, filter)`
>      - **Causal & Proximity Event Triggers**: `after="car_1.stopped"`, `after="human_1.arrived"`, `when_closer_than={"actor_a": "human_1", "actor_b": "car_1", "gap": "far"}`
>      - **Frustum-Solved Camera Vantages**: `camera_frame(actors=[...], style="wide"|"medium"|"close"|"top_down", vantage="eye_level"|"aerial", move="push_in"|"orbit"|"track_with")`
> 3. **Separation of Intent (LLM Planner) vs. Physics & Math (Deterministic Compiler)**:
>    - **The Planner (`llm_planner.py`)** decomposes any natural-language storyboard prompt into entities, semantic placements, and sequential/simultaneous `FunctionCatalog` calls (**76 fundamental building-block verbs** covering locomotion, expressive gaits, vertical/acrobatic traversal, world/device interactions, vehicle occupancy, social/emotional/performance choreography, combat/weapons/medical aid, and camera framing) — either via live LLM tool-calling (`_plan_with_llm_api` using auto-introspected `export_tool_schemas()`) or compositional clause analysis (`_plan_general_semantic`).
>    - **The Compiler (`compiler.py`, `motion_synthesis.py`, `camera_controller.py`, `bake_animation.py`, `validate_collisions.py`)** deterministically computes 4D space-time trajectories, obstacle-avoiding Catmull-Rom splines, stride-matched animation playback speeds, upper-body gesture layering, procedural gaze IK, 3D perspective camera frustum solving, and 48 Hz skeletal capsule collision verification/repair.
> 4. **Automated Enforcement**:
>    - `test_general_llm_planner_zero_hardcoded_coordinates` in [`tests/test_regression_issues.py`](tests/test_regression_issues.py) statically scans `tools/shot_library/llm_planner.py` and fails the build if a single hardcoded `pos(...)` coordinate literal is ever introduced.
>    - **Full Architecture Guide**: See [`docs/LLM_SHOT_PLANNER.md`](docs/LLM_SHOT_PLANNER.md) for a complete step-by-step walkthrough of how the LLM Shot Planner works.

## Current capabilities

- **Live OpenUSD + Hydra Storm Viewport (`UsdViewportWidget`)**:
  - Directly opens and renders composed USD stages (`shot.usda` sublayering `scene.usda`) using native C++ `UsdImagingGLEngine` (`HdStormRendererPlugin` / Metal) inside Qt.
  - Camera selector switching between `/World/ShotCam` and the Free Orbit Camera, with orbit, pan, zoom, and a ground grid overlay.
  - Interactive timeline scrubber and play/pause controls for animated stages (including rigged `UsdSkel` characters).
  - Live reload (`QFileSystemWatcher` + post-LLM generation reload) that updates the viewport in-place without restarting the app while keeping your camera and frame position intact.
  - Fast hybrid raycast picking (sub-millisecond CPU BBox/Ray intersection with GPU context fallback), bounding-box selection highlight, and Move (`W`), Rotate (`E`), and Scale (`R`) gizmo overlays.
  - **Non-destructive `user_edits.usda` override layer**: all manual viewport edits (transforms, visibility toggles, reference asset additions, deletions, duplicates, and ground paint strokes) are written exclusively to `user_edits.usda`, never modifying the LLM's `shot.usda`.
  - Visible **`Your changes`** layer toggle badge with per-prim reset, full layer reset, `Cmd+Z` / `Cmd+Shift+Z` undo/redo, and orphan warnings when a regenerated shot removes a manually edited prim.
  - Ground painting (`UsdGeomBasisCurves` strokes under `/World/Paint` in `user_edits.usda`) with adjustable brush color, size, opacity, and eraser tool.
- **Two-Step Scene & Shot Storyboard Workflow & Scene Versioning (`tools/scene_manager.py`)**:
  - **Step 1 (Describe & Edit Main Scene)**: Configure the shared scene (`scenes/scene_XX/scene.usda`) via an LLM prompt, the 3D editor, or both. Edits to the main scene can be saved independently (`Edit Main Scene` / `Save Main Scene`), automatically versioning snapshots (`scene_v1.usda`, `scene_v2.usda`).
  - **Non-Destructive Shot Inheritance**: New shots always inherit the latest version/state of the main scene, while older, already-compiled shots preserve their historical scene baseline without being mutated or broken.
  - **Step 2 (Individual Shots)**: Build and refine shots in a 3×3 storyboard book grid (`scenes/scene_XX/shot_XXX/shot.usda` + `user_edits.usda`), with Hydra-rendered thumbnails.
- **Minimalist 3D Editor UI (`src/MainWindow.cpp`, `src/UsdViewportWidget.cpp`)**:
  - Streamlined top toolbar featuring direct navigation (`Back to Shots Grid`), clean scene controls (`Edit Main Scene` / `Save Main Scene`, `Save Shot`), and a compact prompt input and shot generation button.
  - Distraction-free 3D viewport canvas with technical debug overlays removed.
- **Solid-Object Collision Prevention, Detection & Repair (`tools/collision/`)**:
  - Non-destructive `.norm.usda` asset normalization (meters, `Y`-up, `-Z` forward, base-center at `y = 0`), height-filtered compound collision boxes ($0.15\text{ m}$–$2.10\text{ m}$ so tree canopies are ignored while trunks and low branches block), live-articulated door colliders, and pre-cleared standing `anchors`.
  - Three-layer runtime guarantee: **Prevent** (8-connected A* + string-pulling path planner [`plan_path.py`](tools/collision/plan_path.py)), **Detect** (48 Hz posed `UsdSkel` bone-capsule vs. OBB/actor signed-distance validator [`validate_collisions.py`](tools/collision/validate_collisions.py)), and **Repair** (up to 3 structured LLM retries, automatic path re-planning `--repair`, and a hard render gate).
- **Deterministic Shot Function Library (`tools/shot_library/`)**:
  - **Plan, Logic, Execution**: The LLM plans shots strictly by calling a closed vocabulary of named semantic functions (`walk_to`, `run_to`, `dodge`, `drive_to`, `argue`, `attack`, `jump_on`, `camera_frame`, etc.) without ever authoring raw OpenUSD text, coordinates, headings, frame numbers, or camera angles.
  - **Actor Registry & Identity (Phase 0)**: Permanent unique IDs (`human_1`, `car_1`), human-editable labels, auto-assigned appearance variants for identical assets (clothing tints, height scale, descriptive tags), prim `customData` serialization, floating name tags in the 3D viewport, and actor tree synchronization.
  - **Core Compiler & Timeline Resolver (Phase 1)**: Dependency graph resolution, cycle detection, physics/clip duration calculation, settle holds, and automatic idle fills between beats.
  - **Vehicle Kinematics (Phase 2)**: Speed profiles (acceleration, cruise, braking), path tangent headings, wheel spin ($\Delta s / r_{wheel}$), steering angle ($\arctan(L \cdot \kappa)$), braking pitch, and `stop_short`, `near_miss`, and `collide` modes.
  - **Proximity Events & Evasive Reactions (Phase 3)**: Swept threat corridor calculation, feasibility check ($v_{req} \le v_{max}$), and evasive dodge trajectories with clear structured error feedback if infeasible.
  - **Social Scenes & Formations (Phase 4)**: Circular/arc formations with personal space spacing, turn-taking schedules, and seeded gesture pools.
  - **Jumping & Contact (Phase 5)**: Top-surface parabolic jump arcs, strike standoff calculations, and dynamic allowed-contacts rule generation for collision validation.
  - **Frustum-Validated Camera (Phase 6)**: Standard cinematic framing styles (`wide`, `medium`, `close`, `over_the_shoulder`, `top_down`) and motion paths (`push_in`, `pull_out`, `track_with`, `crane_up`) with actor visibility checks.
  - **Replay Determinism & Task Queue Integration (Phase 7)**: 100% byte-identical USDA compilation from `shot_plan.json` and CLI integration via `compile_shot.py --process-queue`.
  - **Scalable Vehicle Seating & Articulated Part Egress (Phase 8)**: Multi-vehicle driver/passenger seating, asset-driven seat anchor resolution (`driver_seat`, `passenger_seat`, `seat_rear_left`, `seat_rear_right`), automatic motion coupling (occupants follow host vehicle position, heading, and pitch at every frame without manual parallel beats), and contact exemption generation.
  - **4-Occupant Vehicle Boarding & Articulated 4-Door Egress (Phase 9)**: Scalable batch boarding (`board_vehicle`), dynamic seat allocation, and intelligent seat-to-door disembarking (`disembark_vehicle`) with animated OpenUSD door rotations (`Door_FL`, `Door_FR`, `Door_RL`, `Door_RR`) and roadway stand anchor targeting.

## Reference assets & automatic `manifest.json` sync

The editor’s **Reference Assets** sidebar dynamically scans `assets/usdc_assets/<category>/` and currently includes:

- **Humans (`assets/usdc_assets/humans/`)**: Rigged human USD character (`human.usdc`) and 45 Mixamo animation clips (`walk.usdc`, `run.usdc`, `jump.usdc`, `sitting.usdc`, etc.)
- **Cars (`assets/usdc_assets/cars/`)**: Articulated vehicles (`car1.usdc`, `car2.usdc`) with movable doors, hood, trunk, steering wheel, and paint/state USD VariantSets
- **Trees (`assets/usdc_assets/trees/`)**: Birch, oak, and pine trees (`birch_tree.usdc`, `birch_tree_lush.usdc`, `oak_tree.usdc`, `oak_tree_lush.usdc`, `pine_tree.usdc`, `pine_tree_lush.usdc`)
- **Environments (`assets/usdc_assets/environments/`)**: Downtown art deco tower, residential neighborhood/intersection/avenue, ocean horizon, and grand harbour (`downtown_art_deco_tower.usdc`, `residential_neighborhood.usdc`, `residential_intersection.usdc`, `residential_avenue.usdc`, `ocean_horizon.usdc`, `grand_harbour.usdc`)
- **Procedural props**: Tree, mountain ridge, rock group, light source, and 3D text block

### Automatic `manifest.json` synchronization (`tools/sync_manifest.py`)

Whenever you add, replace, or remove `.usdc` / `.usda` files under `assets/usdc_assets/<category>/`, [`tools/sync_manifest.py`](tools/sync_manifest.py) automatically inspects the OpenUSD stages and updates [`manifest.json`](manifest.json) in-place:

- **Separates animation clips from 3D models**: Skinless Mixamo animation clips (containing `UsdSkelAnimation` and `__MixamoAnimationProxyMesh`) are registered in the top-level `"clips"` block, while placeable 3D models are added to `"assets"`.
- **Extracts USD stage properties automatically**: Computes `id` (`<category>.<stem>`, preserving existing custom IDs and recording `aliases`), `path`, `default_prim`, `source_format`, `usd_reference_ready`, `units`, `up_axis`, `forward_axis` (inferred from root `customData`, vehicle front/rear wheel & hood/trunk positions, or category conventions), `bounds_m` (`[width, height, depth]` in meters, ignoring embedded Blender `Camera`/`Cube`/`Light`/`env_light` helpers), `pivot`, `tags`, movable `parts` (doors, hood, trunk, steering wheel and their rotation axes), and USD `variants`.
- **Runs automatically**:
  - Live in the app via `QFileSystemWatcher` on `assets/usdc_assets/` and its category subfolders, on app startup, and when clicking **↻ Refresh** in [`MainWindow::populateAssetEntries`](src/MainWindow.cpp).
  - Before building world-state records in [`tools/world_state.py`](tools/world_state.py) and before LLM scene/shot generation in [`tools/generate_usda_scene.py`](tools/generate_usda_scene.py) and [`src/main.cpp`](src/main.cpp).
- **Manual CLI**:

```bash
.venv/bin/python tools/sync_manifest.py
```

## Asset files

The custom facial-expression meshes are standalone editable OBJ/MTL pairs in [`assets/facial_expressions/`](assets/facial_expressions/README.md). They are generated by [`scripts/generate_facial_expressions.py`](scripts/generate_facial_expressions.py).

## Build and run

Requirements:

- CMake 3.16 or newer
- C++17 compiler
- Qt 6 with Widgets, OpenGL, and Network modules
- macOS system OpenUSD runtime (`/usr/lib/usd/libusd_ms.dylib` via the Xcode Command Line Tools SDK `libusd_ms.tbd`) and C++ headers staged in `third_party/openusd/include/pxr`

If setting up on a new machine, stage the OpenUSD C++ headers first (or let CMake run the script automatically):

```bash
./scripts/setup_openusd_sdk.sh
```

The current CMake configuration expects Qt at `/Users/vishaljose/Qt/6.11.1/macos`. If Qt is installed elsewhere, update `CMAKE_PREFIX_PATH` in `CMakeLists.txt`.

```bash
cmake -S . -B build
cmake --build build -j4
./build/PainterSpace
```

To run the automated OpenUSD + Hydra exit test suite (Phases 0–3):

```bash
./build/PainterSpace --usd-test-suite
```

To run the automated Shot Function Library test suite (Phases 0–9):

```bash
.venv/bin/python tools/shot_library/test_all_phases.py
```

To run the automated Scene Manager test suite:

```bash
.venv/bin/python -m unittest tests/test_scene_manager.py
```

To compile a shot plan or process the queued task using the Shot Function Library:

```bash
.venv/bin/python tools/shot_library/compile_shot.py --process-queue
```

## Prompt-to-USD storyboard workflow

Each shot's logic lives in a per-shot `shot.usda` file (`scenes/scene_XX/shot_XXX/shot.usda`), while manual viewport adjustments live in `user_edits.usda` beside it. A small `shot.json` file holds metadata.

```text
project/
├── project.json
├── assets/                      # quality models: running_fast.usdc, walking.usdc, ...
├── manifest.json                # describes the assets for the LLM
└── scenes/
    └── scene_01/
        ├── scene.usda           # the world: environment, static props, base lighting
        ├── user_edits.usda      # optional manual scene-level overrides from the USD viewport
        ├── shot_001/
        │   ├── shot.usda        # THE SHOT LOGIC (LLM-written, never touched by manual edits)
        │   ├── user_edits.usda  # MANUAL VIEWPORT EDITS (layered over shot.usda)
        │   ├── shot.json        # metadata: prompt, revisions, fps, duration, camera prim, version
        │   ├── thumbnail.jpg
        │   ├── video.mp4
        │   └── frames/          # generated frame.###.png sequence
        └── shot_002/ ...
```

- `scene.usda` holds what is constant across the scene's shots: environment, buildings, static props, and base lighting (`defaultPrim = "World"`).
- `shot.usda` sublayers `@../scene.usda@` and uses `over "World"` to add or override actors, movement, camera (`ShotCam`), and timing for that moment.
- `user_edits.usda` is opened as the top session/override layer in `UsdViewportWidget` and `tools/world_state.py`. Manual moves, rotations, scales, visibility changes, added reference assets, and `/World/Paint` strokes are written here so `shot.usda` remains clean and manual edits survive LLM regeneration.
- `shot.json` stores only metadata (original prompt, revisions, frame rate, frame range, duration, camera prim path, LLM model/prompt version, validation status, and `scene_order`). If `shot.json` and `shot.usda` disagree, `shot.usda` wins for what renders.

### Generate and edit a storyboard panel

1. Open **Storyboard Project → Storyboard Scenes**, click a scene card to describe or arrange the shared scene (`scene.usda`), and click **Continue to Shots →**.
2. Create or open a shot panel in the 3×3 storyboard book.
3. Describe the cinematic moment in the **Scene / Shot Prompt** box or add assets directly from the **Reference Assets** rail.
4. Click **✦ Generate Shot USD**. PainterSpace runs the USDA generator using `OPENAI_API_KEY`, validates the resulting stage, and **live-reloads** it directly in the Hydra viewport without restarting the app (preserving your camera and `user_edits.usda` overrides).
5. Use the **Select / Move / Rotate / Scale** gizmos (`W`, `E`, `R`) or **Brush / Eraser** tools to refine the shot in `user_edits.usda`, and click **Save / Render Shot →** to update the storyboard panel thumbnail and world state.

### Command-line generation, validation, and rendering

### Multi-action character animation

Characters can use a short action list instead of changing a skeleton's
animation source over time. The deterministic baker turns the list into one
`SkelAnimation`; root-path movement remains authored on the character Xform.
The model supplies only the JSON list and may use only clip names declared in
the manifest's top-level `clips` object.

```json
{"actions": [
  {"clip": "run", "start_s": 0.0, "end_s": 2.0},
  {"clip": "walk", "start_s": 2.0, "end_s": 5.0, "blend_s": 0.25}
]}
```

Bake and bind it to an existing referenced character (the binding command
finds the skeleton automatically):

```bash
.venv/bin/python tools/bake_animation.py \
  --character assets/usdc_assets/humans/human.usdc \
  --manifest manifest.json --actions actions.json \
  --output scenes/scene_01/shot_001/anim_hero.usda --fps 24
.venv/bin/python tools/bind_baked_animation.py \
  --shot scenes/scene_01/shot_001/shot.usda \
  --character-path /World/Hero \
  --animation scenes/scene_01/shot_001/anim_hero.usda
```

`blend_s` crossfades from the prior action using shortest-path quaternion
slerp and smoothstep easing. Gaps hold the last pose; later actions win
overlaps. The baker rejects missing clips, invalid times, and non-positive
speeds, and warns about unmatched joints, gaps, and overlaps.

For the app pipeline, `tools/bake_shot_animations.py` accepts a plan with one
entry per character and persists `actions_<id>.json` beside the baked
`anim_<id>.usda`; those files are the timeline-editable action lists. The app
owns `character_path` and `character_file`; only `actions` should originate
from an LLM.

### Adding a new Mixamo body movement

This is the repeatable asset-ingest workflow for a new movement such as sit,
point, wave, or turn. Keep every download on the same Mixamo character: the
single character FBX is downloaded **With Skin**, while each action FBX is
downloaded **Without Skin** at 24 FPS with keyframe reduction disabled. Use
Mixamo's **In Place** option for walk and run.

Put the FBXs in `assets/usdc_assets/humans/`, then run the Blender exporter.
Choose the one With Skin FBX as `--character`; `--all-clips` imports every
skinless FBX in that directory in a fresh Blender scene and exports a matching
USD clip.

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python tools/export_mixamo_usd.py -- \
  --character "assets/usdc_assets/humans/Idle with skin.fbx" \
  --all-clips --manifest manifest.json --fps 24
```

The command creates `human.usdc`, names clips from their FBX filenames (for
example `Running without skin.fbx` becomes `run.usdc`), updates the manifest's
top-level `clips` object, and records sources/frame ranges in
`assets/usdc_assets/humans/mixamo_export_report.json`. It checks that the
character is approximately 1.7 Blender units tall. The exporter includes a
microscopic skinned proxy in clip files because Blender 5.x otherwise emits an
invalid armature-only `SkelBindingAPI`; the animation baker reads only
`SkelAnimation`, so the proxy has no effect on the baked result.

Validate newly exported files before adding them to a shot:

```bash
usdchecker assets/usdc_assets/humans/human.usdc
usdchecker assets/usdc_assets/humans/sit.usdc
usdcat assets/usdc_assets/humans/sit.usdc | grep -E "SkelRoot|Skeleton|SkelAnimation|joints|timeSamples" | head
```

The character and every clip must have the same joint list. If source clip
translations are centimetres (for example hips values near 100 rather than
near 1), add `"translation_scale": 0.01` to that clip's manifest entry and
rebake; do not scale only the clip or only the character. The provided
`run_walk_test_actions.json` and `anim_run_walk_test.usda` are a known-good
real-asset bake test:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python tools/bake_animation.py -- \
  --character assets/usdc_assets/humans/human.usdc \
  --manifest manifest.json \
  --actions assets/usdc_assets/humans/run_walk_test_actions.json \
  --output assets/usdc_assets/humans/anim_run_walk_test.usda --fps 24
usdchecker assets/usdc_assets/humans/anim_run_walk_test.usda
```

Generate a shot layer and metadata sidecar from the command line:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" ./build/PainterSpace --generate-usda \
  "A human runs toward the camera, stops, then walks forward" \
  scenes/scene_01/shot_001/shot.usda
```

Validate an authored shot layer:

```bash
./build/PainterSpace --validate-usda scenes/scene_01/shot_001/shot.usda
```

Render a single frame directly through the embedded `UsdViewportWidget` Hydra Storm engine:

```bash
./build/PainterSpace --usd-spike \
  scenes/scene_01/shot_001/shot.usda \
  /tmp/shot_001_hydra.png \
  12
```

Render numbered frames through Hydra Storm using `usdrecord` (camera prim path is automatically resolved from `shot.usda`, with fallback to `shot.json`):

```bash
./build/PainterSpace --render-usd \
  scenes/scene_01/shot_001/shot.usda \
  scenes/scene_01/shot_001/frames/frame.###.png \
  0:120 960
```

Encode a numbered PNG sequence as H.264 MP4 with FFmpeg:

```bash
./build/PainterSpace --encode-video \
  scenes/scene_01/shot_001/frames/frame.###.png \
  24 \
  scenes/scene_01/shot_001/video.mp4
```

### World state record

Before every LLM request PainterSpace rebuilds a **world state record**. This is a JSON summary of what is in the scene right now, computed by [`tools/world_state.py`](tools/world_state.py) from the composed USD stage (`user_edits.usda` over `shot.usda` over `scene.usda`). The record goes to the model together with a labelled top-down map and the camera's current view. USD stays the source of truth. The record files are generated caches: never edit them, and delete or regenerate them whenever you like.

Setup (once):

```bash
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt   # usd-core, Pillow
```

PainterSpace uses `.venv/bin/python3` automatically. You can override it with `PAINTERSPACE_PYTHON`. Set `PAINTERSPACE_SEND_IMAGES=0` to send the record without images. If the tool is unavailable, generation still runs, just without the record.

| File | Contents |
| --- | --- |
| `scenes/scene_XX/world_state.json` | Baseline: `scene.usda` (plus scene `user_edits.usda` if present) before any shot |
| `shot_NNN/state_start.json` | Exactly what the LLM received. Equals the previous shot's `state_end.json`, or the baseline for the first shot |
| `shot_NNN/state_end.json` | The shot evaluated at its last frame (including `user_edits.usda`), written after the stage validates |
| `*_topdown.png`, `*_camera.png` | Top-down map (north up, ids labelled) and the camera view (Hydra Storm) |

Conventions: meters, Y up, north = −Z, east = +X. `position` is the centre of an object's base. `heading_deg` is 0 = north, 90 = east, increasing clockwise when seen from above, so `xformOp:rotateY = -heading_deg`. `size` is measured in the object's own axes. `bounds_min`/`bounds_max` are world axis-aligned bounds computed from the real transforms, including skinned character poses.

Derived facts: `relations` (distance, compass direction, facing, gap), `overlaps`, `free_space` per zone, `camera_view` (inside, partial, or outside the frame), `groups` for repeated assets, `defined_in` (`scene`, `shot`, or `user_edits`), `/World/Paint` stroke summaries (`category = "paint"`, `stroke_count`), and `warnings` (below ground, floating, character not upright, wrong stage units).

Scene authoring rules the record relies on:

- Every direct child of `/World` is one object. Group sub-parts under a single Xform.
- Named zones are invisible guide boxes under `/World/Zones` (see `scenes/scene_01/scene.usda`).
- Prims that reference a manifest asset carry `customData = { string asset_id = "<manifest id>" }`. The asset's reference path is used as a fallback.
- `customData` `world_state_role` (`object`, `environment`, `zone`, `ignore`) overrides automatic classification. Movable parts are declared in the manifest as `"parts": {"door_l": {"prim": "Body/DoorL", "axis": "Y"}}`.

Manual use:

```bash
.venv/bin/python tools/world_state.py prepare-shot  scenes/scene_01/shot_002/shot.usda
.venv/bin/python tools/world_state.py finalize-shot scenes/scene_01/shot_002/shot.usda
.venv/bin/python tools/world_state.py build scenes/scene_01/scene.usda --topdown /tmp/map.png
```

### Validation, solid-object collision enforcement, and asset safety

Before rendering, PainterSpace accepts only `.usda` text and checks that the stage exists and is within the 8 MiB file and 10,000-prim limits. Declared duration and frame rate are capped at a combined 72,000 frame-rate/seconds budget. USD references must resolve inside the project `assets/` library (for example `@../../../assets/usdc_assets/humans/human.norm.usda@`), the scene layer (`@../scene.usda@`), or sibling shot sublayers (`anim_*.usda`, `path_*.usda`); absolute paths, URLs, and non-USD references are rejected. `usdcat` is required, `usdchecker` is run when available, and [`tools/collision/validate_collisions.py`](tools/collision/validate_collisions.py) enforces solid-object collision guarantees across every frame.

---

## System Flow of Execution & Autonomous Task Queue

PainterSpace operates on a deterministic, closed-loop pipeline that bridges natural language storyboard prompts, high-level semantic motion planning, and OpenUSD stage authoring:

```
User Prompt (GUI / CLI / Queue)
           │
           ▼
┌────────────────────────────────────────┐
│ 1. LLM Shot Planner (llm_planner.py)   │
│    - Translates prompt into closed-    │
│      vocabulary function calls         │
│      (walk_to, drive, board, etc.)     │
│    - Zero raw USD coordinates or math  │
│    - Emits shot_plan.json              │
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 2. Function Catalog & References       │
│    - Semantic reference resolver       │
│      (anchors, surfaces, gaps)         │
│    - Validates asset IDs & capabilities│
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 3. Timeline & Kinematics Engine        │
│    - Dependency graph resolution       │
│    - MotionSynthesizer (splines,       │
│      speed profiles, pitch, roll)      │
│    - Dynamic vehicle-occupant coupling │
│    - Settle holds & idle beat fills    │
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 4. Shot Compiler (compiler.py)         │
│    - Emits layered shot.usda           │
│    - Dynamic nested part animation     │
│      over blocks (doors, hood, trunk)  │
│    - Actions list for multi-clip bake  │
│    - Generates allow_collisions.json   │
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 5. Character Animation Baker & Binder  │
│    - Multi-clip quaternion slerp bake  │
│    - UsdSkel skeleton binding          │
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 6. Collision Validator (48 Hz)         │
│    - Posed bone capsule vs. OBB        │
│    - Automated repair / push-out       │
│    - Hard render gate                  │
└──────────────────┬─────────────────────┘
                   │
                   ▼
┌────────────────────────────────────────┐
│ 7. Hydra Storm Viewport Live-Reload    │
│    - Metal / OpenGL offscreen blit     │
│    - Sub-frame scrubber update         │
│    - Thumbnail generation              │
└────────────────────────────────────────┘
```

### Automated Queue Execution (`tools/process_agent_queue.py`)
For external agents working asynchronously:
- The UI writes tasks to `task_queue/pending_request.json` containing the prompt, target stage, and scene context.
- The queue processor handles status inspection and archiving:
  ```bash
  .venv/bin/python tools/process_agent_queue.py status
  .venv/bin/python tools/shot_library/compile_shot.py --process-queue
  .venv/bin/python tools/process_agent_queue.py complete <output_stage_path>
  ```
- All pipeline stages (planning $\rightarrow$ synthesis $\rightarrow$ OpenUSD code generation $\rightarrow$ collision validation $\rightarrow$ rendering) execute deterministically and autonomously.

---

## Architectural Corrections & Shot Compiler Upgrades

To make any shot prompt compile into a physically convincing, collision-free OpenUSD shot on the first pass without manual frame inspection, the following architectural upgrades were implemented across [`tools/shot_library/`](tools/shot_library/), [`tools/collision/`](tools/collision/), [`tools/bake_animation.py`](tools/bake_animation.py), [`tools/generate_usda_scene.py`](tools/generate_usda_scene.py), and [`src/main.cpp`](src/main.cpp):

### 1. Dynamic Beat-Start Semantic Reference Resolution (`compiler.py`, `motion_synthesis.py`, `timeline_resolver.py`, `semantic_references.py`)
- **Problem**: Previously, `compiler.py` resolved all semantic targets (`"car_1:door_fl_stand"`, `"beside(human_1, left, 1.4)"`, `"car_1:hood_strike"`) once at $t = 0.0\text{ s}$ against `actor_initial_states`. If a vehicle drove to a stop at $t = 3.0\text{ s}$ and a character walked to `"car_1:door_fl_stand"` at $t = 3.2\text{ s}$, the character walked to where the car *started* at $t = 0.0\text{ s}$ instead of where it stopped.
- **Correction**:
  - `MotionSynthesizer._resolve_target_at_time()` dynamically samples all synthesized actor trajectories (`other_performances`) at each beat's exact `t_start` (`_build_live_actor_states()`) and re-evaluates `ReferenceResolver.resolve()` against the live world state at $t = t_{\text{start}}$.
  - `TimelineResolver._update_provisional_trajectory()` uses `ReferenceResolver` during topological scheduling so `when_closer_than` proximity triggers and `until` event conditions evaluate against true target coordinates.
  - `ReferenceResolver` now supports cardinal directions (`north`, `south`, `east`, `west`, `northeast`, `northwest`, `southeast`, `southwest`), relative actor selectors (`nearest_to(target[, filter])`, `left_of(target[, gap])`, `right_of(target[, gap])`), `beside(target, front|back|left|right, gap)` with preserved `target_asset` provenance, and named `DURATION_LEVELS` (`"brief"`, `"short"`, `"medium"`, `"long"`).

### 2. Obstacle-Avoiding Catmull-Rom Spline Routing & Stride-Matched Human Locomotion (`motion_synthesis.py`, `shot_linter.py`)
- **Problem**: Previously, `MotionSynthesizer` interpolated straight lines between `curr_pos` and `end_pos` (ignoring static/dynamic obstacles in the scene), froze root translation at $u = 0.88$ while rotating in place with the `"walk"`/`"run"` clip still playing (causing characters to march in place), and hardcoded `speed=1.0` on all locomotion clips regardless of actual travel velocity (causing foot sliding).
- **Correction**:
  - **Obstacle-Aware Spline Paths (`_collect_obstacles_at_time` & `_plan_smooth_path`)**: Collects 2D XZ footprints of scene props and vehicles/actors at `[t_start, t_end]`, computes tangent bypass waypoints around any blocking obstacle, and evaluates a smooth centripetal Catmull-Rom spline (`_evaluate_catmull_rom_chain` & `_sample_polyline`).
  - **Stride-Matched Playback Speed**: Computes actual arc-length speed $v_{\text{avg}} = L_{\text{path}} / \Delta t$ and scales `UsdSkelAnimation` playback speed by $\text{clamp}(v_{\text{avg}} / v_{\text{nominal}}, 0.65, 1.85)$ to eliminate foot skating.
  - **Smooth Velocity Profile, Continuous Arrival Turn & Centripetal Banking**: Uses a smooth ease-in/cruise/ease-out velocity curve across the full $[0, 1]$ beat window, blends arrival heading over $u \in [0.82, 1.0]$ while completing the step (never marching in place), and computes centripetal torso `roll_deg` on turns and forward `pitch_deg` lean during sprints.
  - **Gait Stride Linter (`shot_linter.py`)**: Verifies both travel-heading alignment and gait stride speed limits across all keyframes.

### 3. Upper-Body Layered Gestures, Procedural Gaze IK & Idle Breathing (`bake_animation.py`, `compiler.py`)
- **Problem**: Previously, `bake_animation.py` only supported a single full-body clip at a time (`actions`), so a character could not gesture while walking, never turned their head/neck toward a conversation partner or threat, and froze like a mannequin whenever holding an idle pose.
- **Correction**:
  - **Feathered Upper-Body `overlay_actions` (`_upper_body_mask_for_joints` & `blend_masked`)**: Applies a feathered per-joint weight mask (`Spine: 0.35`, `Spine1: 0.65`, `Spine2: 0.90`, `Neck/Head: 0.85`, `Shoulders/Arms/Hands: 1.0`, `Hips/Legs/Feet: 0.0`) so upper-body gestures and reactions blend cleanly over lower-body locomotion without disturbing leg stride.
  - **Procedural Gaze IK (`look_at_targets` in `apply_procedural_life_and_gaze`)**: `compiler.py` computes the relative yaw and pitch from each actor's heading to their active `look_at_targets`, and `bake_animation.py` distributes a smooth, clamped additive gaze rotation across `Spine2` ($15\%$), `Neck` ($35\%$ yaw / $40\%$ pitch), and `Head` ($50\%$ yaw / $60\%$ pitch).
  - **Deterministic Idle Breathing**: Adds a subtle deterministic $0.28\text{ Hz}$ breathing oscillation on `Spine1`, `Spine2`, `LeftShoulder`, and `RightShoulder` during stationary/social clips (`acknowledging`, `idle`, `wait`), keeping actors alive while preserving $100\%$ byte-for-byte replay determinism.

### 4. Complete ~40-Function Catalog Synthesis (`motion_synthesis.py`)
- **Threat-Vector Perpendicular `dodge`**: Samples the approaching threat's live position and velocity vector at `t_start`, computes the true perpendicular escape normal away from the threat centerline, and orients the character + gaze IK toward the threat as they evade.
- **Social Formations & Turn-Taking (`converse`, `argue`, `huddle`, `disperse`, `greet`)**:
  - Computes the live group centroid of all `participants`, orients actors to face their counterpart/centroid, and attaches `look_at_targets`.
  - Subdivides multi-second social beats ($\ge 3.2\text{ s}$) into alternating turn-taking gesture clips with $0.25\text{ s}$ smoothstep crossfades.
  - Removed `"head_hit_during_fight"` from `argue`'s gesture pool so heated arguments use expressive non-combat clips (`"pointing_forward"`, `"reaction"`, `"acknowledging"`).
  - Implemented `huddle` (stepping inward to a $0.85\text{ m}$ circle around the group centroid) and `disperse` (radial walk-away).
- **Multi-Move Combat Combos (`attack`, `push`, `shove`)**: Schedules every clip in `moves` sequentially across `[t_start, t_end]`, auto-faces the target at `t_start`, and scopes `allowed_contacts` to the striking limb joints and time window.
- **Additional Catalog Handlers**: Implemented full motion synthesis for `follow`, `flee_from`, `take_cover`, `stand_at`, `stand_up`, `fall`, `pick_up`, `put_down`, vehicle `reverse_to` (with reversed wheel spin and Ackermann steering), `open_door`, `close_door`, and `set_lights`.

### 5. Full-Body Actor-vs-Actor Collisions, Vehicle-vs-Prop Checks & Beat-Local Repair (`colliders.py`, `validate_collisions.py`)
- **Full-Body Capsule Actor-vs-Actor Validation**: Upgraded Actor-vs-Actor collision detection in `validate_collisions.py` to test all 24 anatomical capsules (`CharacterModel.body_capsules()`, including arms, forearms, thighs, shins, and feet) rather than only `torso_capsules()`.
- **Moving Vehicle-vs-Vehicle / Prop Validation**: Added chassis OBB corner checks ($y > 0.35\text{ m}$) for moving vehicles against other vehicles and static props.
- **Beat-Local `repair_violations()`**: Scoped automatic collision repair to the specific colliding time window `[first_s - 0.45, last_s + 0.45]` using a smoothstep feather envelope whenever an actor has multi-beat keyframes (`len(ts) > 3`), preventing `--repair` from overwriting earlier or later choreography across `[t0, t1]`.

### 6. 3D Perspective Frustum Solver & General Scene-Graph-Driven LLM Shot Planner (`camera_controller.py`, `llm_planner.py`, `generate_usda_scene.py`, `src/main.cpp`)
- **Perspective Frustum Solver & Validator (`camera_controller.py`)**:
  - Computes the 3D bounding box centroid and radius (including vertical elevation spans on stairs) of `tracked_actors` and solves camera standoff distance $D = (R + \text{margin}) / \tan(\text{FOV}_h / 2)$ and `lookAt` Euler angles (`_look_at_rotation_xyz`) automatically, including elevated `vantage="aerial"` / `"drone"` swoops, `orbit`, and multi-camera `cuts`.
  - Upgraded `validate_frustum()` from a 2D distance check to an exact 3D camera-space perspective NDC projection check ($d_{\text{front}} > 0.25\text{ m}$, $|\text{ndc}_x| \le 1.35$, $|\text{ndc}_y| \le 1.35$) with orthogonal camera basis vectors ($\mathbf{F} \cdot \mathbf{U} = 0$).
- **General Scene-Graph-Driven `LLMShotPlanner` (`llm_planner.py`)**:
  - **Eliminated All Hardcoded Scenario Templates & Coordinate Literals**: Removed all ~10 legacy `if "keyword" in p:` template branches and eliminated every hardcoded `pos(x, y, z, h)` coordinate string from `llm_planner.py`.
  - **Mode A — Live LLM Tool-Calling (`_plan_with_llm_api` & `export_tool_schemas`)**: Dynamically introspects `FunctionCatalog` method signatures and docstrings into OpenAI tool schemas, injects live `scene_data` objects/zones/stairs and `manifest.json` capabilities into the system prompt, executes returned tool calls against `self.catalog`, and supports closed-loop self-correction via `plan_and_compile_with_retry()` if the compiler or validator returns structured errors.
  - **Mode B — General Compositional Semantic Planner (`_plan_general_semantic`)**: Works offline/deterministically for any prompt by:
    1. Discovering existing scene objects (`OakTree`, `CitySquareStairs`, vehicles, props) via `ManifestAssetMatcher` and placing any missing environment assets semantically (`_ensure_environment_context`).
    2. Extracting vehicle and human actor counts, roles (`driver`, `passenger`, `couple`, `crowd`, `runner`, etc.), and placing them via semantic references (`in_vehicle=car_id`, `stairs_main_top`, `stairs_main_bottom`, `beside(OakTree, right, conversational)`, `in_front_of(human_1, conversational)`, `lane_east.north`).
    3. Segmenting multi-sentence / multi-clause prompts (`;`, `then`, `after`, `when`, `as`, `while`, `suddenly`, `, but`) and mapping every clause compositionally across the ~40 `FunctionCatalog` methods with causal event chaining (`after="car_1.stopped"`, `when_closer_than=...`).
  - **Grouped Multi-Door OpenUSD Authoring (`compiler.py`)**: Upgraded `_build_grouped_part_animations_usda()` so multiple doors opening on the same vehicle (`door_fl`, `door_fr`, `door_rl`, `door_rr`) are grouped under a single `over "Doors"` block in `shot.usda`, preventing duplicate prim parse errors.

---

## Scene Versioning & Non-Destructive Shot Inheritance (`tools/scene_manager.py`)

PainterSpace decouples main scene authoring from individual shots so that scene-level edits never break or mutate existing shots:

- **Main Scene Editing**:
  - In the 3D editor, clicking **Edit Main Scene** opens `scenes/scene_XX/scene.usda` directly in the viewport.
  - Making layout edits (positioning buildings, trees, parked vehicles) and clicking **Save Main Scene** creates a new immutable version snapshot:
    `scenes/scene_XX/scene_v1.usda`, `scene_v2.usda`, etc., and updates `scenes/scene_XX/scene.json`.
  - **Compiled Shots**: Every compiled shot sublayers an immutable scene snapshot (`@../scene_vN.usda@`) and records the version in `shot.json`. This guarantees that subsequent edits to the live main scene never invalidate camera angles, character paths, or collision checks of finished shots.
- **Python Scene Manager API**:
  ```python
  from tools.scene_manager import SceneManager
  sm = SceneManager(scene_dir="scenes/scene_01")
  new_version = sm.save_scene_edit("Moved red car to sidewalk curb")
  # Returns 'scene_v2.usda' and updates manifest / metadata
  ```

---

## Scalable Vehicle Occupancy & Dynamic Coupling Engine

Vehicle occupancy in PainterSpace is completely data-driven and scalable for 1 driver, 2 people, or 4+ occupants without hardcoded coordinates, seat names, or parallel driving beats:

### 1. High-Level Function Catalog APIs (`tools/shot_library/function_catalog.py`)

- **Scalable Boarding (`board_vehicle` & `place(..., in_vehicle=...)`)**:
  ```python
  # Batch board 4 occupants into a car with automatic seat allocation:
  catalog.board_vehicle("car_1", ["Alice", "Bob", "Charlie", "David"])

  # Or place an individual driver into a car:
  catalog.place("character.human", in_vehicle="car_1", label="Driver 1")
  ```
  The system queries the vehicle asset's anchor schema and automatically allocates available seats in order:
  - Seat 1 $\rightarrow$ `driver_seat` (`[-0.36, 0.35, -0.15]`)
  - Seat 2 $\rightarrow$ `passenger_seat` (`[0.36, 0.35, -0.15]`)
  - Seat 3 $\rightarrow$ `seat_rear_left` (`[-0.36, 0.35, 0.75]`)
  - Seat 4 $\rightarrow$ `seat_rear_right` (`[0.36, 0.35, 0.75]`)

- **Automatic Motion Coupling (Zero Manual Drive Beats)**:
  ```python
  # Only the vehicle needs to be commanded to drive:
  catalog.drive_to("car_1", target="roadway.stop_mark", speed="normal")
  ```
  - In [`motion_synthesis.py`](tools/shot_library/motion_synthesis.py), any occupant seated in a vehicle (running or stationary) automatically samples `v_perf.sample(f_time)` at every frame.
  - The occupant's position, heading, and pitch dynamically follow the host vehicle cabin throughout its acceleration, cruising, turning, and braking profile.
  - Allowed contact exemptions with the vehicle cabin are generated automatically to prevent false collision violations.

- **Intelligent Articulated Disembarking (`disembark_vehicle` & `exit_vehicle`)**:
  ```python
  # Disembark all occupants from the vehicle once stopped:
  catalog.disembark_vehicle("car_1", after="car_1.stopped")
  ```
  - **Seat-to-Door Routing**: Dynamically discovers each occupant's current seat and routes them through their corresponding articulated door and roadway stand anchor:
    $$\begin{aligned}
    \text{driver\_seat} &\longrightarrow \text{Door\_FL} \ (\text{front-hinged, swings } -65^\circ) \longrightarrow \text{door\_fl\_stand} \\
    \text{passenger\_seat} &\longrightarrow \text{Door\_FR} \ (\text{front-hinged, swings } +65^\circ) \longrightarrow \text{door\_fr\_stand} \\
    \text{seat\_rear\_left} &\longrightarrow \text{Door\_RL} \ (\text{rear-hinged car1, swings } +65^\circ) \longrightarrow \text{door\_rl\_stand} \\
    \text{seat\_rear\_right} &\longrightarrow \text{Door\_RR} \ (\text{rear-hinged car1, swings } -65^\circ) \longrightarrow \text{door\_rr\_stand}
    \end{aligned}$$
  - **Dynamic OpenUSD Part Animation**: [`compiler.py`](tools/shot_library/compiler.py) emits nested `over "Doors"` and `over "Door_*"` blocks with exact `rotateY.timeSamples`, swinging doors open in synchronization with character egress.

---

## Solid-Object Collision System (Ingest → Prevent → Detect → Repair)

Prompt instructions alone cannot guarantee that an LLM-authored shot will never pass a character's body through a car, open door, tree trunk, or another actor. PainterSpace enforces solid-object behavior in code across **asset ingestion** and **three runtime enforcement layers**:

1. **Ingest & Normalize ([`tools/collision/ingest_asset.py`](tools/collision/ingest_asset.py))**: Normalizes every 3D asset to a single canonical coordinate frame (meters, `Y`-up, `-Z` forward, base-center at `y = 0`) using non-destructive `.norm.usda` wrapper layers, extracts height-filtered compound collision boxes from real mesh triangles, identifies articulated parts (`doors`, `hood`, `trunk`), and precomputes safe standing `anchors`.
2. **Layer 1 — Prevent ([`tools/collision/plan_path.py`](tools/collision/plan_path.py), [`tools/world_state.py`](tools/world_state.py), [`tools/bake_shot_animations.py`](tools/bake_shot_animations.py))**: Allows the LLM or animation plan to state navigation intent (e.g., `go_to = "Car:door_fl_stand"`) and routes characters around inflated obstacle footprints via 8-connected A* + line-of-sight string-pulling so paths are collision-free by construction.
3. **Layer 2 — Detect ([`tools/collision/colliders.py`](tools/collision/colliders.py), [`tools/collision/validate_collisions.py`](tools/collision/validate_collisions.py))**: Evaluates the composed USD stage at sub-frame resolution (`substeps=2`, 48 Hz at 24 fps), poses `UsdSkel` skeletons, builds 10 anatomical bone capsules per character, and measures exact signed distances against all static hulls, live-articulated doors, inline `Gprim` obstacles, and other characters.
4. **Layer 3 — Repair ([`tools/generate_usda_scene.py`](tools/generate_usda_scene.py), [`src/ShotEngine.cpp`](src/ShotEngine.cpp), [`src/main.cpp`](src/main.cpp))**: Feeds structured collision reports (`actor`, `object`, `part`, `frames`, `max_penetration_m`, `push_out_m`, and `advice`) back to the LLM for up to **3 retries**, automatically re-plans or pushes out colliding trajectories (`--repair`) if retries fail, and **blocks rendering** if any violation remains.

### Root Problems Solved

- **Size-only bounds (`bounds_m`)**: Raw `[width, height, depth]` sizes lacked min/max offsets relative to the pivot and ignored object heading, giving rotated vehicles the wrong footprint.
- **Inconsistent asset up/forward axes (`cars.car2`, `character.human`)**: OpenUSD does not automatically rotate a referenced `Z`-up asset (`car2.usdc`, `human.usdc`) when referenced into a `Y`-up stage. Without normalization, `car2.usdc` (`up_axis: Z`, `forward_axis: -Y`) lay on its side and rotated around the wrong axis.
- **Tree canopies vs. trunks**: Using a tree's full bounding box ($3.1 \times 3.6\text{ m}$ canopy) blocked characters from walking under high foliage, whereas ignoring trees let characters walk through trunks and low branches.
- **Articulated moving parts (car doors)**: Open doors sweep out space up to $\sim 2\text{ m}$ from the vehicle centerline and can be animated via `rotateX/Y/Z`, `rotateXYZ`, `transform` matrices, or USD VariantSets (`doors = "open"`).
- **Full-body limb swing**: In-place Mixamo clips with stripped root motion swing arms and legs outside a simple center-point footprint.
- **Waypoint corner-cutting**: Linear interpolation between two clear endpoints can slice through an obstacle between them.

### 1. Asset Ingestion & Non-Destructive Normalization (`tools/collision/ingest_asset.py`)

Running `ingest_asset.py` inspects a USD asset's composed stage, skin/mesh geometry, and hierarchy and writes structured metadata to [`manifest.json`](manifest.json):

- **Non-Destructive `.norm.usda` Wrapper Layers**:
  - computes the $4 \times 4$ normalization transform $L = T(\text{shift}) \cdot S(\text{metersPerUnit}) \cdot R_{\text{axes}}$ that maps the asset's native up-axis to `+Y`, native forward-axis to `-Z` (automatically inferred for `UsdSkel` characters from the toe-minus-heel foot vector), and shifts the resting base center to `(0, 0, 0)`.
  - If $L \neq I$, writes `<stem>.norm.usda` alongside the original `.usdc` file using `over "<defaultPrim>"` (`prepend references = @./<stem>.usdc@`) with `matrix4d xformOp:transform:painterspaceNorm` prepended to `xformOpOrder` and `customData = { bool painterspace_normalized = 1 }`.
  - Because the wrapper uses `over` on the original `defaultPrim` rather than nesting a new parent Xform, **all internal prim paths (such as `/Root/Hips/...` for `UsdSkel` bindings and `/Car/Body/DoorFL` for vehicle parts) remain 100% unchanged**, and original `.usdc` binaries are never modified.
- **Height-Filtered Compound Collision Boxes (`collision.shapes`)**:
  - Samples triangles across all visible meshes in the normalized frame and retains only geometry between `STEP_HEIGHT_M = 0.15 m` and `REACH_HEIGHT_M = 2.10 m`.
  - **Tree canopies above $2.10\text{ m}$ are automatically ignored** so characters can walk freely beneath foliage while still colliding with trunks and low-hanging branches below $2.10\text{ m}$.
  - Splits geometry along height slabs and horizontal cells (`decompose_boxes`) to produce tight compound oriented bounding boxes (`center`, `size`) for the static `body` and each articulated part.
- **Articulated Parts (`parts`)**:
  - Detects doors (`door_fl`, `door_fr`, `door_rl`, `door_rr`), `hood`, and `trunk`, recording each part's `prim_path`, normalized hinge `axis`, `hinge` point, `range_deg`, `rest_deg`, and $4 \times 4$ rest `collider_frame` so `colliders.py` can transform door colliders using the door prim's live USD transform at any frame.
- **Clearance-Checked Standing Anchors (`anchors`)**:
  - Generates named interaction spots (`stand_front`, `stand_back`, `stand_left`, `stand_right`, and `door_fl_stand`, `door_fr_stand`, `door_rl_stand`, `door_rr_stand`) with `[x, 0, z]` coordinates and `facing_deg` oriented toward the asset.
  - Door standing anchors are placed clear of both the car body (`radius + 0.30 m`) and the full swept arc of the open door (`radius + 0.25 m`), ensuring they never fall inside the path planner's $0.45\text{ m}$ keep-out zone even when the door opens.

```bash
# Ingest a single asset (or re-ingest with explicit forward axis)
.venv/bin/python3 tools/collision/ingest_asset.py assets/usdc_assets/cars/car2.usdc \
  --id cars.car2 --forward=-Y --manifest manifest.json --write
```

### 2. Layer 1 — Prevent: Obstacle-Aware Path Planning (`tools/collision/plan_path.py`)

Instead of hand-guessing intermediate coordinates around obstacles, shots and animation plans can specify **navigation intent**:

- **In `shot.usda` (`customData` on an actor prim)**:
  ```usda
  def Xform "Hero" (
      customData = {
          string asset_id = "character.human"
          string go_to = "Car:door_fl_stand"
          string allow_contact = "Car:door_fl"
          double speed_mps = 1.4
      }
      prepend references = @../../../assets/usdc_assets/humans/human.norm.usda@
  )
  ```
- **In `tools/bake_shot_animations.py` (`trajectory` block)**:
  ```json
  "trajectory": {
    "start": [-4.0, 0.0, 2.0],
    "to_anchor": "Car:door_fl_stand"
  }
  ```
- **Direct CLI usage**:
  ```bash
  .venv/bin/python3 tools/collision/plan_path.py scenes/scene_01/shot_002/shot.usda \
    --manifest manifest.json --actor Hero --to-anchor Car:door_fl_stand --in-place
  ```

How `plan_path.py` works:
1. Evaluates the union of all static and moving obstacle footprints (`obstacles()`) over the time window `t0..t1` (ignoring the specific door part being approached when routing to `door_*_stand`).
2. Rasterizes convex 2D footprints onto a $0.10\text{ m}$ grid and dilates them by `radius = 0.45 m` (`rasterize()`), covering the character's torso and swinging limbs.
3. Solves for the shortest collision-free path using 8-connected A* (`astar()`) with corner-cutting prevention, then simplifies the path via line-of-sight string-pulling (`line_free()`).
4. Samples constant-speed positions and turn-rate-limited headings (`sample_path()`, capped at $270^\circ/\text{s}$), clears any prior transform opinions (`GetAttr().Clear()`), preserves the actor's initial $Y$ elevation, blends to the anchor's `facing_deg` at arrival, and authors `xformOp:translate` and `xformOp:rotateY` keyframes (`author_root_motion()`).

### 3. Layer 2 — Detect: Per-Frame Skeletal Capsule Validator (`tools/collision/validate_collisions.py`)

`validate_collisions.py` inspects every frame (and half-frame substep, `substeps=2`) of a composed stage:

- **Posed Anatomical Capsules (`CharacterModel` in [`tools/collision/colliders.py`](tools/collision/colliders.py))**:
  - Uses `UsdSkel.Cache` + `UsdSkel.AnimQuery` to compute world-space joint positions at each time code.
  - Builds 24 anatomical bone capsules (`Hips→Spine`, `Spine→Spine1`, `Spine1→Spine2`, `Spine2→Neck`, `Neck→Head`, `Head→HeadTop_End`, `Spine2→Shoulders`, `Shoulders→Arms`, `Arms→Forearms`, `Forearms→Hands`, `UpLegs→Legs`, `Legs→Feet`, `Feet→Toes`, `Toes→ToeEnds`) plus 2 hand spheres (`LeftHand`, `RightHand`) with anatomical radii ($0.04\text{ m}$–$0.15\text{ m}$) ensuring pointing gestures, punches, and footsteps are detected.
- **Live Articulated Colliders & Broad-Phase Culling (`BoxGroup`)**:
  - Static `body` boxes are transformed by the instance's world matrix $M_{\text{inst}}(t)$.
  - Articulated `parts` (`door_fl`, etc.) are transformed directly from the part prim's live world matrix $M_{\text{part}}(t) \cdot F_{\text{part}}$, accurately handling doors animated via `rotateY`, `rotateXYZ`, `transform` matrices, or USD VariantSets (`doors = "open"`), as well as non-uniform parent scale.
  - Broad-phase world AABB culling (`BoxGroup.candidates()`) filters box candidates before vectorized narrow-phase segment-to-OBB signed-distance evaluation (`BoxGroup.sd()`).
- **Character-vs-Character & Unprotected Asset Checks**:
  - Checks every pair of characters on stage using their posed torso/limb capsules (`segment_distance()`).
  - Any non-environment asset missing collision shapes is reported in `unprotected_assets`.
- **Compiler-Verified Contact Allow-Lists**:
  - Intentional contact (e.g., striking in combat, opening a door, or sitting in a vehicle) is permitted strictly via compiler-generated sidecars (`allow_collisions.json`, `--allow`, or `allow.json`), where each rule is strictly bound to a limb, a time window, and a maximum penetration depth limit. Seated vehicle occupants are verified to remain within the host vehicle cabin volume.

```bash
# Validate a shot stage (routing any customData go_to intents and auto-repairing if needed)
.venv/bin/python3 tools/collision/validate_collisions.py \
  scenes/scene_01/shot_002/shot.usda --manifest manifest.json --apply-intents --repair
```

### 4. Layer 3 — Repair & Hard Render Gate

- **Structured LLM Retry Loop (`RETRIES = 3`)**:
  - Both [`tools/generate_usda_scene.py`](tools/generate_usda_scene.py) and [`src/main.cpp`](src/main.cpp) run collision validation on every generated shot.
  - If a collision is detected on attempts $1\dots 3$, the validator produces a structured diagnostic for each colliding `(actor, object, part)` tuple including the time range, worst frame, penetration depth in meters, world-space `push_out_m` vector, and actionable `advice` (including available anchor names on the target asset), which is fed back to the LLM.
- **Automatic Path Repair (`--repair`)**:
  - If collisions remain after retries (or when validating directly via `ShotEngine::validateAuthoredUsdStage`), `repair_violations()` nudges any start/goal out of keep-out zones and re-plans moving actors around obstacles via `plan_path.py`, or translates stationary colliding actors along `push_out_m`.
- **Hard Render Gate**:
  - [`ShotEngine::validateAuthoredUsdStage`](src/ShotEngine.cpp) (called by `--validate-usda`, `--generate-usda`, `--render-usd`, and GUI rendering) rejects any shot that still has a collision after `--repair`, ensuring a body-through-solid violation is never rendered.

### Automated Collision Test Suite (`tools/collision/test_collision_tools.py`)

Run the self-contained 13-scenario test suite exercising ingestion, normalization, A* path planning, articulated doors, tree canopies vs. trunks, door anchor routing, and character-vs-character collision detection on real project assets:

```bash
.venv/bin/python3 tools/collision/test_collision_tools.py
```

| Scenario | Description | Expected Result |
| --- | --- | --- |
| **H** | Ingest Z-up, cm-scaled, `+X`-facing asset into Y-up, meter, `-Z`-forward `.norm.usda` wrapper | **PASS** (nose at $-Z$, base at $y=0$) |
| **A** | Character walks straight through `cars.car1` | **FAIL** (detected `cars.car1/body` & doors) |
| **B** | `plan_path.py` routes character around `cars.car1` | **PASS** (4 waypoints, 0 penetrations) |
| **C** | Character walks past `cars.car1` at $x = -1.6\text{ m}$ with doors closed | **PASS** |
| **D** | Same path at $x = -1.6\text{ m}$ with `doors = "open"` ($-62^\circ$) | **FAIL** (detected `cars.car1/door_fl` & `door_rl`) |
| **E** | `plan_path.py` routes around open doors ($x = -2.2\text{ m}$) | **PASS** |
| **F** | Intentional contact with `cars.car1/door_fl` whitelisted via `--allow` | **PASS** |
| **G1** | Character walks into tree trunk | **FAIL** (detected `trees.test/body`) |
| **G2** | Character walks clear of low branch | **PASS** |
| **G3** | Character walks into low branch ($y = 1.4\text{ m}$) | **FAIL** (detected `trees.test/body` by `Spine1>Spine2`) |
| **G4** | Character walks under tree canopy ($y > 2.2\text{ m}$) outside trunk | **PASS** (canopy ignored above $2.1\text{ m}$) |
| **I** | `plan_path.py --to-anchor Car:door_fl_stand` with door opening | **PASS** (arrives clear of body and open door) |
| **J** | Two characters walk through each other | **FAIL** (detected actor-vs-actor torso penetration) |

---

### Current limitations

- The scene prompt is connected to the OpenAI API through the USDA generator; an API key must be available in `OPENAI_API_KEY` when PainterSpace launches.
- Hydra Storm provides guide-quality previs rendering.

## 3D Viewport Asset Picking & Crash Prevention

### The Problem
When clicking or touching 3D assets in the Hydra Storm viewport, previous implementations suffered from an immediate crash (`EXC_BAD_ACCESS` / `SIGSEGV`). This occurred due to architectural mismatches:
1. **Unbound GPU Context During Mouse Events**: `UsdViewportWidget` is a standard `QWidget` using an offscreen blit architecture (Hydra renders offscreen into an AOV buffer and blits to the screen with `QPainter`). Calling hardware GPU picking (`UsdImagingGLEngine::TestIntersection`) directly from Qt's asynchronous `mousePressEvent` dispatched GPU command buffers with no active OpenGL/Metal context bound on the UI thread.
2. **Missing Picking AOVs**: The offscreen engine is configured with `SetRendererAov(HdAovTokens->color)` to optimize render throughput. Hardware picking requires primitive ID, instance ID, and depth AOVs; querying them when only a color buffer existed triggered null pointer dereferences.
3. **Frustum Matrix Corruption**: Attempting to scale projection matrices manually distorted the perspective $W$-divide column, causing frustum inversion and plane culling assertion failures.
4. **Offscreen GPU Highlight Crash**: Enabling Hydra's post-processing outline (`renderParams.highlight = true`) failed in offscreen single-AOV mode due to missing depth/ID texture sampling.

### The Solution
We implemented a robust, high-performance hybrid picking architecture:
- **Fast CPU Ray-Cast Picking (`tryCpuRayPick`)**: Uses the viewport's camera projection to construct a 3D ray (`screenToRay()`) and intersects it (`GfRay::Intersect(bbox)`) with the world bounds of top-level `/World` prims (actors, vehicles, props, and `/World/Paint` strokes). Runs entirely on the CPU and needs no GPU context.
- **Context-Guarded GPU Fallback (`canUseGpuPicking` & `tryGpuPicking`)**: Pre-validates that an active GPU context is bound (`QOpenGLContext::currentContext()`) and uses native `GfFrustum::ComputeNarrowedFrustum` before attempting hardware picking.
- **Native Qt Visual Overlays**: `renderParams.highlight` is kept `false` in Hydra's offscreen pass, while selection bounding boxes and interactive 3D Move/Rotate/Scale gizmos are drawn cleanly and responsively in Qt via `QPainter` in `drawSelectionAndGizmoOverlay()`.

### The actual root cause of the remaining crash: header / library ABI mismatch
Even with CPU picking, clicking an asset still crashed with `EXC_BAD_ACCESS` inside `TfToken::_RemoveRef` ← `UsdGeomBBoxCache::~UsdGeomBBoxCache` ← `tryCpuRayPick`.

PainterSpace compiles against OpenUSD **25.08 headers** staged in `third_party/openusd` but links against **Apple's private system build** (`/usr/lib/usd/libusd_ms`). These do not share the same private layout for every class. For `UsdGeomBBoxCache`, the constructor runs inside `libusd_ms` (Apple's layout) while the destructor is inlined into PainterSpace from the 25.08 headers (a different layout; `libusd_ms` does not even export a destructor). Placing one on the stack therefore corrupted memory and crashed when it went out of scope. The same object was also built in `drawSelectionAndGizmoOverlay()` (the first repaint after a selection) and in `frameSelectedPrim()` (`F` key).

**Fix:** all bounds now go through `computeWorldBoundSafe()`, which calls `UsdGeomImageable::ComputeWorldBound()`. The library creates and destroys its own BBoxCache internally and returns only a `GfBBox3d`, a plain value type with a stable layout.

> **Rule:** Do not create USD classes with private, non-trivial state (`UsdGeomBBoxCache`, `UsdGeomXformCache`, …) in app code. Use library entry points that return plain value types (`GfBBox3d`, `GfMatrix4d`, `UsdPrim`, `TfToken`, …).

**Regression test:** `./build/PainterSpace --usd-pick-test [shot.usda]` opens the real `MainWindow`, makes the Hydra viewport visible, and sends 40 real click + drag + release mouse events across it. Each event triggers picking, a Hydra render, and the selection overlay. Afterwards `user_edits.usda` is restored byte-for-byte.

## High-Density Foliage & Viewport Performance Optimizations

### Root Causes of Lush Tree Freezes / Crashes
Adding high-density foliage assets (`10–13 MB` `.usdc` files such as `oak_tree_lush.usdc`) to the 3D viewport previously caused severe freezes or GPU driver crashes due to a combination of asset topology and viewport update overhead:
1. **Extreme Prim-Count Explosion (1 Mesh Per Leaf)**: `oak_tree_lush.usdc` contained **112,949 total USD prims** and **54,952 individual `UsdGeomMesh` prims** (each 6-vertex leaf exported as a separate mesh). Hydra Storm Metal had to allocate and synchronize 54,952 separate `HdMesh` Rprims and Metal draw items for a single tree.
2. **Missing Authored Extents & Material Binding Schemas**: `birch_tree_lush.usdc` (37,819 meshes) and `pine_tree_lush.usdc` (17,095 meshes) had zero authored `extent` attributes and omitted `MaterialBindingAPI` schema declarations, forcing OpenUSD to transform every vertex of tens of thousands of meshes on the CPU to compute bounding boxes (~375 ms per call).
3. **Uncached Per-Frame `ComputeWorldBound()`**: `drawSelectionAndGizmoOverlay()` and `tryCpuRayPick()` recomputed `ComputeWorldBound()` from scratch on every `paintEvent` and mouse click.
4. **Full Hydra Engine Resets on Scene Edits**: Adding, duplicating, deleting, or undoing an asset called `myImpl->engine.reset()`, destroying and rebuilding the entire `UsdImagingGLEngine` and resyncing all prims from scratch.
5. **Redundant Hydra Selection Traversal & Disk Writes on Drag**: `renderHydraToImage()` called `engine->SetSelected()` on every frame despite `renderParams.highlight = false`, and `setSelectedPrimPosition()` wrote `user_edits.usda` to disk on every `mouseMoveEvent` during gizmo dragging.

### USD Asset & Viewport Optimizations
1. **Merged Static Tree Meshes by Material + Baked Extents (`assets/usdc_assets/trees/*.usdc`)**:
   - Combined static trunk, branch, and leaf sub-meshes per material while preserving **100% of vertices, faces, normals, `UsdPreviewSurface` materials, and `doubleSided` foliage**.
   - Applied `MaterialBindingAPI` schemas and baked `extent` attributes on all meshes and root prims, dropping `ComputeWorldBound()` on `oak_tree_lush.usdc` from **374.6 ms to 0.04 ms (~9,300× faster)**:

| Asset | Before (Meshes / Size) | After (Meshes / Size) | Geometry Preserved |
| --- | --- | --- | --- |
| `oak_tree_lush.usdc` | 54,952 meshes (`13.0 MB`) | **3 meshes (`4.7 MB`)** | 408,329 pts / 351,356 faces |
| `oak_tree.usdc` | 16,639 meshes (`4.0 MB`) | **3 meshes (`1.4 MB`)** | 126,116 pts / 108,686 faces |
| `birch_tree_lush.usdc` | 37,819 meshes (`8.9 MB`) | **5 meshes (`2.6 MB`)** | 228,662 pts / 78,000 faces |
| `birch_tree.usdc` | 19,189 meshes (`4.5 MB`) | **5 meshes (`1.3 MB`)** | 116,452 pts / 40,198 faces |
| `pine_tree_lush.usdc` | 17,095 meshes (`7.3 MB`) | **5 meshes (`5.3 MB`)** | 466,929 pts / 159,793 faces |
| `pine_tree.usdc` | 5,429 meshes (`2.2 MB`) | **5 meshes (`1.6 MB`)** | 141,499 pts / 49,847 faces |

2. **Incremental Hydra Scene Updates (`UsdViewportWidget.cpp`)**:
   - Removed `myImpl->engine.reset()` on asset additions, deletions, duplications, paint strokes, and undo/redo so `UsdImagingGLEngine` updates incrementally via USD change notices.
3. **Per-Prim World Bound Cache (`cachedWorldBound` / `invalidateBound`)**:
   - Cached world-space `GfBBox3d` bounds per prim path and frame in `UsdViewportWidget::Impl`, invalidating only the modified prim when transforms or stage layers change.
4. **Deferred Disk Saves & Zero-Overhead Selection Rendering**:
   - Removed `engine->SetSelected()` / `ClearSelected()` from `renderHydraToImage()` (since Qt renders the selection overlay in 2D) and deferred `userEditsLayer->Save()` during interactive gizmo/ground dragging until `mouseReleaseEvent`.
5. **Filtered Standalone Wind Demo Stages (`MainWindow.cpp`)**:
   - Excluded multi-hundred-MB `*_wind_demo*.usda` simulation stages from `MainWindow::populateAssetEntries()` so only placeable assets appear in the Reference Assets browser.

## Viewport controls

- **Right-drag** (or **Alt + Left-drag**): orbit the camera (automatically switches from `ShotCam` to `Free Orbit Camera`).
- **Middle-drag** (or **Shift + Left-drag**): pan the camera.
- **Scroll wheel**: zoom in/out.
- **Left-click (Select tool)**: pick a USD prim via crash-proof CPU ray-box picking and drag Move / Rotate / Scale gizmo handles (`W` / `E` / `R`).
- **`F` key**: frame the selected USD prim.
- **`Space` key**: play / pause timeline animation.
- **`Delete` / `Backspace`**: deactivate selected prim in `user_edits.usda`.
- **`Cmd+D`**: duplicate selected prim in `user_edits.usda`.
- **`Cmd+Z` / `Cmd+Shift+Z`**: undo / redo `user_edits.usda` changes.
- **Brush / Eraser tools**: paint or erase `UsdGeomBasisCurves` ground strokes under `/World/Paint` in `user_edits.usda`.
