"""
smart_matcher.py — Find the best library component for a fixture request,
using canonical_name + style + context + quality.

The matcher is called by generate.py during fixture placement. It
replaces the old fuzzy-name matching (which still exists as a fallback
for components that haven't been classified yet).

Filter cascade:
    1. canonical_name MUST match (strict, no relaxation)
    2. style_tags overlap with building style — relax if no results
    3. context_tags overlap with building type — relax if no results
    4. dimensional fit — soft sort, never excludes
    5. quality_class — sort key (premium > standard > basic)

If canonical_name is unrecognized OR no rows match it, falls back to the
legacy fuzzy matcher that uses family_name strings.
"""
from __future__ import annotations

import logging
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


# ── Style → tag set mapping ───────────────────────────────────────────────
# Maps the brief's free-form architectural_style string to a set of
# style_tags from STYLE_TAGS that should be considered. The mapping is
# fuzzy by design — "queen anne victorian" matches both "victorian" and
# "italianate" because they're sympathetic styles.

_STYLE_KEYWORD_MAP = {
    # period words → tag set
    "victorian":         {"victorian", "italianate", "traditional"},
    "queen anne":        {"victorian", "traditional"},
    "italianate":        {"italianate", "victorian", "traditional"},
    "georgian":          {"georgian", "colonial", "traditional"},
    "colonial":          {"colonial", "federal", "georgian", "traditional"},
    "federal":           {"federal", "colonial", "neoclassical", "traditional"},
    "neoclassical":      {"federal", "georgian", "traditional"},
    "art deco":          {"art_deco", "art_nouveau"},
    "art nouveau":       {"art_nouveau", "art_deco"},
    "tudor":             {"tudor_revival", "traditional"},
    "spanish":           {"spanish_revival", "mediterranean"},
    "mediterranean":     {"mediterranean", "spanish_revival"},
    "craftsman":         {"craftsman", "traditional"},
    "bungalow":          {"craftsman", "cottage", "traditional"},

    "modern":            {"modern", "contemporary", "minimalist"},
    "contemporary":      {"contemporary", "modern", "transitional"},
    "minimalist":        {"minimalist", "modern", "contemporary"},
    "international":     {"international", "modern", "minimalist"},
    "miesian":           {"international", "modern", "minimalist"},
    "mid-century":       {"mid_century_modern", "modern"},
    "mid century":       {"mid_century_modern", "modern"},
    "industrial":        {"industrial", "modern", "utilitarian"},
    "scandinavian":      {"scandinavian", "minimalist", "modern"},
    "japandi":           {"japandi", "japanese", "scandinavian", "minimalist"},
    "japanese":          {"japanese", "japandi", "minimalist"},
    "shou sugi ban":     {"shou_sugi_ban", "japanese", "japandi"},

    "class a":           {"class_a_glass", "modern", "contemporary"},
    "glass curtain":     {"class_a_glass", "modern", "contemporary"},
    "glass tower":       {"class_a_glass", "modern", "contemporary"},
    "corporate":         {"class_a_glass", "modern", "contemporary"},
    "postmodern":        {"postmodern", "contemporary"},
    "brutalist":         {"brutalist", "utilitarian"},
    "high-tech":         {"high_tech_expressionist", "industrial", "modern"},

    "farmhouse":         {"farmhouse", "rustic", "cottage", "traditional"},
    "rustic":            {"rustic", "farmhouse", "cottage"},
    "cottage":           {"cottage", "traditional", "farmhouse"},
    "coastal":           {"coastal", "cottage", "contemporary"},
    "ranch":             {"ranch", "mid_century_modern", "traditional"},
    "cape cod":          {"cape_cod", "colonial", "traditional"},

    "transitional":      {"transitional", "contemporary", "traditional"},
    "luxury":            {"luxury", "transitional", "traditional"},
    "traditional":       {"traditional", "transitional"},

    "warehouse":         {"utilitarian", "industrial"},
    "manufacturing":     {"utilitarian", "industrial"},
    "civic":             {"institutional", "brutalist", "traditional"},
    "library":           {"institutional", "modern", "contemporary"},
    "school":            {"institutional", "contemporary"},
    "hospital":          {"institutional", "contemporary"},
    "clinic":            {"institutional", "modern", "contemporary"},
}


def style_to_tags(architectural_style: str) -> list[str]:
    """
    Convert the brief's free-form style string into a list of style_tags
    that components matching this style might be tagged with.

    Always includes "any" so that style-neutral components (a generic
    toilet) can still match.
    """
    if not architectural_style:
        return ["any"]

    s = architectural_style.lower()
    tags = {"any"}  # always allow style-neutral

    for keyword, mapped in _STYLE_KEYWORD_MAP.items():
        if keyword in s:
            tags.update(mapped)

    # If we found nothing concrete, fall back to "contemporary" — most
    # libraries skew modern, so that's the safest default.
    if tags == {"any"}:
        tags.update({"contemporary", "modern"})

    return list(tags)


# ── Building type → context tag mapping ───────────────────────────────────

_TYPE_KEYWORD_MAP = {
    "house":         {"residential"},
    "home":          {"residential"},
    "cottage":       {"residential"},
    "apartment":     {"residential"},
    "condo":         {"residential"},
    "duplex":        {"residential"},
    "townhouse":     {"residential"},
    "office":        {"office", "commercial"},
    "retail":        {"retail", "commercial"},
    "store":         {"retail", "commercial"},
    "shop":          {"retail", "commercial"},
    "restaurant":    {"restaurant", "hospitality"},
    "cafe":          {"restaurant", "hospitality"},
    "bar":           {"restaurant", "hospitality"},
    "hotel":         {"hotel", "hospitality"},
    "motel":         {"hotel", "hospitality"},
    "school":        {"education"},
    "university":    {"education"},
    "college":       {"education"},
    "library":       {"civic", "education"},
    "hospital":      {"hospital", "healthcare"},
    "clinic":        {"healthcare"},
    "warehouse":     {"industrial"},
    "factory":       {"industrial"},
    "manufacturing": {"industrial"},
    "gym":           {"sports"},
    "fitness":       {"sports"},
    "stadium":       {"sports"},
    "arena":         {"sports"},
    "church":        {"religious"},
    "temple":        {"religious"},
    "mosque":        {"religious"},
    "courthouse":    {"courthouse", "civic"},
    "civic":         {"civic"},
    "government":    {"civic"},
}


def building_type_to_context_tags(building_type: str,
                                   architectural_style: str = "") -> list[str]:
    """
    Convert a building_type string (or fall back to architectural_style)
    into context_tags. Always includes 'any'.
    """
    tags = {"any"}
    combined = f"{building_type} {architectural_style}".lower()
    for keyword, mapped in _TYPE_KEYWORD_MAP.items():
        if keyword in combined:
            tags.update(mapped)

    if tags == {"any"}:
        # Default to residential, the most common case.
        tags.update({"residential", "commercial"})

    return list(tags)


# ── Quality preference per style ──────────────────────────────────────────

_PREMIUM_STYLE_KEYWORDS = {
    "luxury", "class a", "premium", "high-end", "boutique", "executive",
    "art deco", "victorian", "italianate", "queen anne",
}

def quality_priority(architectural_style: str) -> list[str]:
    """
    Sort order for quality_class. Premium first if the style suggests it,
    otherwise standard first.
    """
    s = (architectural_style or "").lower()
    if any(kw in s for kw in _PREMIUM_STYLE_KEYWORDS):
        return ["premium", "standard", "basic"]
    return ["standard", "premium", "basic"]


# ── The matcher ──────────────────────────────────────────────────────────

def find_best_component(
    cursor,
    *,
    canonical_name: str,
    architectural_style: str = "",
    building_type: str = "",
    target_width_mm: Optional[float] = None,
    target_height_mm: Optional[float] = None,
    target_length_mm: Optional[float] = None,
    library_only: bool = True,
) -> Optional[dict]:
    """
    Find the single best component for a fixture request.

    Args:
        cursor: an open psycopg2 RealDictCursor.
        canonical_name: required. The functional identity of what we want.
        architectural_style: from spec.metadata.architectural_style.
        building_type: from spec.metadata.building_type. Falls back to style.
        target_*_mm: dimensional preferences (None = any).
        library_only: if True, restrict to saved library (vs all uploaded).

    Returns:
        dict with the matched component's fields, or None if no match.
    """
    style_tag_set = style_to_tags(architectural_style)
    context_tag_set = building_type_to_context_tags(building_type, architectural_style)
    quality_order = quality_priority(architectural_style)

    base_select = """
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm,
               c.canonical_name, c.style_tags, c.context_tags, c.quality_class
        FROM components c
    """
    base_join = " JOIN library l ON l.component_id = c.id " if library_only else ""

    # Tier 1: full strict match (canonical + style overlap + context overlap)
    rows = _query(cursor, base_select + base_join + """
        WHERE c.canonical_name = %s
          AND c.style_tags && %s::text[]
          AND c.context_tags && %s::text[]
    """, (canonical_name, style_tag_set, context_tag_set))

    if rows:
        return _pick_best(rows, target_width_mm, target_height_mm,
                          target_length_mm, quality_order)

    # Tier 2: relax context (keep style)
    logger.debug("matcher: relaxing context_tags filter for %s", canonical_name)
    rows = _query(cursor, base_select + base_join + """
        WHERE c.canonical_name = %s
          AND c.style_tags && %s::text[]
    """, (canonical_name, style_tag_set))

    if rows:
        return _pick_best(rows, target_width_mm, target_height_mm,
                          target_length_mm, quality_order)

    # Tier 3: relax style (keep context)
    logger.debug("matcher: relaxing style_tags filter for %s", canonical_name)
    rows = _query(cursor, base_select + base_join + """
        WHERE c.canonical_name = %s
          AND c.context_tags && %s::text[]
    """, (canonical_name, context_tag_set))

    if rows:
        return _pick_best(rows, target_width_mm, target_height_mm,
                          target_length_mm, quality_order)

    # Tier 4: just canonical_name
    logger.debug("matcher: only canonical_name for %s", canonical_name)
    rows = _query(cursor, base_select + base_join + """
        WHERE c.canonical_name = %s
    """, (canonical_name,))

    if rows:
        return _pick_best(rows, target_width_mm, target_height_mm,
                          target_length_mm, quality_order)

    # Tier 5: nothing classified for this canonical_name. Caller should
    # fall back to legacy fuzzy matching on family_name.
    logger.debug("matcher: no rows with canonical_name=%s; falling through", canonical_name)
    return None


def _query(cursor, sql, params):
    cursor.execute(sql, params)
    return [dict(r) for r in cursor.fetchall()]


def _pick_best(candidates, target_w, target_h, target_l, quality_order):
    """
    Score each candidate and return the highest scorer.

    Score:
      + Quality bonus based on the architectural style preference order
      - Dimensional distance (squared, normalized) when targets given
    """
    quality_bonus = {q: 100 - 10 * i for i, q in enumerate(quality_order)}

    best = None
    best_score = float("-inf")

    for c in candidates:
        score = 0.0

        score += quality_bonus.get(c.get("quality_class", "standard"), 50)

        if target_w and c.get("width_mm"):
            score -= abs(c["width_mm"] - target_w) / max(target_w, 1) * 30
        if target_h and c.get("height_mm"):
            score -= abs(c["height_mm"] - target_h) / max(target_h, 1) * 30
        if target_l and c.get("length_mm"):
            score -= abs(c["length_mm"] - target_l) / max(target_l, 1) * 30

        if score > best_score:
            best_score = score
            best = c

    return best


# ── Adapter for generate.py's fixture loop ────────────────────────────────
# The existing matcher in extractor/geometry_transplant.py is called
# via geom_lib.find_component(name, category, ...). This is the new
# replacement that geometry_transplant should call. Intended drop-in
# replacement for the find_component method.

def find_component_v2(
    cursor,
    fixture_request_name: str,    # legacy name like "Sofa", "Toilet"
    canonical_name_hint: Optional[str],   # if generate.py passes one
    category: str,                # IFC class hint
    *,
    architectural_style: str = "",
    building_type: str = "",
    target_width_mm: Optional[float] = None,
    target_height_mm: Optional[float] = None,
    target_length_mm: Optional[float] = None,
) -> Optional[dict]:
    """
    Drop-in replacement for the legacy find_component matcher.

    - If canonical_name_hint is given, do a smart match.
    - If not, try to map fixture_request_name to a canonical via a quick
      lookup table; fall through to legacy fuzzy match if unknown.
    """
    canonical = canonical_name_hint or _legacy_name_to_canonical(fixture_request_name, category)

    if canonical:
        result = find_best_component(
            cursor,
            canonical_name=canonical,
            architectural_style=architectural_style,
            building_type=building_type,
            target_width_mm=target_width_mm,
            target_height_mm=target_height_mm,
            target_length_mm=target_length_mm,
        )
        if result:
            return result

    # Fallback: legacy fuzzy matching path. Caller handles this.
    return None


# Map the spec generator's existing fixture names to canonical names.
# These are the names hardcoded in generate.py's FIXTURES dict.
_LEGACY_FIXTURE_TO_CANONICAL = {
    # Living
    "Sofa": "sofa_3_seat",
    "Coffee Table": "coffee_table",
    "TV Unit": "tv_unit",
    # Bedroom
    "Bed": "bed_queen",
    "Wardrobe": "wardrobe",
    "Nightstand": "nightstand",
    # Office
    "Desk": "office_desk",
    "Chair": "office_chair",
    # Conference / dining
    "Table": "dining_table",
    "Dining Table": "dining_table",
    # Kitchen
    "Counter": "kitchen_cabinet_base",
    "Stove": "range_residential",
    "Fridge": "refrigerator_residential",
    "Refrigerator": "refrigerator_residential",
    # Bathroom
    "Toilet": "toilet_residential",
    "Sink": "sink_lavatory",
    "Shower": "shower_stall",
    "Shower Tray": "shower_tray",
    # Utility
    "Heater": "water_heater_tank",
    "Washer": "washing_machine",
    # Other
    "Rack": "lockers",   # closest available; better fallback than nothing
}


def _legacy_name_to_canonical(name: str, category: str) -> Optional[str]:
    """Map generate.py's FIXTURES dict names to canonical names."""
    if not name:
        return None
    return _LEGACY_FIXTURE_TO_CANONICAL.get(name)
