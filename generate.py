"""
generate.py — BIM Studio
Two-mode IFC generator:

MODE 1 — PROCEDURAL (new): spec contains "rooms" array.
  generate.py builds the full building from room descriptions:
  walls, doors, windows, floor slab, ceiling, and room-appropriate fixtures.
  The AI only needs to describe rooms; all component placement is handled here.

MODE 2 — COMPONENT (legacy): spec contains "floors" with "components" arrays.
  Used as fallback if "rooms" key is absent.
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
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.revit_id, p.filename
            FROM components c JOIN projects p ON p.id = c.project_id
            WHERE c.id = %s
        """, (component_id,))
        row = cur.fetchone(); cur.close(); conn.close()
        return (row["revit_id"], row["filename"]) if row else (None, None)
    except Exception as e:
        print(f"  DB lookup failed: {e}")
        return None, None

# ── IFC helpers ───────────────────────────────────────────────────────────────

def pt3(m, x, y, z): return m.createIfcCartesianPoint((float(x),float(y),float(z)))
def pt2(m, x, y):    return m.createIfcCartesianPoint((float(x),float(y)))
def d3(m, x, y, z):  return m.createIfcDirection((float(x),float(y),float(z)))
def d2(m, x, y):     return m.createIfcDirection((float(x),float(y)))

def ax3(m, ox=0, oy=0, oz=0, az=None, rx=None):
    return m.createIfcAxis2Placement3D(
        pt3(m,ox,oy,oz), az or d3(m,0,0,1), rx or d3(m,1,0,0))

def local_pl(m, rel=None, ox=0, oy=0, oz=0, az=None, rx=None):
    return m.createIfcLocalPlacement(rel, ax3(m,ox,oy,oz,az,rx))

def make_context(m):
    ctx  = m.createIfcGeometricRepresentationContext(None,"Model",3,1e-5,ax3(m),None)
    body = m.createIfcGeometricRepresentationSubContext(
        "Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)
    return ctx, body

def rect_prof(m, w, d, ox=0, oy=0):
    return m.createIfcRectangleProfileDef(
        "AREA",None,
        m.createIfcAxis2Placement2D(pt2(m,ox,oy), d2(m,1,0)),
        float(w), float(d))

def extrude(m, ctx, profile, depth, dx=0, dy=0, dz=1):
    solid = m.createIfcExtrudedAreaSolid(profile, ax3(m), d3(m,dx,dy,dz), float(depth))
    return m.createIfcShapeRepresentation(ctx,"Body","SweptSolid",[solid])

def box_rep(m, ctx, lx, ly, lz, ox=0, oy=0):
    p = rect_prof(m, lx, ly, ox, oy)
    return m.createIfcProductDefinitionShape(None,None,[extrude(m,ctx,p,lz)])

def make_placement(m, x, y, z, rz_deg=0, rel=None):
    rz = math.radians(float(rz_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    a = ax3(m, x, y, z, d3(m,0,0,1), d3(m,cz,sz,0))
    return m.createIfcLocalPlacement(rel, a)

def make_element(m, ifc_type, oh, name, placement, rep):
    try:
        return m.create_entity(ifc_type,
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            Name=name, ObjectPlacement=placement, Representation=rep)
    except Exception:
        return m.createIfcBuildingElementProxy(
            ifcopenshell.guid.new(),oh,name,None,None,placement,rep,None,"ELEMENT")

def attach_material(m, oh, el, mat_name):
    if not mat_name: return
    try:
        mat = m.createIfcMaterial(str(mat_name),None,None)
        m.createIfcRelAssociatesMaterial(
            ifcopenshell.guid.new(),oh,None,None,[el],mat)
    except Exception: pass

# ── Geometry transplant ───────────────────────────────────────────────────────

_ifc_cache = {}

def get_source_ifc(filename):
    if filename not in _ifc_cache:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(path): return None
        try: _ifc_cache[filename] = ifcopenshell.open(path)
        except Exception: return None
    return _ifc_cache[filename]

def _copy_entity(target, entity):
    if not hasattr(_copy_entity, "_cache"): _copy_entity._cache = {}
    eid = entity.id()
    if eid in _copy_entity._cache: return _copy_entity._cache[eid]
    _copy_entity._cache[eid] = None
    new_attrs = [_copy_attr(target, a) for a in entity]
    try:
        ne = target.create_entity(entity.is_a(), *new_attrs)
        _copy_entity._cache[eid] = ne
        return ne
    except Exception: return None

def _copy_attr(target, attr):
    if attr is None: return None
    if isinstance(attr, (bool, int, float, str)): return attr
    if isinstance(attr, (list, tuple)):
        copied = [_copy_attr(target, a) for a in attr]
        return type(attr)(copied) if isinstance(attr, tuple) else copied
    if hasattr(attr, "is_a"): return _copy_entity(target, attr)
    return attr

def get_source_unit_scale(src):
    """Return scale factor to convert source units to metres."""
    try:
        for unit in src.by_type("IfcSIUnit"):
            if unit.UnitType == "LENGTHUNIT":
                if unit.Name == "METRE" and not unit.Prefix:
                    return 1.0
                if unit.Name == "METRE" and unit.Prefix == "MILLI":
                    return 0.001
        for unit in src.by_type("IfcConversionBasedUnit"):
            if unit.UnitType == "LENGTHUNIT":
                if hasattr(unit, "ConversionFactor") and unit.ConversionFactor:
                    return float(unit.ConversionFactor.ValueComponent) * 0.001
    except Exception:
        pass
    return 0.001  # default assume mm

def transplant_geometry(target, body_ctx, revit_id, filename):
    src = get_source_ifc(filename)
    if not src: return None
    src_el = next((e for e in src.by_type("IfcProduct") if e.GlobalId == revit_id), None)
    if not src_el or not src_el.Representation: return None

    scale = get_source_unit_scale(src)

    try:
        new_reps = []
        for rep in src_el.Representation.Representations:
            if rep.RepresentationIdentifier not in ("Body","Facetation","Brep",None): continue
            items = [_copy_entity(target, it) for it in rep.Items]
            items = [i for i in items if i]
            if items:
                # Wrap in a scale transform if source is not already in metres
                if abs(scale - 1.0) > 0.0001:
                    cart_op = target.createIfcCartesianTransformationOperator3D(
                        None, None,
                        target.createIfcCartesianPoint((0.0, 0.0, 0.0)),
                        scale, None, None, None)
                    mapped_rep = target.createIfcShapeRepresentation(
                        body_ctx,
                        rep.RepresentationIdentifier or "Body",
                        rep.RepresentationType or "Brep",
                        items)
                    rep_map = target.createIfcRepresentationMap(
                        target.createIfcAxis2Placement3D(
                            target.createIfcCartesianPoint((0.0,0.0,0.0)), None, None),
                        mapped_rep)
                    mapped_item = target.createIfcMappedItem(rep_map, cart_op)
                    final_rep = target.createIfcShapeRepresentation(
                        body_ctx,
                        rep.RepresentationIdentifier or "Body",
                        "MappedRepresentation",
                        [mapped_item])
                    new_reps.append(final_rep)
                else:
                    new_reps.append(target.createIfcShapeRepresentation(
                        body_ctx,
                        rep.RepresentationIdentifier or "Body",
                        rep.RepresentationType or "Brep",
                        items))
        return target.createIfcProductDefinitionShape(None, None, new_reps) if new_reps else None
    except Exception as e:
        print(f"  Transplant failed: {e}"); return None

# ═══════════════════════════════════════════════════════════════════════════════
# PROCEDURAL ROOM BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

WALL_T     = 0.20   # exterior wall thickness m
INT_WALL_T = 0.12   # interior wall thickness m
FLOOR_T    = 0.20   # slab thickness m
CEIL_H     = 2.70   # default ceiling height m
DOOR_W     = 0.90   # door width m
DOOR_H     = 2.10   # door height m
WIN_W      = 1.20   # window width m
WIN_H      = 1.10   # window height m
WIN_SILL   = 0.90   # window sill height m

# Room type → list of fixture specs
# Each fixture: (name, ifc_type, rel_x, rel_y, w, d, h, search_query)
# rel_x/y as fraction of room width/depth (0=left/bottom, 1=right/top)

ROOM_FIXTURES = {
    "bathroom": [
        ("Toilet",       "IfcSanitaryTerminal", 0.15, 0.20, 0.38, 0.65, 0.82, "toilet"),
        ("Sink",         "IfcSanitaryTerminal", 0.60, 0.15, 0.50, 0.45, 0.85, "sink"),
        ("Shower",       "IfcSanitaryTerminal", 0.55, 0.55, 0.90, 0.90, 2.10, "shower"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "kitchen": [
        ("Counter",      "IfcFurnishingElement",0.10, 0.08, 0.60, 0.60, 0.90, "counter"),
        ("Sink",         "IfcSanitaryTerminal", 0.40, 0.08, 0.60, 0.55, 0.90, "sink"),
        ("Stove",        "IfcElectricAppliance",0.65, 0.08, 0.60, 0.60, 0.90, "stove"),
        ("Refrigerator", "IfcElectricAppliance",0.85, 0.12, 0.70, 0.70, 1.80, "refrigerator"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "bedroom": [
        ("Bed",          "IfcFurniture",        0.50, 0.60, 1.60, 2.00, 0.60, "bed"),
        ("Wardrobe",     "IfcFurniture",        0.15, 0.10, 1.20, 0.60, 2.10, "wardrobe"),
        ("Nightstand",   "IfcFurniture",        0.20, 0.60, 0.50, 0.45, 0.55, "table"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "living": [
        ("Sofa",         "IfcFurniture",        0.30, 0.65, 2.20, 0.90, 0.85, "sofa"),
        ("Coffee Table", "IfcFurniture",        0.30, 0.45, 1.10, 0.55, 0.45, "table"),
        ("TV Stand",     "IfcFurniture",        0.30, 0.10, 1.50, 0.45, 0.55, "table"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "dining": [
        ("Dining Table", "IfcFurniture",        0.50, 0.50, 1.60, 0.90, 0.75, "dining table"),
        ("Chair 1",      "IfcFurniture",        0.20, 0.50, 0.50, 0.50, 0.90, "chair"),
        ("Chair 2",      "IfcFurniture",        0.80, 0.50, 0.50, 0.50, 0.90, "chair"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "office": [
        ("Desk",         "IfcFurniture",        0.50, 0.20, 1.40, 0.70, 0.75, "table"),
        ("Chair",        "IfcFurniture",        0.50, 0.35, 0.60, 0.60, 1.10, "chair"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "hallway": [
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "utility": [
        ("Water Heater", "IfcElectricAppliance",0.20, 0.20, 0.55, 0.55, 1.60, "boiler"),
        ("Light",        "IfcLightFixture",     0.50, 0.50, 0.20, 0.20, 0.05, "light"),
    ],
    "garage": [
        ("Light",        "IfcLightFixture",     0.50, 0.80, 0.20, 0.20, 0.05, "light"),
    ],
}

def get_room_type(name):
    """Classify a room name into a fixture template key."""
    n = name.lower()
    if any(x in n for x in ["bath","wc","toilet","shower","lavatory"]): return "bathroom"
    if any(x in n for x in ["kitchen","cook","culinary"]):               return "kitchen"
    if any(x in n for x in ["bed","master","guest","sleep"]):            return "bedroom"
    if any(x in n for x in ["living","lounge","family","great"]):        return "living"
    if any(x in n for x in ["dining","eat","breakfast"]):                return "dining"
    if any(x in n for x in ["office","study","work"]):                   return "office"
    if any(x in n for x in ["hall","corridor","foyer","entry","lobby"]): return "hallway"
    if any(x in n for x in ["utility","laundry","storage","plant","mech"]): return "utility"
    if any(x in n for x in ["garage","parking","car"]):                  return "garage"
    return "living"  # default

def find_library_component(query):
    """Search library for a component matching the query. Returns (id, filename, revit_id) or None."""
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        q = f"%{query.lower()}%"
        cur.execute("""
            SELECT c.id, c.revit_id, p.filename
            FROM library l
            JOIN components c ON c.id = l.component_id
            JOIN projects p ON p.id = c.project_id
            WHERE LOWER(COALESCE(c.family_name,'')) LIKE %s
               OR LOWER(COALESCE(c.type_name,'')) LIKE %s
            LIMIT 1
        """, (q, q))
        row = cur.fetchone(); cur.close(); conn.close()
        if row: return row["id"], row["filename"], row["revit_id"]
    except Exception: pass
    return None, None, None

def make_wall(m, oh, body_ctx, name, x, y, z, length, thickness, height, storey_pl, material="Concrete"):
    """Create a single wall element with correct extruded geometry."""
    # Profile: rectangle in XY plane, extruded in Z
    prof = rect_prof(m, length, thickness)
    solid = m.createIfcExtrudedAreaSolid(prof, ax3(m), d3(m,0,0,1), float(height))
    rep = m.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [solid])
    prod_rep = m.createIfcProductDefinitionShape(None, None, [rep])
    pl = make_placement(m, x, y, z, rel=storey_pl)
    el = make_element(m, "IfcWall", oh, name, pl, prod_rep)
    attach_material(m, oh, el, material)
    return el

def make_slab(m, oh, body_ctx, name, x, y, z, width, depth, thickness, storey_pl, material="Concrete"):
    """Create a slab element."""
    prof = rect_prof(m, width, depth)
    solid = m.createIfcExtrudedAreaSolid(prof, ax3(m), d3(m,0,0,1), float(thickness))
    rep = m.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [solid])
    prod_rep = m.createIfcProductDefinitionShape(None, None, [rep])
    pl = make_placement(m, x, y, z, rel=storey_pl)
    el = make_element(m, "IfcSlab", oh, name, pl, prod_rep)
    attach_material(m, oh, el, material)
    return el

def build_room(m, oh, body_ctx, room, storey_pl, floor_elev, elements, ceil_h=CEIL_H):
    rx    = float(room.get("x", 0))
    ry    = float(room.get("y", 0))
    rw    = float(room.get("width",  4.0))
    rd    = float(room.get("depth",  3.0))
    rh    = float(room.get("height", ceil_h))
    rname = room.get("name", "Room")
    ext   = room.get("exterior", True)
    wt    = WALL_T if ext else INT_WALL_T
    wall_mat = "CMU" if ext else "Drywall"
    rtype = get_room_type(rname)

    # Floor slab
    elements.append(make_slab(m, oh, body_ctx, f"{rname} Floor",
        rx, ry, -FLOOR_T, rw, rd, FLOOR_T, storey_pl, "Concrete"))

    # Ceiling
    elements.append(make_slab(m, oh, body_ctx, f"{rname} Ceiling",
        rx, ry, rh, rw, rd, FLOOR_T, storey_pl, "Concrete"))

    # South wall
    elements.append(make_wall(m, oh, body_ctx, f"{rname} South Wall",
        rx, ry, 0, rw, wt, rh, storey_pl, wall_mat))
    # North wall
    elements.append(make_wall(m, oh, body_ctx, f"{rname} North Wall",
        rx, ry+rd-wt, 0, rw, wt, rh, storey_pl, wall_mat))
    # West wall (between S and N walls)
    elements.append(make_wall(m, oh, body_ctx, f"{rname} West Wall",
        rx, ry+wt, 0, wt, rd-2*wt, rh, storey_pl, wall_mat))
    # East wall
    elements.append(make_wall(m, oh, body_ctx, f"{rname} East Wall",
        rx+rw-wt, ry+wt, 0, wt, rd-2*wt, rh, storey_pl, wall_mat))

    # Door
    door_wall = room.get("door_wall", "south")
    door_offset = max(wt + 0.1, 0.3)
    if door_wall == "south":
        dx, dy, dw, dd = rx+door_offset, ry, DOOR_W, wt
    elif door_wall == "north":
        dx, dy, dw, dd = rx+door_offset, ry+rd-wt, DOOR_W, wt
    elif door_wall == "west":
        dx, dy, dw, dd = rx, ry+door_offset, wt, DOOR_W
    else:
        dx, dy, dw, dd = rx+rw-wt, ry+door_offset, wt, DOOR_W

    d_prof  = rect_prof(m, dw, dd)
    d_solid = m.createIfcExtrudedAreaSolid(d_prof, ax3(m), d3(m,0,0,1), DOOR_H)
    d_rep   = m.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [d_solid])
    d_prod  = m.createIfcProductDefinitionShape(None, None, [d_rep])
    d_el    = make_element(m, "IfcDoor", oh, f"{rname} Door",
                           make_placement(m, dx, dy, 0, rel=storey_pl), d_prod)
    attach_material(m, oh, d_el, "Wood")
    elements.append(d_el)

    # Window (exterior rooms only)
    if ext and rw >= 2.0:
        wx     = rx + rw/2 - WIN_W/2
        w_prof = rect_prof(m, WIN_W, wt+0.02)
        w_solid= m.createIfcExtrudedAreaSolid(w_prof, ax3(m), d3(m,0,0,1), WIN_H)
        w_rep  = m.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [w_solid])
        w_prod = m.createIfcProductDefinitionShape(None, None, [w_rep])
        w_el   = make_element(m, "IfcWindow", oh, f"{rname} Window",
                              make_placement(m, wx, ry, WIN_SILL, rel=storey_pl), w_prod)
        attach_material(m, oh, w_el, "Aluminium")
        elements.append(w_el)

    # Fixtures
    fixtures = ROOM_FIXTURES.get(rtype, [])
    inner_w  = max(rw - 2*wt, 0.5)
    inner_d  = max(rd - 2*wt, 0.5)

    for fname, ftype, fx, fy, fw, fd, fh, fquery in fixtures:
        abs_x = rx + wt + max(0, fx*inner_w - fw/2)
        abs_y = ry + wt + max(0, fy*inner_d - fd/2)
        abs_x = min(abs_x, rx + rw - wt - fw)
        abs_y = min(abs_y, ry + rd - wt - fd)
        abs_z = (rh - 0.05) if "Light" in fname else 0.0

        lib_id, src_filename, revit_id = find_library_component(fquery)
        f_rep = None
        if lib_id and src_filename and revit_id:
            if not hasattr(_copy_entity, "_cache"): _copy_entity._cache = {}
            f_rep = transplant_geometry(m, body_ctx, revit_id, src_filename)

        if not f_rep:
            fw_s = max(fw, 0.1); fd_s = max(fd, 0.1); fh_s = max(fh, 0.05)
            f_prof  = rect_prof(m, fw_s, fd_s)
            f_solid = m.createIfcExtrudedAreaSolid(f_prof, ax3(m), d3(m,0,0,1), fh_s)
            f_shape = m.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [f_solid])
            f_rep   = m.createIfcProductDefinitionShape(None, None, [f_shape])

        f_el = make_element(m, ftype, oh, f"{rname} {fname}",
                            make_placement(m, abs_x, abs_y, abs_z, rel=storey_pl), f_rep)
        elements.append(f_el)


def build_from_rooms(m, oh, body_ctx, spec, bldg, wp, storey):
    """Build procedurally from rooms array."""
    floors_data = spec.get("floors", [])
    if not floors_data:
        return []

    # Use first floor for now (multi-storey can be added later)
    floor_data = floors_data[0]
    rooms = floor_data.get("rooms", [])
    ceil_h = float(floor_data.get("height", 2700)) / 1000 if floor_data.get("height",0) > 10 else float(floor_data.get("height", 2.7))
    elev   = float(floor_data.get("elevation", 0))

    storey_pl = storey.ObjectPlacement
    elements  = []

    for room in rooms:
        build_room(m, oh, body_ctx, room, storey_pl, elev, elements, ceil_h)

    return elements


# ═══════════════════════════════════════════════════════════════════════════════
# LEGACY COMPONENT MODE
# ═══════════════════════════════════════════════════════════════════════════════

CATEGORY_MAP = {
    "IfcWall":"IfcWall","IfcWallStandardCase":"IfcWallStandardCase",
    "IfcSlab":"IfcSlab","IfcRoof":"IfcRoof",
    "IfcDoor":"IfcDoor","IfcWindow":"IfcWindow",
    "IfcColumn":"IfcColumn","IfcBeam":"IfcBeam",
    "IfcStair":"IfcStair","IfcRailing":"IfcRailing",
    "IfcCurtainWall":"IfcCurtainWall","IfcCovering":"IfcCovering",
    "IfcDuctSegment":"IfcDuctSegment","IfcPipeSegment":"IfcPipeSegment",
    "IfcFurniture":"IfcFurniture","IfcFurnishingElement":"IfcFurnishingElement",
    "IfcSanitaryTerminal":"IfcSanitaryTerminal",
    "IfcElectricAppliance":"IfcElectricAppliance",
    "IfcLightFixture":"IfcLightFixture",
    "IfcFlowSegment":"IfcFlowSegment","IfcFlowFitting":"IfcFlowFitting",
    "IfcFlowTerminal":"IfcFlowTerminal",
}

def wall_geom(m, ctx, w_mm, h_mm, l_mm):
    p = rect_prof(m, l_mm/1000, w_mm/1000, (l_mm/1000)/2, (w_mm/1000)/2)
    return extrude(m, ctx, p, h_mm/1000)

def slab_geom(m, ctx, w_mm, h_mm, l_mm):
    thick = max(w_mm,150)/1000; sx = max(l_mm,1000)/1000; sy = max(h_mm,1000)/1000
    p = rect_prof(m, sx, sy, sx/2, sy/2)
    return extrude(m, ctx, p, thick)

def col_geom(m, ctx, w_mm, h_mm):
    s = max(w_mm,200)/1000; p = rect_prof(m, s, s, s/2, s/2)
    return extrude(m, ctx, p, h_mm/1000)

def beam_geom(m, ctx, w_mm, h_mm, l_mm):
    p = rect_prof(m, w_mm/1000, h_mm/1000, (w_mm/1000)/2, (h_mm/1000)/2)
    return extrude(m, ctx, p, l_mm/1000, dx=1, dy=0, dz=0)

def make_geometry_legacy(m, ctx, category, comp):
    w = float(comp.get("width_mm") or 200)
    h = float(comp.get("height_mm") or 3000)
    l = float(comp.get("length_mm") or 1000)
    if category in ("IfcWall","IfcWallStandardCase","IfcWallElementedCase",
                    "IfcCurtainWall","IfcCovering","IfcPlate","IfcMember","IfcRailing"):
        return wall_geom(m, ctx, w, h, l)
    if category in ("IfcSlab","IfcRoof"):
        return slab_geom(m, ctx, w, h, l)
    if category in ("IfcColumn","IfcColumnStandardCase"):
        return col_geom(m, ctx, w, h)
    if category in ("IfcBeam","IfcBeamStandardCase"):
        return beam_geom(m, ctx, w, h, l)
    w2 = max(w,100)/1000; h2 = max(h,100)/1000; l2 = max(l,100)/1000
    p = rect_prof(m, l2, w2, l2/2, w2/2)
    return extrude(m, ctx, p, h2)

def attach_psets(m, oh, element, properties):
    if not properties: return
    for pset_name, pset_data in properties.items():
        if not isinstance(pset_data, dict): continue
        props = []
        for k, v in pset_data.items():
            if v is None: continue
            try:
                props.append(m.createIfcPropertySingleValue(
                    str(k),None,m.create_entity("IfcLabel",wrappedValue=str(v)),None))
            except Exception: continue
        if not props: continue
        try:
            pset = m.createIfcPropertySet(ifcopenshell.guid.new(),oh,pset_name,None,props)
            m.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(),oh,None,None,[element],pset)
        except Exception: continue

def build_from_components(m, oh, body_ctx, floors, ifc_storeys):
    """Legacy mode: build from floors→components spec."""
    storey_elements = [[] for _ in ifc_storeys]
    for fi, floor in enumerate(floors):
        storey = ifc_storeys[fi]
        elems  = storey_elements[fi]
        for comp in floor.get("components", []):
            category = comp.get("category","IfcBuildingElementProxy")
            ifc_type = CATEGORY_MAP.get(category,"IfcBuildingElementProxy")
            cname    = comp.get("name", category)
            material = comp.get("material","")
            lib_id   = comp.get("library_component_id")

            px = float(comp.get("pos_x",0))/1000
            py = float(comp.get("pos_y",0))/1000
            pz = float(comp.get("pos_z",0))/1000
            rz = float(comp.get("rot_z",0))
            placement = make_placement(m, px, py, pz, rz, rel=storey.ObjectPlacement)

            prod_rep = None
            if lib_id:
                revit_id, filename = get_component_source(lib_id)
                if revit_id and filename:
                    prod_rep = transplant_geometry(m, body_ctx, revit_id, filename)

            if not prod_rep:
                try:
                    sr = make_geometry_legacy(m, body_ctx, category, comp)
                    prod_rep = m.createIfcProductDefinitionShape(None,None,[sr])
                except Exception: prod_rep = None

            try:
                el = m.create_entity(ifc_type,
                    GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
                    Name=cname, ObjectPlacement=placement, Representation=prod_rep)
            except Exception:
                el = m.createIfcBuildingElementProxy(
                    ifcopenshell.guid.new(),oh,cname,None,None,placement,prod_rep,None,"ELEMENT")

            props = {}
            for k,dk in [("width_mm","Width"),("height_mm","Height"),("length_mm","Length")]:
                if comp.get(k) is not None: props[dk] = comp[k]
            if props: attach_psets(m, oh, el, {"BIM_Studio_Dimensions": props})
            if lib_id: attach_psets(m, oh, el, {"BIM_Studio_Library":{"source":str(lib_id)}})
            attach_material(m, oh, el, material)
            elems.append(el)
    return storey_elements


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def generate_ifc(spec: dict, output_path: str = None) -> str:
    if hasattr(_copy_entity, "_cache"): _copy_entity._cache = {}
    _ifc_cache.clear()

    name     = spec.get("name", "Generated Building")
    metadata = spec.get("metadata", {})
    floors   = spec.get("floors", [])

    # Detect mode
    has_rooms = any(f.get("rooms") for f in floors)
    mode = "procedural" if has_rooms else "component"
    print(f"Generating IFC [{mode} mode]: {name}")

    m = ifcopenshell.file(schema="IFC4")

    # Owner history
    app_ent = m.createIfcApplication(
        m.createIfcOrganization(None,"BIM Studio",None,None,None),
        "1.0","BIM Studio AI Generator","BIM-STUDIO-GEN")
    person  = m.createIfcPerson(None,"AI Architect",None,None,None,None,None,None)
    org     = m.createIfcOrganization(None,"BIM Studio",None,None,None)
    pao     = m.createIfcPersonAndOrganization(person,org,None)
    oh      = m.createIfcOwnerHistory(
        pao,app_ent,None,"ADDED",None,pao,app_ent,int(datetime.now().timestamp()))

    units = m.createIfcUnitAssignment([
        m.createIfcSIUnit(None,"LENGTHUNIT",   None,"METRE"),
        m.createIfcSIUnit(None,"AREAUNIT",     None,"SQUARE_METRE"),
        m.createIfcSIUnit(None,"VOLUMEUNIT",   None,"CUBIC_METRE"),
        m.createIfcSIUnit(None,"PLANEANGLEUNIT",None,"RADIAN"),
    ])

    geom_ctx, body_ctx = make_context(m)
    wp = local_pl(m)

    proj = m.createIfcProject(
        ifcopenshell.guid.new(),oh,name,None,None,None,None,[geom_ctx],units)
    site = m.createIfcSite(
        ifcopenshell.guid.new(),oh,metadata.get("location","Site"),
        None,None,wp,None,None,"ELEMENT",None,None,None,None,None)
    bldg = m.createIfcBuilding(
        ifcopenshell.guid.new(),oh,name,None,None,wp,None,None,"ELEMENT",None,None,None)

    m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,proj,[site])
    m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,site,[bldg])

    # Build storeys
    ifc_storeys = []
    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        sp   = local_pl(m, wp, 0, 0, elev)
        st   = m.createIfcBuildingStorey(
            ifcopenshell.guid.new(),oh,
            floor.get("name", f"Level {len(ifc_storeys)+1}"),
            None,None,sp,None,None,"ELEMENT",elev)
        ifc_storeys.append(st)

    if ifc_storeys:
        m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,bldg,ifc_storeys)

    # Generate elements
    if mode == "procedural":
        for fi, floor in enumerate(floors):
            if not floor.get("rooms"): continue
            storey = ifc_storeys[fi]
            ceil_h_raw = floor.get("height", 2.7)
            ceil_h = float(ceil_h_raw)/1000 if float(ceil_h_raw) > 10 else float(ceil_h_raw)
            elev   = float(floor.get("elevation", 0))
            elements = []
            for room in floor.get("rooms", []):
                build_room(m, oh, body_ctx, room, storey.ObjectPlacement, elev, elements, ceil_h)
            if elements:
                m.createIfcRelContainedInSpatialStructure(
                    ifcopenshell.guid.new(),oh,None,None,elements,storey)
            print(f"  Floor '{floor.get('name')}': {len(elements)} elements from {len(floor.get('rooms',[]))} rooms")
    else:
        storey_elements = build_from_components(m, oh, body_ctx, floors, ifc_storeys)
        for fi, elems in enumerate(storey_elements):
            if elems:
                m.createIfcRelContainedInSpatialStructure(
                    ifcopenshell.guid.new(),oh,None,None,elems,ifc_storeys[fi])
        total = sum(len(e) for e in storey_elements)
        print(f"  {total} components generated")

    # Metadata
    if metadata:
        meta_props = []
        for k,v in metadata.items():
            if v is None: continue
            try:
                meta_props.append(m.createIfcPropertySingleValue(
                    str(k),None,m.create_entity("IfcLabel",wrappedValue=str(v)),None))
            except Exception: pass
        if meta_props:
            pset = m.createIfcPropertySet(
                ifcopenshell.guid.new(),oh,"BIM_Studio_Project_Info",None,meta_props)
            m.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(),oh,None,None,[bldg],pset)

    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"generated_{name.replace(' ','_')}_{ts}.ifc"

    m.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    test_spec = {
        "name": "Test 1BR Apartment",
        "floors": [{
            "name": "Ground Floor", "elevation": 0.0, "height": 2.7,
            "rooms": [
                {"name":"Living Room",  "x":0.0, "y":0.0, "width":5.0, "depth":4.0, "exterior":True,  "door_wall":"east"},
                {"name":"Kitchen",      "x":5.0, "y":0.0, "width":3.0, "depth":4.0, "exterior":True,  "door_wall":"west"},
                {"name":"Bedroom",      "x":0.0, "y":4.0, "width":4.0, "depth":3.5, "exterior":True,  "door_wall":"south"},
                {"name":"Bathroom",     "x":4.0, "y":4.0, "width":2.5, "depth":2.0, "exterior":False, "door_wall":"south"},
                {"name":"Hallway",      "x":4.0, "y":6.0, "width":4.0, "depth":1.5, "exterior":False, "door_wall":"west"},
            ]
        }],
        "metadata": {"location":"Test City","building_type":"Residential","estimated_cost_usd":300000}
    }
    path = generate_ifc(test_spec, "/tmp/test_procedural.ifc")
    print(f"Output: {path}")
