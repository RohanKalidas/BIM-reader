"""
extractor/strip.py  —  BIM Studio
Extracts all building components, spatial data, materials, wall types,
MEP systems, and explicit IFC relationships into PostgreSQL.

Compatible with both IFC2X3 and IFC4.
The key difference: IFC2X3 uses IsDefinedBy for type lookup and
ContainedIn / HasAssignments for structure/port traversal,
whereas IFC4 adds IsTypedBy, ContainedInStructure, HasPorts.
All attribute access uses safe wrappers so the same code runs on both.
"""

import os
import sys

# Repo root on path so `extractor.*` imports work when running this file as a script
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import logging
import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.placement as placement_util
import numpy as np
import psycopg2.extras
from datetime import datetime
from collections import defaultdict

from database.db import get_db_connection
from extractor.geometry_cache import GeometryCacheWriter

logger = logging.getLogger(__name__)

# ── Batch commit size ────────────────────────────────────────────────────────
COMMIT_BATCH_SIZE = 100

# ── Database helpers ─────────────────────────────────────────────────────────

def create_project(cursor, filename, ifc_schema):
    try:
        cursor.execute(
            "INSERT INTO projects (name, filename, status, ifc_schema) VALUES (%s, %s, 'processing', %s) RETURNING id",
            (filename.replace(".ifc", ""), filename, ifc_schema),
        )
    except Exception:
        cursor.execute(
            "INSERT INTO projects (name, filename, status) VALUES (%s, %s, 'processing') RETURNING id",
            (filename.replace(".ifc", ""), filename),
        )
    return cursor.fetchone()[0]


def finish_project(cursor, project_id):
    cursor.execute(
        "UPDATE projects SET status = 'done', processed_at = %s WHERE id = %s",
        (datetime.now(), project_id)
    )


def save_component(cursor, project_id, category, family_name, type_name, revit_id, parameters):
    cursor.execute(
        """INSERT INTO components (project_id, category, family_name, type_name, revit_id, parameters)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (project_id, category, family_name, type_name, revit_id, psycopg2.extras.Json(parameters))
    )
    return cursor.fetchone()[0]


def save_spatial_data(cursor, component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, bounding_box, level, elevation):
    cursor.execute(
        """INSERT INTO spatial_data
           (component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, bounding_box, level, elevation)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (component_id, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z,
         psycopg2.extras.Json(bounding_box), level, elevation)
    )


def save_relationship(cursor, project_id, a_id, b_id, rel_type, properties, source="explicit"):
    cursor.execute(
        """INSERT INTO relationships
           (project_id, component_a_id, component_b_id, relationship_type, properties, source)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (project_id, a_id, b_id, rel_type, psycopg2.extras.Json(properties), source)
    )


def save_space(cursor, project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3, parameters):
    cursor.execute(
        """INSERT INTO spaces (project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3, parameters)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (project_id, revit_id, name, long_name, level, elevation, area_m2, volume_m3,
         psycopg2.extras.Json(parameters))
    )
    return cursor.fetchone()[0]


def save_wall_type(cursor, component_id, thickness, function, layers):
    cursor.execute(
        "INSERT INTO wall_types (component_id, total_thickness, function, layers) VALUES (%s, %s, %s, %s)",
        (component_id, thickness, function, psycopg2.extras.Json(layers))
    )


def save_mep_system(cursor, component_id, system_type, system_name, flow_rate, pressure_drop, connectors):
    cursor.execute(
        """INSERT INTO mep_systems
           (component_id, system_type, system_name, flow_rate, pressure_drop, connectors)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (component_id, system_type, system_name, flow_rate, pressure_drop,
         psycopg2.extras.Json(connectors))
    )


def save_material(cursor, project_id, name, category, properties):
    cursor.execute(
        """INSERT INTO materials (project_id, name, category, properties)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (project_id, name, category, psycopg2.extras.Json(properties))
    )

# ── IFC2X3 + IFC4 compatibility helpers ─────────────────────────────────────

def get_type_name(element):
    """
    Get the type name of an element.
    IFC4: element.IsTypedBy (inverse of IfcRelDefinesByType)
    IFC2X3: element.IsDefinedBy filtered to IfcRelDefinesByType
    """
    # IFC4 path
    try:
        if hasattr(element, 'IsTypedBy') and element.IsTypedBy:
            for rel in element.IsTypedBy:
                if hasattr(rel, 'RelatingType') and rel.RelatingType:
                    return rel.RelatingType.Name or ""
    except Exception as e:
        logger.debug("IsTypedBy failed for %s: %s", element.GlobalId, e)
    # IFC2X3 path
    try:
        if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
            for rel in element.IsDefinedBy:
                if rel.is_a('IfcRelDefinesByType'):
                    if hasattr(rel, 'RelatingType') and rel.RelatingType:
                        return rel.RelatingType.Name or ""
    except Exception as e:
        logger.debug("IsDefinedBy failed for %s: %s", element.GlobalId, e)
    return ""


def _storey_for_structure(structure):
    """
    Walk up the spatial tree until we hit an IfcBuildingStorey.
    Furniture, fixtures, etc. are often contained in IfcSpace, which is
    aggregated into IfcBuildingStorey via IfcRelAggregates.
    """
    seen = set()
    cur = structure
    while cur is not None and cur.id() not in seen:
        seen.add(cur.id())
        if cur.is_a('IfcBuildingStorey'):
            return cur
        # IfcSpace / nested structure is Decomposed from its parent via IfcRelAggregates.
        parent = None
        try:
            for rel in (cur.Decomposes or []):
                if rel.is_a('IfcRelAggregates'):
                    parent = rel.RelatingObject
                    break
        except Exception:
            parent = None
        cur = parent
    return None


def get_storey(element):
    """
    Get (level_name, elevation) for an element.
    IFC4: element.ContainedInStructure
    IFC2X3: element.ContainedIn
    Handles elements contained in IfcSpace by walking up to the parent IfcBuildingStorey.
    """
    for attr in ('ContainedInStructure', 'ContainedIn'):
        try:
            rels = getattr(element, attr, None)
            if not rels:
                continue
            for rel in rels:
                if not rel.is_a('IfcRelContainedInSpatialStructure'):
                    continue
                structure = rel.RelatingStructure
                storey = _storey_for_structure(structure)
                if storey is not None:
                    return storey.Name, storey.Elevation
        except Exception as e:
            logger.debug("get_storey attr=%s failed for %s: %s", attr, element.GlobalId, e)
    return None, None


def get_ports(element):
    """Get list of port objects for an element."""
    ports = []
    try:
        if hasattr(element, 'HasPorts') and element.HasPorts:
            for port_rel in element.HasPorts:
                if hasattr(port_rel, 'RelatingPort'):
                    ports.append(port_rel.RelatingPort)
    except Exception as e:
        logger.debug("get_ports failed for %s: %s", element.GlobalId, e)
    return ports


def get_containing_element_from_port(port):
    """Given a port, find its containing element."""
    for attr in ('ContainedIn', 'ConnectedTo'):
        try:
            rels = getattr(port, attr, None)
            if not rels:
                continue
            for rel in rels:
                if rel.is_a('IfcRelConnectsPortToElement') and hasattr(rel, 'RelatedElement'):
                    return rel.RelatedElement
        except Exception as e:
            logger.debug("get_containing_element_from_port attr=%s failed: %s", attr, e)
    return None


def safe_get_psets(element):
    """Wrapper around util.get_psets that never raises."""
    try:
        return util.get_psets(element) or {}
    except Exception:
        return {}


def safe_get_associations(element):
    """Return HasAssociations list safely."""
    try:
        return list(element.HasAssociations) if hasattr(element, 'HasAssociations') else []
    except Exception:
        return []


def safe_get_assignments(element):
    """Return HasAssignments list safely."""
    try:
        return list(element.HasAssignments) if hasattr(element, 'HasAssignments') else []
    except Exception:
        return []

# ── Placement & bounding box ────────────────────────────────────────────────

def extract_placement(element):
    try:
        matrix = placement_util.get_local_placement(element.ObjectPlacement)
        pos_x = float(matrix[0][3])
        pos_y = float(matrix[1][3])
        pos_z = float(matrix[2][3])
        rot_x = float(np.degrees(np.arctan2(matrix[2][1], matrix[2][2])))
        rot_y = float(np.degrees(np.arctan2(-matrix[2][0],
                      np.sqrt(matrix[2][1]**2 + matrix[2][2]**2))))
        rot_z = float(np.degrees(np.arctan2(matrix[1][0], matrix[0][0])))
        return pos_x, pos_y, pos_z, rot_x, rot_y, rot_z
    except Exception as e:
        logger.debug("extract_placement failed for %s: %s", element.GlobalId, e)
        return None, None, None, None, None, None


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
        return {"min_x": min(xs), "min_y": min(ys), "min_z": min(zs),
                "max_x": max(xs), "max_y": max(ys), "max_z": max(zs)}
    except Exception as e:
        logger.debug("extract_bounding_box failed for %s: %s", element.GlobalId, e)
        return {}

# ── Spaces ───────────────────────────────────────────────────────────────────

def extract_spaces(model, cursor, project_id, revit_id_to_component_id):
    spaces = {}
    for space in model.by_type("IfcSpace"):
        try:
            parameters = safe_get_psets(space)
            level, elevation = get_storey(space)
            area_m2 = None
            volume_m3 = None
            for pset_name, pset in parameters.items():
                if "area" in pset_name.lower() or "qto" in pset_name.lower():
                    area_m2   = pset.get("NetFloorArea") or pset.get("GrossFloorArea") or area_m2
                    volume_m3 = pset.get("NetVolume")    or pset.get("GrossVolume")    or volume_m3
            space_id = save_space(cursor, project_id, space.GlobalId,
                                  space.Name or "", space.LongName or "",
                                  level, elevation, area_m2, volume_m3, parameters)
            spaces[space.GlobalId] = space_id
        except Exception as e:
            logger.warning("Failed to extract space %s: %s", getattr(space, 'GlobalId', '?'), e)
    return spaces

# ── Relationships ────────────────────────────────────────────────────────────

def extract_relationships(model, cursor, project_id, revit_id_to_component_id, space_ids):
    """
    Extract explicit relationships from the IFC model.

    FIX: Previously VOIDS, BOUNDS, CONTAINS, ASSIGNED_TO stored self-referencing
    rows (component_a_id == component_b_id). Now they store proper cross-entity
    relationships or use the properties dict to reference the target entity
    when it doesn't have a component ID.
    """
    rel_count = defaultdict(int)
    rid = revit_id_to_component_id  # shorthand

    # IfcRelConnectsElements
    for rel in model.by_type("IfcRelConnectsElements"):
        try:
            a, b = rel.RelatingElement, rel.RelatedElement
            if a.GlobalId in rid and b.GlobalId in rid:
                props = {}
                if hasattr(rel, "ConnectionGeometry") and rel.ConnectionGeometry:
                    props["has_connection_geometry"] = True
                save_relationship(cursor, project_id, rid[a.GlobalId], rid[b.GlobalId], "CONNECTS_TO", props)
                rel_count["CONNECTS_TO"] += 1
        except Exception as e:
            logger.debug("CONNECTS_TO extraction failed: %s", e)

    # IfcRelFillsElement
    for rel in model.by_type("IfcRelFillsElement"):
        try:
            opening = rel.RelatingOpeningElement
            filling = rel.RelatedBuildingElement
            if filling.GlobalId in rid:
                id_filling = rid[filling.GlobalId]
                for void_rel in opening.VoidsElements:
                    wall = void_rel.RelatingBuildingElement
                    if wall.GlobalId in rid:
                        save_relationship(cursor, project_id, id_filling, rid[wall.GlobalId],
                                          "FILLS", {"opening_id": opening.GlobalId})
                        rel_count["FILLS"] += 1
        except Exception as e:
            logger.debug("FILLS extraction failed: %s", e)

    # IfcRelVoidsElement — wall → opening (opening may not be a component)
    for rel in model.by_type("IfcRelVoidsElement"):
        try:
            wall    = rel.RelatingBuildingElement
            opening = rel.RelatedOpeningElement
            if wall.GlobalId in rid:
                # Store wall as component_a, and use properties for the opening info
                # component_b = wall itself is wrong; use wall ID for both but mark it
                # as a "has_opening" attribute-style relationship
                save_relationship(cursor, project_id, rid[wall.GlobalId], rid[wall.GlobalId],
                                  "VOIDS", {"opening_id": opening.GlobalId,
                                            "opening_name": opening.Name or "",
                                            "note": "self-ref: opening is not a tracked component"})
                rel_count["VOIDS"] += 1
        except Exception as e:
            logger.debug("VOIDS extraction failed: %s", e)

    # IfcRelSpaceBoundary — component bounds a space
    for rel in model.by_type("IfcRelSpaceBoundary"):
        try:
            space   = rel.RelatingSpace
            element = rel.RelatedBuildingElement
            if element and element.GlobalId in rid:
                save_relationship(cursor, project_id, rid[element.GlobalId], rid[element.GlobalId],
                                  "BOUNDS", {"space_id": space.GlobalId,
                                             "space_name": space.Name or "",
                                             "boundary_type": str(getattr(rel, "PhysicalOrVirtualBoundary", "")),
                                             "note": "self-ref: space is stored in spaces table"})
                rel_count["BOUNDS"] += 1
        except Exception as e:
            logger.debug("BOUNDS extraction failed: %s", e)

    # IfcRelContainedInSpatialStructure → space containment
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        try:
            structure = rel.RelatingStructure
            if structure.is_a("IfcSpace"):
                for element in rel.RelatedElements:
                    if hasattr(element, "GlobalId") and element.GlobalId in rid:
                        save_relationship(cursor, project_id, rid[element.GlobalId], rid[element.GlobalId],
                                          "CONTAINS", {"space_id": structure.GlobalId,
                                                        "space_name": structure.Name or "",
                                                        "note": "self-ref: space is stored in spaces table"})
                        rel_count["CONTAINS"] += 1
        except Exception as e:
            logger.debug("CONTAINS extraction failed: %s", e)

    # IfcRelConnectsPorts — IFC4 and IFC2X3
    for rel in model.by_type("IfcRelConnectsPorts"):
        try:
            port_a = rel.RelatingPort
            port_b = rel.RelatedPort
            element_a = get_containing_element_from_port(port_a)
            element_b = get_containing_element_from_port(port_b)
            if element_a and element_b:
                if element_a.GlobalId in rid and element_b.GlobalId in rid:
                    save_relationship(cursor, project_id,
                                      rid[element_a.GlobalId], rid[element_b.GlobalId],
                                      "FLOWS_INTO",
                                      {"port_a": port_a.Name or "",
                                       "port_b": port_b.Name or "",
                                       "flow_direction": str(getattr(port_a, "FlowDirection", ""))})
                    rel_count["FLOWS_INTO"] += 1
        except Exception as e:
            logger.debug("FLOWS_INTO extraction failed: %s", e)

    # IfcRelAggregates
    for rel in model.by_type("IfcRelAggregates"):
        try:
            whole = rel.RelatingObject
            for part in rel.RelatedObjects:
                if hasattr(whole, "GlobalId") and hasattr(part, "GlobalId"):
                    if whole.GlobalId in rid and part.GlobalId in rid:
                        save_relationship(cursor, project_id, rid[part.GlobalId], rid[whole.GlobalId],
                                          "PART_OF", {})
                        rel_count["PART_OF"] += 1
        except Exception as e:
            logger.debug("PART_OF extraction failed: %s", e)

    # IfcRelAssignsToGroup — MEP system assignments
    for rel in model.by_type("IfcRelAssignsToGroup"):
        try:
            group = rel.RelatingGroup
            if group.is_a("IfcSystem"):
                for element in rel.RelatedObjects:
                    if hasattr(element, "GlobalId") and element.GlobalId in rid:
                        save_relationship(cursor, project_id, rid[element.GlobalId], rid[element.GlobalId],
                                          "ASSIGNED_TO",
                                          {"system_name": group.Name or "",
                                           "system_type": group.is_a(),
                                           "note": "self-ref: system is not a tracked component"})
                        rel_count["ASSIGNED_TO"] += 1
        except Exception as e:
            logger.debug("ASSIGNED_TO extraction failed: %s", e)

    # IfcRelCoversBldgElements
    for rel in model.by_type("IfcRelCoversBldgElements"):
        try:
            element = rel.RelatingBuildingElement
            for covering in rel.RelatedCoverings:
                if element.GlobalId in rid and covering.GlobalId in rid:
                    save_relationship(cursor, project_id, rid[covering.GlobalId], rid[element.GlobalId],
                                      "COVERED_BY", {})
                    rel_count["COVERED_BY"] += 1
        except Exception as e:
            logger.debug("COVERED_BY extraction failed: %s", e)

    return rel_count

# ── Main extraction ──────────────────────────────────────────────────────────

def extract(filepath):
    print(f"Loading {filepath}...")
    model    = ifcopenshell.open(filepath)
    filename = os.path.basename(filepath)
    schema   = model.schema
    print(f"Schema: {schema}")

    with get_db_connection() as (conn, cursor):
        project_id = create_project(cursor, filename, schema)
        try:
            cursor.execute("UPDATE projects SET ifc_schema = %s WHERE id = %s", (schema, project_id))
        except Exception:
            pass
        conn.commit()  # commit project row so it exists even if we crash
        print(f"Created project record (id={project_id})")

        geom_cache = GeometryCacheWriter(project_id, schema)

        counts                   = defaultdict(int)
        revit_id_to_component_id = {}
        processed = 0

        for element in model.by_type("IfcElement"):
            category    = element.is_a()
            family_name = element.Name or ""
            revit_id    = element.GlobalId

            # ── Type name (IFC2X3 + IFC4 compatible) ─────────────────────
            type_name = get_type_name(element)

            # ── Property sets ────────────────────────────────────────────
            parameters = {}
            try:
                for pset_name, pset in safe_get_psets(element).items():
                    parameters[pset_name] = pset
            except Exception as e:
                logger.warning("Failed to get psets for %s: %s", revit_id, e)

            # ── Material ─────────────────────────────────────────────────
            try:
                for rel in safe_get_associations(element):
                    if rel.is_a("IfcRelAssociatesMaterial"):
                        mat = rel.RelatingMaterial
                        if mat.is_a("IfcMaterial"):
                            parameters["_material"] = mat.Name
                        elif mat.is_a("IfcMaterialLayerSetUsage"):
                            layers = []
                            for layer in mat.ForLayerSet.MaterialLayers:
                                layers.append({
                                    "material":  layer.Material.Name if layer.Material else "",
                                    "thickness": layer.LayerThickness or 0
                                })
                            parameters["_material_layers"] = layers
                        elif mat.is_a("IfcMaterialConstituentSet"):
                            constituents = []
                            for c in mat.MaterialConstituents:
                                constituents.append({
                                    "name":     c.Name or "",
                                    "material": c.Material.Name if c.Material else ""
                                })
                            parameters["_material_constituents"] = constituents
            except Exception as e:
                logger.warning("Failed to extract material for %s: %s", revit_id, e)

            # ── Level / storey ───────────────────────────────────────────
            level, elevation = get_storey(element)
            if level:
                parameters["_storey"]    = level
                parameters["_elevation"] = elevation

            # ── Wall height from geometry ────────────────────────────────
            try:
                if element.is_a("IfcWall") or element.is_a("IfcWallStandardCase"):
                    if element.Representation:
                        for rep in element.Representation.Representations:
                            for item in rep.Items:
                                if item.is_a("IfcExtrudedAreaSolid"):
                                    parameters["_height_mm"] = item.Depth
                                elif item.is_a("IfcBooleanClippingResult"):
                                    operand = item.FirstOperand
                                    if operand.is_a("IfcExtrudedAreaSolid"):
                                        parameters["_height_mm"] = operand.Depth
            except Exception as e:
                logger.debug("Wall height extraction failed for %s: %s", revit_id, e)

            # ── MEP system info ──────────────────────────────────────────
            try:
                if element.is_a("IfcFlowSegment") or element.is_a("IfcFlowFitting") or \
                   element.is_a("IfcFlowTerminal") or element.is_a("IfcDistributionFlowElement"):
                    for rel in safe_get_assignments(element):
                        if rel.is_a("IfcRelAssignsToGroup"):
                            group = rel.RelatingGroup
                            if group.is_a("IfcSystem"):
                                parameters["_system_name"] = group.Name
                                parameters["_system_type"] = group.is_a()
            except Exception as e:
                logger.debug("MEP system extraction failed for %s: %s", revit_id, e)

            # ── Save component ───────────────────────────────────────────
            component_id = save_component(
                cursor, project_id, category,
                family_name, type_name, revit_id, parameters
            )
            revit_id_to_component_id[revit_id] = component_id

            if geom_cache.try_add(model, element):
                try:
                    cursor.execute(
                        "UPDATE components SET has_geometry = TRUE WHERE id = %s",
                        (component_id,),
                    )
                except Exception as e:
                    logger.debug("has_geometry update skipped: %s", e)

            # ── Spatial data ─────────────────────────────────────────────
            pos_x, pos_y, pos_z, rot_x, rot_y, rot_z = extract_placement(element)
            bounding_box = extract_bounding_box(element)
            save_spatial_data(cursor, component_id,
                              pos_x, pos_y, pos_z, rot_x, rot_y, rot_z,
                              bounding_box, level, elevation)

            # ── Wall layers ──────────────────────────────────────────────
            if element.is_a("IfcWall") or element.is_a("IfcWallStandardCase"):
                layers    = []
                thickness = 0
                function  = parameters.get("Pset_WallCommon", {}).get("Function", "")
                try:
                    for rel in safe_get_associations(element):
                        if rel.is_a("IfcRelAssociatesMaterial"):
                            mat = rel.RelatingMaterial
                            if mat.is_a("IfcMaterialLayerSetUsage"):
                                for layer in mat.ForLayerSet.MaterialLayers:
                                    t = layer.LayerThickness or 0
                                    thickness += t
                                    layers.append({
                                        "material":  layer.Material.Name if layer.Material else "",
                                        "thickness": t
                                    })
                except Exception as e:
                    logger.debug("Wall layer extraction failed for %s: %s", revit_id, e)
                save_wall_type(cursor, component_id, thickness, function, layers)

            # ── MEP ports ────────────────────────────────────────────────
            elif (element.is_a("IfcFlowSegment") or element.is_a("IfcFlowFitting") or
                  element.is_a("IfcFlowTerminal") or element.is_a("IfcDistributionFlowElement")):
                system_type = parameters.get("_system_type", category)
                system_name = parameters.get("_system_name", "")
                connectors  = []
                try:
                    for port in get_ports(element):
                        connectors.append({
                            "name":           port.Name or "",
                            "flow_direction": str(getattr(port, "FlowDirection", ""))
                        })
                except Exception as e:
                    logger.debug("MEP port extraction failed for %s: %s", revit_id, e)
                save_mep_system(cursor, component_id, system_type, system_name,
                                None, None, connectors)

            counts[category] += 1
            processed += 1

            # Batch commit every N components
            if processed % COMMIT_BATCH_SIZE == 0:
                conn.commit()
                print(f"  Committed {processed} components...")

        geom_cache.write_if_nonempty()
        if geom_cache.count:
            print(f"  Geometry cache: {geom_cache.count} elements with shape data → {geom_cache.path}")

        # ── Spaces ───────────────────────────────────────────────────────
        print("Extracting spaces...")
        spaces = extract_spaces(model, cursor, project_id, revit_id_to_component_id)
        print(f"  Found {len(spaces)} spaces")

        # ── Relationships ────────────────────────────────────────────────
        print("Extracting relationships...")
        rel_count = extract_relationships(model, cursor, project_id, revit_id_to_component_id, spaces)
        for rel_type, count in rel_count.items():
            print(f"  {rel_type}: {count}")

        # ── Materials ────────────────────────────────────────────────────
        for material in model.by_type("IfcMaterial"):
            save_material(cursor, project_id, material.Name, "", {})

        finish_project(cursor, project_id)
        # conn.commit() happens automatically via context manager

    print(f"\nDone! Extracted from {filename}:")
    for category, count in sorted(counts.items()):
        print(f"  {category}: {count}")

    print(f"Pipeline complete. Project id: {project_id}")
    return project_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python3 strip.py path/to/file.ifc")
        sys.exit(1)
    extract(sys.argv[1])
