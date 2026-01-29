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

def _smax(a, b, k):
    """Polynomial smooth maximum for clumpy blending."""
    h = max(k - abs(a - b), 0.0) / k
    return max(a, b) + h * h * k * 0.25

def get_voronoi_noise(x, y, z, scale, variance):
    """Squished Marshmallow. Massive overlap with deep creases."""
    sx, sy, sz = x * scale, y * scale, z * scale
    cx, cy, cz = math.floor(sx), math.floor(sy), math.floor(sz)
    
    final_val = 0.0
    k_merge = 0.25 # Sharp, deep creases for "squished" look
    
    # Check neighbors for bubble centers
    for i in range(-1, 2):
        for j in range(-1, 2):
            for k in range(-1, 2):
                cell = (cx + i, cy + j, cz + k)
                if cell not in _voronoi_points:
                    seed = hash(cell) % (2**31)
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
                    
                    # Merge with Smooth-Max
                    final_val = _smax(final_val, h, k_merge)
                    
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
            for i in range(0, len(coords), 3):
                pos = (round(coords[i], 5), round(coords[i+1], 5), round(coords[i+2], 5))
                n = (normals[i], normals[i+1], normals[i+2])
                if pos not in v_normals: v_normals[pos] = [0, 0, 0]
                v_normals[pos][0] += n[0]
                v_normals[pos][1] += n[1]
                v_normals[pos][2] += n[2]

            new_coords = list(coords)
            global _voronoi_points
            _voronoi_points = {}

            # 3. Apply Clumped Displacement
            for i in range(0, len(coords), 3):
                x, y, z = coords[i], coords[i+1], coords[i+2]
                pos = (round(x, 5), round(y, 5), round(z, 5))
                
                # Use the averaged normal for the shared vertex
                avg_n = v_normals[pos]
                mag = math.sqrt(avg_n[0]**2 + avg_n[1]**2 + avg_n[2]**2)
                nx, ny, nz = avg_n[0]/mag, avg_n[1]/mag, avg_n[2]/mag
                
                # Puffy Sphere Algorithm
                val, _ = get_voronoi_noise(x, y, z, density, variance)
                
                # Additivie displacement. 
                # If Face detected, add tiny epsilon to prevent Z-fighting with original surface.
                epsilon = 0.02 if not is_body_selection else 0.0
                displacement = (val * height) + epsilon
                
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
