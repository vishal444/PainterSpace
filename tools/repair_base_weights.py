import bpy
import os
import sys
from mathutils import Vector


source_path = next(value for value in reversed(sys.argv) if value.lower().endswith('.blend'))
bpy.ops.wm.open_mainfile(filepath=os.path.abspath(source_path))

armature = bpy.data.objects.get('PoseRig')
body = bpy.data.objects.get('Human')
if armature is None or body is None:
    raise RuntimeError('PoseRig and Human are required')

window = next(iter(bpy.context.window_manager.windows), None)
area = next((candidate for candidate in window.screen.areas if candidate.type == 'VIEW_3D'), None) if window else None
region = next((candidate for candidate in area.regions if candidate.type == 'WINDOW'), None) if area else None
override = {'window': window, 'screen': window.screen if window else None, 'area': area, 'region': region}

with bpy.context.temp_override(**override):
    bpy.ops.object.mode_set(mode='OBJECT') if body.mode != 'OBJECT' else None
for obj in bpy.context.view_layer.objects:
    obj.select_set(False)

for modifier in list(body.modifiers):
    if modifier.type == 'ARMATURE':
        body.modifiers.remove(modifier)
for group in list(body.vertex_groups):
    body.vertex_groups.remove(group)
body.parent = None

body.select_set(True)
armature.select_set(True)
bpy.context.view_layer.objects.active = armature

try:
    with bpy.context.temp_override(**override):
        bpy.ops.object.parent_set(type='ARMATURE_AUTO')
    print('BASE_WEIGHTS', 'automatic')
except RuntimeError as error:
    print('BASE_WEIGHTS', 'automatic_failed', str(error))
    deform_bones = [bone for bone in armature.data.bones if bone.use_deform]
    for bone in deform_bones:
        body.vertex_groups.new(name=bone.name)
    for vertex in body.data.vertices:
        world_point = body.matrix_world @ vertex.co
        nearest = min(
            deform_bones,
            key=lambda bone: min(
                (world_point - (armature.matrix_world @ bone.head_local)).length,
                (world_point - (armature.matrix_world @ bone.tail_local)).length,
            ),
        )
        body.vertex_groups[nearest.name].add([vertex.index], 1.0, 'REPLACE')
    modifier = body.modifiers.new('PoseRig', 'ARMATURE')
    modifier.object = armature
    body.parent = armature

output = os.path.join(os.path.dirname(os.path.abspath(source_path)), 'Untitled_poseable.blend')
bpy.ops.wm.save_as_mainfile(filepath=output)
print('BASE_WEIGHTS_SAVED', output)
bpy.ops.wm.quit_blender()
