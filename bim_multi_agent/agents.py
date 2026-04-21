"""
agents.py — The 4 agent call functions.

Each agent is a single Claude API call with a tight system prompt, a narrow
input, and a strict JSON output schema. No tool use, no function calling —
just prompt engineering + JSON parsing.

This simplicity is deliberate:
 - Easier to debug (one call in, one JSON out)
 - Easier to A/B test prompts
 - No framework lock-in (LangChain/CrewAI/AutoGen not needed)
 - Cheap to retry a single agent when something goes wrong

Each agent function returns the parsed Pydantic model plus an AgentRun
record for debugging / metrics.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import anthropic

from .schemas import (
    AgentRun,
    Brief,
    Facade,
    Layout,
    MEPStrategy,
)
from .prompts import (
    BRIEF_AGENT_PROMPT,
    FACADE_AGENT_PROMPT_TEMPLATE,
    LAYOUT_AGENT_PROMPT,
    MEP_AGENT_PROMPT,
)
from .component_library import build_primitive_reference

logger = logging.getLogger(__name__)


# ── Model config ────────────────────────────────────────────────────────────
# Orchestrator (Brief) uses Sonnet because it needs more world knowledge:
# style interpretation, climate lookup, program sizing.
# Specialists use Haiku because they're narrow tasks with structured output.
# Override per-agent if a specialist underperforms on Haiku.

ORCHESTRATOR_MODEL = os.getenv("BIM_ORCHESTRATOR_MODEL", "claude-sonnet-4-20250514")
SPECIALIST_MODEL = os.getenv("BIM_SPECIALIST_MODEL", "claude-haiku-4-5-20251001")

# Max tokens for each agent's output. Brief and MEP are small, Layout and
# Facade can be larger. All generous — we'd rather spend tokens than truncate.
MAX_TOKENS_BRIEF = 1500
MAX_TOKENS_LAYOUT = 4000
MAX_TOKENS_FACADE = 2500
MAX_TOKENS_MEP = 1500


# ── Shared helpers ──────────────────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    """Lazy client init so imports don't fail without an API key."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to your environment before running."
        )
    return anthropic.Anthropic(api_key=api_key)


def _extract_json(text: str) -> str:
    """
    Pull the JSON object out of a model response.

    Handles three cases:
    1. Response is already clean JSON → return as-is.
    2. Response has ```json ... ``` code fences → strip them.
    3. Response has trailing/leading commentary → find the first { ... last }.

    Claude with clear instructions usually returns (1), but (2) and (3)
    happen often enough that parsing must be robust.
    """
    stripped = text.strip()

    # Case 2: code fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1)

    # Case 3: find outermost braces
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise ValueError(f"No JSON object found in response: {stripped[:200]}")
    return stripped[first : last + 1]


def _call_agent(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    max_tokens: int,
    label: str,
) -> tuple[dict, AgentRun]:
    """
    Single Claude API call, JSON response. Returns (parsed_json, run_record).

    Retries once on JSON parse failure — Claude occasionally emits text
    before the JSON on its first attempt; a simple retry with a reminder
    fixes this reliably.
    """
    client = _get_client()
    start = time.time()

    def _try_once(extra_user: Optional[str] = None) -> tuple[str, int, int]:
        messages = [{"role": "user", "content": user_message}]
        if extra_user:
            messages.append({"role": "assistant", "content": "{"})  # prefill nudge
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        text_parts = [b.text for b in resp.content if b.type == "text"]
        text = "".join(text_parts)
        if extra_user:
            text = "{" + text  # merge the prefill back in
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    try:
        text, in_tok, out_tok = _try_once()
        json_text = _extract_json(text)
        parsed = json.loads(json_text)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("%s: first attempt failed JSON parse (%s), retrying with prefill", label, e)
        text, in_tok, out_tok = _try_once(extra_user="force-json")
        json_text = _extract_json(text)
        parsed = json.loads(json_text)

    duration = time.time() - start
    run = AgentRun(
        agent=label,
        duration_s=round(duration, 2),
        input_tokens=in_tok,
        output_tokens=out_tok,
        raw_output=parsed,
    )
    logger.info("%s: done in %.2fs (%d in / %d out tok)", label, duration, in_tok, out_tok)
    return parsed, run


# ── Agent 1: Brief ─────────────────────────────────────────────────────────

def run_brief_agent(user_prompt: str) -> tuple[Brief, AgentRun]:
    """
    Read the user's natural-language request, produce a structured Brief.
    This is the orchestrator agent — it sets the design intent everything else reads.
    """
    parsed, run = _call_agent(
        system_prompt=BRIEF_AGENT_PROMPT,
        user_message=f"USER REQUEST:\n{user_prompt}\n\nProduce the Brief JSON.",
        model=ORCHESTRATOR_MODEL,
        max_tokens=MAX_TOKENS_BRIEF,
        label="brief_agent",
    )
    brief = Brief(**parsed)
    return brief, run


# ── Agent 2: Layout ────────────────────────────────────────────────────────

def run_layout_agent(brief: Brief) -> tuple[Layout, AgentRun]:
    """Specialist — emits the floor plan as rooms with coordinates."""
    user_message = (
        "BRIEF:\n"
        f"{brief.model_dump_json(indent=2)}\n\n"
        "Produce the Layout JSON. Remember: rooms must share walls (no gaps), "
        "public rooms on the front_elevation side, total area ≈ brief.total_sqft."
    )
    parsed, run = _call_agent(
        system_prompt=LAYOUT_AGENT_PROMPT,
        user_message=user_message,
        model=SPECIALIST_MODEL,
        max_tokens=MAX_TOKENS_LAYOUT,
        label="layout_agent",
    )
    layout = Layout(**parsed)
    return layout, run


# ── Agent 3: Facade ────────────────────────────────────────────────────────

def run_facade_agent(brief: Brief, layout: Layout) -> tuple[Facade, AgentRun]:
    """Specialist — emits exterior features from the primitive library."""
    # Inject the live primitive registry into the prompt
    primitive_reference = build_primitive_reference()
    system_prompt = FACADE_AGENT_PROMPT_TEMPLATE.replace(
        "{primitive_reference}", primitive_reference
    )

    user_message = (
        "BRIEF:\n"
        f"{brief.model_dump_json(indent=2)}\n\n"
        "LAYOUT (for positioning reference):\n"
        f"{layout.model_dump_json(indent=2)}\n\n"
        "Produce the Facade JSON with 3-8 exterior_features matching the style."
    )
    parsed, run = _call_agent(
        system_prompt=system_prompt,
        user_message=user_message,
        model=SPECIALIST_MODEL,
        max_tokens=MAX_TOKENS_FACADE,
        label="facade_agent",
    )
    facade = Facade(**parsed)
    return facade, run


# ── Agent 4: MEP ───────────────────────────────────────────────────────────

def run_mep_agent(brief: Brief, layout: Layout) -> tuple[MEPStrategy, AgentRun]:
    """Specialist — picks HVAC / plumbing / electrical strategy."""
    user_message = (
        "BRIEF:\n"
        f"{brief.model_dump_json(indent=2)}\n\n"
        "LAYOUT (for room program reference):\n"
        f"{layout.model_dump_json(indent=2)}\n\n"
        "Produce the MEPStrategy JSON."
    )
    parsed, run = _call_agent(
        system_prompt=MEP_AGENT_PROMPT,
        user_message=user_message,
        model=SPECIALIST_MODEL,
        max_tokens=MAX_TOKENS_MEP,
        label="mep_agent",
    )
    mep = MEPStrategy(**parsed)
    return mep, run
