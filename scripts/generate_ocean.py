#!/usr/bin/env python3
"""Generate a large low-poly ocean surface designed to run toward the horizon."""
from pathlib import Path
import math

ROOT = Path(__file__).resolve().parents[1]
OBJ = ROOT / "assets/ocean_horizon.obj"
MTL = ROOT / "assets/ocean_horizon.mtl"


def build():
    size = 300.0
    cells = 84
    vertices = []
    faces = []

    for row in range(cells + 1):
        z = (row / cells - 0.5) * size
        for col in range(cells + 1):
            x = (col / cells - 0.5) * size
            wave = (0.42 * math.sin(x * 0.105 + z * 0.018) +
                    0.22 * math.sin(x * 0.043 - z * 0.090) +
                    0.12 * math.cos(x * 0.19 + z * 0.13))
            vertices.append((x, z, 0.12 + wave))

    def material_for(a, b, c):
        crest = (vertices[a][2] + vertices[b][2] + vertices[c][2]) / 3.0
        if crest > 0.58: return "ocean_highlight"
        if crest > 0.35: return "ocean_mid"
        return "ocean_deep"

    stride = cells + 1
    for row in range(cells):
        for col in range(cells):
            a = row * stride + col
            b = a + 1
            c = a + stride
            d = c + 1
            faces.append((material_for(a, b, d), (a + 1, b + 1, d + 1)))
            faces.append((material_for(a, d, c), (a + 1, d + 1, c + 1)))

    with OBJ.open("w") as out:
        out.write("# Ocean Horizon — generated ocean surface\n# Units: meters; Z is up in source coordinates; front is -Y after import.\nmtllib ocean_horizon.mtl\n")
        out.writelines(f"v {x:.4f} {y:.4f} {z:.4f}\n" for x, y, z in vertices)
        current = None
        for material, ids in faces:
            if material != current:
                out.write(f"\nusemtl {material}\n")
                current = material
            out.write("f " + " ".join(map(str, ids)) + "\n")

    materials = {
        "ocean_deep": (0.015, 0.10, 0.17, 80),
        "ocean_mid": (0.02, 0.22, 0.31, 95),
        "ocean_highlight": (0.10, 0.40, 0.46, 120),
    }
    with MTL.open("w") as out:
        out.write("# Materials for ocean_horizon.obj\n")
        for name, (r, g, b, ns) in materials.items():
            out.write(f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .45 .45 .45\nNs {ns}\nillum 2\n")
    print(f"wrote {OBJ} ({len(vertices):,} vertices, {len(faces):,} faces)")
    print(f"wrote {MTL}")


if __name__ == "__main__":
    build()
