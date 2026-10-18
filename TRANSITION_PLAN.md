# PainterSpace: Transition to an OpenUSD + Hydra 3D Editor

## 1. Architecture & Layer Stack

PainterSpace's 3D editor (`UsdViewportWidget`) renders and edits a live composed OpenUSD stage directly with **Hydra Storm (`UsdImagingGLEngine` / `HgiMetal`)**:

1. **Session / Top Override Layer (`user_edits.usda`)**:
   - All manual edits made in the viewport (transform overrides, visibility toggles, added assets, deleted prims, and `/World/Paint` strokes) are authored exclusively into `user_edits.usda` via `UsdEditTarget`.
   - Never mutates the LLM-generated `shot.usda`.
   - Survives LLM regeneration by default and can be toggled (`Your changes` checkbox in the viewport header) or reset per-prim / per-shot.
   - Detects and warns if a manual override references a prim path that no longer exists after regeneration.
2. **Shot Layer (`shot.usda`)**:
   - Authored by the LLM shot generator (`ShotEngine`), containing shot-specific actors, `UsdSkel` animations, and `/World/ShotCam`.
3. **Scene Base Layer (`scene.usda`)**:
   - Sublayered into `shot.usda` (`subLayers = [@../scene.usda@]`), holding the shared environment, buildings, roads, and lighting.

## 2. OpenUSD C++ SDK Setup

PainterSpace links directly against macOS's built-in OpenUSD runtime (`/usr/lib/usd/libusd_ms.dylib` via the Xcode Command Line Tools SDK stub `${CMAKE_OSX_SYSROOT}/usr/lib/usd/libusd_ms.tbd`, providing `UsdImagingGL`, `HdStormRendererPlugin` / `HgiMetal`, `UsdSkel`, and `UsdSkelImaging` with zero external app or Python dependencies) and stages C++ headers in `third_party/openusd/include/pxr`.

To populate or refresh the C++ headers on a new machine:
```bash
./scripts/setup_openusd_sdk.sh
```

## 3. CLI Verification Commands

- **Automated Phase Exit Tests**:
  ```bash
  ./build/PainterSpace --usd-test-suite
  ```
- **Single-Stage Hydra Render Spike**:
  ```bash
  ./build/PainterSpace --usd-spike scenes/scene_01/shot_001/shot.usda /tmp/shot_001_hydra.png 12
  ```

## 4. Phase Status

| Phase | Description | Status | Exit Test Result |
| :--- | :--- | :--- | :--- |
| **Phase 0** | Spike & decision gate (`UsdImagingGLEngine` + `HdStorm` Metal in Qt) | **Complete** | `running_fast.usdc` animated `UsdSkel` renders at ~213 FPS (4.68 ms/frame) and animates across frames. |
| **Phase 1** | Read-only USD viewer (composed stage, `ShotCam` vs `Free Orbit Camera`, timeline scrubber, prim tree, grid overlay) | **Complete** | Opens and renders `shot_001`, `shot_002`, and `shot_003` via Hydra Storm matching `usdrecord`. |
| **Phase 2** | Live reload (`QFileSystemWatcher` + post-LLM reload preserving camera & frame; error banner on validation failure) | **Complete** | Stage reloads in-place without restarting the app; failed generations preserve the previous stage. |
| **Phase 3** | Selection & basic editing (Hydra `TestIntersection` picking, `user_edits.usda` edit target, Move/Rotate/Scale gizmo, Undo/Redo, asset rail authoring, orphan warnings) | **Complete** | Moving `/World/Car` writes to `user_edits.usda`, leaves `shot.usda` byte-identical, and persists across stage reload. |
| **Phase 6** | Ground painting (`UsdGeomBasisCurves` strokes under `/World/Paint` in `user_edits.usda` + `world_state.py` summary) | **Complete** | Brush and eraser author/remove `BasisCurves` prims in `user_edits.usda` and summarize in `world_state.py`. |
| **Phase 8** | Storyboard integration (`shot.usda` + `user_edits.usda` per panel, Hydra thumbnail capture, legacy state cleared) | **Complete** | Panels open their composed USD stage directly and save Hydra-rendered thumbnails. |
| **Phase 9** | Removal of legacy OpenGL 3D editor (`PaintingCanvas`, raw GL pipeline, bone poser, toggle flags) | **Complete** | `UsdViewportWidget` is the sole 3D editor; legacy canvas, GL widget dependencies, and dual paths removed. |

