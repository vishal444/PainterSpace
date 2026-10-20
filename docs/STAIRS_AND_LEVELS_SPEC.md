# PainterSpace Multi-Level Navigation & Stair Synthesis Specification

## Problem Statement

The PainterSpace generation and simulation pipeline previously lacked awareness of elevation along a navigation path. Walk and run animation clips were in-place, root motion was evaluated purely as a 2D planar path on the character's Xform (`y = 0`), and the collision system treated geometry between `0.15 m` and `2.10 m` (measured from `y = 0`) as solid obstacles.

Stairs and multi-level buildings fail across four key areas:
1. **Collision**: Risers appear as vertical walls to planners and validators, treads under `0.15 m` are ignored, and any structures on upper floors sitting above `2.10 m` are ignored.
2. **Root Height**: No elevation channel moves the character's $Y$ with the stair incline and landings.
3. **Foot Sliding and Clipping**: The animation clip's stride frequency and step length do not match physical tread depths.
4. **Level Awareness**: The path planner operates on a single 2D grid, preventing path routing between floors.

---

## Architecture & Subsystems

### 1. Manifest Data Model: Levels & Stairs

All geometry, anchors, and centerlines are defined in the building asset's local coordinate frame so they automatically track instance transformations (`xformOp:translate`, `xformOp:rotateXYZ`, `xformOp:scale`).

```json
{
  "levels": [
    {"id": "L0", "floor_y": 0.0},
    {"id": "L1", "floor_y": 3.0}
  ],
  "stairs": {
    "stairs_front": {
      "from_level": "L0",
      "to_level": "L1",
      "centerline": [
        [0.0, 0.0, 2.0],
        [0.0, 1.4, -0.5],
        [0.0, 3.0, -1.2]
      ],
      "width_m": 1.1,
      "treads": [
        {"center": [0.0, 0.18, 1.8], "rise_m": 0.18, "tread_m": 0.28},
        {"center": [0.0, 0.36, 1.52], "rise_m": 0.18, "tread_m": 0.28}
      ],
      "anchors": {
        "bottom": "stairs_front_bottom",
        "top": "stairs_front_top"
      }
    }
  }
}
```

- **Centerline**: 3D polyline defining the traversal path across landings, straight flights, and switchbacks. Elevation is parameterized along arc length $s$.
- **Tread Table**: Sampled at asset ingestion via raycasting or step prim bounds. Used for root elevation, cadence calculation, and foot contact validation.

---

### 2. Authored Stair Markers in OpenUSD

Stairs are identified in source assets via customData markers:

```usda
def Xform "Stairs_front" (
    customData = {
        string painterspace_role = "stairs"
        string from_level = "L0"
        string to_level = "L1"
    }
)
{
    def Xform "Bottom"
    {
        double3 xformOp:translate = (0, 0, 2.0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def Xform "Landing_01"
    {
        double3 xformOp:translate = (0, 1.4, -0.5)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def Xform "Top"
    {
        double3 xformOp:translate = (0, 3.0, -1.2)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
```

`tools/collision/ingest_asset.py` extracts these markers, derives the centerline, raycasts tread heights, and computes bottom/top anchors. An optional `--suggest-stairs` flag identifies candidate step sequences.

---

### 3. Collision: Stair Carve-Outs, Multi-Level Bands, and 2D Portals

- **Stair Carve-Out**: `decompose_boxes` subtracts the stair sweep volume from the tread surface up to $2.1\text{ m}$ above the centerline. Handrails, side walls, and soffits (underside) remain solid obstacles.
- **Per-Level Height Bands**: Colliders are grouped into height bands per level:
  $$\text{floor\_y} + 0.15\text{ m} \le y \le \text{floor\_y} + 2.10\text{ m}$$
- **Portal Pathfinding**: `tools/collision/plan_path.py` maintains planar 2D obstacle grids per level. Pathfinding from $L0$ to $L1$ executes:
  1. 2D A\* on $L0$ from start to `stairs.bottom`.
  2. Direct parametric traverse along the stair centerline.
  3. 2D A\* on $L1$ from `stairs.top` to destination.
- **Headroom Clearance**: A $2.1\text{ m}$ vertical clearance envelope along the entire stair centerline ensures character heads do not collide with upper floor slabs.

---

### 4. Continuous Elevation Channel & Torso Pitch

The character root translation $Y$ is evaluated along the sloped nosing line:
$$y(s) = y_0 + \frac{\text{rise}}{\text{tread}}(s - s_0)$$
smoothed with Hermite/smoothstep easing over $\sim 0.3\text{ m}$ at the entrance and exit of each flight.

- **Ascent**: Torso pitches forward $+5^\circ$ to $+10^\circ$.
- **Descent**: Torso remains upright or slightly leans back $-2^\circ$ to $-5^\circ$.

---

### 5. Cadence & Stride Matching

Foot sliding is resolved deterministically by synchronizing clip playback speed with step frequency:
1. Target step frequency:
   $$f = \frac{v_h}{\text{tread\_depth}}$$
2. Clip reference frequency (footfalls per second):
   $$f_{clip} = \frac{\text{footfalls}}{\text{clip\_duration\_s}}$$
3. Baker playback speed:
   $$\text{speed} = \frac{f}{f_{clip}}$$

Speed limits:
- Walking stairs: $0.5 - 0.8\text{ m/s}$ horizontal.
- Running stairs: capped at $1.8 - 2.5\text{ m/s}$ (compared to $4.2\text{ m/s}$ flat run).
- Descent speed: strictly capped at $\le 1.0\text{ m/s}$.

---

### 6. Semantic Closed-Vocabulary Function

```python
catalog.climb_stairs(
    actor="human_1",
    stairs="CitySquare:stairs_front",
    direction="up",  # "up" or "down"
    pace="walk",     # "walk" or "run"
)
```

The compiler expands this into:
1. Locomotion to bottom anchor.
2. Elevation-guided, cadence-matched traversal along centerline.
3. Exit to top landing and update actor's current level ($L0 \to L1$).

---

### 7. Validation Rules

- **Foot-vs-Tread**: Planted foot capsules must stay within $[-0.02\text{ m}, +0.08\text{ m}]$ of tread surface.
- **Knee-vs-Nosing**: Prevents characters climbing too steeply for their limb dimensions.
- **Level Integrity**: Flags any shot where an actor changes elevation level without crossing an authored stair portal.
- **Allow Rules**: Auto-generates foot-stair and hand-rail allowed contact bounds in `allow_collisions.json`.

---

## Implementation Roadmap

- [x] **Phase 1: Data Model & Ingestion**
  - Define `levels` and `stairs` schema in `manifest_schema.py` and `ingest_asset.py`.
  - Author test stair markers in asset (`test_building_stairs.usda`).
  - Implement marker extraction and tread raycasting in `ingest_asset.py`.
- [x] **Phase 2: Collision Carve-Outs & Per-Level Bands**
  - Subtract stair envelopes in `decompose_boxes`.
  - Implement per-level height slices in `colliders.py`.
  - Add $2.1\text{ m}$ headroom verification (`check_stair_headroom`).
- [x] **Phase 3: Multi-Level Path Planning**
  - Per-level obstacle grids in `plan_path.py`.
  - Stair portal graph connecting levels.
  - Actor level state tracking and multi-level 3D routing.
- [x] **Phase 4: Motion Synthesis & Cadence Matching**
  - Continuous Hermite eased nosing elevation generator in `motion_synthesis.py` and `plan_path.py`.
  - $f_{clip}$ measurement and cadence-matched speed scaling ($f / f_{clip}$).
  - Torso pitch adjustment ($+7.5^\circ$ ascent, $-3.0^\circ$ descent).
- [x] **Phase 5: Semantic Catalog & Compiler Integration**
  - Add `climb_stairs` to `function_catalog.py`, `llm_planner.py`, `timeline_resolver.py`, and `compiler.py`.
  - Structured error handling for invalid stair paces (`INVALID_STAIR_PACE`) and speed caps (`UNSAFE_STAIR_SPEED`).
- [x] **Phase 6: Verification & Test Suite**
  - Add test scenarios K (straight flight PASS), L (stair wall collision FAIL), M (descent PASS), N (cadence matching & structured pace error), O (low ceiling headroom failure FAIL).
  - Level change integrity checks in `validate_collisions.py`.
- [ ] **Phase 7: Optional Polish (Foot IK)**
  - Two-bone leg IK pass for non-standard or irregular tread heights.

