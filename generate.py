"""
generate.py — BIM Studio v9
Procedural IFC generator with shared walls, colors, and boolean openings.

Wall strategy: Instead of each room building 4 independent walls, we:
1. Collect all wall edges from all rooms
2. Detect shared edges (where two rooms meet)
3. Build ONE wall per unique edge, using interior thickness for shared
   walls and exterior thickness for perimeter walls
4. Cut door/window openings via boolean operations
"""

import math
import os
import sys
import logging
import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Color palette ────────────────────────────────────────────────────────────

COLORS = {
    "ext_wall":     (0.92, 0.87, 0.78, 0.0),
    "int_wall":     (0.95, 0.95, 0.93, 0.0),
    "floor":        (0.75, 0.72, 0.68, 0.0),
    "roof":         (0.6,  0.55, 0.50, 0.0),
    "door":         (0.55, 0.35, 0.17, 0.0),
    "window":       (0.6,  0.78, 0.92, 0.3),
    "furniture":    (0.7,  0.6,  0.45, 0.0),
    "bed":          (0.85, 0.82, 0.78, 0.0),
    "sofa":         (0.45, 0.50, 0.55, 0.0),
    "appliance":    (0.85, 0.85, 0.87, 0.0),
    "sanitaryware": (0.97, 0.97, 0.97, 0.0),
    "duct":         (0.5,  0.7,  0.85, 0.0),
    "pipe":         (0.3,  0.5,  0.75, 0.0),
    "light":        (1.0,  0.95, 0.8,  0.0),
    "ceiling":      (0.96, 0.96, 0.96, 0.0),
}

# ── IFC primitives ───────────────────────────────────────────────────────────

def pt3(m,x,y,z): return m.createIfcCartesianPoint((float(x),float(y),float(z)))
def pt2(m,x,y):   return m.createIfcCartesianPoint((float(x),float(y)))
def d3(m,x,y,z):  return m.createIfcDirection((float(x),float(y),float(z)))
def d2(m,x,y):    return m.createIfcDirection((float(x),float(y)))
def ax3(m, ox=0,oy=0,oz=0, az=None, rx=None):
    return m.createIfcAxis2Placement3D(pt3(m,ox,oy,oz), az or d3(m,0,0,1), rx or d3(m,1,0,0))
def ax2(m, ox=0, oy=0):
    return m.createIfcAxis2Placement2D(pt2(m,ox,oy), d2(m,1,0))
def local_pl(m, rel=None, ox=0,oy=0,oz=0):
    return m.createIfcLocalPlacement(rel, ax3(m,ox,oy,oz))
def make_context(m):
    ctx  = m.createIfcGeometricRepresentationContext(None,"Model",3,1e-5,ax3(m),None)
    body = m.createIfcGeometricRepresentationSubContext("Body","Model",None,None,None,None,ctx,None,"MODEL_VIEW",None)
    return ctx, body
def rect_profile(m, w, d):
    return m.createIfcRectangleProfileDef("AREA", None, ax2(m), float(w), float(d))
def circle_profile(m, r):
    return m.createIfcCircleProfileDef("AREA", None, ax2(m), float(r))
def extrude(m, profile, height, ox=0, oy=0, oz=0):
    return m.createIfcExtrudedAreaSolid(profile, ax3(m,ox,oy,oz), d3(m,0,0,1), float(height))
def make_shape(m, body_ctx, solids, rep_type="SweptSolid"):
    rep = m.createIfcShapeRepresentation(body_ctx, "Body", rep_type, solids)
    return m.createIfcProductDefinitionShape(None, None, [rep]), rep
def place(m, x, y, z, rz_deg=0, rel=None):
    rz = math.radians(float(rz_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    return m.createIfcLocalPlacement(rel, ax3(m, x, y, z, d3(m,0,0,1), d3(m,cz,sz,0)))
def make_el(m, ifc_type, oh, name, pl, shape):
    try:
        return m.create_entity(ifc_type, GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            Name=name, ObjectPlacement=pl, Representation=shape)
    except Exception:
        return m.createIfcBuildingElementProxy(ifcopenshell.guid.new(), oh, name, None, None, pl, shape, None, "ELEMENT")

# ── Style helpers ────────────────────────────────────────────────────────────

_style_cache = {}

def add_color(m, rep, color_key):
    if color_key not in COLORS:
        return
    r, g, b, t = COLORS[color_key]
    cache_key = (id(m), color_key)
    if cache_key not in _style_cache:
        style = ifcopenshell.api.run("style.add_style", m, name=color_key)
        ifcopenshell.api.run("style.add_surface_style", m, style=style,
            ifc_class="IfcSurfaceStyleRendering",
            attributes={"SurfaceColour": {"Name": None, "Red": r, "Green": g, "Blue": b}, "Transparency": t})
        _style_cache[cache_key] = style
    try:
        ifcopenshell.api.run("style.assign_representation_styles", m,
            shape_representation=rep, styles=[_style_cache[cache_key]])
    except Exception:
        pass

# ── Constants ────────────────────────────────────────────────────────────────

EXT_T   = 0.20
INT_T   = 0.12
FLOOR_T = 0.20
CEIL_T  = 0.02
DOOR_W  = 0.90
DOOR_H  = 2.10
DOOR_T  = 0.05
WIN_W   = 1.20
WIN_H   = 1.10
WIN_SILL = 0.90
WIN_T   = 0.06
DUCT_W  = 0.40
DUCT_H  = 0.15
PIPE_W  = 0.05
PIPE_H  = 0.05

# ── Room type / fixtures ────────────────────────────────────────────────────

def room_type(name):
    n = name.lower()
    for kw, rt in [
        (["bath","wc","toilet","shower","lavatory"], "bathroom"),
        (["kitchen","cook"], "kitchen"),
        (["bed","master","guest","sleep"], "bedroom"),
        (["living","lounge","family","great"], "living"),
        (["dining","eat"], "dining"),
        (["office","study"], "office"),
        (["hall","corridor","foyer","entry","lobby"], "hallway"),
        (["utility","laundry","storage","plant"], "utility"),
        (["patio","terrace","balcony","deck"], "patio"),
        (["garage","parking"], "garage"),
        (["conference","meeting"], "conference"),
        (["reception"], "reception"),
        (["server","it","data"], "server"),
    ]:
        if any(x in n for x in kw):
            return rt
    return "living"

# (name, fx, fy, fw, fd, fh, color)
FIXTURES = {
    "bathroom":   [("Toilet",0.15,0.20,0.38,0.65,0.45,"sanitaryware"),("Sink",0.70,0.10,0.50,0.40,0.85,"sanitaryware"),("Shower Tray",0.55,0.65,0.90,0.90,0.10,"sanitaryware")],
    "kitchen":    [("Counter",0.50,0.06,2.00,0.60,0.90,"furniture"),("Stove",0.70,0.06,0.60,0.60,0.90,"appliance"),("Fridge",0.90,0.06,0.70,0.70,1.80,"appliance")],
    "bedroom":    [("Bed",0.50,0.60,1.60,2.00,0.50,"bed"),("Wardrobe",0.10,0.08,1.20,0.60,2.10,"furniture"),("Nightstand",0.85,0.60,0.45,0.40,0.50,"furniture")],
    "living":     [("Sofa",0.50,0.75,2.20,0.90,0.80,"sofa"),("Coffee Table",0.50,0.50,1.00,0.55,0.42,"furniture"),("TV Unit",0.50,0.08,1.50,0.40,0.50,"furniture")],
    "dining":     [("Table",0.50,0.50,1.60,0.90,0.75,"furniture"),("Chair",0.30,0.30,0.45,0.45,0.85,"furniture"),("Chair",0.70,0.30,0.45,0.45,0.85,"furniture"),("Chair",0.30,0.70,0.45,0.45,0.85,"furniture"),("Chair",0.70,0.70,0.45,0.45,0.85,"furniture")],
    "office":     [("Desk",0.50,0.25,1.40,0.70,0.75,"furniture"),("Chair",0.50,0.50,0.55,0.55,1.05,"sofa")],
    "conference": [("Table",0.50,0.50,2.40,1.20,0.75,"furniture"),("Chair",0.20,0.25,0.45,0.45,0.85,"sofa"),("Chair",0.80,0.25,0.45,0.45,0.85,"sofa"),("Chair",0.20,0.75,0.45,0.45,0.85,"sofa"),("Chair",0.80,0.75,0.45,0.45,0.85,"sofa")],
    "reception":  [("Sofa",0.50,0.70,2.20,0.90,0.80,"sofa"),("Desk",0.50,0.15,1.80,0.70,0.75,"furniture")],
    "server":     [("Rack",0.25,0.50,0.60,0.80,2.00,"appliance"),("Rack",0.75,0.50,0.60,0.80,2.00,"appliance")],
    "utility":    [("Heater",0.25,0.25,0.55,0.55,1.50,"appliance"),("Washer",0.75,0.25,0.60,0.60,0.90,"appliance")],
    "hallway": [], "patio": [], "garage": [],
}

# ── Wall planning ────────────────────────────────────────────────────────────

def plan_walls(rooms):
    """
    Analyze all rooms to determine which edges are shared (interior) vs
    perimeter (exterior). Returns a list of wall segments to build.
    
    Each wall segment: (x, y, width, depth, wall_thickness, is_exterior, 
                        side, room_name, door_on_this_wall, has_window)
    """
    # Build a set of all room rectangles
    rects = []
    for r in rooms:
        rects.append({
            "name": r.get("name", "Room"),
            "x": float(r.get("x", 0)),
            "y": float(r.get("y", 0)),
            "w": float(r.get("width", 4)),
            "d": float(r.get("depth", 3)),
            "door_wall": r.get("door_wall", "south"),
            "exterior": r.get("exterior", True),
        })

    # Find building bounding box
    min_x = min(r["x"] for r in rects)
    max_x = max(r["x"] + r["w"] for r in rects)
    min_y = min(r["y"] for r in rects)
    max_y = max(r["y"] + r["d"] for r in rects)

    def is_perimeter(x1, y1, x2, y2):
        """Check if a wall edge is on the building perimeter."""
        # South edge
        if abs(y1 - min_y) < 0.01 and abs(y2 - min_y) < 0.01:
            return True
        # North edge
        if abs(y1 - max_y) < 0.01 and abs(y2 - max_y) < 0.01:
            return True
        # West edge
        if abs(x1 - min_x) < 0.01 and abs(x2 - min_x) < 0.01:
            return True
        # East edge
        if abs(x1 - max_x) < 0.01 and abs(x2 - max_x) < 0.01:
            return True
        return False

    def edge_key(x1, y1, x2, y2):
        a = (round(x1, 2), round(y1, 2))
        b = (round(x2, 2), round(y2, 2))
        return (min(a, b), max(a, b))

    # Collect all edges with their properties
    edges = {}  # edge_key -> {rooms, is_perimeter, side, ...}

    for r in rects:
        rx, ry, rw, rd = r["x"], r["y"], r["w"], r["d"]
        room_edges = [
            ("S", rx, ry, rx + rw, ry),           # south
            ("N", rx, ry + rd, rx + rw, ry + rd),  # north
            ("W", rx, ry, rx, ry + rd),             # west
            ("E", rx + rw, ry, rx + rw, ry + rd),   # east
        ]

        for side, x1, y1, x2, y2 in room_edges:
            ek = edge_key(x1, y1, x2, y2)
            perim = is_perimeter(x1, y1, x2, y2)
            has_door = (r["door_wall"] == {"S":"south","N":"north","W":"west","E":"east"}[side])

            if ek not in edges:
                edges[ek] = {
                    "rooms": [],
                    "is_perimeter": perim,
                    "side": side,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "has_door": has_door,
                }
            else:
                # Edge shared by two rooms — it's interior
                edges[ek]["is_perimeter"] = False
                if has_door:
                    edges[ek]["has_door"] = True
            edges[ek]["rooms"].append(r["name"])

    # Convert to wall segments with geometry
    walls = []
    for ek, info in edges.items():
        x1, y1, x2, y2 = info["x1"], info["y1"], info["x2"], info["y2"]
        is_ext = info["is_perimeter"]
        wt = EXT_T if is_ext else INT_T
        side = info["side"]
        has_door = info["has_door"]

        # Determine wall rectangle — centered on the edge line
        if side in ("S", "N"):
            # Horizontal wall along this Y line
            length = abs(x2 - x1)
            wx = min(x1, x2)
            wy = min(y1, y2) - wt / 2  # center on edge
            ww = length
            wd = wt
        else:
            # Vertical wall along this X line
            length = abs(y2 - y1)
            wx = min(x1, x2) - wt / 2  # center on edge
            wy = min(y1, y2)
            ww = wt
            wd = length

        # Has window? Only on exterior walls that are wide enough
        has_window = is_ext and ((side in ("S","N") and ww >= 2.5) or (side in ("W","E") and wd >= 2.5))

        walls.append({
            "x": wx, "y": wy, "w": ww, "d": wd,
            "thickness": wt, "is_exterior": is_ext,
            "side": side, "rooms": info["rooms"],
            "has_door": has_door, "has_window": has_window,
        })

    return walls


# ── Build walls from plan ────────────────────────────────────────────────────

def build_walls(m, oh, body_ctx, storey_pl, walls, ceil_h, elements):
    """Build all wall geometry from the planned wall segments."""
    for w in walls:
        wx, wy, ww, wd = w["x"], w["y"], w["w"], w["d"]
        if ww <= 0 or wd <= 0:
            continue

        wall_solid = extrude(m, rect_profile(m, ww, wd), ceil_h, wx, wy, 0)
        rep_type = "SweptSolid"

        # Door opening
        if w["has_door"]:
            doff = 0.4
            if w["side"] in ("S", "N"):
                dx = wx + doff
                void = extrude(m, rect_profile(m, DOOR_W, wd + 0.02), DOOR_H, dx, wy - 0.01, 0)
            else:
                dy = wy + doff
                void = extrude(m, rect_profile(m, ww + 0.02, DOOR_W), DOOR_H, wx - 0.01, dy, 0)
            wall_solid = m.createIfcBooleanClippingResult("DIFFERENCE", wall_solid, void)
            rep_type = "Clipping"

            # Door panel
            if w["side"] in ("S", "N"):
                dpx, dpy = dx + 0.025, wy + wd/2 - DOOR_T/2
                dpw, dpd = DOOR_W - 0.05, DOOR_T
            else:
                dpx, dpy = wx + ww/2 - DOOR_T/2, wy + doff + 0.025
                dpw, dpd = DOOR_T, DOOR_W - 0.05
            ds, dr = make_shape(m, body_ctx, [extrude(m, rect_profile(m, dpw, dpd), DOOR_H - 0.05, dpx, dpy, 0)])
            door = make_el(m, "IfcDoor", oh, f"Door", place(m, 0, 0, 0, rel=storey_pl), ds)
            add_color(m, dr, "door")
            elements.append(door)

        # Window opening
        if w["has_window"]:
            if w["side"] in ("S", "N"):
                winx = wx + ww/2 - WIN_W/2
                wvoid = extrude(m, rect_profile(m, WIN_W, wd + 0.02), WIN_H, winx, wy - 0.01, WIN_SILL)
            else:
                winy = wy + wd/2 - WIN_W/2
                wvoid = extrude(m, rect_profile(m, ww + 0.02, WIN_W), WIN_H, wx - 0.01, winy, WIN_SILL)
            wall_solid = m.createIfcBooleanClippingResult("DIFFERENCE", wall_solid, wvoid)
            rep_type = "Clipping"

            # Glass pane
            if w["side"] in ("S", "N"):
                gpx, gpy = wx + ww/2 - WIN_W/2 + 0.02, wy + wd/2 - WIN_T/2
                gpw, gpd = WIN_W - 0.04, WIN_T
            else:
                gpx, gpy = wx + ww/2 - WIN_T/2, wy + wd/2 - WIN_W/2 + 0.02
                gpw, gpd = WIN_T, WIN_W - 0.04
            ws, wr = make_shape(m, body_ctx, [extrude(m, rect_profile(m, gpw, gpd), WIN_H - 0.04, gpx, gpy, WIN_SILL + 0.02)])
            win = make_el(m, "IfcWindow", oh, "Window", place(m, 0, 0, 0, rel=storey_pl), ws)
            add_color(m, wr, "window")
            elements.append(win)

        # Build wall
        color = "ext_wall" if w["is_exterior"] else "int_wall"
        shape_w, rep_w = make_shape(m, body_ctx, [wall_solid], rep_type)
        wall = make_el(m, "IfcWall", oh, f"Wall ({','.join(w['rooms'][:2])})",
                       place(m, 0, 0, 0, rel=storey_pl), shape_w)
        add_color(m, rep_w, color)
        elements.append(wall)


# ── Build room contents (floor, ceiling, fixtures, MEP) ──────────────────────

def build_room_contents(m, oh, body_ctx, room, storey_pl, ceil_h, elements):
    rx = float(room.get("x", 0))
    ry = float(room.get("y", 0))
    rw = float(room.get("width", 4))
    rd = float(room.get("depth", 3))
    rh = float(room.get("height", ceil_h))
    rname = room.get("name", "Room")
    rtype = room_type(rname)
    ext = room.get("exterior", True)
    wt = EXT_T if ext else INT_T

    print(f"    Room: {rname:20} x={rx:6.2f} y={ry:6.2f} w={rw:5.2f} d={rd:5.2f}")

    # Floor
    s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, rw, rd), FLOOR_T, rx, ry, -FLOOR_T)])
    elements.append(make_el(m, "IfcSlab", oh, f"{rname} Floor", place(m,0,0,0,rel=storey_pl), s))
    add_color(m, r, "floor")

    # Ceiling
    inner_x, inner_y = rx + wt, ry + wt
    inner_w, inner_d = rw - 2*wt, rd - 2*wt
    if inner_w > 0 and inner_d > 0:
        s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, inner_w, inner_d), CEIL_T, inner_x, inner_y, rh - CEIL_T)])
        elements.append(make_el(m, "IfcCovering", oh, f"{rname} Ceiling", place(m,0,0,0,rel=storey_pl), s))
        add_color(m, r, "ceiling")

    # Light
    s, r = make_shape(m, body_ctx, [extrude(m, circle_profile(m, 0.15), 0.03, rx+rw/2, ry+rd/2, rh-0.04)])
    elements.append(make_el(m, "IfcLightFixture", oh, f"{rname} Light", place(m,0,0,0,rel=storey_pl), s))
    add_color(m, r, "light")

    # Duct
    if rtype not in ("patio", "garage", "hallway") and inner_w > 1.0:
        duct_len = inner_w - 0.2
        s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, duct_len, DUCT_W), DUCT_H,
            inner_x + 0.1, ry + rd/2 - DUCT_W/2, rh - DUCT_H - 0.08)])
        elements.append(make_el(m, "IfcDuctSegment", oh, f"{rname} Duct", place(m,0,0,0,rel=storey_pl), s))
        add_color(m, r, "duct")

    # Pipe (wet rooms — simple horizontal box)
    if rtype in ("bathroom", "kitchen", "utility") and inner_d > 0.5:
        pipe_len = inner_d - 0.2
        s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, PIPE_W, pipe_len), PIPE_H,
            inner_x + 0.1, inner_y + 0.1, 0.08)])
        elements.append(make_el(m, "IfcPipeSegment", oh, f"{rname} Pipe", place(m,0,0,0,rel=storey_pl), s))
        add_color(m, r, "pipe")

    # Fixtures
    fixture_list = FIXTURES.get(rtype, [])
    iw = max(inner_w, 0.5)
    id_ = max(inner_d, 0.5)

    for fx_data in fixture_list:
        fname, fx, fy, fw, fd, fh, fcolor = fx_data
        ax = inner_x + fx * iw - fw/2
        ay = inner_y + fy * id_ - fd/2
        ax = max(inner_x + 0.05, min(ax, inner_x + iw - fw - 0.05))
        ay = max(inner_y + 0.05, min(ay, inner_y + id_ - fd - 0.05))

        s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, max(fw,0.1), max(fd,0.1)), max(fh,0.05), ax, ay, 0)])
        ifc_type = "IfcFurnishingElement"
        if "toilet" in fname.lower() or "sink" in fname.lower() or "shower" in fname.lower():
            ifc_type = "IfcSanitaryTerminal"
        elif any(x in fname.lower() for x in ["stove","fridge","heater","washer","rack"]):
            ifc_type = "IfcElectricAppliance"
        elements.append(make_el(m, ifc_type, oh, f"{rname} {fname}", place(m,0,0,0,rel=storey_pl), s))
        add_color(m, r, fcolor)


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_ifc(spec, output_path=None):
    global _style_cache
    _style_cache = {}

    try:
        from layout import process_spec
        spec = process_spec(spec)
    except Exception as e:
        print(f"  Layout: {e}")

    name = spec.get("name", "Building")
    floors = spec.get("floors", [])
    metadata = spec.get("metadata", {})
    has_rooms = any(f.get("rooms") for f in floors)

    print(f"Generating IFC: {name}")

    m = ifcopenshell.file(schema="IFC4")
    app = m.createIfcApplication(m.createIfcOrganization(None,"BIM Studio",None,None,None),"1.0","BIM Studio","BIMSTUDIO")
    person = m.createIfcPerson(None,"AI",None,None,None,None,None,None)
    org = m.createIfcOrganization(None,"BIM Studio",None,None,None)
    pao = m.createIfcPersonAndOrganization(person,org,None)
    oh = m.createIfcOwnerHistory(pao,app,None,"ADDED",None,pao,app,int(datetime.now().timestamp()))
    units = m.createIfcUnitAssignment([
        m.createIfcSIUnit(None,"LENGTHUNIT",None,"METRE"),
        m.createIfcSIUnit(None,"AREAUNIT",None,"SQUARE_METRE"),
        m.createIfcSIUnit(None,"VOLUMEUNIT",None,"CUBIC_METRE"),
        m.createIfcSIUnit(None,"PLANEANGLEUNIT",None,"RADIAN")])
    geom_ctx, body_ctx = make_context(m)
    wp = local_pl(m)
    proj = m.createIfcProject(ifcopenshell.guid.new(),oh,name,None,None,None,None,[geom_ctx],units)
    site = m.createIfcSite(ifcopenshell.guid.new(),oh,metadata.get("location","Site"),None,None,wp,None,None,"ELEMENT",None,None,None,None,None)
    bldg = m.createIfcBuilding(ifcopenshell.guid.new(),oh,name,None,None,wp,None,None,"ELEMENT",None,None,None)
    m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,proj,[site])
    m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,site,[bldg])

    ifc_storeys = []
    for floor in floors:
        elev = float(floor.get("elevation",0.0))
        sp = local_pl(m,wp,0,0,elev)
        st = m.createIfcBuildingStorey(ifcopenshell.guid.new(),oh,floor.get("name",f"Level {len(ifc_storeys)+1}"),None,None,sp,None,None,"ELEMENT",elev)
        ifc_storeys.append(st)
    if ifc_storeys:
        m.createIfcRelAggregates(ifcopenshell.guid.new(),oh,None,None,bldg,ifc_storeys)

    for fi, floor in enumerate(floors):
        storey = ifc_storeys[fi]
        elements = []
        ceil_h_raw = floor.get("height", 2.7)
        ceil_h = float(ceil_h_raw)/1000 if float(ceil_h_raw)>10 else float(ceil_h_raw)

        if has_rooms:
            rooms = floor.get("rooms", [])

            # Plan and build walls (shared wall logic)
            wall_plan = plan_walls(rooms)
            print(f"  {len(wall_plan)} wall segments planned for {len(rooms)} rooms")
            build_walls(m, oh, body_ctx, storey.ObjectPlacement, wall_plan, ceil_h, elements)

            # Build room contents (floor, ceiling, furniture, MEP)
            for room in rooms:
                room["height"] = ceil_h
                build_room_contents(m, oh, body_ctx, room, storey.ObjectPlacement, ceil_h, elements)

            # Roof
            if rooms:
                min_x = min(float(r.get("x",0)) for r in rooms)
                min_y = min(float(r.get("y",0)) for r in rooms)
                max_x = max(float(r.get("x",0))+float(r.get("width",4)) for r in rooms)
                max_y = max(float(r.get("y",0))+float(r.get("depth",3)) for r in rooms)
                s, r = make_shape(m, body_ctx, [extrude(m, rect_profile(m, max_x-min_x, max_y-min_y), FLOOR_T, min_x, min_y, ceil_h)])
                elements.append(make_el(m, "IfcRoof", oh, "Roof", place(m,0,0,0,rel=storey.ObjectPlacement), s))
                add_color(m, r, "roof")

            print(f"  Floor '{floor.get('name')}': {len(elements)} elements from {len(rooms)} rooms")

        if elements:
            m.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(),oh,None,None,elements,storey)

    if metadata:
        props = []
        for k,v in metadata.items():
            if v is None: continue
            try: props.append(m.createIfcPropertySingleValue(str(k),None,m.create_entity("IfcLabel",wrappedValue=str(v)),None))
            except: pass
        if props:
            pset = m.createIfcPropertySet(ifcopenshell.guid.new(),oh,"BIM_Studio_Project_Info",None,props)
            m.createIfcRelDefinesByProperties(ifcopenshell.guid.new(),oh,None,None,[bldg],pset)

    if not output_path:
        output_path = f"generated_{name.replace(' ','_')}_{datetime.now().strftime('%H%M%S')}.ifc"
    m.write(output_path)
    print(f"  Written: {output_path}")
    return output_path


if __name__ == "__main__":
    spec = {"name":"Test Apt","floors":[{"name":"Ground","elevation":0,"height":2.7,"rooms":[
        {"name":"Living Room","x":0,"y":0,"width":5,"depth":4,"exterior":True,"door_wall":"north"},
        {"name":"Kitchen","x":5,"y":0,"width":3,"depth":4,"exterior":True,"door_wall":"north"},
        {"name":"Hallway","x":0,"y":4,"width":8,"depth":1.5,"exterior":False,"door_wall":"east"},
        {"name":"Bedroom","x":0,"y":5.5,"width":4.5,"depth":4,"exterior":True,"door_wall":"south"},
        {"name":"Bathroom","x":4.5,"y":5.5,"width":3.5,"depth":4,"exterior":True,"door_wall":"south"},
    ]}]}
    generate_ifc(spec, "/tmp/test_v9.ifc")
