"""
orchestrator.py — The pipeline that runs the 4 agents and merges their output.

Public API:

    from orchestrator import generate_building_multi_agent

    result = generate_building_multi_agent("Victorian cottage in New England")
    # result.spec is the building_spec dict ready for generate.py
    # result.brief / layout / facade / mep are the intermediate agent outputs
    # result.runs has timing + token counts for each agent call

Flow:
    1. BriefAgent (Sonnet) — user_prompt → Brief
    2. LayoutAgent (Haiku) — Brief → Layout
    3. Parallel:
       - FacadeAgent (Haiku) — Brief + Layout → Facade
       - MEPAgent (Haiku)    — Brief + Layout → MEPStrategy
    4. merge_to_spec() — all four → BuildingSpec (shape generate.py expects)

The edit() API (edit_building) re-runs ONE specialist with the user's change
request and preserves the other three. This is the killer feature that
monolithic generation can't do.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any, Dict, Optional

from .agents import run_brief_agent, run_facade_agent, run_layout_agent, run_mep_agent
from .schemas import (
    Brief,
    BuildingSpec,
    Facade,
    Layout,
    MEPStrategy,
    PipelineResult,
)

logger = logging.getLogger(__name__)


# ── Palette hint parser ────────────────────────────────────────────────────
# Fast path for "change the color" edits — no LLM needed. Covers the common
# vocabulary for color names + material keywords. Falls through silently for
# anything it doesn't recognize (caller should use 'materials' target instead).

_NAMED_COLORS = {
    # Neutrals
    "white":        "#F5F5F5",
    "cream":        "#F0E8D6",
    "beige":        "#E8DCC4",
    "tan":          "#D2B48C",
    "gray":         "#808080",
    "grey":         "#808080",
    "dark gray":    "#3A3A3A",
    "dark grey":    "#3A3A3A",
    "light gray":   "#C0C0C0",
    "light grey":   "#C0C0C0",
    "black":        "#1A1A1A",
    "charcoal":     "#36454F",
    # Warms
    "red":          "#B22222",
    "brick":        "#8B3A2F",
    "brick red":    "#8B3A2F",
    "red brick":    "#8B3A2F",
    "rust":         "#8B4513",
    "orange":       "#D35400",
    "terracotta":   "#A0522D",
    "brown":        "#5C4033",
    "dark brown":   "#3D2817",
    # Cools
    "blue":         "#2C5282",
    "navy":         "#1A2E4C",
    "dark blue":    "#1A2E4C",
    "light blue":   "#A8C6DF",
    "teal":         "#2C7A7B",
    # Greens
    "green":        "#3A5F3A",
    "dark green":   "#1F3529",
    "forest green": "#1F3529",
    "sage":         "#87A96B",
    "olive":        "#6B7B3A",
    # Woods / naturals
    "wood":         "#8B6F47",
    "natural wood": "#A78860",
    "oak":          "#A17C52",
    "walnut":       "#5C4033",
    # Materials (maps to ext_wall color conventions)
    "clapboard":    "#E8DCC4",   # typical cream/beige clapboard
    "stone":        "#8A8578",
    "stucco":       "#D8CFBE",
    "siding":       "#C8BFA3",
}

# Which palette key each material/color implies. If the user says "red brick"
# with no qualifier, assume they mean the ext_wall. Trim/accent need explicit
# mention ("white trim", "black trim").
_KEY_HINTS = [
    ("ext_wall", [
        "wall", "exterior", "ext", "siding", "cladding", "clapboard",
        "brick", "stone", "stucco", "facade", "face",
    ]),
    ("trim", [
        "trim", "molding", "fascia", "frame",
    ]),
    ("roof", [
        "roof", "shingle", "tile",
    ]),
    ("accent", [
        "accent", "door", "shutter", "feature",
    ]),
    ("window_glass", [
        "window", "glass", "glazing",
    ]),
]


def _apply_palette_hints(palette: dict, edit_request: str) -> dict:
    """
    Parse a natural-language palette edit into a dict update.

    Examples:
      "Change to red brick" → {"ext_wall": "#8B3A2F"}
      "White trim, dark green walls" → {"trim": "#F5F5F5", "ext_wall": "#1F3529"}
      "Make the roof charcoal" → {"roof": "#36454F"}

    Returns the updated palette. Keys not mentioned in the edit are preserved.
    """
    import re
    req = edit_request.lower()

    # Find all (color_name, position) pairs mentioned in the request,
    # longest-match-first so "dark gray" beats "gray".
    matches = []
    for color_name in sorted(_NAMED_COLORS, key=len, reverse=True):
        for m in re.finditer(r'\b' + re.escape(color_name) + r'\b', req):
            matches.append((m.start(), color_name, _NAMED_COLORS[color_name]))

    if not matches:
        logger.warning("palette hint parser: no colors recognized in %r", edit_request)
        return palette

    # Dedupe overlapping matches — keep the longer ones. "dark green" at pos 12
    # should suppress "green" at pos 17. Sort by (start, -length) so longest
    # match at a given position wins, then filter overlaps left-to-right.
    matches.sort(key=lambda x: (x[0], -len(x[1])))
    filtered = []
    claimed_end = -1
    for pos, color_name, hex_value in matches:
        if pos < claimed_end:
            continue  # overlaps a previously-kept longer match
        filtered.append((pos, color_name, hex_value))
        claimed_end = pos + len(color_name)
    matches = filtered

    updated = dict(palette)
    for pos, color_name, hex_value in matches:
        color_end = pos + len(color_name)
        # Look at words AFTER the color (distance 0-25 chars, up to a punctuation
        # break). "white trim" → look at " trim, dark gr..." → finds "trim".
        # Fallback: look AT/BEFORE the color for material hints ("red brick",
        # "oak siding", "stucco walls" where the material names are the hint).
        after = edit_request[color_end : color_end + 25].lower()
        # Truncate at the next comma/semicolon/period — those separate clauses.
        for sep in (",", ";", "."):
            if sep in after:
                after = after.split(sep)[0]
                break

        before = edit_request[max(0, pos - 25): pos].lower()
        for sep in (",", ";", "."):
            if sep in before:
                before = before.rsplit(sep, 1)[-1]

        # Find the closest matching key hint. Score each (key, hint) combo by
        # how close its hint word is to the color. Prefer words AFTER the
        # color (primary convention: "white trim" not "trim white").
        best_key = None
        best_distance = 9999
        for key, hints in _KEY_HINTS:
            for h in hints:
                # Check after-text first with a low penalty
                if h in after:
                    d = after.find(h)
                    if d < best_distance:
                        best_distance = d
                        best_key = key
                # Then before-text with a higher penalty (distance offset)
                if h in before:
                    d = 100 + (len(before) - before.rfind(h))
                    if d < best_distance:
                        best_distance = d
                        best_key = key

        # If no hint found, default to ext_wall (most common target)
        key_assigned = best_key or "ext_wall"
        updated[key_assigned] = hex_value
        logger.info(
            "palette hint: %r → %s = %s (after=%r, before=%r)",
            color_name, key_assigned, hex_value, after, before,
        )

    return updated


# ── Merge ──────────────────────────────────────────────────────────────────

def merge_to_spec(
    brief: Brief,
    layout: Layout,
    facade: Facade,
    mep: MEPStrategy,
) -> BuildingSpec:
    """
    Combine the 4 agent outputs into the single dict that generate.py takes.

    The target shape (same as today's single-call output) is:
    {
      "name": str,
      "floors": [...],              # straight from layout
      "metadata": {
        "architectural_style": str,
        "style_notes": str,
        "style_palette": {...},     # facade can override brief
        "front_elevation": str,
        "location": str,
        "exterior_features": [...], # from facade
        "mep_strategy": {...},      # nested so generate.py can ignore if not ready
        ...
      }
    }
    """
    # Facade's palette takes precedence over brief's (facade agent gets last say
    # on colors; brief agent's palette is just a starting guess).
    palette = {**brief.style_palette, **(facade.style_palette or {})}

    metadata: Dict[str, Any] = {
        "architectural_style": brief.architectural_style,
        "style_notes": brief.style_notes,
        "style_palette": palette,
        "front_elevation": brief.front_elevation,
        "program": brief.program,
        "total_sqft": brief.total_sqft,
        "exterior_features": [f.model_dump() for f in facade.exterior_features],
        "mep_strategy": mep.model_dump(),
        "rationale": {
            "layout": layout.rationale,
            "facade": facade.rationale,
            "mep": mep.rationale,
        },
    }
    if brief.location:
        metadata["location"] = brief.location
    if brief.climate_zone:
        metadata["climate_zone"] = brief.climate_zone
    if brief.budget_usd:
        metadata["budget_usd"] = brief.budget_usd
    if brief.constraints:
        metadata["constraints"] = brief.constraints

    return BuildingSpec(
        name=brief.name,
        floors=layout.floors,
        metadata=metadata,
    )


# ── Main pipeline ──────────────────────────────────────────────────────────

def generate_building_multi_agent(
    user_prompt: str,
    *,
    parallel_specialists: bool = True,
) -> PipelineResult:
    """
    Run the full 4-agent pipeline.

    Args:
        user_prompt: The user's natural-language building request.
        parallel_specialists: If True, run Facade + MEP agents concurrently
            (saves 2-4s of wall-clock time). If False, run sequentially
            (easier debugging, half the concurrency cost on rate limits).

    Returns:
        PipelineResult with spec, intermediate outputs, and run metrics.

    Raises:
        Anything the underlying Anthropic client raises — rate limits,
        context overflow, auth errors. The orchestrator does NOT swallow
        these; the caller needs to decide whether to retry.
    """
    start = time.time()
    runs = []

    # Step 1: Brief (orchestrator, Sonnet)
    logger.info("Step 1/4: Brief agent reading user prompt...")
    brief, brief_run = run_brief_agent(user_prompt)
    runs.append(brief_run)
    logger.info("  → style=%r, %d floors, %s sqft",
                brief.architectural_style, brief.floors_count, brief.total_sqft)

    # Step 2: Layout (specialist, Haiku)
    logger.info("Step 2/4: Layout agent placing rooms...")
    layout, layout_run = run_layout_agent(brief)
    runs.append(layout_run)
    logger.info("  → %d floors, %.1fm × %.1fm footprint",
                len(layout.floors), layout.footprint_width, layout.footprint_depth)

    # Step 3 + 4: Facade + MEP (specialists, can run in parallel)
    if parallel_specialists:
        logger.info("Step 3+4/4: Facade + MEP agents running in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            facade_future = pool.submit(run_facade_agent, brief, layout)
            mep_future = pool.submit(run_mep_agent, brief, layout)
            facade, facade_run = facade_future.result()
            mep, mep_run = mep_future.result()
    else:
        logger.info("Step 3/4: Facade agent composing exterior features...")
        facade, facade_run = run_facade_agent(brief, layout)
        logger.info("Step 4/4: MEP agent picking HVAC strategy...")
        mep, mep_run = run_mep_agent(brief, layout)

    runs.extend([facade_run, mep_run])
    logger.info("  → %d exterior features, hvac=%s",
                len(facade.exterior_features), mep.hvac_type)

    # Merge
    spec = merge_to_spec(brief, layout, facade, mep)
    total = time.time() - start
    logger.info("Pipeline done in %.2fs (%d agent calls)", total, len(runs))

    return PipelineResult(
        spec=spec,
        brief=brief,
        layout=layout,
        facade=facade,
        mep=mep,
        runs=runs,
        total_duration_s=round(total, 2),
    )


# ── Edit API ───────────────────────────────────────────────────────────────
# Surgical edits — re-run the minimum number of agents required for the
# user's change, and never cascade unless the caller opts in.
#
# Targets (fine-grained, in order of cost from cheapest to most expensive):
#
#   palette     - Just rewrite style_palette (no LLM call, instant)
#                 "Change ext_wall to brick red"
#                 Updates only: metadata.style_palette
#
#   materials   - Palette + material-only facade feature tweaks (1 Haiku call)
#                 "Swap clapboard for brick"
#                 Updates: facade.style_palette + facade feature materials
#                 Keeps: facade feature list unchanged
#
#   facade      - Full facade re-design (1 Haiku call, ~5-7s)
#                 "Add a cupola and change to Italianate style"
#                 Updates: exterior_features list + palette
#                 Keeps: layout, MEP, brief
#
#   mep         - MEP strategy re-pick (1 Haiku call, ~4-5s)
#                 "Use gas heating instead"
#                 Updates: mep_strategy
#                 Keeps: everything else
#
#   layout      - Layout re-plan (1 Haiku call, ~5-6s)
#                 "Add a 4th bedroom" / "Move kitchen to east side"
#                 Updates: floors/rooms
#                 Keeps: brief, facade, MEP — UNLESS cascade=True, then rerun
#                 facade + MEP against new layout
#
#   brief       - Brief update (1 Sonnet call, ~4-5s)
#                 "Actually make it 1800 sqft" / "Change style to Colonial"
#                 Updates: brief (style, program, front_elevation, etc.)
#                 Keeps: layout, facade, MEP — UNLESS cascade=True, then rerun
#                 all downstream specialists against the new brief
#
# cascade parameter:
#   - False (default): edit ONLY the target. Faster, preserves everything else.
#   - True: edit target AND all downstream agents whose outputs logically
#     depend on it. Use when the edit genuinely changes things downstream.
#
# Recommended UX: default to cascade=False. If the user isn't satisfied with
# the result because the untouched parts now look wrong against the edit
# (e.g. they changed brief to "Colonial" but the facade still has a turret),
# surface a "Re-run affected areas" button that does cascade=True.

def edit_building(
    previous: PipelineResult,
    edit_request: str,
    *,
    target: str,
    cascade: bool = False,
) -> PipelineResult:
    """
    Re-run ONLY the targeted specialist by default. Preserves everything else.

    Args:
        previous: The last PipelineResult.
        edit_request: Plain-text edit. Keep it short and specific.
        target: One of 'palette', 'materials', 'facade', 'mep', 'layout', 'brief'.
        cascade: If True, also rerun any downstream specialists that logically
            depend on the edited layer. Default False — user must opt in.

    Returns:
        A new PipelineResult. AgentRun records from un-re-run agents are
        preserved verbatim, so you can see which runs were fresh vs cached.
    """
    start = time.time()
    runs = list(previous.runs)

    brief = previous.brief
    layout = previous.layout
    facade = previous.facade
    mep = previous.mep

    def _replace_run(label: str, new_run):
        nonlocal runs
        runs = [r for r in runs if r.agent != label]
        runs.append(new_run)

    # ── palette: pure metadata edit, no LLM ───────────────────────────────
    if target == "palette":
        logger.info("Edit: palette-only rewrite (no LLM): %r", edit_request)
        # Parse the edit request for palette hints. Very simple heuristics —
        # for anything more sophisticated, use 'materials' instead.
        palette = dict(facade.style_palette or brief.style_palette)
        palette = _apply_palette_hints(palette, edit_request)
        facade = facade.model_copy(update={"style_palette": palette})
        # No run record to update — no agent was called.
        logger.info("  → palette updated: %s", palette)

    # ── materials: palette + feature color tweaks via facade agent ───────
    elif target == "materials":
        logger.info("Edit: materials-only facade rewrite: %r", edit_request)
        original_notes = brief.style_notes
        brief.style_notes = (
            f"{original_notes}\n\n"
            f"MATERIALS EDIT (keep exterior_features list EXACTLY the same, "
            f"only change style_palette and feature color_key params): {edit_request}"
        )
        try:
            new_facade, run = run_facade_agent(brief, layout)
        finally:
            brief.style_notes = original_notes
        # Sanity check — if the agent returned a different feature COUNT,
        # warn but accept. It may have tweaked feature params which is fine.
        if len(new_facade.exterior_features) != len(facade.exterior_features):
            logger.warning(
                "Materials edit changed feature count (%d → %d). Expected same count.",
                len(facade.exterior_features), len(new_facade.exterior_features),
            )
        facade = new_facade
        _replace_run("facade_agent", run)

    # ── facade: re-design exterior features ──────────────────────────────
    elif target == "facade":
        logger.info("Edit: re-running facade agent: %r", edit_request)
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nFACADE EDIT: {edit_request}"
        try:
            facade, run = run_facade_agent(brief, layout)
        finally:
            brief.style_notes = original_notes
        _replace_run("facade_agent", run)

    # ── mep: re-pick HVAC/plumbing/electrical strategy ───────────────────
    elif target == "mep":
        logger.info("Edit: re-running MEP agent: %r", edit_request)
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nMEP EDIT: {edit_request}"
        try:
            mep, run = run_mep_agent(brief, layout)
        finally:
            brief.style_notes = original_notes
        _replace_run("mep_agent", run)

    # ── layout: re-plan rooms ────────────────────────────────────────────
    elif target == "layout":
        logger.info("Edit: re-running layout agent (cascade=%s): %r", cascade, edit_request)
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nLAYOUT EDIT: {edit_request}"
        try:
            layout, run = run_layout_agent(brief)
        finally:
            brief.style_notes = original_notes
        _replace_run("layout_agent", run)

        if cascade:
            logger.info("  cascade=True: re-running facade + MEP against new layout")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                facade_future = pool.submit(run_facade_agent, brief, layout)
                mep_future = pool.submit(run_mep_agent, brief, layout)
                facade, frun = facade_future.result()
                mep, mrun = mep_future.result()
            _replace_run("facade_agent", frun)
            _replace_run("mep_agent", mrun)

    # ── brief: update the design intent ──────────────────────────────────
    elif target == "brief":
        logger.info("Edit: re-running brief agent (cascade=%s): %r", cascade, edit_request)
        brief_user_prompt = (
            f"PREVIOUS BRIEF:\n{brief.model_dump_json(indent=2)}\n\n"
            f"EDIT REQUEST:\n{edit_request}\n\n"
            "Produce an updated Brief JSON. Change ONLY what the edit request "
            "asks for. Keep everything else (program, palette, floors_count, "
            "front_elevation, etc.) exactly as it was unless the edit forces a "
            "change. Be conservative — if unsure, leave it alone."
        )
        brief, run = run_brief_agent(brief_user_prompt)
        _replace_run("brief_agent", run)

        if cascade:
            logger.info("  cascade=True: re-running layout + facade + MEP against new brief")
            layout, lrun = run_layout_agent(brief)
            _replace_run("layout_agent", lrun)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                facade_future = pool.submit(run_facade_agent, brief, layout)
                mep_future = pool.submit(run_mep_agent, brief, layout)
                facade, frun = facade_future.result()
                mep, mrun = mep_future.result()
            _replace_run("facade_agent", frun)
            _replace_run("mep_agent", mrun)

    else:
        raise ValueError(
            f"Unknown edit target: {target!r}. "
            "Use palette, materials, facade, mep, layout, or brief."
        )

    spec = merge_to_spec(brief, layout, facade, mep)
    total = time.time() - start
    logger.info("Edit done in %.2fs (target=%s, cascade=%s)", total, target, cascade)

    return PipelineResult(
        spec=spec,
        brief=brief,
        layout=layout,
        facade=facade,
        mep=mep,
        runs=runs,
        total_duration_s=round(total, 2),
    )
    # ── Layout-first entry point ────────────────────────────────────────────
# For the use case where the user (or an upstream tool) provides the floor
# plan layout already, and we just need to add style + facade + MEP on top.
#
# The Brief Agent runs in a stripped-down mode: it doesn't lay out rooms
# (we already have those) — it just classifies typology, picks a style,
# generates a palette, and fills in metadata. Layout Agent is skipped.

def generate_building_from_layout(
    layout: Layout,
    style_hint: str = "",
    *,
    name: str = "Building",
    front_elevation: str = "south",
    location: str | None = None,
    parallel_specialists: bool = True,
) -> PipelineResult:
    """
    Generate a building when the floor plan is already known.

    Skips the Layout Agent entirely. Runs Brief in "describe this layout" mode
    to get style + palette, then runs Facade and MEP against the user-provided
    layout, then merges into a BuildingSpec ready for generate.py.

    Args:
        layout: An already-validated Layout object (floors + rooms).
        style_hint: Short style hint, e.g. "modern", "victorian", "class A office".
                    The Brief Agent uses this to fill in architectural_style and
                    style_palette. Empty string = let the agent guess.
        name: Building display name.
        front_elevation: Which side has the main entry. Affects Facade placement.
        location: Optional location string. Affects MEP via climate inference.
        parallel_specialists: Run Facade + MEP concurrently (default True).

    Returns:
        PipelineResult — same shape as generate_building_multi_agent(), so all
        downstream code (rendering, edit API) works identically.
    """
    start = time.time()
    runs = []

    # Compute total sqft from the layout for the brief.
    total_sqm = sum(
        room.width * room.depth
        for floor in layout.floors
        for room in floor.rooms
    )
    total_sqft = total_sqm / 0.0929
    floors_count = len(layout.floors)

    # Build a synthetic prompt for the Brief Agent.
    program_seed = sorted({room.name.split()[0].lower() for f in layout.floors for room in f.rooms})
    prompt_parts = [f"A {floors_count}-floor building, approximately {total_sqft:.0f} sqft."]
    if style_hint:
        prompt_parts.append(f"Style: {style_hint}.")
    if location:
        prompt_parts.append(f"Location: {location}.")
    prompt_parts.append(f"The layout is fixed and contains: {', '.join(program_seed)}.")
    prompt_parts.append(
        "Produce a Brief that classifies the typology, picks an architectural style "
        "and palette appropriate to it. Do NOT redesign the layout — it's already set."
    )
    synthetic_prompt = " ".join(prompt_parts)

    logger.info("Step 1/3: Brief agent (style + palette only, layout pre-supplied)...")
    brief, brief_run = run_brief_agent(synthetic_prompt)
    runs.append(brief_run)
    # Override fields the agent might have guessed wrong, since we have ground truth.
    brief.name = name
    brief.floors_count = floors_count
    brief.total_sqft = total_sqft
    brief.front_elevation = front_elevation
    if location:
        brief.location = location
    logger.info("  → typology=%s style=%r palette keys: %s",
                getattr(brief, "typology_key", "(none)"),
                brief.architectural_style, list(brief.style_palette.keys()))

    # Steps 2 + 3: Facade + MEP in parallel against the supplied layout.
    if parallel_specialists:
        logger.info("Step 2+3/3: Facade + MEP agents running in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            facade_future = pool.submit(run_facade_agent, brief, layout)
            mep_future = pool.submit(run_mep_agent, brief, layout)
            facade, facade_run = facade_future.result()
            mep, mep_run = mep_future.result()
    else:
        logger.info("Step 2/3: Facade agent composing exterior features...")
        facade, facade_run = run_facade_agent(brief, layout)
        logger.info("Step 3/3: MEP agent picking HVAC strategy...")
        mep, mep_run = run_mep_agent(brief, layout)

    runs.extend([facade_run, mep_run])
    logger.info("  → %d exterior features, hvac=%s",
                len(facade.exterior_features), mep.hvac_type)

    spec = merge_to_spec(brief, layout, facade, mep)
    total = time.time() - start
    logger.info("Layout-first pipeline done in %.2fs (%d agent calls)", total, len(runs))

    return PipelineResult(
        spec=spec,
        brief=brief,
        layout=layout,
        facade=facade,
        mep=mep,
        runs=runs,
        total_duration_s=round(total, 2),
    )
