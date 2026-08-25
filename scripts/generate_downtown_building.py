#!/usr/bin/env python3
"""Generate a detailed, self-contained OBJ/MTL downtown high-rise asset."""
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "assets"
OBJ = OUT / "downtown_art_deco_tower.obj"
MTL = OUT / "downtown_art_deco_tower.mtl"


class Mesh:
    def __init__(self):
        self.v = []
        self.f = []
        self.mat = "default"

    def vertex(self, p):
        self.v.append(tuple(round(float(x), 4) for x in p))
        return len(self.v)

    def face(self, ids):
        self.f.append((self.mat, ids))

    def use(self, mat):
        self.mat = mat

    def box(self, cx, cy, cz, sx, sy, sz, mat, bevel=0):
        # Chamfering is represented by a compact inset-ring prism.
        if bevel:
            b = min(bevel, sx / 2 - .001, sy / 2 - .001, sz / 2 - .001)
            rings = []
            for z, q in ((cz-sz/2, b), (cz-sz/2+b, 0), (cz+sz/2-b, 0), (cz+sz/2, b)):
                rings.append([(cx-sx/2+q, cy-sy/2+q, z), (cx+sx/2-q, cy-sy/2+q, z),
                              (cx+sx/2-q, cy+sy/2-q, z), (cx-sx/2+q, cy+sy/2-q, z)])
            ids = [self.vertex(p) for ring in rings for p in ring]
            self.use(mat)
            self.face([ids[0], ids[1], ids[2], ids[3]])
            self.face([ids[12], ids[15], ids[14], ids[13]])
            for a, c in ((0, 1), (1, 2), (2, 3), (3, 0)):
                self.face([ids[a], ids[c], ids[4+c], ids[4+a]])
                self.face([ids[4+a], ids[4+c], ids[8+c], ids[8+a]])
                self.face([ids[8+a], ids[8+c], ids[12+c], ids[12+a]])
            return
        x0, x1, y0, y1, z0, z1 = cx-sx/2, cx+sx/2, cy-sy/2, cy+sy/2, cz-sz/2, cz+sz/2
        ids = [self.vertex(p) for p in ((x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                                        (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1))]
        self.use(mat)
        for q in ([0,1,2,3], [4,7,6,5], [0,4,5,1], [1,5,6,2], [2,6,7,3], [4,0,3,7]):
            self.face([ids[i] for i in q])

    def write(self):
        with OBJ.open("w", encoding="utf8") as out:
            out.write("# Downtown Art Deco Tower — generated architectural asset\n# Units: meters; Z is up; front faces -Y.\nmtllib downtown_art_deco_tower.mtl\n")
            out.writelines(f"v {x} {y} {z}\n" for x, y, z in self.v)
            current = None
            for mat, ids in self.f:
                if mat != current:
                    out.write(f"\nusemtl {mat}\n")
                    current = mat
                out.write("f " + " ".join(map(str, ids)) + "\n")


def window(m, x, y, z, w, h, front=True, lit=False):
    glass = "window_warm" if lit else "window_blue"
    m.box(x, y, z, w, .10, h, glass)
    yy = y - .08 if front else y + .08
    for xx in (x-w/2-.10, x+w/2+.10):
        m.box(xx, yy, z, .20, .18, h+.18, "bronze_trim")
    for zz in (z-h/2-.10, z+h/2+.10):
        m.box(x, yy, zz, w+.35, .18, .20, "bronze_trim")
    m.box(x, yy-(.07 if front else -.07), z, .08, .20, h+.02, "mullion")


def side_window(m, x, y, z, w, h, right=False, lit=False):
    glass = "window_warm" if lit else "window_blue"
    m.box(x, y, z, .10, w, h, glass)
    xx = x + (.08 if right else -.08)
    for yy in (y-w/2-.10, y+w/2+.10):
        m.box(xx, yy, z, .18, .20, h+.18, "bronze_trim")
    for zz in (z-h/2-.10, z+h/2+.10):
        m.box(xx, y, zz, .18, w+.35, .20, "bronze_trim")
    m.box(xx+(.07 if right else -.07), y, z, .20, .08, h+.02, "mullion")


def fire_escape(m, x, y, z):
    w, d = 5.5, 1.7
    m.box(x, y, z, w, d, .22, "fire_escape")
    for xx in (x-w/2, x+w/2):
        m.box(xx, y, z+.82, .15, d, 1.65, "fire_escape")
    for yy in (y-d/2, y+d/2):
        m.box(x, yy, z+1.65, w, .15, .15, "fire_escape")
    for i in range(1, 5):
        m.box(x-w/2+i*w/5, y, z+.82, .10, .10, 1.35, "fire_escape")
    m.box(x+w/2+.9, y, z-2, .16, .16, 3.95, "fire_escape")
    for i in range(6):
        m.box(x+w/2+.9, y, z-3.65+i*.62, 1.18, .12, .12, "fire_escape")


def build():
    m = Mesh()
    # Street pad, curb, and sidewalk.
    m.box(0, 0, -.35, 76, 70, .7, "asphalt")
    m.box(0, 0, .10, 53, 43, .20, "sidewalk")
    for x, y, sx, sy in ((0,-21.8,53,.38),(0,21.8,53,.38),(-26.5,0,.38,44),(26.5,0,.38,44)):
        m.box(x, y, .45, sx, sy, .55, "curb")
    # Podium, tower, setbacks, and stepped crown.
    m.box(0,0,11.5,43.5,32.5,22.8,"limestone",.25)
    m.box(0,0,23.2,39.5,29,2.2,"bronze_trim",.15)
    m.box(0,0,25.5,36.5,26.5,3,"brick",.15)
    m.box(0,0,82,35.5,25.5,110,"brick",.18)
    m.box(0,0,139,32.5,23.5,4.2,"limestone",.18)
    m.box(0,0,158,28.5,21,34,"brick",.18)
    m.box(0,0,176,24.5,18.5,3,"limestone",.14)
    m.box(0,0,180,18.5,14,5,"brick",.10)
    m.box(0,0,184,12.5,9.5,3,"limestone",.08)
    # Piers and ribs.
    for z0,z1,hx,hy in ((1,24,21.6,16.1),(27,137,17.7,12.7),(141,174,14.2,10.6),(177,181.5,12.2,9.2)):
        for x in (-hx,hx):
            for y in (-hy,hy): m.box(x,y,(z0+z1)/2,.95,.95,z1-z0,"limestone",.12)
        for x in (-hx+4.2,hx-4.2):
            m.box(x,-hy-.17,(z0+z1)/2,.38,.34,z1-z0,"limestone")
            m.box(x,hy+.17,(z0+z1)/2,.38,.34,z1-z0,"limestone")
    # Double-height podium storefronts.
    for x in (-17,-11.3,-5.6,5.6,11.3,17):
        m.box(x,-16.42,8.1,.34,.22,14.5,"limestone")
        window(m,x,-16.58,9,4.7,12.7,True,x in (-11.3,5.6))
        m.box(x,16.42,8.1,.34,.22,14.5,"limestone")
        window(m,x,16.58,9,4.7,12.7,False,x in (-5.6,11.3))
    for y in (-11.5,-5.8,5.8,11.5):
        side_window(m,-21.9,y,9,4.7,12.7,False,y in (-5.8,5.8))
        side_window(m,21.9,y,9,4.7,12.7,True,y in (-11.5,11.5))
    # 42 individually framed levels on all sides of the main shaft.
    for floor in range(42):
        z = 29.18 + floor*2.62
        m.box(0,-12.92,z-1.25,34.2,.22,.20,"limestone")
        m.box(0,12.92,z-1.25,34.2,.22,.20,"limestone")
        m.box(-17.92,0,z-1.25,.22,24.2,.20,"limestone")
        m.box(17.92,0,z-1.25,.22,24.2,.20,"limestone")
        lit = floor*7%19 in (2,5,8,13)
        for x in (-13.8,-9.2,-4.6,0,4.6,9.2,13.8):
            window(m,x,-12.98,z,3.75,1.72,True,lit and x in (-9.2,4.6))
            window(m,x,12.98,z,3.75,1.72,False,lit and x in (0,9.2))
        for y in (-9.2,-4.6,0,4.6,9.2):
            side_window(m,-17.98,y,z,3.75,1.72,False,lit and y in (-4.6,4.6))
            side_window(m,17.98,y,z,3.75,1.72,True,lit and y in (0,9.2))
    for z,y in ((41,-13.8),(57,13.8),(73,-13.8),(89,13.8),(105,-13.8),(121,13.8)):
        fire_escape(m,7,y,z)
    # Grand entrance with canopy, doors, marquee, and flag poles.
    m.box(0,-16.9,4.25,14,3.8,.42,"bronze_trim",.08)
    m.box(0,-18.25,5.05,12.5,.22,1.25,"glass_blue")
    for x in (-5.5,5.5): m.box(x,-18.3,2.65,.72,2.7,4.6,"limestone",.12)
    for x in (-3.3,-1.1,1.1,3.3):
        m.box(x,-16.62,4.8,1.65,.15,4,"glass_blue")
        m.box(x-.86,-16.73,4.8,.10,.25,4.1,"bronze_trim")
        m.box(x+.86,-16.73,4.8,.10,.25,4.1,"bronze_trim")
    m.box(0,-18.42,6.05,10.8,.30,.62,"sign_gold")
    for x in (-7.3,7.3):
        m.box(x,-18,8.3,.18,.18,6.4,"bronze_trim")
        m.box(x,-18,11.55,1.6,.18,.08,"sign_gold")
        m.box(x,-18,11.15,.08,.18,.8,"sign_gold")
    # Rooftop penthouse, mechanical units, water tank, and antenna.
    m.box(-2.8,0,188.5,8,6,4,"roof_equipment",.15)
    for x in (-5.4,-3.3,-1.2,.9,3): m.box(x,-3.2,191,1.2,.7,1.4,"metal")
    m.box(4.3,1.5,189,3.8,3.8,3.8,"roof_equipment",.12)
    for x in (3.3,5.3):
        for y in (.4,2.5): m.box(x,y,191.3,.9,.9,1,"metal")
    for x in (-4.7,-2.2):
        for y in (3.2,6): m.box(x,y,190.5,.28,.28,7,"water_tank_wood")
    m.box(-3.45,4.6,187,4.1,4,.25,"water_tank_wood")
    m.box(-3.45,4.6,194.4,4.4,4,7,"water_tank")
    m.box(-3.45,4.6,198.15,4,3.55,.25,"water_tank_wood")
    for y in (2.52,6.68): m.box(-3.45,y,194.4,4.7,.18,6.5,"water_tank_wood")
    m.box(0,0,199,.28,.28,13,"antenna")
    m.box(0,0,205.6,4,.16,.16,"antenna")
    m.box(0,0,202.2,.16,4,.16,"antenna")
    for y in (-10,10): m.box(0,y,186.7,25,.45,1.1,"limestone")
    for x in range(-11,12,2):
        for y in (-10.15,10.15): m.box(x,y,188,.55,.55,1.2,"limestone",.08)
    # Scale anchors: lamps, planters, trees, and bollards.
    for x in (-22,22):
        for y in (-18,18):
            m.box(x,y,3.1,.22,.22,5.2,"street_lamp")
            m.box(x,y-(.9 if y<0 else -.9),5.5,.9,.12,.12,"street_lamp")
            m.box(x,y-(1.35 if y<0 else -1.35),5.85,.6,.6,.25,"street_lamp")
    for x in (-19,-15,15,19):
        m.box(x,-20,1.1,1.8,1.1,1,"planter")
        m.box(x,-20,3.1,.22,.22,3.2,"tree_trunk")
        m.box(x,-20,5,2.7,2.7,2.4,"tree_leaf")
    for x in (-23.5,-21.5,-19.5,19.5,21.5,23.5): m.box(x,-20.8,1,.22,.22,1.15,"bollard")
    OUT.mkdir(exist_ok=True)
    m.write()
    mats = {
        "asphalt":(.045,.05,.055,0,8),"sidewalk":(.38,.37,.34,0,12),"curb":(.24,.24,.23,0,15),
        "brick":(.28,.095,.055,0,18),"limestone":(.57,.49,.35,0,20),"bronze_trim":(.20,.12,.055,0,70),
        "mullion":(.08,.075,.06,0,90),"window_blue":(.018,.075,.12,0,100),"window_warm":(.95,.48,.12,0,70),
        "glass_blue":(.04,.24,.31,.18,90),"sign_gold":(.86,.53,.12,0,110),"fire_escape":(.07,.075,.068,0,45),
        "roof_equipment":(.10,.11,.115,0,35),"metal":(.29,.31,.30,0,95),"water_tank":(.40,.22,.10,0,28),
        "water_tank_wood":(.18,.075,.025,0,22),"antenna":(.45,.46,.43,0,110),"street_lamp":(.055,.06,.065,0,60),
        "planter":(.12,.13,.12,0,20),"tree_trunk":(.18,.07,.025,0,15),"tree_leaf":(.035,.14,.055,0,12),"bollard":(.36,.36,.32,0,60),
    }
    with MTL.open("w", encoding="utf8") as out:
        out.write("# Materials for downtown_art_deco_tower.obj\n")
        for name,(r,g,b,alpha,ns) in mats.items():
            out.write(f"\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .35 .35 .35\nNs {ns}\n")
            if alpha: out.write(f"d {1-alpha:.3f}\nTr {alpha:.3f}\nillum 4\n")
            else: out.write("illum 2\n")
    print(f"wrote {OBJ} ({len(m.v):,} vertices, {len(m.f):,} faces)")
    print(f"wrote {MTL}")


if __name__ == "__main__":
    build()
