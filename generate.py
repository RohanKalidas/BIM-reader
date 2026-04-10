"""
generate.py — BIM Studio
Writes a valid IFC4 file with real extruded geometry from a BuildingSpec dict.
APS / Autodesk Viewer requires actual geometry — IfcExtrudedAreaSolid shapes.
"""

import math, json, os, sys, ifcopenshell, ifcopenshell.guid
from datetime import datetime

# ── Category map ──────────────────────────────────────────────────────────────
CATEGORY_MAP = {
    "IfcWall":"IfcWall","IfcWallStandardCase":"IfcWallStandardCase",
    "IfcSlab":"IfcSlab","IfcRoof":"IfcRoof",
    "IfcDoor":"IfcDoor","IfcWindow":"IfcWindow",
    "IfcColumn":"IfcColumn","IfcColumnStandardCase":"IfcColumnStandardCase",
    "IfcBeam":"IfcBeam","IfcBeamStandardCase":"IfcBeamStandardCase",
    "IfcStair":"IfcStair","IfcStairFlight":"IfcStairFlight",
    "IfcRailing":"IfcRailing","IfcCurtainWall":"IfcCurtainWall",
    "IfcCovering":"IfcCovering","IfcPlate":"IfcPlate","IfcMember":"IfcMember",
    "IfcDuctSegment":"IfcDuctSegment","IfcPipeSegment":"IfcPipeSegment",
    "IfcFurniture":"IfcFurniture","IfcFurnishingElement":"IfcFurnishingElement",
}

# ── Geometry helpers ──────────────────────────────────────────────────────────

def pt(model, x, y, z=None):
    if z is None:
        return model.createIfcCartesianPoint((float(x), float(y)))
    return model.createIfcCartesianPoint((float(x), float(y), float(z)))

def dir3(model, x, y, z):
    return model.createIfcDirection((float(x), float(y), float(z)))

def dir2(model, x, y):
    return model.createIfcDirection((float(x), float(y)))

def axis2_2d(model, ox=0, oy=0, dx=1, dy=0):
    return model.createIfcAxis2Placement2D(pt(model, ox, oy), dir2(model, dx, dy))

def axis2_3d(model, ox=0, oy=0, oz=0, az=None, rx=None):
    loc = pt(model, ox, oy, oz)
    a   = az if az else dir3(model, 0, 0, 1)
    r   = rx if rx else dir3(model, 1, 0, 0)
    return model.createIfcAxis2Placement3D(loc, a, r)

def local_placement(model, relative_to=None, ox=0, oy=0, oz=0, az=None, rx=None):
    ax = axis2_3d(model, ox, oy, oz, az, rx)
    return model.createIfcLocalPlacement(relative_to, ax)

def make_context(model):
    ctx = model.createIfcGeometricRepresentationContext(
        None, "Model", 3, 1e-5,
        axis2_3d(model), None
    )
    body = model.createIfcGeometricRepresentationSubContext(
        "Body", "Model", None, None, None, None, ctx, None, "MODEL_VIEW", None
    )
    return ctx, body


def rect_profile(model, w, d, ox=0, oy=0):
    """Rectangular profile centred at (ox, oy)."""
    return model.createIfcRectangleProfileDef(
        "AREA", None,
        axis2_2d(model, ox, oy),
        float(w), float(d)
    )


def extrusion(model, body_ctx, profile, depth, dx=0, dy=0, dz=1):
    """Extruded area solid."""
    solid = model.createIfcExtrudedAreaSolid(
        profile,
        axis2_3d(model),
        dir3(model, dx, dy, dz),
        float(depth)
    )
    return model.createIfcShapeRepresentation(
        body_ctx, "Body", "SweptSolid", [solid]
    )


def wall_geometry(model, body_ctx, w_mm, h_mm, l_mm):
    """
    Wall: length along X, thickness along Y, height along Z.
    Profile is a rectangle (l_mm × w_mm) extruded upward h_mm.
    """
    prof = rect_profile(model, l_mm / 1000, w_mm / 1000, (l_mm / 1000) / 2, (w_mm / 1000) / 2)
    return extrusion(model, body_ctx, prof, h_mm / 1000)


def slab_geometry(model, body_ctx, l_mm, d_mm, h_mm):
    """
    Slab: length × depth footprint, extruded h_mm (thickness) upward.
    """
    prof = rect_profile(model, l_mm / 1000, d_mm / 1000, (l_mm / 1000) / 2, (d_mm / 1000) / 2)
    return extrusion(model, body_ctx, prof, h_mm / 1000)


def column_geometry(model, body_ctx, w_mm, h_mm):
    """Square column: w×w cross-section extruded h upward."""
    s = max(w_mm, 200) / 1000
    prof = rect_profile(model, s, s)
    return extrusion(model, body_ctx, prof, h_mm / 1000)


def beam_geometry(model, body_ctx, w_mm, h_mm, l_mm):
    """Beam along X axis: w×h cross-section extruded l_mm along X."""
    prof = rect_profile(model, w_mm / 1000, h_mm / 1000)
    return extrusion(model, body_ctx, prof, l_mm / 1000, dx=1, dy=0, dz=0)


def door_geometry(model, body_ctx, w_mm, h_mm):
    """Simple door box."""
    w = max(w_mm, 800) / 1000
    h = max(h_mm, 2100) / 1000
    t = 0.05
    prof = rect_profile(model, w, t, w / 2, t / 2)
    return extrusion(model, body_ctx, prof, h)


def window_geometry(model, body_ctx, w_mm, h_mm):
    """Simple window box."""
    w = max(w_mm, 1000) / 1000
    h = max(h_mm, 1200) / 1000
    t = 0.05
    prof = rect_profile(model, w, t, w / 2, t / 2)
    return extrusion(model, body_ctx, prof, h)


def generic_box(model, body_ctx, w_mm, h_mm, l_mm):
    """Fallback box for unknown categories."""
    w = max(w_mm or 500, 100) / 1000
    h = max(h_mm or 500, 100) / 1000
    l = max(l_mm or 500, 100) / 1000
    prof = rect_profile(model, l, w, l / 2, w / 2)
    return extrusion(model, body_ctx, prof, h)


def make_geometry(model, body_ctx, category, comp):
    """Dispatch geometry creation based on IFC category."""
    w = float(comp.get("width_mm")  or 200)
    h = float(comp.get("height_mm") or 3000)
    l = float(comp.get("length_mm") or 1000)

    cat = category
    if cat in ("IfcWall", "IfcWallStandardCase", "IfcWallElementedCase",
               "IfcCurtainWall", "IfcCovering", "IfcPlate", "IfcMember"):
        return wall_geometry(model, body_ctx, w, h, l)
    elif cat in ("IfcSlab", "IfcRoof"):
        # For slabs: width_mm = thickness, length_mm = X span, height_mm = Y span
        thickness = max(w, 150)
        span_x    = max(l, 1000)
        span_y    = max(h, 1000)
        return slab_geometry(model, body_ctx, span_x, span_y, thickness)
    elif cat in ("IfcColumn", "IfcColumnStandardCase"):
        return column_geometry(model, body_ctx, w, h)
    elif cat in ("IfcBeam", "IfcBeamStandardCase"):
        return beam_geometry(model, body_ctx, w, h, l)
    elif cat == "IfcDoor":
        return door_geometry(model, body_ctx, l, h)
    elif cat == "IfcWindow":
        return window_geometry(model, body_ctx, l, h)
    else:
        return generic_box(model, body_ctx, w, h, l)


# ── Placement ────────────────────────────────────────────────────────────────

def make_placement(model, px, py, pz, rot_z_deg=0.0, relative_to=None):
    x, y, z = float(px or 0), float(py or 0), float(pz or 0)
    rz  = math.radians(float(rot_z_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    az  = dir3(model, 0, 0, 1)
    rx  = dir3(model, cz, sz, 0)
    ax  = axis2_3d(model, x, y, z, az, rx)
    return model.createIfcLocalPlacement(relative_to, ax)


# ── Psets & material ────────────────────────────────────────────────────────

def attach_psets(model, oh, element, properties):
    if not properties:
        return
    for pset_name, pset_data in properties.items():
        if not isinstance(pset_data, dict):
            continue
        props = []
        for k, v in pset_data.items():
            if v is None:
                continue
            try:
                props.append(model.createIfcPropertySingleValue(
                    k, None,
                    model.create_entity("IfcLabel", wrappedValue=str(v)),
                    None
                ))
            except Exception:
                continue
        if not props:
            continue
        try:
            pset = model.createIfcPropertySet(
                ifcopenshell.guid.new(), oh, pset_name, None, props)
            model.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(), oh, None, None, [element], pset)
        except Exception:
            continue


def attach_material(model, oh, element, material_name):
    if not material_name:
        return
    try:
        mat = model.createIfcMaterial(str(material_name), None, None)
        model.createIfcRelAssociatesMaterial(
            ifcopenshell.guid.new(), oh, None, None, [element], mat)
    except Exception:
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_ifc(spec: dict, output_path: str = None) -> str:
    name     = spec.get("name", "Generated Building")
    floors   = spec.get("floors", [])
    metadata = spec.get("metadata", {})

    total_comps = sum(len(f.get("components", [])) for f in floors)
    print(f"Generating IFC: {name} | {len(floors)} floors | {total_comps} components")

    model = ifcopenshell.file(schema="IFC4")

    # Owner history
    app_ent = model.createIfcApplication(
        model.createIfcOrganization(None, "BIM Studio", None, None, None),
        "1.0", "BIM Studio AI Generator", "BIM-STUDIO-GEN"
    )
    person  = model.createIfcPerson(None, "AI Architect", None, None, None, None, None, None)
    org     = model.createIfcOrganization(None, "BIM Studio", None, None, None)
    pao     = model.createIfcPersonAndOrganization(person, org, None)
    oh      = model.createIfcOwnerHistory(
        pao, app_ent, None, "ADDED", None, pao, app_ent,
        int(datetime.now().timestamp())
    )

    # Units
    units = model.createIfcUnitAssignment([
        model.createIfcSIUnit(None, "LENGTHUNIT",    None, "METRE"),
        model.createIfcSIUnit(None, "AREAUNIT",      None, "SQUARE_METRE"),
        model.createIfcSIUnit(None, "VOLUMEUNIT",    None, "CUBIC_METRE"),
        model.createIfcSIUnit(None, "PLANEANGLEUNIT",None, "RADIAN"),
    ])

    # Geometric context
    geom_ctx, body_ctx = make_context(model)

    # World placement
    wp = local_placement(model)

    # Project → Site → Building
    proj = model.createIfcProject(
        ifcopenshell.guid.new(), oh, name, None, None, None, None, [geom_ctx], units)
    site = model.createIfcSite(
        ifcopenshell.guid.new(), oh, metadata.get("location", "Site"),
        None, None, wp, None, None, "ELEMENT", None, None, None, None, None)
    bldg = model.createIfcBuilding(
        ifcopenshell.guid.new(), oh, name, None, None, wp, None, None, "ELEMENT", None, None, None)

    model.createIfcRelAggregates(ifcopenshell.guid.new(), oh, None, None, proj, [site])
    model.createIfcRelAggregates(ifcopenshell.guid.new(), oh, None, None, site, [bldg])

    # Storeys
    ifc_storeys     = []
    storey_elements = []

    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        sp   = local_placement(model, wp, 0, 0, elev)
        st   = model.createIfcBuildingStorey(
            ifcopenshell.guid.new(), oh,
            floor.get("name", f"Level {len(ifc_storeys)+1}"),
            None, None, sp, None, None, "ELEMENT", elev)
        ifc_storeys.append(st)
        storey_elements.append([])

    if ifc_storeys:
        model.createIfcRelAggregates(
            ifcopenshell.guid.new(), oh, None, None, bldg, ifc_storeys)

    # Components
    for fi, floor in enumerate(floors):
        storey = ifc_storeys[fi]
        elems  = storey_elements[fi]

        for comp in floor.get("components", []):
            category = comp.get("category", "IfcBuildingElementProxy")
            ifc_type = CATEGORY_MAP.get(category, "IfcBuildingElementProxy")
            cname    = comp.get("name", category)
            material = comp.get("material", "")

            placement = make_placement(
                model,
                comp.get("pos_x", 0), comp.get("pos_y", 0), comp.get("pos_z", 0),
                comp.get("rot_z", 0),
                relative_to=storey.ObjectPlacement
            )

            # Build geometry
            try:
                shape_rep = make_geometry(model, body_ctx, category, comp)
                prod_rep  = model.createIfcProductDefinitionShape(None, None, [shape_rep])
            except Exception as e:
                print(f"  Geometry failed for {cname}: {e}")
                prod_rep = None

            # Create element
            try:
                element = model.create_entity(
                    ifc_type,
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=oh,
                    Name=cname,
                    ObjectPlacement=placement,
                    Representation=prod_rep
                )
            except Exception:
                element = model.createIfcBuildingElementProxy(
                    ifcopenshell.guid.new(), oh, cname, None, None,
                    placement, prod_rep, None, "ELEMENT"
                )

            # Dimension pset
            dim = {}
            for k, dk in [("width_mm","Width"),("height_mm","Height"),("length_mm","Length")]:
                if comp.get(k) is not None:
                    dim[dk] = comp[k]
            all_props = {}
            if dim:
                all_props["BIM_Studio_Dimensions"] = dim
            if comp.get("properties"):
                all_props.update(comp["properties"])

            attach_psets(model, oh, element, all_props)
            attach_material(model, oh, element, material)
            elems.append(element)

        if elems:
            model.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(), oh, None, None, elems, storey)

    # Metadata pset on building
    if metadata:
        meta_props = []
        for k, v in metadata.items():
            if v is None:
                continue
            try:
                meta_props.append(model.createIfcPropertySingleValue(
                    str(k), None,
                    model.create_entity("IfcLabel", wrappedValue=str(v)),
                    None
                ))
            except Exception:
                pass
        if meta_props:
            pset = model.createIfcPropertySet(
                ifcopenshell.guid.new(), oh, "BIM_Studio_Project_Info", None, meta_props)
            model.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(), oh, None, None, [bldg], pset)

    # Write
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = name.replace(" ", "_").replace("/", "-")
        output_path = f"generated_{safe}_{ts}.ifc"

    model.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    test_spec = {
        "name": "Test House",
        "floors": [{
            "name": "Ground Floor", "elevation": 0.0, "height": 3000,
            "components": [
                {"category":"IfcWall","name":"South Wall","material":"Brick",
                 "pos_x":0,"pos_y":0,"pos_z":0,"rot_z":0,
                 "width_mm":290,"height_mm":3000,"length_mm":10000},
                {"category":"IfcWall","name":"North Wall","material":"Brick",
                 "pos_x":0,"pos_y":8000,"pos_z":0,"rot_z":0,
                 "width_mm":290,"height_mm":3000,"length_mm":10000},
                {"category":"IfcWall","name":"West Wall","material":"Brick",
                 "pos_x":0,"pos_y":0,"pos_z":0,"rot_z":90,
                 "width_mm":290,"height_mm":3000,"length_mm":8000},
                {"category":"IfcWall","name":"East Wall","material":"Brick",
                 "pos_x":10000,"pos_y":0,"pos_z":0,"rot_z":90,
                 "width_mm":290,"height_mm":3000,"length_mm":8000},
                {"category":"IfcSlab","name":"Ground Slab","material":"Concrete",
                 "pos_x":0,"pos_y":0,"pos_z":0,"rot_z":0,
                 "width_mm":200,"height_mm":8000,"length_mm":10000},
                {"category":"IfcSlab","name":"Roof Slab","material":"Concrete",
                 "pos_x":0,"pos_y":0,"pos_z":3000,"rot_z":0,
                 "width_mm":200,"height_mm":8000,"length_mm":10000},
            ]
        }],
        "metadata": {"location": "Test City", "estimated_cost_usd": 100000}
    }
    path = generate_ifc(test_spec, "/tmp/test_house.ifc")
    print(f"Test output: {path}")
