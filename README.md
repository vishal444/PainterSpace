# PainterSpace

PainterSpace is a Qt / OpenUSD + Hydra Storm 3D storyboard studio where the composed USD stage (`user_edits.usda` over `shot.usda` over `scene.usda`) is the single source of truth for interactive editing, LLM shot generation, and rendering.

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
- **Two-Step Scene & Shot Storyboard Workflow**:
  - **Step 1 (Describe Scene)**: Configure the shared scene (`scenes/scene_XX/scene.usda`) via an LLM prompt, the USD 3D editor, or both.
  - **Step 2 (Individual Shots)**: Build and refine shots in a 3×3 storyboard book grid (`scenes/scene_XX/shot_XXX/shot.usda` + `user_edits.usda`), with Hydra-rendered thumbnails.

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

### Validation and asset safety

Before rendering, PainterSpace accepts only `.usda` text and checks that the stage exists and is within the 8 MiB file and 10,000-prim limits. Declared duration and frame rate are capped at a combined 72,000 frame-rate/seconds budget. USD references must resolve inside the project `assets/` library (for example `@../../../assets/usdc_assets/humans/running_fast.usdc@`) or the scene layer (`@../scene.usda@`); absolute paths, URLs, and non-USD references are rejected. `usdcat` is required, while `usdchecker` is run when available.

### Current limitations

- The scene prompt is connected to the OpenAI API through the USDA generator; an API key must be available in `OPENAI_API_KEY` when PainterSpace launches.
- Hydra Storm provides guide-quality previs rendering. USD does not simulate physics, and character movement requires authored clips or keyframed transforms.

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
