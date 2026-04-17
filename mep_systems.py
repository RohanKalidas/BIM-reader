"""
mep_systems.py — Real MEP generation for BIM Studio.

Adds a central HVAC system, electrical service, plumbing service, and fire
protection to a floor of rooms. Called from generate.py after rooms/walls are
built but before the roof is placed.

The generator is intentionally approximate — this is visual/schematic MEP,
not a engineered design. But every element uses the correct IFC class so the
downstream PostgreSQL extractor sees real HVAC/electrical/plumbing rows.

Public entry point:
    build_mep(model, body, storey, rooms, elev, ceil_h, *, box_rep,
              place_element, color_rep, metadata=None)

`box_rep`, `place_element`, and `color_rep` are the helpers from generate.py
(passed in to avoid circular imports).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import ifcopenshell.api
import numpy as np

logger = logging.getLogger(__name__)


# ── Room typing (duplicate of generate.room_type to avoid import cycle) ──────
def _room_type(name: str) -> str:
    n = (name or "").lower()
    buckets = [
        (["bath", "wc", "toilet", "shower", "lavatory"], "bathroom"),
        (["kitchen", "cook"], "kitchen"),
        (["bed", "master", "guest", "sleep"], "bedroom"),
        (["living", "lounge", "family", "great"], "living"),
        (["dining", "eat"], "dining"),
        (["office", "study"], "office"),
        (["hall", "corridor", "foyer", "entry", "lobby"], "hallway"),
        (["utility", "mechanical", "mech", "plant", "laundry"], "utility"),
        (["patio", "terrace", "balcony", "deck"], "patio"),
        (["garage", "parking"], "garage"),
        (["conference", "meeting"], "conference"),
        (["reception"], "reception"),
        (["server", "it", "data"], "server"),
    ]
    for kws, rt in buckets:
        if any(k in n for k in kws):
            return rt
    return "living"


def _conditioned(rtype: str) -> bool:
    return rtype not in ("patio", "garage")


def _wet(rtype: str) -> bool:
    return rtype in ("bathroom", "kitchen", "utility")


def _room_center(room: Dict[str, Any]) -> Tuple[float, float]:
    x = float(room.get("x", 0))
    y = float(room.get("y", 0))
    w = float(room.get("width", 4))
    d = float(room.get("depth", 3))
    return (x + w / 2.0, y + d / 2.0)


def _building_bounds(rooms: List[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    xs0 = [float(r.get("x", 0)) for r in rooms]
    ys0 = [float(r.get("y", 0)) for r in rooms]
    xs1 = [float(r.get("x", 0)) + float(r.get("width", 4)) for r in rooms]
    ys1 = [float(r.get("y", 0)) + float(r.get("depth", 3)) for r in rooms]
    return min(xs0), min(ys0), max(xs1), max(ys1)


def _mech_room(rooms: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prefer utility/mechanical room; fall back to hallway; else None."""
    for r in rooms:
        if _room_type(r.get("name", "")) == "utility":
            return r
    for r in rooms:
        if _room_type(r.get("name", "")) == "hallway":
            return r
    return None


# ── IFC helpers ──────────────────────────────────────────────────────────────
def _create(model, ifc_class: str, name: str):
    return ifcopenshell.api.run("root.create_entity", model, ifc_class=ifc_class, name=name)


def _assign_rep(model, product, rep):
    ifcopenshell.api.run("geometry.assign_representation", model, product=product, representation=rep)


def _contain(model, products, storey):
    ifcopenshell.api.run("spatial.assign_container", model, products=products, relating_structure=storey)


def _place(model, product, x, y, z, place_element):
    place_element(model, product, x, y, z)


# ── HVAC: air handler, condenser, trunk duct, branch ducts, diffusers, returns
def _build_hvac(model, body, storey, rooms, elev, ceil_h, *,
                box_rep, place_element, color_rep):
    mech = _mech_room(rooms)
    conditioned_rooms = [r for r in rooms if _conditioned(_room_type(r.get("name", "")))]
    if not conditioned_rooms:
        return

    # ── Air handler (indoor unit) ────────────────────────────────────────
    if mech:
        mx = float(mech.get("x", 0)) + 0.3
        my = float(mech.get("y", 0)) + 0.3
        ah = _create(model, "IfcUnitaryEquipment", "Air Handler (AHU)")
        ah_shape, ah_rep = box_rep(model, body, 0.80, 0.60, 1.50)
        _assign_rep(model, ah, ah_shape)
        _place(model, ah, mx, my, elev, place_element)
        color_rep(model, ah_rep, "hvac_equipment")
        _contain(model, [ah], storey)
        trunk_origin = (mx + 0.40, my + 0.60)
    else:
        # No mech room — put AHU in first conditioned room, corner
        r0 = conditioned_rooms[0]
        mx = float(r0.get("x", 0)) + 0.2
        my = float(r0.get("y", 0)) + 0.2
        ah = _create(model, "IfcUnitaryEquipment", "Air Handler (AHU)")
        ah_shape, ah_rep = box_rep(model, body, 0.70, 0.50, 1.30)
        _assign_rep(model, ah, ah_shape)
        _place(model, ah, mx, my, elev, place_element)
        color_rep(model, ah_rep, "hvac_equipment")
        _contain(model, [ah], storey)
        trunk_origin = (mx + 0.35, my + 0.50)

    # ── Outdoor condenser on a pad ───────────────────────────────────────
    min_x, min_y, max_x, max_y = _building_bounds(rooms)
    cond_x = max_x + 0.6
    cond_y = min_y + (max_y - min_y) * 0.5 - 0.4
    cond = _create(model, "IfcUnitaryEquipment", "Condenser (Outdoor)")
    cond_shape, cond_rep = box_rep(model, body, 0.80, 0.80, 1.00)
    _assign_rep(model, cond, cond_shape)
    _place(model, cond, cond_x, cond_y, elev, place_element)
    color_rep(model, cond_rep, "hvac_equipment")
    _contain(model, [cond], storey)

    # Refrigerant line set (pipe) from AHU to condenser — at ceiling
    line_len = max(0.5, cond_x - trunk_origin[0])
    refr = _create(model, "IfcPipeSegment", "Refrigerant Line Set")
    refr_shape, refr_rep = box_rep(model, body, line_len, 0.08, 0.08)
    _assign_rep(model, refr, refr_shape)
    _place(model, refr, trunk_origin[0], cond_y + 0.4, elev + ceil_h - 0.25, place_element)
    color_rep(model, refr_rep, "pipe")
    _contain(model, [refr], storey)

    # ── Main supply trunk duct along the main corridor ───────────────────
    # Heuristic: longest x-extent or longest hallway; if no hallway, run
    # a trunk from trunk_origin across the building in +x direction at ceiling.
    hallway = next((r for r in rooms if _room_type(r.get("name", "")) == "hallway"), None)
    if hallway:
        hx = float(hallway.get("x", 0)) + 0.2
        hy = float(hallway.get("y", 0)) + float(hallway.get("depth", 2)) / 2.0 - 0.2
        hw = max(1.0, float(hallway.get("width", 4)) - 0.4)
        trunk = _create(model, "IfcDuctSegment", "Supply Trunk Duct")
        trunk_shape, trunk_rep = box_rep(model, body, hw, 0.40, 0.25)
        _assign_rep(model, trunk, trunk_shape)
        _place(model, trunk, hx, hy, elev + ceil_h - 0.30, place_element)
        color_rep(model, trunk_rep, "duct_supply")
        _contain(model, [trunk], storey)
    else:
        trunk_len = max(1.0, max_x - trunk_origin[0] - 0.3)
        trunk = _create(model, "IfcDuctSegment", "Supply Trunk Duct")
        trunk_shape, trunk_rep = box_rep(model, body, trunk_len, 0.40, 0.25)
        _assign_rep(model, trunk, trunk_shape)
        _place(model, trunk, trunk_origin[0], trunk_origin[1], elev + ceil_h - 0.30, place_element)
        color_rep(model, trunk_rep, "duct_supply")
        _contain(model, [trunk], storey)

    # ── Branch duct + supply diffuser per conditioned room ───────────────
    for r in conditioned_rooms:
        rx = float(r.get("x", 0))
        ry = float(r.get("y", 0))
        rw = float(r.get("width", 4))
        rd = float(r.get("depth", 3))
        rname = r.get("name", "Room")

        cx, cy = _room_center(r)

        # Branch duct: small flex from trunk down into room ceiling cavity
        branch = _create(model, "IfcDuctSegment", f"{rname} Branch Duct")
        branch_shape, branch_rep = box_rep(model, body, 0.15, 0.15, 0.30)
        _assign_rep(model, branch, branch_shape)
        _place(model, branch, cx - 0.075, cy - 0.075, elev + ceil_h - 0.35, place_element)
        color_rep(model, branch_rep, "duct_supply")
        _contain(model, [branch], storey)

        # Supply diffuser at ceiling
        diff = _create(model, "IfcAirTerminal", f"{rname} Supply Diffuser")
        diff_shape, diff_rep = box_rep(model, body, 0.30, 0.30, 0.04)
        _assign_rep(model, diff, diff_shape)
        _place(model, diff, cx - 0.15, cy - 0.15, elev + ceil_h - 0.06, place_element)
        color_rep(model, diff_rep, "air_terminal")
        _contain(model, [diff], storey)

        # Return grille near the room doorway side (bigger rooms only)
        if rw * rd > 9.0:
            ret = _create(model, "IfcAirTerminal", f"{rname} Return Grille")
            ret_shape, ret_rep = box_rep(model, body, 0.45, 0.25, 0.04)
            _assign_rep(model, ret, ret_shape)
            rx2 = rx + rw * 0.15
            ry2 = ry + rd * 0.15
            _place(model, ret, rx2, ry2, elev + ceil_h - 0.06, place_element)
            color_rep(model, ret_rep, "air_terminal_return")
            _contain(model, [ret], storey)


# ── Plumbing: water heater, cold/hot supply, DWV stack per wet room ──────────
def _build_plumbing(model, body, storey, rooms, elev, ceil_h, *,
                    box_rep, place_element, color_rep):
    wet_rooms = [r for r in rooms if _wet(_room_type(r.get("name", "")))]
    if not wet_rooms:
        return

    mech = _mech_room(rooms) or wet_rooms[0]
    mx = float(mech.get("x", 0)) + 0.8
    my = float(mech.get("y", 0)) + 0.3

    # Water heater
    wh = _create(model, "IfcTank", "Water Heater")
    wh_shape, wh_rep = box_rep(model, body, 0.55, 0.55, 1.50)
    _assign_rep(model, wh, wh_shape)
    _place(model, wh, mx, my, elev, place_element)
    color_rep(model, wh_rep, "plumbing_equipment")
    _contain(model, [wh], storey)

    # One cold-water supply pipe + one hot-water supply pipe + one DWV stack per wet room
    wh_x, wh_y = mx + 0.275, my + 0.275
    for r in wet_rooms:
        rname = r.get("name", "Room")
        rx = float(r.get("x", 0))
        ry = float(r.get("y", 0))
        rw = float(r.get("width", 4))
        rd = float(r.get("depth", 3))
        cx, cy = _room_center(r)

        # Cold water supply
        cw = _create(model, "IfcPipeSegment", f"{rname} Cold Water Supply")
        length = max(0.5, math.hypot(cx - wh_x, cy - wh_y))
        cw_shape, cw_rep = box_rep(model, body, 0.04, 0.04, length)
        _assign_rep(model, cw, cw_shape)
        _place(model, cw, rx + 0.3, ry + 0.2, elev + 0.05, place_element)
        color_rep(model, cw_rep, "pipe_cold")
        _contain(model, [cw], storey)

        # Hot water supply
        hw = _create(model, "IfcPipeSegment", f"{rname} Hot Water Supply")
        hw_shape, hw_rep = box_rep(model, body, 0.04, 0.04, length)
        _assign_rep(model, hw, hw_shape)
        _place(model, hw, rx + 0.4, ry + 0.2, elev + 0.05, place_element)
        color_rep(model, hw_rep, "pipe_hot")
        _contain(model, [hw], storey)

        # DWV vertical stack — runs through the wet wall up to above the ceiling
        dwv = _create(model, "IfcPipeSegment", f"{rname} DWV Stack")
        dwv_shape, dwv_rep = box_rep(model, body, 0.10, 0.10, ceil_h + 0.3)
        _assign_rep(model, dwv, dwv_shape)
        _place(model, dwv, rx + rw - 0.25, ry + rd - 0.25, elev - 0.1, place_element)
        color_rep(model, dwv_rep, "pipe_dwv")
        _contain(model, [dwv], storey)


# ── Electrical: panel, branch circuits (visual), outlets, light fixtures ─────
def _build_electrical(model, body, storey, rooms, elev, ceil_h, *,
                      box_rep, place_element, color_rep):
    if not rooms:
        return

    mech = _mech_room(rooms) or rooms[0]
    px = float(mech.get("x", 0)) + 0.1
    py = float(mech.get("y", 0)) + float(mech.get("depth", 3)) - 0.25

    # Electrical distribution panel
    panel = _create(model, "IfcElectricDistributionBoard", "Main Electrical Panel")
    p_shape, p_rep = box_rep(model, body, 0.40, 0.15, 0.75)
    _assign_rep(model, panel, p_shape)
    _place(model, panel, px, py, elev + 1.20, place_element)
    color_rep(model, p_rep, "elec_panel")
    _contain(model, [panel], storey)

    # Per-room: one ceiling light fixture + two outlets on opposite walls
    for r in rooms:
        rname = r.get("name", "Room")
        rx = float(r.get("x", 0))
        ry = float(r.get("y", 0))
        rw = float(r.get("width", 4))
        rd = float(r.get("depth", 3))
        cx, cy = _room_center(r)

        # Ceiling light
        light = _create(model, "IfcLightFixture", f"{rname} Light")
        l_shape, l_rep = box_rep(model, body, 0.30, 0.30, 0.10)
        _assign_rep(model, light, l_shape)
        _place(model, light, cx - 0.15, cy - 0.15, elev + ceil_h - 0.12, place_element)
        color_rep(model, l_rep, "light")
        _contain(model, [light], storey)

        # Outlets on two opposite walls (skip very small rooms)
        if rw >= 1.5 and rd >= 1.5:
            for ox, oy in [
                (rx + rw * 0.25, ry + 0.02),
                (rx + rw * 0.75, ry + rd - 0.10),
            ]:
                out = _create(model, "IfcOutlet", f"{rname} Outlet")
                o_shape, o_rep = box_rep(model, body, 0.10, 0.06, 0.12)
                _assign_rep(model, out, o_shape)
                _place(model, out, ox, oy, elev + 0.30, place_element)
                color_rep(model, o_rep, "outlet")
                _contain(model, [out], storey)


# ── Fire: smoke detectors + sprinklers (commercial) ──────────────────────────
def _build_fire(model, body, storey, rooms, elev, ceil_h, *,
                box_rep, place_element, color_rep, metadata):
    if not rooms:
        return
    building_type = (metadata.get("building_type") or "").lower()
    # Residential: smoke detector per room. Commercial: add sprinklers too.
    is_commercial = any(k in building_type for k in ("office", "commercial", "retail", "warehouse",
                                                     "hospital", "school", "industrial", "hotel"))
    for r in rooms:
        rname = r.get("name", "Room")
        cx, cy = _room_center(r)

        # Smoke detector
        sd = _create(model, "IfcFireSuppressionTerminal", f"{rname} Smoke Detector")
        sd_shape, sd_rep = box_rep(model, body, 0.15, 0.15, 0.04)
        _assign_rep(model, sd, sd_shape)
        _place(model, sd, cx - 0.40, cy - 0.075, elev + ceil_h - 0.05, place_element)
        color_rep(model, sd_rep, "fire_device")
        _contain(model, [sd], storey)

        if is_commercial:
            sp = _create(model, "IfcFireSuppressionTerminal", f"{rname} Sprinkler Head")
            sp_shape, sp_rep = box_rep(model, body, 0.08, 0.08, 0.08)
            _assign_rep(model, sp, sp_shape)
            _place(model, sp, cx + 0.40, cy, elev + ceil_h - 0.08, place_element)
            color_rep(model, sp_rep, "fire_device")
            _contain(model, [sp], storey)


# ── Public entry point ──────────────────────────────────────────────────────
def build_mep(model, body, storey, rooms, elev, ceil_h, *,
              box_rep: Callable, place_element: Callable, color_rep: Callable,
              metadata: Optional[Dict[str, Any]] = None) -> None:
    """
    Populate an IFC floor with HVAC, plumbing, electrical, and fire systems.

    Safe to call even with empty rooms — it silently no-ops.

    Each subsystem is wrapped in its own try/except so a failure in one
    (e.g. exotic IFC class not in schema) doesn't kill the rest.
    """
    metadata = metadata or {}
    if not rooms:
        return

    for name, fn in [
        ("HVAC",       _build_hvac),
        ("Plumbing",   _build_plumbing),
        ("Electrical", _build_electrical),
    ]:
        try:
            fn(model, body, storey, rooms, elev, ceil_h,
               box_rep=box_rep, place_element=place_element, color_rep=color_rep)
        except Exception as e:
            logger.warning("MEP %s subsystem failed: %s", name, e)

    try:
        _build_fire(model, body, storey, rooms, elev, ceil_h,
                    box_rep=box_rep, place_element=place_element, color_rep=color_rep,
                    metadata=metadata)
    except Exception as e:
        logger.warning("MEP Fire subsystem failed: %s", e)
