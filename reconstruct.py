"""
reconstruct.py — Reconstructs an IFC file from the PostgreSQL database.

Uses centralized database module with context managers.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import math
import json
import logging
import psycopg2.extras
import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.util.placement as placement_util
import numpy as np
from datetime import datetime

from database.db import get_db_connection
from extractor.geometry_cache import (
    open_geometry_cache,
    copy_cached_geometry_to_element,
    copy_unit_assignment_to_model,
    default_unit_assignment,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database loaders
# ---------------------------------------------------------------------------

def load_project(cursor, project_id):
    cursor.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
    return cursor.fetchone()


def load_components(cursor, project_id):
    """Full component + spatial data, ordered bottom-up so floors are created first."""
    sql = """
        SELECT
            c.id, c.category, c.family_name, c.type_name,
            c.revit_id, c.parameters,
            COALESCE(c.has_geometry, FALSE) AS has_geometry,
            c.width_mm, c.height_mm, c.length_mm,
            c.area_m2, c.volume_m3,
            s.pos_x, s.pos_y, s.pos_z,
            s.rot_x, s.rot_y, s.rot_z,
            s.bounding_box, s.level, s.elevation
        FROM components c
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE c.project_id = %s
        ORDER BY COALESCE(s.pos_z, 0), c.category
    """
    sql_legacy = """
        SELECT
            c.id, c.category, c.family_name, c.type_name,
            c.revit_id, c.parameters,
            c.width_mm, c.height_mm, c.length_mm,
            c.area_m2, c.volume_m3,
            s.pos_x, s.pos_y, s.pos_z,
            s.rot_x, s.rot_y, s.rot_z,
            s.bounding_box, s.level, s.elevation
        FROM components c
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE c.project_id = %s
        ORDER BY COALESCE(s.pos_z, 0), c.category
    """
    try:
        cursor.execute(sql, (project_id,))
    except Exception:
        cursor.execute(sql_legacy, (project_id,))
        rows = cursor.fetchall()
        for r in rows:
            r["has_geometry"] = False
        return rows
    return cursor.fetchall()


def load_relationships(cursor, project_id):
    cursor.execute("""
        SELECT component_a_id, component_b_id, relationship_type, properties, source
        FROM relationships
        WHERE project_id = %s
    """, (project_id,))
    return cursor.fetchall()


def load_wall_types(cursor, project_id):
    """Returns {component_id: {total_thickness, function, layers}}."""
    cursor.execute("""
        SELECT w.component_id, w.total_thickness, w.function, w.layers
        FROM wall_types w
        JOIN components c ON c.id = w.component_id
        WHERE c.project_id = %s
    """, (project_id,))
    return {row["component_id"]: row for row in cursor.fetchall()}


def load_spaces(cursor, project_id):
    cursor.execute("SELECT * FROM spaces WHERE project_id = %s", (project_id,))
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Placement helpers
# ---------------------------------------------------------------------------

def euler_to_matrix(rot_x_deg, rot_y_deg, rot_z_deg):
    """
    Recompose the 3x3 rotation matrix from the Euler angles stored by strip.py.
    strip.py used ZYX convention, so we reconstruct in the same order.
    """
    rx = math.radians(rot_x_deg or 0.0)
    ry = math.radians(rot_y_deg or 0.0)
    rz = math.radians(rot_z_deg or 0.0)

    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)

    # R = Rz * Ry * Rx
    r = [
        [cz*cy,  cz*sy*sx - sz*cx,  cz*sy*cx + sz*sx],
        [sz*cy,  sz*sy*sx + cz*cx,  sz*sy*cx - cz*sx],
        [-sy,    cy*sx,              cy*cx            ],
    ]
    return r


def make_ifc_placement(model, pos_x, pos_y, pos_z, rot_x, rot_y, rot_z, scale=1.0):
    """
    Build an IfcLocalPlacement from stored position and Euler angles.
    `scale` converts DB coordinates (in source file units, typically mm) into
    the output model's length unit (e.g. 0.001 if output is metres and DB is mm).
    """
    x = float(pos_x or 0.0) * scale
    y = float(pos_y or 0.0) * scale
    z = float(pos_z or 0.0) * scale

    r = euler_to_matrix(rot_x, rot_y, rot_z)

    location = model.createIfcCartesianPoint((x, y, z))
    axis     = model.createIfcDirection((r[0][2], r[1][2], r[2][2]))   # Z col
    ref_dir  = model.createIfcDirection((r[0][0], r[1][0], r[2][0]))   # X col

    axis2 = model.createIfcAxis2Placement3D(location, axis, ref_dir)
    return model.createIfcLocalPlacement(None, axis2)


# ---------------------------------------------------------------------------
# Placeholder geometry (viewers need Product.Representation)
# ---------------------------------------------------------------------------

def _to_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


_SI_PREFIX_FACTORS = {
    None: 1.0, "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9,
    "MEGA": 1e6, "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1,
    "DECI": 1e-1, "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6,
    "NANO": 1e-9, "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18,
}


def length_unit_metres_factor(model) -> float:
    """
    Return 1 output-length-unit expressed in metres, e.g. 0.001 for mm, 1.0 for m.
    DB stores values in the source file units (which the output also uses), so to
    keep this number-space consistent we use 1.0 (no conversion) almost always.
    """
    try:
        proj = model.by_type("IfcProject")[0]
        for u in (proj.UnitsInContext.Units or []):
            if not u.is_a("IfcSIUnit"):
                continue
            if u.UnitType != "LENGTHUNIT":
                continue
            prefix = getattr(u, "Prefix", None)
            return _SI_PREFIX_FACTORS.get(prefix, 1.0)
    except Exception:
        pass
    return 1.0


# Cap placeholder boxes so bad bbox / unit metadata cannot create km-scale solids.
# Expressed in metres — actual cap in output units is derived from `length_unit_metres_factor`.
_MAX_PLACEHOLDER_DIM_M = 50.0
_MIN_PLACEHOLDER_DIM_M = 0.05
# Default when no dimension info is available (metres).
_DEFAULT_PLACEHOLDER_DIM_M = 0.2

# These are voids, logical hosts, or non-visible containers. Drawing a placeholder
# box for them is what produces "extra artefacts" (phantom cubes, transparent boxes
# on roofs, discs at the origin). Leave them without a Body representation.
_NO_PLACEHOLDER_CATEGORIES = {
    "IfcOpeningElement",       # voids, not solids
    "IfcCurtainWall",          # host only; real geometry is on IfcMember/IfcPlate
    "IfcSpace",                # abstract volume; placeholders look like stray boxes
    "IfcElementAssembly",      # container for children that carry their own geometry
    "IfcVirtualElement",       # by definition has no physical geometry
    "IfcGrid",
    "IfcAnnotation",
    "IfcOpeningStandardCase",
}


def _dims_in_output_units(comp, unit_m):
    """
    Return (width, depth, height, has_real_dims) in the output file's length unit.
    DB stores width_mm/height_mm/length_mm literally in millimetres, and bounding
    box values in the source IFC's length unit (mm for Revit exports). We convert
    to metres first (internal), then scale into output units.
    `unit_m` = length of 1 output unit expressed in metres (e.g. 0.001 for mm).
    """
    to_m_mm = 0.001
    w_m = (_to_float(comp.get("width_mm")) or 0) * to_m_mm or None
    h_m = (_to_float(comp.get("height_mm")) or 0) * to_m_mm or None
    l_m = (_to_float(comp.get("length_mm")) or 0) * to_m_mm or None

    bb = comp.get("bounding_box") or {}
    bb_w = bb_d = bb_h = None
    if isinstance(bb, dict) and bb:
        # Bounding box is in source file units. Assume mm (typical Revit export).
        # If someone feeds a metres-based IFC this will overestimate 1000x; we cap
        # below so it won't blow out the scene.
        mx, mn = _to_float(bb.get("max_x")), _to_float(bb.get("min_x"))
        my, ny = _to_float(bb.get("max_y")), _to_float(bb.get("min_y"))
        mz, nz = _to_float(bb.get("max_z")), _to_float(bb.get("min_z"))
        if mx is not None and mn is not None:
            bb_w = (mx - mn) * to_m_mm
        if my is not None and ny is not None:
            bb_d = (my - ny) * to_m_mm
        if mz is not None and nz is not None:
            bb_h = (mz - nz) * to_m_mm

    width_m = w_m or bb_w
    depth_m = l_m or bb_d or w_m
    height_m = h_m or bb_h
    has_real_dims = any(v is not None and v > 0 for v in (width_m, depth_m, height_m))

    width_m = width_m or _DEFAULT_PLACEHOLDER_DIM_M
    depth_m = depth_m or _DEFAULT_PLACEHOLDER_DIM_M
    height_m = height_m or _DEFAULT_PLACEHOLDER_DIM_M

    width_m = min(max(width_m, _MIN_PLACEHOLDER_DIM_M), _MAX_PLACEHOLDER_DIM_M)
    depth_m = min(max(depth_m, _MIN_PLACEHOLDER_DIM_M), _MAX_PLACEHOLDER_DIM_M)
    height_m = min(max(height_m, _MIN_PLACEHOLDER_DIM_M), _MAX_PLACEHOLDER_DIM_M)

    # Scale metres into the output model's length unit (e.g. 1m -> 1000 when unit is mm).
    to_out = 1.0 / (unit_m or 1.0)
    return (
        width_m * to_out,
        depth_m * to_out,
        height_m * to_out,
        has_real_dims,
    )


def _dims_from_component(comp):
    """Legacy helper (returns metres). Kept for callers that still want metres."""
    w, d, h, has = _dims_in_output_units(comp, 1.0)
    return w, d, h, has


def _component_dims_m(comp):
    """Backwards-compatible tuple-only dims getter (metres)."""
    w, d, h, _ = _dims_from_component(comp)
    return w, d, h


def should_attach_placeholder(comp) -> bool:
    """
    Decide whether to emit a placeholder box for a component that has no cached
    source geometry. Suppresses the main sources of visible artefacts in viewers.
    """
    category = comp.get("category") or ""
    if category in _NO_PLACEHOLDER_CATEGORIES:
        return False
    # Skip MEP flow elements with no dims — they become tiny random boxes.
    if category.startswith("IfcFlow") or category in {
        "IfcDuctSegment",
        "IfcDuctFitting",
        "IfcPipeSegment",
        "IfcPipeFitting",
        "IfcCableCarrierSegment",
        "IfcCableCarrierFitting",
    }:
        _, _, _, has_dims = _dims_from_component(comp)
        if not has_dims:
            return False
    # If nothing but the 0.2m default would be used AND we have no placement,
    # we'd be planting identical cubes on top of each other at the origin.
    _, _, _, has_dims = _dims_from_component(comp)
    no_placement = comp.get("pos_x") is None
    if no_placement and not has_dims:
        return False
    return True


def create_geometry_context(model, schema="IFC4"):
    """IfcGeometricRepresentationContext + Body context (subcontext on IFC4)."""
    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    wcs = model.create_entity("IfcAxis2Placement3D", Location=origin, Axis=None, RefDirection=None)
    ctx = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Model",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1.0e-5,
        WorldCoordinateSystem=wcs,
        TrueNorth=None,
    )
    if "IFC4" in schema or "IFC4X3" in schema:
        body = model.create_entity(
            "IfcGeometricRepresentationSubContext",
            ContextIdentifier="Body",
            ContextType="Model",
            ParentContext=ctx,
            TargetView="MODEL_VIEW",
        )
    else:
        body = ctx
    return ctx, body


def attach_placeholder_extrusion(model, body_context, element, comp, unit_m=1.0):
    """
    Attach a simple extruded rectangle so the product has Body geometry.
    Dimensions are emitted in the output file's length unit.
    """
    width, depth, height, _ = _dims_in_output_units(comp, unit_m)

    p0 = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0))
    p1 = model.create_entity("IfcCartesianPoint", Coordinates=(width, 0.0))
    p2 = model.create_entity("IfcCartesianPoint", Coordinates=(width, depth))
    p3 = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, depth))
    p4 = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0))
    polyline = model.create_entity("IfcPolyline", Points=[p0, p1, p2, p3, p4])
    profile = model.create_entity("IfcArbitraryClosedProfileDef", ProfileType="AREA", OuterCurve=polyline)

    pos = model.create_entity(
        "IfcAxis2Placement3D",
        Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
    )
    solid = model.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=pos,
        ExtrudedDirection=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=height,
    )
    shape = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_context,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=[solid],
    )
    element.Representation = model.create_entity(
        "IfcProductDefinitionShape",
        Name=None,
        Description=None,
        Representations=[shape],
    )


# ---------------------------------------------------------------------------
# IFC entity factories
# ---------------------------------------------------------------------------

CATEGORY_MAP = {
    # Architectural
    "IfcWall":                  "IfcWall",
    "IfcWallStandardCase":      "IfcWallStandardCase",
    "IfcWallElementedCase":     "IfcWallElementedCase",
    "IfcSlab":                  "IfcSlab",
    "IfcRoof":                  "IfcRoof",
    "IfcDoor":                  "IfcDoor",
    "IfcWindow":                "IfcWindow",
    "IfcStair":                 "IfcStair",
    "IfcStairFlight":           "IfcStairFlight",
    "IfcRailing":               "IfcRailing",
    "IfcCurtainWall":           "IfcCurtainWall",
    "IfcCovering":              "IfcCovering",
    "IfcPlate":                 "IfcPlate",
    "IfcMember":                "IfcMember",
    "IfcOpeningElement":        "IfcOpeningElement",
    # Structural
    "IfcColumn":                "IfcColumn",
    "IfcColumnStandardCase":    "IfcColumnStandardCase",
    "IfcBeam":                  "IfcBeam",
    "IfcBeamStandardCase":      "IfcBeamStandardCase",
    # MEP
    "IfcDuctSegment":           "IfcDuctSegment",
    "IfcDuctFitting":           "IfcDuctFitting",
    "IfcPipeSegment":           "IfcPipeSegment",
    "IfcPipeFitting":           "IfcPipeFitting",
    "IfcAirTerminal":           "IfcAirTerminal",
    "IfcValve":                 "IfcValve",
    "IfcPump":                  "IfcPump",
    "IfcFan":                   "IfcFan",
    "IfcFlowSegment":           "IfcFlowSegment",
    "IfcFlowFitting":           "IfcFlowFitting",
    "IfcFlowTerminal":          "IfcFlowTerminal",
    "IfcFlowController":        "IfcFlowController",
    "IfcFlowMovingDevice":      "IfcFlowMovingDevice",
    "IfcFlowStorageDevice":     "IfcFlowStorageDevice",
    "IfcElectricAppliance":     "IfcElectricAppliance",
    "IfcLightFixture":          "IfcLightFixture",
    "IfcOutlet":                "IfcOutlet",
    "IfcElectricDistributionBoard": "IfcElectricDistributionBoard",
    "IfcDistributionFlowElement": "IfcDistributionFlowElement",
    # Furniture
    "IfcFurnishingElement":     "IfcFurnishingElement",
    "IfcFurniture":             "IfcFurniture",
}


def create_element(model, category, global_id, name, placement):
    """
    Create the correct IfcElement subtype. Falls back to IfcBuildingElementProxy
    for unknown categories.
    """
    ifc_type = CATEGORY_MAP.get(category, "IfcBuildingElementProxy")
    try:
        element = model.create_entity(
            ifc_type,
            GlobalId=global_id,
            Name=name or "",
            ObjectPlacement=placement,
            Representation=None
        )
        return element
    except Exception as e:
        logger.debug("Failed to create %s, falling back to proxy: %s", ifc_type, e)
        element = model.create_entity(
            "IfcBuildingElementProxy",
            GlobalId=global_id,
            Name=name or f"[{category}]",
            ObjectPlacement=placement,
            Representation=None
        )
        return element


# ---------------------------------------------------------------------------
# Property set helpers
# ---------------------------------------------------------------------------

def attach_psets(model, owner_history, element, parameters):
    """
    Re-attach all Pset_* property sets from the stored parameters JSON.
    Skips internal keys (prefixed with _) and ai_enrichment.
    """
    if not parameters:
        return

    for pset_name, pset_data in parameters.items():
        if pset_name.startswith("_") or pset_name == "ai_enrichment":
            continue
        if not isinstance(pset_data, dict):
            continue

        props = []
        for prop_name, prop_value in pset_data.items():
            if prop_value is None:
                continue
            try:
                str_val = str(prop_value)
                nominal = model.create_entity(
                    "IfcLabel", wrappedValue=str_val
                )
                prop = model.create_entity(
                    "IfcPropertySingleValue",
                    Name=prop_name,
                    NominalValue=nominal
                )
                props.append(prop)
            except Exception as e:
                logger.debug("Failed to create property %s: %s", prop_name, e)
                continue

        if not props:
            continue

        try:
            pset = model.create_entity(
                "IfcPropertySet",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_history,
                Name=pset_name,
                HasProperties=props
            )
            model.create_entity(
                "IfcRelDefinesByProperties",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_history,
                RelatedObjects=[element],
                RelatingPropertyDefinition=pset
            )
        except Exception as e:
            logger.debug("Failed to attach pset %s: %s", pset_name, e)
            continue


def attach_wall_layers(model, owner_history, element, wall_type_row):
    """Rebuild IfcMaterialLayerSetUsage from the stored wall_types layers JSON."""
    if not wall_type_row:
        return

    layers_data = wall_type_row.get("layers") or []
    if not layers_data:
        return

    ifc_layers = []
    for layer in layers_data:
        mat_name = layer.get("material") or "Unknown"
        thickness = float(layer.get("thickness") or 0.0)

        material = model.create_entity("IfcMaterial", Name=mat_name)
        ifc_layer = model.create_entity(
            "IfcMaterialLayer",
            Material=material,
            LayerThickness=thickness,
            IsVentilated=False
        )
        ifc_layers.append(ifc_layer)

    if not ifc_layers:
        return

    layer_set = model.create_entity(
        "IfcMaterialLayerSet",
        MaterialLayers=ifc_layers,
        LayerSetName=element.Name or "Wall"
    )
    usage = model.create_entity(
        "IfcMaterialLayerSetUsage",
        ForLayerSet=layer_set,
        LayerSetDirection="AXIS2",
        DirectionSense="POSITIVE",
        OffsetFromReferenceLine=0.0
    )
    model.create_entity(
        "IfcRelAssociatesMaterial",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatedObjects=[element],
        RelatingMaterial=usage
    )


# ---------------------------------------------------------------------------
# Relationship helpers
# ---------------------------------------------------------------------------

def attach_relationships(model, owner_history, relationships, component_map):
    """
    Recreate explicit IFC relationships from the relationships table.
    Skips self-referential rows (VOIDS/BOUNDS/CONTAINS/ASSIGNED_TO workaround).
    """
    counts = {}

    for rel in relationships:
        a_id   = rel["component_a_id"]
        b_id   = rel["component_b_id"]
        rel_type = rel["relationship_type"]
        props  = rel["properties"] or {}

        # Skip self-referential rows (known workaround for entities without component IDs)
        if a_id == b_id:
            continue

        elem_a = component_map.get(a_id)
        elem_b = component_map.get(b_id)

        if not elem_a or not elem_b:
            continue

        try:
            if rel_type == "CONNECTS_TO":
                model.create_entity(
                    "IfcRelConnectsElements",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingElement=elem_a,
                    RelatedElement=elem_b
                )
                counts["CONNECTS_TO"] = counts.get("CONNECTS_TO", 0) + 1

            elif rel_type == "FILLS":
                opening_id = props.get("opening_id", ifcopenshell.guid.new())
                placement = elem_b.ObjectPlacement
                opening = model.create_entity(
                    "IfcOpeningElement",
                    GlobalId=opening_id,
                    OwnerHistory=owner_history,
                    Name="Opening",
                    ObjectPlacement=placement,
                    Representation=None
                )
                model.create_entity(
                    "IfcRelVoidsElement",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingBuildingElement=elem_b,
                    RelatedOpeningElement=opening
                )
                model.create_entity(
                    "IfcRelFillsElement",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingOpeningElement=opening,
                    RelatedBuildingElement=elem_a
                )
                counts["FILLS"] = counts.get("FILLS", 0) + 1

            elif rel_type == "FLOWS_INTO":
                model.create_entity(
                    "IfcRelConnectsElements",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingElement=elem_a,
                    RelatedElement=elem_b
                )
                counts["FLOWS_INTO"] = counts.get("FLOWS_INTO", 0) + 1

            elif rel_type == "PART_OF":
                model.create_entity(
                    "IfcRelAggregates",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingObject=elem_b,
                    RelatedObjects=[elem_a]
                )
                counts["PART_OF"] = counts.get("PART_OF", 0) + 1

            elif rel_type == "COVERED_BY":
                model.create_entity(
                    "IfcRelCoversBldgElements",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_history,
                    RelatingBuildingElement=elem_b,
                    RelatedCoverings=[elem_a]
                )
                counts["COVERED_BY"] = counts.get("COVERED_BY", 0) + 1

        except Exception as e:
            logger.debug("Failed to create %s relationship: %s", rel_type, e)
            continue

    return counts


# ---------------------------------------------------------------------------
# Main reconstruction function
# ---------------------------------------------------------------------------

def reconstruct(project_id, output_path=None):
    print("=" * 50)
    print("BIM RECONSTRUCTOR")
    print("=" * 50)

    # --- Load from Postgres ---
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        project = load_project(cursor, project_id)
        if not project:
            print(f"Error: project {project_id} not found")
            sys.exit(1)

        print(f"\nProject: {project['name']} (id={project_id})")

        components    = load_components(cursor, project_id)
        relationships = load_relationships(cursor, project_id)
        wall_types    = load_wall_types(cursor, project_id)
        spaces        = load_spaces(cursor, project_id)

        print(f"Loaded {len(components)} components, "
              f"{len(relationships)} relationships, "
              f"{len(spaces)} spaces")

    ifc_schema = project.get("ifc_schema") or "IFC4"
    geom_cache_model = open_geometry_cache(project_id)
    if geom_cache_model:
        n_cached = sum(1 for c in components if c.get("has_geometry"))
        print(f"Geometry cache open ({n_cached} components may use source shapes)")

    # --- Build the IFC model ---
    print("\nBuilding IFC model...")
    model = ifcopenshell.file(schema=ifc_schema)
    geom_context, body_context = create_geometry_context(model, ifc_schema)

    # Owner history
    application = model.create_entity(
        "IfcApplication",
        ApplicationDeveloper=model.create_entity(
            "IfcOrganization", Name="BIM Component Stripper"
        ),
        Version="1.0",
        ApplicationFullName="BIM Component Stripper Reconstructor",
        ApplicationIdentifier="BIM-RECONSTRUCTOR"
    )
    person = model.create_entity("IfcPerson", FamilyName="Reconstructor")
    org    = model.create_entity("IfcOrganization", Name="BIM Component Stripper")
    person_and_org = model.create_entity(
        "IfcPersonAndOrganization", ThePerson=person, TheOrganization=org
    )
    owner_history = model.create_entity(
        "IfcOwnerHistory",
        OwningUser=person_and_org,
        OwningApplication=application,
        State="READWRITE",
        ChangeAction="ADDED",
        CreationDate=int(datetime.now().timestamp())
    )

    # Project -> Site -> Building hierarchy
    # Match source IFC length unit (e.g. mm) so placement + cached geometry numbers agree.
    units = None
    if geom_cache_model:
        units = copy_unit_assignment_to_model(model, geom_cache_model)
    if units is None:
        src_name = os.path.basename(project.get("filename") or "")
        if src_name:
            src_path = os.path.join(_ROOT, "uploads", src_name)
            if os.path.isfile(src_path):
                try:
                    src_ifc = ifcopenshell.open(src_path)
                    units = copy_unit_assignment_to_model(model, src_ifc)
                except Exception as e:
                    logger.debug("Could not read units from %s: %s", src_path, e)
    if units is None:
        units = default_unit_assignment(model)
    world_origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    world_axis   = model.create_entity(
        "IfcAxis2Placement3D",
        Location=world_origin, Axis=None, RefDirection=None
    )
    world_placement = model.create_entity("IfcLocalPlacement", PlacementRelTo=None, RelativePlacement=world_axis)

    ifc_project = model.create_entity(
        "IfcProject",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name=project["name"],
        RepresentationContexts=[geom_context],
        UnitsInContext=units,
    )

    # One output length unit expressed in metres (e.g. 0.001 for mm, 1.0 for m).
    unit_m = length_unit_metres_factor(model)
    # DB coordinates (pos_x, elevation, bbox, etc.) are in the source file's length
    # unit, and the output declares the same unit. So the scale from DB → output is 1.
    # `width_mm / height_mm / length_mm` are always mm and handled inside the placeholder.
    db_to_out_scale = 1.0
    print(f"Output length unit: 1 unit = {unit_m} m  (DB→output scale = {db_to_out_scale})")
    ifc_site = model.create_entity(
        "IfcSite",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name="Site",
        ObjectPlacement=world_placement
    )
    ifc_building = model.create_entity(
        "IfcBuilding",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name=project["name"],
        ObjectPlacement=world_placement
    )

    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatingObject=ifc_project,
        RelatedObjects=[ifc_site]
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatingObject=ifc_site,
        RelatedObjects=[ifc_building]
    )

    # --- Build floor (storey) nodes ---
    print("Creating building storeys...")
    storey_map = {}
    storey_elements = {}

    seen_levels = {}
    for comp in components:
        level = comp["level"]
        elevation = comp["elevation"]
        if level and level not in seen_levels:
            seen_levels[level] = elevation

    for level_name, elevation in sorted(seen_levels.items(), key=lambda x: (x[1] or 0)):
        e = _to_float(elevation, 0.0) or 0.0
        # DB stores elevation in source units (same as output), no conversion needed.
        elev = e * db_to_out_scale
        storey_placement = model.create_entity(
            "IfcLocalPlacement",
            PlacementRelTo=world_placement,
            RelativePlacement=model.create_entity(
                "IfcAxis2Placement3D",
                Location=model.create_entity(
                    "IfcCartesianPoint", Coordinates=(0.0, 0.0, elev)
                ),
                Axis=None, RefDirection=None
            )
        )
        storey = model.create_entity(
            "IfcBuildingStorey",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_history,
            Name=level_name,
            ObjectPlacement=storey_placement,
            Elevation=elev
        )
        storey_map[level_name] = storey
        storey_elements[level_name] = []
        print(f"  Storey: {level_name} @ {elev:.2f}m")

    if storey_map:
        model.create_entity(
            "IfcRelAggregates",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_history,
            RelatingObject=ifc_building,
            RelatedObjects=list(storey_map.values())
        )

    # --- Create all component elements ---
    print(f"\nCreating {len(components)} elements...")
    component_map = {}
    skipped = 0

    for comp in components:
        db_id    = comp["id"]
        category = comp["category"]
        name     = comp["family_name"] or comp["type_name"] or category
        guid     = comp["revit_id"] or ifcopenshell.guid.new()

        if comp["pos_x"] is not None:
            placement = make_ifc_placement(
                model,
                comp["pos_x"], comp["pos_y"], comp["pos_z"],
                comp["rot_x"], comp["rot_y"], comp["rot_z"],
                scale=db_to_out_scale,
            )
        else:
            placement = world_placement
            skipped += 1

        element = create_element(model, category, guid, name, placement)
        geom_ok = False
        if comp.get("has_geometry") and geom_cache_model and guid:
            try:
                cache_el = geom_cache_model.by_guid(guid)
                if cache_el is not None:
                    geom_ok = copy_cached_geometry_to_element(
                        model, body_context, cache_el, element, owner_history
                    )
            except Exception as e:
                logger.debug("Geometry cache copy failed for %s: %s", guid, e)
        if not geom_ok and should_attach_placeholder(comp):
            attach_placeholder_extrusion(model, body_context, element, comp, unit_m=unit_m)
        component_map[db_id] = element

        level = comp["level"]
        if level and level in storey_map:
            storey_elements[level].append(element)

        attach_psets(model, owner_history, element, comp["parameters"])

        if category in ("IfcWall", "IfcWallStandardCase", "IfcWallElementedCase"):
            attach_wall_layers(model, owner_history, element, wall_types.get(db_id))

    # Attach elements to storeys
    for level_name, elements in storey_elements.items():
        if elements:
            model.create_entity(
                "IfcRelContainedInSpatialStructure",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_history,
                RelatingStructure=storey_map[level_name],
                RelatedElements=elements
            )

    if skipped:
        print(f"  Note: {skipped} elements had no spatial data — placed at world origin")

    # --- Recreate relationships ---
    print("\nRecreating relationships...")
    rel_counts = attach_relationships(model, owner_history, relationships, component_map)
    for rel_type, count in rel_counts.items():
        print(f"  {rel_type}: {count}")

    # --- Write output ---
    if not output_path:
        output_path = f"reconstructed_with_geom_{project_id}.ifc"

    model.write(output_path)

    print(f"\n{'=' * 50}")
    print(f"Reconstruction complete.")
    print(f"  Components: {len(component_map)}")
    print(f"  Storeys:    {len(storey_map)}")
    total_rels = sum(rel_counts.values())
    print(f"  Relationships: {total_rels}")
    print(f"  Output: {output_path}")
    print(f"{'=' * 50}")

    return output_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Reconstruct an IFC file from the BIM database")
    parser.add_argument("project_id", type=int, help="Project ID to reconstruct")
    parser.add_argument("--output", "-o", help="Output path for the reconstructed IFC file")
    args = parser.parse_args()

    reconstruct(args.project_id, output_path=args.output)
