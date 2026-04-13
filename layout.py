"""
layout.py — BIM Studio floor plan packer
Takes a list of rooms with only width/depth and packs them into a valid
floor plan with zero gaps.

FIX: Rooms in the same row are normalized to the same depth (the max depth
in that row). This ensures north/south walls align perfectly and adjacent
rooms share walls without gaps.

Algorithm: row-based shelf packing
- Rooms are grouped into rows
- Each row's depth = deepest room in that row (all rooms stretched to match)
- Rows stack north (increasing Y)
- Within each row, rooms stack east (increasing X)
"""

import math

EXT_T = 0.20
INT_T = 0.12


def pack_rooms(rooms, max_width=None):
    """
    Pack rooms into a floor plan.
    
    Input: list of dicts with at least {name, width, depth}
    Output: same list with x, y, depth added (depth normalized per row)
    """
    if not rooms:
        return rooms

    total_area = sum(r.get("width", 4) * r.get("depth", 3) for r in rooms)
    if not max_width:
        max_width = math.sqrt(total_area) * 1.6
        max_width = max(max_width, max(r.get("width", 4) for r in rooms))

    # Separate room types for smarter placement
    public_rooms  = []
    private_rooms = []
    hallways      = []

    for r in rooms:
        n = r.get("name", "").lower()
        if any(x in n for x in ["hall", "corridor", "foyer", "entry", "lobby"]):
            hallways.append(r)
        elif any(x in n for x in ["bed", "master", "guest", "sleep", "bath", "wc",
                                    "toilet", "utility", "laundry"]):
            private_rooms.append(r)
        else:
            public_rooms.append(r)

    ordered = public_rooms + hallways + private_rooms

    # Pack into rows
    rows = []
    current_row = []
    current_x = 0.0

    for room in ordered:
        rw = float(room.get("width", 4.0))

        if current_row and current_x + rw > max_width + 0.01:
            rows.append(current_row)
            current_row = []
            current_x = 0.0

        current_row.append(room)
        current_x += rw

    if current_row:
        rows.append(current_row)

    # Assign coordinates — normalize depth within each row
    result = []
    current_y = 0.0

    for row in rows:
        # All rooms in a row get the SAME depth (the max in that row)
        # This eliminates gaps and ensures walls align
        row_depth = max(float(r.get("depth", 3.0)) for r in row)

        current_x = 0.0
        for room in row:
            r = dict(room)
            r["x"] = round(current_x, 3)
            r["y"] = round(current_y, 3)
            r["depth"] = round(row_depth, 3)  # <-- NORMALIZE depth to row max
            r["width"] = round(float(room.get("width", 4.0)), 3)
            result.append(r)
            current_x += r["width"]

        current_y += row_depth

    # Make hallways span the full building width
    if result:
        building_width = max(r["x"] + r["width"] for r in result)
        for r in result:
            n = r.get("name", "").lower()
            if any(x in n for x in ["hall", "corridor", "foyer", "entry", "lobby"]):
                r["width"] = round(building_width, 3)

    return result


def assign_door_walls(rooms):
    """
    Assign door_wall based on room position and adjacency.
    """
    if not rooms:
        return rooms

    min_y = min(r.get("y", 0) for r in rooms)
    max_y = max(r.get("y", 0) for r in rooms)

    for r in rooms:
        if "door_wall" in r:
            continue

        n = r.get("name", "").lower()
        y = r.get("y", 0)

        if any(x in n for x in ["hall", "corridor", "foyer"]):
            r["door_wall"] = "east"
        elif y <= min_y + 0.1:
            r["door_wall"] = "north"
        elif y >= max_y - 0.1:
            r["door_wall"] = "south"
        else:
            r["door_wall"] = "south"

    return rooms


def assign_exterior(rooms):
    """Mark rooms as exterior if they're on the building perimeter."""
    if not rooms:
        return rooms

    min_x = min(r.get("x", 0) for r in rooms)
    max_x = max(r.get("x", 0) + r.get("width", 4) for r in rooms)
    min_y = min(r.get("y", 0) for r in rooms)
    max_y = max(r.get("y", 0) + r.get("depth", 3) for r in rooms)

    for r in rooms:
        if "exterior" in r:
            continue
        rx = r.get("x", 0)
        ry = r.get("y", 0)
        rw = r.get("width", 4)
        rd = r.get("depth", 3)
        on_boundary = (
            abs(rx - min_x) < 0.01 or
            abs(rx + rw - max_x) < 0.01 or
            abs(ry - min_y) < 0.01 or
            abs(ry + rd - max_y) < 0.01
        )
        r["exterior"] = on_boundary

    return rooms


def process_floor(floor):
    """Process a single floor: pack, assign exterior, assign doors."""
    rooms = floor.get("rooms", [])
    if not rooms:
        return floor

    has_coords = all("x" in r and "y" in r for r in rooms)

    if not has_coords:
        rooms = pack_rooms(rooms)
    else:
        rooms = fix_gaps(rooms)

    rooms = assign_exterior(rooms)
    rooms = assign_door_walls(rooms)

    floor = dict(floor)
    floor["rooms"] = rooms
    return floor


def fix_gaps(rooms):
    """
    If rooms have AI-provided coordinates, snap them to remove gaps.
    Normalizes depth within each row.
    """
    if not rooms:
        return rooms

    # Group by approximate Y (within 0.5m)
    rows = {}
    for r in rooms:
        y = round(float(r.get("y", 0)) * 2) / 2
        if y not in rows:
            rows[y] = []
        rows[y].append(r)

    result = []
    current_y = 0.0
    for y_key in sorted(rows.keys()):
        row = sorted(rows[y_key], key=lambda r: float(r.get("x", 0)))
        row_depth = max(float(r.get("depth", 3)) for r in row)
        current_x = 0.0
        for r in row:
            r = dict(r)
            r["x"] = round(current_x, 3)
            r["y"] = round(current_y, 3)
            r["depth"] = round(row_depth, 3)  # normalize depth
            r["width"] = round(float(r.get("width", 4)), 3)
            result.append(r)
            current_x += r["width"]
        current_y += row_depth

    return result


def process_spec(spec):
    """Process a full building spec, fixing room layouts on all floors."""
    spec = dict(spec)
    floors = spec.get("floors", [])
    spec["floors"] = [process_floor(f) for f in floors]
    return spec


if __name__ == "__main__":
    test_rooms = [
        {"name": "Living Room", "width": 5.5, "depth": 4.5},
        {"name": "Kitchen",     "width": 3.5, "depth": 4.5},
        {"name": "Hallway",     "width": 9.0, "depth": 1.5},
        {"name": "Bedroom",     "width": 4.5, "depth": 3.5},
        {"name": "Bathroom",    "width": 2.5, "depth": 2.0},
        {"name": "Patio",       "width": 2.0, "depth": 3.5},
    ]
    packed = pack_rooms(test_rooms)
    packed = assign_exterior(packed)
    packed = assign_door_walls(packed)
    for r in packed:
        print(f"{r['name']:20} x={r['x']:5.1f} y={r['y']:5.1f} "
              f"w={r['width']:4.1f} d={r['depth']:4.1f} "
              f"ext={r.get('exterior', False)} door={r.get('door_wall', '?')}")
