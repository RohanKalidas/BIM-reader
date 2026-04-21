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

from agents import run_brief_agent, run_facade_agent, run_layout_agent, run_mep_agent
from schemas import (
    Brief,
    BuildingSpec,
    Facade,
    Layout,
    MEPStrategy,
    PipelineResult,
)

logger = logging.getLogger(__name__)


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
# This is the thing monolithic generation can't do cleanly. Rerun ONE
# specialist with an edit intent, keep the other three outputs, remerge.

def edit_building(
    previous: PipelineResult,
    edit_request: str,
    *,
    target: str,
) -> PipelineResult:
    """
    Re-run a single specialist based on an edit request.

    Args:
        previous: The last PipelineResult (must have .brief, .layout, etc.)
        edit_request: Plain-text edit. "Change facade to brick." "Redo MEP
            with a heat pump." "Move the bathroom next to the bedroom."
        target: Which specialist to re-run. One of 'brief', 'layout',
            'facade', 'mep'. If 'layout' is chosen, facade and MEP are
            also re-run (they depend on layout); otherwise only the
            targeted specialist.

    Returns:
        A new PipelineResult. The un-re-run agents' outputs are copied
        verbatim from `previous`. Their AgentRun records are preserved
        too (so you can see which calls are fresh vs cached).

    Design note: the edit_request is injected into the chosen agent's
    user message with clear "EDIT REQUEST" framing. The agent sees the
    previous output and the user's desired change, so it can make a
    minimal revision rather than a full regeneration.
    """
    start = time.time()
    runs = list(previous.runs)  # start with cached runs; replace targeted ones

    brief = previous.brief
    layout = previous.layout
    facade = previous.facade
    mep = previous.mep

    def _replace_run(label: str, new_run):
        """Replace the matching run record with the fresh one."""
        nonlocal runs
        runs = [r for r in runs if r.agent != label]
        runs.append(new_run)

    if target == "brief":
        logger.info("Edit: re-running brief agent with request: %r", edit_request)
        # We pass the previous brief + edit_request so the agent sees its own output.
        brief_user_prompt = (
            f"PREVIOUS BUILDING REQUEST:\n(derived brief: {brief.model_dump_json(indent=2)})\n\n"
            f"EDIT REQUEST FROM USER:\n{edit_request}\n\n"
            "Produce an updated Brief JSON that incorporates the edit while keeping "
            "everything else unchanged."
        )
        brief, run = run_brief_agent(brief_user_prompt)
        _replace_run("brief_agent", run)
        # Brief changed → re-run everything downstream
        target = "layout"  # fallthrough

    if target == "layout":
        logger.info("Edit: re-running layout (and downstream specialists) for: %r", edit_request)
        # Inject the edit into the brief's style_notes so the layout agent sees
        # the change without us needing a richer API. Restore after the call.
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nEDIT FROM PREVIOUS VERSION: {edit_request}"
        try:
            layout, run = run_layout_agent(brief)
        finally:
            brief.style_notes = original_notes
        _replace_run("layout_agent", run)
        # Layout changed → rerun facade + MEP
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            facade_future = pool.submit(run_facade_agent, brief, layout)
            mep_future = pool.submit(run_mep_agent, brief, layout)
            facade, frun = facade_future.result()
            mep, mrun = mep_future.result()
        _replace_run("facade_agent", frun)
        _replace_run("mep_agent", mrun)

    elif target == "facade":
        logger.info("Edit: re-running facade agent: %r", edit_request)
        # Inject edit_request into the brief's style_notes so the facade agent
        # sees the change. Don't touch layout.
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nFACADE EDIT: {edit_request}"
        facade, run = run_facade_agent(brief, layout)
        brief.style_notes = original_notes
        _replace_run("facade_agent", run)

    elif target == "mep":
        logger.info("Edit: re-running MEP agent: %r", edit_request)
        original_notes = brief.style_notes
        brief.style_notes = f"{original_notes}\n\nMEP EDIT: {edit_request}"
        mep, run = run_mep_agent(brief, layout)
        brief.style_notes = original_notes
        _replace_run("mep_agent", run)

    else:
        raise ValueError(f"Unknown edit target: {target!r}. Use brief, layout, facade, or mep.")

    spec = merge_to_spec(brief, layout, facade, mep)
    total = time.time() - start
    logger.info("Edit done in %.2fs (target=%s)", total, target)

    return PipelineResult(
        spec=spec,
        brief=brief,
        layout=layout,
        facade=facade,
        mep=mep,
        runs=runs,
        total_duration_s=round(total, 2),
    )
