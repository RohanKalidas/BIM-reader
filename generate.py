"""
generate.py — BIM Studio v10
Built entirely on ifcopenshell.api for correct geometry.

Uses:
- ifcopenshell.api.root.create_entity for all elements
- ifcopenshell.api.geometry.create_2pt_wall for walls (handles placement + geometry)
- ifcopenshell.api.geometry.add_door_representation for realistic doors
- ifcopenshell.api.geometry.add_window_representation for realistic windows
- ifcopenshell.api.geometry.add_slab_representation with polyline for floors/roofs
- ifcopenshell.api.style for surface colors
- ifcopenshell.api.spatial for container assignment
- ifcopenshell.api.aggregate for spatial hierarchy

Wall strategy: perimeter walls are drawn as continuous segments along the
building edge. Interior walls are drawn between room boundaries.
No manual coordinate math for wall geometry — the API handles it.
"""

import math
import os
import sys
import logging
import numpy as np
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
    "roof":         (0.55, 0.50, 0.45, 0.0),
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

_style_cache = {}

DEFAULT_GROUNDED_OPENINGS = {
    "door_width": 0.9,
    "door_height": 2.1,
    "window_width": 1.4,
    "window_height": 1.2,
}

def get_style(m, color_key):
    """Get or create a named surface style."""
    if color_key not in COLORS:
        return None
    ck = (id(m), color_key)
    if ck not in _style_cache:
        r, g, b, t = COLORS[color_key]
        style = ifcopenshell.api.run("style.add_style", m, name=color_key)
        ifcopenshell.api.run("style.add_surface_style", m, style=style,
            ifc_class="IfcSurfaceStyleRendering",
            attributes={"SurfaceColour": {"Name": None, "Red": r, "Green": g, "Blue": b},
                        "Transparency": t})
        _style_cache[ck] = style
    return _style_cache[ck]

def color_rep(m, rep, color_key):
    """Apply color to a representation."""
    style = get_style(m, color_key)
    if style and rep:
        try:
            ifcopenshell.api.run("style.assign_representation_styles", m,
                shape_representation=rep, styles=[style])        except Exception:
            pass


def get_grounding(spec):
    """Return generation defaults learned from the component library when available."""
    metadata = spec.get("metadata", {}) if isinstance(spec, dict) else {}
    grounding = metadata.get("grounding", {}) if isinstance(metadata, dict) else {}
    openings = dict(DEFAULT_GROUNDED_OPENINGS)
    openings.update(grounding.get("openings", {}) if isinstance(grounding, dict) else {})
    fixture_defaults = grounding.get("fixtures", {}) if isinstance(grounding, dict) else {}
    wall_defaults = grounding.get("wall_defaults", {}) if isinstance(grounding, dict) else {}
    return {
        "openings": openings,
        "fixtures": fixture_defaults if isinstance(fixture_defaults, dict) else {},
        "wall_defaults": wall_defaults if isinstance(wall_defaults, dict) else {},
    }

# ── Simple geometry helpers (for furniture/MEP that API doesn't cover) ───────

def box_rep(m, body, w, d, h):
    """Create a simple extruded rectangle representation."""
    pts = [[0.0,0.0],[float(w),0.0],[float(w),float(d)],[0.0,float(d)],[0.0,0.0]]
    if m.schema == "IFC2X3":
        curve = m.createIfcPolyline([m.createIfcCartesianPoint(p) for p in pts])
    else:
        curve = m.createIfcIndexedPolyCurve(m.createIfcCartesianPointList2D(pts), None, False)
    profile = m.createIfcArbitraryClosedProfileDef("AREA", None, curve)
    solid = m.createIfcExtrudedAreaSolid(profile,
        m.createIfcAxis2Placement3D(m.createIfcCartesianPoint((0.,0.,0.)),None,None),
        m.createIfcDirection((0.,0.,1.)), float(h))
    rep = m.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    return m.createIfcProductDefinitionShape(None, None, [rep]), rep

def place_element(m, el, x, y, z):
    """Set element placement via matrix."""
    ifcopenshell.api.run("geometry.edit_object_placement", m, product=el,
        matrix=np.array([[1,0,0,x],[0,1,0,y],[0,0,1,z],[0,0,0,1]], dtype=float))

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

# (name, fx, fy, fw, fd, fh, color, ifc_class)
FIXTURES = {
    "bathroom":   [("Toilet",0.15,0.20,0.38,0.65,0.45,"sanitaryware","IfcSanitaryTerminal"),
                   ("Sink",0.70,0.10,0.50,0.40,0.85,"sanitaryware","IfcSanitaryTerminal"),
                   ("Shower Tray",0.55,0.65,0.90,0.90,0.10,"sanitaryware","IfcSanitaryTerminal")],
    "kitchen":    [("Counter",0.50,0.06,2.00,0.60,0.90,"furniture","IfcFurnishingElement"),
                   ("Stove",0.70,0.06,0.60,0.60,0.90,"appliance","IfcElectricAppliance"),
                   ("Fridge",0.90,0.06,0.70,0.70,1.80,"appliance","IfcElectricAppliance")],
    "bedroom":    [("Bed",0.50,0.60,1.60,2.00,0.50,"bed","IfcFurniture"),
                   ("Wardrobe",0.10,0.08,1.20,0.60,2.10,"furniture","IfcFurniture"),
                   ("Nightstand",0.85,0.60,0.45,0.40,0.50,"furniture","IfcFurniture")],
    "living":     [("Sofa",0.50,0.75,2.20,0.90,0.80,"sofa","IfcFurniture"),
                   ("Coffee Table",0.50,0.50,1.00,0.55,0.42,"furniture","IfcFurniture"),
                   ("TV Unit",0.50,0.08,1.50,0.40,0.50,"furniture","IfcFurniture")],
    "dining":     [("Table",0.50,0.50,1.60,0.90,0.75,"furniture","IfcFurniture"),
                   ("Chair",0.30,0.30,0.45,0.45,0.85,"furniture","IfcFurniture"),
                   ("Chair",0.70,0.70,0.45,0.45,0.85,"furniture","IfcFurniture")],
    "office":     [("Desk",0.50,0.25,1.40,0.70,0.75,"furniture","IfcFurniture"),
                   ("Chair",0.50,0.50,0.55,0.55,1.05,"sofa","IfcFurniture")],
    "conference": [("Table",0.50,0.50,2.40,1.20,0.75,"furniture","IfcFurniture"),
                   ("Chair",0.20,0.30,0.45,0.45,0.85,"sofa","IfcFurniture"),
                   ("Chair",0.80,0.70,0.45,0.45,0.85,"sofa","IfcFurniture")],
    "reception":  [("Sofa",0.50,0.70,2.20,0.90,0.80,"sofa","IfcFurniture"),
                   ("Desk",0.50,0.15,1.80,0.70,0.75,"furniture","IfcFurniture")],
    "server":     [("Rack",0.25,0.50,0.60,0.80,2.00,"appliance","IfcElectricAppliance"),
                   ("Rack",0.75,0.50,0.60,0.80,2.00,"appliance","IfcElectricAppliance")],
    "utility":    [("Heater",0.25,0.25,0.55,0.55,1.50,"appliance","IfcElectricAppliance"),
                   ("Washer",0.75,0.25,0.60,0.60,0.90,"appliance","IfcElectricAppliance")],
    "hallway": [], "patio": [], "garage": [],
}

# ── Wall planning ────────────────────────────────────────────────────────────

EXT_T = 0.20
INT_T = 0.12

def plan_walls(rooms):
    """
    Plan wall segments. Each wall is defined by two endpoints (p1, p2),
    thickness, and whether it's exterior.
    
    Strategy: find all unique edges between rooms. Shared edges get
    interior thickness, perimeter edges get exterior thickness.
    """
    rects = []
    for r in rooms:
        rects.append((
            round(float(r.get("x",0)), 2),
            round(float(r.get("y",0)), 2),
            round(float(r.get("width",4)), 2),
            round(float(r.get("depth",3)), 2),
            r.get("name","Room"),
            r.get("door_wall","south"),
        ))

    # Building bounds
    min_x = min(r[0] for r in rects)
    max_x = max(r[0]+r[2] for r in rects)
    min_y = min(r[1] for r in rects)
    max_y = max(r[1]+r[3] for r in rects)

    def on_perimeter(x1,y1,x2,y2):
        if abs(y1-y2) < 0.01:  # horizontal
            return abs(y1-min_y)<0.01 or abs(y1-max_y)<0.01
        if abs(x1-x2) < 0.01:  # vertical
            return abs(x1-min_x)<0.01 or abs(x1-max_x)<0.01
        return False

    def ek(x1,y1,x2,y2):
        a,b = (round(x1,2),round(y1,2)), (round(x2,2),round(y2,2))
        return (min(a,b), max(a,b))

    edges = {}  # edge_key -> {count, is_perim, rooms, side, has_door}

    for rx, ry, rw, rd, rname, door_wall in rects:
        room_edges = [
            ("S", rx, ry, rx+rw, ry, door_wall=="south"),
            ("N", rx, ry+rd, rx+rw, ry+rd, door_wall=="north"),
            ("W", rx, ry, rx, ry+rd, door_wall=="west"),
            ("E", rx+rw, ry, rx+rw, ry+rd, door_wall=="east"),
        ]
        for side, x1, y1, x2, y2, has_door in room_edges:
            key = ek(x1,y1,x2,y2)
            if key not in edges:
                edges[key] = {"count":0, "is_perim": on_perimeter(x1,y1,x2,y2),
                              "x1":x1,"y1":y1,"x2":x2,"y2":y2,
                              "has_door": has_door, "rooms":[rname]}
            else:
                edges[key]["count"] += 1
                edges[key]["is_perim"] = False  # shared = interior
                if has_door:
                    edges[key]["has_door"] = True
                edges[key]["rooms"].append(rname)

    walls = []
    for key, e in edges.items():
        is_ext = e["is_perim"]
        walls.append({
            "p1": (e["x1"], e["y1"]),
            "p2": (e["x2"], e["y2"]),
            "thickness": EXT_T if is_ext else INT_T,
            "is_exterior": is_ext,
            "has_door": e["has_door"],
            "rooms": e["rooms"],
        })


    return walls


def grounded_fixture_dims(grounding, fixture_name, fallback_dims):
    """Use library-derived fixture dimensions when present."""
    defaults = grounding.get("fixtures", {})
    dims = defaults.get(fixture_name.lower(), {}) if isinstance(defaults, dict) else {}
    fw, fd, fh = fallback_dims
    return (
        float(dims.get("width_m", fw) or fw),
        float(dims.get("depth_m", fd) or fd),
        float(dims.get("height_m", fh) or fh),
    )


# ── Main generation ──────────────────────────────────────────────────────────

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
    grounding = get_grounding(spec)
    has_rooms = any(f.get("rooms") for f in floors)

    print(f"Generating IFC: {name}")

    # ── Setup via API ────────────────────────────────────────────────────
    m = ifcopenshell.file(schema="IFC4")
    proj = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcProject", name=name)
    ifcopenshell.api.run("unit.assign_unit", m)
    ctx = ifcopenshell.api.run("context.add_context", m, context_type="Model")
    body = ifcopenshell.api.run("context.add_context", m,
        context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=ctx)

    site = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcSite",
        name=metadata.get("location", "Site"))
    bldg = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcBuilding", name=name)
    ifcopenshell.api.run("aggregate.assign_object", m, products=[site], relating_object=proj)
    ifcopenshell.api.run("aggregate.assign_object", m, products=[bldg], relating_object=site)

    # ── Storeys ──────────────────────────────────────────────────────────
    storeys = []
    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        st = ifcopenshell.api.run("root.create_entity", m,
            ifc_class="IfcBuildingStorey", name=floor.get("name", f"Level {len(storeys)+1}"))
        ifcopenshell.api.run("geometry.edit_object_placement", m, product=st,
            matrix=np.array([[1,0,0,0],[0,1,0,0],[0,0,1,elev],[0,0,0,1]], dtype=float))
        storeys.append(st)
    if storeys:
        ifcopenshell.api.run("aggregate.assign_object", m, products=storeys, relating_object=bldg)

    # ── Build each floor ─────────────────────────────────────────────────
    for fi, floor in enumerate(floors):
        storey = storeys[fi]
        ceil_h_raw = floor.get("height", 2.7)
        ceil_h = float(ceil_h_raw)/1000 if float(ceil_h_raw) > 10 else float(ceil_h_raw)
        elev = float(floor.get("elevation", 0.0))

        if not has_rooms:
            continue

        rooms = floor.get("rooms", [])
        if not rooms:
            continue

        # ── Plan walls ───────────────────────────────────────────────
        wall_plan = plan_walls(rooms)
        print(f"  Floor '{floor.get('name')}': {len(wall_plan)} walls, {len(rooms)} rooms")

        # ── Build walls using create_2pt_wall ────────────────────────
        for w in wall_plan:            p1, p2 = w["p1"], w["p2"]
            wall_name = f"Wall ({','.join(w['rooms'][:2])})"
            color = "ext_wall" if w["is_exterior"] else "int_wall"
            wall_thickness = w["thickness"]
            if w["is_exterior"]:
                wall_thickness = float(
                    grounding["wall_defaults"].get("exterior_thickness_m", wall_thickness) or wall_thickness
                )
            else:
                wall_thickness = float(
                    grounding["wall_defaults"].get("interior_thickness_m", wall_thickness) or wall_thickness
                )

            wall = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcWall", name=wall_name)
            rep = ifcopenshell.api.run("geometry.create_2pt_wall", m,
                element=wall, context=body,
                p1=p1, p2=p2,
               
                elevation=elev, height=ceil_h, thickness=wall_thickness)
            if rep:
                ifcopenshell.api.run("geometry.assign_representation", m, product=wall, representation=rep)
                color_rep(m, rep, color)
            ifcopenshell.api.run("spatial.assign_container", m, products=[wall], relating_structure=storey)

            # Door
            if w["has_door"]:
                door = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcDoor", name="Door")
                try:
                    door_width = float(grounding["openings"].get("door_width", 0.9) or 0.9)
                    door_height = float(grounding["openings"].get("door_height", 2.1) or 2.1)
                    drep = ifcopenshell.api.run("geometry.add_door_representation", m,
                        context=body, overall_height=door_height, overall_width=door_width,
                        operation_type="SINGLE_SWING_LEFT")
                    if drep:
                        ifcopenshell.api.run("geometry.assign_representation", m, product=door, representation=drep)
                        # Position door along wall
                        dx = p1[0] + (p2[0]-p1[0]) * 0.3
                        dy = p1[1] + (p2[1]-p1[1]) * 0.3
                        # Calculate wall direction for door rotation
                        vx = p2[0]-p1[0]
                        vy = p2[1]-p1[1]
                        length = math.sqrt(vx*vx + vy*vy)
                        if length > 0:
                            vx, vy = vx/length, vy/length
                        else:
                            vx, vy = 1, 0
                        mat = np.array([
                            [vx, -vy, 0, dx],
                            [vy,  vx, 0, dy],
                            [0,   0,  1, elev],
                            [0,   0,  0, 1]], dtype=float)
                        ifcopenshell.api.run("geometry.edit_object_placement", m, product=door, matrix=mat)
                        color_rep(m, drep, "door")
                        ifcopenshell.api.run("spatial.assign_container", m, products=[door], relating_structure=storey)
                except Exception as e:
                    logger.debug("Door failed: %s", e)

            # Window on exterior walls
            if w["is_exterior"]:
                wlen = math.sqrt((p2[0]-p1[0])**2 + (p2[1]-p1[1])**2)
                if wlen >= 2.5:
                    win = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcWindow", name="Window")
                    try:
                        window_width = float(grounding["openings"].get("window_width", 1.4) or 1.4)
                        window_height = float(grounding["openings"].get("window_height", 1.2) or 1.2)
                        wrep = ifcopenshell.api.run("geometry.add_window_representation", m,
                             context=body, overall_height=window_height, overall_width=window_width)
                        if wrep:
                            ifcopenshell.api.run("geometry.assign_representation", m, product=win, representation=wrep)
                            wx = p1[0] + (p2[0]-p1[0]) * 0.5
                            wy = p1[1] + (p2[1]-p1[1]) * 0.5
                            vx = p2[0]-p1[0]
                            vy = p2[1]-p1[1]
                            length = math.sqrt(vx*vx + vy*vy)
                            if length > 0:
                                vx, vy = vx/length, vy/length
                            else:
                                vx, vy = 1, 0
                            mat = np.array([
                                [vx, -vy, 0, wx],
                                [vy,  vx, 0, wy],
                                [0,   0,  1, elev + 0.9],
                                [0,   0,  0, 1]], dtype=float)
                            ifcopenshell.api.run("geometry.edit_object_placement", m, product=win, matrix=mat)
                            color_rep(m, wrep, "window")
                            ifcopenshell.api.run("spatial.assign_container", m, products=[win], relating_structure=storey)
                    except Exception as e:
                        logger.debug("Window failed: %s", e)

        # ── Floor slabs, ceilings, furniture per room ────────────────
        for room in rooms:
            rx = float(room.get("x", 0))
            ry = float(room.get("y", 0))
            rw = float(room.get("width", 4))
            rd = float(room.get("depth", 3))
            rname = room.get("name", "Room")
            rtype = room_type(rname)

            print(f"    Room: {rname:20} x={rx:6.2f} y={ry:6.2f} w={rw:5.2f} d={rd:5.2f}")

            # Floor slab with polyline outline
            slab = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcSlab", name=f"{rname} Floor")
            slab_outline = [(0,0),(rw,0),(rw,rd),(0,rd),(0,0)]
            srep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=0.2, polyline=slab_outline)
            if srep:
                ifcopenshell.api.run("geometry.assign_representation", m, product=slab, representation=srep)
                place_element(m, slab, rx, ry, elev - 0.2)
                color_rep(m, srep, "floor")
                ifcopenshell.api.run("spatial.assign_container", m, products=[slab], relating_structure=storey)

            # Duct
            if rtype not in ("patio","garage","hallway") and rw > 1.5:
                duct = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcDuctSegment", name=f"{rname} Duct")
                duct_w = rw - 0.6
                dshape, drep = box_rep(m, body, duct_w, 0.4, 0.15)
                ifcopenshell.api.run("geometry.assign_representation", m, product=duct, representation=dshape)
                place_element(m, duct, rx + 0.3, ry + rd/2 - 0.2, elev + ceil_h - 0.2)
                color_rep(m, drep, "duct")
                ifcopenshell.api.run("spatial.assign_container", m, products=[duct], relating_structure=storey)

            # Pipe (wet rooms)
            if rtype in ("bathroom","kitchen","utility") and rd > 1.0:
                pipe = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcPipeSegment", name=f"{rname} Pipe")
                pipe_len = rd - 0.6
                pshape, prep = box_rep(m, body, 0.05, pipe_len, 0.05)
                ifcopenshell.api.run("geometry.assign_representation", m, product=pipe, representation=pshape)
                place_element(m, pipe, rx + 0.3, ry + 0.3, elev + 0.1)
                color_rep(m, prep, "pipe")
                ifcopenshell.api.run("spatial.assign_container", m, products=[pipe], relating_structure=storey)

            # Fixtures
            inner_x = rx + 0.15
            inner_y = ry + 0.15
            inner_w = max(rw - 0.3, 0.5)
            inner_d = max(rd - 0.3, 0.5)

            for fx_data in FIXTURES.get(rtype, []):
                fname, fx, fy, fw, fd, fh, fcolor, fclass = fx_data
                fw, fd, fh = grounded_fixture_dims(grounding, fname, (fw, fd, fh))
                ax = inner_x + fx * inner_w - fw/2
                ay = inner_y + fy * inner_d - fd/2
                ax = max(inner_x, min(ax, inner_x + inner_w - fw))
                ay = max(inner_y, min(ay, inner_y + inner_d - fd))

                el = ifcopenshell.api.run("root.create_entity", m, ifc_class=fclass, name=f"{rname} {fname}")
                fshape, frep = box_rep(m, body, max(fw,0.1), max(fd,0.1), max(fh,0.05))
                ifcopenshell.api.run("geometry.assign_representation", m, product=el, representation=fshape)
                place_element(m, el, ax, ay, elev)
                color_rep(m, frep, fcolor)
                ifcopenshell.api.run("spatial.assign_container", m, products=[el], relating_structure=storey)

        # Roof
        if rooms:
            min_x = min(float(r.get("x",0)) for r in rooms)
            min_y = min(float(r.get("y",0)) for r in rooms)
            max_x = max(float(r.get("x",0))+float(r.get("width",4)) for r in rooms)
            max_y = max(float(r.get("y",0))+float(r.get("depth",3)) for r in rooms)

            roof = ifcopenshell.api.run("root.create_entity", m, ifc_class="IfcRoof", name="Roof")
            rw = max_x - min_x
            rd = max_y - min_y
            roof_outline = [(0,0),(rw,0),(rw,rd),(0,rd),(0,0)]
            rrep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=0.25, polyline=roof_outline)
            if rrep:
                ifcopenshell.api.run("geometry.assign_representation", m, product=roof, representation=rrep)
                place_element(m, roof, min_x, min_y, elev + ceil_h)
                color_rep(m, rrep, "roof")
                ifcopenshell.api.run("spatial.assign_container", m, products=[roof], relating_structure=storey)

        print(f"  Floor complete")

    # ── Metadata ─────────────────────────────────────────────────────────
    if metadata:
        pset = ifcopenshell.api.run("pset.add_pset", m, product=bldg, name="BIM_Studio_Info")
        props = {str(k): str(v) for k, v in metadata.items() if v is not None}
        if props:
            ifcopenshell.api.run("pset.edit_pset", m, pset=pset, properties=props)

    # ── Write ────────────────────────────────────────────────────────────
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
    generate_ifc(spec, "/tmp/test_v10.ifc")
