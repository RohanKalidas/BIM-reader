"""
component_library.py — Inject the primitive vocabulary into the facade prompt.

Reads exterior_primitives.PRIMITIVES at import time and produces a compact
text description of every available primitive. This description gets merged
into FACADE_AGENT_PROMPT_TEMPLATE so the facade agent's prompt is always in
sync with the actual library, without you maintaining two copies.

If you add a new primitive to exterior_primitives.py, register it in
PRIMITIVES, and add an entry here in PRIMITIVE_DOCS — that's the only change
needed. No prompt editing.
"""

from typing import Dict


# Human-readable documentation for each primitive, used to build the prompt.
# Keep each entry to one line of what-it-is + the param list.
PRIMITIVE_DOCS: Dict[str, str] = {
    "turret": (
        "Polygonal tower projecting from a corner. "
        "Params: corner (sw/se/nw/ne), radius (m), height (m, default ceil_h+0.4), "
        "sides (int, 4-12), cap ('conical'|'flat'|'none'), spire (bool), "
        "color_key (palette key), cap_color_key."
    ),
    "porch": (
        "Covered porch slab along one or more sides. "
        "Params: sides (list of 'south'/'north'/'east'/'west' — use multiple for wraparound), "
        "depth (m), column_count (int), column_style ('square'|'round'|'turned'|'tapered'), "
        "column_size (m), column_color_key, has_roof (bool)."
    ),
    "wraparound_porch": "Alias for porch with multiple sides. Prefer using 'porch' with sides=[...] directly.",
    "gable": (
        "Triangular facade end — over entry or along a roof span. "
        "Params: side, position (0-1 along side), width (m), height (m, vertical peak), color_key."
    ),
    "dormer": (
        "Small gabled roof-window projection. "
        "Params: side, position (0-1), width (m), height (m), projection (m, how far it sticks out)."
    ),
    "chimney": (
        "Rectangular chimney stack with optional cap. "
        "Params: position ([x, y] in meters — absolute world coords), "
        "width (m), depth (m), height (m — total height above floor), cap (bool), color_key."
    ),
    "bay_window": (
        "Projecting box with windows on a facade. "
        "Params: side, position (0-1), width (m, along facade), projection (m, outward), "
        "height (m), sill (m, bottom above floor), sides (3 or 5 — angled vs bow)."
    ),
    "canopy": (
        "Flat slab projecting from a wall, above an entry or window. "
        "Params: side, position (0-1), width (m), projection (m), "
        "elevation (m, height on wall — default ceil_h*0.9), color_key."
    ),
    "column": (
        "Standalone column — decorative or structural. "
        "Params: position ([x, y] absolute meters — REQUIRED), "
        "height (m), size (m), style ('square'|'round'|'fluted'), color_key."
    ),
    "portico": (
        "Classical entry: columns + pediment triangle on top. "
        "Params: side, width (m), projection (m), column_count (int), pediment (bool)."
    ),
    "balcony": (
        "Cantilevered slab with railing on an upper floor. "
        "Params: side, position (0-1), width (m), projection (m), floor (int — 1=second floor)."
    ),
    "parapet": (
        "Low wall along the roof edge for flat-roofed buildings. "
        "Params: height (m), thickness (m), "
        "sides (list — default all four), stepped (bool — Art Deco step-back), color_key."
    ),
    "half_timber_band": (
        "Tudor-style dark vertical timbers on the upper facade. "
        "Params: side, count (int), band_top_frac (0-1), band_bottom_frac (0-1), color_key."
    ),
    "shutter": (
        "Decorative shutters flanking a window. Automatically places both sides. "
        "Params: side, position (0-1 — window center), sill (m), width (m), height (m), color_key."
    ),
    "pergola": (
        "Open beam structure — patio cover. "
        "Params: position ([x, y] meters — REQUIRED, center), "
        "width (m), depth (m), height (m), beam_count (int), color_key."
    ),
    "vertical_fin": (
        "Thin vertical wall fins — modernist/brutalist detail. "
        "Params: side, count (int), depth (m, outward projection), color_key."
    ),
    "awning": (
        "Angled sun shade over a window. "
        "Params: side, position (0-1), width (m), projection (m), sill (m), color_key."
    ),
}


def build_primitive_reference() -> str:
    """
    Build the text block injected into FACADE_AGENT_PROMPT_TEMPLATE at the
    {primitive_reference} slot.

    Tries to import exterior_primitives.PRIMITIVES at runtime to catch any
    primitives that are registered but undocumented here. If the import
    fails (e.g. running in a fresh venv without ifcopenshell), falls back to
    PRIMITIVE_DOCS alone.
    """
    try:
        from exterior_primitives import PRIMITIVES  # type: ignore
        registered = set(PRIMITIVES.keys())
    except Exception:
        registered = set(PRIMITIVE_DOCS.keys())

    # Warn about drift between registry and docs (in logs, not in prompt)
    undocumented = registered - set(PRIMITIVE_DOCS.keys())
    if undocumented:
        import logging
        logging.getLogger(__name__).warning(
            "Primitives registered in exterior_primitives.py but missing docs in "
            "component_library.py: %s", sorted(undocumented)
        )

    lines = []
    for name in sorted(registered or PRIMITIVE_DOCS.keys()):
        doc = PRIMITIVE_DOCS.get(name, "(undocumented — see exterior_primitives.py)")
        lines.append(f"• {name}: {doc}")
    return "\n".join(lines)
