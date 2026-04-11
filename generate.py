"""
generate.py — BIM Studio
Writes a valid IFC4 file with:
 - Real extruded geometry for structural elements (walls, slabs, columns, beams)
 - Transplanted real geometry for library components (furniture, fixtures, etc.)
   by opening the source IFC file and copying the element's actual shape.
"""

import math, json, os, sys
import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.util.element
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "bim_components"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD")
    )

def get_component_source(component_id):
    """Return (revit_id, filename) for a library component."""
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.revit_id, p.filename
            FROM components c
            JOIN projects p ON p.id = c.project_id
            WHERE c.id = %s
        """, (component_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return (row["revit_id"], row["filename"]) if row else (None, None)
    except Exception as e:
        print(f"  DB lookup failed: {e}")
        return None, None

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

def pt3(model, x, y, z): return model.createIfcCartesianPoint((float(x),float(y),float(z)))
def pt2(model, x, y):    return model.createIfcCartesianPoint((float(x),float(y)))
def d3(model, x, y, z):  return model.createIfcDirection((float(x),float(y),float(z)))
def d2(model, x, y):     return model.createIfcDirection((float(x),float(y)))

def ax3(model, ox=0, oy=0, oz=0, az=None, rx=None):
    return model.createIfcAxis2Placement3D(
        pt3(model,ox,oy,oz),
        az or d3(model,0,0,1),
        rx or d3(model,1,0,0)
    )

def local_pl(model, rel=None, ox=0, oy=0, oz=0, az=None, rx=None):
    return model.createIfcLocalPlacement(rel, ax3(model,ox,oy,oz,az,rx))

def make_context(model):
    ctx  = model.createIfcGeometricRepresentationContext(
        None,"Model",3,1e-5, ax3(model), None)
    body = model.createIfcGeometricRepresentationSubContext(
        "Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)
    return ctx, body

def rect_prof(model, w, d, ox=0, oy=0):
    return model.createIfcRectangleProfileDef(
        "AREA", None,
        model.createIfcAxis2Placement2D(pt2(model,ox,oy), d2(model,1,0)),
        float(w), float(d)
    )

def extrude(model, body_ctx, profile, depth, dx=0, dy=0, dz=1):
    solid = model.createIfcExtrudedAreaSolid(
        profile, ax3(model), d3(model,dx,dy,dz), float(depth))
    return model.createIfcShapeRepresentation(body_ctx,"Body","SweptSolid",[solid])

# ── Geometry per category ─────────────────────────────────────────────────────

def wall_geom(model, ctx, w_mm, h_mm, l_mm):
    p = rect_prof(model, l_mm/1000, w_mm/1000, (l_mm/1000)/2, (w_mm/1000)/2)
    return extrude(model, ctx, p, h_mm/1000)

def slab_geom(model, ctx, w_mm, h_mm, l_mm):
    # w=thickness, l=X span, h=Y span
    thick = max(w_mm, 150)/1000
    sx    = max(l_mm, 1000)/1000
    sy    = max(h_mm, 1000)/1000
    p = rect_prof(model, sx, sy, sx/2, sy/2)
    return extrude(model, ctx, p, thick)

def col_geom(model, ctx, w_mm, h_mm):
    s = max(w_mm,200)/1000
    p = rect_prof(model, s, s, s/2, s/2)
    return extrude(model, ctx, p, h_mm/1000)

def beam_geom(model, ctx, w_mm, h_mm, l_mm):
    p = rect_prof(model, w_mm/1000, h_mm/1000, (w_mm/1000)/2, (h_mm/1000)/2)
    return extrude(model, ctx, p, l_mm/1000, dx=1, dy=0, dz=0)

def door_geom(model, ctx, w_mm, h_mm):
    w,h,t = max(w_mm,800)/1000, max(h_mm,2100)/1000, 0.05
    p = rect_prof(model, w, t, w/2, t/2)
    return extrude(model, ctx, p, h)

def window_geom(model, ctx, w_mm, h_mm):
    w,h,t = max(w_mm,1000)/1000, max(h_mm,1200)/1000, 0.05
    p = rect_prof(model, w, t, w/2, t/2)
    return extrude(model, ctx, p, h)

def box_geom(model, ctx, w_mm, h_mm, l_mm):
    w = max(w_mm or 500, 100)/1000
    h = max(h_mm or 500, 100)/1000
    l = max(l_mm or 500, 100)/1000
    p = rect_prof(model, l, w, l/2, w/2)
    return extrude(model, ctx, p, h)

def make_geometry(model, ctx, category, comp):
    w = float(comp.get("width_mm")  or 200)
    h = float(comp.get("height_mm") or 3000)
    l = float(comp.get("length_mm") or 1000)
    if category in ("IfcWall","IfcWallStandardCase","IfcWallElementedCase",
                    "IfcCurtainWall","IfcCovering","IfcPlate","IfcMember","IfcRailing"):
        return wall_geom(model, ctx, w, h, l)
    if category in ("IfcSlab","IfcRoof"):
        return slab_geom(model, ctx, w, h, l)
    if category in ("IfcColumn","IfcColumnStandardCase"):
        return col_geom(model, ctx, w, h)
    if category in ("IfcBeam","IfcBeamStandardCase"):
        return beam_geom(model, ctx, w, h, l)
    if category == "IfcDoor":
        return door_geom(model, ctx, l, h)
    if category == "IfcWindow":
        return window_geom(model, ctx, l, h)
    return box_geom(model, ctx, w, h, l)

# ── Geometry transplant ───────────────────────────────────────────────────────

# Cache open IFC files so we don't re-open the same file for every component
_ifc_cache = {}

def get_source_ifc(filename):
    if filename not in _ifc_cache:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(path):
            print(f"  Source IFC not found: {path}")
            return None
        try:
            _ifc_cache[filename] = ifcopenshell.open(path)
        except Exception as e:
            print(f"  Failed to open {path}: {e}")
            return None
    return _ifc_cache[filename]


def transplant_geometry(target_model, body_ctx, revit_id, filename):
    """
    Open the source IFC file, find the element by GlobalId,
    and copy its shape representations into the target model.
    Returns an IfcProductDefinitionShape or None on failure.
    """
    src = get_source_ifc(filename)
    if not src:
        return None

    # Find source element by GlobalId
    src_element = None
    for el in src.by_type("IfcProduct"):
        if el.GlobalId == revit_id:
            src_element = el
            break

    if not src_element:
        print(f"  Element {revit_id} not found in {filename}")
        return None

    if not src_element.Representation:
        return None

    try:
        # Collect all items we need to copy (walk the graph)
        # We serialise the source element's representation to a string
        # then parse it back into the target model.
        # The simplest safe approach: extract each representation item's
        # geometry by iterating IfcRepresentationItem subtypes.

        new_reps = []
        for rep in src_element.Representation.Representations:
            if rep.RepresentationIdentifier not in ("Body", "Facetation", "Brep", None):
                continue
            new_items = []
            for item in rep.Items:
                try:
                    # Deep-copy the item into the target model
                    new_item = _copy_entity(target_model, item)
                    if new_item:
                        new_items.append(new_item)
                except Exception as e:
                    print(f"    Item copy failed: {e}")
                    continue

            if new_items:
                new_rep = target_model.createIfcShapeRepresentation(
                    body_ctx,
                    rep.RepresentationIdentifier or "Body",
                    rep.RepresentationType or "Brep",
                    new_items
                )
                new_reps.append(new_rep)

        if not new_reps:
            return None

        return target_model.createIfcProductDefinitionShape(None, None, new_reps)

    except Exception as e:
        print(f"  Transplant failed for {revit_id}: {e}")
        return None


def _copy_entity(target, entity):
    """
    Recursively copy an IFC entity and all its attributes into the target model.
    Handles cycles via a per-call cache stored on the function itself.
    """
    if not hasattr(_copy_entity, "_cache"):
        _copy_entity._cache = {}

    eid = entity.id()
    if eid in _copy_entity._cache:
        return _copy_entity._cache[eid]

    # Placeholder to break cycles
    _copy_entity._cache[eid] = None

    schema_name = entity.is_a()
    new_attrs = []

    for i, attr in enumerate(entity):
        new_attrs.append(_copy_attr(target, attr))

    try:
        new_entity = target.create_entity(schema_name, *new_attrs)
        _copy_entity._cache[eid] = new_entity
        return new_entity
    except Exception as e:
        return None


def _copy_attr(target, attr):
    """Recursively copy an attribute value into the target model."""
    if attr is None:
        return None
    if isinstance(attr, (bool, int, float, str)):
        return attr
    if isinstance(attr, (list, tuple)):
        copied = [_copy_attr(target, a) for a in attr]
        return type(attr)(copied) if isinstance(attr, tuple) else copied
    if hasattr(attr, "is_a"):
        # It's an IFC entity
        return _copy_entity(target, attr)
    return attr

# ── Placement ─────────────────────────────────────────────────────────────────

def make_placement(model, px, py, pz, rot_z_deg=0.0, relative_to=None):
    # AI outputs positions in mm, IFC uses metres
    x, y, z = float(px or 0)/1000, float(py or 0)/1000, float(pz or 0)/1000
    rz = math.radians(float(rot_z_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    a = ax3(model, x, y, z, d3(model,0,0,1), d3(model,cz,sz,0))
    return model.createIfcLocalPlacement(relative_to, a)

# ── Psets & material ──────────────────────────────────────────────────────────

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
                    str(k), None,
                    model.create_entity("IfcLabel", wrappedValue=str(v)), None))
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

# ── Main ──────────────────────────────────────────────────────────────────────

def generate_ifc(spec: dict, output_path: str = None) -> str:
    # Clear transplant cache between runs
    if hasattr(_copy_entity, "_cache"):
        _copy_entity._cache = {}
    _ifc_cache.clear()

    name     = spec.get("name", "Generated Building")
    floors   = spec.get("floors", [])
    metadata = spec.get("metadata", {})

    total_comps = sum(len(f.get("components", [])) for f in floors)
    print(f"Generating IFC: {name} | {len(floors)} floors | {total_comps} components")

    model = ifcopenshell.file(schema="IFC4")

    # Owner history
    app_ent = model.createIfcApplication(
        model.createIfcOrganization(None,"BIM Studio",None,None,None),
        "1.0","BIM Studio AI Generator","BIM-STUDIO-GEN")
    person  = model.createIfcPerson(None,"AI Architect",None,None,None,None,None,None)
    org     = model.createIfcOrganization(None,"BIM Studio",None,None,None)
    pao     = model.createIfcPersonAndOrganization(person,org,None)
    oh      = model.createIfcOwnerHistory(
        pao,app_ent,None,"ADDED",None,pao,app_ent,int(datetime.now().timestamp()))

    # Units
    units = model.createIfcUnitAssignment([
        model.createIfcSIUnit(None,"LENGTHUNIT",   None,"METRE"),
        model.createIfcSIUnit(None,"AREAUNIT",     None,"SQUARE_METRE"),
        model.createIfcSIUnit(None,"VOLUMEUNIT",   None,"CUBIC_METRE"),
        model.createIfcSIUnit(None,"PLANEANGLEUNIT",None,"RADIAN"),
    ])

    geom_ctx, body_ctx = make_context(model)
    wp = local_pl(model)

    # Project → Site → Building
    proj = model.createIfcProject(
        ifcopenshell.guid.new(),oh,name,None,None,None,None,[geom_ctx],units)
    site = model.createIfcSite(
        ifcopenshell.guid.new(),oh,metadata.get("location","Site"),
        None,None,wp,None,None,"ELEMENT",None,None,None,None,None)
    bldg = model.createIfcBuilding(
        ifcopenshell.guid.new(),oh,name,None,None,wp,None,None,"ELEMENT",None,None,None)

    model.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,proj,[site])
    model.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,site,[bldg])

    # Storeys
    ifc_storeys     = []
    storey_elements = []
    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        sp   = local_pl(model, wp, 0, 0, elev)
        st   = model.createIfcBuildingStorey(
            ifcopenshell.guid.new(),oh,
            floor.get("name", f"Level {len(ifc_storeys)+1}"),
            None,None,sp,None,None,"ELEMENT",elev)
        ifc_storeys.append(st)
        storey_elements.append([])

    if ifc_storeys:
        model.createIfcRelAggregates(
            ifcopenshell.guid.new(),oh,None,None,bldg,ifc_storeys)

    # Components
    for fi, floor in enumerate(floors):
        storey = ifc_storeys[fi]
        elems  = storey_elements[fi]

        for comp in floor.get("components", []):
            category = comp.get("category", "IfcBuildingElementProxy")
            ifc_type = CATEGORY_MAP.get(category, "IfcBuildingElementProxy")
            cname    = comp.get("name", category)
            material = comp.get("material", "")
            lib_id   = comp.get("library_component_id")  # set by AI for library items

            placement = make_placement(
                model,
                comp.get("pos_x",0), comp.get("pos_y",0), comp.get("pos_z",0),
                comp.get("rot_z",0),
                relative_to=storey.ObjectPlacement
            )

            # ── Geometry: transplant from library if available, else generate ──
            prod_rep = None

            if lib_id:
                # Try to get real geometry from the source IFC
                revit_id, filename = get_component_source(lib_id)
                if revit_id and filename:
                    print(f"  Transplanting {cname} from {filename} (GlobalId={revit_id})")
                    prod_rep = transplant_geometry(model, body_ctx, revit_id, filename)
                    if not prod_rep:
                        print(f"    Transplant failed — falling back to generated geometry")

            if not prod_rep:
                # Generate parametric geometry
                try:
                    shape_rep = make_geometry(model, body_ctx, category, comp)
                    prod_rep  = model.createIfcProductDefinitionShape(None,None,[shape_rep])
                except Exception as e:
                    print(f"  Geometry generation failed for {cname}: {e}")
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
                    ifcopenshell.guid.new(),oh,cname,None,None,
                    placement,prod_rep,None,"ELEMENT")

            # Psets
            dim = {}
            for k,dk in [("width_mm","Width"),("height_mm","Height"),("length_mm","Length")]:
                if comp.get(k) is not None:
                    dim[dk] = comp[k]
            all_props = {}
            if dim:
                all_props["BIM_Studio_Dimensions"] = dim
            if lib_id:
                all_props["BIM_Studio_Library"] = {"source_component_id": str(lib_id)}
            if comp.get("properties"):
                all_props.update(comp["properties"])

            attach_psets(model, oh, element, all_props)
            attach_material(model, oh, element, material)
            elems.append(element)

        if elems:
            model.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(),oh,None,None,elems,storey)

    # Metadata pset on building
    if metadata:
        meta_props = []
        for k,v in metadata.items():
            if v is None: continue
            try:
                meta_props.append(model.createIfcPropertySingleValue(
                    str(k),None,
                    model.create_entity("IfcLabel",wrappedValue=str(v)),None))
            except Exception:
                pass
        if meta_props:
            pset = model.createIfcPropertySet(
                ifcopenshell.guid.new(),oh,"BIM_Studio_Project_Info",None,meta_props)
            model.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(),oh,None,None,[bldg],pset)

    if not output_path:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = name.replace(" ","_").replace("/","-")
        output_path = f"generated_{safe}_{ts}.ifc"

    model.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    # Test with a library component (dining table id=70 from Ifc4_SampleHouse.ifc)
    test_spec = {
        "name": "Test House with Furniture",
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
                {"category":"IfcSlab","name":"Floor Slab","material":"Concrete",
                 "pos_x":0,"pos_y":0,"pos_z":0,"rot_z":0,
                 "width_mm":200,"height_mm":8000,"length_mm":10000},
                {"category":"IfcSlab","name":"Roof","material":"Concrete",
                 "pos_x":0,"pos_y":0,"pos_z":3000,"rot_z":0,
                 "width_mm":200,"height_mm":8000,"length_mm":10000},
                # Library furniture — real geometry from source IFC
                {"category":"IfcFurniture","name":"Dining Table","material":"Wood",
                 "pos_x":3000,"pos_y":3000,"pos_z":0,"rot_z":0,
                 "library_component_id": 70},
                {"category":"IfcFurniture","name":"Dining Chair 1","material":"Wood",
                 "pos_x":2000,"pos_y":3000,"pos_z":0,"rot_z":0,
                 "library_component_id": 71},
            ]
        }],
        "metadata": {"location":"Test City","estimated_cost_usd":100000}
    }
    path = generate_ifc(test_spec, "/tmp/test_house_furniture.ifc")
    print(f"Output: {path}")
