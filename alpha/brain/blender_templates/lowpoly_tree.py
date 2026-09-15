"""Low-poly tree — Alpha's bundled Blender bpy example (§7).

Run headless; renders a preview to __ALPHA_PREVIEW__ (replaced at runtime).
"""
import bpy
import random

OUT = "__ALPHA_PREVIEW__"
random.seed(7)

# clean scene
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)

# ground
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, 0))

# low-poly trunk
bpy.ops.mesh.primitive_cone_add(vertices=6, radius1=0.28, radius2=0.18,
                                depth=1.4, location=(0, 0, 0.7))
trunk = bpy.context.object
trunk.name = "Trunk"
mat_trunk = bpy.data.materials.new("TrunkMat")
mat_trunk.use_nodes = True
mat_trunk.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.28, 0.17, 0.08, 1)
trunk.data.materials.append(mat_trunk)

# low-poly foliage: stacked cones
mat_leaf = bpy.data.materials.new("LeafMat")
mat_leaf.use_nodes = True
mat_leaf.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.1, 0.45, 0.15, 1)

for i, (r, d, z) in enumerate([(1.5, 1.1, 1.6), (1.1, 0.9, 2.2), (0.7, 0.7, 2.75)]):
    jitter = (random.uniform(-0.08, 0.08), random.uniform(-0.08, 0.08))
    bpy.ops.mesh.primitive_cone_add(vertices=7, radius1=r, radius2=0.02,
                                    depth=d, location=(jitter[0], jitter[1], z))
    part = bpy.context.object
    part.name = f"Leaves{i}"
    part.rotation_euler = (0, 0, random.uniform(0, 0.6))
    part.data.materials.append(mat_leaf)

# sun + camera
bpy.ops.object.light_add(type="SUN", location=(3, -3, 6))
sun = bpy.context.object
sun.data.energy = 3
bpy.ops.object.camera_add(location=(5.2, -4.6, 3.2), rotation=(1.15, 0, 0.75))
cam = bpy.context.object
bpy.context.scene.camera = cam

# render
bpy.context.scene.render.filepath = OUT
bpy.context.scene.render.resolution_x = 960
bpy.context.scene.render.resolution_y = 540
bpy.context.scene.render.engine = "BLENDER_EEVEE" if hasattr(bpy.context.scene.render, "engine") else "CYCLES"
bpy.ops.render.render(write_still=True)
print("ALPHA_BLENDER_OK")
