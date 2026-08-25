import bpy
import sys

path = next(value for value in reversed(sys.argv) if value.lower().endswith('.blend'))
bpy.ops.wm.open_mainfile(filepath=path)
armature = bpy.data.objects.get('PoseRig')
print('VALIDATE_ARMATURE', bool(armature))
if armature:
    print('VALIDATE_BONES', len(armature.data.bones))
    print('VALIDATE_CONTROLLERS', sorted(bone.name for bone in armature.data.bones if bone.name.startswith('CTRL_')))
    print('VALIDATE_IK', sum(1 for pose_bone in armature.pose.bones for constraint in pose_bone.constraints if constraint.type == 'IK'))
print('VALIDATE_MESH_MODIFIERS', [(obj.name, sum(1 for modifier in obj.modifiers if modifier.type == 'ARMATURE'), len(obj.vertex_groups)) for obj in bpy.data.objects if obj.type == 'MESH'])
bpy.ops.wm.quit_blender()
