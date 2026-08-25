import bpy
import os
import sys
from mathutils import Vector


def input_path():
    for value in reversed(sys.argv):
        if value.lower().endswith('.blend'):
            return os.path.abspath(value)
    return bpy.data.filepath


source_path = input_path()
if source_path and os.path.abspath(bpy.data.filepath) != source_path:
    bpy.ops.wm.open_mainfile(filepath=source_path)

mesh_objects = [obj for obj in bpy.context.scene.objects
                if obj.type == 'MESH' and obj.visible_get()
                and obj.name.lower() not in {'backdrop', 'ground', 'groundplane'}]
if not mesh_objects:
    raise RuntimeError('No visible mesh objects found in the supplied asset.')

# Work from world-space bounds so the rig follows assets that were authored
# with non-zero object transforms or multiple mesh objects.
world_points = []
for obj in mesh_objects:
    world_points.extend(obj.matrix_world @ vertex.co for vertex in obj.data.vertices)

minimum = Vector((min(point.x for point in world_points), min(point.y for point in world_points), min(point.z for point in world_points)))
maximum = Vector((max(point.x for point in world_points), max(point.y for point in world_points), max(point.z for point in world_points)))
center = (minimum + maximum) * 0.5
height = max(0.001, maximum.z - minimum.z)
width = max(0.001, maximum.x - minimum.x)
depth = max(0.001, maximum.y - minimum.y)

old = bpy.data.objects.get('PoseRig')
if old:
    bpy.data.objects.remove(old, do_unlink=True)

armature_data = bpy.data.armatures.new('PoseRig')
armature = bpy.data.objects.new('PoseRig', armature_data)
bpy.context.collection.objects.link(armature)
armature.show_in_front = True
armature.display_type = 'WIRE'
armature.data.display_type = 'BBONE'
armature.data.axes_position = 0.0

def point(x, z, depth_offset=0.0):
    """Return an X/Y/Z point for the asset's Z-up coordinate system."""
    return Vector((center.x + x * width,
                   center.y + depth_offset * depth,
                   minimum.z + z * height))

bone_specs = {
    'root': (point(0, 0.04), point(0, 0.14), None, True),
    'pelvis': (point(0, 0.43), point(0, 0.55), 'root', True),
    'spine': (point(0, 0.50), point(0, 0.68), 'pelvis', True),
    'chest': (point(0, 0.65), point(0, 0.78), 'spine', True),
    'neck': (point(0, 0.76), point(0, 0.86), 'chest', True),
    'head': (point(0, 0.81), point(0, 0.98), 'neck', True),
    'upper_arm.L': (point(-0.22, 0.70), point(-0.40, 0.58), 'chest', True),
    'forearm.L': (point(-0.40, 0.58), point(-0.52, 0.45), 'upper_arm.L', True),
    'hand.L': (point(-0.52, 0.45), point(-0.56, 0.39), 'forearm.L', True),
    'upper_arm.R': (point(0.22, 0.70), point(0.40, 0.58), 'chest', True),
    'forearm.R': (point(0.40, 0.58), point(0.52, 0.45), 'upper_arm.R', True),
    'hand.R': (point(0.52, 0.45), point(0.56, 0.39), 'forearm.R', True),
    'thigh.L': (point(-0.12, 0.48), point(-0.15, 0.28), 'pelvis', True),
    'shin.L': (point(-0.15, 0.28), point(-0.15, 0.08), 'thigh.L', True),
    'foot.L': (point(-0.15, 0.08), point(-0.15, 0.035, 0.18), 'shin.L', True),
    'thigh.R': (point(0.12, 0.48), point(0.15, 0.28), 'pelvis', True),
    'shin.R': (point(0.15, 0.28), point(0.15, 0.08), 'thigh.R', True),
    'foot.R': (point(0.15, 0.08), point(0.15, 0.035, 0.18), 'shin.R', True),
}

controller_specs = {
    'CTRL_hand.L': (point(-0.56, 0.39, 0.02), point(-0.56, 0.33, 0.02), None),
    'CTRL_hand.R': (point(0.56, 0.39, 0.02), point(0.56, 0.33, 0.02), None),
    'CTRL_foot.L': (point(-0.15, 0.035, 0.18), point(-0.15, 0.035, 0.28), None),
    'CTRL_foot.R': (point(0.15, 0.035, 0.18), point(0.15, 0.035, 0.28), None),
    'CTRL_elbow.L': (point(-0.40, 0.58, -0.14), point(-0.40, 0.58, -0.22), None),
    'CTRL_elbow.R': (point(0.40, 0.58, -0.14), point(0.40, 0.58, -0.22), None),
    'CTRL_knee.L': (point(-0.15, 0.28, -0.14), point(-0.15, 0.28, -0.22), None),
    'CTRL_knee.R': (point(0.15, 0.28, -0.14), point(0.15, 0.28, -0.22), None),
}

bpy.ops.object.select_all(action='DESELECT')
armature.select_set(True)
bpy.context.view_layer.objects.active = armature
window = next(iter(bpy.context.window_manager.windows), None)
area = next((candidate for candidate in window.screen.areas if candidate.type == 'VIEW_3D'), None) if window else None
region = next((candidate for candidate in area.regions if candidate.type == 'WINDOW'), None) if area else None
override = {'window': window, 'screen': window.screen if window else None, 'area': area, 'region': region}
with bpy.context.temp_override(**override):
    bpy.ops.object.mode_set(mode='EDIT')
for name, (head, tail, parent, deform) in bone_specs.items():
    bone = armature.data.edit_bones.new(name)
    bone.head = head
    bone.tail = tail
    bone.use_deform = deform
    if parent:
        bone.parent = armature.data.edit_bones.get(parent)
for name, (head, tail, parent) in controller_specs.items():
    bone = armature.data.edit_bones.new(name)
    bone.head = head
    bone.tail = tail
    bone.use_deform = False
    if parent:
        bone.parent = armature.data.edit_bones.get(parent)
with bpy.context.temp_override(**override):
    bpy.ops.object.mode_set(mode='POSE')

def add_ik(name, target, pole, chain_count):
    constraint = armature.pose.bones[name].constraints.new('IK')
    constraint.name = 'IK control'
    constraint.target = armature
    constraint.subtarget = target
    constraint.pole_target = armature
    constraint.pole_subtarget = pole
    constraint.chain_count = chain_count

add_ik('hand.L', 'CTRL_hand.L', 'CTRL_elbow.L', 2)
add_ik('hand.R', 'CTRL_hand.R', 'CTRL_elbow.R', 2)
add_ik('foot.L', 'CTRL_foot.L', 'CTRL_knee.L', 2)
add_ik('foot.R', 'CTRL_foot.R', 'CTRL_knee.R', 2)
with bpy.context.temp_override(**override):
    bpy.ops.object.mode_set(mode='OBJECT')

# Parent every visible mesh to the armature with automatic weights when
# possible. Existing modifiers and materials are preserved.
with bpy.context.temp_override(**override):
    bpy.ops.object.select_all(action='DESELECT')
for obj in mesh_objects:
    obj.select_set(True)
armature.select_set(True)
bpy.context.view_layer.objects.active = armature
try:
    bpy.ops.object.parent_set(type='ARMATURE_AUTO')
except RuntimeError:
    # Keep the armature usable even for meshes Blender cannot auto-weight.
    for obj in mesh_objects:
        if not any(mod.type == 'ARMATURE' for mod in obj.modifiers):
            modifier = obj.modifiers.new('PoseRig', 'ARMATURE')
            modifier.object = armature

# Some clothing and hair meshes do not accept Blender's automatic-weight
# operator. Give those pieces a stable nearest-bone fallback so they follow
# the body instead of remaining rigid when the pose changes.
deform_bones = [bone for bone in armature.data.bones if bone.use_deform]
for obj in mesh_objects:
    if len(obj.vertex_groups) > 0:
        continue
    for bone in deform_bones:
        obj.vertex_groups.new(name=bone.name)
    for vertex in obj.data.vertices:
        world_point = obj.matrix_world @ vertex.co
        nearest_bone = min(
            deform_bones,
            key=lambda bone: min((world_point - bone.head_local).length,
                                 (world_point - bone.tail_local).length))
        obj.vertex_groups[nearest_bone.name].add([vertex.index], 1.0, 'REPLACE')
    if not any(modifier.type == 'ARMATURE' for modifier in obj.modifiers):
        modifier = obj.modifiers.new('PoseRig', 'ARMATURE')
        modifier.object = armature

armature.select_set(True)
bpy.context.view_layer.objects.active = armature
with bpy.context.temp_override(**override):
    bpy.ops.object.mode_set(mode='POSE')
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(os.path.dirname(source_path), 'Untitled_poseable.blend'))

print('POSEABLE_ASSET_SAVED', os.path.join(os.path.dirname(source_path), 'Untitled_poseable.blend'))
