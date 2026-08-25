#!/usr/bin/env python3
"""Create a compact, detailed row of NYC brownstones and apartment buildings."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBJ = ROOT / "assets/residential_neighborhood.obj"
MTL = ROOT / "assets/residential_neighborhood.mtl"


class Mesh:
    def __init__(self):
        self.v, self.f, self.mat = [], [], "brick"

    def use(self, mat): self.mat = mat
    def box(self, c, size, mat, bevel=0):
        x,y,z = c; sx,sy,sz = size
        if bevel:
            b = min(bevel, sx*.45, sy*.45, sz*.45)
            rings = []
            for zz,q in ((z-sz/2,b),(z-sz/2+b,0),(z+sz/2-b,0),(z+sz/2,b)):
                rings.append([(x-sx/2+q,y-sy/2+q,zz),(x+sx/2-q,y-sy/2+q,zz),
                              (x+sx/2-q,y+sy/2-q,zz),(x-sx/2+q,y+sy/2-q,zz)])
            ids = [self.vertex(p) for r in rings for p in r]
            self.use(mat)
            self.f.append((self.mat,[ids[0],ids[1],ids[2],ids[3]]))
            self.f.append((self.mat,[ids[12],ids[15],ids[14],ids[13]]))
            for a,c in ((0,1),(1,2),(2,3),(3,0)):
                self.f.append((self.mat,[ids[a],ids[c],ids[4+c],ids[4+a]]))
                self.f.append((self.mat,[ids[4+a],ids[4+c],ids[8+c],ids[8+a]]))
                self.f.append((self.mat,[ids[8+a],ids[8+c],ids[12+c],ids[12+a]]))
            return
        x0,x1,y0,y1,z0,z1=x-sx/2,x+sx/2,y-sy/2,y+sy/2,z-sz/2,z+sz/2
        ids=[self.vertex(p) for p in ((x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
                                      (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1))]
        self.use(mat)
        for q in ((0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(4,0,3,7)):
            self.f.append((self.mat,[ids[i] for i in q]))

    def vertex(self,p): self.v.append(tuple(round(float(q),4) for q in p)); return len(self.v)
    def write(self):
        with OBJ.open('w') as out:
            out.write('# Residential neighborhood — generated NYC brownstone and apartment cluster\n# Units: meters; Z is up; front faces -Y.\nmtllib residential_neighborhood.mtl\n')
            out.writelines(f'v {x} {y} {z}\n' for x,y,z in self.v)
            current=None
            for mat,ids in self.f:
                if mat!=current: out.write(f'\nusemtl {mat}\n'); current=mat
                out.write('f '+' '.join(map(str,ids))+'\n')


def front_window(m,x,y,z,w=1.15,h=1.55,lit=False):
    m.box((x,y,z),(w,.10,h),'window_warm' if lit else 'window_blue')
    for xx in (x-w/2-.08,x+w/2+.08): m.box((xx,y-.08,z),(.14,.17,h+.14),'bronze_trim')
    for zz in (z-h/2-.08,z+h/2+.08): m.box((x,y-.08,zz),(w+.30,.17,.14),'bronze_trim')
    m.box((x,y-.17,z),(.07,.20,h),'mullion')


def side_window(m,x,y,z,w=1.15,h=1.55,lit=False,right=False):
    m.box((x,y,z),(.10,w,h),'window_warm' if lit else 'window_blue')
    for yy in (y-w/2-.08,y+w/2+.08): m.box((x+(.08 if right else -.08),yy,z),(.17,.14,h+.14),'bronze_trim')
    m.box((x+(.17 if right else -.17),y,z),(.20,.07,h),'mullion')


def fire_escape(m,x,y,z,w=5.2):
    m.box((x,y,z),(w,1.5,.18),'fire_escape')
    for xx in (x-w/2,x+w/2): m.box((xx,y,z+.82),(.12,1.5,1.65),'fire_escape')
    m.box((x,y-0.75,z+1.65),(w,.12,.12),'fire_escape')
    for i in range(1,5): m.box((x-w/2+i*w/5,y,z+.82),(.08,.08,1.35),'fire_escape')
    m.box((x+w/2+.75,y,z-1.9),(.13,.13,3.8),'fire_escape')
    for i in range(6): m.box((x+w/2+.75,y,z-3.5+i*.60),(1.0,.10,.10),'fire_escape')


def brownstone(m,x,y,w,d,floors,mat='brick',fire=True):
    h=floors*2.75
    m.box((x,y,.6+h/2),(w,d,h),mat,.12)
    # Brownstone facade banding and cornice.
    for floor in range(floors):
        z=1.35+floor*2.75
        m.box((x,y-d/2-.08,z-1.05),(w+.08,.18,.16),'limestone')
        for xx in (x-w*.30,x,x+w*.30): front_window(m,xx,y-d/2-.15,z,1.28,1.62,(floor+int(x))%7 in (1,4) and xx==x)
        for yy in (y-d*.28,y,y+d*.28): side_window(m,x+w/2+.08,yy,z,1.18,1.62,floor%6==2,True)
    m.box((x,y-d/2-.12,h+1.25),(w+.5,.32,.32),'limestone',.08)
    # stoop, railings, and entry door
    for i in range(3): m.box((x,y-d/2-.72-i*.42,.22+i*.28),(2.0,.55,.28),'limestone',.04)
    m.box((x,y-d/2-.20,1.48),(1.10,.12,2.05),'door')
    for xx in (x-.82,x+.82):
        m.box((xx,y-d/2-.70,1.45),(.10,.10,2.3),'bronze_trim')
        m.box((xx,y-d/2-.70,2.55),(.9,.10,.10),'bronze_trim')
    if fire:
        for floor in range(1,floors,2): fire_escape(m,x+w*.18,y+d/2+.45,1.8+floor*2.75,w*.62)
    # tank and small rooftop utility box
    if floors>=7:
        m.box((x+w*.18,y+.1,h+3.0),(3.2,3.0,4.8),'water_tank')
        m.box((x+w*.18,y+.1,h+5.5),(3.5,3.3,.18),'water_tank_wood')
        for xx in (x+w*.18-1.3,x+w*.18+1.3): m.box((xx,y+.1,h+.7),(.18,.18,4.5),'water_tank_wood')


def apartment(m,x,y,w,d,floors,mat):
    h=floors*2.9
    m.box((x,y,.6+h/2),(w,d,h),mat,.18)
    for floor in range(floors):
        z=1.45+floor*2.9
        m.box((x,y-d/2-.10,z-1.12),(w+.18,.18,.18),'limestone')
        for xx in (x-w*.36,x-w*.12,x+w*.12,x+w*.36): front_window(m,xx,y-d/2-.16,z,1.5,1.75,(floor+int(xx*3))%9 in (2,6) and xx>x)
        for yy in (y-d*.30,y-d*.10,y+d*.10,y+d*.30): side_window(m,x+w/2+.10,yy,z,1.5,1.75,floor%8==3,True)
    for xoff in (-w*.34,w*.34):
        m.box((x+xoff,y-d/2-.25,2.0),(2.5,.18,3.1),'glass_blue')
        m.box((x+xoff,y-d/2-.37,3.65),(3.0,.25,.22),'bronze_trim')
    m.box((x,y-d/2-.16,h+1.1),(w+.6,.36,.35),'limestone',.08)
    if floors>10:
        m.box((x,y,h+3.0),(w*.35,d*.32,4.2),'roof_equipment',.10)
        m.box((x+w*.28,y+d*.10,h+5.0),(2.4,2.4,4.8),'water_tank')


def build():
    m=Mesh()
    m.box((0,0,-.30),(82,60,.6),'asphalt')
    m.box((0,-2,.08),(76,38,.18),'sidewalk')
    m.box((0,-21.4,.42),(78,.38,.55),'curb')
    # A mix of narrow historic rowhouses and taller infill apartments.
    brownstone(m,-30,-4,8.4,15,7,'brick',True)
    brownstone(m,-20.5,-4,8.4,15,8,'limestone',True)
    brownstone(m,-11,-4,8.4,15,6,'brick',True)
    brownstone(m,-1.5,-4,8.4,15,9,'brick',True)
    apartment(m,10.5,0,13.5,19,13,'limestone')
    apartment(m,26.5,0,15.0,20,16,'brick')
    # A lower corner house turns the block and adds visual variation.
    brownstone(m,-31,13,13,9,5,'limestone',False)
    # Street trees, lamps, hydrants and planters.
    for x in (-34,-24,-14,-4,7,18,31):
        m.box((x,-20,1.0),(1.5,1.0,1.0),'planter')
        m.box((x,-20,3.1),(.20,.20,3.4),'tree_trunk')
        m.box((x,-20,5.2),(2.5,2.5,2.5),'tree_leaf')
    for x in (-39,39):
        m.box((x,-18,3.2),(.20,.20,5.4),'street_lamp')
        m.box((x,-18.8,5.8),(1.0,.12,.12),'street_lamp')
    for x in (-36,-28,-18, -8, 4, 16, 28, 36): m.box((x,-20.8,1.0),(.22,.22,1.2),'bollard')
    (ROOT / "assets").mkdir(exist_ok=True)
    m.write()
    mats={
      'asphalt':(.045,.05,.055,8),'sidewalk':(.38,.37,.34,12),'curb':(.24,.24,.23,15),
      'brick':(.31,.12,.075,18),'limestone':(.57,.49,.35,20),'bronze_trim':(.20,.12,.055,70),
      'mullion':(.08,.075,.06,90),'window_blue':(.018,.075,.12,100),'window_warm':(.95,.48,.12,70),
      'glass_blue':(.04,.24,.31,90),'fire_escape':(.07,.075,.068,45),'door':(.12,.055,.03,35),
      'water_tank':(.40,.22,.10,28),'water_tank_wood':(.18,.075,.025,22),'roof_equipment':(.10,.11,.115,35),
      'street_lamp':(.055,.06,.065,60),'planter':(.12,.13,.12,20),'tree_trunk':(.18,.07,.025,15),
      'tree_leaf':(.035,.14,.055,12),'bollard':(.36,.36,.32,60)
    }
    with MTL.open('w') as out:
        out.write('# Materials for residential_neighborhood.obj\n')
        for name,(r,g,b,ns) in mats.items():
            out.write(f'\nnewmtl {name}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa .02 .02 .02\nKs .35 .35 .35\nNs {ns}\nillum 2\n')
    print(f'wrote {OBJ} ({len(m.v):,} vertices, {len(m.f):,} faces)')
    print(f'wrote {MTL}')


if __name__=='__main__': build()
