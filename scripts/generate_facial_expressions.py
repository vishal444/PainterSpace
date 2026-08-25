#!/usr/bin/env python3
"""Generate a small, consistent library of stylized 3D facial-expression busts.

The output is intentionally simple OBJ/MTL geometry so PainterSpace can load it
without a third-party model importer. OBJ uses Z-up coordinates; PainterSpace's
OBJ loader already maps those coordinates into its Y-up scene.
"""
from __future__ import annotations

import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "facial_expressions"


class Mesh:
    def __init__(self) -> None:
        self.vertices: list[tuple[float, float, float]] = []
        self.faces: list[tuple[str, list[int]]] = []

    def vertex(self, p: tuple[float, float, float]) -> int:
        self.vertices.append(tuple(round(float(value), 5) for value in p))
        return len(self.vertices)

    def face(self, material: str, indices: list[int]) -> None:
        self.faces.append((material, indices))

    def uv_sphere(
        self,
        center: tuple[float, float, float],
        radii: tuple[float, float, float],
        material: str,
        segments: int = 16,
        rings: int = 8,
    ) -> None:
        cx, cy, cz = center
        rx, ry, rz = radii
        rows: list[list[int]] = []
        for ring in range(rings + 1):
            phi = -math.pi / 2 + math.pi * ring / rings
            row = []
            for segment in range(segments):
                theta = 2 * math.pi * segment / segments
                row.append(self.vertex((
                    cx + rx * math.cos(phi) * math.cos(theta),
                    cy + ry * math.cos(phi) * math.sin(theta),
                    cz + rz * math.sin(phi),
                )))
            rows.append(row)
        for ring in range(rings):
            for segment in range(segments):
                a = rows[ring][segment]
                b = rows[ring][(segment + 1) % segments]
                c = rows[ring + 1][(segment + 1) % segments]
                d = rows[ring + 1][segment]
                self.face(material, [a, b, c, d])

    def dome(
        self,
        center: tuple[float, float, float],
        radii: tuple[float, float, float],
        material: str,
        segments: int = 20,
        rings: int = 5,
    ) -> None:
        """A top-half sphere for hair, with the open edge hidden in the head."""
        cx, cy, cz = center
        rx, ry, rz = radii
        rows: list[list[int]] = []
        for ring in range(rings + 1):
            phi = math.pi / 2 - math.pi / 2 * ring / rings
            row = []
            for segment in range(segments):
                theta = 2 * math.pi * segment / segments
                row.append(self.vertex((
                    cx + rx * math.cos(phi) * math.cos(theta),
                    cy + ry * math.cos(phi) * math.sin(theta),
                    cz + rz * math.sin(phi),
                )))
            rows.append(row)
        for ring in range(rings):
            for segment in range(segments):
                a = rows[ring][segment]
                b = rows[ring][(segment + 1) % segments]
                c = rows[ring + 1][(segment + 1) % segments]
                d = rows[ring + 1][segment]
                self.face(material, [a, b, c, d])

    def cylinder_between(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        radius: float,
        material: str,
        segments: int = 8,
    ) -> None:
        sx, sy, sz = start
        ex, ey, ez = end
        direction = (ex - sx, ey - sy, ez - sz)
        length = math.sqrt(sum(component * component for component in direction))
        if length < 1e-6:
            return
        direction = tuple(component / length for component in direction)
        reference = (0.0, 0.0, 1.0) if abs(direction[2]) < 0.9 else (0.0, 1.0, 0.0)
        side = (
            direction[1] * reference[2] - direction[2] * reference[1],
            direction[2] * reference[0] - direction[0] * reference[2],
            direction[0] * reference[1] - direction[1] * reference[0],
        )
        side_length = math.sqrt(sum(component * component for component in side))
        side = tuple(component / side_length for component in side)
        up = (
            direction[1] * side[2] - direction[2] * side[1],
            direction[2] * side[0] - direction[0] * side[2],
            direction[0] * side[1] - direction[1] * side[0],
        )
        start_ring: list[int] = []
        end_ring: list[int] = []
        for index in range(segments):
            angle = 2 * math.pi * index / segments
            offset = tuple(radius * (math.cos(angle) * side[i] + math.sin(angle) * up[i]) for i in range(3))
            start_ring.append(self.vertex(tuple(start[i] + offset[i] for i in range(3))))
            end_ring.append(self.vertex(tuple(end[i] + offset[i] for i in range(3))))
        for index in range(segments):
            self.face(material, [start_ring[index], start_ring[(index + 1) % segments],
                                 end_ring[(index + 1) % segments], end_ring[index]])

    def arc_tube(
        self,
        points: list[tuple[float, float, float]],
        radius: float,
        material: str,
    ) -> None:
        for start, end in zip(points, points[1:]):
            self.cylinder_between(start, end, radius, material, 7)
        for point in points:
            self.uv_sphere(point, (radius, radius, radius), material, 8, 4)

    def write(self, stem: str, materials: dict[str, tuple[float, float, float, int]]) -> None:
        obj_path = OUT / f"{stem}.obj"
        mtl_path = OUT / f"{stem}.mtl"
        with obj_path.open("w", encoding="utf-8") as obj:
            obj.write(f"# PainterSpace facial expression: {stem}\n# Units: meters; Z is up; face points toward -Y.\n")
            obj.write(f"mtllib {stem}.mtl\n")
            obj.writelines(f"v {x} {y} {z}\n" for x, y, z in self.vertices)
            current = None
            for material, indices in self.faces:
                if material != current:
                    obj.write(f"\nusemtl {material}\n")
                    current = material
                obj.write("f " + " ".join(map(str, indices)) + "\n")
        with mtl_path.open("w", encoding="utf-8") as mtl:
            mtl.write(f"# Materials for PainterSpace facial expression: {stem}\n")
            for name, (r, g, b, shininess) in materials.items():
                mtl.write(
                    f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\n"
                    f"Ka .025 .025 .025\nKs .32 .32 .32\nNs {shininess}\nillum 2\n"
                )


MATERIALS = {
    "skin": (0.66, 0.30, 0.22, 28),
    "skin_light": (0.82, 0.43, 0.31, 34),
    "skin_shadow": (0.48, 0.17, 0.12, 24),
    "hair": (0.055, 0.035, 0.028, 48),
    "eye_white": (0.93, 0.90, 0.80, 65),
    "eye_dark": (0.018, 0.012, 0.011, 90),
    "mouth_dark": (0.12, 0.012, 0.012, 28),
    "teeth": (0.98, 0.86, 0.62, 45),
    "tongue": (0.78, 0.15, 0.16, 32),
    "shirt": (0.055, 0.20, 0.28, 24),
}


EXPRESSIONS = {
    "happy": {
        "eye": (0.22, 0.12, 0.19), "pupil": (0.085, 0.065, 0.105), "brow": "soft",
        "mouth": "smile", "mouth_size": 0.40, "note": "warm open smile",
    },
    "sad": {
        "eye": (0.22, 0.12, 0.18), "pupil": (0.08, 0.06, 0.095), "brow": "sad",
        "mouth": "frown", "mouth_size": 0.36, "note": "downturned mouth and inner-raised brows",
    },
    "scared": {
        "eye": (0.28, 0.14, 0.27), "pupil": (0.065, 0.055, 0.08), "brow": "high",
        "mouth": "oval", "mouth_size": 0.27, "note": "wide eyes and small open mouth",
    },
    "grumpy": {
        "eye": (0.24, 0.12, 0.10), "pupil": (0.075, 0.06, 0.06), "brow": "down",
        "mouth": "flat", "mouth_size": 0.34, "note": "lowered brows and narrowed eyes",
    },
    "terrified": {
        "eye": (0.31, 0.15, 0.29), "pupil": (0.055, 0.05, 0.065), "brow": "high",
        "mouth": "oval", "mouth_size": 0.44, "note": "extreme alarm with large open mouth",
    },
    "ecstatic": {
        "eye": (0.22, 0.11, 0.07), "pupil": (0.065, 0.055, 0.04), "brow": "soft",
        "mouth": "grin", "mouth_size": 0.48, "note": "closed happy eyes and huge toothy grin",
    },
    "angry": {
        "eye": (0.24, 0.12, 0.095), "pupil": (0.075, 0.06, 0.06), "brow": "down",
        "mouth": "tight", "mouth_size": 0.31, "note": "furrowed brow and tight lips",
    },
    "surprised": {
        "eye": (0.27, 0.14, 0.25), "pupil": (0.07, 0.055, 0.085), "brow": "high",
        "mouth": "oval", "mouth_size": 0.22, "note": "round eyes and startled O mouth",
    },
}


def facial_bust(expression: str) -> Mesh:
    spec = EXPRESSIONS[expression]
    mesh = Mesh()

    # Bust silhouette.
    mesh.uv_sphere((0.0, 0.02, 0.52), (1.38, 0.68, 0.56), "shirt", 18, 7)
    mesh.cylinder_between((0.0, 0.0, 0.70), (0.0, 0.0, 1.48), 0.31, "skin_shadow", 12)
    mesh.uv_sphere((0.0, 0.0, 2.55), (1.08, 0.84, 1.16), "skin", 20, 10)
    mesh.uv_sphere((-1.03, 0.0, 2.55), (0.22, 0.16, 0.30), "skin_light", 12, 6)
    mesh.uv_sphere((1.03, 0.0, 2.55), (0.22, 0.16, 0.30), "skin_light", 12, 6)
    mesh.dome((0.0, 0.04, 3.06), (1.11, 0.86, 0.92), "hair", 20, 6)

    # Eye whites and pupils are placed toward -Y, the front of the model.
    eye_z = 2.76
    eye_y = -0.72
    for side in (-1, 1):
        ex = side * 0.43
        eye_radii = spec["eye"]
        mesh.uv_sphere((ex, eye_y, eye_z), eye_radii, "eye_white", 14, 7)
        pupil = spec["pupil"]
        mesh.uv_sphere((ex, eye_y - eye_radii[1] * 0.92, eye_z), pupil, "eye_dark", 12, 6)

    # Brows are angled as a pair of short, readable facial strokes.
    brow_y = -0.91
    brow_z = {"soft": 3.12, "sad": 3.10, "high": 3.22, "down": 3.10}[spec["brow"]]
    for side in (-1, 1):
        if spec["brow"] == "sad":
            inner_z, outer_z = brow_z + 0.11, brow_z - 0.03
        elif spec["brow"] == "down":
            inner_z, outer_z = brow_z - 0.15, brow_z + 0.02
        elif spec["brow"] == "high":
            inner_z, outer_z = brow_z, brow_z + 0.04
        else:
            inner_z, outer_z = brow_z + 0.04, brow_z
        inner = (side * 0.17, brow_y, inner_z)
        outer = (side * 0.69, brow_y, outer_z)
        mesh.arc_tube([outer, inner], 0.075 if spec["brow"] != "high" else 0.06, "hair")

    # Nose bridge and rounded tip.
    mesh.uv_sphere((0.0, -0.79, 2.49), (0.13, 0.20, 0.34), "skin_light", 12, 6)
    mesh.uv_sphere((0.0, -0.88, 2.35), (0.18, 0.19, 0.16), "skin_light", 12, 6)

    mouth_center = 2.17
    width = spec["mouth_size"]
    if spec["mouth"] in ("smile", "frown", "flat", "tight"):
        points = []
        for index in range(7):
            x = -width + 2 * width * index / 6
            if spec["mouth"] == "smile":
                z = mouth_center + 0.20 * (abs(x) / width) ** 2
            elif spec["mouth"] == "frown":
                z = mouth_center + 0.18 * (1 - (x / width) ** 2)
            elif spec["mouth"] == "tight":
                z = mouth_center + 0.02 * (1 - (x / width) ** 2)
            else:
                z = mouth_center
            points.append((x, -0.86, z))
        mesh.arc_tube(points, 0.055 if spec["mouth"] != "tight" else 0.075, "mouth_dark")
    else:
        height = 0.27 if spec["mouth"] == "grin" else width * 0.76
        mesh.uv_sphere((0.0, -0.86, mouth_center), (width, 0.065, height), "mouth_dark", 16, 8)
        if spec["mouth"] == "grin":
            mesh.uv_sphere((0.0, -0.925, mouth_center + 0.10), (width * 0.80, 0.03, 0.095), "teeth", 14, 6)
            mesh.uv_sphere((0.0, -0.925, mouth_center - 0.13), (width * 0.66, 0.028, 0.07), "tongue", 14, 6)
        elif expression == "terrified":
            mesh.uv_sphere((0.0, -0.925, mouth_center + 0.14), (width * 0.72, 0.03, 0.065), "teeth", 14, 6)

    return mesh


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for expression in EXPRESSIONS:
        facial_bust(expression).write(expression, MATERIALS)
    manifest = OUT / "README.md"
    with manifest.open("w", encoding="utf-8") as readme:
        readme.write("# 3D facial-expression assets\n\n")
        readme.write("Low-poly, front-facing human busts for PainterSpace. Each expression is a standalone OBJ/MTL pair.\n\n")
        readme.write("| Asset | Expression cue |\n|---|---|\n")
        for name, spec in EXPRESSIONS.items():
            readme.write(f"| `{name}.obj` | {spec['note']} |\n")
        readme.write("\nCoordinate system: Z-up OBJ, face toward -Y, units in meters.\n")
    print(f"wrote {len(EXPRESSIONS)} facial-expression OBJ/MTL pairs to {OUT}")


if __name__ == "__main__":
    main()
