"""
batch_backfill.py — Same job as backfill.py, but using Claude's Message
Batches API instead of synchronous calls.

Why batch:
  - 50% cheaper per token (~$3-4 for 21k components vs ~$7)
  - No rate limit nightmares (batch has its own much higher limits)
  - Async — you submit, walk away, come back when done

Usage:
    # Step 1: submit batches (creates jobs, returns immediately)
    python -m bim_multi_agent.batch_backfill submit

    # Step 2: poll + collect results (re-runnable, resumes from state file)
    python -m bim_multi_agent.batch_backfill collect

    # Optional: just check status without polling forever
    python -m bim_multi_agent.batch_backfill status

State is persisted in /tmp/bim_batch_state.json so you can run
collect/status from any shell session and it picks up where it left off.

Notes:
  - Each batch holds up to ~5,000 requests (conservative; max is 100k).
  - Batches "end" when all requests in them complete. Most finish in
    minutes; the API's hard cap is 24 hours.
  - results are streamed back from the batch's results_url.
  - Each request's custom_id is the component's database ID, so result
    mapping is exact.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

import psycopg2.extras

# Local imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.db import get_db_connection  # noqa: E402

from .canonical_vocab import (
    CANONICAL_NAMES, STYLE_TAGS, CONTEXT_TAGS, QUALITY_CLASS,
)
from .classifier import _system_prompt  # reuse the same prompt

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS_PER_REQUEST = 300
REQUESTS_PER_BATCH = 5000     # split 21k into ~5 batches
STATE_FILE = "/tmp/bim_batch_state.json"
POLL_INTERVAL_SEC = 30        # initial poll interval; backs off

# Same TARGET_CATEGORIES filter as backfill.py — we only want fixtures.
TARGET_CATEGORIES = (
    "IfcFurniture", "IfcFurnishingElement",
    "IfcSanitaryTerminal",
    "IfcElectricAppliance",
    "IfcLightFixture",
    "IfcDoor", "IfcWindow",
    "IfcOutlet", "IfcSwitchingDevice",
    "IfcCableSegment", "IfcCableCarrierSegment",
    "IfcElectricDistributionBoard", "IfcElectricFlowStorageDevice",
    "IfcAirTerminal", "IfcAirTerminalBox",
    "IfcDuctSegment", "IfcDuctFitting",
    "IfcUnitaryEquipment", "IfcBoiler", "IfcChiller", "IfcFan",
    "IfcPipeSegment", "IfcPipeFitting", "IfcPump", "IfcValve",
    "IfcFlowMeter", "IfcTank",
    "IfcFireSuppressionTerminal", "IfcAlarm",
    "IfcMedicalDevice",
    "IfcStair", "IfcStairFlight", "IfcRailing",
    "IfcCovering", "IfcCurtainWall",
)


# ── State management ──────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"batches": [], "submitted_at": None, "collected_batch_ids": []}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Build a single classification request ─────────────────────────────────

def _build_request(row: dict) -> Request:
    """Convert a DB row into a Batch API Request."""
    user_message = (
        f"ifc_class: {row['category']}\n"
        f"family_name: {row.get('family_name') or '(empty)'}\n"
        f"type_name: {row.get('type_name') or '(empty)'}\n"
        f"dims: "
        f"{int(row['width_mm']) if row.get('width_mm') else '?'} W × "
        f"{int(row['length_mm']) if row.get('length_mm') else '?'} D × "
        f"{int(row['height_mm']) if row.get('height_mm') else '?'} H"
    )
    return Request(
        custom_id=f"comp_{row['id']}",   # critical: how we map results back
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_TOKENS_PER_REQUEST,
            system=_system_prompt(),
            messages=[{"role": "user", "content": user_message}],
        ),
    )


# ── Submit ────────────────────────────────────────────────────────────────

def submit_batches(library_only: bool = False, limit: Optional[int] = None) -> None:
    """
    Query unclassified components, build batches of REQUESTS_PER_BATCH
    requests each, submit them all, save batch IDs to the state file.
    """
    state = _load_state()
    if state["batches"]:
        print(f"State file already has {len(state['batches'])} batch(es).")
        print("Run 'collect' to fetch their results, or delete the state file:")
        print(f"  rm {STATE_FILE}")
        return

    cat_list = ",".join(f"'{c}'" for c in TARGET_CATEGORIES)
    base_query = f"""
        SELECT c.id, c.category, c.family_name, c.type_name,
               c.width_mm, c.height_mm, c.length_mm
        FROM components c
        {"JOIN library l ON l.component_id = c.id" if library_only else ""}
        WHERE c.classified_at IS NULL
          AND c.category IN ({cat_list})
        ORDER BY c.id
    """
    if limit:
        base_query += f" LIMIT {limit}"

    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute(base_query)
        rows = [dict(r) for r in cursor.fetchall()]

    if not rows:
        print("No unclassified components to process.")
        return

    n_total = len(rows)
    n_batches = (n_total + REQUESTS_PER_BATCH - 1) // REQUESTS_PER_BATCH
    est_cost = n_total * 0.00015  # Haiku batch ~50% off, ~$0.00015/call
    print(f"Submitting {n_total:,} components in {n_batches} batch(es)")
    print(f"  Model:           {MODEL}")
    print(f"  Estimated cost:  ${est_cost:.2f} (50% batch discount applied)")
    print(f"  Estimated time:  1 hour to 24 hours (most batches finish < 1 hr)")
    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    state["batches"] = []
    state["submitted_at"] = time.time()

    for i in range(0, n_total, REQUESTS_PER_BATCH):
        chunk = rows[i : i + REQUESTS_PER_BATCH]
        requests = [_build_request(row) for row in chunk]
        batch_idx = i // REQUESTS_PER_BATCH + 1
        print(f"  Submitting batch {batch_idx}/{n_batches} ({len(requests):,} requests)...")
        try:
            batch = client.messages.batches.create(requests=requests)
            print(f"    ✓ batch id: {batch.id}")
            state["batches"].append({
                "id": batch.id,
                "submitted_at": time.time(),
                "size": len(requests),
                "component_ids": [r["id"] for r in chunk],
                "status": "in_progress",
                "results_collected": False,
            })
            _save_state(state)
        except Exception as e:
            print(f"    ✗ failed: {e}")
            logger.exception("batch submission failed")
            return

    print(f"\nSubmitted {len(state['batches'])} batch(es). State saved to {STATE_FILE}")
    print("Run 'collect' to retrieve results once they're ready.")
    print("Run 'status' to check progress without blocking.")


# ── Status check ──────────────────────────────────────────────────────────

def check_status() -> None:
    """Print status of each batch without blocking."""
    state = _load_state()
    if not state["batches"]:
        print(f"No batches in state file ({STATE_FILE}). Did you run 'submit'?")
        return

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    print(f"Checking {len(state['batches'])} batch(es)...\n")

    for entry in state["batches"]:
        batch_id = entry["id"]
        try:
            batch = client.messages.batches.retrieve(batch_id)
            counts = batch.request_counts
            print(f"  {batch_id}")
            print(f"    status:       {batch.processing_status}")
            print(f"    succeeded:    {counts.succeeded:,} / {entry['size']:,}")
            print(f"    errored:      {counts.errored}")
            print(f"    processing:   {counts.processing}")
            if entry.get("results_collected"):
                print(f"    [✓ results already pulled into DB]")
            print()
            entry["status"] = batch.processing_status
        except Exception as e:
            print(f"  {batch_id}: ERROR - {e}\n")

    _save_state(state)


# ── Collect ───────────────────────────────────────────────────────────────

def _validate_and_persist(component_id: int, parsed: dict) -> bool:
    """Apply the same validation as classifier.py, write to DB."""
    canonical = parsed.get("canonical_name", "other")
    if canonical not in CANONICAL_NAMES:
        canonical = "other"

    style_tags = [t for t in parsed.get("style_tags", []) if t in STYLE_TAGS]
    if not style_tags:
        style_tags = ["any"]

    context_tags = [t for t in parsed.get("context_tags", []) if t in CONTEXT_TAGS]
    if not context_tags:
        context_tags = ["any"]

    quality_class = parsed.get("quality_class", "standard")
    if quality_class not in QUALITY_CLASS:
        quality_class = "standard"

    try:
        with get_db_connection() as (conn, cursor):
            cursor.execute("""
                UPDATE components
                SET canonical_name = %s,
                    style_tags     = %s,
                    context_tags   = %s,
                    quality_class  = %s,
                    classified_at  = NOW()
                WHERE id = %s
            """, (canonical, style_tags, context_tags, quality_class, component_id))
        return True
    except Exception as e:
        logger.warning("DB write failed for component %d: %s", component_id, e)
        return False


def collect_results(wait: bool = True) -> None:
    """
    Poll batches until they all 'end', then stream results into the DB.
    If wait=False, only collect from already-ended batches.
    """
    state = _load_state()
    if not state["batches"]:
        print(f"No batches in state file ({STATE_FILE}). Did you run 'submit'?")
        return

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Wait loop
    if wait:
        poll_interval = POLL_INTERVAL_SEC
        while True:
            statuses = []
            for entry in state["batches"]:
                if entry.get("results_collected"):
                    statuses.append("done")
                    continue
                try:
                    batch = client.messages.batches.retrieve(entry["id"])
                    entry["status"] = batch.processing_status
                    statuses.append(batch.processing_status)
                    counts = batch.request_counts
                    print(f"  {entry['id'][:24]}... {batch.processing_status:>14}  "
                          f"({counts.succeeded:,}/{entry['size']:,} succeeded, "
                          f"{counts.errored} errored, {counts.processing} pending)")
                except Exception as e:
                    print(f"  {entry['id']}: poll error - {e}")
                    statuses.append("error")
            _save_state(state)

            if all(s in ("ended", "done") for s in statuses):
                print("\nAll batches finished. Pulling results...")
                break

            print(f"  ...waiting {poll_interval}s before next poll\n")
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.3, 300)  # exp backoff, max 5 min

    # Stream results into DB
    n_success = 0
    n_parse_err = 0
    n_request_err = 0
    n_db_err = 0

    for entry in state["batches"]:
        if entry.get("results_collected"):
            print(f"  {entry['id']}: already collected, skipping")
            continue

        try:
            batch = client.messages.batches.retrieve(entry["id"])
            if batch.processing_status != "ended":
                print(f"  {entry['id']}: status={batch.processing_status}, skipping")
                continue

            print(f"  {entry['id']}: streaming results...")
            for result in client.messages.batches.results(entry["id"]):
                custom_id = result.custom_id
                if not custom_id.startswith("comp_"):
                    continue
                component_id = int(custom_id.split("_", 1)[1])

                if result.result.type == "succeeded":
                    text = result.result.message.content[0].text.strip()
                    # Strip markdown fences if present
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                        text = text.strip()
                    try:
                        parsed = json.loads(text)
                        ok = _validate_and_persist(component_id, parsed)
                        if ok:
                            n_success += 1
                        else:
                            n_db_err += 1
                    except json.JSONDecodeError as e:
                        logger.warning("parse failed for %s: %s | text=%r",
                                       custom_id, e, text[:200])
                        n_parse_err += 1
                else:
                    n_request_err += 1
                    if n_request_err <= 5:
                        logger.warning("%s result type: %s", custom_id,
                                       result.result.type)

            entry["results_collected"] = True
            _save_state(state)
        except Exception as e:
            print(f"  ERROR streaming {entry['id']}: {e}")
            logger.exception("results stream failed")

    print()
    print("=" * 50)
    print(f"  Successful classifications: {n_success:,}")
    if n_parse_err:    print(f"  Parse errors:               {n_parse_err}")
    if n_request_err:  print(f"  Request errors:             {n_request_err}")
    if n_db_err:       print(f"  DB write errors:            {n_db_err}")
    print("=" * 50)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Batch backfill via Claude Message Batches API")
    sub = p.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="Create batches, exit")
    p_submit.add_argument("--library-only", action="store_true",
                          help="Only components in saved library")
    p_submit.add_argument("--limit", type=int,
                          help="Cap total components (testing)")

    sub.add_parser("status", help="Check batch status without polling")

    p_collect = sub.add_parser("collect", help="Poll + write results to DB")
    p_collect.add_argument("--no-wait", action="store_true",
                           help="Don't poll; only collect already-ended batches")

    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "submit":
        submit_batches(library_only=args.library_only, limit=args.limit)
    elif args.command == "status":
        check_status()
    elif args.command == "collect":
        collect_results(wait=not args.no_wait)
    else:
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
