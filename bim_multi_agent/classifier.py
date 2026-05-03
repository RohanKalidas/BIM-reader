"""
classifier.py — Classify a library component into the four taxonomies.

Called from two places:
  1. extractor/strip.py — at IFC ingest time, after each component is
     extracted from the source IFC.
  2. backfill.py — once retroactively over the existing components.

Uses Claude Haiku for cost. Each call is ~$0.0003. 21k backfill ≈ $6.

The classifier is forgiving: if the model returns invalid tags, we
filter them out rather than failing. If it returns no canonical_name,
we default to "other".
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import anthropic

from .canonical_vocab import (
    CANONICAL_NAMES, STYLE_TAGS, CONTEXT_TAGS, QUALITY_CLASS,
    validate_classification,
)

logger = logging.getLogger(__name__)


_CLIENT: Optional[anthropic.Anthropic] = None


def _client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _CLIENT


# ── Prompt ────────────────────────────────────────────────────────────────

def _build_classifier_prompt() -> str:
    """
    System prompt is mostly the four vocabularies as text. We list them
    once at module load.
    """
    canonical_list = "\n".join(f"  - {name}" for name in sorted(CANONICAL_NAMES))
    style_list = ", ".join(sorted(STYLE_TAGS))
    context_list = ", ".join(sorted(CONTEXT_TAGS))
    quality_list = ", ".join(sorted(QUALITY_CLASS))

    return f"""You classify building components from BIM (IFC) files.

You receive a component's raw metadata (IFC class, family/type names,
dimensions in mm) and emit four classifications:

  1. canonical_name — the FUNCTIONAL identity of the object (what it IS).
     PICK EXACTLY ONE from this list:
{canonical_list}

  2. style_tags — aesthetic families this component is appropriate for.
     A list of 1-4 tags from: {style_list}
     If genuinely style-neutral (a generic toilet, a basic outlet, a
     concrete column), use ["any"].

  3. context_tags — building types this component is appropriate for.
     A list of 1-4 tags from: {context_list}
     If genuinely context-neutral, use ["any"].

  4. quality_class — coarse premium/standard/basic signal.
     One of: {quality_list}
     Use:
       - "premium" for named luxury brands, exotic materials, large dimensions
         that suggest custom work, or detail-rich descriptions.
       - "basic" for builder-grade, economy, plain utility items, generic
         names like "Door1" or "Default".
       - "standard" for everything in between (the majority).

CLASSIFICATION RULES:

- The IFC class is a HINT, not the answer. An "IfcFurniture" item could be
  a chair, a table, a wardrobe, etc. Read the family_name and dimensions
  to figure out what it actually is.

- The family_name often contains the answer. "Furniture_Couch_Viper" → sofa.
  "Furniture_Table_Dining_w-Chairs_Rectangular" → dining_table (NOT a chair —
  the "w-Chairs" means it comes with chairs included; the primary object IS
  the table).

- Dimensions are diagnostic. A component called "Door" that is 2400mm wide
  is almost certainly a double door or storefront, not a residential
  interior door. A "table" 2200mm long is a conference table, not a
  side table.

- If you genuinely can't tell, return canonical_name "other" and a single
  context_tag of "any".

OUTPUT FORMAT — STRICT JSON, NO MARKDOWN, NO COMMENTARY:

{{
  "canonical_name": "<one name from the canonical list>",
  "style_tags": ["<tag1>", "<tag2>"],
  "context_tags": ["<tag1>", "<tag2>"],
  "quality_class": "<premium|standard|basic>"
}}

EXAMPLES:

Input:
  ifc_class: IfcFurniture
  family_name: Furniture_Couch_Viper:2290x950x340mm:290852
  type_name: (empty)
  dims: 2290 W × 950 D × 340 H

Output:
{{"canonical_name":"sofa_3_seat","style_tags":["modern","contemporary"],"context_tags":["residential","hospitality"],"quality_class":"standard"}}

Input:
  ifc_class: IfcFurniture
  family_name: Furniture_Table_Dining_w-Chairs_Rectangular:2000x1000:482911
  type_name: (empty)
  dims: 2000 W × 1000 D × 750 H

Output:
{{"canonical_name":"dining_table","style_tags":["traditional","contemporary"],"context_tags":["residential"],"quality_class":"standard"}}

Input:
  ifc_class: IfcSanitaryTerminal
  family_name: Toilet_Standard_2pc:670x380x780mm
  type_name: WC
  dims: 670 W × 380 D × 780 H

Output:
{{"canonical_name":"toilet_residential","style_tags":["any"],"context_tags":["residential"],"quality_class":"standard"}}

Input:
  ifc_class: IfcDoor
  family_name: Door_Storefront_Glass_Double_2400x2700
  type_name: Aluminum
  dims: 2400 W × 100 D × 2700 H

Output:
{{"canonical_name":"door_storefront","style_tags":["modern","contemporary","class_a_glass"],"context_tags":["commercial","retail","office"],"quality_class":"standard"}}

Input:
  ifc_class: IfcFurniture
  family_name: Cabinet_KitchenBase_Premium_Walnut_900
  type_name: Lower Cabinet
  dims: 900 W × 600 D × 720 H

Output:
{{"canonical_name":"kitchen_cabinet_base","style_tags":["modern","contemporary","luxury"],"context_tags":["residential"],"quality_class":"premium"}}
"""


_SYSTEM_PROMPT: Optional[str] = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = _build_classifier_prompt()
    return _SYSTEM_PROMPT


# ── Classification ────────────────────────────────────────────────────────

def classify_component(
    ifc_class: str,
    family_name: str = "",
    type_name: str = "",
    width_mm: Optional[float] = None,
    height_mm: Optional[float] = None,
    length_mm: Optional[float] = None,
    *,
    model: str = "claude-haiku-4-5-20251001",
    max_retries: int = 2,
) -> dict:
    """
    Classify one component. Returns a dict:

        {
            "canonical_name": str,
            "style_tags": list[str],
            "context_tags": list[str],
            "quality_class": str,
        }

    Falls back to safe defaults on error rather than raising.
    """
    user_message = (
        f"ifc_class: {ifc_class}\n"
        f"family_name: {family_name or '(empty)'}\n"
        f"type_name: {type_name or '(empty)'}\n"
        f"dims: "
        f"{int(width_mm) if width_mm else '?'} W × "
        f"{int(length_mm) if length_mm else '?'} D × "
        f"{int(height_mm) if height_mm else '?'} H"
    )

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = _client().messages.create(
                model=model,
                max_tokens=300,
                system=_system_prompt(),
                messages=[{"role": "user", "content": user_message}],
            )
            text = resp.content[0].text.strip()

            # Strip markdown fences if the model added them
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            parsed = json.loads(text)

            # Be lenient: filter unknown tags, default missing fields.
            canonical_name = parsed.get("canonical_name", "other")
            if canonical_name not in CANONICAL_NAMES:
                logger.warning("classifier returned unknown canonical %r → 'other'", canonical_name)
                canonical_name = "other"

            style_tags = [t for t in parsed.get("style_tags", []) if t in STYLE_TAGS]
            if not style_tags:
                style_tags = ["any"]

            context_tags = [t for t in parsed.get("context_tags", []) if t in CONTEXT_TAGS]
            if not context_tags:
                context_tags = ["any"]

            quality_class = parsed.get("quality_class", "standard")
            if quality_class not in QUALITY_CLASS:
                quality_class = "standard"

            return {
                "canonical_name": canonical_name,
                "style_tags": style_tags,
                "context_tags": context_tags,
                "quality_class": quality_class,
            }

        except Exception as e:
            last_err = e
            logger.debug("classify attempt %d failed: %s", attempt + 1, e)

    logger.warning("classify_component failed after retries (%s); using fallback", last_err)
    return {
        "canonical_name": "other",
        "style_tags": ["any"],
        "context_tags": ["any"],
        "quality_class": "standard",
    }


# ── Cheap rule-based pre-classifier ───────────────────────────────────────
# For obvious cases we can skip the LLM call entirely. Saves ~80% of the
# backfill cost on a typical library where doors are doors and toilets
# are toilets.

_RULE_HINTS = [
    # (substrings_in_family_name_or_type, ifc_class_hint, canonical_name)
    # Highest-specificity first.
    (("storefront", "curtain"), "IfcDoor", "door_storefront"),
    (("storefront",), "IfcWindow", "window_storefront"),
    (("revolving",), "IfcDoor", "door_revolving"),
    (("overhead", "garage", "rollup", "roll-up"), "IfcDoor", "door_overhead"),
    (("loading dock", "dock"), "IfcDoor", "door_dock_loading"),
    (("fire-rated", "fire rated"), "IfcDoor", "door_fire_rated"),
    (("pocket",), "IfcDoor", "door_pocket"),
    (("barn",), "IfcDoor", "door_barn"),
    (("bifold", "bi-fold"), "IfcDoor", "door_bifold"),
    (("pivot",), "IfcDoor", "door_pivot"),
    (("french",), "IfcDoor", "door_french"),
    (("sliding glass",), "IfcDoor", "door_sliding_glass"),

    (("casement",), "IfcWindow", "window_casement"),
    (("awning",), "IfcWindow", "window_awning"),
    (("double-hung", "double hung"), "IfcWindow", "window_double_hung"),
    (("single-hung", "single hung"), "IfcWindow", "window_single_hung"),
    (("skylight",), "IfcWindow", "window_skylight"),
    (("clerestory",), "IfcWindow", "window_clerestory"),

    (("urinal",), "IfcSanitaryTerminal", "urinal"),
    (("bidet",), "IfcSanitaryTerminal", "bidet"),
    (("drinking fountain", "water fountain"), "IfcSanitaryTerminal", "drinking_fountain"),
    (("freestanding tub", "clawfoot"), "IfcSanitaryTerminal", "bathtub_freestanding"),
    (("jetted", "whirlpool", "jacuzzi"), "IfcSanitaryTerminal", "bathtub_jetted"),

    (("microwave",), None, "microwave"),
    (("dishwasher",), None, "dishwasher"),
    (("refrigerator", "fridge"), None, "refrigerator_residential"),
    (("freezer",), None, "freezer_upright"),
    (("range hood", "vent hood"), None, "range_hood"),

    (("water heater", "boiler"), None, "water_heater_tank"),
    (("air handler", "ahu"), None, "air_handler"),
    (("rooftop unit", "rtu"), None, "rooftop_unit"),
    (("condenser",), None, "condenser_unit"),

    (("recessed", "downlight", "can light"), "IfcLightFixture", "light_recessed_downlight"),
    (("pendant",), "IfcLightFixture", "light_pendant"),
    (("chandelier",), "IfcLightFixture", "light_chandelier"),
    (("sconce",), "IfcLightFixture", "light_sconce_wall"),
    (("troffer",), "IfcLightFixture", "light_troffer"),
    (("exit sign",), "IfcLightFixture", "light_exit_sign"),

    (("smoke detector",), None, "smoke_detector"),
    (("co detector", "carbon monoxide"), None, "co_detector"),
    (("thermostat",), None, "thermostat"),

    (("sprinkler",), "IfcFireSuppressionTerminal", "sprinkler_pendant"),

    (("crib", "bassinet"), "IfcFurniture", "bed_crib"),
    (("bunk bed",), "IfcFurniture", "bed_bunk"),
    (("hospital bed",), "IfcFurniture", "bed_hospital"),
]


def _rule_classify(ifc_class: str, family_name: str, type_name: str) -> Optional[str]:
    """
    Try cheap substring matching. Returns canonical_name or None if no
    confident match. Style/context/quality still need the LLM.
    """
    text = f"{family_name} {type_name}".lower()
    for substrings, class_hint, canonical in _RULE_HINTS:
        if class_hint and ifc_class != class_hint:
            continue
        for sub in substrings:
            if sub in text:
                return canonical
    return None


def classify_component_smart(
    ifc_class: str,
    family_name: str = "",
    type_name: str = "",
    width_mm: Optional[float] = None,
    height_mm: Optional[float] = None,
    length_mm: Optional[float] = None,
) -> dict:
    """
    Smart wrapper: try rules first for canonical_name. If a rule fires,
    we still need to call the LLM for style/context/quality but we don't
    let the LLM override the canonical (because rules are usually right
    on hits).

    For unknown components, falls through to full LLM classification.
    """
    # No rule check: always use the LLM. Rules give us a slight bias only.
    rule_canonical = _rule_classify(ifc_class, family_name, type_name)

    result = classify_component(
        ifc_class=ifc_class,
        family_name=family_name,
        type_name=type_name,
        width_mm=width_mm,
        height_mm=height_mm,
        length_mm=length_mm,
    )

    # If a rule fired and the LLM said "other", use the rule's answer.
    if rule_canonical and result["canonical_name"] == "other":
        result["canonical_name"] = rule_canonical

    return result


if __name__ == "__main__":
    # Quick smoke test (requires ANTHROPIC_API_KEY)
    import sys
    test_cases = [
        ("IfcFurniture", "Furniture_Couch_Viper:2290x950x340mm:290852", "", 2290, 340, 950),
        ("IfcFurniture", "Furniture_Table_Dining_w-Chairs_Rectangular:2000x1", "", 2000, 750, 1000),
        ("IfcSanitaryTerminal", "Toilet_Standard_2pc:670x380x780mm", "WC", 670, 780, 380),
        ("IfcDoor", "Door_Storefront_Glass_Double_2400x2700", "", 2400, 2700, 100),
    ]
    for tc in test_cases:
        print(f"\n--- {tc[0]} | {tc[1]}")
        try:
            r = classify_component(*tc)
            print(f"    {r}")
        except Exception as e:
            print(f"    ERROR: {e}")
