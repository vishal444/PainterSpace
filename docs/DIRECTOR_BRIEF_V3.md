# Director’s Brief v3

Director intent is now connected from the desktop editor through planning,
constraint solving, baking, validation, and independent camera rendering.

## Editor workflow

- Choose **Auto**, **Assisted**, or **Manual** actor generation. Manual runs the
  compiler without making an LLM request. Camera mode is independent.
- Add per-actor segments using catalog verbs or manifest clips. Choose a target
  from the list or use **Pick target** and click the viewport. Add a style note,
  choose locked/preferred/open, and optionally set explicit timing flexibility.
  Zero flexibility means exact time windows at every lock level.
- Select a segment and use **Draw segment path** for its own route. Routes are
  stored under `/World/Paths/<id>` as guide curves. Selecting the segment selects
  the guide. Multiple segments on the same actor can have independent paths.
  Constant and gait-bounded ease-in/out pacing are available. Scrubbing draws
  actor ghosts without changing the stage.
- Open gaps appear explicitly. The digest gives implicit gaps stable IDs such as
  `gap_before_<segment>` so the planner can fill them symbolically. Path-only
  briefs are also valid and compile directly.
- **What the AI will see · Check brief** shows the actual qualitative digest and
  preflight lint. Click a diagnostic to seek or choose an explicit repair: route
  around, fit path length or window, switch action, trim overlap, or widen corners.
  Bad paths are highlighted in red. Templates cover approach/talk/leave and
  chase/hide. Undo/redo includes paths, segment metadata, and camera sidecars.

## Camera workflow

The existing viewfinder, drawing tools, lens/framing presets and camera inspector
now save the canonical `camera_rig.json`. Regeneration preserves that file.

- **Cmd/Ctrl+Shift+C** adds the free camera pose as a waypoint.
- **Camera keys** edits waypoint position/height, optional arrival time, dwell,
  lens keys, focus windows and joints, free orientation keys, normalized pacing,
  aim smoothing, lead room, roll, seeded shake and explicit camera speed.
- The camera lane is labelled **render only**. Dragging its window changes camera
  timing without changing the actor brief.
- **Save camera take** creates a named camera. The take selector switches the active
  camera. **Render all takes** writes a sequence and video for each take and opens
  a comparison dialog with a keep button.
- **Re-render camera** rebakes against composed actor motion, including viewport
  overrides, then renders without planning, motion synthesis, actor baking, or
  collision revalidation. Actor-input changes block this route until generation.
- Desktop previews use the same Python camera baker after a debounce. A preview
  never updates actor fingerprints or clears a stale actor state.

The baker reads baked skeleton joint transforms for Head, Chest, Hips, Hands or an
explicit joint name. Multiple focus targets use their centroid. It supports
centripetal Catmull-Rom arc-length pacing, custom speed keys, timed waypoints and
dwell, damped look-at, tangent/fixed/keyed aim, continuous quaternions, focal keys,
dolly zoom, depth-of-field attributes and deterministic shake. Camera diagnostics
cover missing targets/joints, cropping/frustum, near-clip/solid intersections,
speed/acceleration, aim lag, extreme orientation and focal range. They are grouped
into seekable time ranges and remain warnings.

`/World/Cameras/<id>` and sibling `cam_<id>.usda` layers are separate from the AI
ShotCam. `render_stage.usda` composes cameras, user edits and actors. The render
resolver uses the active manual camera first. Guide curves are excluded from
renders. Cuts between cameras remain outside the single-camera-at-a-time v1 scope.

## Compiler and planner guarantees

- The editor persists `director_brief.json` beside `shot.json`; legacy `actions.json`
  remains readable. Regeneration does not rewrite either authored brief or camera
  document. Camera-only baking leaves `shot.usda`, `shot_plan.json`, and the brief
  byte-identical.
- Numeric geometry, clocks, distances, angles, camera documents, raw USDA and raw
  world state stay out of planner payloads. The queue and live endpoint use the
  same projected context, qualitative digest and symbolic call guard. Manual
  camera mode removes all camera verbs from the schema. Numeric retry diagnostics
  stay local; the planner receives qualitative recovery advice.
- Locked segments bypass the LLM. Preferred segments permit discrete clip/target
  choices; variant menus come from the manifest or explicit alternatives. Style
  notes cannot change timing or geometry. `during` binds actions to user segments;
  `after="actor.segment"` refers to their compiled completion events.
- The timing solver propagates lower/upper lane bounds, choosing feasible starts
  nearest the requested times. Only an explicit `flexible` flag enables the
  authored tolerance. Locked neighboring windows cannot be silently displaced.
- Contacts use manifest reach metadata. The solver creates an obstacle-routed
  approach in an available gap and holds the target-relative standoff throughout
  the interaction. An impossible approach or a conflict with a drawn successor
  path fails with structured feedback instead of teleporting an actor.
- Locomotion path playback is stride matched. Gait-bounded easing varies speed
  smoothly while retaining the selected action's speed band, including window
  endpoints. Variable stride clocks integrate playback speed without resetting
  the clip each frame. Crossfades finish at the new action boundary.
- Conformance checks synthesized motion and then independently reads composed USD
  root transforms, speeds, and the baker's actual source-clip samples at 48 Hz.
  Animation source identity is read from bake metadata, not inferred from poses.
  Skeleton samples include half frames and exact authored boundaries, including
  windows that start between render frames.
  Collision validation and the render gate remain separate checks.
- A failed symbolic compile cannot fall back to raw LLM-authored USD. In-process
  regeneration reloads cached USD layers safely; repeated inputs produce identical
  actor and camera output.

## Headless commands

```sh
.venv/bin/python tools/shot_library/compile_shot.py --out <shot.usda> --brief-info
.venv/bin/python tools/shot_library/compile_shot.py --out <shot.usda> --manual --prompt '<prompt>'
.venv/bin/python tools/shot_library/compile_shot.py --out <shot.usda> --status --prompt '<current prompt>'
.venv/bin/python tools/shot_library/compile_shot.py --out <shot.usda> --camera-only
```

`--brief-fix <operation> --segment-id <id> --path-id <id>` returns a repaired
brief for explicit application; it does not write author files. `--camera-preview`
bakes derived camera files without updating fingerprints. Camera-only output
reports the render composition, active camera, diagnostics and all take paths.

## Validation

```sh
cmake --build build -j 4
PYTHONPATH=.venv/lib/python3.12/site-packages python3 -m pytest tests tools/test_bake_animation.py -q
./build/PainterSpace --usd-brief-test
.venv/bin/python tools/evaluate_director_brief.py --output /tmp/director-eval.json
```

The Qt harness uses an isolated temporary shot, synthetic mouse strokes and lane
edits. It checks path and camera JSON/layer undo/redo, including byte restoration.
The 81 Python tests cover projected payloads, every manual-camera verb, locking, explicit
flexibility, implicit gaps, real contact and skeleton baking, gait bands, obstacle
routing, joint focus, camera pacing, stale-state rejection, camera-only immutability,
and actor/camera replay determinism. A one-frame Hydra render verifies that the
manual-camera composition renders successfully without guides.

The evaluation runner compares actual baked path constraint satisfaction,
collision-free first pass and retry counts, with and without briefs. It runs the
compositional planner offline by default; `--live-llm` explicitly uses the configured
endpoint. The bundled two-case offline fixture reaches full authored path
satisfaction with briefs. This small regression fixture is not a general LLM
quality benchmark; live evaluations require an endpoint and representative scenes.
