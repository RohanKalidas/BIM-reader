"""
Lightweight exterior massing hints from metadata.architectural_style.
Not a full architectural model — adds porch slabs, columns, pediment, gable hints.
"""

from typing import Any, Callable, Dict, Tuple

import ifcopenshell.api
import numpy as np


def _norm_style(metadata: Dict[str, Any]) -> str:
    s = (
        metadata.get("architectural_style")
        or metadata.get("exterior_style")
        or metadata.get("style")
        or ""
    )
    return str(s).lower()


def build_exterior_accents(
    m,
    body,
    storey,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    elev: float,
    ceil_h: float,
    metadata: Dict[str, Any],
    color_rep: Callable,
    place_element: Callable,
    box_rep: Callable,
) -> None:
    """Mutates IFC model with decorative exterior elements."""
    st = _norm_style(metadata)
    if not st or st in ("default", "simple", "box", "rectangular"):
        return

    w = max_x - min_x
    d = max_y - min_y
    if w < 2 or d < 2:
        return

    front = (metadata.get("front_elevation") or "south").lower().strip()
    if front not in ("south", "north", "east", "west"):
        front = "south"

    if "neo" in st or "classical" in st or "colonial" in st:
        _neoclassical_portico(
            m,
            body,
            storey,
            min_x,
            min_y,
            max_x,
            max_y,
            elev,
            ceil_h,
            front,
            color_rep,
            place_element,
            box_rep,
        )
    if "ranch" in st or "craftsman" in st or "bungalow" in st or "mediterranean" in st:
        _ranch_porch_and_gable(
            m,
            body,
            storey,
            min_x,
            min_y,
            max_x,
            max_y,
            elev,
            ceil_h,
            front,
            color_rep,
            place_element,
            box_rep,
        )
    if "modern" in st or "contemporary" in st:
        _modern_canopy(
            m,
            body,
            storey,
            min_x,
            min_y,
            max_x,
            max_y,
            elev,
            ceil_h,
            front,
            color_rep,
            place_element,
            box_rep,
        )


def _front_frame(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    front: str,
) -> Tuple[float, float, float, float, str]:
    """Returns (fx0, fy0, fx1, fy1, axis) for front segment."""
    if front == "south":
        return min_x, min_y, max_x, min_y, "x"
    if front == "north":
        return min_x, max_y, max_x, max_y, "x"
    if front == "west":
        return min_x, min_y, min_x, max_y, "y"
    return max_x, min_y, max_x, max_y, "y"


def _neoclassical_portico(
    m,
    body,
    storey,
    min_x,
    min_y,
    max_x,
    max_y,
    elev,
    ceil_h,
    front,
    color_rep,
    place_element,
    box_rep,
):
    fx0, fy0, fx1, fy1, axis = _front_frame(min_x, min_y, max_x, max_y, front)
    cx = (fx0 + fx1) / 2
    cy = (fy0 + fy1) / 2
    span = abs(fx1 - fx0) if axis == "x" else abs(fy1 - fy0)
    col_w = 0.45
    col_d = 0.45
    col_h = max(ceil_h * 0.72, 2.4)
    xs = np.linspace(
        cx - span * 0.32, cx + span * 0.32, num=4, dtype=float
    ).tolist()

    for i, xv in enumerate(xs):
        col = ifcopenshell.api.run(
            "root.create_entity", m, ifc_class="IfcColumn", name=f"Portico Column {i+1}"
        )
        sh, rep = box_rep(m, body, col_w, col_d, col_h)
        ifcopenshell.api.run(
            "geometry.assign_representation", m, product=col, representation=sh
        )
        if axis == "x":
            place_element(m, col, float(xv) - col_w / 2, fy0 - col_d / 2, elev)
        else:
            place_element(m, col, fx0 - col_w / 2, float(xv) - col_d / 2, elev)
        color_rep(m, rep, "ext_wall")
        ifcopenshell.api.run(
            "spatial.assign_container", m, products=[col], relating_structure=storey
        )

    # Triangular pediment (thin slab)
    ped_w = span * 0.55
    ped_h = 0.35
    ped = ifcopenshell.api.run(
        "root.create_entity", m, ifc_class="IfcPlate", name="Pediment"
    )
    tri = [(0, 0), (ped_w, 0), (ped_w / 2, ped_h), (0, 0)]
    try:
        prep = ifcopenshell.api.run(
            "geometry.add_slab_representation",
            m,
            context=body,
            depth=0.12,
            polyline=tri,
        )
        if prep:
            ifcopenshell.api.run(
                "geometry.assign_representation", m, product=ped, representation=prep
            )
            if axis == "x":
                place_element(m, ped, cx - ped_w / 2, fy0 - 0.25, elev + col_h)
            else:
                place_element(m, ped, fx0 - 0.25, cy - ped_w / 2, elev + col_h)
            color_rep(m, prep, "ext_wall")
            ifcopenshell.api.run(
                "spatial.assign_container", m, products=[ped], relating_structure=storey
            )
    except Exception:
        pass


def _ranch_porch_and_gable(
    m,
    body,
    storey,
    min_x,
    min_y,
    max_x,
    max_y,
    elev,
    ceil_h,
    front,
    color_rep,
    place_element,
    box_rep,
):
    depth_porch = min(2.0, max(1.2, (max_y - min_y) * 0.22))
    w = max_x - min_x
    if front == "south":
        porch = ifcopenshell.api.run(
            "root.create_entity", m, ifc_class="IfcSlab", name="Front Porch"
        )
        outline = [
            (0, 0),
            (w + 0.4, 0),
            (w + 0.4, depth_porch),
            (0, depth_porch),
            (0, 0),
        ]
        prep = ifcopenshell.api.run(
            "geometry.add_slab_representation",
            m,
            context=body,
            depth=0.18,
            polyline=outline,
        )
        if prep:
            ifcopenshell.api.run(
                "geometry.assign_representation", m, product=porch, representation=prep
            )
            place_element(m, porch, min_x - 0.2, min_y - depth_porch, elev - 0.05)
            color_rep(m, prep, "floor")
            ifcopenshell.api.run(
                "spatial.assign_container", m, products=[porch], relating_structure=storey
            )
    elif front == "north":
        porch = ifcopenshell.api.run(
            "root.create_entity", m, ifc_class="IfcSlab", name="Front Porch"
        )
        outline = [(0, 0), (w + 0.4, 0), (w + 0.4, depth_porch), (0, depth_porch), (0, 0)]
        prep = ifcopenshell.api.run(
            "geometry.add_slab_representation",
            m,
            context=body,
            depth=0.18,
            polyline=outline,
        )
        if prep:
            ifcopenshell.api.run(
                "geometry.assign_representation", m, product=porch, representation=prep
            )
            place_element(m, porch, min_x - 0.2, max_y, elev - 0.05)
            color_rep(m, prep, "floor")
            ifcopenshell.api.run(
                "spatial.assign_container", m, products=[porch], relating_structure=storey
            )

def _modern_canopy(
    m,
    body,
    storey,
    min_x,
    min_y,
    max_x,
    max_y,
    elev,
    ceil_h,
    front,
    color_rep,
    place_element,
    box_rep,
):
    w = max_x - min_x
    can = ifcopenshell.api.run(
        "root.create_entity", m, ifc_class="IfcSlab", name="Entry canopy"
    )
    outline = [(0, 0), (min(w * 0.5, 6), 0), (min(w * 0.5, 6), 1.2), (0, 1.2), (0, 0)]
    prep = ifcopenshell.api.run(
        "geometry.add_slab_representation",
        m,
        context=body,
        depth=0.22,
        polyline=outline,
    )
    if not prep:
        return
    ifcopenshell.api.run(
        "geometry.assign_representation", m, product=can, representation=prep
    )
    cx = (min_x + max_x) / 2
    if front == "south":
        place_element(m, can, cx - min(w * 0.5, 6) / 2, min_y - 1.0, elev + ceil_h - 0.35)
    elif front == "north":
        place_element(m, can, cx - min(w * 0.5, 6) / 2, max_y - 0.2, elev + ceil_h - 0.35)
    else:
        place_element(m, can, min_x + (max_x - min_x) / 2 - 3, min_y, elev + ceil_h - 0.35)
    color_rep(m, prep, "roof")
    ifcopenshell.api.run(
        "spatial.assign_container", m, products=[can], relating_structure=storey
    )
