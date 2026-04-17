"""
styles.py — Architectural style registry for BIM Studio.

Maps architectural_style strings (e.g. "victorian", "tudor") to a palette of
colors, massing hints, roof pitch, window proportions, and trim details.

Used by:
- generate.py: to override the default COLORS palette and choose window sizes
- architectural_exterior.py: to read roof_pitch and massing features
- server.py: the AI can reference this list in the system prompt

Add new styles by appending to STYLES. Every style gets sensible fallbacks
via _BASE_STYLE so partial entries work fine.
"""

from typing import Any, Dict, Tuple

# RGBA tuples, 0.0-1.0 range. Alpha=0 means opaque.
# (r, g, b, transparency)  — matches the tuple shape used in generate.COLORS

_BASE_STYLE: Dict[str, Any] = {
    "ext_wall":     (0.88, 0.84, 0.76, 0.0),
    "int_wall":     (0.95, 0.94, 0.90, 0.0),
    "floor":        (0.72, 0.66, 0.55, 0.0),  # wood-ish default
    "roof":         (0.45, 0.40, 0.36, 0.0),
    "trim":         (0.98, 0.97, 0.94, 0.0),  # white trim default
    "accent":       (0.55, 0.35, 0.25, 0.0),  # door/accent default
    "window_frame": (0.30, 0.28, 0.26, 0.0),
    "window_glass": (0.55, 0.78, 0.92, 0.25),  # slight transparency, not invisible
    "ceiling":      (0.96, 0.96, 0.95, 0.0),

    # geometry hints
    "roof_pitch_deg":        22.0,   # 0 = flat, 45 = steep
    "roof_style":            "gable",  # gable | hip | flat | mansard | gambrel
    "window_width_m":        1.2,
    "window_height_m":       1.4,
    "window_sill_m":         0.9,
    "door_width_m":          0.9,
    "door_height_m":         2.1,

    # exterior accents — flags read by architectural_exterior.py
    "has_porch":             False,
    "has_portico":           False,
    "has_wraparound_porch":  False,
    "has_bay_window":        False,
    "has_turret":            False,
    "has_dormer":            False,
    "has_shutters":          False,
    "has_columns":           False,
    "has_half_timbering":    False,
    "has_overhang_eaves":    False,

    # narrative string the AI can use to justify choices to the user
    "description":           "Simple modern massing with neutral palette.",
}


STYLES: Dict[str, Dict[str, Any]] = {
    # ── Traditional / Historical ─────────────────────────────────────────
    "victorian": {
        "ext_wall":     (0.68, 0.48, 0.55, 0.0),   # dusty rose siding
        "trim":         (0.94, 0.92, 0.86, 0.0),   # cream gingerbread trim
        "accent":       (0.35, 0.20, 0.30, 0.0),   # deep plum accents
        "roof":         (0.28, 0.22, 0.25, 0.0),   # dark slate
        "floor":        (0.55, 0.40, 0.28, 0.0),   # oak
        "roof_pitch_deg":       38.0,
        "roof_style":           "gable",
        "window_width_m":       0.9,
        "window_height_m":      1.8,   # tall narrow sash windows
        "window_sill_m":        0.85,
        "has_porch":            True,
        "has_wraparound_porch": True,
        "has_bay_window":       True,
        "has_turret":           True,
        "has_dormer":           True,
        "has_shutters":         True,
        "description":          "Queen Anne Victorian with steep gables, turret, wraparound porch, tall narrow windows and gingerbread trim.",
    },

    "tudor": {
        "ext_wall":     (0.78, 0.72, 0.58, 0.0),   # cream stucco
        "trim":         (0.25, 0.18, 0.12, 0.0),   # dark timber
        "accent":       (0.20, 0.14, 0.10, 0.0),
        "roof":         (0.32, 0.24, 0.20, 0.0),   # brown shingle
        "floor":        (0.45, 0.32, 0.22, 0.0),   # dark wood
        "roof_pitch_deg":      42.0,
        "roof_style":          "gable",
        "window_width_m":      0.8,
        "window_height_m":     1.3,
        "has_porch":           False,
        "has_half_timbering":  True,
        "has_dormer":          True,
        "description":         "English Tudor revival with steep cross-gabled roof, decorative half-timbering over stucco, small casement windows.",
    },

    "colonial": {
        "ext_wall":     (0.90, 0.88, 0.82, 0.0),   # white clapboard
        "trim":         (0.96, 0.95, 0.92, 0.0),
        "accent":       (0.15, 0.20, 0.28, 0.0),   # navy shutters/door
        "roof":         (0.22, 0.22, 0.25, 0.0),   # dark asphalt
        "floor":        (0.62, 0.48, 0.35, 0.0),
        "roof_pitch_deg":      30.0,
        "roof_style":          "gable",
        "window_width_m":      0.85,
        "window_height_m":     1.4,
        "has_shutters":        True,
        "has_columns":         True,
        "has_portico":         True,
        "description":         "Symmetrical Colonial with white clapboard, navy shutters, centered portico entry, double-hung windows.",
    },

    "neoclassical": {
        "ext_wall":     (0.92, 0.90, 0.85, 0.0),
        "trim":         (0.98, 0.97, 0.94, 0.0),
        "accent":       (0.55, 0.45, 0.32, 0.0),
        "roof":         (0.30, 0.28, 0.28, 0.0),
        "floor":        (0.75, 0.70, 0.58, 0.0),   # pale stone
        "roof_pitch_deg":    18.0,
        "roof_style":        "hip",
        "window_height_m":   2.1,
        "has_columns":       True,
        "has_portico":       True,
        "description":       "Neoclassical with grand columned portico, symmetrical facade, tall proportions, stone-colored palette.",
    },

    "craftsman": {
        "ext_wall":     (0.48, 0.40, 0.30, 0.0),   # earthy brown
        "trim":         (0.85, 0.78, 0.62, 0.0),   # beige
        "accent":       (0.30, 0.22, 0.14, 0.0),   # dark wood
        "roof":         (0.25, 0.20, 0.16, 0.0),
        "floor":        (0.50, 0.36, 0.24, 0.0),   # quartersawn oak
        "roof_pitch_deg":     28.0,
        "roof_style":         "gable",
        "window_width_m":     0.9,
        "window_height_m":    1.3,
        "has_porch":          True,
        "has_columns":        True,           # tapered columns on stone piers
        "has_overhang_eaves": True,
        "description":        "Arts-and-Crafts bungalow with low gable, deep overhanging eaves, tapered porch columns, earth-tone siding.",
    },

    "farmhouse": {
        "ext_wall":     (0.95, 0.94, 0.90, 0.0),   # off-white
        "trim":         (0.20, 0.20, 0.22, 0.0),   # black trim (modern farmhouse)
        "accent":       (0.15, 0.15, 0.17, 0.0),
        "roof":         (0.28, 0.26, 0.25, 0.0),
        "floor":        (0.55, 0.42, 0.30, 0.0),   # reclaimed wood
        "roof_pitch_deg":     32.0,
        "roof_style":         "gable",
        "window_width_m":     0.95,
        "window_height_m":    1.5,
        "has_porch":          True,
        "has_wraparound_porch": True,
        "description":        "Modern farmhouse: white board-and-batten siding, black trim and roof, wraparound porch, black-framed windows.",
    },

    "ranch": {
        "ext_wall":     (0.82, 0.75, 0.58, 0.0),
        "trim":         (0.90, 0.85, 0.72, 0.0),
        "accent":       (0.50, 0.35, 0.22, 0.0),
        "roof":         (0.35, 0.30, 0.25, 0.0),
        "floor":        (0.65, 0.50, 0.38, 0.0),
        "roof_pitch_deg":     14.0,
        "roof_style":         "hip",
        "window_width_m":     1.5,
        "window_height_m":    1.1,             # wider, shorter
        "has_overhang_eaves": True,
        "description":        "Mid-century ranch with long low profile, shallow-pitch hip roof, wide horizontal windows.",
    },

    # ── Modern / Contemporary ────────────────────────────────────────────
    "modern": {
        "ext_wall":     (0.92, 0.92, 0.90, 0.0),   # white stucco
        "trim":         (0.18, 0.18, 0.20, 0.0),
        "accent":       (0.22, 0.22, 0.24, 0.0),
        "roof":         (0.20, 0.20, 0.22, 0.0),
        "floor":        (0.70, 0.65, 0.58, 0.0),   # light oak
        "window_frame": (0.18, 0.18, 0.20, 0.0),
        "roof_pitch_deg":     2.0,
        "roof_style":         "flat",
        "window_width_m":     1.8,
        "window_height_m":    2.0,             # floor-to-ceiling
        "window_sill_m":      0.3,
        "description":        "Modernist: flat roof, crisp white volumes, black-framed floor-to-ceiling glazing, minimal ornament.",
    },

    "contemporary": {
        "ext_wall":     (0.78, 0.76, 0.72, 0.0),
        "trim":         (0.30, 0.30, 0.32, 0.0),
        "accent":       (0.45, 0.35, 0.25, 0.0),   # warm wood accent
        "roof":         (0.22, 0.22, 0.24, 0.0),
        "floor":        (0.68, 0.60, 0.50, 0.0),
        "roof_pitch_deg":    4.0,
        "roof_style":        "flat",
        "window_width_m":    1.6,
        "window_height_m":   1.8,
        "window_sill_m":     0.4,
        "description":       "Contemporary: mixed materials (stucco + wood cladding), flat/low-slope roof, large windows, asymmetric massing.",
    },

    "mid_century_modern": {
        "ext_wall":     (0.72, 0.55, 0.38, 0.0),   # cedar tone
        "trim":         (0.85, 0.82, 0.75, 0.0),
        "accent":       (0.85, 0.45, 0.22, 0.0),   # burnt orange door
        "roof":         (0.28, 0.26, 0.26, 0.0),
        "floor":        (0.68, 0.50, 0.32, 0.0),
        "roof_pitch_deg":     8.0,
        "roof_style":         "flat",          # butterfly-ish also common
        "window_width_m":     2.0,
        "window_height_m":    1.4,
        "window_sill_m":      0.5,
        "has_overhang_eaves": True,
        "description":        "Mid-century modern: clerestory bands, deep eaves, wood + stone, burnt-orange accents, integration with landscape.",
    },

    "industrial": {
        "ext_wall":     (0.55, 0.42, 0.35, 0.0),   # exposed brick
        "trim":         (0.25, 0.25, 0.26, 0.0),   # steel
        "accent":       (0.20, 0.20, 0.22, 0.0),
        "roof":         (0.30, 0.30, 0.32, 0.0),
        "floor":        (0.55, 0.52, 0.48, 0.0),   # concrete
        "window_frame": (0.20, 0.20, 0.22, 0.0),
        "roof_pitch_deg":     2.0,
        "roof_style":         "flat",
        "window_width_m":     2.2,
        "window_height_m":    2.4,              # factory sash
        "window_sill_m":      0.6,
        "description":        "Industrial loft: exposed brick, black steel mullions, concrete floors, tall factory-sash windows.",
    },

    # ── Regional / Vernacular ────────────────────────────────────────────
    "mediterranean": {
        "ext_wall":     (0.95, 0.88, 0.72, 0.0),   # warm stucco
        "trim":         (0.85, 0.75, 0.55, 0.0),
        "accent":       (0.50, 0.25, 0.18, 0.0),
        "roof":         (0.72, 0.38, 0.22, 0.0),   # terracotta barrel tile
        "floor":        (0.82, 0.70, 0.55, 0.0),   # travertine
        "roof_pitch_deg":   22.0,
        "roof_style":       "hip",
        "window_width_m":   1.1,
        "window_height_m":  1.6,
        "has_columns":      True,
        "description":      "Mediterranean villa: warm stucco walls, terracotta barrel-tile roof, arched openings, travertine floors.",
    },

    "spanish_revival": {
        "ext_wall":     (0.96, 0.90, 0.78, 0.0),
        "trim":         (0.78, 0.62, 0.42, 0.0),
        "accent":       (0.35, 0.22, 0.15, 0.0),   # wrought iron / dark wood
        "roof":         (0.70, 0.35, 0.22, 0.0),
        "floor":        (0.72, 0.45, 0.32, 0.0),   # saltillo tile
        "roof_pitch_deg":  18.0,
        "roof_style":      "hip",
        "window_width_m": 1.0,
        "window_height_m":1.5,
        "description":    "Spanish Colonial Revival: whitewashed stucco, clay tile roof, dark wood beams, saltillo tile floors.",
    },

    "cape_cod": {
        "ext_wall":     (0.72, 0.72, 0.70, 0.0),   # weathered shingle
        "trim":         (0.94, 0.93, 0.90, 0.0),
        "accent":       (0.15, 0.22, 0.32, 0.0),
        "roof":         (0.30, 0.28, 0.28, 0.0),
        "floor":        (0.82, 0.72, 0.58, 0.0),
        "roof_pitch_deg":   35.0,
        "roof_style":       "gable",
        "window_width_m":   0.85,
        "window_height_m": 1.25,
        "has_dormer":       True,
        "has_shutters":     True,
        "description":      "Cape Cod: cedar shingle siding, steep gable with dormers, white trim, navy shutters.",
    },

    # ── Commercial / Utility ─────────────────────────────────────────────
    "commercial_office": {
        "ext_wall":     (0.55, 0.60, 0.65, 0.0),   # glass/metal panel
        "trim":         (0.30, 0.32, 0.34, 0.0),
        "accent":       (0.25, 0.45, 0.65, 0.0),
        "roof":         (0.28, 0.28, 0.30, 0.0),
        "floor":        (0.60, 0.60, 0.58, 0.0),   # commercial carpet
        "roof_pitch_deg":     1.0,
        "roof_style":         "flat",
        "window_width_m":     2.4,
        "window_height_m":    2.7,               # full-height curtain wall panel
        "window_sill_m":      0.0,
        "description":        "Commercial curtain wall: metal-panel facade with full-height glazing, flat roof, minimal exterior detail.",
    },

    "warehouse": {
        "ext_wall":     (0.58, 0.56, 0.52, 0.0),   # metal siding
        "trim":         (0.30, 0.30, 0.32, 0.0),
        "accent":       (0.55, 0.50, 0.45, 0.0),
        "roof":         (0.32, 0.32, 0.34, 0.0),
        "floor":        (0.55, 0.55, 0.55, 0.0),   # polished concrete
        "roof_pitch_deg":   4.0,
        "roof_style":       "flat",
        "window_width_m":   1.2,
        "window_height_m":  0.8,
        "window_sill_m":    3.5,                 # clerestory only
        "description":      "Industrial warehouse: ribbed metal siding, low-slope roof, clerestory strip windows.",
    },
}


# Aliases so Claude can throw in almost any phrasing and we still resolve.
_ALIASES: Dict[str, str] = {
    "queen_anne":          "victorian",
    "queen anne":          "victorian",
    "gothic":              "victorian",
    "gingerbread":         "victorian",
    "english":             "tudor",
    "storybook":           "tudor",
    "cottage":             "tudor",
    "georgian":            "colonial",
    "federal":             "colonial",
    "saltbox":             "colonial",
    "greek_revival":       "neoclassical",
    "greek revival":       "neoclassical",
    "classical":           "neoclassical",
    "bungalow":            "craftsman",
    "arts_and_crafts":     "craftsman",
    "arts and crafts":     "craftsman",
    "modern_farmhouse":    "farmhouse",
    "modern farmhouse":    "farmhouse",
    "rambler":             "ranch",
    "midcentury":          "mid_century_modern",
    "mid-century":         "mid_century_modern",
    "mid century modern":  "mid_century_modern",
    "mcm":                 "mid_century_modern",
    "loft":                "industrial",
    "warehouse_conversion":"industrial",
    "tuscan":              "mediterranean",
    "italian":             "mediterranean",
    "villa":               "mediterranean",
    "spanish":             "spanish_revival",
    "mission":             "spanish_revival",
    "adobe":               "spanish_revival",
    "shingle":             "cape_cod",
    "new_england":         "cape_cod",
    "commercial":          "commercial_office",
    "office":              "commercial_office",
    "glass_box":           "commercial_office",
    "industrial_warehouse":"warehouse",
}


def resolve_style(name: str) -> str:
    """Normalize a free-form style string to a key in STYLES."""
    if not name:
        return ""
    k = name.strip().lower().replace("-", "_").replace("  ", " ")
    if k in STYLES:
        return k
    k2 = k.replace(" ", "_")
    if k2 in STYLES:
        return k2
    if k in _ALIASES:
        return _ALIASES[k]
    if k2 in _ALIASES:
        return _ALIASES[k2]
    # fuzzy: any style key whose first word appears in the input
    for key in STYLES:
        root = key.split("_")[0]
        if root and root in k:
            return key
    return ""


def get_style(name: str) -> Dict[str, Any]:
    """Return the full merged style dict. Unknown names return the base style."""
    merged = dict(_BASE_STYLE)
    resolved = resolve_style(name)
    if resolved:
        merged.update(STYLES[resolved])
        merged["_resolved_key"] = resolved
    else:
        merged["_resolved_key"] = ""
    return merged


def palette_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Tuple[float, float, float, float]]:
    """
    Build a palette (ext_wall, int_wall, floor, roof, trim, accent, window_glass, ...)
    from metadata. Priority:
    1. metadata["style_palette"] if the AI provided explicit hex colors
    2. STYLES[architectural_style]
    3. _BASE_STYLE fallback
    """
    style = get_style(metadata.get("architectural_style", ""))
    palette: Dict[str, Tuple[float, float, float, float]] = {
        k: v for k, v in style.items()
        if isinstance(v, tuple) and len(v) == 4
    }

    # Override with explicit hex colors from the AI if present.
    sp = metadata.get("style_palette") or {}
    if isinstance(sp, dict):
        for key, hexval in sp.items():
            rgba = _hex_to_rgba(hexval)
            if rgba is not None:
                palette[key] = rgba

    return palette


def _hex_to_rgba(hex_str: Any) -> Any:
    """Accept '#RRGGBB' or '#RRGGBBAA'. Return (r, g, b, transparency) in 0-1."""
    if not isinstance(hex_str, str):
        return None
    s = hex_str.strip().lstrip("#")
    try:
        if len(s) == 6:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            return (r, g, b, 0.0)
        if len(s) == 8:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            a = int(s[6:8], 16) / 255.0
            # our tuple is (r,g,b, TRANSPARENCY), not alpha — invert
            return (r, g, b, 1.0 - a)
    except ValueError:
        return None
    return None


def list_style_keys() -> list:
    """For the system prompt — show the AI what styles are available."""
    return sorted(STYLES.keys())
