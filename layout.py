"""
layout.py — BIM Studio floor plan validator
The AI provides x, y coordinates for all rooms. This module only:
1. Validates that coordinates exist (warns if missing)
2. Assigns exterior flags based on perimeter detection
3. Assigns door_wall if not specified
No repacking, no row-based shelf packing, no depth normalization.
"""


def assign_door_walls(rooms):
    """Assign door_wall based on room position if not specified by AI."""
    if not rooms:
        return rooms

    min_y = min(r.get("y", 0) for r in rooms)
    max_y = max(r.get("y", 0) + r.get("depth", 3) for r in rooms)

    for r in rooms:
        if "door_wall" in r:
            continue

        n = r.get("name", "").lower()
        y = r.get("y", 0)
        rd = r.get("depth", 3)

        if any(x in n for x in ["hall", "corridor", "foyer"]):
            r["door_wall"] = "east"
        elif y <= min_y + 0.1:
            r["door_wall"] = "north"
        elif y + rd >= max_y - 0.1:
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
    """Validate and enrich a single floor's room data."""
    rooms = floor.get("rooms", [])
    if not rooms:
        return floor

    # Check if AI provided coordinates
    has_coords = all("x" in r and "y" in r for r in rooms)

    if not has_coords:
        # Fallback: simple left-to-right, row-by-row packing
        # This should rarely happen since the AI is instructed to provide coords
        print("  WARNING: AI did not provide room coordinates, using fallback packing")
        rooms = _fallback_pack(rooms)

    rooms = assign_exterior(rooms)
    rooms = assign_door_walls(rooms)

    floor = dict(floor)
    floor["rooms"] = rooms
    return floor


def _fallback_pack(rooms):
    """
    Simple fallback if AI doesn't provide coordinates.
    Places rooms in a single row left to right.
    """
    result = []
    x = 0.0
    for room in rooms:
        r = dict(room)
        r["x"] = round(x, 3)
        r["y"] = 0.0
        rw = float(r.get("width", 4.0))
        result.append(r)
        x += rw
    return result


def process_spec(spec):
    """Process a full building spec."""
    spec = dict(spec)
    floors = spec.get("floors", [])
    spec["floors"] = [process_floor(f) for f in floors]
    return spec
