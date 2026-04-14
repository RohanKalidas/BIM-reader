"""
Door-aware fixture positions (world metres). Coordinates: +x east, +y north.
"""

import math
from typing import Any, Dict, List, Optional, Tuple


def _door(d: Any) -> str:
    x = (d or "south").lower().strip()
    if x not in ("north", "south", "east", "west"):
        return "south"
    return x


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _inward_and_tangent(
    rx: float, ry: float, rw: float, rd: float, door_wall: str
) -> Tuple[Tuple[float, float], Tuple[float, float], str]:
    """
    Back wall = wall opposite door. Returns:
    - inward unit (into room from back wall)
    - tangent unit (along back wall, west→east)
    - edge id n/s/e/w for the back wall
    """
    d = _door(door_wall)
    if d == "south":
        inward = (0.0, -1.0)
        tangent = (1.0, 0.0)
        edge = "n"
    elif d == "north":
        inward = (0.0, 1.0)
        tangent = (1.0, 0.0)
        edge = "s"
    elif d == "east":
        inward = (-1.0, 0.0)
        tangent = (0.0, 1.0)
        edge = "w"
    else:  # west
        inward = (1.0, 0.0)
        tangent = (0.0, 1.0)
        edge = "e"
    return inward, tangent, edge


def _angle_deg_from_x(vx: float, vy: float) -> float:
    """Angle in degrees so local +X axis aligns with (vx, vy) in plan."""
    return math.degrees(math.atan2(vy, vx))


def plan_positions(
    rtype: str,
    room: Dict[str, Any],
    fixture_rows: List[Tuple],
) -> List[Dict[str, Any]]:
    """
    fixture_rows: list of (fname, fx, fy, fw, fd, fh, fcolor, fclass) template —
    fx,fy from FIXTURES are ignored; we replace with computed ax,ay and plan_rot_z.

    Returns list of dicts:
      fname, fw, fd, fh, fcolor, fclass, ax, ay, plan_rot_z
    """
    rx = float(room.get("x", 0))
    ry = float(room.get("y", 0))
    rw = float(room.get("width", 4))
    rd = float(room.get("depth", 3))
    door = _door(room.get("door_wall"))
    inward, tangent, _back = _inward_and_tangent(rx, ry, rw, rd, door)
    ix, iy = inward
    tx, ty = tangent

    margin = 0.22
    inner_lo_x, inner_hi_x = rx + margin, rx + rw - margin
    inner_lo_y, inner_hi_y = ry + margin, ry + rd - margin
    mid_x = rx + rw / 2.0
    mid_y = ry + rd / 2.0

    # Back edge anchor (inside room from back wall)
    if door in ("south", "north"):
        if door == "south":
            back_y = inner_hi_y
            back_x0, back_x1 = inner_lo_x, inner_hi_x
            row_y = back_y - 0.55
        else:
            back_y = inner_lo_y
            back_x0, back_x1 = inner_lo_x, inner_hi_x
            row_y = back_y + 0.55
        row_x0, row_x1 = sorted((back_x0, back_x1))
    else:
        if door == "east":
            back_x = inner_lo_x
            row_x = back_x + 0.55
        else:
            back_x = inner_hi_x
            row_x = back_x - 0.55
        row_y0, row_y1 = inner_lo_y, inner_hi_y
        row_y0, row_y1 = sorted((row_y0, row_y1))

    out: List[Dict[str, Any]] = []

    def add(fname, fw, fd, fh, fcolor, fclass, ax, ay, plan_rot_z):
        out.append(
            {
                "fname": fname,
                "fw": fw,
                "fd": fd,
                "fh": fh,
                "fcolor": fcolor,
                "fclass": fclass,
                "ax": ax,
                "ay": ay,
                "plan_rot_z": plan_rot_z,
            }
        )

    # --- Kitchen: single row along back wall ---
    if rtype == "kitchen" and len(fixture_rows) >= 3:
        names = [row[0] for row in fixture_rows]
        dims = [(row[3], row[4]) for row in fixture_rows]
        if door in ("south", "north"):
            total_w = sum(d[0] for d in dims) + 0.35 * (len(dims) - 1)
            start_x = _clamp(mid_x - total_w / 2, inner_lo_x, inner_hi_x - total_w)
            x_cursor = start_x
            row_y_k = row_y
            for i, row in enumerate(fixture_rows):
                fname, _, _, fw, fd, fh, fcolor, fclass = row
                fw, fd = float(fw), float(fd)
                cx = x_cursor + fw / 2
                ay = row_y_k - fd / 2
                ax = cx - fw / 2
                ax = _clamp(ax, inner_lo_x, inner_hi_x - fw)
                ay = _clamp(ay, inner_lo_y, inner_hi_y - fd)
                rz = _angle_deg_from_x(ix, iy)
                add(fname, fw, fd, fh, fcolor, fclass, ax, ay, rz)
                x_cursor += fw + 0.35
        else:
            total_d = sum(d[1] for d in dims) + 0.35 * (len(dims) - 1)
            start_y = _clamp(mid_y - total_d / 2, inner_lo_y, inner_hi_y - total_d)
            y_cursor = start_y
            for row in fixture_rows:
                fname, _, _, fw, fd, fh, fcolor, fclass = row
                fw, fd = float(fw), float(fd)
                cy = y_cursor + fd / 2
                ax = row_x - fw / 2
                ay = cy - fd / 2
                ax = _clamp(ax, inner_lo_x, inner_hi_x - fw)
                ay = _clamp(ay, inner_lo_y, inner_hi_y - fd)
                rz = _angle_deg_from_x(ix, iy)
                add(fname, fw, fd, fh, fcolor, fclass, ax, ay, rz)
                y_cursor += fd + 0.35
        return out

    # --- Bedroom ---
    if rtype == "bedroom":
        bed = next((r for r in fixture_rows if "bed" in r[0].lower()), None)
        ward = next((r for r in fixture_rows if "wardrobe" in r[0].lower()), None)
        night = next((r for r in fixture_rows if "night" in r[0].lower()), None)
        bed_rz = _angle_deg_from_x(ix, iy)
        if bed:
            fname, _, _, fw, fd, fh, fcolor, fclass = bed
            fw, fd = float(fw), float(fd)
            bx = mid_x - fw / 2
            if door in ("south", "north"):
                by = inner_hi_y - fd - 0.08 if door == "south" else inner_lo_y + 0.08
                bx = _clamp(bx, inner_lo_x, inner_hi_x - fw)
                by = _clamp(by, inner_lo_y, inner_hi_y - fd)
            else:
                bx = inner_lo_x + 0.08 if door == "east" else inner_hi_x - fw - 0.08
                by = mid_y - fd / 2
                bx = _clamp(bx, inner_lo_x, inner_hi_x - fw)
                by = _clamp(by, inner_lo_y, inner_hi_y - fd)
            add(fname, fw, fd, fh, fcolor, fclass, bx, by, bed_rz)
        if ward:
            fname, _, _, fw, fd, fh, fcolor, fclass = ward
            fw, fd = float(fw), float(fd)
            wrz = _angle_deg_from_x(tx, ty)
            if door in ("south", "north"):
                wx = inner_lo_x + 0.06
                wy = mid_y - fd / 2
            else:
                wx = mid_x - fw / 2
                wy = inner_lo_y + 0.06
            wx = _clamp(wx, inner_lo_x, inner_hi_x - fw)
            wy = _clamp(wy, inner_lo_y, inner_hi_y - fd)
            add(fname, fw, fd, fh, fcolor, fclass, wx, wy, wrz)
        if night:
            fname, _, _, fw, fd, fh, fcolor, fclass = night
            fw, fd = float(fw), float(fd)
            bbed = next((o for o in out if "bed" in o["fname"].lower()), None)
            if bbed:
                nax = bbed["ax"] + bbed["fw"] - fw - 0.08
                nay = bbed["ay"]
                nax = _clamp(nax, inner_lo_x, inner_hi_x - fw)
                add(fname, fw, fd, fh, fcolor, fclass, nax, nay, bbed.get("plan_rot_z", 0))
        placed = {o["fname"] for o in out}
        for row in fixture_rows:
            if row[0] in placed:
                continue
            _, _, _, fw, fd, fh, fcolor, fclass = row
            fw, fd = float(fw), float(fd)
            add(row[0], fw, fd, fh, fcolor, fclass, mid_x - fw / 2, mid_y - fd / 2, 0.0)
        return out

    # --- Living ---
    if rtype == "living":
        sofa = next((r for r in fixture_rows if "sofa" in r[0].lower()), None)
        tv = next((r for r in fixture_rows if "tv" in r[0].lower()), None)
        ct = next((r for r in fixture_rows if "coffee" in r[0].lower()), None)
        if sofa:
            fname, _, _, fw, fd, fh, fcolor, fclass = sofa
            fw, fd = float(fw), float(fd)
            if door in ("south", "north"):
                if door == "south":
                    sy = inner_hi_y - fd - 0.1
                else:
                    sy = inner_lo_y + 0.1
                sx = mid_x - fw / 2
                rz = _angle_deg_from_x(-ix, -iy)
            else:
                if door == "east":
                    sx = inner_lo_x + 0.1
                else:
                    sx = inner_hi_x - fw - 0.1
                sy = mid_y - fd / 2
                rz = _angle_deg_from_x(-ix, -iy)
            sx = _clamp(sx, inner_lo_x, inner_hi_x - fw)
            sy = _clamp(sy, inner_lo_y, inner_hi_y - fd)
            add(fname, fw, fd, fh, fcolor, fclass, sx, sy, rz)
        if tv:
            fname, _, _, fw, fd, fh, fcolor, fclass = tv
            fw, fd = float(fw), float(fd)
            if door in ("south", "north"):
                ty = inner_lo_y + 0.12 if door == "south" else inner_hi_y - fd - 0.12
                tx = mid_x - fw / 2
                rz = _angle_deg_from_x(ix, iy)
            else:
                tx = inner_hi_x - fw - 0.12 if door == "east" else inner_lo_x + 0.12
                ty = mid_y - fd / 2
                rz = _angle_deg_from_x(ix, iy)
            tx = _clamp(tx, inner_lo_x, inner_hi_x - fw)
            ty = _clamp(ty, inner_lo_y, inner_hi_y - fd)
            add(fname, fw, fd, fh, fcolor, fclass, tx, ty, rz)
        if ct:
            fname, _, _, fw, fd, fh, fcolor, fclass = ct
            fw, fd = float(fw), float(fd)
            add(
                fname,
                fw,
                fd,
                fh,
                fcolor,
                fclass,
                mid_x - fw / 2,
                mid_y - fd / 2,
                0.0,
            )
        placed = {o["fname"] for o in out}
        for row in fixture_rows:
            if row[0] in placed:
                continue
            _, _, _, fw, fd, fh, fcolor, fclass = row
            fw, fd = float(fw), float(fd)
            add(row[0], fw, fd, fh, fcolor, fclass, mid_x - fw / 2, mid_y - fd / 2, 0.0)
        return out

    # --- Bathroom: wet wall along back ---
    if rtype == "bathroom":
        n = len(fixture_rows)
        if door in ("south", "north") and n:
            total_w = sum(float(r[3]) for r in fixture_rows) + 0.25 * (n - 1)
            sx = _clamp(mid_x - total_w / 2, inner_lo_x, inner_hi_x - total_w)
            cx = sx
            row_y_b = row_y
            for row in fixture_rows:
                fname, _, _, fw, fd, fh, fcolor, fclass = row
                fw, fd = float(fw), float(fd)
                ax = cx
                ay = row_y_b - fd / 2
                ax = _clamp(ax, inner_lo_x, inner_hi_x - fw)
                ay = _clamp(ay, inner_lo_y, inner_hi_y - fd)
                rz = _angle_deg_from_x(ix, iy)
                add(fname, fw, fd, fh, fcolor, fclass, ax, ay, rz)
                cx += fw + 0.25
        elif n:
            total_d = sum(float(r[4]) for r in fixture_rows) + 0.25 * (n - 1)
            sy = _clamp(mid_y - total_d / 2, inner_lo_y, inner_hi_y - total_d)
            cy = sy
            for row in fixture_rows:
                fname, _, _, fw, fd, fh, fcolor, fclass = row
                fw, fd = float(fw), float(fd)
                ax = row_x - fw / 2
                ay = cy
                ax = _clamp(ax, inner_lo_x, inner_hi_x - fw)
                ay = _clamp(ay, inner_lo_y, inner_hi_y - fd)
                rz = _angle_deg_from_x(ix, iy)
                add(fname, fw, fd, fh, fcolor, fclass, ax, ay, rz)
                cy += fd + 0.25
        return out

    # --- Default: grid from template fx,fy but snapped to margins ---
    for row in fixture_rows:
        fname, fx, fy, fw, fd, fh, fcolor, fclass = row
        fw, fd = float(fw), float(fd)
        inner_w = max(rw - 2 * margin, 0.5)
        inner_d = max(rd - 2 * margin, 0.5)
        ax = rx + margin + float(fx) * inner_w - fw / 2
        ay = ry + margin + float(fy) * inner_d - fd / 2
        ax = _clamp(ax, inner_lo_x, inner_hi_x - fw)
        ay = _clamp(ay, inner_lo_y, inner_hi_y - fd)
        add(fname, fw, fd, fh, fcolor, fclass, ax, ay, 0.0)
    return out
