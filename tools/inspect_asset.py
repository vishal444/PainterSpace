import bpy

print("OBJECTS")
for obj in bpy.data.objects:
    vertex_count = len(obj.data.vertices) if obj.type == 'MESH' else ''
    print(obj.name, obj.type, vertex_count)
print("ARMATURES", [obj.name for obj in bpy.data.objects if obj.type == 'ARMATURE'])
print("SCENE", bpy.context.scene.name)
