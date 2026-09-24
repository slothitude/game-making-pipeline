import bpy, math, os, sys, time, json
import mathutils as V

# SCULMM PILOT v3 -- root-cause fixes:
#   ACTOR BIND: ARMATURE_AUTO weights were INERT (27 groups, zero follow)
#               -> nearest-segment RIGID BIND (proven in bind_fix test)
#   CAMERA:     SPRING ARM per user directive -- critically-damped follow of
#               the Hips target; arm owns distance, Track To owns aim,
#               DOF focus on the target; constants are NAMED LAW values.
glb = "/home/aaron/ardi/t2d3/winston_n1_3d.glb"
bvh = "/home/aaron/ardi/t2d3/walk.bvh"
w = json.load(open("/home/aaron/sculmm/llm_world.json"))["world"]["rooms"][0]
outdir = "/home/aaron/sculmm/pilot3"
STILL = "--still" in sys.argv
os.makedirs(outdir, exist_ok=True)

# ---- CAMERA LAW (named values, LLM-verb-modulatable) ----
SPRING_K = 5.0          # critically-damped-ish follow (firm, soft settle)
ARM_OFFSET = V.Vector((2.6, -3.3, 0.5))   # distance+elevation owned by the arm
ORTHO_SCALE = 5.3       # m4v2 window (room-fitting)
DOF_FSTOP = 9.0         # generous: cel-crisp actor, soft set edges

bpy.ops.wm.read_factory_settings(use_empty=True)

def clay_mat(name, base, texpath=None):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    d = nt.nodes.new("ShaderNodeBsdfDiffuse")
    d.inputs["Color"].default_value = (*base, 1)
    nt.links.new(d.outputs[0], o.inputs[0]); return m

def cel_mat(name, base, texpath=None):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    d = nt.nodes.new("ShaderNodeBsdfDiffuse"); d.inputs["Color"].default_value = (*base, 1)
    if texpath:
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = bpy.data.images.load(texpath); t.interpolation = "Closest"
        nt.links.new(t.outputs["Color"], d.inputs["Color"])
    s2r = nt.nodes.new("ShaderNodeShaderToRGB"); r = nt.nodes.new("ShaderNodeValToRGB")
    r.color_ramp.interpolation = "CONSTANT"; e = r.color_ramp.elements
    while len(e) < 3: e.new(0.75)
    e[0].position, e[0].color = 0.0, (base[0]*.45, base[1]*.45, base[2]*.5, 1)
    e[1].position, e[1].color = 0.45, (base[0]*.85, base[1]*.85, base[2]*.9, 1)
    e[2].position, e[2].color = 0.7, (min(base[0]*1.35,1), min(base[1]*1.35,1), min(base[2]*1.35,1), 1)
    nt.links.new(d.outputs[0], s2r.inputs[0]); nt.links.new(s2r.outputs[0], r.inputs[0])
    nt.links.new(r.outputs[0], o.inputs[0]); return m

MATFN = clay_mat if "--clay" in sys.argv else cel_mat
SHELL = []
fw = w["shell"]["floor"]["size_m"][0]; half = fw/2
wh = w["shell"]["walls"][0]["height_m"]
def wallinfo(nm): return next((s for s in w["shell"]["walls"] if s["wall"]==nm), None)
def plane(size, loc, rot, mat):
    bpy.ops.mesh.primitive_plane_add(size=size, rotation=rot, location=loc)
    o = bpy.context.object; o.data.materials.append(mat); SHELL.append(o)
plane(fw, (0,0,0), (0,0,0), MATFN("floor", (0.55,0.55,0.55), w["shell"]["floor"]["texture"]["asset"]))
for nm, loc, rot in (("north",(0,-half,wh/2),(math.radians(90),0,0)),
                     ("south",(0, half,wh/2),(math.radians(-90),0,0)),
                     ("east", (half,0,wh/2),(math.radians(90),0,math.radians(-90))),
                     ("west", (-half,0,wh/2),(math.radians(90),0,math.radians(90)))):
    wi = wallinfo(nm)
    plane(fw, loc, rot, MATFN("w_"+nm, (0.6,0.6,0.62), wi["texture"]["asset"] if wi else None))
PF = {"brass telescope": ("telescope_n.glb", (-2.4,-half+1.3)),
      "star chart table": ("star_table_n.glb", (0.6,0.4)),
      "lantern": ("lantern_n.glb", (half-1.5,1.8)),
      "wooden chair": ("chair_n.glb", (-half+1.7,1.6))}
for nm in w["props"]:
    fn, (x, y) = PF.get(nm, (None,(0,0)))
    if not fn: continue
    bpy.ops.import_scene.gltf(filepath="/mnt/seagate/sculmm/meshes/"+fn)
    o = [o2 for o2 in bpy.context.scene.objects if o2.type=="MESH"][-1]
    o.location = V.Vector((x, y, 0)); o.data.materials.clear()
    o.data.materials.append(MATFN("p_"+nm.replace(" ","_"), (0.62,0.55,0.42)))
    SHELL.append(o)

# ---- actor + RIGID BIND ----
bpy.ops.import_anim.bvh(filepath=bvh)
arm = [o for o in bpy.context.scene.objects if o.type=="ARMATURE"][0]
bpy.ops.import_scene.gltf(filepath=glb)
mesh = [o for o in bpy.context.scene.objects if o.type=="MESH" and o not in SHELL][0]
s = 1.7/max(mesh.dimensions.z,1e-6); mesh.scale=(s,s,s)
bpy.ops.object.select_all(action="DESELECT"); mesh.select_set(True)
bpy.context.view_layer.objects.active = mesh
bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
bb=[mesh.matrix_world @ V.Vector(c) for c in mesh.bound_box]
mesh.location.z -= min(v.z for v in bb)
mesh.parent = arm
mesh.modifiers.new("arm","ARMATURE").object = arm
sc = bpy.context.scene; sc.frame_set(1)
arm_rest = {b.name: (arm.matrix_world @ b.head_local, arm.matrix_world @ b.tail_local) for b in arm.data.bones}
segs = [(n, h, t) for n, (h, t) in arm_rest.items()]
mesh_vg = {n: mesh.vertex_groups.new(name=n) for n, _, _ in segs}
mw = mesh.matrix_world
for v in mesh.data.vertices:
    p = mw @ v.co
    best, bn = 1e9, None
    for n, h, t in segs:
        hv = t - h; L = max(hv.length_squared, 1e-9)
        k = min(max((p - h).dot(hv) / L, 0.0), 1.0)
        d = (p - (h + hv*k)).length_squared
        if d < best: best, bn = d, n
    mesh_vg[bn].add([v.index], 1.0, "REPLACE")
mesh.data.materials.clear()
mesh.data.materials.append(MATFN("body", (0.32,0.42,0.72)))
sc.frame_set(1); bpy.context.view_layer.update()
bb=[mesh.matrix_world @ V.Vector(c) for c in mesh.bound_box]
print("RIGID_BIND_OK f1 bbox y %.2f..%.2f" % (min(v.y for v in bb), max(v.y for v in bb)), flush=True)

sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun","SUN"))
sun.data.energy=3.0; sun.rotation_euler=(math.radians(55),0,math.radians(35))
bpy.context.collection.objects.link(sun)
world = bpy.data.worlds.new("w"); world.use_nodes=True
world.node_tree.nodes["Background"].inputs[0].default_value=(0.65,0.68,0.72,1)
sc.world = world

# ---- SPRING ARM CAMERA ----
target_empty = bpy.data.objects.new("spring_target", None)
bpy.context.collection.objects.link(target_empty)
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("c")); cdata = cam.data
cdata.type="ORTHO"; cdata.ortho_scale = ORTHO_SCALE
cdata.clip_start, cdata.clip_end = 0.1, 100.0
cdata.dof.use_dof = True; cdata.dof.aperture_fstop = DOF_FSTOP
bpy.context.collection.objects.link(cam)
tc = cam.constraints.new("TRACK_TO")
tc.target = target_empty; tc.track_axis = "TRACK_NEGATIVE_Z"; tc.up_axis = "UP_Y"
bpy.context.scene.camera = cam
cdata.dof.focus_object = target_empty
cam_pos = V.Vector((0,0,0))
DT = 1.0/30.0

def spring_step(f):
    global cam_pos
    sc.frame_set(f); bpy.context.view_layer.update()
    hip = arm.matrix_world @ arm.pose.bones["Hips"].head.copy()
    tgt = V.Vector((hip.x, hip.y, hip.z + 0.55))
    target_empty.location = tgt
    want = tgt + ARM_OFFSET
    cam_pos = cam_pos.lerp(want, 1.0 - math.exp(-SPRING_K * DT))
    cam.location = cam_pos

sc.render.resolution_x=1920; sc.render.resolution_y=1080
sc.render.image_settings.file_format="PNG"
act = arm.animation_data.action
F2 = min(int(act.frame_range[1]), 150)
t0 = time.time()
for f in range(1, F2+1):   # spring integrates EVERY frame; render per mode
    spring_step(f)
    if (not STILL) or (STILL and f == 40):
        sc.render.filepath = os.path.join(outdir, "f%04d.png" % f)
        bpy.ops.render.render(write_still=True)
        if f in (1, 40, 80, 120, 150):
            bb=[mesh.matrix_world @ V.Vector(c) for c in mesh.bound_box]
            hip = arm.matrix_world @ arm.pose.bones["Hips"].head
            print("SPRING f=%d cam=(%.1f,%.1f) ortho=%.1f hips=(%.2f,%.2f) meshy=%.2f..%.2f"
                  % (f, cam.location.x, cam.location.y, cdata.ortho_scale,
                     hip.x, hip.y, min(v.y for v in bb), max(v.y for v in bb)), flush=True)
n = F2
print("V3_DONE frames=%d wall=%.0fs spring_k=%.1f arm=%s dof=f/%.0f ortho=%.1f"
      % (n, time.time()-t0, SPRING_K, tuple(ARM_OFFSET), DOF_FSTOP, ORTHO_SCALE), flush=True)
