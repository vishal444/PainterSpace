import bpy
import sys
from mathutils import Vector

path = next(value for value in reversed(sys.argv) if value.lower().endswith('.blend'))
bpy.ops.wm.open_mainfile(filepath=path)
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        minimum = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
        maximum = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
        print('BOUNDS', obj.name, tuple(round(value, 3) for value in minimum), tuple(round(value, 3) for value in maximum))
bpy.ops.wm.quit_blender()
