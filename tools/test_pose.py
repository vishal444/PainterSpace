import sys


def main():
    import bpy
    from mathutils import Vector

    path = next(value for value in reversed(sys.argv) if value.lower().endswith('.blend'))
    bpy.ops.wm.open_mainfile(filepath=path)
    armature = bpy.data.objects['PoseRig']
    armature.data.pose_position = 'POSE'
    armature.pose.bones['CTRL_hand.L'].location += Vector((-0.12, 0.0, 0.16))
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = 700
    scene.render.resolution_y = 700
    scene.render.resolution_percentage = 100
    scene.render.filepath = '/tmp/poseable_test_pose.png'
    meshes = [obj for obj in scene.objects if obj.type == 'MESH' and obj.name != 'Backdrop']
    points = [obj.matrix_world @ vertex.co for obj in meshes for vertex in obj.data.vertices]
    minimum = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
    maximum = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
    center = (minimum + maximum) * 0.5
    camera_data = bpy.data.cameras.new('PreviewCamera')
    camera = bpy.data.objects.new('PreviewCamera', camera_data)
    scene.collection.objects.link(camera)
    camera.location = center + Vector((0.0, -max(2.0, (maximum.z - minimum.z) * 2.2), 0.0))
    camera.data.lens = 52
    camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
    scene.camera = camera
    bpy.ops.render.render(write_still=True)
    print('RENDERED', scene.render.filepath)
    bpy.ops.wm.quit_blender()


if __name__ == '__main__':
    main()
