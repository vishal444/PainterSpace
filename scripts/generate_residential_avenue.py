#!/usr/bin/env python3
"""Create a long, vehicle-free residential avenue streetscape."""
from pathlib import Path

import generate_residential_neighborhood as neighborhood_generator

ROOT = Path(__file__).resolve().parents[1]
OUT_OBJ = ROOT / "assets/residential_avenue.obj"
OUT_MTL = ROOT / "assets/residential_avenue.mtl"
neighborhood_generator.OBJ = OUT_OBJ
Mesh = neighborhood_generator.Mesh
apartment = neighborhood_generator.apartment
brownstone = neighborhood_generator.brownstone


def tree(m, x, y):
    m.box((x, y, 1.0), (1.45, 1.0, 1.0), "planter")
    m.box((x, y, 3.0), (.18, .18, 3.2), "tree_trunk")
    m.box((x, y, 5.0), (2.35, 2.35, 2.4), "tree_leaf")


def lamp(m, x, y):
    m.box((x, y, 3.1), (.20, .20, 5.3), "street_lamp")
    m.box((x, y + (1 if y < 0 else -1), 5.6), (1.0, .12, .12), "street_lamp")


def build():
    m = Mesh()
    # 300-meter straight avenue, generous sidewalks, curbs, and an asphalt verge.
    m.box((0, 0, -.35), (310, 82, .7), "asphalt")
    m.box((0, -22, .08), (300, 10, .18), "sidewalk")
    m.box((0, 22, .08), (300, 10, .18), "sidewalk")
    m.box((0, -16.8, .42), (300, .38, .55), "curb")
    m.box((0, 16.8, .42), (300, .38, .55), "curb")

    # Center markings and repeated pedestrian crossings break up the long view.
    for x in range(-145, 146, 7):
        if -8 < x < 8: continue
        m.box((x, 0, .14), (3.4, .12, .05), "sign_gold")
    for crossing_x in (-128, -64, 0, 64, 128):
        for i in range(9):
            m.box((crossing_x + (i - 4) * .65, 0, .16), (.32, 25, .06), "limestone")
        m.box((crossing_x, -15.3, .56), (4.2, 2.3, .08), "limestone")
        m.box((crossing_x, 15.3, .56), (4.2, 2.3, .08), "limestone")

    # Varied brownstones and mid-rise apartments continue on both sides.
    positions = list(range(-132, 133, 22))
    materials = ("brick", "limestone", "brick", "brick", "limestone")
    heights = (6, 8, 7, 9, 6, 10, 7)
    for index, x in enumerate(positions):
        mat = materials[index % len(materials)]
        floors = heights[index % len(heights)]
        brownstone(m, x, -34, 16.0, 18, floors, mat, True)
        brownstone(m, x, 34, 16.0, 18, floors + (1 if index % 4 == 0 else 0), mat, True)

    # Taller anchors at the ends and midway points keep the avenue from feeling flat.
    for x, mat, floors in ((-145, "limestone", 12), (-72, "brick", 14), (72, "brick", 13), (145, "limestone", 11)):
        apartment(m, x, -34, 18, 20, floors, mat)
        apartment(m, x, 34, 18, 20, floors - 1, mat)

    # Regularly spaced street trees, lamps, planters, hydrants, and bollards.
    for x in range(-140, 141, 20):
        tree(m, x, -21); tree(m, x, 21)
        lamp(m, x - 7, -21); lamp(m, x + 7, 21)
        m.box((x + 3, -20.8, 1.0), (.22, .22, 1.2), "bollard")
        m.box((x - 3, 20.8, 1.0), (.22, .22, 1.2), "bollard")
        m.box((x + 6, -18.9, 1.05), (.32, .32, 1.1), "metal")
        m.box((x - 6, 18.9, 1.05), (.32, .32, 1.1), "metal")

    ROOT.joinpath("assets").mkdir(exist_ok=True)
    m.write()
    mats = {
        "asphalt": (.045,.05,.055,8), "sidewalk": (.38,.37,.34,12), "curb": (.24,.24,.23,15),
        "brick": (.31,.12,.075,18), "limestone": (.57,.49,.35,20), "bronze_trim": (.20,.12,.055,70),
        "mullion": (.08,.075,.06,90), "window_blue": (.018,.075,.12,100), "window_warm": (.95,.48,.12,70),
        "glass_blue": (.04,.24,.31,90), "fire_escape": (.07,.075,.068,45), "door": (.12,.055,.03,35),
        "water_tank": (.40,.22,.10,28), "water_tank_wood": (.18,.075,.025,22), "roof_equipment": (.10,.11,.115,35),
        "street_lamp": (.055,.06,.065,60), "planter": (.12,.13,.12,20), "tree_trunk": (.18,.07,.025,15),
        "tree_leaf": (.035,.14,.055,12), "bollard": (.36,.36,.32,60), "sign_gold": (.86,.53,.12,110), "metal": (.29,.31,.30,95)
    }
    with OUT_MTL.open("w") as out:
        out.write("# Materials for residential_avenue.obj\n")
        for name, (r,g,b,ns) in mats.items():
            out.write(f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .35 .35 .35\nNs {ns}\nillum 2\n")
    print(f"wrote {OUT_OBJ} ({len(m.v):,} vertices, {len(m.f):,} faces)")
    print(f"wrote {OUT_MTL}")


if __name__ == "__main__":
    build()
