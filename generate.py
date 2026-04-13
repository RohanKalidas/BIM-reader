"""
generate.py — BIM Studio
Procedural IFC generator from room-based specs.
All geometry is parametric (no transplanting) for consistent scale.
Rooms share walls — adjacent rooms don't duplicate shared boundaries.
Includes basic MEP: supply ducts at ceiling, cold water pipes at floor level.
"""

import math
import json
import os
import logging
import ifcopenshell
import ifcopenshell.guid
import psycopg2.extras
from datetime import datetime

from database.db import get_db_connection

logger = logging.getLogger(__name__)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

# ── IFC primitives ───────────────────────────────────────────────────────────

def pt3(m,x,y,z): return m.createIfcCartesianPoint((float(x),float(y),float(z)))
def pt2(m,x,y):   return m.createIfcCartesianPoint((float(x),float(y)))
def d3(m,x,y,z):  return m.createIfcDirection((float(x),float(y),float(z)))
def d2(m,x,y):    return m.createIfcDirection((float(x),float(y)))

def ax3(m, ox=0,oy=0,oz=0, az=None, rx=None):
    return m.createIfcAxis2Placement3D(
        pt3(m,ox,oy,oz), az or d3(m,0,0,1), rx or d3(m,1,0,0))

def local_pl(m, rel=None, ox=0,oy=0,oz=0):
    return m.createIfcLocalPlacement(rel, ax3(m,ox,oy,oz))

def make_context(m):
    ctx  = m.createIfcGeometricRepresentationContext(None,"Model",3,1e-5,ax3(m),None)
    body = m.createIfcGeometricRepresentationSubContext(
        "Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)
    return ctx, body

def box_shape(m, body_ctx, lx, ly, lz):
    """Solid box: lx wide (X), ly deep (Y), lz tall (Z). Origin at SW bottom corner."""
    prof  = m.createIfcRectangleProfileDef("AREA", None,
        m.createIfcAxis2Placement2D(pt2(m,0,0), d2(m,1,0)), float(lx), float(ly))
    solid = m.createIfcExtrudedAreaSolid(prof, ax3(m), d3(m,0,0,1), float(lz))
    rep   = m.createIfcShapeRepresentation(body_ctx,"Body","SweptSolid",[solid])
    return m.createIfcProductDefinitionShape(None,None,[rep])

def place(m, x,y,z, rz_deg=0, rel=None):
    rz = math.radians(float(rz_deg or 0))
    cz,sz = math.cos(rz), math.sin(rz)
    a = ax3(m,x,y,z, d3(m,0,0,1), d3(m,cz,sz,0))
    return m.createIfcLocalPlacement(rel, a)

def make_el(m, ifc_type, oh, name, pl, rep):
    try:
        return m.create_entity(ifc_type,
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            Name=name, ObjectPlacement=pl, Representation=rep)
    except Exception:
        return m.createIfcBuildingElementProxy(
            ifcopenshell.guid.new(),oh,name,None,None,pl,rep,None,"ELEMENT")

def add_material(m, oh, el, name):
    try:
        mat = m.createIfcMaterial(str(name),None,None)
        m.createIfcRelAssociatesMaterial(
            ifcopenshell.guid.new(),oh,None,None,[el],mat)
    except Exception as e:
        logger.debug("Failed to add material %s: %s", name, e)

# ── Constants ────────────────────────────────────────────────────────────────

EXT_WALL_T = 0.20   # exterior wall thickness m
INT_WALL_T = 0.12   # interior wall thickness m
FLOOR_T    = 0.20   # slab thickness m
DOOR_W     = 0.90
DOOR_H     = 2.10
WIN_W      = 1.20
WIN_H      = 1.10
WIN_SILL   = 0.90
DUCT_W     = 0.40   # supply duct width
DUCT_H     = 0.20   # supply duct height
PIPE_D     = 0.05   # pipe diameter (drawn as square)

# ── Room type classification ─────────────────────────────────────────────────

def room_type(name):
    n = name.lower()
    if any(x in n for x in ["bath","wc","toilet","shower","lavatory"]): return "bathroom"
    if any(x in n for x in ["kitchen","cook"]):                          return "kitchen"
    if any(x in n for x in ["bed","master","guest","sleep"]):            return "bedroom"
    if any(x in n for x in ["living","lounge","family","great"]):        return "living"
    if any(x in n for x in ["dining","eat"]):                            return "dining"
    if any(x in n for x in ["office","study"]):                          return "office"
    if any(x in n for x in ["hall","corridor","foyer","entry","lobby"]): return "hallway"
    if any(x in n for x in ["utility","laundry","storage","plant"]):     return "utility"
    if any(x in n for x in ["patio","terrace","balcony","deck"]):        return "patio"
    if any(x in n for x in ["garage","parking"]):                        return "garage"
    return "living"

# ── Fixture templates ────────────────────────────────────────────────────────

FIXTURES = {
    "bathroom": [
        ("Toilet",   "IfcSanitaryTerminal", 0.15, 0.20, 0.38, 0.65, 0.82),
        ("Sink",     "IfcSanitaryTerminal", 0.70, 0.12, 0.50, 0.40, 0.85),
        ("Shower",   "IfcSanitaryTerminal", 0.60, 0.65, 0.90, 0.90, 2.10),
    ],
    "kitchen": [
        ("Lower Cabinets", "IfcFurnishingElement", 0.20, 0.08, 1.80, 0.60, 0.90),
        ("Upper Cabinets", "IfcFurnishingElement", 0.20, 0.08, 1.80, 0.35, 0.70),
        ("Stove",    "IfcElectricAppliance",  0.55, 0.08, 0.60, 0.60, 0.90),
        ("Fridge",   "IfcElectricAppliance",  0.82, 0.10, 0.70, 0.70, 1.75),
        ("Sink",     "IfcSanitaryTerminal",   0.35, 0.08, 0.60, 0.50, 0.90),
    ],
    "bedroom": [
        ("Bed",       "IfcFurniture", 0.50, 0.62, 1.60, 2.00, 0.55),
        ("Wardrobe",  "IfcFurniture", 0.15, 0.10, 1.20, 0.60, 2.10),
        ("Nightstand","IfcFurniture", 0.18, 0.62, 0.45, 0.40, 0.55),
    ],
    "living": [
        ("Sofa",         "IfcFurniture", 0.30, 0.68, 2.20, 0.90, 0.85),
        ("Coffee Table", "IfcFurniture", 0.30, 0.48, 1.10, 0.55, 0.42),
        ("TV Unit",      "IfcFurniture", 0.30, 0.10, 1.50, 0.45, 0.55),
    ],
    "dining": [
        ("Dining Table", "IfcFurniture", 0.50, 0.50, 1.60, 0.90, 0.75),
        ("Chair N",      "IfcFurniture", 0.50, 0.75, 0.45, 0.45, 0.90),
        ("Chair S",      "IfcFurniture", 0.50, 0.25, 0.45, 0.45, 0.90),
        ("Chair E",      "IfcFurniture", 0.80, 0.50, 0.45, 0.45, 0.90),
        ("Chair W",      "IfcFurniture", 0.20, 0.50, 0.45, 0.45, 0.90),
    ],
    "office": [
        ("Desk",  "IfcFurniture", 0.50, 0.18, 1.40, 0.70, 0.75),
        ("Chair", "IfcFurniture", 0.50, 0.38, 0.55, 0.55, 1.10),
    ],
    "utility": [
        ("Water Heater", "IfcElectricAppliance", 0.20, 0.20, 0.55, 0.55, 1.60),
        ("Washer",       "IfcElectricAppliance", 0.70, 0.20, 0.60, 0.60, 0.90),
    ],
    "hallway": [],
    "patio":   [],
    "garage":  [],
}

# ── Wall deduplication ───────────────────────────────────────────────────────

def wall_key(x1,y1,x2,y2):
    """Canonical key for a wall segment (order-independent)."""
    a,b = (round(x1,3),round(y1,3)), (round(x2,3),round(y2,3))
    return (min(a,b), max(a,b))

# ── Build single room ────────────────────────────────────────────────────────

def build_room(m, oh, body_ctx, room, storey_pl, ceil_h, built_walls, elements):
    rx    = float(room.get("x",0))
    ry    = float(room.get("y",0))
    rw    = float(room.get("width",4.0))
    rd    = float(room.get("depth",3.0))
    rh    = float(room.get("height", ceil_h))
    rname = room.get("name","Room")
    ext   = room.get("exterior", True)
    wt    = EXT_WALL_T if ext else INT_WALL_T
    wmat  = "CMU" if ext else "Drywall"
    rtype = room_type(rname)

    # ── Floor slab ───────────────────────────────────────────────────────
    fl = make_el(m,"IfcSlab",oh,f"{rname} Floor",
        place(m,rx,ry,-FLOOR_T,rel=storey_pl), box_shape(m,body_ctx,rw,rd,FLOOR_T))
    add_material(m,oh,fl,"Concrete"); elements.append(fl)

    # ── Walls (deduplicated) ─────────────────────────────────────────────
    wall_defs = [
        (f"{rname} S Wall", rx,      ry,        rw, wt),
        (f"{rname} N Wall", rx,      ry+rd-wt,  rw, wt),
        (f"{rname} W Wall", rx,      ry+wt,     wt, rd-2*wt),
        (f"{rname} E Wall", rx+rw-wt,ry+wt,     wt, rd-2*wt),
    ]
    wall_keys = [
        wall_key(rx,      ry,        rx+rw, ry+wt),
        wall_key(rx,      ry+rd-wt,  rx+rw, ry+rd),
        wall_key(rx,      ry+wt,     rx+wt, ry+rd-wt),
        wall_key(rx+rw-wt,ry+wt,    rx+rw, ry+rd-wt),
    ]
    for (wname,wx,wy,wlx,wly), wk in zip(wall_defs, wall_keys):
        if wk in built_walls: continue
        built_walls.add(wk)
        if wlx <= 0 or wly <= 0: continue
        w = make_el(m,"IfcWall",oh,wname,
            place(m,wx,wy,0,rel=storey_pl), box_shape(m,body_ctx,wlx,wly,rh))
        add_material(m,oh,w,wmat); elements.append(w)

    # ── Door ─────────────────────────────────────────────────────────────
    dwall = room.get("door_wall","south")
    doff  = max(wt+0.15, 0.4)
    if dwall=="south":   dx,dy,dlx,dly = rx+doff, ry,       DOOR_W, wt
    elif dwall=="north": dx,dy,dlx,dly = rx+doff, ry+rd-wt, DOOR_W, wt
    elif dwall=="west":  dx,dy,dlx,dly = rx,      ry+doff,  wt, DOOR_W
    else:                dx,dy,dlx,dly = rx+rw-wt,ry+doff,  wt, DOOR_W
    d = make_el(m,"IfcDoor",oh,f"{rname} Door",
        place(m,dx,dy,0,rel=storey_pl), box_shape(m,body_ctx,dlx,dly,DOOR_H))
    add_material(m,oh,d,"Wood"); elements.append(d)

    # ── Window (exterior rooms) ──────────────────────────────────────────
    if ext and rw >= 2.0:
        wx2 = rx + rw/2 - WIN_W/2
        w2 = make_el(m,"IfcWindow",oh,f"{rname} Window",
            place(m,wx2,ry,WIN_SILL,rel=storey_pl),
            box_shape(m,body_ctx,WIN_W,wt+0.02,WIN_H))
        add_material(m,oh,w2,"Glass"); elements.append(w2)

    # ── Light fixture ────────────────────────────────────────────────────
    lx2 = rx + rw/2 - 0.10
    ly2 = ry + rd/2 - 0.10
    lf = make_el(m,"IfcLightFixture",oh,f"{rname} Light",
        place(m,lx2,ly2,rh-0.05,rel=storey_pl), box_shape(m,body_ctx,0.20,0.20,0.05))
    add_material(m,oh,lf,"Aluminium"); elements.append(lf)

    # ── Supply duct at ceiling ───────────────────────────────────────────
    if rtype not in ("patio","garage") and rw > 1.0:
        duct_len = rw - 2*wt - 0.1
        if duct_len > 0.3:
            duct_x = rx + wt + 0.05
            duct_y = ry + rd/2 - DUCT_W/2
            duct_z = rh - DUCT_H - 0.05
            dc = make_el(m,"IfcDuctSegment",oh,f"{rname} Supply Duct",
                place(m,duct_x,duct_y,duct_z,rel=storey_pl),
                box_shape(m,body_ctx,duct_len,DUCT_W,DUCT_H))
            add_material(m,oh,dc,"GalvanisedSteel"); elements.append(dc)

    # ── Cold water pipe (wet rooms only) ─────────────────────────────────
    if rtype in ("bathroom","kitchen","utility"):
        pipe_len = rd - 2*wt - 0.1
        if pipe_len > 0.2:
            px2 = rx + wt + 0.15
            py2 = ry + wt + 0.05
            pz2 = 0.10
            pp = make_el(m,"IfcPipeSegment",oh,f"{rname} Cold Water Pipe",
                place(m,px2,py2,pz2,rel=storey_pl),
                box_shape(m,body_ctx,PIPE_D,pipe_len,PIPE_D))
            add_material(m,oh,pp,"Copper"); elements.append(pp)

    # ── Furniture & fixtures ─────────────────────────────────────────────
    fixture_list = FIXTURES.get(rtype,[])
    inner_w = max(rw - 2*wt, 0.4)
    inner_d = max(rd - 2*wt, 0.4)

    for fname, ftype, fx, fy, fw, fd, fh in fixture_list:
        ax2 = rx + wt + fx*inner_w - fw/2
        ay2 = ry + wt + fy*inner_d - fd/2
        # Clamp inside room
        ax2 = max(rx+wt, min(ax2, rx+rw-wt-fw))
        ay2 = max(ry+wt, min(ay2, ry+rd-wt-fd))
        fw2 = max(fw,0.1); fd2 = max(fd,0.1); fh2 = max(fh,0.05)
        fe = make_el(m, ftype, oh, f"{rname} {fname}",
            place(m,ax2,ay2,0,rel=storey_pl),
            box_shape(m,body_ctx,fw2,fd2,fh2))
        elements.append(fe)

# ── Main entry point ─────────────────────────────────────────────────────────

def generate_ifc(spec: dict, output_path: str = None) -> str:
    # Run layout packer to fix/assign room coordinates
    try:
        from layout import process_spec
        spec = process_spec(spec)
    except Exception as e:
        logger.warning("Layout packer error (continuing): %s", e)

    name     = spec.get("name","Generated Building")
    floors   = spec.get("floors",[])
    metadata = spec.get("metadata",{})

    has_rooms = any(f.get("rooms") for f in floors)
    mode = "procedural" if has_rooms else "component"
    print(f"Generating IFC [{mode} mode]: {name}")

    m = ifcopenshell.file(schema="IFC4")

    app = m.createIfcApplication(
        m.createIfcOrganization(None,"BIM Studio",None,None,None),
        "1.0","BIM Studio","BIMSTUDIO")
    person = m.createIfcPerson(None,"AI",None,None,None,None,None,None)
    org    = m.createIfcOrganization(None,"BIM Studio",None,None,None)
    pao    = m.createIfcPersonAndOrganization(person,org,None)
    oh     = m.createIfcOwnerHistory(
        pao,app,None,"ADDED",None,pao,app,int(datetime.now().timestamp()))

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

    ifc_storeys = []
    for floor in floors:
        elev = float(floor.get("elevation",0.0))
        sp   = local_pl(m,wp,0,0,elev)
        st   = m.createIfcBuildingStorey(
            ifcopenshell.guid.new(),oh,
            floor.get("name",f"Level {len(ifc_storeys)+1}"),
            None,None,sp,None,None,"ELEMENT",elev)
        ifc_storeys.append(st)

    if ifc_storeys:
        m.createIfcRelAggregates(
            ifcopenshell.guid.new(),oh,None,None,bldg,ifc_storeys)

    for fi, floor in enumerate(floors):
        storey   = ifc_storeys[fi]
        elements = []
        built_walls = set()

        ceil_h_raw = floor.get("height",2.7)
        ceil_h = float(ceil_h_raw)/1000 if float(ceil_h_raw)>10 else float(ceil_h_raw)

        if has_rooms:
            rooms = floor.get("rooms",[])
            for room in rooms:
                build_room(m,oh,body_ctx,room,storey.ObjectPlacement,
                           ceil_h,built_walls,elements)

            # Single unified roof slab
            if rooms:
                min_x = min(float(r.get("x",0)) for r in rooms)
                min_y = min(float(r.get("y",0)) for r in rooms)
                max_x = max(float(r.get("x",0))+float(r.get("width",4)) for r in rooms)
                max_y = max(float(r.get("y",0))+float(r.get("depth",3)) for r in rooms)
                roof = make_el(m,"IfcRoof",oh,"Roof",
                    place(m,min_x,min_y,ceil_h,rel=storey.ObjectPlacement),
                    box_shape(m,body_ctx,max_x-min_x,max_y-min_y,FLOOR_T))
                add_material(m,oh,roof,"Concrete"); elements.append(roof)

            print(f"  Floor '{floor.get('name')}': {len(elements)} elements "
                  f"from {len(rooms)} rooms")
        else:
            # Legacy component mode
            for comp in floor.get("components",[]):
                category = comp.get("category","IfcBuildingElementProxy")
                ifc_type = category if category in (
                    "IfcWall","IfcSlab","IfcRoof","IfcDoor","IfcWindow",
                    "IfcColumn","IfcBeam","IfcStair","IfcRailing",
                    "IfcFurniture","IfcFurnishingElement",
                    "IfcDuctSegment","IfcPipeSegment",
                    "IfcSanitaryTerminal","IfcElectricAppliance","IfcLightFixture",
                ) else "IfcBuildingElementProxy"
                px = float(comp.get("pos_x",0))/1000
                py = float(comp.get("pos_y",0))/1000
                pz = float(comp.get("pos_z",0))/1000
                w  = max(float(comp.get("width_mm",200)),50)/1000
                h  = max(float(comp.get("height_mm",3000)),50)/1000
                l  = max(float(comp.get("length_mm",1000)),50)/1000
                el = make_el(m, ifc_type, oh, comp.get("name",category),
                    place(m,px,py,pz,comp.get("rot_z",0),storey.ObjectPlacement),
                    box_shape(m,body_ctx,l,w,h))
                add_material(m,oh,el,comp.get("material",""))
                elements.append(el)

        if elements:
            m.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(),oh,None,None,elements,storey)

    # Metadata pset
    if metadata:
        props = []
        for k,v in metadata.items():
            if v is None: continue
            try:
                props.append(m.createIfcPropertySingleValue(
                    str(k),None,m.create_entity("IfcLabel",wrappedValue=str(v)),None))
            except Exception as e:
                logger.debug("Failed to create metadata property %s: %s", k, e)
        if props:
            pset = m.createIfcPropertySet(
                ifcopenshell.guid.new(),oh,"BIM_Studio_Project_Info",None,props)
            m.createIfcRelDefinesByProperties(
                ifcopenshell.guid.new(),oh,None,None,[bldg],pset)

    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"generated_{name.replace(' ','_')}_{ts}.ifc"

    m.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_spec = {
        "name":"Test 1BR Apartment",
        "floors":[{
            "name":"Ground Floor","elevation":0.0,"height":2.7,
            "rooms":[
                {"name":"Living Room", "x":0.0,"y":0.0,"width":5.0,"depth":4.0,"exterior":True, "door_wall":"east"},
                {"name":"Kitchen",     "x":5.0,"y":0.0,"width":3.0,"depth":4.0,"exterior":True, "door_wall":"west"},
                {"name":"Bedroom",     "x":0.0,"y":4.0,"width":4.0,"depth":3.5,"exterior":True, "door_wall":"south"},
                {"name":"Bathroom",    "x":4.0,"y":4.0,"width":2.5,"depth":2.0,"exterior":False,"door_wall":"south"},
                {"name":"Hallway",     "x":4.0,"y":6.0,"width":4.0,"depth":1.5,"exterior":False,"door_wall":"west"},
            ]
        }],
        "metadata":{"location":"Test City","building_type":"Residential","estimated_cost_usd":300000}
    }
    path = generate_ifc(test_spec,"/tmp/test_apt.ifc")
    print(f"Output: {path}")
