#!/usr/bin/env python3
"""Convert PainterSpace architectural/environment OBJ+MTL assets into Y-up OpenUSD (.usdc) assets."""

from __future__ import annotations

from pathlib import Path
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "assets"
OUT_DIR = ASSETS_DIR / "usdc_assets" / "environments"

ASSETS = [
    "downtown_art_deco_tower",
    "residential_neighborhood",
    "residential_intersection",
    "residential_avenue",
    "ocean_horizon",
    "grand_harbour",
]


def parse_mtl(mtl_path: Path) -> dict[str, tuple[float, float, float]]:
    colors: dict[str, tuple[float, float, float]] = {}
    current = None
    if not mtl_path.is_file():
        return colors
    for raw in mtl_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "newmtl" and len(parts) >= 2:
            current = parts[1]
        elif parts[0] == "Kd" and current and len(parts) >= 4:
            colors[current] = (float(parts[1]), float(parts[2]), float(parts[3]))
    return colors


def convert_obj_to_usdc(stem: str) -> Path:
    obj_path = ASSETS_DIR / f"{stem}.obj"
    mtl_path = ASSETS_DIR / f"{stem}.mtl"
    out_path = OUT_DIR / f"{stem}.usdc"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mtl_colors = parse_mtl(mtl_path)
    default_color = (0.35, 0.35, 0.35)

    points: list[Gf.Vec3f] = []
    face_vertex_counts: list[int] = []
    face_vertex_indices: list[int] = []
    face_colors: list[Gf.Vec3f] = []
    current_mat = "default"

    min_pt = [float("inf"), float("inf"), float("inf")]
    max_pt = [float("-inf"), float("-inf"), float("-inf")]

    for raw in obj_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        cmd = parts[0]
        if cmd == "v" and len(parts) >= 4:
            # Source OBJs have Z-up; map (x, y_depth, z_up) -> (x, z_up, y_depth) to match PaintingCanvas Y-up
            x = float(parts[1])
            raw_y = float(parts[3])
            y = max(-0.02, raw_y) if stem in {
                "downtown_art_deco_tower",
                "residential_neighborhood",
                "residential_intersection",
                "residential_avenue",
            } else raw_y
            z = float(parts[2])
            points.append(Gf.Vec3f(x, y, z))
            if x < min_pt[0]: min_pt[0] = x
            if y < min_pt[1]: min_pt[1] = y
            if z < min_pt[2]: min_pt[2] = z
            if x > max_pt[0]: max_pt[0] = x
            if y > max_pt[1]: max_pt[1] = y
            if z > max_pt[2]: max_pt[2] = z
        elif cmd == "usemtl" and len(parts) >= 2:
            current_mat = parts[1]
        elif cmd == "f" and len(parts) >= 4:
            idxs = [int(tok.split("/")[0]) - 1 for tok in parts[1:]]
            # Reverse winding because swapping Y and Z flips handedness
            idxs.reverse()
            face_vertex_counts.append(len(idxs))
            face_vertex_indices.extend(idxs)
            c = mtl_colors.get(current_mat, default_color)
            face_colors.append(Gf.Vec3f(*c))

    if out_path.exists():
        out_path.unlink()

    stage = Usd.Stage.CreateNew(str(out_path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    root = UsdGeom.Xform.Define(stage, Sdf.Path("/root"))
    stage.SetDefaultPrim(root.GetPrim())

    mesh = UsdGeom.Mesh.Define(stage, Sdf.Path("/root/Geometry"))
    mesh.CreatePointsAttr(Vt.Vec3fArray(points))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(face_vertex_counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(face_vertex_indices))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*min_pt), Gf.Vec3f(*max_pt)]))

    display_color = mesh.CreateDisplayColorPrimvar(UsdGeom.Tokens.uniform)
    display_color.Set(Vt.Vec3fArray(face_colors))

    stage.GetRootLayer().Save()
    size = [round(max_pt[i] - min_pt[i], 2) for i in range(3)]
    print(f"Wrote {out_path.relative_to(ROOT)} ({len(points)} pts, {len(face_vertex_counts)} faces, size={size}m)")
    return out_path


def main() -> None:
    for stem in ASSETS:
        convert_obj_to_usdc(stem)


if __name__ == "__main__":
    main()
