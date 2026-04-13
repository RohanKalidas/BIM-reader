"""
generate.py — BIM Studio
Procedural IFC generator with styled parametric geometry.

Features:
- Boolean wall openings for doors and windows (doors cut holes in walls)
- Surface colors/materials on all elements (walls, floors, furniture, MEP)
- Cylindrical pipes, rectangular ducts
- Proper door panels and glass windows
- Rooms share walls via deduplication
- AI provides room coordinates; layout.py only validates
"""

import math
import os
import sys
import logging
import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid
from datetime import datetime

logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Color palette (R, G, B, Transparency) ────────────────────────────────────

COLORS = {
    "ext_wall":    (0.92, 0.87, 0.78, 0.0),   # warm beige
    "int_wall":    (0.95, 0.95, 0.93, 0.0),   # off-white
    "floor":       (0.75, 0.72, 0.68, 0.0),   # concrete gray
    "roof":        (0.6,  0.55, 0.50, 0.0),   # darker concrete
    "door":        (0.55, 0.35, 0.17, 0.0),   # wood brown
    "window":      (0.6,  0.78, 0.92, 0.3),   # glass blue, transparent
    "furniture":   (0.7,  0.6,  0.45, 0.0),   # wood/tan
    "bed":         (0.85, 0.82, 0.78, 0.0),   # linen
    "sofa":        (0.45, 0.50, 0.55, 0.0),   # dark gray fabric
    "appliance":   (0.85, 0.85, 0.87, 0.0),   # stainless steel
    "sanitaryware":(0.97, 0.97, 0.97, 0.0),   # white porcelain
    "duct":        (0.5,  0.7,  0.85, 0.0),   # light blue metal
    "pipe_cold":   (0.3,  0.5,  0.75, 0.0),   # blue
    "pipe_hot":    (0.8,  0.3,  0.2,  0.0),   # red
    "light":       (1.0,  0.95, 0.8,  0.0),   # warm white
    "ceiling":     (0.96, 0.96, 0.96, 0.0),   # white
}

# ── IFC primitives ───────────────────────────────────────────────────────────

def pt3(m,x,y,z): return m.createIfcCartesianPoint((float(x),float(y),float(z)))
def pt2(m,x,y):   return m.createIfcCartesianPoint((float(x),float(y)))
def d3(m,x,y,z):  return m.createIfcDirection((float(x),float(y),float(z)))
def d2(m,x,y):    return m.createIfcDirection((float(x),float(y)))

def ax3(m, ox=0,oy=0,oz=0, az=None, rx=None):
    return m.createIfcAxis2Placement3D(
        pt3(m,ox,oy,oz), az or d3(m,0,0,1), rx or d3(m,1,0,0))

def ax2(m, ox=0, oy=0):
    return m.createIfcAxis2Placement2D(pt2(m,ox,oy), d2(m,1,0))

def local_pl(m, rel=None, ox=0,oy=0,oz=0):
    return m.createIfcLocalPlacement(rel, ax3(m,ox,oy,oz))

def make_context(m):
    ctx  = m.createIfcGeometricRepresentationContext(None,"Model",3,1e-5,ax3(m),None)
    body = m.createIfcGeometricRepresentationSubContext(
        "Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)
    return ctx, body

def rect_profile(m, w, d):
    return m.createIfcRectangleProfileDef("AREA", None, ax2(m), float(w), float(d))

def circle_profile(m, radius):
    return m.createIfcCircleProfileDef("AREA", None, ax2(m), float(radius))

def extrude(m, profile, height, ox=0, oy=0, oz=0):
    return m.createIfcExtrudedAreaSolid(profile, ax3(m,ox,oy,oz), d3(m,0,0,1), float(height))

def make_shape(m, body_ctx, solids, rep_type="SweptSolid"):
    rep = m.createIfcShapeRepresentation(body_ctx, "Body", rep_type, solids)
    return m.createIfcProductDefinitionShape(None, None, [rep]), rep

def place(m, x, y, z, rz_deg=0, rel=None):
    rz = math.radians(float(rz_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    a = ax3(m, x, y, z, d3(m,0,0,1), d3(m,cz,sz,0))
    return m.createIfcLocalPlacement(rel, a)

def make_el(m, ifc_type, oh, name, pl, shape):
    try:
        return m.create_entity(ifc_type,
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            Name=name, ObjectPlacement=pl, Representation=shape)
    except Exception:
        return m.createIfcBuildingElementProxy(
            ifcopenshell.guid.new(), oh, name, None, None, pl, shape, None, "ELEMENT")

# ── Style helpers ────────────────────────────────────────────────────────────

_style_cache = {}

def add_color(m, rep, color_key):
    """Apply a surface color to a shape representation."""
    if color_key not in COLORS:
        return
    r, g, b, t = COLORS[color_key]

    cache_key = (id(m), color_key)
    if cache_key not in _style_cache:
        style = ifcopenshell.api.run("style.add_style", m, name=color_key)
        ifcopenshell.api.run("style.add_surface_style", m,
            style=style, ifc_class="IfcSurfaceStyleRendering",
            attributes={"SurfaceColour": {"Name": None, "Red": r, "Green": g, "Blue": b},
                        "Transparency": t})
        _style_cache[cache_key] = style
    else:
        style = _style_cache[cache_key]

    try:
        ifcopenshell.api.run("style.assign_representation_styles", m,
            shape_representation=rep, styles=[style])
    except Exception:
        pass

# ── Constants ────────────────────────────────────────────────────────────────

EXT_T  = 0.20    # exterior wall thickness
INT_T  = 0.12    # interior wall thickness
FLOOR_T = 0.20   # slab thickness
CEIL_T  = 0.02   # ceiling plane thickness
DOOR_W  = 0.90
DOOR_H  = 2.10
DOOR_T  = 0.05   # door panel thickness
WIN_W   = 1.20
WIN_H   = 1.10
WIN_SILL = 0.90
WIN_T   = 0.06   # window glass thickness
DUCT_W  = 0.40
DUCT_H  = 0.20
PIPE_R  = 0.025  # pipe outer radius

# ── Room type ────────────────────────────────────────────────────────────────

def room_type(name):
    n = name.lower()
    if any(x in n for x in ["bath","wc","toilet","shower","lavatory"]): return "bathroom"
    if any(x in n for x in ["kitchen","cook"]):     return "kitchen"
    if any(x in n for x in ["bed","master","guest","sleep"]): return "bedroom"
    if any(x in n for x in ["living","lounge","family","great"]): return "living"
    if any(x in n for x in ["dining","eat"]):       return "dining"
    if any(x in n for x in ["office","study"]):     return "office"
    if any(x in n for x in ["hall","corridor","foyer","entry","lobby"]): return "hallway"
    if any(x in n for x in ["utility","laundry","storage","plant"]): return "utility"
    if any(x in n for x in ["patio","terrace","balcony","deck"]): return "patio"
    if any(x in n for x in ["garage","parking"]):   return "garage"
    if any(x in n for x in ["conference","meeting"]): return "conference"
    if any(x in n for x in ["reception","lobby"]):  return "reception"
    if any(x in n for x in ["server","it","data"]): return "server"
    return "living"

# ── Fixture definitions ──────────────────────────────────────────────────────
# (name, fx, fy, fw, fd, fh, color_key)
# fx/fy = fraction of inner room

FIXTURES = {
    "bathroom": [
        ("Toilet",   0.15, 0.20, 0.38, 0.65, 0.45, "sanitaryware"),
        ("Sink",     0.70, 0.10, 0.50, 0.40, 0.85, "sanitaryware"),
        ("Shower Tray", 0.55, 0.65, 0.90, 0.90, 0.10, "sanitaryware"),
    ],
    "kitchen": [
        ("Counter",  0.50, 0.06, 2.00, 0.60, 0.90, "furniture"),
        ("Stove",    0.70, 0.06, 0.60, 0.60, 0.90, "appliance"),
        ("Fridge",   0.90, 0.06, 0.70, 0.70, 1.80, "appliance"),
    ],
    "bedroom": [
        ("Bed",       0.50, 0.60, 1.60, 2.00, 0.50, "bed"),
        ("Wardrobe",  0.10, 0.08, 1.20, 0.60, 2.10, "furniture"),
        ("Nightstand",0.85, 0.60, 0.45, 0.40, 0.50, "furniture"),
    ],
    "living": [
        ("Sofa",         0.50, 0.75, 2.20, 0.90, 0.80, "sofa"),
        ("Coffee Table", 0.50, 0.50, 1.00, 0.55, 0.42, "furniture"),
        ("TV Unit",      0.50, 0.08, 1.50, 0.40, 0.50, "furniture"),
    ],
    "dining": [
        ("Dining Table", 0.50, 0.50, 1.60, 0.90, 0.75, "furniture"),
        ("Chair 1",      0.30, 0.30, 0.45, 0.45, 0.85, "furniture"),
        ("Chair 2",      0.70, 0.30, 0.45, 0.45, 0.85, "furniture"),
        ("Chair 3",      0.30, 0.70, 0.45, 0.45, 0.85, "furniture"),
        ("Chair 4",      0.70, 0.70, 0.45, 0.45, 0.85, "furniture"),
    ],
    "office": [
        ("Desk",  0.50, 0.25, 1.40, 0.70, 0.75, "furniture"),
        ("Chair", 0.50, 0.50, 0.55, 0.55, 1.05, "sofa"),
    ],
    "conference": [
        ("Table",    0.50, 0.50, 2.40, 1.20, 0.75, "furniture"),
        ("Chair 1",  0.20, 0.25, 0.45, 0.45, 0.85, "sofa"),
        ("Chair 2",  0.80, 0.25, 0.45, 0.45, 0.85, "sofa"),
        ("Chair 3",  0.20, 0.75, 0.45, 0.45, 0.85, "sofa"),
        ("Chair 4",  0.80, 0.75, 0.45, 0.45, 0.85, "sofa"),
    ],
    "reception": [
        ("Sofa",     0.50, 0.70, 2.20, 0.90, 0.80, "sofa"),
        ("Desk",     0.50, 0.15, 1.80, 0.70, 0.75, "furniture"),
    ],
    "server": [
        ("Server Rack 1", 0.25, 0.50, 0.60, 0.80, 2.00, "appliance"),
        ("Server Rack 2", 0.75, 0.50, 0.60, 0.80, 2.00, "appliance"),
    ],
    "utility": [
        ("Heater",  0.25, 0.25, 0.55, 0.55, 1.50, "appliance"),
        ("Washer",  0.75, 0.25, 0.60, 0.60, 0.90, "appliance"),
    ],
    "hallway": [],
    "patio":   [],
    "garage":  [],
}

# ── Wall deduplication ───────────────────────────────────────────────────────

def wall_key(x1, y1, x2, y2):
    a = (round(x1, 3), round(y1, 3))
    b = (round(x2, 3), round(y2, 3))
    return (min(a, b), max(a, b))

# ── Build room ───────────────────────────────────────────────────────────────

def build_room(m, oh, body_ctx, room, storey_pl, ceil_h, built_walls, elements):
    rx    = float(room.get("x", 0))
    ry    = float(room.get("y", 0))
    rw    = float(room.get("width", 4.0))
    rd    = float(room.get("depth", 3.0))
    rh    = float(room.get("height", ceil_h))
    rname = room.get("name", "Room")
    ext   = room.get("exterior", True)
    wt    = EXT_T if ext else INT_T
    rtype = room_type(rname)
    wall_color = "ext_wall" if ext else "int_wall"

    # ── Floor slab ───────────────────────────────────────────────────────
    shape, rep = make_shape(m, body_ctx, [extrude(m, rect_profile(m, rw, rd), FLOOR_T, rx, ry, -FLOOR_T)])
    fl = make_el(m, "IfcSlab", oh, f"{rname} Floor", place(m, 0, 0, 0, rel=storey_pl), shape)
    add_color(m, rep, "floor")
    elements.append(fl)

    # ── Ceiling ──────────────────────────────────────────────────────────
    shape_c, rep_c = make_shape(m, body_ctx, [extrude(m, rect_profile(m, rw-2*wt, rd-2*wt), CEIL_T, rx+wt, ry+wt, rh-CEIL_T)])
    ceil = make_el(m, "IfcCovering", oh, f"{rname} Ceiling", place(m, 0, 0, 0, rel=storey_pl), shape_c)
    add_color(m, rep_c, "ceiling")
    elements.append(ceil)

    # ── Door info (for wall boolean cuts) ────────────────────────────────
    dwall = room.get("door_wall", "south")
    doff  = max(wt + 0.15, 0.4)

    # ── Walls (deduplicated, with door openings) ─────────────────────────
    wall_specs = [
        ("S", rx, ry, rw, wt, dwall == "south"),
        ("N", rx, ry + rd - wt, rw, wt, dwall == "north"),
        ("W", rx, ry + wt, wt, rd - 2 * wt, dwall == "west"),
        ("E", rx + rw - wt, ry + wt, wt, rd - 2 * wt, dwall == "east"),
    ]
    wall_key_specs = [
        wall_key(rx, ry, rx + rw, ry + wt),
        wall_key(rx, ry + rd - wt, rx + rw, ry + rd),
        wall_key(rx, ry + wt, rx + wt, ry + rd - wt),
        wall_key(rx + rw - wt, ry + wt, rx + rw, ry + rd - wt),
    ]

    for (side, wx, wy, wlx, wly, has_door), wk in zip(wall_specs, wall_key_specs):
        if wk in built_walls:
            continue
        built_walls.add(wk)
        if wlx <= 0 or wly <= 0:
            continue

        wall_solid = extrude(m, rect_profile(m, wlx, wly), rh, wx, wy, 0)

        if has_door:
            # Boolean cut for door opening
            if side in ("S", "N"):
                dx = wx + doff
                door_void = extrude(m, rect_profile(m, DOOR_W, wt + 0.02), DOOR_H, dx, wy - 0.01, 0)
            else:
                dy = wy + doff
                door_void = extrude(m, rect_profile(m, wt + 0.02, DOOR_W), DOOR_H, wx - 0.01, dy, 0)

            wall_solid = m.createIfcBooleanClippingResult("DIFFERENCE", wall_solid, door_void)

            # Window opening too if exterior
            if ext and ((side in ("S", "N") and wlx >= 3.0) or (side in ("W", "E") and wly >= 3.0)):
                if side in ("S", "N"):
                    winx = wx + wlx / 2 - WIN_W / 2
                    win_void = extrude(m, rect_profile(m, WIN_W, wt + 0.02), WIN_H, winx, wy - 0.01, WIN_SILL)
                else:
                    winy = wy + wly / 2 - WIN_W / 2
                    win_void = extrude(m, rect_profile(m, wt + 0.02, WIN_W), WIN_H, wx - 0.01, winy, WIN_SILL)
                wall_solid = m.createIfcBooleanClippingResult("DIFFERENCE", wall_solid, win_void)

            rep_type = "Clipping"
        elif ext:
            # Window opening on exterior walls without doors (if wide enough)
            if (side in ("S", "N") and wlx >= 2.5) or (side in ("W", "E") and wly >= 2.5):
                if side in ("S", "N"):
                    winx = wx + wlx / 2 - WIN_W / 2
                    win_void = extrude(m, rect_profile(m, WIN_W, wt + 0.02), WIN_H, winx, wy - 0.01, WIN_SILL)
                else:
                    winy = wy + wly / 2 - WIN_W / 2
                    win_void = extrude(m, rect_profile(m, wt + 0.02, WIN_W), WIN_H, wx - 0.01, winy, WIN_SILL)
                wall_solid = m.createIfcBooleanClippingResult("DIFFERENCE", wall_solid, win_void)
                rep_type = "Clipping"
            else:
                rep_type = "SweptSolid"
        else:
            rep_type = "SweptSolid"

        shape_w, rep_w = make_shape(m, body_ctx, [wall_solid], rep_type)
        w = make_el(m, "IfcWall", oh, f"{rname} {side} Wall", place(m, 0, 0, 0, rel=storey_pl), shape_w)
        add_color(m, rep_w, wall_color)
        elements.append(w)

    # ── Door panel ───────────────────────────────────────────────────────
    if dwall == "south":
        dx, dy = rx + doff + 0.025, ry + 0.01
        dw_x, dw_y = DOOR_W - 0.05, DOOR_T
    elif dwall == "north":
        dx, dy = rx + doff + 0.025, ry + rd - wt + 0.01
        dw_x, dw_y = DOOR_W - 0.05, DOOR_T
    elif dwall == "west":
        dx, dy = rx + 0.01, ry + wt + doff + 0.025
        dw_x, dw_y = DOOR_T, DOOR_W - 0.05
    else:
        dx, dy = rx + rw - wt + 0.01, ry + wt + doff + 0.025
        dw_x, dw_y = DOOR_T, DOOR_W - 0.05

    shape_d, rep_d = make_shape(m, body_ctx, [extrude(m, rect_profile(m, dw_x, dw_y), DOOR_H - 0.05, dx, dy, 0)])
    door = make_el(m, "IfcDoor", oh, f"{rname} Door", place(m, 0, 0, 0, rel=storey_pl), shape_d)
    add_color(m, rep_d, "door")
    elements.append(door)

    # ── Windows (glass panes in openings) ────────────────────────────────
    if ext and rw >= 2.5:
        # South wall window
        winx = rx + rw / 2 - WIN_W / 2 + 0.02
        shape_win, rep_win = make_shape(m, body_ctx, [extrude(m, rect_profile(m, WIN_W - 0.04, WIN_T), WIN_H - 0.04, winx, ry + wt/2 - WIN_T/2, WIN_SILL + 0.02)])
        win = make_el(m, "IfcWindow", oh, f"{rname} Window", place(m, 0, 0, 0, rel=storey_pl), shape_win)
        add_color(m, rep_win, "window")
        elements.append(win)

    # ── Light fixture (flat disc on ceiling) ─────────────────────────────
    lx = rx + rw / 2
    ly = ry + rd / 2
    shape_l, rep_l = make_shape(m, body_ctx, [extrude(m, circle_profile(m, 0.15), 0.03, lx, ly, rh - 0.04)])
    light = make_el(m, "IfcLightFixture", oh, f"{rname} Light", place(m, 0, 0, 0, rel=storey_pl), shape_l)
    add_color(m, rep_l, "light")
    elements.append(light)

    # ── Supply duct (rectangular, at ceiling) ────────────────────────────
    if rtype not in ("patio", "garage", "hallway") and rw > 1.5:
        duct_len = rw - 2 * wt - 0.2
        if duct_len > 0.5:
            duct_x = rx + wt + 0.1
            duct_y = ry + rd / 2 - DUCT_W / 2
            duct_z = rh - DUCT_H - 0.08
            shape_duct, rep_duct = make_shape(m, body_ctx,
                [extrude(m, rect_profile(m, duct_len, DUCT_W), DUCT_H, duct_x, duct_y, duct_z)])
            duct = make_el(m, "IfcDuctSegment", oh, f"{rname} Duct",
                place(m, 0, 0, 0, rel=storey_pl), shape_duct)
            add_color(m, rep_duct, "duct")
            elements.append(duct)

    # ── Cold water pipe (cylindrical, wet rooms only) ────────────────────
    if rtype in ("bathroom", "kitchen", "utility"):
        pipe_len = rd - 2 * wt - 0.2
        if pipe_len > 0.3:
            px = rx + wt + 0.15
            py = ry + wt + 0.1
            # Extrude cylinder along Y by using rotated axis
            pipe_solid = m.createIfcExtrudedAreaSolid(
                circle_profile(m, PIPE_R),
                ax3(m, px, py, 0.12, d3(m, 0, 1, 0), d3(m, 1, 0, 0)),
                d3(m, 0, 0, 1), pipe_len)
            shape_p, rep_p = make_shape(m, body_ctx, [pipe_solid])
            pipe = make_el(m, "IfcPipeSegment", oh, f"{rname} Cold Water",
                place(m, 0, 0, 0, rel=storey_pl), shape_p)
            add_color(m, rep_p, "pipe_cold")
            elements.append(pipe)

    # ── Furniture & fixtures ─────────────────────────────────────────────
    fixture_list = FIXTURES.get(rtype, [])
    inner_w = max(rw - 2 * wt, 0.5)
    inner_d = max(rd - 2 * wt, 0.5)

    for fixture in fixture_list:
        fname, fx, fy, fw, fd, fh, fcolor = fixture
        ax = rx + wt + fx * inner_w - fw / 2
        ay = ry + wt + fy * inner_d - fd / 2
        ax = max(rx + wt + 0.05, min(ax, rx + rw - wt - fw - 0.05))
        ay = max(ry + wt + 0.05, min(ay, ry + rd - wt - fd - 0.05))
        fw = max(fw, 0.1)
        fd = max(fd, 0.1)
        fh = max(fh, 0.05)

        shape_f, rep_f = make_shape(m, body_ctx, [extrude(m, rect_profile(m, fw, fd), fh, ax, ay, 0)])
        ifc_type = "IfcFurnishingElement"
        if "toilet" in fname.lower() or "sink" in fname.lower() or "shower" in fname.lower():
            ifc_type = "IfcSanitaryTerminal"
        elif "stove" in fname.lower() or "fridge" in fname.lower() or "heater" in fname.lower() or "washer" in fname.lower() or "rack" in fname.lower():
            ifc_type = "IfcElectricAppliance"

        el = make_el(m, ifc_type, oh, f"{rname} {fname}", place(m, 0, 0, 0, rel=storey_pl), shape_f)
        add_color(m, rep_f, fcolor)
        elements.append(el)


# ── Main entry point ─────────────────────────────────────────────────────────

def generate_ifc(spec, output_path=None):
    global _style_cache
    _style_cache = {}

    try:
        from layout import process_spec
        spec = process_spec(spec)
    except Exception as e:
        print(f"  Layout processing: {e}")

    name   = spec.get("name", "Generated Building")
    floors = spec.get("floors", [])
    metadata = spec.get("metadata", {})
    has_rooms = any(f.get("rooms") for f in floors)

    print(f"Generating IFC: {name}")

    m = ifcopenshell.file(schema="IFC4")

    # Setup
    app = m.createIfcApplication(
        m.createIfcOrganization(None, "BIM Studio", None, None, None),
        "1.0", "BIM Studio", "BIMSTUDIO")
    person = m.createIfcPerson(None, "AI", None, None, None, None, None, None)
    org = m.createIfcOrganization(None, "BIM Studio", None, None, None)
    pao = m.createIfcPersonAndOrganization(person, org, None)
    oh = m.createIfcOwnerHistory(pao, app, None, "ADDED", None, pao, app, int(datetime.now().timestamp()))

    units = m.createIfcUnitAssignment([
        m.createIfcSIUnit(None, "LENGTHUNIT", None, "METRE"),
        m.createIfcSIUnit(None, "AREAUNIT", None, "SQUARE_METRE"),
        m.createIfcSIUnit(None, "VOLUMEUNIT", None, "CUBIC_METRE"),
        m.createIfcSIUnit(None, "PLANEANGLEUNIT", None, "RADIAN"),
    ])

    geom_ctx, body_ctx = make_context(m)
    wp = local_pl(m)

    proj = m.createIfcProject(ifcopenshell.guid.new(), oh, name, None, None, None, None, [geom_ctx], units)
    site = m.createIfcSite(ifcopenshell.guid.new(), oh, metadata.get("location", "Site"),
        None, None, wp, None, None, "ELEMENT", None, None, None, None, None)
    bldg = m.createIfcBuilding(ifcopenshell.guid.new(), oh, name,
        None, None, wp, None, None, "ELEMENT", None, None, None)
    m.createIfcRelAggregates(ifcopenshell.guid.new(), oh, None, None, proj, [site])
    m.createIfcRelAggregates(ifcopenshell.guid.new(), oh, None, None, site, [bldg])

    ifc_storeys = []
    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        sp = local_pl(m, wp, 0, 0, elev)
        st = m.createIfcBuildingStorey(ifcopenshell.guid.new(), oh,
            floor.get("name", f"Level {len(ifc_storeys)+1}"),
            None, None, sp, None, None, "ELEMENT", elev)
        ifc_storeys.append(st)

    if ifc_storeys:
        m.createIfcRelAggregates(ifcopenshell.guid.new(), oh, None, None, bldg, ifc_storeys)

    for fi, floor in enumerate(floors):
        storey = ifc_storeys[fi]
        elements = []
        built_walls = set()

        ceil_h_raw = floor.get("height", 2.7)
        ceil_h = float(ceil_h_raw) / 1000 if float(ceil_h_raw) > 10 else float(ceil_h_raw)

        if has_rooms:
            rooms = floor.get("rooms", [])
            for room in rooms:
                build_room(m, oh, body_ctx, room, storey.ObjectPlacement,
                           ceil_h, built_walls, elements)

            # Roof slab
            if rooms:
                min_x = min(float(r.get("x", 0)) for r in rooms)
                min_y = min(float(r.get("y", 0)) for r in rooms)
                max_x = max(float(r.get("x", 0)) + float(r.get("width", 4)) for r in rooms)
                max_y = max(float(r.get("y", 0)) + float(r.get("depth", 3)) for r in rooms)
                shape_r, rep_r = make_shape(m, body_ctx,
                    [extrude(m, rect_profile(m, max_x - min_x, max_y - min_y), FLOOR_T, min_x, min_y, ceil_h)])
                roof = make_el(m, "IfcRoof", oh, "Roof", place(m, 0, 0, 0, rel=storey.ObjectPlacement), shape_r)
                add_color(m, rep_r, "roof")
                elements.append(roof)

            print(f"  Floor '{floor.get('name')}': {len(elements)} elements from {len(rooms)} rooms")

        if elements:
            m.createIfcRelContainedInSpatialStructure(
                ifcopenshell.guid.new(), oh, None, None, elements, storey)

    # Metadata pset
    if metadata:
        props = []
        for k, v in metadata.items():
            if v is None:
                continue
            try:
                props.append(m.createIfcPropertySingleValue(
                    str(k), None, m.create_entity("IfcLabel", wrappedValue=str(v)), None))
            except Exception:
                pass
        if props:
            pset = m.createIfcPropertySet(ifcopenshell.guid.new(), oh, "BIM_Studio_Project_Info", None, props)
            m.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), oh, None, None, [bldg], pset)

    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"generated_{name.replace(' ', '_')}_{ts}.ifc"

    m.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    spec = {
        "name": "Test Apartment",
        "floors": [{
            "name": "Ground Floor", "elevation": 0.0, "height": 2.7,
            "rooms": [
                {"name": "Living Room", "x": 0.0, "y": 0.0, "width": 5.0, "depth": 4.0, "exterior": True, "door_wall": "north"},
                {"name": "Kitchen",     "x": 5.0, "y": 0.0, "width": 3.0, "depth": 4.0, "exterior": True, "door_wall": "north"},
                {"name": "Hallway",     "x": 0.0, "y": 4.0, "width": 8.0, "depth": 1.5, "exterior": False, "door_wall": "east"},
                {"name": "Bedroom",     "x": 0.0, "y": 5.5, "width": 4.5, "depth": 4.0, "exterior": True, "door_wall": "south"},
                {"name": "Bathroom",    "x": 4.5, "y": 5.5, "width": 3.5, "depth": 4.0, "exterior": True, "door_wall": "south"},
            ]
        }],
        "metadata": {"location": "Orange County, FL", "building_type": "Residential"}
    }
    generate_ifc(spec, "/tmp/test_styled.ifc")
