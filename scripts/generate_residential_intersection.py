#!/usr/bin/env python3
"""Create a vehicle-free residential street junction with buildings and street furniture."""
from pathlib import Path

import generate_residential_neighborhood as neighborhood_generator

neighborhood_generator.OBJ = Path(__file__).resolve().parents[1] / "assets/residential_intersection.obj"
Mesh = neighborhood_generator.Mesh
apartment = neighborhood_generator.apartment
brownstone = neighborhood_generator.brownstone

ROOT = Path(__file__).resolve().parents[1]
OBJ = ROOT / "assets/residential_intersection.obj"
MTL = ROOT / "assets/residential_intersection.mtl"


def signal(m, x, y, side):
    """Traffic signal and street sign at a corner; side is the mast direction."""
    m.box((x, y, 4.0), (.22, .22, 7.2), "street_lamp")
    if side == "x":
        m.box((x + (1.5 if x < 0 else -1.5), y, 7.0), (3.0, .18, .18), "street_lamp")
        m.box((x + (2.65 if x < 0 else -2.65), y, 6.1), (.42, .55, 1.15), "metal")
    else:
        m.box((x, y + (1.5 if y < 0 else -1.5), 7.0), (.18, 3.0, .18), "street_lamp")
        m.box((x, y + (2.65 if y < 0 else -2.65), 6.1), (.55, .42, 1.15), "metal")
    m.box((x, y, 8.25), (2.2, .12, .12), "sign_gold")


def tree(m, x, y):
    m.box((x, y, 1.0), (1.45, 1.0, 1.0), "planter")
    m.box((x, y, 3.0), (.18, .18, 3.2), "tree_trunk")
    m.box((x, y, 5.0), (2.35, 2.35, 2.4), "tree_leaf")


def build():
    m = Mesh()
    # Asphalt city block with two crossing roads.
    m.box((0, 0, -.35), (180, 180, .7), "asphalt")
    m.box((0, -22, .08), (180, 10, .18), "sidewalk")
    m.box((0, 22, .08), (180, 10, .18), "sidewalk")
    m.box((-22, 0, .08), (10, 180, .18), "sidewalk")
    m.box((22, 0, .08), (10, 180, .18), "sidewalk")
    # Continuous curbs create four crisp corners and accessible ramp wedges.
    for y in (-16.8, 16.8): m.box((0, y, .42), (180, .38, .55), "curb")
    for x in (-16.8, 16.8): m.box((x, 0, .42), (.38, 180, .55), "curb")
    for x, y in ((-20,-20),(-20,20),(20,-20),(20,20)):
        m.box((x, y, .27), (4.4, 4.4, .14), "limestone")

    # Four zebra crossings, two per street, with a clear central junction.
    for x in (-14.0, 14.0):
        for i in range(7): m.box((x + (i - 3) * .62, 0, .16), (.30, 24, .06), "limestone")
    for y in (-14.0, 14.0):
        for i in range(7): m.box((0, y + (i - 3) * .62, .16), (24, .30, .06), "limestone")
    # Center lines stop before the crossing, as on a real junction.
    for x in range(-78, -19, 7): m.box((x, 0, .14), (3.4, .12, .05), "sign_gold")
    for x in range(20, 79, 7): m.box((x, 0, .14), (3.4, .12, .05), "sign_gold")
    for y in range(-78, -19, 7): m.box((0, y, .14), (.12, 3.4, .05), "sign_gold")
    for y in range(20, 79, 7): m.box((0, y, .14), (.12, 3.4, .05), "sign_gold")

    # Corner traffic signals, hydrants, and street lamps; no vehicles.
    for x, y in ((-18,-18),(-18,18),(18,-18),(18,18)):
        signal(m, x, y, "x" if abs(x) >= abs(y) else "y")
        m.box((x + (3 if x < 0 else -3), y + (3 if y < 0 else -3), 1.0), (.32, .32, 1.0), "metal")
        m.box((x + (3 if x < 0 else -3), y + (3 if y < 0 else -3), 1.75), (.50, .50, .70), "sign_gold")
    for x, y in ((-42,-21),(-14,-21),(14,-21),(42,-21),(-42,21),(-14,21),(14,21),(42,21)):
        m.box((x, y, 3.1), (.20, .20, 5.2), "street_lamp")
        m.box((x, y + (1 if y < 0 else -1), 5.6), (1.0, .12, .12), "street_lamp")

    # Residential buildings line both sides of the main avenue and side street.
    for x, floors, mat in ((-57,6,"brick"),(-46,8,"limestone"),(-35,7,"brick"),(35,7,"brick"),(46,9,"limestone"),(57,6,"brick")):
        brownstone(m, x, -34, 9.2, 15, floors, mat, True)
        brownstone(m, x, 34, 9.2, 15, floors + (1 if x in (-46,46) else 0), mat, True)
    for y, floors, mat in ((-59,7,"limestone"),(-48,6,"brick"),(48,8,"brick"),(59,6,"limestone")):
        apartment(m, -34, y, 14, 12, floors + 3, mat)
        apartment(m, 34, y, 14, 12, floors, mat)

    # Trees and planters create a walkable residential streetscape.
    for x in (-62,-49,-36,36,49,62):
        tree(m, x, -21); tree(m, x, 21)
    for y in (-62,-49,49,62):
        tree(m, -21, y); tree(m, 21, y)
    for x, y in ((-10,-20),(10,-20),(-10,20),(10,20)):
        m.box((x, y, 1.0), (1.1, .9, 1.0), "planter")

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
    with MTL.open("w") as out:
        out.write("# Materials for residential_intersection.obj\n")
        for name, (r,g,b,ns) in mats.items():
            out.write(f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .35 .35 .35\nNs {ns}\nillum 2\n")
    print(f"wrote {OBJ} ({len(m.v):,} vertices, {len(m.f):,} faces)")
    print(f"wrote {MTL}")


if __name__ == "__main__":
    build()
