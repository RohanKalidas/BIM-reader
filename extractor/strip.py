import ifcopenshell
import ifcopenshell.util.element as util
import psycopg2
import psycopg2.extras
import os
import sys
import numpy as np
from dotenv import load_dotenv
from datetime import datetime
from collections import defaultdict

load_dotenv()

# --- Database connection ---
def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# --- Create a project record ---
def create_project(cursor, filename):
    cursor.execute(
        """
        INSERT INTO projects (name, filename, status)
        VALUES (%s, %s, 'processing')
        RETURNING id
        """,
        (filename.replace(".ifc", ""), filename)
    )
    return cursor.fetchone()[0]

# --- Mark project as done ---
def finish_project(cursor, project_id):
    cursor.execute(
        """
        UPDATE projects
        SET status = 'done', processed_at = %s
        WHERE id = %s
        """,
        (datetime.now(), project_id)
    )

# --- Save a component ---
def save_component(cursor, project_id, category, family_name, type_name, revit_id, parameters):
    cursor.execute(
        """
        INSERT INTO components (project_id, category, family_name, type_name, revit_id, parameters)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (project_id, category, family_name, type_name, revit_id, psycopg2.extras.Json(parameters))
    )
    return cursor.fetchone()[0]

# --- Save spatial data ---
def save_spatial_data(cursor, component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, bounding_box, level, elevation):
    cursor.execute(
        """
        INSERT INTO spatial_data (component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, bounding_box, level, elevation)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, psycopg2.extras.Json(bounding_box), level, elevation)
    )

# --- Save a wall type ---
def save_wall_type(cursor, component_id, thickness, function, layers):
    cursor.execute(
        """
        INSERT INTO wall_types (component_id, total_thickness, function, layers)
        VALUES (%s, %s, %s, %s)
        """,
        (component_id, thickness, function, psycopg2.extras.Json(layers))
    )

# --- Save an MEP system ---
def save_mep_system(cursor, component_id, system_type, flow_rate, pressure_drop, connectors):
    cursor.execute(
        """
        INSERT INTO mep_systems (component_id, system_type, flow_rate, pressure_drop, connectors)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (component_id, system_type, flow_rate, pressure_drop, psycopg2.extras.Json(connectors))
    )

# --- Save a material ---
def save_material(cursor, project_id, name, category, properties):
    cursor.execute(
        """
        INSERT INTO materials (project_id, name, category, properties)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (project_id, name, category, psycopg2.extras.Json(properties))
    )

# --- Extract position and rotation from IFC placement ---
def extract_placement(element):
    try:
        placement = element.ObjectPlacement
        if not placement:
            return None, None, None, None, None, None

        loc = placement.RelativePlacement
        if not loc:
            return None, None, None, None, None, None

        # Position
        origin = loc.Location
        pos_x = origin.Coordinates[0] if origin else None
        pos_y = origin.Coordinates[1] if origin else None
        pos_z = origin.Coordinates[2] if origin and len(origin.Coordinates) > 2 else 0.0

        # Rotation — extract from axis and ref direction
        rot_x, rot_y, rot_z = 0.0, 0.0, 0.0
        if hasattr(loc, 'Axis') and loc.Axis and hasattr(loc, 'RefDirection') and loc.RefDirection:
            axis = np.array(loc.Axis.DirectionRatios)
            ref = np.array(loc.RefDirection.DirectionRatios)
            # Calculate rotation Z angle from ref direction
            rot_z = float(np.degrees(np.arctan2(ref[1], ref[0])))

        return pos_x, pos_y, pos_z, rot_x, rot_y, rot_z
    except:
        return None, None, None, None, None, None

# --- Extract bounding box from element geometry ---
def extract_bounding_box(element):
    try:
        if not element.Representation:
            return {}

        all_points = []
        for rep in element.Representation.Representations:
            for item in rep.Items:
                if item.is_a("IfcBoundingBox"):
                    corner = item.Corner
                    x, y, z = corner.Coordinates
                    all_points.append((x, y, z))
                    all_points.append((x + item.XDim, y + item.YDim, z + item.ZDim))

        if not all_points:
            return {}

        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]
        zs = [p[2] for p in all_points]

        return {
            "min_x": min(xs), "min_y": min(ys), "min_z": min(zs),
            "max_x": max(xs), "max_y": max(ys), "max_z": max(zs)
        }
    except:
        return {}

# --- Extract all components from the IFC file ---
def extract(filepath):
    print(f"Loading {filepath}...")
    model = ifcopenshell.open(filepath)
    filename = os.path.basename(filepath)

    conn = get_db()
    cursor = conn.cursor()

    project_id = create_project(cursor, filename)
    print(f"Created project record (id={project_id})")

    counts = defaultdict(int)

    for element in model.by_type("IfcElement"):
        category = element.is_a()
        family_name = element.Name or ""
        type_name = ""
        revit_id = element.GlobalId

        # Try to get the type name
        if element.IsTypedBy:
            for rel in element.IsTypedBy:
                type_name = rel.RelatingType.Name or ""

        # Grab all parameters
        parameters = {}
        try:
            for pset_name, pset in util.get_psets(element).items():
                parameters[pset_name] = pset
        except:
            pass

        # --- Extract material name ---
        try:
            for rel in element.HasAssociations:
                if rel.is_a("IfcRelAssociatesMaterial"):
                    mat = rel.RelatingMaterial
                    if mat.is_a("IfcMaterial"):
                        parameters["_material"] = mat.Name
                    elif mat.is_a("IfcMaterialLayerSetUsage"):
                        layers = []
                        for layer in mat.ForLayerSet.MaterialLayers:
                            layers.append({
                                "material": layer.Material.Name if layer.Material else "",
                                "thickness": layer.LayerThickness or 0
                            })
                        parameters["_material_layers"] = layers
        except:
            pass

        # --- Extract storey/level ---
        level = None
        elevation = None
        try:
            for rel in element.ContainedInStructure:
                if rel.is_a("IfcRelContainedInSpatialStructure"):
                    storey = rel.RelatingStructure
                    if storey.is_a("IfcBuildingStorey"):
                        level = storey.Name
                        elevation = storey.Elevation
                        parameters["_storey"] = level
                        parameters["_elevation"] = elevation
        except:
            pass

        # --- Extract wall height from geometry ---
        try:
            if element.is_a("IfcWall") or element.is_a("IfcWallStandardCase"):
                for rep in element.Representation.Representations:
                    for item in rep.Items:
                        if item.is_a("IfcExtrudedAreaSolid"):
                            parameters["_height_mm"] = item.Depth
                        elif item.is_a("IfcBooleanClippingResult"):
                            operand = item.FirstOperand
                            if operand.is_a("IfcExtrudedAreaSolid"):
                                parameters["_height_mm"] = operand.Depth
        except:
            pass

        # --- Extract MEP flow direction and system ---
        try:
            if element.is_a("IfcFlowSegment") or element.is_a("IfcFlowFitting") or element.is_a("IfcFlowTerminal"):
                for rel in element.HasAssignments:
                    if rel.is_a("IfcRelAssignsToGroup"):
                        group = rel.RelatingGroup
                        if group.is_a("IfcSystem"):
                            parameters["_system_name"] = group.Name
                            parameters["_system_type"] = group.is_a()
        except:
            pass

        # Save the base component
        component_id = save_component(
            cursor, project_id, category,
            family_name, type_name, revit_id, parameters
        )

        # --- Extract and save spatial data ---
        pos_x, pos_y, pos_z, rot_x, rot_y, rot_z = extract_placement(element)
        bounding_box = extract_bounding_box(element)
        save_spatial_data(cursor, component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, bounding_box, level, elevation)

        # --- Walls get extra treatment ---
        if element.is_a("IfcWall") or element.is_a("IfcWallStandardCase"):
            layers = []
            thickness = 0
            function = parameters.get("Pset_WallCommon", {}).get("Function", "")

            if element.HasAssociations:
                for rel in element.HasAssociations:
                    if rel.is_a("IfcRelAssociatesMaterial"):
                        material = rel.RelatingMaterial
                        if material.is_a("IfcMaterialLayerSetUsage"):
                            for layer in material.ForLayerSet.MaterialLayers:
                                layer_thickness = layer.LayerThickness or 0
                                thickness += layer_thickness
                                layers.append({
                                    "material": layer.Material.Name if layer.Material else "",
                                    "thickness": layer_thickness
                                })

            save_wall_type(cursor, component_id, thickness, function, layers)

        # --- MEP elements get extra treatment ---
        elif element.is_a("IfcFlowSegment") or element.is_a("IfcFlowFitting") or element.is_a("IfcFlowTerminal"):
            system_type = parameters.get("_system_type", category)
            flow_rate = None
            pressure_drop = None
            connectors = []

            if element.HasPorts:
                for port_rel in element.HasPorts:
                    port = port_rel.RelatingPort
                    connectors.append({
                        "name": port.Name or "",
                        "flow_direction": port.FlowDirection or ""
                    })

            save_mep_system(cursor, component_id, system_type, flow_rate, pressure_drop, connectors)

        counts[category] += 1

    # --- Extract materials ---
    for material in model.by_type("IfcMaterial"):
        save_material(cursor, project_id, material.Name, "", {})

    finish_project(cursor, project_id)
    conn.commit()
    cursor.close()
    conn.close()

    print(f"\nDone! Extracted from {filename}:")
    for category, count in sorted(counts.items()):
        print(f"  {category}: {count}")

# --- Entry point ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python strip.py path/to/file.ifc")
        sys.exit(1)

    extract(sys.argv[1])
