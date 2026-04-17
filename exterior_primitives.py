"""
exterior_primitives.py — Parametric exterior feature builders for BIM Studio.

Instead of hardcoding architectural styles, this module provides a small
library of geometric primitives that Claude can compose into any style.
The AI emits a list of feature dicts in metadata.exterior_features, and
this module dispatches each one to its builder.

Each builder takes:
    model, body, storey, building_bounds, elev, ceil_h, params, helpers
and returns the number of IFC elements it created (for logging).

building_bounds = (min_x, min_y, max_x, max_y)
helpers = dict with keys: box_rep, place_element, color_rep

Adding a new primitive: write a function _build_<name>(...), register it
in PRIMITIVES at the bottom. That's it.

Coordinate system: +x east, +y north, +z up. Origin is the SW corner of
the building footprint.
"""

from __future__ import annotations

import math
import logging
from typing import Any, Callable, Dict, List, Tuple

import ifcopenshell
import ifcopenshell.api

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create(m, ifc_class: str, name: str):
    return ifcopenshell.api.run("root.create_entity", m, ifc_class=ifc_class, name=name)


def _assign(m, product, rep):
    ifcopenshell.api.run("geometry.assign_representation", m, product=product, representation=rep)


def _contain(m, products, storey):
    ifcopenshell.api.run("spatial.assign_container", m, products=products, relating_structure=storey)


def _side_to_edge(
    side: str, bounds: Tuple[float, float, float, float]
) -> Tuple[float, float, float, float, str]:
    """
    Given 'south'/'north'/'east'/'west', return (x0, y0, x1, y1, axis)
    for that edge of the building. axis is 'x' for south/north edges
    (the edge runs along x) and 'y' for east/west.
    """
    min_x, min_y, max_x, max_y = bounds
    s = (side or "south").lower().strip()
    if s == "south":
        return min_x, min_y, max_x, min_y, "x"
    if s == "north":
        return min_x, max_y, max_x, max_y, "x"
    if s == "east":
        return max_x, min_y, max_x, max_y, "y"
    if s == "west":
        return min_x, min_y, min_x, max_y, "y"
    return min_x, min_y, max_x, min_y, "x"  # fallback south


def _corner_point(corner: str, bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
    min_x, min_y, max_x, max_y = bounds
    return {
        "sw": (min_x, min_y),
        "se": (max_x, min_y),
        "nw": (min_x, max_y),
        "ne": (max_x, max_y),
    }.get((corner or "sw").lower(), (min_x, min_y))


def _outward_normal(side: str) -> Tuple[float, float]:
    """Unit vector pointing OUT of the building from a given side."""
    return {
        "south": (0.0, -1.0),
        "north": (0.0,  1.0),
        "east":  (1.0,  0.0),
        "west":  (-1.0, 0.0),
    }.get((side or "south").lower(), (0.0, -1.0))


def _polygon_outline(n_sides: int, radius: float) -> List[Tuple[float, float]]:
    """Closed polygon outline centered at origin, used for turrets/bays."""
    pts = []
    for i in range(n_sides + 1):
        a = (i / n_sides) * 2 * math.pi
        pts.append((radius * math.cos(a), radius * math.sin(a)))
    return pts


# ── Primitives ───────────────────────────────────────────────────────────────

def _build_turret(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Polygonal tower projecting from a corner.
    Params:
        corner: 'sw'|'se'|'nw'|'ne'   (default 'sw')
        radius: float m                (default 1.3)
        height: float m                (default ceil_h + 0.4)
        sides: int                     (default 8)  — 4=square, 8=octagonal
        cap: 'conical'|'flat'|'none'   (default 'conical')
        color_key: str                 (default 'ext_wall')
        cap_color_key: str             (default 'roof')
        spire: bool                    (default True)
    """
    corner = p.get("corner", "sw")
    radius = float(p.get("radius", 1.3))
    height = float(p.get("height", ceil_h + 0.4))
    sides = int(p.get("sides", 8))
    cap = p.get("cap", "conical")
    color_key = p.get("color_key", "ext_wall")
    cap_color_key = p.get("cap_color_key", "roof")
    want_spire = bool(p.get("spire", True))

    cx, cy = _corner_point(corner, bounds)
    # push the turret outward from the building corner by half its radius
    push = {
        "sw": (-radius * 0.5, -radius * 0.5),
        "se": ( radius * 0.5, -radius * 0.5),
        "nw": (-radius * 0.5,  radius * 0.5),
        "ne": ( radius * 0.5,  radius * 0.5),
    }[corner]
    ocx, ocy = cx + push[0], cy + push[1]

    count = 0
    outline = _polygon_outline(sides, radius)
    try:
        body_el = _create(m, "IfcBuildingElementProxy", f"Turret ({corner})")
        rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=height, polyline=outline)
        if rep:
            _assign(m, body_el, rep)
            h["place_element"](m, body_el, ocx, ocy, elev)
            h["color_rep"](m, rep, color_key)
            _contain(m, [body_el], storey)
            count += 1
    except Exception as e:
        logger.debug("turret body failed: %s", e)

    if cap == "conical":
        try:
            cap_outline = _polygon_outline(sides, radius * 1.08)
            cap_el = _create(m, "IfcRoof", f"Turret Cap ({corner})")
            crep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=max(0.8, radius * 0.9), polyline=cap_outline)
            if crep:
                _assign(m, cap_el, crep)
                h["place_element"](m, cap_el, ocx, ocy, elev + height)
                h["color_rep"](m, crep, cap_color_key)
                _contain(m, [cap_el], storey)
                count += 1
        except Exception:
            pass
    elif cap == "flat":
        try:
            flat_outline = _polygon_outline(sides, radius * 1.05)
            cap_el = _create(m, "IfcRoof", f"Turret Cap ({corner})")
            crep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=0.15, polyline=flat_outline)
            if crep:
                _assign(m, cap_el, crep)
                h["place_element"](m, cap_el, ocx, ocy, elev + height)
                h["color_rep"](m, crep, cap_color_key)
                _contain(m, [cap_el], storey)
                count += 1
        except Exception:
            pass

    if want_spire and cap != "none":
        try:
            spike = _create(m, "IfcBuildingElementProxy", f"Turret Spire ({corner})")
            sh, rep = h["box_rep"](m, body, 0.10, 0.10, 0.8)
            _assign(m, spike, sh)
            h["place_element"](m, spike, ocx - 0.05, ocy - 0.05, elev + height + max(0.8, radius * 0.9))
            h["color_rep"](m, rep, cap_color_key)
            _contain(m, [spike], storey)
            count += 1
        except Exception:
            pass

    return count


def _build_porch(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Covered porch slab along one or more sides.
    Params:
        sides: list[str]                (default ['south']) — multiple = wraparound
        depth: float m                  (default 1.8)
        column_count: int               (default auto from span / 2.0)
        column_style: 'square'|'round'|'turned'|'tapered'  (default 'square')
        column_size: float m            (default 0.15)
        column_color_key: str           (default 'accent')
        slab_color_key: str             (default 'ext_wall')
        has_roof: bool                  (default True) — thin sheltering slab above
    """
    sides = p.get("sides", ["south"])
    if isinstance(sides, str):
        sides = [sides]
    depth = float(p.get("depth", 1.8))
    col_style = p.get("column_style", "square")
    col_size = float(p.get("column_size", 0.15))
    col_color = p.get("column_color_key", "accent")
    slab_color = p.get("slab_color_key", "ext_wall")
    has_roof = bool(p.get("has_roof", True))
    col_h = ceil_h + 0.1

    count = 0
    min_x, min_y, max_x, max_y = bounds

    for side in sides:
        nx, ny = _outward_normal(side)
        x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
        span = math.hypot(x1 - x0, y1 - y0)
        if span < 0.5:
            continue

        # Slab outline in LOCAL coords, then placed at an origin
        if axis == "x":
            outline = [(0, 0), (span, 0), (span, depth), (0, depth), (0, 0)]
            ox = x0
            oy = y0 + ny * depth if ny < 0 else y0
        else:
            outline = [(0, 0), (depth, 0), (depth, span), (0, span), (0, 0)]
            ox = x0 + nx * depth if nx < 0 else x0
            oy = y0

        try:
            porch = _create(m, "IfcSlab", f"Porch Slab ({side})")
            rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=0.15, polyline=outline)
            if rep:
                _assign(m, porch, rep)
                h["place_element"](m, porch, ox, oy, elev - 0.15)
                h["color_rep"](m, rep, slab_color)
                _contain(m, [porch], storey)
                count += 1
        except Exception:
            pass

        # Columns
        explicit_count = p.get("column_count")
        n_cols = int(explicit_count) if explicit_count else max(3, int(span / 2.0) + 1)
        col_r = col_size / 2.0

        for i in range(n_cols):
            if n_cols == 1:
                t = 0.5
            else:
                t = i / (n_cols - 1)
            try:
                col = _create(m, "IfcColumn", f"Porch Column {i+1} ({side})")
                # Round columns use an octagonal cross-section; otherwise box
                if col_style in ("round", "turned"):
                    outline_c = _polygon_outline(8, col_r)
                    crep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                        context=body, depth=col_h, polyline=outline_c)
                    if crep:
                        _assign(m, col, crep)
                        if axis == "x":
                            px = x0 + t * span
                            py = y0 + ny * (depth * 0.1) - (col_r if ny > 0 else -col_r)
                        else:
                            px = x0 + nx * (depth * 0.1) - (col_r if nx > 0 else -col_r)
                            py = y0 + t * span
                        h["place_element"](m, col, px, py, elev)
                        h["color_rep"](m, crep, col_color)
                        _contain(m, [col], storey)
                        count += 1
                else:
                    sh, rep = h["box_rep"](m, body, col_size, col_size, col_h)
                    _assign(m, col, sh)
                    if axis == "x":
                        px = x0 + t * span - col_r
                        py = y0 + ny * (depth - 0.2)
                    else:
                        px = x0 + nx * (depth - 0.2)
                        py = y0 + t * span - col_r
                    h["place_element"](m, col, px, py, elev)
                    h["color_rep"](m, rep, col_color)
                    _contain(m, [col], storey)
                    count += 1
            except Exception:
                pass

        # Porch roof (thin slab above)
        if has_roof:
            try:
                roof_el = _create(m, "IfcRoof", f"Porch Roof ({side})")
                rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                    context=body, depth=0.12, polyline=outline)
                if rep:
                    _assign(m, roof_el, rep)
                    h["place_element"](m, roof_el, ox, oy, elev + col_h)
                    h["color_rep"](m, rep, "roof")
                    _contain(m, [roof_el], storey)
                    count += 1
            except Exception:
                pass

    return count


def _build_gable(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Triangular gable end plate on a facade.
    Params:
        side: 'south'|'north'|'east'|'west'  (default 'south')
        position: float 0-1           (default 0.5) — along the edge
        width: float m                (default 2.6)
        height: float m               (default 1.6)  — steeper = bigger
        color_key: str                (default 'roof')
        elevation_offset: float m     (default 0)  — height above wall top
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 2.6))
    height = float(p.get("height", 1.6))
    color_key = p.get("color_key", "roof")
    dz = float(p.get("elevation_offset", 0.0))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    if span < width:
        width = span * 0.8
    nx, ny = _outward_normal(side)

    try:
        gable = _create(m, "IfcPlate", f"Gable ({side})")
        tri = [(0, 0), (width, 0), (width / 2, height), (0, 0)]
        rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=0.18, polyline=tri)
        if not rep:
            return 0
        _assign(m, gable, rep)
        if axis == "x":
            px = x0 + pos * span - width / 2
            py = y0 + ny * 0.1
        else:
            px = x0 + nx * 0.1
            py = y0 + pos * span - width / 2
        h["place_element"](m, gable, px, py, elev + ceil_h + dz)
        h["color_rep"](m, rep, color_key)
        _contain(m, [gable], storey)
        return 1
    except Exception:
        return 0


def _build_dormer(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Small projecting gabled window on the roof.
    Params:
        side: str                     (default 'south')
        position: float 0-1           (default 0.5)
        width: float m                (default 1.4)
        height: float m               (default 1.2)
        projection: float m           (default 0.8)
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 1.4))
    height = float(p.get("height", 1.2))
    proj = float(p.get("projection", 0.8))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    try:
        dormer = _create(m, "IfcBuildingElementProxy", f"Dormer ({side})")
        sh, rep = h["box_rep"](m, body, width, proj, height)
        _assign(m, dormer, sh)
        if axis == "x":
            px = x0 + pos * span - width / 2
            py = y0 + ny * (proj * 0.3)
        else:
            px = x0 + nx * (proj * 0.3)
            py = y0 + pos * span - width / 2
        h["place_element"](m, dormer, px, py, elev + ceil_h)
        h["color_rep"](m, rep, "ext_wall")
        _contain(m, [dormer], storey)
        # mini gable on the dormer
        gable = _create(m, "IfcPlate", f"Dormer Gable ({side})")
        tri = [(0, 0), (width, 0), (width / 2, height * 0.6), (0, 0)]
        grep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=0.10, polyline=tri)
        if grep:
            _assign(m, gable, grep)
            h["place_element"](m, gable, px, py - 0.05 if ny < 0 else py + proj + 0.05, elev + ceil_h + height)
            h["color_rep"](m, grep, "roof")
            _contain(m, [gable], storey)
            return 2
        return 1
    except Exception:
        return 0


def _build_chimney(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Rectangular chimney stack.
    Params:
        position: [x, y]              (default [building_center])
        width: float m                (default 0.7)
        depth: float m                (default 0.7)
        height: float m               (default ceil_h + 2.0) — extends above roof
        cap: bool                     (default True)
        color_key: str                (default 'ext_wall')
    """
    min_x, min_y, max_x, max_y = bounds
    pos = p.get("position", [(min_x + max_x) / 2, (min_y + max_y) / 2])
    width = float(p.get("width", 0.7))
    depth = float(p.get("depth", 0.7))
    height = float(p.get("height", ceil_h + 2.0))
    want_cap = bool(p.get("cap", True))
    color_key = p.get("color_key", "ext_wall")

    count = 0
    try:
        stack = _create(m, "IfcBuildingElementProxy", "Chimney")
        sh, rep = h["box_rep"](m, body, width, depth, height)
        _assign(m, stack, sh)
        h["place_element"](m, stack, pos[0] - width / 2, pos[1] - depth / 2, elev)
        h["color_rep"](m, rep, color_key)
        _contain(m, [stack], storey)
        count += 1
    except Exception:
        pass
    if want_cap:
        try:
            cap = _create(m, "IfcBuildingElementProxy", "Chimney Cap")
            sh, rep = h["box_rep"](m, body, width + 0.15, depth + 0.15, 0.12)
            _assign(m, cap, sh)
            h["place_element"](m, cap, pos[0] - (width + 0.15) / 2, pos[1] - (depth + 0.15) / 2, elev + height)
            h["color_rep"](m, rep, "roof")
            _contain(m, [cap], storey)
            count += 1
        except Exception:
            pass
    return count


def _build_bay_window(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Projecting box on a facade with windows.
    Params:
        side: str                     (default 'south')
        position: float 0-1           (default 0.5)
        width: float m                (default 2.2) — along facade
        projection: float m           (default 0.9)
        height: float m               (default ceil_h * 0.75)
        sill: float m                 (default 0.7)
        sides: int                    (default 3) — 3 = classic angled bay, 5 = bow
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 2.2))
    proj = float(p.get("projection", 0.9))
    height = float(p.get("height", ceil_h * 0.75))
    sill = float(p.get("sill", 0.7))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    try:
        bay = _create(m, "IfcBuildingElementProxy", f"Bay Window ({side})")
        sh, rep = h["box_rep"](m, body, width, proj, height)
        _assign(m, bay, sh)
        if axis == "x":
            px = x0 + pos * span - width / 2
            py = y0 + (ny * proj if ny < 0 else 0)
        else:
            px = x0 + (nx * proj if nx < 0 else 0)
            py = y0 + pos * span - width / 2
        h["place_element"](m, bay, px, py, elev + sill)
        h["color_rep"](m, rep, "window_glass")
        _contain(m, [bay], storey)
        return 1
    except Exception:
        return 0


def _build_canopy(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Flat slab projecting from a wall above an entry/window.
    Params:
        side: str                     (default 'south')
        position: float 0-1           (default 0.5)
        width: float m                (default 2.5)
        projection: float m           (default 1.2)
        elevation: float m            (default ceil_h * 0.9)
        color_key: str                (default 'roof')
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 2.5))
    proj = float(p.get("projection", 1.2))
    ez = float(p.get("elevation", ceil_h * 0.9))
    color_key = p.get("color_key", "roof")

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    try:
        can = _create(m, "IfcRoof", f"Canopy ({side})")
        if axis == "x":
            outline = [(0, 0), (width, 0), (width, proj), (0, proj), (0, 0)]
            px = x0 + pos * span - width / 2
            py = y0 + (ny * proj if ny < 0 else 0)
        else:
            outline = [(0, 0), (proj, 0), (proj, width), (0, width), (0, 0)]
            px = x0 + (nx * proj if nx < 0 else 0)
            py = y0 + pos * span - width / 2
        rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=0.12, polyline=outline)
        if rep:
            _assign(m, can, rep)
            h["place_element"](m, can, px, py, elev + ez)
            h["color_rep"](m, rep, color_key)
            _contain(m, [can], storey)
            return 1
    except Exception:
        pass
    return 0


def _build_column(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Standalone column (for porticos, pergolas, decorative).
    Params:
        position: [x, y]              REQUIRED
        height: float m               (default ceil_h + 0.2)
        size: float m                 (default 0.18)
        style: 'square'|'round'|'fluted'  (default 'square')
        color_key: str                (default 'ext_wall')
    """
    pos = p.get("position")
    if not pos:
        return 0
    height = float(p.get("height", ceil_h + 0.2))
    size = float(p.get("size", 0.18))
    style = p.get("style", "square")
    color_key = p.get("color_key", "ext_wall")

    try:
        col = _create(m, "IfcColumn", "Column")
        if style in ("round", "fluted"):
            outline = _polygon_outline(12 if style == "round" else 16, size / 2)
            rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=height, polyline=outline)
            if rep:
                _assign(m, col, rep)
                h["place_element"](m, col, float(pos[0]), float(pos[1]), elev)
                h["color_rep"](m, rep, color_key)
                _contain(m, [col], storey)
                return 1
        else:
            sh, rep = h["box_rep"](m, body, size, size, height)
            _assign(m, col, sh)
            h["place_element"](m, col, float(pos[0]) - size / 2, float(pos[1]) - size / 2, elev)
            h["color_rep"](m, rep, color_key)
            _contain(m, [col], storey)
            return 1
    except Exception:
        pass
    return 0


def _build_portico(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Classical entry portico: columns + pediment.
    Params:
        side: str                     (default 'south')
        width: float m                (default 4.0)
        projection: float m           (default 1.8)
        column_count: int             (default 4)
        pediment: bool                (default True)
    """
    side = p.get("side", "south")
    width = float(p.get("width", 4.0))
    proj = float(p.get("projection", 1.8))
    n_cols = int(p.get("column_count", 4))
    pediment = bool(p.get("pediment", True))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    count = 0

    # Base slab
    try:
        base = _create(m, "IfcSlab", f"Portico Base ({side})")
        if axis == "x":
            outline = [(0, 0), (width, 0), (width, proj), (0, proj), (0, 0)]
            px = x0 + span / 2 - width / 2
            py = y0 + (ny * proj if ny < 0 else 0)
        else:
            outline = [(0, 0), (proj, 0), (proj, width), (0, width), (0, 0)]
            px = x0 + (nx * proj if nx < 0 else 0)
            py = y0 + span / 2 - width / 2
        rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=0.18, polyline=outline)
        if rep:
            _assign(m, base, rep)
            h["place_element"](m, base, px, py, elev - 0.18)
            h["color_rep"](m, rep, "ext_wall")
            _contain(m, [base], storey)
            count += 1
    except Exception:
        pass

    # Columns (fluted/round)
    col_h = ceil_h
    col_r = 0.15
    for i in range(n_cols):
        t = i / max(1, n_cols - 1)
        col = _create(m, "IfcColumn", f"Portico Column {i+1}")
        try:
            outline_c = _polygon_outline(16, col_r)
            rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=col_h, polyline=outline_c)
            if rep:
                _assign(m, col, rep)
                if axis == "x":
                    cx = x0 + span / 2 - width / 2 + col_r + t * (width - 2 * col_r)
                    cy = y0 + (ny * (proj - 0.3) if ny < 0 else 0.3)
                else:
                    cx = x0 + (nx * (proj - 0.3) if nx < 0 else 0.3)
                    cy = y0 + span / 2 - width / 2 + col_r + t * (width - 2 * col_r)
                h["place_element"](m, col, cx, cy, elev)
                h["color_rep"](m, rep, "ext_wall")
                _contain(m, [col], storey)
                count += 1
        except Exception:
            pass

    # Pediment (triangle atop the columns)
    if pediment:
        try:
            ped = _create(m, "IfcPlate", f"Pediment ({side})")
            ph = width * 0.22
            tri = [(0, 0), (width, 0), (width / 2, ph), (0, 0)]
            rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
                context=body, depth=0.15, polyline=tri)
            if rep:
                _assign(m, ped, rep)
                if axis == "x":
                    h["place_element"](m, ped, x0 + span / 2 - width / 2,
                                       y0 + (ny * 0.15 if ny < 0 else 0.15), elev + col_h)
                else:
                    h["place_element"](m, ped, x0 + (nx * 0.15 if nx < 0 else 0.15),
                                       y0 + span / 2 - width / 2, elev + col_h)
                h["color_rep"](m, rep, "ext_wall")
                _contain(m, [ped], storey)
                count += 1
        except Exception:
            pass

    return count


def _build_balcony(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Cantilevered balcony slab with railing.
    Params:
        side, position, width, projection, floor (default 1 = second floor at elev+ceil_h)
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 2.8))
    proj = float(p.get("projection", 1.2))
    floor_idx = int(p.get("floor", 1))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)
    z = elev + floor_idx * ceil_h

    count = 0
    try:
        slab = _create(m, "IfcSlab", f"Balcony ({side})")
        if axis == "x":
            outline = [(0, 0), (width, 0), (width, proj), (0, proj), (0, 0)]
            px = x0 + pos * span - width / 2
            py = y0 + (ny * proj if ny < 0 else 0)
        else:
            outline = [(0, 0), (proj, 0), (proj, width), (0, width), (0, 0)]
            px = x0 + (nx * proj if nx < 0 else 0)
            py = y0 + pos * span - width / 2
        rep = ifcopenshell.api.run("geometry.add_slab_representation", m,
            context=body, depth=0.15, polyline=outline)
        if rep:
            _assign(m, slab, rep)
            h["place_element"](m, slab, px, py, z - 0.15)
            h["color_rep"](m, rep, "ext_wall")
            _contain(m, [slab], storey)
            count += 1
    except Exception:
        pass
    # Simple railing — single bar
    try:
        rail = _create(m, "IfcRailing", f"Balcony Railing ({side})")
        if axis == "x":
            sh, rep = h["box_rep"](m, body, width, 0.05, 1.0)
            h["place_element"](m, rail, px, py + (proj - 0.05 if ny >= 0 else 0), z)
        else:
            sh, rep = h["box_rep"](m, body, 0.05, width, 1.0)
            h["place_element"](m, rail, px + (proj - 0.05 if nx >= 0 else 0), py, z)
        _assign(m, rail, sh)
        h["color_rep"](m, rep, "accent")
        _contain(m, [rail], storey)
        count += 1
    except Exception:
        pass
    return count


def _build_parapet(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Low wall along the roof perimeter (flat-roofed buildings).
    Params:
        height: float m  (default 0.9)
        thickness: float m  (default 0.18)
        sides: list[str]  (default all four)
        stepped: bool  (default False) — Art Deco style step-back
        color_key: str  (default 'ext_wall')
    """
    height = float(p.get("height", 0.9))
    thickness = float(p.get("thickness", 0.18))
    sides = p.get("sides", ["south", "north", "east", "west"])
    stepped = bool(p.get("stepped", False))
    color_key = p.get("color_key", "ext_wall")

    count = 0
    for side in sides:
        x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
        span = math.hypot(x1 - x0, y1 - y0)
        nx, ny = _outward_normal(side)
        try:
            para = _create(m, "IfcBuildingElementProxy", f"Parapet ({side})")
            if axis == "x":
                sh, rep = h["box_rep"](m, body, span, thickness, height)
                # push slightly outward so it sits on the wall centreline
                h["place_element"](m, para, x0, y0 - (thickness / 2 if ny < 0 else -thickness / 2), elev + ceil_h)
            else:
                sh, rep = h["box_rep"](m, body, thickness, span, height)
                h["place_element"](m, para, x0 - (thickness / 2 if nx < 0 else -thickness / 2), y0, elev + ceil_h)
            _assign(m, para, sh)
            h["color_rep"](m, rep, color_key)
            _contain(m, [para], storey)
            count += 1
        except Exception:
            pass

        if stepped:
            # One additional step-back on top, shorter
            try:
                step = _create(m, "IfcBuildingElementProxy", f"Parapet Step ({side})")
                step_w = span * 0.4
                if axis == "x":
                    sh, rep = h["box_rep"](m, body, step_w, thickness, height * 0.6)
                    h["place_element"](m, step, x0 + span / 2 - step_w / 2,
                                       y0 - (thickness / 2 if ny < 0 else -thickness / 2),
                                       elev + ceil_h + height)
                else:
                    sh, rep = h["box_rep"](m, body, thickness, step_w, height * 0.6)
                    h["place_element"](m, step, x0 - (thickness / 2 if nx < 0 else -thickness / 2),
                                       y0 + span / 2 - step_w / 2,
                                       elev + ceil_h + height)
                _assign(m, step, sh)
                h["color_rep"](m, rep, color_key)
                _contain(m, [step], storey)
                count += 1
            except Exception:
                pass
    return count


def _build_half_timber_band(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Tudor-style dark vertical timbers across upper portion of a facade.
    Params:
        side: str                     (default 'south')
        count: int                    (default 6)
        band_top_frac: float 0-1      (default 0.95) — how high on the wall the band ends
        band_bottom_frac: float 0-1   (default 0.5)
        color_key: str                (default 'accent')
        add_diagonals: bool           (default False)
    """
    side = p.get("side", "south")
    count = int(p.get("count", 6))
    band_top = float(p.get("band_top_frac", 0.95))
    band_bot = float(p.get("band_bottom_frac", 0.5))
    color_key = p.get("color_key", "accent")
    diag = bool(p.get("add_diagonals", False))

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)
    z_bot = elev + ceil_h * band_bot
    z_top = elev + ceil_h * band_top
    band_h = z_top - z_bot

    n = 0
    # Verticals
    for i in range(count):
        t = i / max(1, count - 1)
        try:
            beam = _create(m, "IfcBuildingElementProxy", f"Half-Timber V{i+1}")
            if axis == "x":
                sh, rep = h["box_rep"](m, body, 0.10, 0.04, band_h)
                h["place_element"](m, beam, x0 + t * span - 0.05,
                                   y0 + (ny * 0.02), z_bot)
            else:
                sh, rep = h["box_rep"](m, body, 0.04, 0.10, band_h)
                h["place_element"](m, beam, x0 + (nx * 0.02),
                                   y0 + t * span - 0.05, z_bot)
            _assign(m, beam, sh)
            h["color_rep"](m, rep, color_key)
            _contain(m, [beam], storey)
            n += 1
        except Exception:
            pass
    # Top horizontal band
    try:
        top = _create(m, "IfcBuildingElementProxy", "Half-Timber Top")
        if axis == "x":
            sh, rep = h["box_rep"](m, body, span, 0.04, 0.10)
            h["place_element"](m, top, x0, y0 + (ny * 0.02), z_top - 0.10)
        else:
            sh, rep = h["box_rep"](m, body, 0.04, span, 0.10)
            h["place_element"](m, top, x0 + (nx * 0.02), y0, z_top - 0.10)
        _assign(m, top, sh)
        h["color_rep"](m, rep, color_key)
        _contain(m, [top], storey)
        n += 1
    except Exception:
        pass
    return n


def _build_shutter(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Decorative shutter plate flanking a window.
    Params:
        side, position, sill, height, width (thin rectangle)
    """
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    sill = float(p.get("sill", 0.9))
    width = float(p.get("width", 0.30))
    height = float(p.get("height", 1.4))
    color_key = p.get("color_key", "accent")

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    count = 0
    for offset in (-0.8, 0.8):  # one on each side
        try:
            sh_el = _create(m, "IfcPlate", f"Shutter ({side})")
            if axis == "x":
                sh, rep = h["box_rep"](m, body, width, 0.03, height)
                h["place_element"](m, sh_el,
                                   x0 + pos * span - width / 2 + offset,
                                   y0 + (ny * 0.02), elev + sill)
            else:
                sh, rep = h["box_rep"](m, body, 0.03, width, height)
                h["place_element"](m, sh_el,
                                   x0 + (nx * 0.02),
                                   y0 + pos * span - width / 2 + offset,
                                   elev + sill)
            _assign(m, sh_el, sh)
            h["color_rep"](m, rep, color_key)
            _contain(m, [sh_el], storey)
            count += 1
        except Exception:
            pass
    return count


def _build_pergola(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Open-beam structure (typically over a patio).
    Params:
        position: [x, y]              REQUIRED (center of pergola)
        width: float m                (default 3.5)
        depth: float m                (default 3.0)
        height: float m               (default ceil_h - 0.3)
        beam_count: int               (default 5)
        color_key: str                (default 'accent')
    """
    pos = p.get("position")
    if not pos:
        return 0
    width = float(p.get("width", 3.5))
    depth = float(p.get("depth", 3.0))
    height = float(p.get("height", ceil_h - 0.3))
    n_beams = int(p.get("beam_count", 5))
    color_key = p.get("color_key", "accent")

    cx, cy = float(pos[0]), float(pos[1])
    count = 0
    # 4 posts at corners
    for dx, dy in [(-width / 2, -depth / 2), (width / 2, -depth / 2),
                   (-width / 2, depth / 2), (width / 2, depth / 2)]:
        try:
            post = _create(m, "IfcColumn", "Pergola Post")
            sh, rep = h["box_rep"](m, body, 0.12, 0.12, height)
            _assign(m, post, sh)
            h["place_element"](m, post, cx + dx - 0.06, cy + dy - 0.06, elev)
            h["color_rep"](m, rep, color_key)
            _contain(m, [post], storey)
            count += 1
        except Exception:
            pass
    # Top beams running along x
    for i in range(n_beams):
        t = i / max(1, n_beams - 1)
        try:
            beam = _create(m, "IfcBeam", f"Pergola Beam {i+1}")
            sh, rep = h["box_rep"](m, body, width, 0.08, 0.12)
            _assign(m, beam, sh)
            h["place_element"](m, beam,
                               cx - width / 2,
                               cy - depth / 2 + t * depth - 0.04,
                               elev + height)
            h["color_rep"](m, rep, color_key)
            _contain(m, [beam], storey)
            count += 1
        except Exception:
            pass
    return count


def _build_vertical_fin(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Thin vertical wall fin — used in modernist/brutalist facades.
    Params:
        side, count, depth, color_key
    """
    side = p.get("side", "south")
    count = int(p.get("count", 6))
    depth = float(p.get("depth", 0.4))
    color_key = p.get("color_key", "accent")

    x0, y0, x1, y1, axis = _side_to_edge(side, bounds)
    span = math.hypot(x1 - x0, y1 - y0)
    nx, ny = _outward_normal(side)

    n = 0
    for i in range(count):
        t = i / max(1, count - 1)
        try:
            fin = _create(m, "IfcBuildingElementProxy", f"Fin {i+1}")
            if axis == "x":
                sh, rep = h["box_rep"](m, body, 0.08, depth, ceil_h - 0.2)
                h["place_element"](m, fin,
                                   x0 + t * span - 0.04,
                                   y0 + (ny * depth if ny < 0 else 0),
                                   elev)
            else:
                sh, rep = h["box_rep"](m, body, depth, 0.08, ceil_h - 0.2)
                h["place_element"](m, fin,
                                   x0 + (nx * depth if nx < 0 else 0),
                                   y0 + t * span - 0.04,
                                   elev)
            _assign(m, fin, sh)
            h["color_rep"](m, rep, color_key)
            _contain(m, [fin], storey)
            n += 1
        except Exception:
            pass
    return n


def _build_awning(m, body, storey, bounds, elev, ceil_h, p, h) -> int:
    """
    Angled sun shade over a window or entry.
    Params: side, position, width, projection, sill, tilt_deg
    """
    # Implementation: same as canopy but slightly tilted visual (we can't
    # actually tilt easily in IFC without ruled surfaces, so render as a
    # flat canopy with a slightly thinner leading edge). Good enough.
    side = p.get("side", "south")
    pos = float(p.get("position", 0.5))
    width = float(p.get("width", 1.6))
    proj = float(p.get("projection", 0.8))
    sill = float(p.get("sill", 1.9))
    color_key = p.get("color_key", "accent")

    return _build_canopy(m, body, storey, bounds, elev, ceil_h,
                         {"side": side, "position": pos, "width": width,
                          "projection": proj, "elevation": sill,
                          "color_key": color_key}, h)


# ── Registry & public entry ──────────────────────────────────────────────────

PRIMITIVES: Dict[str, Callable] = {
    "turret":             _build_turret,
    "porch":              _build_porch,
    "wraparound_porch":   _build_porch,   # alias — pass sides=[...]
    "gable":              _build_gable,
    "dormer":             _build_dormer,
    "chimney":            _build_chimney,
    "bay_window":         _build_bay_window,
    "canopy":             _build_canopy,
    "column":             _build_column,
    "portico":            _build_portico,
    "balcony":            _build_balcony,
    "parapet":            _build_parapet,
    "half_timber_band":   _build_half_timber_band,
    "shutter":            _build_shutter,
    "pergola":            _build_pergola,
    "vertical_fin":       _build_vertical_fin,
    "awning":             _build_awning,
}


def build_exterior_features(
    m, body, storey,
    bounds: Tuple[float, float, float, float],
    elev: float, ceil_h: float,
    features: List[Dict[str, Any]],
    *,
    box_rep: Callable,
    place_element: Callable,
    color_rep: Callable,
) -> int:
    """
    Render a list of exterior features. Each feature is a dict with 'type'
    and parameters specific to that type. Unknown types are logged and
    skipped; one bad feature doesn't kill the rest.
    """
    if not features:
        return 0

    helpers = {
        "box_rep": box_rep,
        "place_element": place_element,
        "color_rep": color_rep,
    }

    total = 0
    for i, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        ftype = str(feat.get("type", "")).lower().strip()
        if not ftype:
            continue
        fn = PRIMITIVES.get(ftype)
        if not fn:
            logger.info("Unknown exterior feature type %r (feature %d); skipping", ftype, i)
            continue
        try:
            n = fn(m, body, storey, bounds, elev, ceil_h, feat, helpers)
            total += n
            logger.info("Built feature %s (%d IFC elements)", ftype, n)
        except Exception as e:
            logger.warning("Feature %s (%d) raised: %s", ftype, i, e)
    return total


def list_primitive_types() -> List[str]:
    """For the system prompt and for debug UIs."""
    return sorted(set(PRIMITIVES.keys()))
