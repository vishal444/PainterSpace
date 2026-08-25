import bpy
import os
import sys

path = next(value for value in reversed(sys.argv) if value.lower().endswith('.blend'))
bpy.ops.wm.open_mainfile(filepath=path)
output = os.path.join(os.path.dirname(path), 'Untitled_poseable.glb')
window = next(iter(bpy.context.window_manager.windows), None)
area = next((candidate for candidate in window.screen.areas if candidate.type == 'VIEW_3D'), None) if window else None
region = next((candidate for candidate in area.regions if candidate.type == 'WINDOW'), None) if area else None
override = {'window': window, 'screen': window.screen if window else None, 'area': area, 'region': region}
with bpy.context.temp_override(**override):
    bpy.ops.export_scene.gltf(
        filepath=output,
        export_format='GLB',
        export_apply=False,
        export_animations=False,
        export_skins=True,
        export_morph=True,
        export_materials='EXPORT',
    )
print('GLB_EXPORTED', output)
bpy.ops.wm.quit_blender()
