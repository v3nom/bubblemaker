import adsk.core, adsk.fusion, adsk.cam, traceback
import math
import random

# Global list to keep a reference to All Command Events to prevent them from being GC'd
handlers = []

COMMAND_ID = 'BubbleMakerCommand'
COMMAND_NAME = 'Bubble Maker'
COMMAND_DESCRIPTION = 'Create a bubble texture on a selected face'

# Define the Voronoi Noise point cache to keep it consistent
_voronoi_points = {}

def _pmax(a, b, k):
    """Power smooth maximum. Ensures f(x, 0) == x, preventing tears."""
    # Avoid complex numbers with tiny negative inputs (though we expect positive)
    a = max(0.0, a)
    b = max(0.0, b)
    return (a**k + b**k)**(1.0/k)

def get_voronoi_noise(x, y, z, scale, variance):
    """Squished Marshmallow. Massive overlap with deep creases."""
    sx, sy, sz = x * scale, y * scale, z * scale
    cx, cy, cz = math.floor(sx), math.floor(sy), math.floor(sz)
    
    final_val = 0.0
    # k_merge removed, using power factor 6.0 in _pmax
    
    # Check neighbors for bubble centers
    # Expanded range to catch bubbles that overlap grid lines
    for i in range(-3, 4):
        for j in range(-3, 4):
            for k in range(-3, 4):
                cell = (cx + i, cy + j, cz + k)
                if cell not in _voronoi_points:
                    # Custom deterministic hash to avoid hash(-1) == hash(-2) collision
                    # and ensure cross-platform consistency
                    cx_int, cy_int, cz_int = int(cell[0]), int(cell[1]), int(cell[2])
                    seed = ((cx_int * 73856093) ^ (cy_int * 19349663) ^ (cz_int * 83492791)) % (2**31)
                    random.seed(seed)
                    px = cell[0] + random.random()
                    py = cell[1] + random.random()
                    pz = cell[2] + random.random()
                    # High variance ensures "Chaos" vs "Grid"
                    h_f = 0.9 + (random.uniform(0, 1) * variance * 1.5)
                    # Radius 1.1 - 1.6 guarantees massive compression/overlap
                    r_f = 1.1 + (random.uniform(0, 1) * 0.5)
                    _voronoi_points[cell] = (px, py, pz, h_f, r_f)
                
                px, py, pz, h_f, r_f = _voronoi_points[cell]
                d2 = ((sx - px)**2 + (sy - py)**2 + (sz - pz)**2) / (r_f**2)
                
                if d2 < 1.0:
                    # Quartic Profile: (1-d^2)^2
                    # Fat top, steep shoulders
                    t = 1.0 - d2
                    bubble = t * t
                    h = bubble * h_f
                    
                    # Merge with Power-Max
                    # k=6.0 provides a good balance of smoothness and distinct shapes
                    final_val = _pmax(final_val, h, 6.0)
                    
    return final_val, 1.0

class BubbleMakerCommandExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()
    def notify(self, args):
        try:
            command = args.firingEvent.sender
            inputs = command.commandInputs

            # Get input values
            selection_input = inputs.itemById('selection')
            height_input = inputs.itemById('height')
            density_input = inputs.itemById('density')
            variance_input = inputs.itemById('variance')

            selected_entity = selection_input.selection(0).entity
            height = height_input.value
            density = density_input.value
            variance = variance_input.value

            app = adsk.core.Application.get()
            design = adsk.fusion.Design.cast(app.activeProduct)
            
            # Determine if we selected a face or a whole body
            is_body_selection = False
            target_obj = selected_entity
            if hasattr(selected_entity, 'body'):
                # It's a face, we'll use its parent body for the component context
                parent_comp = selected_entity.body.parentComponent
                is_body_selection = False
            else:
                # It's a body
                parent_comp = selected_entity.parentComponent
                is_body_selection = True

            # 1. Tesselate target with EXTREMELY high density
            mesh_mgr = target_obj.meshManager
            mesh_calc = mesh_mgr.createMeshCalculator()
            mesh_calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.HighQualityTriangleMesh)
            
            # Premium Refinement: Ensure small triangles for smooth rounds
            target_edge_length = 1.0 / (max(0.1, density) * 20.0)
            mesh_calc.maxSideLength = max(0.003, min(0.3, target_edge_length))
            mesh_calc.surfaceTolerance = mesh_calc.maxSideLength * 0.1
            
            base_mesh = mesh_calc.calculate()

            # 2. Extract Vertices and Normals
            coords = base_mesh.nodeCoordinatesAsFloat
            normals = base_mesh.normalVectorsAsFloat
            indices = base_mesh.nodeIndices

            # --- Vertex Welding & Normal Averaging ---
            # Identifies shared vertices (corners) and ensures they displace together.
            v_normals = {}
            v_counts = {}
            for i in range(0, len(coords), 3):
                pos = (round(coords[i], 4), round(coords[i+1], 4), round(coords[i+2], 4))
                n = (normals[i], normals[i+1], normals[i+2])
                if pos not in v_normals: 
                    v_normals[pos] = [0, 0, 0]
                    v_counts[pos] = 0
                v_normals[pos][0] += n[0]
                v_normals[pos][1] += n[1]
                v_normals[pos][2] += n[2]
                v_counts[pos] += 1

            # --- Multi-Pass Smoothing Diffusion ---
            # 1. Map Position -> index
            pos_list = list(v_normals.keys())
            pos_idx_map = {p: i for i, p in enumerate(pos_list)}
            
            # 2. Build Adjacency List (Index -> Set of Neighbors)
            adj_list = [set() for _ in range(len(pos_list))]
            
            # Iterate triangles to find connections
            for i in range(0, len(coords), 3):
                p1 = (round(coords[i], 4), round(coords[i+1], 4), round(coords[i+2], 4))
                p2 = (round(coords[i+3], 4), round(coords[i+4], 4), round(coords[i+5], 4)) if i+5 < len(coords) else None
                p3 = (round(coords[i+6], 4), round(coords[i+7], 4), round(coords[i+8], 4)) if i+8 < len(coords) else None
                
                # Wait, range step 3 means we are iterating vertices? No, indices are tri-based?
                # The loop is `range(0, len(coords), 3)`. Coords is flat x,y,z,x,y,z...
                # i corresponds to a vertex. A triangle is i, i+3, i+6? NO.
                # `coords` is raw vertex buffer. 3 floats per vertex. Triangle is 9 floats (3 verts).
                # But `coords` is just a list of points. The `indices` array defines topology!
                # If `indices` exists, we use it. If not (rare for fusion mesh?), we assume linear.
                # However, this script ignores `indices` for building `v_normals` and just uses spatial hashing.
                # For adjacency, we MUST use the triangle connectivity.
                
                # Correct approach using indices (Fusion creates indexed mesh)
                pass 
                
            # Iterate using indices to build adjacency based on spatial hashing
            for i in range(0, len(indices), 3):
                # Get vertex indices of a triangle
                idx1, idx2, idx3 = indices[i], indices[i+1], indices[i+2]
                
                # Get their coordinates from coords array
                # Each index points to a (x,y,z) triplet start = index * 3
                def get_pos_key(idx):
                    base = idx * 3
                    return (round(coords[base], 4), round(coords[base+1], 4), round(coords[base+2], 4))
                
                pos1 = get_pos_key(idx1)
                pos2 = get_pos_key(idx2)
                pos3 = get_pos_key(idx3)
                
                # Get mapped IDs
                id1, id2, id3 = pos_idx_map[pos1], pos_idx_map[pos2], pos_idx_map[pos3]
                
                # Add connections
                adj_list[id1].add(id2)
                adj_list[id1].add(id3)
                adj_list[id2].add(id1)
                adj_list[id2].add(id3)
                adj_list[id3].add(id1)
                adj_list[id3].add(id2)

            # 3. Initial Smoothness
            current_smoothness = [0.0] * len(pos_list)
            for i, p in enumerate(pos_list):
                 n_sum = v_normals[p]
                 count = v_counts[p]
                 mag = math.sqrt(n_sum[0]**2 + n_sum[1]**2 + n_sum[2]**2)
                 # Base smoothness
                 val = (mag / count) if count > 0 else 1.0
                 current_smoothness[i] = val
            
            # 4. Multi-Pass Diffusion
            # 3 passes is usually enough to blur the sharp edge
            passes = 3
            for _ in range(passes):
                next_smoothness = list(current_smoothness)
                for i in range(len(pos_list)):
                    # Gather neighbors
                    neighbors = adj_list[i]
                    if not neighbors: continue
                    
                    sum_val = current_smoothness[i] # Weight self
                    count = 1
                    
                    for n_idx in neighbors:
                        sum_val += current_smoothness[n_idx]
                        count += 1
                    
                    next_smoothness[i] = sum_val / count
                current_smoothness = next_smoothness
            
            final_smoothness = current_smoothness

            new_coords = list(coords)
            global _voronoi_points
            _voronoi_points = {}

            # 3. Apply Clumped Displacement
            for i in range(0, len(coords), 3):
                x, y, z = coords[i], coords[i+1], coords[i+2]
                pos = (round(x, 4), round(y, 4), round(z, 4))
                # Use the averaged normal for the shared vertex
                # Fetch smoothed "attenuation" value
                smooth_val = final_smoothness[pos_idx_map[pos]]
                
                # Apply Power atttentuation on smoothed factor
                attenuation = smooth_val ** 4.0
                
                avg_n = v_normals[pos]
                mag = math.sqrt(avg_n[0]**2 + avg_n[1]**2 + avg_n[2]**2)
                
                if mag < 0.0001: 
                    nx, ny, nz = 0.0, 0.0, 1.0
                else:
                    nx, ny, nz = avg_n[0]/mag, avg_n[1]/mag, avg_n[2]/mag
                
                # Puffy Sphere Algorithm
                val, _ = get_voronoi_noise(x, y, z, density, variance)
                
                # Additivie displacement. 
                # If Face detected, add tiny epsilon to prevent Z-fighting with original surface.
                epsilon = 0.02 if not is_body_selection else 0.0
                displacement = (val * height * attenuation) + epsilon
                
                new_coords[i] = x + (nx * displacement)
                new_coords[i+1] = y + (ny * displacement)
                new_coords[i+2] = z + (nz * displacement)

            # 4. Hide Original Body if "Smart Auto-Hide" is active (Body mode only)
            if is_body_selection:
                target_obj.isVisible = False

            # 6. Create the new Mesh Body
            # 6. Create the new Mesh Body via a temporary STL file
            # Fusion 360 API is limited on creating TriangleMesh objects from scratch.
            # The most reliable way to inject custom mesh data is via STL import.
            import os, tempfile
            
            with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as tmp:
                tmp_path = tmp.name
                # Write STL manually
                # Header (80 bytes)
                tmp.write(b'\x00' * 80)
                # Number of triangles (4 bytes)
                num_tris = len(indices) // 3
                tmp.write(num_tris.to_bytes(4, 'little'))
                
                for i in range(0, len(indices), 3):
                    # Normal (3 floats) - we can just use 0,0,0
                    tmp.write(float_to_bytes(0.0))
                    tmp.write(float_to_bytes(0.0))
                    tmp.write(float_to_bytes(0.0))
                    # Vertices (3 * 3 floats)
                    for j in range(3):
                        idx = indices[i + j] * 3
                        tmp.write(float_to_bytes(new_coords[idx]))
                        tmp.write(float_to_bytes(new_coords[idx+1]))
                        tmp.write(float_to_bytes(new_coords[idx+2]))
                    # Attribute byte count (2 bytes)
                    tmp.write(b'\x00\x00')
            
            # Import the STL as a mesh body
            units = adsk.fusion.MeshUnits.CentimeterMeshUnit
            # Use the direct add method which returns a MeshBodyList
            mesh_list = parent_comp.meshBodies.add(tmp_path, units)
            if mesh_list.count > 0:
                new_mesh = mesh_list.item(0)
                new_mesh.name = "Bubble_Texture"
            
            # Cleanup
            os.remove(tmp_path)

            args.isValidResult = True

        except:
            if adsk.core.Application.get():
                adsk.core.Application.get().userInterface.messageBox('Failed:\n{}'.format(traceback.format_exc()))

import struct
def float_to_bytes(f):
    return struct.pack('<f', f)

class BubbleMakerCommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()
    def notify(self, args):
        try:
            # Terminate the script
            # adsk.terminate()
            pass
        except:
            pass

class BubbleMakerCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()
    def notify(self, args):
        try:
            cmd = args.command
            cmd.isExecutedWhenPreEmpted = False
            inputs = cmd.commandInputs

            # Create selection input
            selection_input = inputs.addSelectionInput('selection', 'Target Face/Body', 'Select a face or body to apply texture.')
            selection_input.setSelectionLimits(1, 1)
            selection_input.addSelectionFilter('Faces')
            selection_input.addSelectionFilter('Bodies')

            # Create value inputs
            # Units are in cm by default in Fusion internal UI if not specified
            inputs.addValueInput('height', 'Bubble Height', 'cm', adsk.core.ValueInput.createByReal(0.5))
            inputs.addValueInput('density', 'Density', '', adsk.core.ValueInput.createByReal(1.5))
            inputs.addValueInput('variance', 'Variance', '', adsk.core.ValueInput.createByReal(0.4))

            # Connect execute handler
            on_execute = BubbleMakerCommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)
            
            on_destroy = BubbleMakerCommandDestroyHandler()
            cmd.destroy.add(on_destroy)
            handlers.append(on_destroy)

        except:
            if adsk.core.Application.get():
                adsk.core.Application.get().userInterface.messageBox('Failed:\n{}'.format(traceback.format_exc()))

def run(context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        # Create the command definition
        cmd_def = ui.commandDefinitions.itemById(COMMAND_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(COMMAND_ID, COMMAND_NAME, COMMAND_DESCRIPTION, './resources')

        # Connect created handler
        on_created = BubbleMakerCommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)

        # Execute the command
        cmd_def.execute()

        # Prevent from being GC'd
        adsk.autoTerminate(False)

    except:
        if adsk.core.Application.get():
            adsk.core.Application.get().userInterface.messageBox('Failed:\n{}'.format(traceback.format_exc()))

def stop(context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        
        # Clean up UI
        cmd_def = ui.commandDefinitions.itemById(COMMAND_ID)
        if cmd_def:
            cmd_def.deleteMe()
            
    except:
        pass
