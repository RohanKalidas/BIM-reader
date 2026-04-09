import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.placement as placement_util
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

# --- Save a relationship ---
def save_relationship(cursor, project_id, component_a_id, component_b_id, relationship_type, properties, source="explicit"):
    cursor.execute(
        """
        INSERT INTO relationships (project_id, component_a_id, component_b_id, relationship_type, properties, source)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (project_id, component_a_id, component_b_id, relationship_type, psycopg2.extras.Json(properties), source)
    )

# --- Save a space ---
def save_space(cursor, project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3, parameters):
    cursor.execute(
        """
        INSERT INTO spaces (project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3, parameters)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3, psycopg2.extras.Json(parameters))
    )
    return cursor.fetchone()[0]

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
def save_mep_system(cursor, component_id, system_type, system_name, flow_rate, pressure_drop, connectors):
    cursor.execute(
        """
        INSERT INTO mep_systems (component_id, system_type, system_name, flow_rate, pressure_drop, connectors)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (component_id, system_type, system_name, flow_rate, pressure_drop, psycopg2.extras.Json(connectors))
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
        matrix = placement_util.get_local_placement(element.ObjectPlacement)
        pos_x = float(matrix[0][3])
        pos_y = float(matrix[1][3])
        pos_z = float(matrix[2][3])
        rot_x = float(np.degrees(np.arctan2(matrix[2][1], matrix[2][2])))
        rot_y = float(np.degrees(np.arctan2(-matrix[2][0], np.sqrt(matrix[2][1]**2 + matrix[2][2]**2))))
        rot_z = float(np.degrees(np.arctan2(matrix[1][0], matrix[0][0])))
        return pos_x, pos_y, pos_z, rot_x, rot_y, rot_z
    except:
        return None, None, None, None, None, None

# --- Extract bounding box ---
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

# --- Extract spaces/rooms ---
def extract_spaces(model, cursor, project_id, revit_id_to_component_id):
    spaces = {}
    for space in model.by_type("IfcSpace"):
        try:
            parameters = {}
            for pset_name, pset in util.get_psets(space).items():
                parameters[pset_name] = pset

            level = None
            elevation = None
            for rel in space.ContainedInStructure:
                if rel.is_a("IfcRelContainedInSpatialStructure"):
                    storey = rel.RelatingStructure
                    if storey.is_a("IfcBuildingStorey"):
                        level = storey.Name
                        elevation = storey.Elevation

            area_m2 = None
            volume_m3 = None
            for pset_name, pset in parameters.items():
                if "area" in pset_name.lower() or "qto" in pset_name.lower():
                    area_m2 = pset.get("NetFloorArea") or pset.get("GrossFloorArea") or area_m2
                    volume_m3 = pset.get("NetVolume") or pset.get("GrossVolume") or volume_m3

            space_id = save_space(
                cursor, project_id, space.GlobalId,
                space.Name or "", space.LongName or "",
                level, elevation, area_m2, volume_m3, parameters
            )
            spaces[space.GlobalId] = space_id

        except Exception as e:
            pass

    return spaces

# --- Extract explicit relationships from IFC ---
def extract_relationships(model, cursor, project_id, revit_id_to_component_id):
    rel_count = defaultdict(int)

    # IfcRelConnectsElements — direct physical connections
    for rel in model.by_type("IfcRelConnectsElements"):
        try:
            a = rel.RelatingElement
            b = rel.RelatedElement
            if a.GlobalId in revit_id_to_component_id and b.GlobalId in revit_id_to_component_id:
                id_a = revit_id_to_component_id[a.GlobalId]
                id_b = revit_id_to_component_id[b.GlobalId]
                props = {}
                if hasattr(rel, "ConnectionGeometry") and rel.ConnectionGeometry:
                    props["has_connection_geometry"] = True
                save_relationship(cursor, project_id, id_a, id_b, "CONNECTS_TO", props)
                rel_count["CONNECTS_TO"] += 1
        except:
            pass

    # IfcRelFillsElement — doors/windows filling openings
    for rel in model.by_type("IfcRelFillsElement"):
        try:
            opening = rel.RelatingOpeningElement
            filling = rel.RelatedBuildingElement
            if filling.GlobalId in revit_id_to_component_id:
                id_filling = revit_id_to_component_id[filling.GlobalId]
                # Find the wall that has this opening
                for void_rel in opening.VoidsElements:
                    wall = void_rel.RelatingBuildingElement
                    if wall.GlobalId in revit_id_to_component_id:
                        id_wall = revit_id_to_component_id[wall.GlobalId]
                        save_relationship(cursor, project_id, id_filling, id_wall, "FILLS", {
                            "opening_id": opening.GlobalId
                        })
                        rel_count["FILLS"] += 1
        except:
            pass

    # IfcRelVoidsElement — openings cut into walls/slabs
    for rel in model.by_type("IfcRelVoidsElement"):
        try:
            wall = rel.RelatingBuildingElement
            opening = rel.RelatedOpeningElement
            if wall.GlobalId in revit_id_to_component_id:
                id_wall = revit_id_to_component_id[wall.GlobalId]
                save_relationship(cursor, project_id, id_wall, id_wall, "VOIDS", {
                    "opening_id": opening.GlobalId,
                    "opening_name": opening.Name or ""
                })
                rel_count["VOIDS"] += 1
        except:
            pass

    # IfcRelSpaceBoundary — elements bounding spaces
    for rel in model.by_type("IfcRelSpaceBoundary"):
        try:
            space = rel.RelatingSpace
            element = rel.RelatedBuildingElement
            if element and element.GlobalId in revit_id_to_component_id:
                id_element = revit_id_to_component_id[element.GlobalId]
                save_relationship(cursor, project_id, id_element, id_element, "BOUNDS", {
                    "space_id": space.GlobalId,
                    "space_name": space.Name or "",
                    "boundary_type": str(rel.PhysicalOrVirtualBoundary) if hasattr(rel, "PhysicalOrVirtualBoundary") else ""
                })
                rel_count["BOUNDS"] += 1
        except:
            pass

    # IfcRelContainedInSpatialStructure — elements in spaces
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        try:
            structure = rel.RelatingStructure
            if structure.is_a("IfcSpace"):
                for element in rel.RelatedElements:
                    if element.GlobalId in revit_id_to_component_id:
                        id_element = revit_id_to_component_id[element.GlobalId]
                        save_relationship(cursor, project_id, id_element, id_element, "CONTAINS", {
                            "space_id": structure.GlobalId,
                            "space_name": structure.Name or ""
                        })
                        rel_count["CONTAINS"] += 1
        except:
            pass

    # IfcRelConnectsPorts — MEP port to port connections
    for rel in model.by_type("IfcRelConnectsPorts"):
        try:
            port_a = rel.RelatingPort
            port_b = rel.RelatedPort

            element_a = None
            element_b = None

            for port_rel in port_a.ContainedIn:
                element_a = port_rel.RelatedElement
            for port_rel in port_b.ContainedIn:
                element_b = port_rel.RelatedElement

            if element_a and element_b:
                if element_a.GlobalId in revit_id_to_component_id and element_b.GlobalId in revit_id_to_component_id:
                    id_a = revit_id_to_component_id[element_a.GlobalId]
                    id_b = revit_id_to_component_id[element_b.GlobalId]
                    save_relationship(cursor, project_id, id_a, id_b, "FLOWS_INTO", {
                        "port_a": port_a.Name or "",
                        "port_b": port_b.Name or "",
                        "flow_direction": str(port_a.FlowDirection) if hasattr(port_a, "FlowDirection") else ""
                    })
                    rel_count["FLOWS_INTO"] += 1
        except:
            pass

    # IfcRelAggregates — compound elements and their parts
    for rel in model.by_type("IfcRelAggregates"):
        try:
            whole = rel.RelatingObject
            for part in rel.RelatedObjects:
                if hasattr(whole, "GlobalId") and hasattr(part, "GlobalId"):
                    if whole.GlobalId in revit_id_to_component_id and part.GlobalId in revit_id_to_component_id:
                        id_whole = revit_id_to_component_id[whole.GlobalId]
                        id_part = revit_id_to_component_id[part.GlobalId]
                        save_relationship(cursor, project_id, id_part, id_whole, "PART_OF", {})
                        rel_count["PART_OF"] += 1
        except:
            pass

    # IfcRelAssignsToGroup — MEP system assignments
    for rel in model.by_type("IfcRelAssignsToGroup"):
        try:
            group = rel.RelatingGroup
            if group.is_a("IfcSystem"):
                for element in rel.RelatedObjects:
                    if hasattr(element, "GlobalId") and element.GlobalId in revit_id_to_component_id:
                        id_element = revit_id_to_component_id[element.GlobalId]
                        save_relationship(cursor, project_id, id_element, id_element, "ASSIGNED_TO", {
                            "system_name": group.Name or "",
                            "system_type": group.is_a()
                        })
                        rel_count["ASSIGNED_TO"] += 1
        except:
            pass

    # IfcRelCoversBldgElements — coverings
    for rel in model.by_type("IfcRelCoversBldgElements"):
        try:
            element = rel.RelatingBuildingElement
            for covering in rel.RelatedCoverings:
                if element.GlobalId in revit_id_to_component_id and covering.GlobalId in revit_id_to_component_id:
                    id_element = revit_id_to_component_id[element.GlobalId]
                    id_covering = revit_id_to_component_id[covering.GlobalId]
                    save_relationship(cursor, project_id, id_covering, id_element, "COVERED_BY", {})
                    rel_count["COVERED_BY"] += 1
        except:
            pass

    return rel_count

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
    revit_id_to_component_id = {}

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

        # --- Extract material ---
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
                    elif mat.is_a("IfcMaterialConstituentSet"):
                        constituents = []
                        for constituent in mat.MaterialConstituents:
                            constituents.append({
                                "name": constituent.Name or "",
                                "material": constituent.Material.Name if constituent.Material else ""
                            })
                        parameters["_material_constituents"] = constituents
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

        # --- Extract MEP system info ---
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

        revit_id_to_component_id[revit_id] = component_id

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
            system_name = parameters.get("_system_name", "")
            flow_rate = None
            pressure_drop = None
            connectors = []

            if element.HasPorts:
                for port_rel in element.HasPorts:
                    port = port_rel.RelatingPort
                    connectors.append({
                        "name": port.Name or "",
                        "flow_direction": str(port.FlowDirection) if hasattr(port, "FlowDirection") else ""
                    })

            save_mep_system(cursor, component_id, system_type, system_name, flow_rate, pressure_drop, connectors)

        counts[category] += 1

    # --- Extract spaces ---
    print("Extracting spaces...")
    spaces = extract_spaces(model, cursor, project_id, revit_id_to_component_id)
    print(f"  Found {len(spaces)} spaces")

    # --- Extract explicit relationships ---
    print("Extracting relationships...")
    rel_count = extract_relationships(model, cursor, project_id, revit_id_to_component_id)
    for rel_type, count in rel_count.items():
        print(f"  {rel_type}: {count}")

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
        
    return project_id
    
# --- Entry point ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 strip.py path/to/file.ifc")
        sys.exit(1)

    extract(sys.argv[1])
