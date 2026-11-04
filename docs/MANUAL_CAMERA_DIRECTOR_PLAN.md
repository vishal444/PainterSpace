# Manual Camera Director Mode — Detailed Plan

Give users full manual control of shot composition in the 3D editor, alongside the existing LLM prompt mode: a movable camera, aim/focus targets, and drawn camera paths for moving shots (dolly, track, crane, orbit, …). What the camera sees is what gets rendered.

---

## 1. Goals & Non-Goals

**Goals**
1. A directly manipulable **ShotCam**: position, aim, roll, focal length, aperture/DOF.
2. **Aim modes**: free aim, look-at a prim/character/bone, or frame-and-follow a set of actors.
3. **Drawn camera paths**: user draws or places a 3D spline; the camera travels along it with controllable timing/easing.
4. **Look-at tracks** independent from the position path (dolly while keeping the hero in frame).
5. **Live "through the lens" preview**, safe-frame/aspect overlays, and scrubbable timeline.
6. Output renders (thumbnail, Hydra playback, `video.mp4`) use exactly this camera.
7. Coexists with the LLM planner: manual camera is never silently overwritten, and the LLM can optionally be asked to produce a *starting* camera that the user then refines.

**Non-goals (v1):** multi-camera editing/cuts within one shot (see Phase 7), physically simulated rigs (gimbal/steadicam shake beyond a simple noise layer), camera collision avoidance beyond a warning.

---

## 2. Current State (what we build on)

| Area | Today | File |
|---|---|---|
| Viewport | Hydra Storm in a Qt widget; `useShotCam` toggle between `/World/ShotCam` and a free orbit camera (`yaw/pitch/distance/target`) | [UsdViewportWidget.cpp](../src/UsdViewportWidget.cpp) (`setUseShotCamera` ~L912, `SetCameraState` ~L1827, `renderCurrentView` ~L1906) |
| Interaction | Select / gizmo (Move/Rotate/Scale) on prims, mouse/wheel/key handlers, QPainter overlays (grid, gizmo, name tags) | `mousePressEvent` ~L2437, `drawSelectionAndGizmoOverlay` ~L2159 |
| Persistence | Manual edits go in `user_edits.usda`, layered over `shot.usda` over `scene.usda`; LLM regeneration preserves it | Edit target set in `UsdViewportWidget.cpp` ~L584–607 |
| LLM camera | `CameraController.synthesize_camera` bakes per-frame `translate`/`rotateXYZ` time samples onto `/World/ShotCam` in `shot.usda`; `validate_frustum` checks actor visibility | [camera_controller.py](../tools/shot_library/camera_controller.py) |
| Render | `ShotEngine::renderOpenUsdStage(stage, pattern, start, end, width, cameraPath)` renders any camera path | [ShotEngine.cpp](../src/ShotEngine.cpp) ~L915 |

**Key insight:** the architecture already fits. Because `user_edits.usda` is the strongest layer and the renderer accepts a camera path, manual camera control is mostly (a) a new authoring UI in the viewport and (b) a new, *compact* USD representation that sits in `user_edits.usda`, with no change to the render pipeline beyond choosing the camera.

> [!IMPORTANT]
> The README's "foundational law" bans hardcoded coordinates in the **LLM planner**. This feature is user-authored, so raw coordinates in `user_edits.usda` are fine. But we should still store *semantic targets* (prim paths, bones) rather than baked positions wherever the user chose a target, so the shot stays correct when actors move.

---

## 3. UX Design

### 3.1 Modes
A **Shot Mode** switch in the shot panel / viewport bottom bar:

- **Prompt (LLM)** — existing behaviour.
- **Director (Manual)** — new. Enables camera tools, hides nothing else.
- **Hybrid** — LLM generates, user refines (the default after "Convert to manual").

Switching to Manual does not delete the LLM camera. It copies the LLM camera into the manual camera rig as a starting point (see 4.4).

### 3.2 Viewports
Replace the single "Camera: ShotCam / Free Orbit" combo with:

- **Free view** (editing view; orbit/pan/zoom as today) showing the camera as a 3D frustum gizmo and the path as a curve.
- **Camera view** (through the lens) with overlays: safe frames, aspect mask, rule-of-thirds/centre cross, focal length/HUD readout, and a **dolly zoom** warning when focal and distance change together.
- **Split view** (optional, Phase 5): Free view large + picture-in-picture camera view. Very useful when drawing paths.

Shortcut: `Tab` toggles free ↔ camera view; `C` selects the camera.

### 3.2a Camera Viewfinder Screen & World/Camera Toggle

A dedicated **Camera Viewfinder** shows exactly what the shot camera sees, and the user switches between it and the 3D world view with one toggle.

- **Toggle:** a prominent `World | Camera` segmented switch in the viewport bar (shortcut `Tab`). Replaces the current "Camera: ShotCam / Free Orbit" combo.
  - **World view** is the free-orbit editing view. It shows the camera frustum, the path curve and the aim target.
  - **Camera viewfinder** is a full-screen "through the lens" view of `/World/ShotCam` at the current timeline time. It shows only the scene, with no editing gizmos, and is cropped to the output aspect ratio with letterbox/pillarbox bars.
- **Optional layouts** (Phase 5): *Split* (World large, viewfinder as a PiP) and *Side by side*. The PiP can be popped out as a floating, resizable window for a second monitor.
- **Viewfinder HUD** (toggleable): shot size label (e.g. "Medium"), focal length, aspect, timecode, rule-of-thirds/centre cross, action/title safe frames, and a REC-style indicator while flying or recording.
- **Editing in the viewfinder:** drag to aim (pan/tilt), scroll to zoom, `Shift`+drag to roll, and `Shift`+scroll to dolly. All of these edit the same camera, so the World view updates live.
- **Scrubbing and playback:** the timeline scrubber and Play drive the viewfinder, so the user previews the shot, including the moving camera along its path.

> [!IMPORTANT]
> **Single source of truth:** the viewfinder, the thumbnail and the final render all use the same `/World/ShotCam` samples and the same aspect and resolution. Whatever the viewfinder shows at frame *f* is what renders at frame *f*. The viewfinder, thumbnail and render crops are identical, and a test (§7) enforces this.

### 3.2b Shot Size Presets & Zoom

A **Shot Size** control sits in the viewfinder toolbar and the Camera inspector.

| Preset | Framing (subject = target actor/object) | Typical use |
|---|---|---|
| **Extreme Wide / Establishing** | Subject small in the environment | Locations, scale |
| **Wide / Full** | Whole body plus surroundings | Action, movement |
| **Medium** | Waist-up | Dialogue |
| **Medium Close-Up** | Chest-up | Emotion plus context |
| **Close-Up** | Face fills the frame | Emotion |
| **Extreme Close-Up** | Eyes, or a detail of an object | Emphasis |
| **Over-the-Shoulder / Top-down** | Reuse the existing `CameraController` styles | Dialogue, overview |

- **How a preset is applied:** the user chooses a **subject** (the selected actor/object, or the current look-at target), then a preset. The framing solver (the one behind `CameraController.synthesize_camera`) uses the subject's bounding box, or key bones such as head, chest and hips for humans, to compute the target frame occupancy.
- **Two ways to reach the framing** (a toggle, default *Move camera*):
  1. **Move camera (dolly):** keeps the focal length and changes distance. This preserves perspective and is how a real crew would do it.
  2. **Change lens (zoom):** keeps the position and changes the focal length.
- **Zoom controls:**
  - A continuous **Zoom In / Zoom Out** slider, `+`/`-` keys and scroll wheel in the viewfinder, mapped to focal length (e.g. 12–200 mm) with a live mm readout.
  - Lens preset chips (18 / 24 / 35 / 50 / 85 / 135 mm).
  - Optional **Zoom animation**: set a start and end focal length over a time range, with an ease. This is the zoom lane in the timeline, and it is also available as a path point property.
  - An optional **Dolly zoom** helper (see §5.3).
- **Presets are starting points:** after applying one, the user can still nudge the camera, aim, or zoom. The label shows "Medium (adjusted)" once edited.
- **Presets along a path:** each path control point can carry its own shot size, such as a path that starts **Wide** and ends **Close-Up**. Size is interpolated by distance/focal length, and the camera stays on the subject through the aim track.
- **Storage:** `ps:shot_size` (token) and `ps:shot_subject` (rel) are stored on the rig and per path point as `primvars:ps:shot_size`. They are advisory metadata, and the baked focal length and position are the authority.
- **Framing guides:** overlay lines in the viewfinder mark head-room, look-room and the chosen shot size's target occupancy band. A warning shows when the subject falls outside the band.

### 3.3 Camera tools (toolbar)
| Tool | Behaviour |
|---|---|
| **Select Camera** | Click camera icon/frustum to select; show Move/Rotate gizmo (reuses existing gizmo code). |
| **Aim** | Drag in camera view to pan/tilt (FPS-style); `Shift` = roll. |
| **Look-At** | Click a prim/character/bone in the scene to set the target (cursor shows crosshair). Camera continuously aims at it. |
| **Frame** | Click one or more actors → auto-compute framing (wide/medium/close, vantage) using existing `CameraController` solver; result is editable. |
| **Draw Path** | Click to place control points, or press-and-drag to freehand draw on a drawing plane (see 3.4). |
| **Edit Path** | Move/insert/delete points, adjust tangent handles, set per-point focal length/speed. |
| **Fly (WASD)** | Hold right mouse + WASD/QE to fly camera; "Record" captures the motion as a path (handheld feel). |

### 3.4 Drawing paths in 3D
Drawing a curve in a 2D mouse space is ambiguous in 3D. We provide several unambiguous input methods:

1. **Point-click on surfaces** — ray-pick against scene geometry (Hydra picking already exists for prims); the point is lifted by a *height offset* (default 1.6 m, adjustable with a scroll modifier). Good for ground-level dollies/tracks.
2. **Drawing plane** — pick a plane (ground, height-locked horizontal plane, camera-facing plane, or an axis-aligned plane); freehand strokes are projected onto it. A `Shift`+drag moves the plane height.
3. **Two-view authoring** — draw in top-down view (XZ), then switch to side view and drag an elevation profile curve (for crane/jib shots).
4. **Fly & record** — described above.
5. **Presets** — one-click parametric rigs the user can then edit as ordinary points: *Dolly in/out, Truck left/right, Pedestal up/down, Crane arc, Orbit (around target, radius, arc degrees), Arc, Whip pan, Handheld*.
6. **Attach to actor** — "Follow path of actor X with offset" (tracking shot) that creates a path parented to the actor's motion.

After drawing, the raw stroke is **simplified & fitted** (Ramer–Douglas–Peucker → Catmull-Rom/Bezier fit) into editable control points. Users edit points, not hundreds of samples.

### 3.5 Timeline panel
A camera lane in the shot timeline:

- Path start/end times (the camera can start moving at 1.5 s and stop at 5 s; hold before and after).
- **Keyframe markers** on the path: each control point can have `time`, `focal_length`, `focus_distance`, `aim target weight`, `roll`.
- **Speed graph** (distance vs. time) with ease presets: Linear, Ease-in/out, Ease-in, Ease-out, Custom Bezier. Optional constant-speed reparameterisation by arc length.
- **Aim lane**: segments such as "0–3 s look at Hero", "3–5 s look at Car", with a blend duration (rack-focus-like pan).
- **Focal / DOF lane**: zoom and rack-focus.
- Scrubbing updates both the camera gizmo in the Free view and the camera view.

### 3.6 Inspector (right panel, "Camera")
**Shot size** (preset dropdown + subject picker, move/zoom toggle, see §3.2b) · zoom slider · position, rotation, roll · focal length (mm) with lens presets (18/24/35/50/85/135) · sensor preset (Super35, Full-frame, 16:9 default) · aspect (16:9, 2.39:1, 9:16, 1:1) · aim mode and target · near/far clip · DOF (on/off, f-stop, focus target or distance) · shake (amplitude/frequency) · path summary (length m, duration s, avg speed m/s) · lock/unlock.

### 3.7 Guard rails (warnings, not blockers)
- Target out of frame (reuse `validate_frustum`, now shown live as a red target marker).
- Camera inside geometry (ray test along path, coarse sampling).
- Excessive speed/acceleration or jerk (flag whip-pan like speeds with an info icon).
- Gimbal/roll flips on steep pitch (use quaternion/up-vector approach; see 5.4).

---

## 4. Data Model

### 4.1 USD representation

All authoring goes to the **`user_edits.usda`** layer (existing edit target), under a dedicated scope so it can be edited, reset, or exported independently of other overrides:

```usda
over "World"
{
    over "ShotCam" (
        customData = {
            string painterspace_mode = "manual"      # "llm" | "manual" | "hybrid"
            string painterspace_rig  = "/World/CameraRig/Rig_01"
        }
    ) {
        # Final evaluated camera transform is *baked* here every frame for the renderer
        # (see 4.3).
    }

    def Scope "CameraRig"
    {
        def Xform "Rig_01" (
            customData = { int schemaVersion = 1 }
        ) {
            # Position path
            def BasisCurves "PositionPath" {
                uniform token type   = "cubic"
                uniform token basis  = "catmullRom"      # or "bezier"
                int[] curveVertexCounts = [N]
                point3f[] points = [...]
                # Per-point authoring data (primvars, uniform interpolation: vertex)
                float[] primvars:ps:time   = [...]       # seconds (optional; else from timing curve)
                float[] primvars:ps:focal  = [...]       # mm
                float[] primvars:ps:roll   = [...]       # degrees
            }
            def BasisCurves "ElevationPath" {...}        # optional separate curve

            # Timing
            float ps:path_start_s    = 0.0
            float ps:path_end_s      = 6.0
            token ps:ease            = "easeInOut"
            float2[] ps:speed_curve  = [(0,0),(0.2,0.1),...]   # normalized t -> normalized arc

            # Aim
            token ps:aim_mode        = "lookAt"          # free | lookAt | frame | pathTangent
            rel   ps:aim_targets     = [</World/Hero>]
            string ps:aim_bone       = "Head"            # optional UsdSkel joint
            float3 ps:aim_offset     = (0, 0.1, 0)
            float2[] ps:aim_schedule = [...]             # (time, target_index) segments
            float  ps:aim_blend_s    = 0.5
            float  ps:aim_smoothing  = 0.2               # damping for smooth tracking

            # Lens / DOF
            float ps:focal_start = 35, ps:focal_end = 35
            bool  ps:dof_enabled = false
            float ps:fstop = 2.8
            rel   ps:focus_target
            float ps:shake_amp = 0, ps:shake_freq = 2
        }
    }
}
```

Rationale:
- **`BasisCurves`** displays natively in Hydra (we get path rendering in Storm for free) and in other USD tools.
- Authoring params (`ps:*` / `primvars:ps:*`) hold *intent*; they are small and diff-friendly.
- A **JSON mirror** `camera_rig.json` per shot is *not* needed: USD is the single source of truth (consistent with README).

### 4.2 Evaluation: intent → baked camera
The renderers (Storm viewport, `renderOpenUsdStage`, any external `usdrecord`) only understand `UsdGeomCamera` time samples. So:

```
Rig intent (curves + aim + lens)  ──evaluate(t)──►  /World/ShotCam time samples
```

- A **`CameraRigEvaluator`** computes position, orientation, and lens at each frame `f ∈ [start, end]`.
- Written as `xformOp:translate.timeSamples`, `xformOp:orient` (preferred, quaternion; see 5.4) or `rotateXYZ.timeSamples`, `focalLength.timeSamples`, `focusDistance.timeSamples`, `fStop`.
- Re-baked automatically (debounced ~100 ms) whenever the rig or targeted actors change (actors moving ⇒ look-at result changes).
- Baked samples live in `user_edits.usda` as an `over` of `ShotCam`, so they override the LLM-baked samples in `shot.usda`.
- The baked block is marked `customData.painterspace_baked = true` and is treated as **derived data** (never hand-edited; regenerated).

### 4.3 Why bake rather than evaluate in the renderer
- Zero change to Hydra/`usdrecord`/ffmpeg paths; works with any USD consumer.
- Deterministic replay (matches the README's byte-identical replay principle): same rig + same actor animation ⇒ same samples.

### 4.4 Interaction with the LLM planner
| Scenario | Behaviour |
|---|---|
| Mode = Prompt | Rig ignored; `shot.usda` camera used (no `painterspace_mode` override). |
| Mode = Manual | Planner still runs for actors/animation, but its camera output is **skipped or kept as reference only**; the user's baked camera in `user_edits.usda` wins by layer strength. |
| Mode = Hybrid | "Convert LLM camera → rig": sample `/World/ShotCam` from `shot.usda`, fit a spline (RDP + Catmull-Rom), derive aim target from `plan.camera` if present, and write the rig. |
| Regeneration (actors change) | Rig preserved. Look-at targets are semantic so they follow actor motion. Warn if a referenced prim vanished (reuse the "orphaned edits" badge). |
| Prompt text mentions camera while Manual | Planner receives a flag `camera_locked: true`, and `plan.camera` is not synthesised (`compiler.py` ~L281 gains a guard); frustum validation still runs against the manual camera and reports warnings. |

Optional later: LLM prompt like "dolly in on Hero then pan to Car" → generates a **rig** (not baked samples) using semantic refs, so the user can fine-tune.

---

## 5. Architecture & Implementation

### 5.1 New components

| Component | Language | Responsibility |
|---|---|---|
| `CameraRig` (data) + `CameraRigIO` | C++ (`src/CameraRig.{h,cpp}`) | Load/save the rig USD prims in `user_edits.usda`; undo/redo integration. |
| `CameraRigEvaluator` | C++ (core) | Spline sampling (arc-length param), easing, aim solve, lens interpolation, shake; produces per-frame transforms. |
| `CameraToolController` | C++ (`src/CameraTools.{h,cpp}`) | Tool state machine (Aim, Look-At, Draw, Edit Path, Fly); handles viewport input events. |
| `CameraOverlay` | C++ | Draws frustum, path curve, handles, target markers, safe frames in QPainter overlay (and/or Hydra curve prim for the path itself). |
| `CameraInspectorPanel`, `CameraTimelineLane` | C++/Qt | Inspector and timeline widgets. |
| `camera_rig.py` (mirror evaluator) | Python (`tools/shot_library/camera_rig.py`) | Same evaluation used by headless compile/CLI/tests, and by the LLM compiler to validate. |

> [!WARNING]
> Two evaluators (C++ + Python) risk drift. Mitigation: define the math once as a **spec with golden test vectors** (JSON of rig → expected samples) and test both. Alternative if the Qt app already embeds Python/USD for the heavy work: single Python evaluator invoked via the existing bridge. **Decision needed in Phase 0 spike** (see §9).

### 5.2 Viewport changes (`UsdViewportWidget`)
1. Extend camera state: `enum ViewMode { Free, ShotCamera }` replacing `useShotCam`; keep API `setUseShotCamera` as a shim.
2. Add `PaintingTool` entries (or a parallel `CameraTool` enum): `CameraSelect, CameraAim, CameraLookAt, CameraDrawPath, CameraEditPath, CameraFly`.
3. Route `mouse*/wheel/key` events to `CameraToolController` when a camera tool is active; otherwise existing behaviour.
4. **Picking**: reuse the existing Hydra prim picking for Look-At and surface-point path placement; add ray–plane intersection for drawing planes.
5. **Overlay**: `drawCameraOverlay(QPainter&, viewProj)` in the existing overlay chain (next to `drawSelectionAndGizmoOverlay`).
   - Frustum pyramid built from `focalLength`, apertures, and a chosen display distance.
   - Path polyline sampled from the evaluator (so what you see is what's evaluated), with points/handles; time ticks every 0.5 s; direction arrows.
   - Camera "ghosts" (small frustums) at selected keyframes/time ticks.
6. **Through-the-lens**: when `ViewMode::ShotCamera`, use `/World/ShotCam` at the current time with the aspect mask applied via `CameraUtilFraming`/conform policy (already in the includes).
7. Gizmo reuse: the Move/Rotate gizmo operates on the selected camera or on selected path control point (pose extracted from the point).
8. Undo/redo: every rig edit is a single `SdfChangeBlock` on the user-edits layer, hooking into the existing text-snapshot undo history.

### 5.3 Path math (spec)
- **Curve:** centripetal Catmull-Rom by default (no overshoot/loops), optional Bezier with handles. Closed = false in v1.
- **Arc-length parametrisation:** build a lookup table (≥ 64 samples per segment) ⇒ `s(u)`; map time → distance via ease curve → `u`.
- **Time model:** `distance(t) = L · ease((t − t0)/(t1 − t0))`, clamped (hold before/after). Per-point `time` overrides (piecewise monotone cubic Hermite → no speed reversal).
- **Aim solve:** for `lookAt`: direction `d = target(t) + offset − cameraPos(t)`; world up = +Y; orientation from `d` with roll applied around `d`. Smoothing: critically damped spring or low-pass on the target direction (`aim_smoothing`). For multi-target `frame`: reuse the frustum solver from `CameraController` to compute a bounding sphere & standoff but **do not move position** unless in "Frame" auto-dolly mode.
- **Aim schedule blending:** slerp between aim orientations across `aim_blend_s` with ease.
- **Path tangent aim:** forward = path tangent (look-ahead 0.3 s), banking optional.
- **Lens:** ease between focal values; maintain `horizontalAperture` per sensor preset. Dolly-zoom helper: given a subject distance profile, compute focal so subject size is constant (Vertigo effect).
- **Shake:** deterministic seeded Perlin noise (seed = rig id) to preserve replay determinism.

### 5.4 Orientation representation
Avoid Euler gimbal artifacts: compute orientation as quaternion, write `xformOp:orient` (USD supports) or, if a downstream tool requires Euler, convert with *continuous unwrapping* (choose the Euler triple nearest the previous frame) to prevent 360° flips in interpolation. Document the xformOpOrder (`translate`, `orient`) and confirm the existing `ShotCam` reader (`UsdViewportWidget.cpp` ~L625 and `ShotEngine`) can handle `orient`; otherwise write `rotateXYZ` with unwrap.

### 5.5 Rendering & export
- No change to `renderOpenUsdStage` besides ensuring `cameraPath` = `/World/ShotCam` and the time range spans the rig (`endTimeCode` updated if path ends after the shot).
- Thumbnail (`renderCurrentView`) uses the camera at the **current timeline frame** (or a user-pinned "thumbnail frame").
- Playback dialog / `video.mp4` flow unchanged.
- **Aspect & resolution**: render settings (16:9, 2.39:1, vertical) from the camera inspector → passed to `renderOpenUsdStage` (now hardcoded 960 width in `MainWindow.cpp` ~L2282; make configurable).

### 5.6 Persistence & versioning
- `schemaVersion` on rig prim; migration function stub.
- "Reset camera edits" button clears only `/World/CameraRig` + ShotCam override (not other user edits). The existing "Your changes" badge counts camera edits separately.
- Copy/paste rig across shots; **Camera presets library** saved under `assets/camera_presets/*.usda` (e.g., `dolly_in_slow.usda`) — presets use *relative* offsets to the target so they transfer between scenes.

---

## 6. Phased Delivery

Each phase ships something usable and is independently testable.

### Phase 0 — Spike & decisions (≈ 3–4 days)
- Verify `xformOp:orient` and time-sampled `focalLength` render correctly in the Storm viewport and `renderOpenUsdStage`.
- Decide evaluator location (C++ vs. Python bridge) — prototype spline sampling in both; measure latency for live edits.
- Confirm layer-strength behaviour: `user_edits.usda` `ShotCam` samples override `shot.usda` samples after regeneration (script test with `Usd.Stage`).
- Deliverable: short decision note appended to this doc.

### Phase 1 — Manual camera transform & through-the-lens (≈ 1–1.5 weeks)
- **World | Camera viewfinder toggle (`Tab`)** with aspect-cropped viewfinder and HUD (§3.2a).
- **Shot size presets (Wide/Medium/Close-up…) and zoom in/out** (move-camera or change-lens), applied to a picked subject (§3.2b).
- View mode switch (Free / Camera), frustum gizmo, select camera, Move/Rotate gizmo for camera, inspector (pos/rot/focal/aperture/aspect), safe-frame overlay.
- Writes **static** camera to `user_edits.usda`; static manual camera works in thumbnail and render.
- Fly mode (WASD) without recording.
- *Acceptance:* user can place and aim the camera, toggle through-the-lens, regenerate the shot (LLM) without losing the camera, and render shows the manual framing.

### Phase 2 — Aim: Look-At and Frame (≈ 1 week)
- Look-At tool (pick prim/bone), target offset, smoothing; look-at follows moving actors (bake per frame).
- Frame tool using the frustum solver (style, vantage), producing a manual starting camera.
- Live target-visibility indicator.
- *Acceptance:* camera locked on a walking character stays framed throughout the shot; moving the actor path updates the camera after re-bake.

### Phase 3 — Camera paths (≈ 2 weeks) — core of the request
- `CameraRig` data + IO; `BasisCurves` path prim; evaluator (spline, arc-length, ease, hold).
- Draw Path tool: click-to-place on surfaces with height offset; drawing plane freehand with simplify/fit; Edit Path tool (move/insert/delete points); path displayed in viewport with time ticks.
- Timeline lane: path start/end, ease preset, scrubbing.
- Presets: dolly in/out, truck, pedestal, crane arc, orbit.
- Bake to `ShotCam` time samples; debounced.
- *Acceptance:* user draws a dolly path, hits Play: the camera follows it with chosen easing; rendered frames match the preview within 1 frame; undo/redo works on every edit.

### Phase 4 — Combined aim + path, lens animation (≈ 1–1.5 weeks)
- Aim schedule lane (target segments + blends), path-tangent aim, banking/roll.
- Focal length animation, dolly-zoom helper, rack focus/DOF (needs Storm DOF support check; otherwise only emit `fStop`/`focusDistance` for external renderers and show indicator).
- Shake layer (seeded).
- Speed graph editor with custom Bezier; per-point timing.

### Phase 5 — Polish & workflow (≈ 1–1.5 weeks)
- Split view with PiP camera preview; ghost frustums; speed/accel heatmap along path.
- Fly & record (resample & simplify).
- Attach to actor (tracking shots) and follow-offset rigs.
- Preset library (save/load), copy to other shots.
- Guard rails: out-of-frame, inside-geometry, speed warnings.
- Keyboard shortcuts, tooltips, onboarding hint.

### Phase 6 — LLM ↔ manual bridge (≈ 1 week)
- "Convert LLM camera to editable rig".
- Planner flag `camera_locked`; skip camera synthesis; keep frustum validation as warnings.
- Optional: LLM generates *rig* from phrases ("slow dolly in on Hero", "orbit the car") via `camera_move` catalog functions emitting rig intent (semantic refs only — consistent with the foundational law).

### Phase 7 — Stretch
- Multi-camera shots with cuts (cut list on timeline), multiple rigs per shot.
- Export camera to `.fbx`/`.abc`-compatible USD camera for DCC tools.
- Lens kit presets with real-world distortion/vignette (post).
- Gamepad/3D-mouse input.

---

## 7. Testing Strategy

**Unit (Python, `tests/test_camera_rig.py`, runs in CI like the existing `tests/*.py`)**
- Spline: passes through control points; arc-length monotonic; total length ±0.1 %.
- Easing: `t0` → start, `t1` → end, hold before/after, monotonic.
- Aim: look-at yields forward vector hitting target within 0.01°; no roll drift; steep-pitch stability (±89°).
- Quaternion continuity: no sign flips/frame-to-frame jumps > threshold.
- Schedule blending endpoints and monotone blend.
- Determinism: same rig ⇒ byte-identical baked USDA (shake seeded).
- Layering: after regenerating `shot.usda`, the manual camera is intact; reset restores LLM camera.
- Golden vectors shared with C++ evaluator (if two evaluators).

**C++ / integration**
- **Viewfinder parity:** the viewfinder image, the thumbnail and the rendered frame at the same timecode are pixel-identical in framing (same aspect crop). Compare against a reference within a small tolerance.
- **Shot size presets:** for each preset, the subject's projected height lands in the expected frame-occupancy band, for both the *move camera* and *change lens* modes, on a human and on a car.
- **Zoom:** focal length clamps to range, and zoom animation interpolates monotonically.
- Qt test harness (or scripted viewport): create rig → bake → compare `UsdGeomCamera` samples to golden.
- Render a 24-frame clip along a path; assert subject centroid stays within a tolerance of the frame center for look-at (reuse `validate_frustum`).
- Undo/redo stress: 50 random rig edits and undo all ⇒ original layer text.

**Manual QA checklist**
- Draw path at different drawing planes/height offsets; edit points; delete a point mid-path.
- Actor moves after look-at assigned ⇒ camera follows after re-bake.
- Delete the targeted actor ⇒ warning, no crash, camera holds last aim.
- Very short and very long paths; path with a cusp; near-vertical crane.
- Aspect changes ⇒ framing overlay and render agree.
- Save/reload project ⇒ identical camera.

**Performance targets**
- Re-bake 500 frames < 50 ms (C++) / < 250 ms (Python bridge); UI stays at 60 fps while dragging a control point (bake debounced, overlay uses direct evaluator preview).

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 2D mouse → 3D path ambiguity | Frustrating drawing | Multiple input methods (surface pick, drawing plane, two-view, fly-record, presets); always show ground-projected shadow line + height stakes. |
| Evaluator drift between C++ and Python | Preview ≠ render | Single spec + golden vectors; or one evaluator via bridge. |
| Look-at re-bake is stale after actor edits | Camera not on target | Bake dirty-flag on any actor transform/animation change; status chip "Camera out of date → Re-bake". |
| Euler/gimbal artifacts | Camera flips | Quaternion pipeline; unwrap if Euler needed. |
| Storm viewport lacks DOF | Can't preview rack focus | Show focus plane indicator; document as external-renderer-only for v1. |
| Manual camera clobbered by LLM regeneration | Lost work | Layer strength + `camera_locked` flag + regression test (`tests/test_regression_issues.py`). |
| Large baked time samples bloat `user_edits.usda` | File size / slow diffs | Bake only on save/render; keep live preview in memory; or write sparse samples + Catmull-Rom interpolation (`xformOp` with `Linear` interpolation) at 1 sample/frame only for the active path range. |
| Heavy overlay in QPainter for long curves | Frame drops | Cache polyline; or push path into Hydra as `BasisCurves` prim. |

---

## 9. Open Questions (need your input)

1. **Evaluator location:** OK to implement the evaluator in C++ in the app (fast live preview) with a Python twin for headless/CLI tests, or do you prefer a single Python evaluator through a bridge?
2. **Multi-camera/cuts per shot:** is one camera per shot enough for v1 (assumed), given that the storyboard is already one shot per panel?
3. **DOF/rack-focus:** is it a requirement for the render, or optional? (Hydra Storm doesn't do true DOF; this may need a different render delegate.)
4. **Default output aspect/resolution:** currently 960 px wide, fixed; should this become a per-project render setting?
5. **Hybrid mode:** should LLM prompts that mention camera work in Manual mode be ignored (assumed) or produce a *suggested* rig the user can accept?

---

## 10. Suggested File Changes (summary)

| File | Change |
|---|---|
| `src/UsdViewportWidget.{h,cpp}` | View mode, camera tools routing, camera overlay, picking helpers, `ShotCam` through-lens with aspect mask. |
| `src/CameraRig.{h,cpp}` *(new)* | Rig data, USD IO, evaluator, bake. |
| `src/CameraTools.{h,cpp}` *(new)* | Tool state machine (aim, look-at, draw, edit path, fly/record). |
| `src/MainWindow.{h,cpp}` | Mode switch (Prompt / Director / Hybrid), camera inspector & timeline lane docking, configurable render resolution/aspect. |
| `src/ShotEngine.{h,cpp}` | Pass aspect/size to `renderOpenUsdStage`; extend end frame to cover rig. |
| `tools/shot_library/camera_rig.py` *(new)* | Python evaluator, LLM→rig fitting, validators. |
| `tools/shot_library/compiler.py` | `camera_locked` guard around camera synthesis (~L281); validation against manual camera. |
| `tools/shot_library/plan_model.py` | Optional `camera_locked` field. |
| `tests/test_camera_rig.py` *(new)* | Unit & layering tests above. |
| `README.md`, `docs/LLM_SHOT_PLANNER.md` | Document Director mode and layering rules. |

**Total estimate:** ~8–10 weeks for Phases 0–6 for one engineer; Phases 1–3 (≈ 4 weeks) already deliver the headline capability (manual camera + drawn dolly paths).

