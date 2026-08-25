#!/usr/bin/env python3
"""Generate a large shorefront harbour with piers, cranes, terminals, and horizon water."""
from pathlib import Path
import math

import generate_residential_neighborhood as neighborhood_generator

ROOT = Path(__file__).resolve().parents[1]
OBJ = ROOT / "assets/grand_harbour.obj"
MTL = ROOT / "assets/grand_harbour.mtl"
neighborhood_generator.OBJ = OBJ
Mesh = neighborhood_generator.Mesh


def ocean(m):
    # The water occupies the far half of the asset so it reads as open horizon,
    # rather than a small decorative pond around the docks.
    cols, rows = 54, 42
    x0, x1 = -150.0, 150.0
    y0, y1 = 30.0, 180.0
    for row in range(rows + 1):
        y = y0 + (y1 - y0) * row / rows
        for col in range(cols + 1):
            x = x0 + (x1 - x0) * col / cols
            wave = (0.18 * math.sin(x * .09 + y * .025) +
                    0.10 * math.sin(x * .19 - y * .07) +
                    0.06 * math.cos(x * .04 + y * .12))
            m.vertex((x, y, 0.18 + wave))
    stride = cols + 1
    for row in range(rows):
        for col in range(cols):
            a = row * stride + col
            b, c, d = a + 1, a + stride, a + stride + 1
            crest = sum(m.v[i][2] for i in (a, b, d)) / 3.0
            mat = "ocean_highlight" if crest > .34 else ("ocean_mid" if crest > .18 else "ocean_deep")
            m.f.append((mat, (a + 1, b + 1, d + 1)))
            crest = sum(m.v[i][2] for i in (a, d, c)) / 3.0
            mat = "ocean_highlight" if crest > .34 else ("ocean_mid" if crest > .18 else "ocean_deep")
            m.f.append((mat, (a + 1, d + 1, c + 1)))


def window(m, x, y, z, w=2.0, h=2.0, lit=False):
    m.box((x, y, z), (w, .10, h), "window_warm" if lit else "window_blue")
    m.box((x - w/2 - .10, y - .08, z), (.18, .16, h + .18), "bronze_trim")
    m.box((x + w/2 + .10, y - .08, z), (.18, .16, h + .18), "bronze_trim")
    m.box((x, y - .08, z - h/2 - .10), (w + .35, .16, .18), "bronze_trim")
    m.box((x, y - .08, z + h/2 + .10), (w + .35, .16, .18), "bronze_trim")
    m.box((x, y - .17, z), (.07, .20, h), "mullion")


def crane(m, x, y):
    # Portal crane with tall legs, trolley beam, hook, and diagonal braces.
    for xx in (x - 5.0, x + 5.0):
        m.box((xx, y, 13.5), (1.1, 2.0, 27.0), "crane")
        m.box((xx, y, 1.2), (3.0, 4.0, .6), "crane")
    m.box((x, y, 27.0), (13.0, 2.0, 1.1), "crane")
    m.box((x, y - 13.0, 27.0), (2.0, 26.0, .55), "crane")
    m.box((x, y - 12.0, 17.5), (.45, .45, 18.0), "crane")
    m.box((x, y - 12.0, 8.2), (4.0, 4.0, .45), "metal")
    m.box((x, y - 12.0, 4.0), (.13, .13, 7.5), "metal")


def lamp(m, x, y):
    m.box((x, y, 3.5), (.20, .20, 6.0), "street_lamp")
    m.box((x, y - 1.0, 6.3), (1.2, .14, .14), "street_lamp")
    m.box((x, y - 1.55, 6.55), (.55, .55, .24), "sign_gold")


def build():
    m = Mesh()
    # Shore land, promenade, and sea wall.
    m.box((0, -20, -.35), (300, 100, .7), "asphalt")
    m.box((0, 18, .08), (300, 12, .18), "sidewalk")
    m.box((0, 27.0, .60), (300, 1.1, 1.35), "concrete")
    m.box((0, 27.8, 1.35), (300, .20, 1.0), "limestone")
    for x in range(-145, 146, 18):
        lamp(m, x, 19)
        m.box((x, 24.9, 1.1), (.32, .32, 1.0), "metal")
    # Ocean on the far side, extending to the horizon.
    ocean(m)

    # Grand terminal with glass concourse, ticket hall, and roof signage.
    m.box((-48, 6, 9.2), (64, 25, 18.0), "concrete", .25)
    m.box((-48, -6.75, 9.0), (54, .24, 15.8), "glass_blue")
    for x in range(-70, -24, 7): window(m, x, -6.93, 9.5, 5.2, 12.6, (x + 70) % 14 == 0)
    for x in (-78, -18): m.box((x, -6.9, 8.4), (1.0, .30, 16.8), "limestone")
    m.box((-48, -7.2, 18.8), (39, .50, 1.1), "sign_gold")
    m.box((-48, 18.6, 18.5), (64, .50, 1.2), "limestone")
    for x in range(-76, -19, 8): m.box((x, 18.2, 21.0), (.60, .60, 3.2), "metal")

    # Long warehouses and a maritime control tower along the shore.
    for x, width, height, mat in ((30, 48, 11, "brick"), (83, 38, 14, "concrete"), (-102, 42, 10, "brick")):
        m.box((x, 6, height / 2 + .6), (width, 28, height), mat, .18)
        for wx in range(int(x - width/2 + 5), int(x + width/2 - 3), 7):
            window(m, wx, -8.15, height * .58, 4.0, height * .36, (wx + int(x)) % 3 == 0)
        m.box((x, 20.2, height + .8), (width + 1.2, .42, .35), "limestone")
    # Tower with glazed observation room and antenna.
    m.box((119, 6, 17), (7, 7, 34), "limestone", .12)
    m.box((119, 2.35, 27), (6.0, .18, 5.0), "glass_blue")
    m.box((119, 6, 35), (11, 11, 2.5), "concrete", .10)
    m.box((119, 6, 45), (.35, .35, 18), "antenna")
    m.box((119, 6, 53), (4.0, .18, .18), "antenna")

    # Four piers with fenders, rails, lamps, bollards, and container stacks.
    for pier_x in (-105, -40, 28, 96):
        m.box((pier_x, 74, 1.0), (16, 86, 1.5), "concrete", .12)
        for side in (-1, 1):
            m.box((pier_x + side * 7.7, 74, 2.2), (.18, 84, 1.9), "metal")
            for y in range(36, 113, 14): m.box((pier_x + side * 7.9, y, 1.0), (.55, .75, .80), "wood")
        for y in range(39, 112, 16):
            m.box((pier_x - 5.0, y, 2.0), (5.5, 9.0, 2.0), "container_red" if y % 32 else "container_blue")
            m.box((pier_x + 3.0, y + 4.5, 3.3), (5.0, 8.0, 2.2), "container_blue" if y % 32 else "container_red")
            m.box((pier_x - 7.2, y, 3.0), (.35, .35, 1.2), "metal")
        for y in range(40, 112, 18): lamp(m, pier_x - 7.0, y)
        crane(m, pier_x, 46)

    # Ferry slips, gangways, bollards, and a small terminal canopy.
    m.box((-7, 47, 1.0), (32, 3.8, 1.2), "wood")
    m.box((-7, 31, 2.1), (20, 2.2, 3.2), "metal")
    m.box((-7, 31, 4.0), (23, .18, .18), "metal")
    for x in (-19, -11, -3, 5): m.box((x, 45, 1.9), (.30, .30, 1.0), "metal")
    for x in range(-136, 137, 16): m.box((x, 29.8, 1.0), (.35, .35, 1.0), "metal")

    # Lighthouse at the outer breakwater: beacon room, roof, and balcony.
    m.box((137, 37, 8), (5, 5, 16), "limestone", .12)
    m.box((137, 37, 17), (7.5, 7.5, .35), "metal")
    m.box((137, 37, 18.5), (5.5, 5.5, 2.2), "glass_blue", .12)
    m.box((137, 37, 20.1), (6.8, 6.8, .35), "sign_gold")
    m.box((137, 37, 24), (.30, .30, 7.5), "antenna")
    for x in (134.5, 139.5): m.box((x, 34.5, 17.7), (.12, 5, 2.2), "metal")
    for y in (34.5, 39.5): m.box((137, y, 17.7), (5, .12, 2.2), "metal")

    ROOT.joinpath("assets").mkdir(exist_ok=True)
    m.write()
    materials = {
        "asphalt": (.045,.05,.055,8), "sidewalk": (.38,.37,.34,12), "concrete": (.30,.32,.32,24),
        "limestone": (.57,.49,.35,20), "brick": (.31,.12,.075,18), "bronze_trim": (.20,.12,.055,70),
        "mullion": (.08,.075,.06,90), "window_blue": (.018,.075,.12,100), "window_warm": (.95,.48,.12,70),
        "glass_blue": (.04,.24,.31,90), "metal": (.29,.31,.30,95), "crane": (.82,.46,.08,80),
        "wood": (.22,.11,.045,22), "container_red": (.54,.10,.055,30), "container_blue": (.04,.20,.34,45),
        "ocean_deep": (.015,.10,.17,80), "ocean_mid": (.02,.22,.31,95), "ocean_highlight": (.10,.40,.46,120),
        "street_lamp": (.055,.06,.065,60), "sign_gold": (.86,.53,.12,110), "antenna": (.45,.46,.43,110)
    }
    with MTL.open("w") as out:
        out.write("# Materials for grand_harbour.obj\n")
        for name, (r,g,b,ns) in materials.items():
            out.write(f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .38 .38 .38\nNs {ns}\nillum 2\n")
    print(f"wrote {OBJ} ({len(m.v):,} vertices, {len(m.f):,} faces)")
    print(f"wrote {MTL}")


if __name__ == "__main__":
    build()
